"""Live CPU, memory, disk, network and temperature readings for the Monitor page.

Everything here runs as the normal user and only reads /proc and /sys. Per-app
disk traffic is only visible for the user's own processes (the kernel keeps
/proc/<pid>/io private), and per-app network traffic comes from TCP socket
counters reported by `ss`, so UDP (including QUIC) is not attributed.
"""

import glob
import os
import re
import time

from gi.repository import Gio, GLib

from . import core

SS_COMMAND = ["ss", "-tinpH"]

# hwmon chips whose readings all measure one thing: name prefix -> (label, kind).
CHIPS = (
    ("coretemp", "CPU", "cpu"), ("k10temp", "CPU", "cpu"), ("zenpower", "CPU", "cpu"),
    ("acpitz", "Motherboard (ACPI)", "board"), ("pch_", "Chipset", "board"),
    ("nvme", "SSD", "storage"), ("drivetemp", "Disk", "storage"),
    ("amdgpu", "GPU", "gpu"), ("nouveau", "GPU", "gpu"), ("radeon", "GPU", "gpu"),
    ("iwlwifi", "Wi-Fi", "wifi"), ("mt79", "Wi-Fi", "wifi"), ("ath1", "Wi-Fi", "wifi"),
)
# Super I/O chips (desktop-style boards); their readings are labelled per sensor.
SUPERIO = ("nct", "it8", "f71", "w83", "asus_ec_sensors")
# Embedded-controller chips mix CPU, GPU, memory and ambient readings (Dell dell_smm,
# Framework cros_ec, HP hp_wmi, ThinkPad thinkpad, ...); each reading is classified by
# its label instead. Words are matched against the lowercased label, first match wins.
LABEL_KINDS = (
    (("gpu", "video", "vga"), "gpu"),
    (("cpu", "package", "core", "tdie", "tctl", "soc"), "cpu"),
    (("battery", "batt"), "battery"),
    (("ssd", "nvme", "drive", "disk"), "storage"),
    (("ambient", "sodimm", "dimm", "ddr", "memory", "board", "systin", "auxtin", "system",
      "local", "chipset", "pch", "skin", "other"), "board"),
)
# ARM laptops name CPU and GPU thermal zones by function (cpu0_thermal, gpuss1_thermal).
NAME_KINDS = ((("cpu", "cluster", "soc"), "CPU", "cpu"), (("gpu",), "GPU", "gpu"))
# Best CPU reading first. Tdie before Tctl: on many Ryzen parts Tctl carries a +20-27 °C
# offset meant for fan control.
CPU_LABELS = ("Package id 0", "Tdie", "Tctl", "CPU")
FRIENDLY_LABELS = {"Package id 0": "CPU package", "Tctl": "CPU control (Tctl)", "Tdie": "CPU die",
                   "Composite": "Drive", "edge": "GPU edge", "junction": "GPU hotspot"}

# Reading these sends a command to the drive (10-30 ms each, and it can keep an
# SSD out of its low-power state), so only the main value is read, at most every 30 s.
SLOW_CHIPS = ("nvme", "drivetemp")
SLOW_SECONDS = 30
SKIP_PATH_PARTS = {"bin", "sbin", "lib", "libexec", "versions", "current", "app", "opt", "usr", "share"}

# Above these the value turns amber / red.
THRESHOLDS = {"cpu": (75, 90), "battery": (40, 50), "board": (65, 80), "storage": (60, 70),
              "gpu": (75, 90), "wifi": (60, 75), "other": (70, 85)}


def _read_kv(path):
    out = {}
    for line in (core.read(path) or "").splitlines():
        key, _, value = line.partition(":")
        parts = value.split()
        if parts:
            try:
                out[key.strip()] = int(parts[0])
            except ValueError:
                pass
    return out


def physical(kind, name):
    """True for real disks and network adapters (not loop, dm, lo, bridges, VPNs)."""
    return os.path.exists(f"/sys/{kind}/{name}/device")


def cpu_freq_mhz():
    values = [core.read_int(p) for p in glob.glob("/sys/devices/system/cpu/cpu[0-9]*/cpufreq/scaling_cur_freq")]
    values = [v for v in values if v]
    return sum(values) / len(values) / 1000 if values else None


class Names:
    """Readable app names per pid, cached by start time."""

    VERSIONISH = re.compile(r"^[\d.]+$")

    def __init__(self):
        self.cache = {}

    def get(self, pid, comm, started):
        key = (pid, started)
        name = self.cache.get(key)
        if name is None:
            name = comm
            # comm is cut at 15 characters and some apps rename their threads;
            # argv[0] is usually the better name then.
            if len(comm) >= 15 or self.VERSIONISH.match(comm):
                argv0 = (core.read(f"/proc/{pid}/cmdline") or "").split("\0")[0].split(" ")[0]
                # ~/.local/share/claude/versions/2.1.3 -> claude
                for part in reversed([p for p in argv0.split("/") if p]):
                    if not self.VERSIONISH.match(part) and part not in SKIP_PATH_PARTS:
                        name = part
                        break
            name = core.APP_ALIASES.get(name, name)
            self.cache[key] = name
        return name

    def prune(self, alive):
        self.cache = {k: v for k, v in self.cache.items() if k in alive}


