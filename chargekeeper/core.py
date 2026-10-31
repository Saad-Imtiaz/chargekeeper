"""Battery discovery and charge-control backends.

Everything here that only reads sysfs is safe to call as a normal user (GUI,
CLI). The write paths are reached only through chargekeeper-helper, which
runs as root via pkexec or the systemd service.
"""

import csv
import glob
import json
import os
import time

POWER_SUPPLY = "/sys/class/power_supply"
CONFIG_PATH = "/etc/chargekeeper/config.json"
DATA_DIR = "/var/lib/chargekeeper"
HISTORY_PATH = DATA_DIR + "/history.csv"
HEALTH_PATH = DATA_DIR + "/health.csv"
USAGE_PATH = DATA_DIR + "/usage.json"
STATE_PATH = "/run/chargekeeper/state.json"

ACER_DRIVER = "/sys/bus/wmi/drivers/acer-wmi-battery"
ACER_WMI_DEVICE = "/sys/bus/wmi/devices/79772EC5-04B1-4BFD-843C-61E7F77B6CC9"
PLATFORM_PROFILE = "/sys/firmware/acpi/platform_profile"
CPUFREQ = "/sys/devices/system/cpu/cpufreq"

END_NAMES = ("charge_control_end_threshold", "charge_stop_threshold")
START_NAMES = ("charge_control_start_threshold", "charge_start_threshold")

DEFAULT_CONFIG = {
    "limit_enabled": True,
    "end": 85,
    "start": 80,
    "top_up": False,
    "behaviour": "auto",
    "charge_type": None,
    "calibrating": False,
    "calibration_started": None,
}

HISTORY_KEEP_LINES = 14 * 24 * 60
USAGE_KEEP_DAYS = 8

# Helper processes reported under the app the user knows.
APP_ALIASES = {
    "Isolated Web Co": "firefox", "Web Content": "firefox", "WebExtensions": "firefox",
    "Privileged Cont": "firefox", "RDD Process": "firefox", "Socket Process": "firefox",
    "Utility Process": "firefox", "GPU Process": "firefox",
}

# Below this level "pause charging" and "run on battery" are dropped so a
# forgotten setting can never run a plugged-in laptop flat.
SAFETY_FLOOR = 20

# Calibration is switched off after this long even if the firmware never says
# it finished, so a forgotten calibration can't keep cycling the battery.
CALIBRATION_MAX_HOURS = 24

# "Charge to full" ends on unplug, or once the battery has sat full this long.
TOP_UP_FULL_HOURS = 6


def is_full(info):
    return bool(info) and (info["status"] == "Full" or (info["capacity"] or 0) >= 99)


# --- sysfs helpers ---------------------------------------------------------

def read(path, default=None):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return default


def read_int(path, default=None):
    try:
        return int(read(path))
    except (TypeError, ValueError):
        return default


def write(path, value):
    with open(path, "w") as f:
        f.write(f"{value}\n")


def write_threshold(path, value):
    """Write a charge threshold, turning the kernel's EINVAL into an actionable message."""
    try:
        write(path, value)
    except OSError as exc:
        if exc.errno == 22:
            raise OSError(22, f"the firmware does not accept {value}%. Some models only allow "
                              "80% or 100%; choose 80%") from exc
        raise


def parse_choices(text):
    """'auto [inhibit-charge] force-discharge' -> (choices, current)."""
    choices, current = [], None
    for token in (text or "").split():
        if token.startswith("[") and token.endswith("]"):
            token = token[1:-1]
            current = token
        choices.append(token)
    return choices, current


def _attr(bat, names):
    for name in names:
        path = f"{bat}/{name}"
        if os.path.exists(path):
            return path
    return None


def dmi(field):
    return read(f"/sys/class/dmi/id/{field}", "")


# --- batteries and adapters ------------------------------------------------

