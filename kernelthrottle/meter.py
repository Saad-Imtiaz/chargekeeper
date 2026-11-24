"""Measured processor and graphics power, with no estimates.

Processor: the CPU's own energy counter (RAPL, /sys/class/powercap, Intel and AMD). The
kernel lets only root read it, because a fast, fine-grained counter can leak secrets
(CVE-2020-8694). The service (root) therefore answers on a local socket with the package
power averaged over at least METER_WINDOW seconds, rounded to 0.1 W: enough to show, far too
coarse to attack with.

Graphics: amdgpu reports power in hwmon, readable by anyone. NVIDIA reports it through NVML
(libnvidia-ml, the library nvidia-smi uses). A graphics card that is asleep (PCI runtime
suspend) uses next to nothing and is never woken to ask: it reads as "asleep". NVML is opened
and closed around every reading, because an open NVML handle keeps the card awake.
"""

import ctypes
import glob
import json
import os
import socket
import time

POWERCAP = "/sys/class/powercap"
PCI = "/sys/bus/pci/devices"
HWMON = "/sys/class/hwmon"
SOCKET_PATH = "/run/kernelthrottle/meter.sock"
METER_WINDOW = 1.0          # shortest averaging window the service hands out
CLIENT_TIMEOUT = 0.3


def _read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def _int(path):
    text = _read(path)
    try:
        return int(text)
    except (TypeError, ValueError):
        return None


# --- processor (service side) ------------------------------------------------

def rapl_packages():
    """Energy counters of each CPU package: [(energy_uj path, wrap range in µJ)].

    Only the top-level intel-rapl:N zones named package-*; intel-rapl-mmio repeats the same
    package and would count it twice, and the core/uncore/dram subzones are parts of it.
    """
    zones = []
    for zone in sorted(glob.glob(POWERCAP + "/intel-rapl:*")):
        if zone.count(":") != 1 or not (_read(zone + "/name") or "").startswith("package"):
            continue
        zones.append((zone + "/energy_uj", _int(zone + "/max_energy_range_uj") or 2 ** 32))
    return zones


class RaplMeter:
    """Package power in watts from the change of the energy counters between readings."""

    def __init__(self, window=METER_WINDOW):
        self.window = window
        self.zones = rapl_packages()
        self.last = None            # (monotonic time, [energy per zone])
        self.watts = None
        self.span = None            # seconds the last value was averaged over

    def available(self):
        return bool(self.zones)

    def read(self, now=None):
        """Watts averaged since the previous reading, or the last value if that was too recent."""
        now = time.monotonic() if now is None else now
        if not self.zones:
            return None
        energies = [_int(path) for path, _ in self.zones]
        if None in energies:
            return self.watts
        if self.last is None:
            self.last = (now, energies)
            return None
        elapsed = now - self.last[0]
        if elapsed < self.window:
            return self.watts
        joules = 0.0
        for (_, wrap), old, new in zip(self.zones, self.last[1], energies):
            delta = new - old
            if delta < 0:           # the counter wrapped
                delta += wrap
            joules += delta / 1e6
        self.last = (now, energies)
        self.watts = round(joules / elapsed, 1)
        self.span = round(elapsed, 1)
        return self.watts


class MeterServer:
    """The service's socket: every connection gets one JSON line, {"cpu": watts or null,
    "span": seconds that figure is averaged over}."""

    def __init__(self, path=SOCKET_PATH, meter=None):
        self.path = path
        self.meter = meter or RaplMeter()
        self.sock = None
        if not self.meter.available():
            return
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            if os.path.exists(path):
                os.unlink(path)
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.bind(path)
            os.chmod(path, 0o666)       # any local user may ask; the answer is coarse
            sock.listen(8)
            sock.setblocking(False)
            self.sock = sock
            self.meter.read()           # the baseline, so the first answer is a real one
        except OSError:
            self.sock = None

    def fileno(self):
        return self.sock.fileno() if self.sock else None

    def handle(self):
        """Answer every waiting connection."""
        while self.sock:
            try:
                conn, _ = self.sock.accept()
            except (BlockingIOError, OSError):
                return
            try:
                conn.settimeout(CLIENT_TIMEOUT)
                watts = self.meter.read()
                conn.sendall(json.dumps({"cpu": watts, "span": self.meter.span}).encode() + b"\n")
            except OSError:
                pass
            finally:
                conn.close()

    def close(self):
        if self.sock:
            self.sock.close()
            self.sock = None
            try:
                os.unlink(self.path)
            except OSError:
                pass


FRESH_SECONDS = 5.0


def cpu_power(path=SOCKET_PATH, fresh=True):
    """Processor package watts from the service, or None (no service or no counter).

    The service averages over the time since anyone last asked. With fresh, an average over
    more than FRESH_SECONDS (nobody asked for a while) is replaced by a new one-second
    reading, so a one-off question gets what the processor uses now. Blocks up to a second.
    """
    watts, span = _ask(path)
    if fresh and (watts is None or span is None or span > FRESH_SECONDS):
        if watts is None and span is None and not os.path.exists(path):
            return None
        time.sleep(METER_WINDOW)
        watts, span = _ask(path)
    return watts


