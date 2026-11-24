"""Fans and maximum performance, through standard kernel interfaces with per-maker rules.

Fans: hwmon `fanN_input` (speed), `pwmN` (0-255) and `pwmN_enable` (who controls the fan).
`pwmN_enable` means different things per driver, so FAN_RULES says which value gives full
speed and which gives the firmware's automatic control. MSI's msi-ec has no pwm files, only
Cooler Boost (full speed on or off).

Maximum performance: power-profiles-daemon (or TLP's tlp-pd) and the firmware platform profile,
the CPU energy preference, turbo boost, NVIDIA Dynamic Boost, MSI shift mode and ASUS power
limits. The values found before are saved and restored when it is switched off.

Some makers' features are hidden behind a kernel module option (Acer `predator_v4`, ThinkPad
`fan_control`); UNLOCKS turns those on with a modprobe.d file.
"""

import glob
import json
import os
import shutil
import subprocess

from . import core

HWMON = "/sys/class/hwmon"
CPU = "/sys/devices/system/cpu"
PLATFORM_PROFILE = "/sys/firmware/acpi/platform_profile"
MSI_EC = "/sys/devices/platform/msi-ec"
ASUS_ARMOURY = "/sys/class/firmware-attributes/asus-armoury/attributes"
SYS_MODULE = "/sys/module"
MODPROBE_DIR = "/etc/modprobe.d"
THINKPAD_FAN = "/proc/acpi/ibm/fan"
SAVED_PATH = core.DATA_DIR + "/performance-saved.json"

# Highest first. "max-power" is Lenovo Legion's extreme mode; Acer maps "performance" to Turbo.
PROFILE_ORDER = ("max-power", "performance", "balanced-performance")
ASUS_LIMITS = ("ppt_pl1_spl", "ppt_pl2_sppt", "ppt_pl3_fppt", "ppt_apu_sppt", "ppt_platform_sppt",
               "nv_dynamic_boost")
NVIDIA_BOOST = "nvidia-powerd"

# A fixed fan speed below 100% is handed back to the firmware at this CPU temperature.
FAN_SAFE_TEMP = 90
FAN_MIN_PERCENT = 20

# Per hwmon driver name: pwm_enable value for full speed (None: write pwm 255 in manual mode
# instead), for manual and for automatic; names for fans without a fanN_label.
DEFAULT_RULE = {"full": None, "manual": 1, "auto": 2, "labels": {}}
FAN_RULES = {
    "acer": {"full": None, "manual": 1, "auto": 2, "labels": {1: "CPU fan", 2: "GPU fan"}},
    "thinkpad": {"full": 0, "manual": 1, "auto": 2, "labels": {1: "Fan", 2: "Second fan"}},
    "asus": {"full": 0, "manual": None, "auto": 2, "labels": {1: "CPU fan"}},
    "asus_custom_fan_curve": None,     # fan curve points, not a fan
    "hp": {"full": 0, "manual": None, "auto": 2, "labels": {1: "Fan"}},
    "dell_smm": {"full": None, "manual": 1, "auto": 2, "labels": {}},
}


def _rule(driver):
    rule = FAN_RULES.get(driver, DEFAULT_RULE)
    return rule


def _writable(path):
    """Root can write a sysfs file only if some write bit is set."""
    try:
        return bool(os.stat(path).st_mode & 0o222)
    except OSError:
        return False


# --- fans ------------------------------------------------------------------

def fans():
    """Every fan with a speed sensor or a control: dicts with id, label, paths and abilities."""
    found = []
    for hwmon in sorted(glob.glob(HWMON + "/hwmon*")):
        driver = core.read(hwmon + "/name", "")
        if driver in FAN_RULES and FAN_RULES[driver] is None:
            continue
        rule = _rule(driver)
        indexes = set()
        for path in glob.glob(hwmon + "/fan[0-9]*_input") + glob.glob(hwmon + "/pwm[0-9]*"):
            digits = "".join(c for c in os.path.basename(path).split("_")[0] if c.isdigit())
            if digits:
                indexes.add(int(digits))
        for index in sorted(indexes):
            rpm = f"{hwmon}/fan{index}_input"
            pwm = f"{hwmon}/pwm{index}"
            enable = f"{hwmon}/pwm{index}_enable"
            has_rpm = os.path.exists(rpm)
            has_pwm = os.path.exists(pwm) and _writable(pwm)
            has_enable = os.path.exists(enable) and _writable(enable)
            if not has_rpm and not has_enable:
                continue
            label = core.read(f"{hwmon}/fan{index}_label") or rule["labels"].get(index)
            found.append({
                "id": f"{driver}:{index}",
                "driver": driver,
                "label": label or (f"Fan {index}" if index > 1 or len(indexes) > 1 else "Fan"),
                "rpm": rpm if has_rpm else None,
                "pwm": pwm if has_pwm else None,
                "enable": enable if has_enable else None,
                "can_max": has_enable and (rule["full"] is not None
                                           or (rule["manual"] is not None and has_pwm)),
                "can_fixed": has_enable and has_pwm and rule["manual"] is not None,
            })
    return found