def batteries():
    """System batteries (not mice, headsets and other device batteries)."""
    return [
        path for path in sorted(glob.glob(POWER_SUPPLY + "/*"))
        if read(path + "/type") == "Battery" and read(path + "/scope") != "Device"
    ]


def ac_online():
    """True/False when an adapter reports its state, None when none does."""
    state = None
    for path in glob.glob(POWER_SUPPLY + "/*"):
        if read(path + "/scope") == "Device":
            continue
        if read(path + "/type") not in ("Mains", "USB", "USB_C", "USB_PD"):
            continue
        online = read_int(path + "/online")
        if online is not None:
            state = bool(state) or online == 1
    return state


def supply_props(path):
    """All properties of a power supply from one read of its uevent file.

    One read returns every value the driver reports (0.01 ms), where reading the
    attribute files one by one costs a syscall each. Keys are lowercased without the
    POWER_SUPPLY_ prefix, e.g. "energy_now". If the driver fails the combined read
    (one property erroring makes the whole file fail), fall back to single files.
    """
    text = read(path + "/uevent")
    if text:
        props = {}
        for line in text.splitlines():
            key, _, value = line.partition("=")
            if key.startswith("POWER_SUPPLY_"):
                props[key[13:].lower()] = value
        if props:
            return props
    names = ("status", "capacity", "energy_now", "energy_full", "energy_full_design", "charge_now",
             "charge_full", "charge_full_design", "power_now", "current_now", "voltage_now",
             "voltage_min_design", "cycle_count", "technology", "manufacturer", "model_name", "temp")
    return {n: v for n in names if (v := read(f"{path}/{n}")) is not None}


# Slow sensors are read at most this often. On Acer the battery temperature is a
# firmware (WMI) call that took 4 ms, more than everything else in a reading together.
TEMPERATURE_SECONDS = 30
_temperature_cache = {}


def battery_temperature(bat, props=None):
    # The uevent already carries "temp" when the driver has one; read the file otherwise.
    tenths = _int(props.get("temp") if props is not None else read(bat + "/temp"))
    # Some firmware reports 0 or a Kelvin-based value when there is no sensor.
    if tenths is not None and -200 < tenths < 1000 and tenths != 0:
        return tenths / 10
    cached = _temperature_cache.get(bat)
    if cached and time.monotonic() - cached[0] < TEMPERATURE_SECONDS:
        return cached[1]
    value = None
    for path in glob.glob(bat + "/hwmon*/temp1_input"):
        milli = read_int(path)
        if milli is not None:
            value = milli / 1000
            break
    if value is None:
        milli = read_int(ACER_DRIVER + "/temperature")
        if milli is not None and -20000 < milli < 100000:
            value = milli / 1000
    _temperature_cache[bat] = (time.monotonic(), value)
    return value


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _plausible(value, low, high):
    return value if value is not None and low <= value <= high else None


def battery_info(bat):
    props = supply_props(bat)

    def num(name):
        value = _int(props.get(name))
        return None if value is None else value / 1e6

    # Values outside these ranges are firmware glitches (seen as 0 V, or
    # 65535-style sentinels), so they're treated as missing.
    voltage = _plausible(num("voltage_now"), 1, 60)
    v_design = _plausible(num("voltage_min_design"), 1, 60) or voltage
    current = num("current_now")
    power = num("power_now")
    if power is None and voltage is not None and current is not None:
        power = voltage * current
    if power is not None:
        power = _plausible(abs(power), 0, 300)

    if "energy_full" in props or "energy_now" in props:
        now, full, design = num("energy_now"), num("energy_full"), num("energy_full_design")
    else:
        def wh(value):
            return None if value is None or v_design is None else value * v_design
        now, full, design = wh(num("charge_now")), wh(num("charge_full")), wh(num("charge_full_design"))
    now, full, design = (_plausible(v, 0.1, 1000) for v in (now, full, design))

    status = props.get("status", "Unknown")
    capacity = _plausible(_int(props.get("capacity")), 0, 100)
    if capacity is None and now and full:
        capacity = min(100, round(100 * now / full))

    return {
        "name": os.path.basename(bat),
        "status": status,
        "capacity": capacity,
        "power": power,
        "voltage": voltage,
        "voltage_design": v_design,
        "energy_now": now,
        "energy_full": full,
        "energy_design": design,
        "health": 100 * full / design if full and design else None,
        "cycle_count": _int(props.get("cycle_count")),
        "technology": props.get("technology"),
        "manufacturer": (props.get("manufacturer") or "").strip() or None,
        "model": props.get("model_name"),
        "temperature": battery_temperature(bat, props),
    }


