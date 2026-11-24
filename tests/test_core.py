"""Backend logic against a fake sysfs tree. Run: python3 -m unittest discover tests"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from kernelthrottle import core  # noqa: E402



def make(root, rel, value):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(f"{value}\n")
    return path


class ThresholdTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bat = os.path.join(self.tmp.name, "BAT0")
        make(self.bat, "type", "Battery")
        make(self.bat, "charge_control_start_threshold", 0)
        make(self.bat, "charge_control_end_threshold", 100)
        self.writes = []
        original = core.write

        def checked_write(path, value):
            # Enforce the kernel rule after each individual write.
            original(path, value)
            self.writes.append((os.path.basename(path), value))
            start = core.read_int(self.bat + "/charge_control_start_threshold")
            end = core.read_int(self.bat + "/charge_control_end_threshold")
            if start >= end:
                raise AssertionError(f"kernel would reject start={start} end={end}")

        core.write = checked_write
        self.addCleanup(setattr, core, "write", original)
        self.backend = core.ThresholdBackend([self.bat], [self.bat])

    def tearDown(self):
        self.tmp.cleanup()

    def test_order_raising_and_lowering(self):
        self.backend.ensure(85, 80)
        self.assertEqual((self.backend.limit(), self.backend.start()), (85, 80))
        self.backend.ensure(95, 90)   # start above the old end: end must go first
        self.assertEqual((self.backend.limit(), self.backend.start()), (95, 90))
        self.backend.ensure(60, 50)   # end below the old start: start must go first
        self.assertEqual((self.backend.limit(), self.backend.start()), (60, 50))

    def test_no_writes_when_matching(self):
        self.backend.ensure(80, 75)
        self.writes.clear()
        self.backend.ensure(80, 75)
        self.assertEqual(self.writes, [])

    def test_disable_lowers_start(self):
        self.backend.ensure(80, 75)
        self.backend.ensure(100, None)
        self.assertEqual(self.backend.limit(), 100)
        self.assertEqual(self.backend.start(), 0)

    def test_target(self):
        config = dict(core.DEFAULT_CONFIG, end=85, start=90)
        self.assertEqual(core.target(config, self.backend), (85, 80))  # bad start repaired
        self.assertEqual(core.target(dict(config, top_up=True), self.backend), (100, None))
        self.assertEqual(core.target(dict(config, limit_enabled=False), self.backend), (100, None))
        self.assertIsNone(core.target(dict(config, calibrating=True), self.backend))


class DellTests(unittest.TestCase):
    def test_dell_gap_and_minimums(self):
        backend = core.ThresholdBackend([], [])
        backend.min_end, backend.min_start, backend.min_gap = 55, 50, 5
        self.assertEqual(backend.snap(40), 55)
        config = dict(core.DEFAULT_CONFIG, end=85, start=83)
        self.assertEqual(core.target(config, backend), (85, 80))   # gap of 2 widened to 5
        self.assertEqual(core.target(dict(config, end=55, start=20), backend), (55, 50))


class VendorTests(unittest.TestCase):
    def backend_for(self, vendor):
        original = core.dmi
        core.dmi = lambda field: vendor
        try:
            return core.ThresholdBackend([], [])
        finally:
            core.dmi = original

    def test_maker_notes_are_kept(self):
        # A later general assignment used to overwrite these.
        self.assertIn("single fixed 80%", self.backend_for("LG Electronics").detail)
        self.assertIn("10% below", self.backend_for("Micro-Star International").detail)
        self.assertIn("stop threshold", self.backend_for("Framework").detail)

    def test_lg_and_toshiba_only_80(self):
        for vendor in ("LG Electronics", "TOSHIBA", "Dynabook Inc."):
            backend = self.backend_for(vendor)
            self.assertFalse(backend.continuous, vendor)
            self.assertEqual(backend.snap(85), 80)
            self.assertEqual(backend.snap(60), 80)
            self.assertEqual(core.target(dict(core.DEFAULT_CONFIG, end=85), backend)[0], 80)

    def test_msi_start_follows_end(self):
        backend = self.backend_for("Micro-Star International Co., Ltd.")
        self.assertEqual(core.target(dict(core.DEFAULT_CONFIG, end=85, start=80), backend), (85, 75))

    def test_msi_never_writes_start(self):
        """Emulate msi-ec: one EC value; start = value - 10; writing start sets value = start + 10."""
        with tempfile.TemporaryDirectory() as tmp:
            bat = os.path.join(tmp, "BAT1")
            make(bat, "type", "Battery")
            end_path = make(bat, "charge_control_end_threshold", 100)
            start_path = make(bat, "charge_control_start_threshold", 90)
            original = core.write

            def ec_write(path, value):
                ec = int(value) + 10 if path == start_path else int(value)
                with open(end_path, "w") as f:
                    f.write(f"{ec}\n")
                with open(start_path, "w") as f:
                    f.write(f"{ec - 10}\n")

            core.write = ec_write
            try:
                backend = self.backend_for("Micro-Star International Co., Ltd.")
                backend.threshold_bats = [bat]
                for config in (dict(core.DEFAULT_CONFIG, end=80), dict(core.DEFAULT_CONFIG, limit_enabled=False)):
                    for _ in range(3):      # several daemon cycles
                        backend.ensure(*core.target(config, backend))
                    expected = 80 if config["limit_enabled"] else 100
                    self.assertEqual(core.read_int(end_path), expected)
            finally:
                core.write = original

    def test_rejected_value_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = make(tmp, "charge_control_end_threshold", 100)
            original = core.write

            def reject(p, value):
                raise OSError(22, "Invalid argument")

            core.write = reject
            try:
                with self.assertRaisesRegex(OSError, "does not accept 85%"):
                    core.write_threshold(path, 85)
            finally:
                core.write = original


class CalibrationTests(unittest.TestCase):
    def test_stop_reasons(self):
        backend = core.AcerBackend([])
        backend.calibrating = lambda: True
        now = core.time.time()
        info = {"ac": True}
        config = dict(core.DEFAULT_CONFIG, calibrating=True, calibration_started=now - 3600)
        self.assertIsNone(core.calibration_stop_reason(backend, config, info))
        self.assertIn("unplugged", core.calibration_stop_reason(backend, config, {"ac": False}))
        old = dict(config, calibration_started=now - 25 * 3600)
        self.assertIn("24 hours", core.calibration_stop_reason(backend, old, info))
        backend.calibrating = lambda: False
        self.assertIn("finished", core.calibration_stop_reason(backend, config, info))
        fresh = dict(config, calibration_started=now - 60)
        self.assertIsNone(core.calibration_stop_reason(backend, fresh, info))


class FixedTests(unittest.TestCase):
    def test_acer_snaps_85_to_80(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = make(tmp, "health_mode", 0)
            backend = core.AcerBackend([])
            backend.path = path
            config = dict(core.DEFAULT_CONFIG, end=85)
            self.assertEqual(core.target(config, backend), (80, 75))
            core.apply(backend, config)
            self.assertEqual(core.read(path), "1")
            self.assertEqual(backend.limit(), 80)
            core.apply(backend, dict(config, limit_enabled=False))
            self.assertEqual(core.read(path), "0")
            self.assertEqual(backend.limit(), 100)

    def test_sony_steps(self):
        backend = core.SonyBackend([])
        self.assertEqual(backend.snap(85), 80)
        self.assertEqual(backend.snap(60), 50)
        self.assertEqual(backend.snap(100), 100)


class SoftwareTests(unittest.TestCase):
    def test_hysteresis(self):
        with tempfile.TemporaryDirectory() as tmp:
            bat = os.path.join(tmp, "BAT0")
            path = make(bat, "charge_behaviour", "[auto] inhibit-charge force-discharge")
            original = core.write

            def fake_write(p, value):  # emulate the kernel's bracket format
                choices, _ = core.parse_choices(core.read(p))
                with open(p, "w") as f:
                    f.write(" ".join(f"[{c}]" if c == value else c for c in choices))

            core.write = fake_write
            try:
                backend = core.SoftwareBackend([bat])
                config = dict(core.DEFAULT_CONFIG, end=80, start=70)
                for capacity, expected in ((15, "auto"), (60, "auto"), (80, "inhibit-charge"), (75, "inhibit-charge"),
                                           (70, "auto"), (75, "auto")):
                    core.enforce_software(backend, config, capacity)
                    self.assertEqual(core.parse_choices(core.read(path))[1], expected, capacity)
            finally:
                core.write = original


class SafetyTests(unittest.TestCase):
    def test_behaviour_override(self):
        info = {"capacity": 50, "ac": True}
        config = dict(core.DEFAULT_CONFIG, behaviour="force-discharge")
        self.assertFalse(core.behaviour_override(config, info))
        self.assertTrue(core.behaviour_override(config, dict(info, capacity=core.SAFETY_FLOOR)))
        self.assertEqual(config["behaviour"], "auto")
        config = dict(core.DEFAULT_CONFIG, behaviour="inhibit-charge")
        self.assertTrue(core.behaviour_override(config, dict(info, ac=False)))
        self.assertEqual(config["behaviour"], "auto")
        self.assertFalse(core.behaviour_override(dict(core.DEFAULT_CONFIG), info))

    def test_software_limit_respects_switch(self):
        backend = core.SoftwareBackend([])
        original = core.load_config
        try:
            core.load_config = lambda: dict(core.DEFAULT_CONFIG, end=80, start=70)
            self.assertEqual((backend.limit(), backend.start()), (80, 70))
            core.load_config = lambda: dict(core.DEFAULT_CONFIG, end=80, limit_enabled=False)
            self.assertEqual((backend.limit(), backend.start()), (100, None))
        finally:
            core.load_config = original


class AdapterTests(unittest.TestCase):
    def test_ac_inferred_from_status(self):
        original_bats, original_ac, original_info = core.batteries, core.ac_online, core.battery_info
        try:
            core.batteries = lambda: ["/x/BAT0"]
            core.ac_online = lambda: None
            for status, expected in (("Charging", True), ("Discharging", False), ("Unknown", None)):
                core.battery_info = lambda bat, s=status: {"status": s, "name": "BAT0"}
                self.assertEqual(core.combined_info()["ac"], expected, status)
        finally:
            core.batteries, core.ac_online, core.battery_info = original_bats, original_ac, original_info


class FullTests(unittest.TestCase):
    def test_is_full(self):
        self.assertTrue(core.is_full({"status": "Full", "capacity": 97}))
        self.assertTrue(core.is_full({"status": "Charging", "capacity": 99}))
        self.assertFalse(core.is_full({"status": "Charging", "capacity": 90}))
        self.assertFalse(core.is_full(None))


class PowerFlowTests(unittest.TestCase):
    """What the charger and the battery each supply, and how far to trust it."""

    def flow(self, status, ac, power, typical=None, sensor=None):
        return core.power_flow({"status": status, "ac": ac, "power": power, "charger_power": sensor}, typical)

    def test_on_battery_is_measured(self):
        f = self.flow("Discharging", False, 24.5, typical=30)
        self.assertEqual((f["source"], f["from_battery"], f["from_charger"]), ("battery", 24.5, 0.0))
        self.assertEqual((f["laptop"], f["laptop_kind"], f["charger_kind"]), (24.5, "measured", "unplugged"))

    def test_charging_without_sensor_estimates_from_typical_draw(self):
        f = self.flow("Charging", True, 40.0, typical=20.0)
        self.assertEqual((f["into_battery"], f["from_charger"], f["charger_kind"]), (40.0, 60.0, "estimate"))
        self.assertEqual((f["laptop"], f["laptop_kind"], f["source"]), (20.0, "estimate", "charger"))
        self.assertIsNone(f["from_battery"])

    def test_charging_without_any_data_gives_a_lower_bound_only(self):
        f = self.flow("Charging", True, 40.0)
        self.assertEqual((f["from_charger"], f["charger_kind"]), (40.0, "at_least"))
        self.assertEqual((f["laptop"], f["laptop_kind"]), (None, "unknown"))

    def test_held_at_limit_runs_on_the_charger(self):
        # A held battery can report a small current; it isn't powering anything.
        f = self.flow("Not charging", True, 0.2, typical=22.0)
        self.assertEqual((f["from_battery"], f["from_charger"], f["charger_kind"]), (0.0, 22.0, "estimate"))
        self.assertEqual(self.flow("Full", True, 0.0)["charger_kind"], "unknown")

    def test_a_real_sensor_wins(self):
        f = self.flow("Charging", True, 40.0, typical=20.0, sensor=65.0)
        self.assertEqual((f["from_charger"], f["charger_kind"], f["laptop"], f["laptop_kind"]),
                         (65.0, "measured", 25.0, "measured"))
        f = self.flow("Not charging", True, 0.0, sensor=18.0)
        self.assertEqual((f["from_charger"], f["laptop"], f["laptop_kind"]), (18.0, 18.0, "measured"))

    def test_plugged_in_but_draining(self):
        # Run-on-battery mode, or a charger too weak for the load.
        f = self.flow("Discharging", True, 30.0)
        self.assertEqual((f["source"], f["from_battery"], f["from_charger"]), ("battery", 30.0, None))
        self.assertEqual((f["laptop"], f["laptop_kind"]), (30.0, "at_least"))
        f = self.flow("Discharging", True, 30.0, sensor=45.0)
        self.assertEqual((f["source"], f["laptop"], f["laptop_kind"]), ("both", 75.0, "measured"))

    def test_steady_reading_is_preferred(self):
        f = core.power_flow({"status": "Discharging", "ac": False, "power": 90.0, "power_steady": 25.0})
        self.assertEqual(f["from_battery"], 25.0)

    def test_typical_draw(self):
        def point(power, status="Discharging", ac=False):
            return {"power": power, "status": status, "ac": ac}
        self.assertIsNone(core.typical_draw([point(20)] * 9))       # too little to go on
        points = [point(20)] * 9 + [point(90)] * 2                  # a spike doesn't move a median
        points += [point(60, "Charging", True), point(0.1), point(60, "Discharging", True)]
        self.assertEqual(core.typical_draw(points), 20)

    def test_power_series_splits_and_breaks_lines(self):
        def point(t, status, power=10.0):
            return {"t": t, "status": status, "power": power}
        battery, charging = core.power_series([
            point(1, "Discharging"), point(2, "Discharging"), point(3, "Charging"),
            point(4, "Not charging", 0.0), point(5, "Discharging"), point(6, "Charging", None)])
        self.assertEqual(battery, [(1, 10.0), (2, 10.0), None, (5, 10.0), None])
        self.assertEqual(charging, [(3, 10.0), None])


class ChartLineTests(unittest.TestCase):
    def test_gaps_and_breaks_split_a_line(self):
        series = [(0, 1), (10, 1), None, (20, 1), (100, 1), (105, 1)]
        self.assertEqual(core.split_lines(series, 50), [[(0, 1), (10, 1)], [(20, 1)], [(100, 1), (105, 1)]])
        self.assertEqual(core.split_lines([None, None], 50), [])

    def test_daily_health_samples_connect(self):
        # Samples a day apart: the zoom-based gap on a 30-day chart (9 h) drew nothing at all.
        days = [(i * 86400, 90) for i in range(3)]
        self.assertEqual(len(core.split_lines(days, 30 * 86400 / 80)), 3)
        self.assertEqual(core.split_lines(days, 14 * 86400), [days])


class ChargerSensorTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        original = core.POWER_SUPPLY
        core.POWER_SUPPLY = self.dir.name
        self.addCleanup(setattr, core, "POWER_SUPPLY", original)

    def supply(self, name, uevent):
        path = os.path.join(self.dir.name, name)
        os.makedirs(path)
        with open(path + "/uevent", "w") as f:
            f.write("".join(f"POWER_SUPPLY_{k}={v}\n" for k, v in uevent.items()))
        return path

    def test_adapter_without_a_sensor(self):
        self.supply("ACAD", {"TYPE": "Mains", "ONLINE": 1})
        self.assertIsNone(core.charger_power())

    def test_usb_c_contract_values_are_not_a_measurement(self):
        self.supply("ucsi", {"TYPE": "USB", "ONLINE": 1, "VOLTAGE_NOW": 20000000, "CURRENT_NOW": 3250000})
        self.assertIsNone(core.charger_power())

    def test_power_now(self):
        self.supply("ADP1", {"TYPE": "Mains", "ONLINE": 1, "POWER_NOW": 45000000})
        self.assertEqual(core.charger_power(), 45.0)

    def test_hwmon_power(self):
        path = self.supply("ADP1", {"TYPE": "Mains", "ONLINE": 1})
        os.makedirs(path + "/hwmon3")
        with open(path + "/hwmon3/power1_input", "w") as f:
            f.write("30000000\n")
        self.assertEqual(core.charger_power(), 30.0)

    def test_offline_device_and_implausible_are_ignored(self):
        self.supply("offline", {"TYPE": "Mains", "ONLINE": 0, "POWER_NOW": 45000000})
        self.supply("mouse", {"TYPE": "USB", "ONLINE": 1, "SCOPE": "Device", "POWER_NOW": 5000000})
        self.supply("bad", {"TYPE": "Mains", "ONLINE": 1, "POWER_NOW": 900000000})
        self.assertIsNone(core.charger_power())


class HistoryFileTests(unittest.TestCase):
    def test_a_broken_line_only_loses_that_line(self):
        with tempfile.TemporaryDirectory() as folder:
            original = core.HISTORY_PATH, core.HEALTH_PATH
            core.HISTORY_PATH, core.HEALTH_PATH = folder + "/history.csv", folder + "/health.csv"
            self.addCleanup(lambda: (setattr(core, "HISTORY_PATH", original[0]),
                                     setattr(core, "HEALTH_PATH", original[1])))
            with open(core.HISTORY_PATH, "w") as f:
                f.write("100,50,10.00,Discharging,0,30.0\n17x,5,garbage,,,\n200,49,10.00,Discharging,0,30.0\n")
            with open(core.HEALTH_PATH, "w") as f:
                f.write("2026-01-01,90,100,5\nbroken\n2026-01-02,89,100,6\n")
            self.assertEqual([p["t"] for p in core.load_history(0)], [100, 200])
            self.assertEqual([r["cycles"] for r in core.load_health()], [5, 6])


class UsageTests(unittest.TestCase):
    def test_summary(self):
        def point(t, cap, power, ac):
            return {"t": t, "capacity": cap, "power": power, "ac": ac,
                    "status": "Charging" if ac else "Discharging"}
        points = [point(0, 80, 10, False), point(180, 79, 10, False), point(360, 78, 10, False),
                  point(1000, 78, 30, True), point(1060, 80, 30, True)]   # gap of 640 s skipped
        summary = core.usage_summary(points)
        self.assertEqual(summary["battery_seconds"], 360)
        self.assertEqual(summary["used_pct"], 2)
        self.assertAlmostEqual(summary["energy_wh"], 1.0)
        self.assertAlmostEqual(summary["average_w"], 10)
        self.assertEqual((summary["plugged_seconds"], summary["charged_pct"]), (60, 2))
        self.assertAlmostEqual(summary["charged_wh"], 0.5)

    def test_sampler(self):
        with tempfile.TemporaryDirectory() as proc:
            def stat(pid, name, ppid, ticks, started):
                fields = ["S", ppid] + [0] * 9 + [ticks, 0] + [0] * 6 + [started]
                make(proc, f"{pid}/stat", f"{pid} ({name}) " + " ".join(map(str, fields)))
            hz = os.sysconf("SC_CLK_TCK")
            make(proc, "uptime", "100.0 0")
            stat(10, "Web Content", 1, 5 * hz, 1 * hz)
            stat(11, "kworker/0:1", 2, 1 * hz, 1 * hz)
            sampler = core.CpuSampler(proc)
            self.assertEqual(sampler.sample(), {})               # first call is the baseline
            make(proc, "uptime", "160.0 0")
            stat(10, "Web Content", 1, 7 * hz, 1 * hz)
            stat(11, "kworker/0:1", 2, 2 * hz, 1 * hz)
            stat(12, "make (job)", 1, 3 * hz, 120 * hz)        # started after the baseline
            self.assertEqual(sampler.sample(), {"firefox": 2, "Kernel": 1, "make (job)": 3})


class ParseTests(unittest.TestCase):
    def test_choices(self):
        self.assertEqual(core.parse_choices("auto [inhibit-charge] force-discharge"),
                         (["auto", "inhibit-charge", "force-discharge"], "inhibit-charge"))
        self.assertEqual(core.parse_choices(None), ([], None))

    def test_real_battery_reads(self):
        info = core.combined_info()
        if info:
            self.assertIn(info["status"], ("Charging", "Discharging", "Full", "Not charging", "Unknown"))


if __name__ == "__main__":
    unittest.main()