class Monitor:
    def __init__(self):
        self.hz = os.sysconf("SC_CLK_TCK")
        self.page = os.sysconf("SC_PAGE_SIZE")
        self.ncpu = os.cpu_count() or 1
        self.uid = os.getuid()
        self.names = Names()
        self.prev = None
        self.prev_sockets = None
        self.prev_ss_time = None
        self.pid_names = {}

    # --- system totals ---

    @staticmethod
    def _cpu_times():
        """(total ticks, idle ticks, online CPUs) from /proc/stat.

        guest/guest_nice are already part of user/nice, so only the first 8 fields count.
        The CPU count comes from the same file so per-app shares use the same base as the
        total; os.cpu_count() would also count CPUs the kernel has switched off.
        """
        lines = (core.read("/proc/stat") or "cpu 0").splitlines()
        values = [int(v) for v in lines[0].split()[1:9]]
        idle = values[3] + (values[4] if len(values) > 4 else 0)
        online = sum(1 for line in lines[1:] if line.startswith("cpu") and line[3:4].isdigit())
        return sum(values), idle, max(online, 1)

    @staticmethod
    def _disk_bytes():
        read = written = 0
        for line in (core.read("/proc/diskstats") or "").splitlines():
            parts = line.split()
            if len(parts) > 9 and physical("block", parts[2]):
                read += int(parts[5]) * 512
                written += int(parts[9]) * 512
        return read, written

    @staticmethod
    def _net_bytes():
        down = up = 0
        for line in (core.read("/proc/net/dev") or "").splitlines()[2:]:
            name, _, rest = line.partition(":")
            name = name.strip()
            parts = rest.split()
            if len(parts) > 8 and name != "lo" and physical("class/net", name):
                down += int(parts[0])
                up += int(parts[8])
        return down, up

    def _processes(self):
        """{pid: (name, cpu_ticks, private_bytes, io_bytes or None, started)}

        Memory is resident minus shared pages (statm), as GNOME System Monitor shows it.
        Summing plain RSS counted shared libraries once per process, which made
        multi-process apps look 2-3x bigger (Firefox: 1.4 GB instead of 0.5 GB).
        """
        out = {}
        for entry in os.scandir("/proc"):
            if not entry.name.isdigit():
                continue
            stat = core.read(f"/proc/{entry.name}/stat")
            if not stat:
                continue
            close = stat.rfind(")")
            comm = stat[stat.find("(") + 1:close]
            fields = stat[close + 2:].split()
            try:
                ppid, ticks, started = int(fields[1]), int(fields[11]) + int(fields[12]), int(fields[19])
                rss = 0
                if int(fields[21]):     # kernel threads have no memory; skip the extra read
                    resident, shared = (core.read(f"/proc/{entry.name}/statm") or "0 0 0").split()[1:3]
                    rss = max(0, int(resident) - int(shared)) * self.page
            except (IndexError, ValueError):
                continue
            pid = int(entry.name)
            name = "Kernel" if pid == 2 or ppid == 2 else self.names.get(pid, comm, started)
            io = None
            try:
                if self.uid == 0 or entry.stat().st_uid == self.uid:
                    counters = _read_kv(f"/proc/{entry.name}/io")
                    if counters:
                        io = (counters.get("read_bytes", 0), counters.get("write_bytes", 0))
            except OSError:
                pass
            out[pid] = (name, ticks, rss, io, started)
        return out

    def sample(self):
        """One reading. Rates and percentages need two calls; the first returns zeros."""
        now = time.monotonic()
        total, idle, self.ncpu = self._cpu_times()
        disk = self._disk_bytes()
        net = self._net_bytes()
        procs = self._processes()
        mem = _read_kv("/proc/meminfo")

        result = {
            "cpu_pct": 0.0, "cpu_count": self.ncpu, "cpu_mhz": cpu_freq_mhz(),
            "mem_total": mem.get("MemTotal", 0) * 1024,
            "mem_used": (mem.get("MemTotal", 0) - mem.get("MemAvailable", 0)) * 1024,
            "swap_total": mem.get("SwapTotal", 0) * 1024,
            "swap_used": (mem.get("SwapTotal", 0) - mem.get("SwapFree", 0)) * 1024,
            "disk_read": 0.0, "disk_write": 0.0, "net_down": 0.0, "net_up": 0.0,
            "top_cpu": [], "top_mem": [], "top_disk": [],
        }

        mem_by = {}
        for name, _ticks, rss, _io, _started in procs.values():
            if rss:
                mem_by[name] = mem_by.get(name, 0) + rss
        result["top_mem"] = top(mem_by)

        if self.prev:
            p_now, p_total, p_idle, p_disk, p_net, p_procs = self.prev
            dt = max(now - p_now, 0.001)
            span = max(total - p_total, 1)
            result["cpu_pct"] = 100 * (span - (idle - p_idle)) / span
            result["disk_read"] = max(0, disk[0] - p_disk[0]) / dt
            result["disk_write"] = max(0, disk[1] - p_disk[1]) / dt
            result["net_down"] = max(0, net[0] - p_net[0]) / dt
            result["net_up"] = max(0, net[1] - p_net[1]) / dt

            cpu_by, disk_by = {}, {}
            for pid, (name, ticks, _rss, io, started) in procs.items():
                before = p_procs.get(pid)
                if not before or before[4] != started:
                    continue
                if ticks > before[1]:
                    pct = 100 * (ticks - before[1]) / self.hz / dt / self.ncpu
                    cpu_by[name] = cpu_by.get(name, 0) + pct
                if io and before[3]:
                    rate = (max(0, io[0] - before[3][0]) + max(0, io[1] - before[3][1])) / dt
                    if rate:
                        disk_by[name] = disk_by.get(name, 0) + rate
            result["top_cpu"] = top(cpu_by)
            result["top_disk"] = top(disk_by)

        self.prev = (now, total, idle, disk, net, procs)
        self.pid_names = {pid: p[0] for pid, p in procs.items()}
        self.names.prune({(pid, p[4]) for pid, p in procs.items()})
        return result

    # --- per-app network from `ss -tinpH` output ---

    def network_apps(self, ss_output):
        """Top apps by TCP bytes per second since the previous call."""
        now = time.monotonic()
        sockets = parse_ss(ss_output)
        result = []
        if self.prev_sockets is not None:
            dt = max(now - self.prev_ss_time, 0.001)
            by_app = {}
            for key, (pid, sent, received) in sockets.items():
                before = self.prev_sockets.get(key)
                if not before:
                    continue
                rate = (max(0, sent - before[1]) + max(0, received - before[2])) / dt
                if rate:
                    name = self.pid_names.get(pid) or f"pid {pid}"
                    by_app[name] = by_app.get(name, 0) + rate
            result = top(by_app)
        self.prev_sockets, self.prev_ss_time = sockets, now
        return result