def cooler_boost():
    """MSI Cooler Boost (all fans at full speed): 'on', 'off' or None if missing."""
    value = core.read(MSI_EC + "/cooler_boost")
    return value if value in ("on", "off") else None


def fan_modes(found=None):
    """The fan modes this laptop supports: a subset of auto, max, fixed."""
    found = fans() if found is None else found
    modes = ["auto"]
    if any(f["can_max"] for f in found) or cooler_boost() is not None:
        modes.append("max")
    if any(f["can_fixed"] for f in found):
        modes.append("fixed")
    return modes if len(modes) > 1 else []


def fan_speeds(found=None):
    """[(label, rpm or None)]. On some laptops each read is a firmware call: read off the main loop."""
    found = fans() if found is None else found
    return [(f["label"], core.read_int(f["rpm"]) if f["rpm"] else None) for f in found]


def _pwm_for(percent):
    return max(0, min(255, round(percent * 255 / 100)))


def _fan_state(fan):
    return core.read_int(fan["enable"]), core.read_int(fan["pwm"]) if fan["pwm"] else None


def ensure_fans(mode, percent, saved):
    """Put every controllable fan in mode; write only what differs. Returns True if it wrote.

    saved maps fan id -> the pwm_enable value found before the first change, so "auto" gives
    back exactly what the firmware had (desktop boards use several automatic modes).
    """
    wrote = False
    boost = cooler_boost()
    if boost is not None:
        wanted = "on" if mode == "max" else "off"
        if boost != wanted and (mode == "max" or saved.get("msi:cooler_boost") is not None):
            saved.setdefault("msi:cooler_boost", boost)
            core.write(MSI_EC + "/cooler_boost", wanted)
            wrote = True
    for fan in fans():
        if not fan["enable"]:
            continue
        rule = _rule(fan["driver"])
        enable, pwm = _fan_state(fan)
        if mode == "auto":
            if fan["id"] not in saved:
                continue        # never touched: leave the firmware alone
            original = saved[fan["id"]]
            target = original if original not in (None, rule["manual"], rule["full"]) else rule["auto"]
            if enable != target:
                core.write(fan["enable"], target)
                wrote = True
            continue
        if mode == "max" and not fan["can_max"] or mode == "fixed" and not fan["can_fixed"]:
            continue
        saved.setdefault(fan["id"], enable)
        if mode == "max" and rule["full"] is not None:
            if enable != rule["full"]:
                _write_fan(fan, rule["full"], None)
                wrote = True
            continue
        target = 255 if mode == "max" else _pwm_for(max(FAN_MIN_PERCENT, min(100, percent)))
        # Drivers round pwm through a percentage, so allow a small difference.
        if enable != rule["manual"] or pwm is None or abs(pwm - target) > 3:
            _write_fan(fan, rule["manual"], target)
            wrote = True
    return wrote


def _write_fan(fan, enable, pwm):
    try:
        core.write(fan["enable"], enable)
    except OSError:
        # Some drivers want a speed before manual mode, others refuse a speed before it.
        if pwm is None:
            raise
        core.write(fan["pwm"], pwm)
        core.write(fan["enable"], enable)
    if pwm is not None:
        core.write(fan["pwm"], pwm)


def fans_too_hot(mode, cpu_temp):
    return mode == "fixed" and cpu_temp is not None and cpu_temp >= FAN_SAFE_TEMP


# --- maximum performance ---------------------------------------------------

def _run(args, timeout=10):
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return result.returncode, result.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return -1, ""


PPD = ("net.hadess.PowerProfiles", "/net/hadess/PowerProfiles", "net.hadess.PowerProfiles")