def combined_info():
    """One summary over all system batteries (dual-battery ThinkPads)."""
    infos = [battery_info(b) for b in batteries()]
    if not infos:
        return None
    if len(infos) == 1:
        info = infos[0]
    else:
        info = dict(infos[0])
        for key in ("energy_now", "energy_full", "energy_design", "power"):
            values = [i[key] for i in infos if i[key] is not None]
            info[key] = sum(values) if values else None
        if info["energy_now"] and info["energy_full"]:
            info["capacity"] = round(100 * info["energy_now"] / info["energy_full"])
        if info["energy_full"] and info["energy_design"]:
            info["health"] = 100 * info["energy_full"] / info["energy_design"]
        statuses = {i["status"] for i in infos}
        for status in ("Charging", "Discharging"):
            if status in statuses:
                info["status"] = status
                break
        info["name"] = " + ".join(i["name"] for i in infos)
    info["ac"] = ac_online()
    if info["ac"] is None:
        # No adapter reports its state (some USB-C-only designs): infer it from the battery.
        info["ac"] = {"Charging": True, "Full": True, "Discharging": False}.get(info["status"])
    info["batteries"] = infos
    return info


def time_remaining(info, limit=100):
    """Hours until empty (discharging) or until the target (charging).

    Uses the smoothed power from power.BatteryMonitor when present: instantaneous
    readings jump with every load change, which made the estimate jump too.
    """
    power = info.get("power_avg") or info.get("power")
    now = info.get("energy_now")
    full = info.get("energy_full")
    if not power or power < 0.3 or now is None:
        return None
    if info["status"] == "Discharging":
        return now / power
    if info["status"] == "Charging" and full:
        target = full * min(limit, 100) / 100
        return max(target - now, 0) / power
    return None


# --- charge-control backends -----------------------------------------------

class Backend:
    """No charge control available."""

    id = "none"
    name = "No charge control found"
    detail = "The firmware does not expose a charge limit to Linux."
    continuous = False      # any end value from min_end to 100
    min_end = 50
    steps = (100,)          # allowed end values when not continuous
    supports_start = False
    software = False        # limit enforced by the daemon, not by firmware
    calibration = False
    min_start = 0           # lowest start threshold the firmware accepts
    min_gap = 1             # firmware needs end - start >= min_gap
    fixed_gap = None        # firmware derives start = end - fixed_gap by itself
    fixed_text = None       # label for a fixed limit whose exact value is unknown

    def __init__(self, bats):
        self.bats = bats
        self.control = [b for b in bats if os.path.exists(b + "/charge_behaviour")]
        self.typed = [b for b in bats if os.path.exists(b + "/charge_types")]

    @property
    def available(self):
        return self.id != "none"

    def snap(self, end):
        end = max(self.min_end, min(100, int(end)))
        if self.continuous:
            return end
        lower = [s for s in self.steps if s <= end]
        return max(lower) if lower else min(self.steps)

    def limit(self):
        return None

    def start(self):
        return None

    def ensure(self, end, start):
        """Make the firmware match end/start, writing only on mismatch."""

    def behaviours(self):
        return parse_choices(read(self.control[0] + "/charge_behaviour"))[0] if self.control else []

    def behaviour(self):
        return parse_choices(read(self.control[0] + "/charge_behaviour"))[1] if self.control else None

    def set_behaviour(self, value):
        for bat in self.control:
            if parse_choices(read(bat + "/charge_behaviour"))[1] != value:
                write(bat + "/charge_behaviour", value)

    def charge_types(self):
        return parse_choices(read(self.typed[0] + "/charge_types"))[0] if self.typed else []

    def charge_type(self):
        return parse_choices(read(self.typed[0] + "/charge_types"))[1] if self.typed else None

    def set_charge_type(self, value):
        for bat in self.typed:
            if parse_choices(read(bat + "/charge_types"))[1] != value:
                write(bat + "/charge_types", value)

    def calibrating(self):
        return False

    def set_calibration(self, on):
        raise OSError("Calibration is not supported on this laptop")

    def describe(self):
        return {
            "id": self.id,
            "name": self.name,
            "detail": self.detail,
            "continuous": self.continuous,
            "min_end": self.min_end,
            "steps": list(self.steps),
            "supports_start": self.supports_start,
            "software": self.software,
            "calibration": self.calibration,
            "min_start": self.min_start,
            "min_gap": self.min_gap,
            "fixed_gap": self.fixed_gap,
            "fixed_text": self.fixed_text,
            "behaviours": self.behaviours(),
            "charge_types": self.charge_types(),
        }


