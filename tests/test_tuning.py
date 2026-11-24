"""Fans and maximum performance against a fake sysfs tree. Run: python3 -m unittest discover tests"""

import errno
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from powerkeeper import core, tuning  # noqa: E402


def make(root, rel, value):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(f"{value}\n")
    return path


class Sysfs(unittest.TestCase):
    """A scratch root with every tuning path pointed into it, and a log of writes."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        for name, rel in (("HWMON", "hwmon"), ("CPU", "cpu"), ("PLATFORM_PROFILE", "platform_profile"),
                          ("MSI_EC", "msi-ec"), ("ASUS_ARMOURY", "armoury"), ("SYS_MODULE", "module"),
                          ("MODPROBE_DIR", "modprobe.d"), ("THINKPAD_FAN", "ibm-fan"),
                          ("SAVED_PATH", "lib/saved.json")):
            original = getattr(tuning, name)
            setattr(tuning, name, os.path.join(self.root, rel))
            self.addCleanup(setattr, tuning, name, original)
        for name, value in (("ppd_profile", lambda: None), ("_service_unit_exists", lambda n: False),
                            ("_service_active", lambda n: False)):
            original = getattr(tuning, name)
            setattr(tuning, name, value)
            self.addCleanup(setattr, tuning, name, original)
        self.writes = []
        original_write = core.write

        def logged(path, value):
            self.writes.append((os.path.relpath(path, self.root), str(value)))
            self.kernel_check(path, value)
            original_write(path, value)

        self.kernel_check = lambda path, value: None
        core.write = logged
        self.addCleanup(setattr, core, "write", original_write)

    def read(self, rel):
        return core.read(os.path.join(self.root, rel))


class AcerFanTests(Sysfs):
    def setUp(self):
        super().setUp()
        make(self.root, "hwmon/hwmon9/name", "acer")
        for i, rpm in ((1, 2400), (2, 2900)):
            make(self.root, f"hwmon/hwmon9/fan{i}_input", rpm)
            make(self.root, f"hwmon/hwmon9/pwm{i}", 128)
            make(self.root, f"hwmon/hwmon9/pwm{i}_enable", 2)
        make(self.root, "hwmon/hwmon3/name", "nvme")
        make(self.root, "hwmon/hwmon3/temp1_input", 40000)

    def test_finds_named_fans_with_controls(self):
        found = tuning.fans()
        self.assertEqual([f["label"] for f in found], ["CPU fan", "GPU fan"])
        self.assertTrue(all(f["can_max"] and f["can_fixed"] for f in found))
        self.assertEqual(tuning.fan_modes(found), ["auto", "max", "fixed"])
        self.assertEqual(tuning.fan_speeds(found), [("CPU fan", 2400), ("GPU fan", 2900)])

    def test_fixed_then_no_rewrites_then_auto_restores(self):
        saved = {}
        self.assertTrue(tuning.ensure_fans("fixed", 50, saved))
        self.assertEqual(self.read("hwmon/hwmon9/pwm1_enable"), "1")
        self.assertEqual(self.read("hwmon/hwmon9/pwm1"), "128")
        self.assertEqual(saved, {"acer:1": 2, "acer:2": 2})
        self.writes.clear()
        self.assertFalse(tuning.ensure_fans("fixed", 50, saved))
        self.assertEqual(self.writes, [])
        tuning.ensure_fans("auto", 0, saved)
        self.assertEqual(self.read("hwmon/hwmon9/pwm1_enable"), "2")
        self.assertEqual(self.read("hwmon/hwmon9/pwm2_enable"), "2")

    def test_driver_rounding_is_not_a_mismatch(self):
        saved = {}
        tuning.ensure_fans("fixed", 60, saved)          # 153
        make(self.root, "hwmon/hwmon9/pwm1", 152)       # the driver stored 60% as 152
        self.writes.clear()
        tuning.ensure_fans("fixed", 60, saved)
        self.assertEqual(self.writes, [])

    def test_max_without_full_speed_value_writes_255(self):
        tuning.ensure_fans("max", 0, {})
        self.assertEqual(self.read("hwmon/hwmon9/pwm1"), "255")
        self.assertEqual(self.read("hwmon/hwmon9/pwm1_enable"), "1")

    def test_fixed_speed_has_a_floor(self):
        tuning.ensure_fans("fixed", 0, {})
        self.assertEqual(self.read("hwmon/hwmon9/pwm1"), str(round(tuning.FAN_MIN_PERCENT * 2.55)))

    def test_auto_never_touches_fans_it_did_not_change(self):
        make(self.root, "hwmon/hwmon9/pwm1_enable", 1)  # someone else set manual
        tuning.ensure_fans("auto", 0, {})
        self.assertEqual(self.writes, [])

    def test_speed_first_when_manual_mode_is_refused_without_one(self):
        def kernel(path, value):
            if path.endswith("_enable") and str(value) == "1" and not self.writes[:-1]:
                raise OSError(errno.EINVAL, "Invalid argument")
        self.kernel_check = kernel
        tuning.ensure_fans("fixed", 50, {})
        self.assertEqual(self.read("hwmon/hwmon9/pwm1_enable"), "1")

    def test_watchdog(self):
        self.assertTrue(tuning.fans_too_hot("fixed", tuning.FAN_SAFE_TEMP))
        self.assertFalse(tuning.fans_too_hot("fixed", tuning.FAN_SAFE_TEMP - 1))
        self.assertFalse(tuning.fans_too_hot("max", 105))
        self.assertFalse(tuning.fans_too_hot("fixed", None))


class OtherFanTests(Sysfs):
    def test_thinkpad_full_speed_is_enable_zero(self):
        make(self.root, "hwmon/hwmon5/name", "thinkpad")
        make(self.root, "hwmon/hwmon5/fan1_input", 3100)
        make(self.root, "hwmon/hwmon5/pwm1", 100)
        make(self.root, "hwmon/hwmon5/pwm1_enable", 2)
        saved = {}
        tuning.ensure_fans("max", 0, saved)
        self.assertEqual(self.writes, [("hwmon/hwmon5/pwm1_enable", "0")])
        tuning.ensure_fans("auto", 0, saved)
        self.assertEqual(self.read("hwmon/hwmon5/pwm1_enable"), "2")

    def test_asus_has_max_but_no_fixed_and_curves_are_not_fans(self):
        make(self.root, "hwmon/hwmon6/name", "asus")
        make(self.root, "hwmon/hwmon6/fan1_input", 2800)
        make(self.root, "hwmon/hwmon6/pwm1_enable", 2)
        make(self.root, "hwmon/hwmon7/name", "asus_custom_fan_curve")
        make(self.root, "hwmon/hwmon7/pwm1_enable", 2)
        make(self.root, "hwmon/hwmon7/pwm1_auto_point1_pwm", 20)
        found = tuning.fans()
        self.assertEqual(len(found), 1)
        self.assertEqual(tuning.fan_modes(found), ["auto", "max"])

    def test_read_only_sensor_offers_no_modes(self):
        make(self.root, "hwmon/hwmon2/name", "dell_smm")
        make(self.root, "hwmon/hwmon2/fan1_input", 2000)
        os.chmod(make(self.root, "hwmon/hwmon2/pwm1_enable", 2), 0o444)
        self.assertEqual(tuning.fan_modes(), [])

    def test_msi_cooler_boost(self):
        make(self.root, "msi-ec/cooler_boost", "off")
        self.assertEqual(tuning.fan_modes(), ["auto", "max"])
        saved = {}
        tuning.ensure_fans("max", 0, saved)
        self.assertEqual(self.read("msi-ec/cooler_boost"), "on")
        tuning.ensure_fans("auto", 0, saved)
        self.assertEqual(self.read("msi-ec/cooler_boost"), "off")
        self.writes.clear()
        tuning.ensure_fans("auto", 0, {})               # never touched: hands off
        self.assertEqual(self.writes, [])


class PerformanceTests(Sysfs):
    def setUp(self):
        super().setUp()
        make(self.root, "platform_profile_choices", "low-power quiet balanced balanced-performance performance")
        make(self.root, "platform_profile", "balanced")
        for i in range(2):
            make(self.root, f"cpu/cpufreq/policy{i}/energy_performance_preference", "balance_performance")
            make(self.root, f"cpu/cpufreq/policy{i}/energy_performance_available_preferences",
                 "default performance balance_performance balance_power power")
            make(self.root, f"cpu/cpufreq/policy{i}/scaling_governor", "powersave")
        make(self.root, "cpu/intel_pstate/no_turbo", 1)

        def kernel(path, value):
            # intel_pstate refuses EPP changes under the performance governor.
            if path.endswith("energy_performance_preference") and str(value) != "performance":
                if core.read(os.path.join(os.path.dirname(path), "scaling_governor")) == "performance":
                    raise OSError(errno.EBUSY, "Device or resource busy")
        self.kernel_check = kernel

    def test_on_then_off_restores_everything(self):
        self.assertEqual(tuning.ensure(dict(core.DEFAULT_CONFIG, performance="on"), True), [])
        self.assertEqual(self.read("platform_profile"), "performance")
        self.assertEqual(self.read("cpu/cpufreq/policy1/energy_performance_preference"), "performance")
        self.assertEqual(self.read("cpu/cpufreq/policy0/scaling_governor"), "powersave")
        self.assertEqual(self.read("cpu/intel_pstate/no_turbo"), "0")
        self.writes.clear()
        tuning.ensure(dict(core.DEFAULT_CONFIG, performance="on"), True)
        self.assertEqual(self.writes, [])
        self.assertEqual(tuning.ensure(dict(core.DEFAULT_CONFIG), True), [])
        self.assertEqual(self.read("platform_profile"), "balanced")
        self.assertEqual(self.read("cpu/cpufreq/policy0/energy_performance_preference"), "balance_performance")
        self.assertEqual(self.read("cpu/intel_pstate/no_turbo"), "1")
        self.assertIsNone(tuning.load_saved())

    def test_firmware_dropping_turbo_is_put_back(self):
        config = dict(core.DEFAULT_CONFIG, performance="on")
        tuning.ensure(config, True)
        make(self.root, "platform_profile", "balanced")     # Acer firmware on unplug
        tuning.ensure(config, True)
        self.assertEqual(self.read("platform_profile"), "performance")
        tuning.ensure(dict(config, performance="off"), True)
        self.assertEqual(self.read("platform_profile"), "balanced")  # the value from before

    def test_only_while_plugged_in(self):
        config = dict(core.DEFAULT_CONFIG, performance="ac")
        tuning.ensure(config, False)
        self.assertEqual(self.writes, [])
        tuning.ensure(config, True)
        self.assertEqual(self.read("platform_profile"), "performance")
        tuning.ensure(config, False)
        self.assertEqual(self.read("platform_profile"), "balanced")

    def test_legion_extreme_mode_is_preferred(self):
        self.assertEqual(tuning.best_profile(["quiet", "balanced", "performance", "max-power"]),
                         "max-power")
        self.assertIsNone(tuning.best_profile(["low-power", "quiet"]))

    def test_saved_values_survive_a_restart_of_the_service(self):
        config = dict(core.DEFAULT_CONFIG, performance="on")
        tuning.ensure(config, True)
        tuning.ensure(config, True)     # a second capture must not overwrite the original
        self.assertEqual(tuning.load_saved()["performance"]["profile"], "balanced")

    def test_release_undoes_performance_and_fans(self):
        make(self.root, "hwmon/hwmon9/name", "acer")
        make(self.root, "hwmon/hwmon9/fan1_input", 2400)
        make(self.root, "hwmon/hwmon9/pwm1", 128)
        make(self.root, "hwmon/hwmon9/pwm1_enable", 2)
        tuning.ensure(dict(core.DEFAULT_CONFIG, performance="on", fan_mode="fixed", fan_percent=40), True)
        self.assertEqual(self.read("hwmon/hwmon9/pwm1_enable"), "1")
        self.assertEqual(tuning.release(), [])
        self.assertEqual(self.read("hwmon/hwmon9/pwm1_enable"), "2")
        self.assertEqual(self.read("platform_profile"), "balanced")
        self.assertIsNone(tuning.load_saved())

    def test_fan_auto_clears_saved_fans(self):
        make(self.root, "hwmon/hwmon9/name", "acer")
        make(self.root, "hwmon/hwmon9/pwm1", 128)
        make(self.root, "hwmon/hwmon9/pwm1_enable", 2)
        tuning.ensure(dict(core.DEFAULT_CONFIG, fan_mode="max"), True)
        self.assertIn("fans", tuning.load_saved())
        tuning.ensure(dict(core.DEFAULT_CONFIG), True)
        self.assertEqual(self.read("hwmon/hwmon9/pwm1_enable"), "2")
        self.assertIsNone(tuning.load_saved())

    def test_one_failing_part_does_not_stop_the_rest(self):
        def kernel(path, value):
            if path.endswith("platform_profile"):
                raise OSError(errno.EOPNOTSUPP, "Operation not supported")
        self.kernel_check = kernel
        errors = tuning.ensure(dict(core.DEFAULT_CONFIG, performance="on"), True)
        self.assertEqual(len(errors), 1)
        self.assertIn("firmware profile", errors[0])
        self.assertEqual(self.read("cpu/intel_pstate/no_turbo"), "0")

    def test_parts_describe_this_laptop(self):
        keys = [k for k, _ in tuning.performance_parts()]
        self.assertEqual(keys, ["profile", "epp", "turbo"])
        self.assertEqual([k for k, _ in tuning.performance_parts(ppd=True)],
                         ["ppd", "profile", "epp", "turbo"])

    def test_profile_daemon_is_asked_and_the_maximum_still_set(self):
        state = {"ppd": "balanced", "sets": []}
        tuning.ppd_profile = lambda: state["ppd"]

        def set_ppd(value):
            state["sets"].append(value)
            state["ppd"] = value
            # TLP's performance profile only goes as far as balance_performance.
            make(self.root, "cpu/cpufreq/policy0/energy_performance_preference",
                 "balance_performance" if value == "performance" else "balance_power")
        self.addCleanup(setattr, tuning, "set_ppd_profile", tuning.set_ppd_profile)
        tuning.set_ppd_profile = set_ppd
        config = dict(core.DEFAULT_CONFIG, performance="on")
        tuning.ensure(config, True)
        self.assertEqual(self.read("cpu/cpufreq/policy0/energy_performance_preference"), "performance")
        self.assertEqual(self.read("platform_profile"), "performance")
        tuning.ensure(dict(config, performance="off"), True)
        self.assertEqual(state["sets"], ["performance", "balanced"])
        self.assertEqual(self.read("cpu/cpufreq/policy0/energy_performance_preference"),
                         "balance_performance")

    def test_asus_limits_go_to_their_maximum_and_back(self):
        make(self.root, "armoury/ppt_pl1_spl/current_value", 45)
        make(self.root, "armoury/ppt_pl1_spl/max_value", 80)
        config = dict(core.DEFAULT_CONFIG, performance="on")
        tuning.ensure(config, True)
        self.assertEqual(self.read("armoury/ppt_pl1_spl/current_value"), "80")
        tuning.ensure(dict(config, performance="off"), True)
        self.assertEqual(self.read("armoury/ppt_pl1_spl/current_value"), "45")


class UnlockTests(Sysfs):
    def setUp(self):
        super().setUp()
        self.dmi = {"sys_vendor": "Acer", "product_name": "Predator PH315-55",
                    "product_family": "Predator Helios 300", "product_version": "V1.08"}
        original = core.dmi
        core.dmi = lambda field: self.dmi.get(field, "")
        self.addCleanup(setattr, core, "dmi", original)
        make(self.root, "module/acer_wmi/parameters/predator_v4", "N")

    def test_acer_predator_needs_the_option(self):
        self.assertEqual([u["id"] for u in tuning.unlocks()], ["acer-predator"])
        make(self.root, "module/acer_wmi/parameters/predator_v4", "Y")
        self.assertEqual(tuning.unlocks(), [])

    def test_other_acers_are_not_offered_it(self):
        self.dmi.update(product_name="Swift SF314-42", product_family="Swift 3")
        self.assertEqual(tuning.unlocks(), [])

    def test_unlock_writes_the_option_and_reloads(self):
        calls = []

        def run(args, timeout=10):
            calls.append(args)
            make(self.root, "module/acer_wmi/parameters/predator_v4", "Y")
            return 0, ""
        original = tuning._run
        tuning._run = run
        self.addCleanup(setattr, tuning, "_run", original)
        self.assertIsNone(tuning.apply_unlock("acer-predator"))
        conf = core.read(os.path.join(self.root, "modprobe.d/powerkeeper-acer-predator.conf"))
        self.assertIn("options acer_wmi predator_v4=1", conf)
        self.assertEqual(calls, [["modprobe", "-r", "acer_wmi"], ["modprobe", "acer_wmi"]])

    def test_driver_in_use_asks_for_a_restart(self):
        original = tuning._run
        tuning._run = lambda args, timeout=10: (1, "")
        self.addCleanup(setattr, tuning, "_run", original)
        self.assertIn("restart", tuning.apply_unlock("acer-predator"))

    def test_thinkpad_fan_control(self):
        self.dmi = {"sys_vendor": "LENOVO", "product_version": "ThinkPad T14 Gen 3"}
        os.makedirs(os.path.join(self.root, "module/thinkpad_acpi"))
        make(self.root, "ibm-fan", "status:\t\tenabled\nspeed:\t\t2900\nlevel:\t\tauto")
        self.assertEqual([u["id"] for u in tuning.unlocks()], ["thinkpad-fan"])
        make(self.root, "ibm-fan", "status:\t\tenabled\nlevel:\t\tauto\ncommands:\tlevel <level>")
        self.assertEqual(tuning.unlocks(), [])


if __name__ == "__main__":
    unittest.main()