SS_USER = re.compile(r'users:\(\("[^"]*",pid=(\d+)')
SS_COUNTER = re.compile(r"\b(bytes_acked|bytes_received):(\d+)")


def parse_ss(text):
    """{(local, peer): (pid, bytes_sent, bytes_received)} for sockets with a known owner."""
    sockets = {}
    lines = (text or "").splitlines()
    for head, info in zip(lines[::2], lines[1::2]):
        parts = head.split()
        user = SS_USER.search(head)
        if len(parts) < 4 or not user:
            continue
        counters = dict(SS_COUNTER.findall(info))
        sockets[(parts[2], parts[3])] = (int(user.group(1)), int(counters.get("bytes_acked", 0)),
                                         int(counters.get("bytes_received", 0)))
    return sockets


def top(values, n=3):
    return sorted(values.items(), key=lambda item: -item[1])[:n]


# --- temperatures ------------------------------------------------------------

def _chip_label(hwmon, name):
    """(chip label, kind) for a whole hwmon chip; kind None means classify per reading."""
    for prefix, label, kind in CHIPS:
        if name.startswith(prefix):
            if kind == "storage":
                label = (core.read(hwmon + "/device/model") or "").strip() or label
            return label, kind
    if name.startswith(SUPERIO):
        return "Motherboard", None
    for words, label, kind in NAME_KINDS:
        if any(w in name.lower() for w in words):
            return label, kind
    return name.replace("_", " "), None


def classify(label):
    """Kind of a single reading from its label, for mixed chips."""
    text = label.lower()
    for words, kind in LABEL_KINDS:
        if any(w in text for w in words):
            return kind
    return "other"


_slow_cache = {}


def sensors():
    """Every temperature sensor: [{chip, name, label, kind, value}]."""
    out = []
    for hwmon in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
        name = core.read(hwmon + "/name") or ""
        if name.startswith("BAT"):
            continue
        if name in SLOW_CHIPS:
            cached = _slow_cache.get(hwmon)
            if not cached or time.monotonic() - cached[0] > SLOW_SECONDS:
                cached = (time.monotonic(), _read_chip(hwmon, name, first_only=True))
                _slow_cache[hwmon] = cached
            out.extend(cached[1])
        else:
            out.extend(_read_chip(hwmon, name))
    return out