class ThresholdBackend(Backend):
    """Standard kernel thresholds: ThinkPad, ASUS, Dell, Framework, MSI, Huawei, LG, ..."""

    id = "thresholds"
    continuous = True
    min_end = 20

    def __init__(self, bats, threshold_bats):
        super().__init__(bats)
        self.threshold_bats = threshold_bats
        self.supports_start = any(_attr(b, START_NAMES) for b in threshold_bats)
        vendor = dmi("sys_vendor") or "Laptop"
        maker = vendor.lower()
        if maker.startswith("dell"):
            # dell-laptop: start 50-95, end 55-100, end at least 5 above start.
            self.min_end, self.min_start, self.min_gap = 55, 50, 5
        elif maker.startswith(("lg electronics", "toshiba", "dynabook")):
            # lg-laptop and toshiba_acpi accept only 80 or 100 and reject anything else.
            self.continuous, self.steps, self.min_end = False, (80, 100), 80
            self.detail = "The firmware supports a single fixed 80% limit."
        elif maker.startswith("micro-star"):
            # msi-ec keeps one value in the EC: start is always end - 10, and writing
            # start moves end. Matching that stops the daemon rewriting both each cycle.
            self.min_end, self.min_gap, self.fixed_gap = 20, 10, 10
            self.detail = "MSI firmware resumes charging 10% below the stop threshold."
        self.name = f"{vendor} firmware charge thresholds"
        self.detail = ("Start and stop thresholds are set in firmware." if self.supports_start
                       else "The stop threshold is set in firmware.")

    def limit(self):
        return read_int(_attr(self.threshold_bats[0], END_NAMES))

    def start(self):
        path = _attr(self.threshold_bats[0], START_NAMES)
        return read_int(path) if path else None

    def ensure(self, end, start):
        for bat in self.threshold_bats:
            end_path = _attr(bat, END_NAMES)
            start_path = _attr(bat, START_NAMES)
            current_end = read_int(end_path, 100)
            current_start = read_int(start_path) if start_path else None

            if start_path is None or self.fixed_gap:
                # No start threshold, or one the EC derives from end by itself (MSI).
                # Never write start there: on msi-ec that moves end to start + 10, so
                # "limit off" would otherwise stop charging at 10%.
                if current_end != end:
                    write_threshold(end_path, end)
                continue

            if start is None:
                # Limit off: charging must resume whenever below full.
                if current_end != end:
                    write_threshold(end_path, end)
                if current_start is not None and current_start > 50:
                    self._write_low_start(start_path)
                continue

            if current_end == end and current_start == start:
                continue
            # The kernel rejects start >= end, so order the writes.
            if start >= current_end:
                write_threshold(end_path, end)
                write(start_path, start)
            else:
                write(start_path, start)
                write_threshold(end_path, end)

    @staticmethod
    def _write_low_start(path):
        # ThinkPad accepts 0; Dell's minimum is 50.
        for value in (0, 50):
            try:
                write(path, value)
                return
            except OSError:
                continue