def ppd_profile():
    """power-profiles-daemon's profile, or None when it isn't running."""
    if not shutil.which("busctl"):
        return None
    code, out = _run(["busctl", "--system", "get-property", *PPD, "ActiveProfile"], 5)
    if code != 0 or not out.startswith("s "):
        return None
    return out[2:].strip('"')


def set_ppd_profile(value):
    code, _ = _run(["busctl", "--system", "set-property", *PPD, "ActiveProfile", "s", value], 5)
    if code != 0:
        raise OSError(f"power-profiles-daemon refused '{value}'")


def best_profile(choices):
    for name in PROFILE_ORDER:
        if name in choices:
            return name
    return None


def _epp_paths():
    return sorted(glob.glob(CPU + "/cpufreq/policy*/energy_performance_preference"))


def _governor_paths():
    return sorted(glob.glob(CPU + "/cpufreq/policy*/scaling_governor"))


def _service_unit_exists(name):
    code, out = _run(["systemctl", "list-unit-files", name + ".service", "--no-legend"], 5)
    return code == 0 and name in out


def _service_active(name):
    code, out = _run(["systemctl", "is-active", name], 5)
    return out == "active"


def performance_parts(ppd=None):
    """What maximum performance changes on this laptop: [(key, description)]."""
    parts = []
    choices = (core.read(PLATFORM_PROFILE + "_choices") or "").split()
    if ppd is None:
        ppd = ppd_profile() is not None
    if ppd:
        parts.append(("ppd", "Power mode: Performance"))
    if best_profile(choices):
        parts.append(("profile", f"Firmware profile: {best_profile(choices)}"))
    paths = _epp_paths()
    if paths and "performance" in (core.read(os.path.dirname(paths[0])
                                             + "/energy_performance_available_preferences") or ""):
        parts.append(("epp", "CPU energy preference: performance"))
    elif not paths and _governor_paths() and "performance" in (core.read(
            CPU + "/cpufreq/policy0/scaling_available_governors") or ""):
        parts.append(("governor", "CPU governor: performance"))
    if os.path.exists(CPU + "/intel_pstate/no_turbo") or os.path.exists(CPU + "/cpufreq/boost"):
        parts.append(("turbo", "Turbo boost on"))
    if "turbo" in (core.read(MSI_EC + "/available_shift_modes") or "").split():
        parts.append(("msi", "MSI shift mode: turbo"))
    if any(os.path.exists(f"{ASUS_ARMOURY}/{n}/max_value") for n in ASUS_LIMITS):
        parts.append(("asus", "ASUS power limits at their maximum"))
    if _service_unit_exists(NVIDIA_BOOST):
        parts.append(("nvidia", "NVIDIA Dynamic Boost"))
    return parts


def capture():
    """The current values of everything maximum performance changes."""
    saved = {"ppd": ppd_profile(), "profile": core.read(PLATFORM_PROFILE)}
    saved["epp"] = {p: core.read(p) for p in _epp_paths()}
    saved["governor"] = {p: core.read(p) for p in _governor_paths()}
    saved["no_turbo"] = core.read(CPU + "/intel_pstate/no_turbo")
    saved["boost"] = core.read(CPU + "/cpufreq/boost")
    saved["msi_shift"] = core.read(MSI_EC + "/shift_mode")
    saved["asus"] = {n: core.read(f"{ASUS_ARMOURY}/{n}/current_value") for n in ASUS_LIMITS
                     if os.path.exists(f"{ASUS_ARMOURY}/{n}/current_value")}
    saved["nvidia"] = _service_active(NVIDIA_BOOST) if _service_unit_exists(NVIDIA_BOOST) else None
    return saved


def _set(path, value):
    """Write only on mismatch; returns True if it wrote."""
    if value is None or core.read(path) in (None, str(value)):
        return False
    core.write(path, value)
    return True


_nvidia_failed = False     # nvidia-powerd exits on GPUs without Dynamic Boost: don't retry