def _read_chip(hwmon, name, first_only=False):
    out = []
    chip, chip_kind = _chip_label(hwmon, name)
    paths = sorted(glob.glob(hwmon + "/temp*_input"), key=lambda p: int(re.sub(r"\D", "", os.path.basename(p)) or 0))
    for path in paths[:1] if first_only else paths:
        milli = core.read_int(path)
        if milli is None or not -40000 < milli < 150000:
            continue
        label = core.read(path.replace("_input", "_label")) or ""
        kind = chip_kind or classify(label)
        out.append({"chip": chip, "name": name, "label": label, "kind": kind, "value": milli / 1000})
    return out


def pick_board(all_sensors):
    """Best motherboard reading: Super I/O, then labelled EC readings, then chipset, then ACPI."""
    boards = [s for s in all_sensors if s["kind"] == "board"]
    ranked = (
        [s for s in boards if s["name"].startswith(SUPERIO)],
        [s for s in boards if not s["name"].startswith(("pch_", "acpitz"))],
        [s for s in boards if s["name"].startswith("pch_")],
        [s for s in boards if s["name"].startswith("acpitz")],
    )
    for group in ranked:
        if group:
            return group[0]
    return None


def highlights(all_sensors, battery_temp):
    """The headline readings: CPU, battery, motherboard, then SSD and GPU when present."""
    def pick(kind, labels=()):
        candidates = [s for s in all_sensors if s["kind"] == kind]
        for wanted in labels:
            for s in candidates:
                if s["label"] == wanted:
                    return s
        return max(candidates, key=lambda s: s["value"]) if candidates else None

    tiles = []
    cpu = pick("cpu", CPU_LABELS)
    tiles.append(("cpu", "CPU", cpu["value"] if cpu else None,
                  FRIENDLY_LABELS.get(cpu["label"], cpu["label"] or cpu["chip"]) if cpu else "No sensor found"))
    source = "Battery sensor"
    if battery_temp is None:
        # Some EC drivers (Framework cros_ec, Dell) report the pack temperature as a sensor.
        reading = pick("battery")
        battery_temp, source = (reading["value"], f"{reading['chip']} · {reading['label']}") if reading else (None, "")
    tiles.append(("battery", "Battery", battery_temp, source if battery_temp is not None else "Not reported"))
    board = pick_board(all_sensors)
    tiles.append(("board", "Motherboard", board["value"] if board else None,
                  (board["chip"] + (f" · {board['label']}" if board["label"] else "")) if board else "No sensor found"))
    storage = pick("storage", ("Composite",))
    if storage:
        tiles.append(("storage", "SSD", storage["value"], storage["chip"]))
    gpu = pick("gpu", ("edge", "junction"))
    if gpu:
        tiles.append(("gpu", "GPU", gpu["value"], FRIENDLY_LABELS.get(gpu["label"], gpu["label"] or gpu["chip"])))
    return tiles


def level(kind, value):
    """'ok', 'warm' or 'hot' for a reading."""
    if value is None:
        return "ok"
    warm, hot = THRESHOLDS.get(kind, THRESHOLDS["other"])
    return "hot" if value >= hot else "warm" if value >= warm else "ok"


def fmt_bytes(n, rate=False):
    suffix = "/s" if rate else ""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1000 or unit == "TB":
            if unit == "B":
                return f"{n:.0f} {unit}{suffix}"
            return f"{n:.1f} {unit}{suffix}" if n < 100 else f"{n:.0f} {unit}{suffix}"
        n /= 1000
    return ""


# --- app names and icons -------------------------------------------------------

_apps = {}


def app_identity(name):
    """(display name, Gio.Icon) for a process name, from its .desktop file when one matches."""
    if name in _apps:
        return _apps[name]
    result = (name, Gio.ThemedIcon.new("application-x-executable-symbolic"))
    if name == "Kernel":
        result = ("Kernel", Gio.ThemedIcon.new("system-run-symbolic"))
    else:
        wanted = name.lower()
        try:
            groups = Gio.DesktopAppInfo.search(name)
        except GLib.Error:
            groups = []
        for desktop_id in [i for group in groups[:3] for i in group]:
            info = Gio.DesktopAppInfo.new(desktop_id)
            if not info:
                continue
            stem = desktop_id.removesuffix(".desktop").lower()
            candidates = {
                os.path.basename(info.get_executable() or "").lower(),
                (info.get_string("StartupWMClass") or "").lower(),
                stem, stem.split("_")[-1], stem.split(".")[-1],
            }
            if wanted in candidates:
                result = (info.get_display_name() or name, info.get_icon() or result[1])
                break
    _apps[name] = result
    return result