class FixedLimitBackend(Backend):
    """An on/off firmware switch with one fixed limit."""

    path = None
    on_value = "1"
    off_value = "0"
    fixed = 80

    @property
    def steps(self):
        return (self.fixed, 100)

    @property
    def min_end(self):
        return self.fixed

    def limit(self):
        return self.fixed if read(self.path) == self.on_value else 100

    def ensure(self, end, start):
        want = self.on_value if end < 100 else self.off_value
        if read(self.path) != want:
            write(self.path, want)


class AcerBackend(FixedLimitBackend):
    id = "acer"
    name = "Acer battery health mode"
    detail = "Acer firmware supports a single fixed 80% limit and battery calibration."
    path = ACER_DRIVER + "/health_mode"
    calibration = True

    def calibrating(self):
        return read(ACER_DRIVER + "/calibration_mode") == "1"

    def set_calibration(self, on):
        write(ACER_DRIVER + "/calibration_mode", 1 if on else 0)


class IdeapadBackend(FixedLimitBackend):
    id = "ideapad"
    name = "Lenovo conservation mode"
    detail = "Lenovo firmware holds the charge at a fixed level (80% on newer models, 60% on older ones)."
    fixed_text = "60–80%"

    def __init__(self, bats, path):
        super().__init__(bats)
        self.path = path


class SamsungBackend(FixedLimitBackend):
    id = "samsung"
    name = "Samsung battery life extender"
    detail = "Samsung firmware supports a single fixed 80% limit."
    path = "/sys/devices/platform/samsung/battery_life_extender"


class SonyBackend(Backend):
    id = "sony"
    name = "Sony battery care limiter"
    detail = "Sony firmware supports 50%, 80% and 100%."
    path = "/sys/devices/platform/sony-laptop/battery_care_limiter"
    steps = (50, 80, 100)

    def limit(self):
        value = read_int(self.path, 0)
        return 100 if value == 0 else value

    def ensure(self, end, start):
        want = 0 if end >= 100 else end
        if read_int(self.path) != want:
            write(self.path, want)


class SoftwareBackend(Backend):
    """No firmware threshold, but charging can be paused: the daemon enforces the limit."""

    id = "software"
    name = "Software charge limit"
    detail = "The daemon pauses charging at the limit and resumes at the start level."
    continuous = True
    supports_start = True
    software = True

    def limit(self):
        wanted = target(load_config(), self)
        return wanted[0] if wanted else 100

    def start(self):
        wanted = target(load_config(), self)
        return wanted[1] if wanted else None


def detect():
    bats = batteries()
    with_thresholds = [b for b in bats if _attr(b, END_NAMES)]
    if with_thresholds:
        return ThresholdBackend(bats, with_thresholds)
    if read(ACER_DRIVER + "/health_mode") in ("0", "1"):
        return AcerBackend(bats)
    for path in glob.glob("/sys/bus/platform/drivers/ideapad_acpi/*/conservation_mode"):
        return IdeapadBackend(bats, path)
    if os.path.exists(SamsungBackend.path):
        return SamsungBackend(bats)
    if os.path.exists(SonyBackend.path):
        return SonyBackend(bats)
    for bat in bats:
        if "inhibit-charge" in parse_choices(read(bat + "/charge_behaviour"))[0]:
            return SoftwareBackend(bats)
    return Backend(bats)