def apply_max(deep=True):
    """Switch everything to its highest setting. Errors in one part don't stop the others.

    deep=False only re-checks sysfs (cheap); the D-Bus and systemd parts start processes,
    so the service runs them only when something changed.
    """
    global _nvidia_failed
    errors = []

    def attempt(name, action):
        try:
            action()
        except OSError as exc:
            errors.append(f"{name}: {exc.strerror or exc}")

    # A power-profiles daemon (power-profiles-daemon, or TLP's tlp-pd) is asked first so the
    # desktop shows the right mode, but its "performance" isn't the maximum everywhere (TLP
    # keeps balance_performance), so the firmware profile and the CPU are set directly too.
    ppd = ppd_profile() if deep else None
    if ppd is not None and ppd != "performance":
        attempt("power mode", lambda: set_ppd_profile("performance"))
    choices = (core.read(PLATFORM_PROFILE + "_choices") or "").split()
    attempt("firmware profile", lambda: _set(PLATFORM_PROFILE, best_profile(choices)))
    epp = _epp_paths()
    if epp:
        for path in epp:
            if "performance" in (core.read(os.path.dirname(path)
                                           + "/energy_performance_available_preferences") or ""):
                attempt("CPU energy preference", lambda p=path: _set(p, "performance"))
    else:
        for path in _governor_paths():
            attempt("CPU governor", lambda p=path: _set(p, "performance"))
    attempt("turbo", lambda: _set(CPU + "/intel_pstate/no_turbo", 0))
    attempt("turbo", lambda: _set(CPU + "/cpufreq/boost", 1))
    if "turbo" in (core.read(MSI_EC + "/available_shift_modes") or "").split():
        attempt("MSI shift mode", lambda: _set(MSI_EC + "/shift_mode", "turbo"))
    for name in ASUS_LIMITS:
        top = core.read(f"{ASUS_ARMOURY}/{name}/max_value")
        if top is not None:
            attempt(name, lambda n=name, t=top: _set(f"{ASUS_ARMOURY}/{n}/current_value", t))
    if deep and not _nvidia_failed and _service_unit_exists(NVIDIA_BOOST) \
            and not _service_active(NVIDIA_BOOST):
        code, _ = _run(["systemctl", "start", NVIDIA_BOOST])
        if code != 0:
            _nvidia_failed = True
            errors.append("NVIDIA Dynamic Boost would not start (this graphics chip may not "
                          "support it)")
    return errors


def restore(saved):
    """Put back what capture() found. EPP last: firmware profiles reset it on some laptops."""
    global _nvidia_failed
    _nvidia_failed = False
    errors = []

    def attempt(name, action):
        try:
            action()
        except OSError as exc:
            errors.append(f"{name}: {exc.strerror or exc}")

    # The daemon first, so its own writes don't undo the values put back after it.
    if saved.get("ppd") and ppd_profile() not in (None, saved["ppd"]):
        attempt("power mode", lambda: set_ppd_profile(saved["ppd"]))
    attempt("firmware profile", lambda: _set(PLATFORM_PROFILE, saved.get("profile")))
    for path, value in (saved.get("governor") or {}).items():
        attempt("CPU governor", lambda p=path, v=value: _set(p, v))
    for path, value in (saved.get("epp") or {}).items():
        attempt("CPU energy preference", lambda p=path, v=value: _set(p, v))
    attempt("turbo", lambda: _set(CPU + "/intel_pstate/no_turbo", saved.get("no_turbo")))
    attempt("turbo", lambda: _set(CPU + "/cpufreq/boost", saved.get("boost")))
    attempt("MSI shift mode", lambda: _set(MSI_EC + "/shift_mode", saved.get("msi_shift")))
    for name, value in (saved.get("asus") or {}).items():
        attempt(name, lambda n=name, v=value: _set(f"{ASUS_ARMOURY}/{n}/current_value", v))
    if saved.get("nvidia") is False and _service_active(NVIDIA_BOOST):
        _run(["systemctl", "stop", NVIDIA_BOOST])
    return errors


def performance_wanted(config, ac):
    mode = config.get("performance", "off")
    return mode == "on" or (mode == "ac" and ac is not False)