def _ask(path):
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(CLIENT_TIMEOUT)
            sock.connect(path)
            data = b""
            while not data.endswith(b"\n"):
                chunk = sock.recv(256)
                if not chunk:
                    break
                data += chunk
        answer = json.loads(data)
        value, span = answer.get("cpu"), answer.get("span")
        return (float(value) if isinstance(value, (int, float)) else None,
                float(span) if isinstance(span, (int, float)) else None)
    except (OSError, ValueError, AttributeError):
        return None, None


# --- graphics (user side) -----------------------------------------------------

def gpus():
    """Graphics cards: [{"pci", "vendor", "awake"}], integrated ones included."""
    found = []
    for dev in sorted(glob.glob(PCI + "/*")):
        if not (_read(dev + "/class") or "").startswith("0x03"):
            continue
        found.append({"pci": os.path.basename(dev), "vendor": _read(dev + "/vendor"),
                      "awake": _read(dev + "/power/runtime_status") != "suspended"})
    return found


def _amdgpu_read(pci):
    """(watts, load percent) from amdgpu's sysfs files."""
    watts = None
    for hwmon in glob.glob(f"{PCI}/{pci}/hwmon/hwmon*"):
        for name in ("power1_average", "power1_input"):
            micro = _int(f"{hwmon}/{name}")
            if micro is not None and watts is None:
                watts = micro / 1e6
    return watts, _int(f"{PCI}/{pci}/gpu_busy_percent")


class _Utilization(ctypes.Structure):
    _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint)]


class _Nvml:
    """Just enough of NVML to read board power and load, opened and closed per reading."""

    def __init__(self):
        try:
            self.lib = ctypes.CDLL("libnvidia-ml.so.1")
        except OSError:
            self.lib = None

    def read(self, pci):
        """(watts, load percent), either None if the driver doesn't say."""
        if not self.lib:
            return None, None
        lib = self.lib
        if lib.nvmlInit_v2() != 0:
            return None, None
        try:
            handle = ctypes.c_void_p()
            if lib.nvmlDeviceGetHandleByPciBusId_v2(pci.encode(), ctypes.byref(handle)) != 0:
                return None, None
            milli, load = ctypes.c_uint(), _Utilization()
            watts = milli.value / 1000 if lib.nvmlDeviceGetPowerUsage(handle, ctypes.byref(milli)) == 0 else None
            busy = load.gpu if lib.nvmlDeviceGetUtilizationRates(handle, ctypes.byref(load)) == 0 else None
            return watts, busy
        finally:
            lib.nvmlShutdown()


# Every reading restarts the card's idle timer (NVML and amdgpu alike) (measured on an RTX 3060 laptop GPU: it
# sleeps about 24 s after the last call), so asking every couple of seconds would keep an
# idle card awake at 10-20 W. An idle card is therefore asked only every IDLE_GPU_SECONDS,
# long enough to fall asleep in between; a busy one is awake anyway and is read every time.
IDLE_GPU_SECONDS = 30.0

_nvml = None
_last = {}      # pci -> (monotonic time of the last NVML call, its result)


def _nvidia_read(pci):
    global _nvml
    if _nvml is None:
        _nvml = _Nvml()
    return _nvml.read(pci)


def _sample(pci, now, read):
    """(watts, busy) from read(pci), reusing an idle card's last answer for IDLE_GPU_SECONDS."""
    previous = _last.get(pci)
    if previous and previous[1][1] == 0 and now - previous[0] < IDLE_GPU_SECONDS:
        return previous[1]
    result = read(pci)
    _last[pci] = (now, result)
    return result


def gpu_power(now=None):
    """Power of each separate graphics card: [{"name", "watts", "state"}].

    state is "measured", "idle" (measured, and re-read only every IDLE_GPU_SECONDS so the card
    can sleep), "asleep" (watts 0, not woken to ask) or "unknown". Integrated graphics are
    part of the processor package and are left out.
    """
    now = time.monotonic() if now is None else now
    result = []
    for gpu in gpus():
        if gpu["vendor"] == "0x10de":
            name, read = "NVIDIA graphics", _nvidia_read
        elif gpu["vendor"] == "0x1002" and glob.glob(f"{PCI}/{gpu['pci']}/hwmon/hwmon*"):
            # On a laptop the AMD card that drives the screen is the processor's own graphics,
            # already inside the package figure; only a separate card is counted.
            if _read(f"{PCI}/{gpu['pci']}/boot_vga") == "1":
                continue
            name, read = "AMD graphics", _amdgpu_read
        else:
            continue
        if not gpu["awake"]:
            _last.pop(gpu["pci"], None)
            result.append({"name": name, "watts": 0.0, "state": "asleep"})
            continue
        watts, busy = _sample(gpu["pci"], now, read)
        state = "idle" if busy == 0 else "measured"
        result.append({"name": name, "watts": None if watts is None else round(watts, 1),
                       "state": "unknown" if watts is None else state})
    return result