def hints():
    """Fixable reasons why charge control is missing or may misbehave."""
    out = []
    if os.path.exists(ACER_WMI_DEVICE) and not os.path.isdir(ACER_DRIVER):
        out.append({
            "id": "acer-driver",
            "title": "Acer battery driver not installed",
            "body": "This Acer laptop has the battery health interface, but Linux needs the "
                    "acer-wmi-battery driver to use it. Run the installer again with --acer "
                    "to build and load it.",
        })
    tlp_files = ["/etc/tlp.conf"] + sorted(glob.glob("/etc/tlp.d/*.conf"))
    for path in tlp_files:
        text = read(path, "")
        if any(line.strip().startswith(("START_CHARGE_THRESH", "STOP_CHARGE_THRESH"))
               for line in text.splitlines()):
            out.append({
                "id": "tlp-thresholds",
                "title": "TLP also sets charge thresholds",
                "body": f"{path} sets START/STOP_CHARGE_THRESH and will fight with Chargekeeper. "
                        "Comment those lines out.",
            })
            break
    return out


# --- configuration and policy ----------------------------------------------

def load_config():
    config = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH) as f:
            stored = json.load(f)
        config.update({k: v for k, v in stored.items() if k in DEFAULT_CONFIG})
    except (OSError, ValueError):
        pass
    return config


def save_config(config):
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(config, f, indent=2)
    os.chmod(tmp, 0o644)
    os.replace(tmp, CONFIG_PATH)


def target(config, backend):
    """(end, start) the firmware should hold, or None to leave it alone."""
    if config["calibrating"]:
        return None
    if not config["limit_enabled"] or config["top_up"]:
        return 100, None
    end = backend.snap(config["end"])
    if end >= 100:
        return 100, None
    if backend.fixed_gap:
        return end, max(end - backend.fixed_gap, 0)
    start = config.get("start")
    if start is None or start > end - backend.min_gap:
        start = end - max(5, backend.min_gap)
    return end, max(start, backend.min_start, 0)


def apply(backend, config):
    if not backend.available:
        return
    wanted = target(config, backend)
    if wanted and not backend.software:
        backend.ensure(*wanted)
    if not backend.software and config["behaviour"] in backend.behaviours():
        if backend.behaviour() != config["behaviour"]:
            backend.set_behaviour(config["behaviour"])
    if config["charge_type"] and config["charge_type"] in backend.charge_types():
        if backend.charge_type() != config["charge_type"]:
            backend.set_charge_type(config["charge_type"])


def behaviour_override(config, info):
    """Reset pause/discharge to normal when the battery is low or the charger is unplugged.

    Returns True when config was changed and should be saved.
    """
    if config["behaviour"] == "auto" or not info:
        return False
    low = info["capacity"] is not None and info["capacity"] <= SAFETY_FLOOR
    if low or info["ac"] is False:
        config["behaviour"] = "auto"
        return True
    return False


def calibration_stop_reason(backend, config, info):
    """Why a running calibration should be switched off now, or None."""
    elapsed = time.time() - (config.get("calibration_started") or time.time())
    if elapsed > CALIBRATION_MAX_HOURS * 3600:
        return f"still running after {CALIBRATION_MAX_HOURS} hours"
    if info and info["ac"] is False:
        return "the charger was unplugged"
    # Give the firmware time to start before trusting its flag.
    if elapsed > 600 and not backend.calibrating():
        return "the firmware finished"
    return None


def release(backend):
    """Let the battery charge normally; used when the service stops."""
    if backend.behaviours() and backend.behaviour() != "auto":
        backend.set_behaviour("auto")


def enforce_software(backend, config, capacity):
    """Pause charging at the limit and resume below the start level."""
    if capacity is None:
        return
    if capacity <= SAFETY_FLOOR:
        backend.set_behaviour("auto")
        return
    if config["behaviour"] == "force-discharge":
        backend.set_behaviour("force-discharge")
        return
    wanted = target(config, backend)
    if wanted is None or wanted[0] >= 100:
        backend.set_behaviour("auto")
    elif capacity >= wanted[0]:
        backend.set_behaviour("inhibit-charge")
    elif capacity <= wanted[1]:
        backend.set_behaviour("auto")