def load_saved():
    try:
        with open(SAVED_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def store_saved(saved):
    os.makedirs(os.path.dirname(SAVED_PATH), exist_ok=True)
    tmp = SAVED_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(saved, f, indent=1)
    os.replace(tmp, SAVED_PATH)


def _clear_saved():
    try:
        os.remove(SAVED_PATH)
    except FileNotFoundError:
        pass


def ensure(config, ac, deep=True):
    """Bring performance and fans in line with config. Called by the service every cycle.

    deep: also check the power-profiles daemon and NVIDIA Dynamic Boost (starts processes).
    Returns a list of error messages (empty when everything took).
    """
    saved = load_saved()
    errors = []
    if performance_wanted(config, ac):
        if saved is None or "performance" not in saved:
            saved = dict(saved or {}, performance=capture())
            store_saved(saved)
            deep = True
        # Re-applied every cycle: Acer firmware drops Turbo on unplug, and TLP or the
        # desktop may change the energy preference. Only mismatches are written.
        errors += apply_max(deep)
    elif saved and "performance" in saved:
        errors += restore(saved.pop("performance"))
        _save_or_clear(saved)

    fan_saved = (saved or {}).get("fans", {})
    before = dict(fan_saved)
    try:
        ensure_fans(config.get("fan_mode", "auto"), config.get("fan_percent", 60), fan_saved)
    except OSError as exc:
        errors.append(f"fans: {exc.strerror or exc}")
    if config.get("fan_mode", "auto") == "auto":
        fan_saved = {}
    if fan_saved != before or (saved and saved.get("fans") and not fan_saved):
        saved = dict(saved or {})
        if fan_saved:
            saved["fans"] = fan_saved
        else:
            saved.pop("fans", None)
        _save_or_clear(saved)
    return errors


def _save_or_clear(saved):
    if saved:
        store_saved(saved)
    else:
        _clear_saved()


def release():
    """Service stopping: give fans back to the firmware and undo maximum performance."""
    saved = load_saved() or {}
    errors = []
    if saved.get("fans"):
        try:
            ensure_fans("auto", 0, saved["fans"])
        except OSError as exc:
            errors.append(f"fans: {exc}")
    if "performance" in saved:
        errors += restore(saved["performance"])
    _clear_saved()
    return errors


# --- features hidden behind module options ---------------------------------

UNLOCKS = (
    {
        "id": "acer-predator",
        "module": "acer_wmi", "option": "predator_v4", "value": "1",
        "vendor": "acer", "models": ("predator", "helios", "triton", "nitro"),
        "title": "Acer Predator controls are switched off",
        "body": "Linux hides Turbo mode and fan control on this model until the acer_wmi driver "
                "is told it's a Predator. Turning this on writes a driver option and reloads "
                "the driver.",
    },
    {
        "id": "thinkpad-fan",
        "module": "thinkpad_acpi", "option": "fan_control", "value": "1",
        "vendor": "lenovo", "models": ("thinkpad",),
        "title": "ThinkPad fan control is switched off",
        "body": "The thinkpad_acpi driver only allows fan control with its fan_control option. "
                "Turning this on writes the option and reloads the driver.",
    },
)


def _conf_path(unlock):
    return f"{MODPROBE_DIR}/powerkeeper-{unlock['id']}.conf"


def _unlock_active(unlock):
    param = core.read(f"{SYS_MODULE}/{unlock['module']}/parameters/{unlock['option']}")
    if param is not None:
        return param in ("Y", "1", unlock["value"])
    if unlock["id"] == "thinkpad-fan":
        return "commands:" in (core.read(THINKPAD_FAN) or "")
    return os.path.exists(_conf_path(unlock))


def unlocks():
    """Module options that would unlock features on this laptop and aren't on yet."""
    vendor = core.dmi("sys_vendor").lower()
    model = " ".join(core.dmi(f) for f in ("product_name", "product_family", "product_version")).lower()
    out = []
    for unlock in UNLOCKS:
        if unlock["vendor"] not in vendor or not any(m in model for m in unlock["models"]):
            continue
        if not os.path.isdir(f"{SYS_MODULE}/{unlock['module']}"):
            continue
        if not _unlock_active(unlock):
            out.append(unlock)
    return out


def apply_unlock(unlock_id):
    """Write the module option and reload the driver. Returns None, or why a restart is needed."""
    unlock = next((u for u in UNLOCKS if u["id"] == unlock_id), None)
    if unlock is None:
        raise ValueError(f"unknown option '{unlock_id}'")
    os.makedirs(MODPROBE_DIR, exist_ok=True)
    with open(_conf_path(unlock), "w") as f:
        f.write(f"# Written by Powerkeeper: {unlock['title'].lower()} otherwise.\n"
                f"options {unlock['module']} {unlock['option']}={unlock['value']}\n")
    code, _ = _run(["modprobe", "-r", unlock["module"]], 30)
    if code == 0:
        code, _ = _run(["modprobe", unlock["module"]], 30)
    if code != 0 or not _unlock_active(unlock):
        return "The driver is in use; restart the laptop to finish."
    return None


def remove_unlocks():
    for unlock in UNLOCKS:
        try:
            os.remove(_conf_path(unlock))
        except FileNotFoundError:
            pass
