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

from . import core

SS_COMMAND = ["ss", "-tinpH"]

# Friendly names for hwmon chips: (label, kind).
CHIPS = {
    "coretemp": ("CPU", "cpu"), "k10temp": ("CPU", "cpu"), "zenpower": ("CPU", "cpu"),
    "acpitz": ("Motherboard (ACPI)", "board"), "nvme": ("SSD", "storage"), "drivetemp": ("Disk", "storage"),
    "amdgpu": ("GPU", "gpu"), "nouveau": ("GPU", "gpu"), "radeon": ("GPU", "gpu"),
    "iwlwifi": ("Wi-Fi", "wifi"), "dell_smm": ("Motherboard", "board"), "thinkpad": ("Motherboard", "board"),
    "asus_ec_sensors": ("Motherboard", "board"),
}
# Super I/O and chipset sensors that read the board itself, best first.
BOARD_PREFIXES = ("nct", "it8", "f71", "w83", "asus", "dell_smm", "thinkpad", "pch_", "acpitz")
CPU_LABELS = ("Package id 0", "Tctl", "Tdie", "CPU")
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
        fields = (core.read("/proc/stat") or "cpu 0").splitlines()[0].split()[1:]
        values = [int(v) for v in fields[:8]]
        idle = values[3] + (values[4] if len(values) > 4 else 0)
        return sum(values), idle

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
        """{pid: (name, cpu_ticks, rss_bytes, io_bytes or None, started)}"""
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
                ppid, ticks = int(fields[1]), int(fields[11]) + int(fields[12])
                started, rss = int(fields[19]), int(fields[21]) * self.page
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
        total, idle = self._cpu_times()
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
    label, kind = CHIPS.get(name, (None, None))
    if label is None:
        if name.startswith("pch_"):
            label, kind = "Chipset", "board"
        elif name.startswith(BOARD_PREFIXES[:5]):
            label, kind = "Motherboard", "board"
        else:
            label, kind = name.replace("_", " "), "other"
    if kind == "storage":
        model = (core.read(hwmon + "/device/model") or "").strip()
        if model:
            label = model
    return label, kind


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
    chip, kind = _chip_label(hwmon, name)
    paths = sorted(glob.glob(hwmon + "/temp*_input"), key=lambda p: int(re.sub(r"\D", "", os.path.basename(p)) or 0))
    for path in paths[:1] if first_only else paths:
        milli = core.read_int(path)
        if milli is None or not -40000 < milli < 150000:
            continue
        label = core.read(path.replace("_input", "_label")) or ""
        out.append({"chip": chip, "name": name, "label": label, "kind": kind, "value": milli / 1000})
    return out


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
    tiles.append(("battery", "Battery", battery_temp,
                  "Battery sensor" if battery_temp is not None else "Not reported"))
    board = None
    for prefix in BOARD_PREFIXES:
        board = next((s for s in all_sensors if s["name"].startswith(prefix)), None)
        if board:
            break
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