# --- history ---------------------------------------------------------------

def log_sample(info):
    os.makedirs(DATA_DIR, exist_ok=True)
    row = [
        int(time.time()),
        info["capacity"] if info["capacity"] is not None else "",
        f"{info['power']:.2f}" if info["power"] is not None else "",
        info["status"],
        "" if info["ac"] is None else int(info["ac"]),
        f"{info['temperature']:.1f}" if info["temperature"] is not None else "",
    ]
    with open(HISTORY_PATH, "a") as f:
        f.write(",".join(str(v) for v in row) + "\n")
    if os.path.getsize(HISTORY_PATH) > 1_500_000:
        with open(HISTORY_PATH) as f:
            lines = f.readlines()[-HISTORY_KEEP_LINES:]
        with open(HISTORY_PATH, "w") as f:
            f.writelines(lines)

    today = time.strftime("%Y-%m-%d")
    last = None
    try:
        with open(HEALTH_PATH) as f:
            for last in f:
                pass
    except OSError:
        pass
    if (last is None or not last.startswith(today)) and info["energy_full"]:
        with open(HEALTH_PATH, "a") as f:
            f.write(f"{today},{info['energy_full']:.2f},{info['energy_design'] or 0:.2f},"
                    f"{info['cycle_count'] if info['cycle_count'] is not None else ''}\n")


def load_history(since):
    points = []
    try:
        with open(HISTORY_PATH) as f:
            for row in csv.reader(f):
                if len(row) < 6 or int(row[0]) < since:
                    continue
                points.append({
                    "t": int(row[0]),
                    "capacity": int(row[1]) if row[1] else None,
                    "power": float(row[2]) if row[2] else None,
                    "status": row[3],
                    "ac": row[4] == "1",
                    "temperature": float(row[5]) if row[5] else None,
                })
    except (OSError, ValueError):
        pass
    return points


def load_health():
    rows = []
    try:
        with open(HEALTH_PATH) as f:
            for row in csv.reader(f):
                full, design = float(row[1]), float(row[2])
                rows.append({
                    "t": time.mktime(time.strptime(row[0], "%Y-%m-%d")),
                    "full": full,
                    "health": 100 * full / design if design else None,
                    "cycles": int(row[3]) if row[3] else None,
                })
    except (OSError, ValueError, IndexError):
        pass
    return rows


# --- battery usage ---------------------------------------------------------

class CpuSampler:
    """CPU seconds used per app between calls, from /proc/<pid>/stat."""

    def __init__(self, proc="/proc"):
        self.proc = proc
        self.last = {}
        self.last_uptime = None
        self.ticks = os.sysconf("SC_CLK_TCK")

    def sample(self):
        uptime = float((read(self.proc + "/uptime") or "0").split()[0])
        current, used_by = {}, {}
        for entry in os.scandir(self.proc):
            if not entry.name.isdigit():
                continue
            stat = read(f"{self.proc}/{entry.name}/stat")
            if not stat:
                continue
            # The name is in parentheses and may itself contain spaces or ')'.
            close = stat.rfind(")")
            name = stat[stat.find("(") + 1:close]
            fields = stat[close + 2:].split()
            try:
                ppid, used, started = int(fields[1]), int(fields[11]) + int(fields[12]), int(fields[19])
            except (IndexError, ValueError):
                continue
            pid = int(entry.name)
            if pid == 2 or ppid == 2:
                name = "Kernel"
            name = APP_ALIASES.get(name, name)
            current[pid] = (name, used, started)
            prev = self.last.get(pid)
            if prev and prev[2] == started:
                delta = used - prev[1]
            elif self.last_uptime is not None and started / self.ticks >= self.last_uptime:
                delta = used          # started since the last sample
            else:
                continue
            if delta > 0:
                used_by[name] = used_by.get(name, 0) + delta / self.ticks
        first = self.last_uptime is None
        self.last, self.last_uptime = current, uptime
        return {} if first else used_by


def load_usage():
    try:
        with open(USAGE_PATH) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def add_usage(apps, seconds_on_battery):
    """Add one interval of on-battery app CPU time to today's totals."""
    data = load_usage()
    today = data.setdefault(time.strftime("%Y-%m-%d"), {"battery_seconds": 0, "apps": {}})
    today["battery_seconds"] += seconds_on_battery
    for name, cpu in apps.items():
        today["apps"][name] = round(today["apps"].get(name, 0) + cpu, 2)
    for day in sorted(data)[:-USAGE_KEEP_DAYS]:
        del data[day]
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = USAGE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.chmod(tmp, 0o644)
    os.replace(tmp, USAGE_PATH)


def app_usage(days):
    """[(app, cpu_seconds)] on battery over the last `days` days, busiest first."""
    data = load_usage()
    keep = {time.strftime("%Y-%m-%d", time.localtime(time.time() - i * 86400)) for i in range(days)}
    totals = {}
    for day, entry in data.items():
        if day in keep:
            for name, cpu in entry.get("apps", {}).items():
                totals[name] = totals.get(name, 0) + cpu
    return sorted(totals.items(), key=lambda item: -item[1])


def usage_summary(points, max_gap=180):
    """Time, charge and energy on battery and on the charger from history samples."""
    out = {"battery_seconds": 0, "used_pct": 0, "energy_wh": 0.0,
           "plugged_seconds": 0, "charged_pct": 0}
    for a, b in zip(points, points[1:]):
        dt = b["t"] - a["t"]
        if dt <= 0 or dt > max_gap:
            continue
        if a["ac"]:
            out["plugged_seconds"] += dt
            if a["capacity"] is not None and b["capacity"] is not None:
                out["charged_pct"] += max(0, b["capacity"] - a["capacity"])
        else:
            out["battery_seconds"] += dt
            if a["power"] is not None:
                out["energy_wh"] += a["power"] * dt / 3600
            if a["capacity"] is not None and b["capacity"] is not None:
                out["used_pct"] += max(0, a["capacity"] - b["capacity"])
    hours = out["battery_seconds"] / 3600
    out["average_w"] = out["energy_wh"] / hours if hours > 0.05 else None
    return out


def daemon_state():
    try:
        with open(STATE_PATH) as f:
            state = json.load(f)
    except (OSError, ValueError):
        return None
    state["alive"] = time.time() - state.get("updated", 0) < 60
    return state


# --- power tuning ----------------------------------------------------------

def platform_profiles():
    choices = read(PLATFORM_PROFILE + "_choices")
    return (choices.split(), read(PLATFORM_PROFILE)) if choices else ([], None)


def epp_choices():
    policy = CPUFREQ + "/policy0"
    choices = read(policy + "/energy_performance_available_preferences")
    if not choices:
        return [], None
    return choices.split(), read(policy + "/energy_performance_preference")


def set_epp(value):
    for path in glob.glob(CPUFREQ + "/policy*/energy_performance_preference"):
        write(path, value)


def cpu_info():
    temp = None
    for hwmon in glob.glob("/sys/class/hwmon/hwmon*"):
        if read(hwmon + "/name") in ("coretemp", "k10temp", "zenpower"):
            temp = read_int(hwmon + "/temp1_input")
            if temp is not None:
                temp /= 1000
                break
    return {
        "driver": read(CPUFREQ + "/policy0/scaling_driver"),
        "governor": read(CPUFREQ + "/policy0/scaling_governor"),
        "temperature": temp,
    }


def service_active(name):
    import subprocess
    try:
        result = subprocess.run(["systemctl", "is-active", name], capture_output=True,
                                text=True, timeout=3)
        return result.stdout.strip() == "active"
    except (OSError, subprocess.TimeoutExpired):
        return False
