"""Live battery readings that cost as little as possible.

How a reading is kept fresh without spending power on it:

1. Kernel events. Battery and charger drivers send a uevent whenever something
   changes that the firmware reports (charger plugged or unplugged, charging
   started or stopped, most firmware also on every 1% step). UeventMonitor listens
   on the kernel's netlink socket, which needs no privileges and no polling.
2. One cheap read. A reading parses each supply's uevent file once (core.battery_info)
   and caches slow sensors, about 0.1 ms in total.
3. Polling only on demand. Code that shows live numbers asks for an interval with
   LiveBattery.want(); with nobody asking, a slow heartbeat only catches firmware
   that never sends events. The kernel caches battery values for a second anyway
   (battery.cache_time), so faster than MIN_INTERVAL is never useful.
4. Estimating. Instantaneous power jumps with every load change, so time remaining
   uses a 3-minute average of this session's power (see Estimator for why), which
   restarts when the charging state changes. It reuses readings taken anyway. Power
   is derived from the energy change when the firmware doesn't report it.

Everything here only reads.
"""

import math
import socket
import time

from . import core

NETLINK_KOBJECT_UEVENT = 15
MIN_INTERVAL = 1.0
HEARTBEAT = 60.0
STEADY_SECONDS = 8.0          # for showing watts: readable, but follows a real change
SMOOTHING_SECONDS = 60.0      # fast average: what the laptop is doing now
PATTERN_SECONDS = 180.0       # estimate: the recent pattern, without brief spikes
DERIVE_MIN_SECONDS = 30.0     # shortest window for power from energy change
DEBOUNCE_MS = 150             # drivers often send several events in a burst


class UeventMonitor:
    """Non-blocking listener for kernel power_supply uevents. fileno() is None if unavailable."""

    def __init__(self):
        self.sock = None
        try:
            sock = socket.socket(socket.AF_NETLINK, socket.SOCK_DGRAM, NETLINK_KOBJECT_UEVENT)
            sock.bind((0, 1))           # port 0: the kernel assigns one; group 1: kernel events
            sock.setblocking(False)
            self.sock = sock
        except OSError:
            pass    # containers and some sandboxes forbid netlink: callers fall back to polling

    def fileno(self):
        return self.sock.fileno() if self.sock else None

    def drain(self):
        """Read every pending event; True if any concerned a power supply."""
        relevant = False
        while self.sock:
            try:
                data = self.sock.recv(65536)
            except BlockingIOError:
                break
            except OSError:
                break
            if b"SUBSYSTEM=power_supply" in data:
                relevant = True
        return relevant

    def close(self):
        if self.sock:
            self.sock.close()
            self.sock = None


class PowerSmoother:
    """Exponential average of power that restarts when the charging state changes."""

    def __init__(self, tau=SMOOTHING_SECONDS):
        self.tau = tau
        self.reset(None)

    def reset(self, status):
        self.status, self.value, self.time = status, None, None

    def update(self, status, power, now):
        if status != self.status:
            self.reset(status)
        if power is None:
            return self.value
        if self.value is None:
            self.value, self.time = power, now
        else:
            alpha = 1 - math.exp(-max(now - self.time, 0) / self.tau)
            self.value += alpha * (power - self.value)
            self.time = now
        return self.value


class Estimator:
    """Power to base time estimates on: a 3-minute average of this session's use.

    Chosen by simulation (tests/test_power.py, EstimatorAccuracyTests) against a
    1-minute average, a 10-minute average, the session average, last week's typical
    power, medians and blends: the plain 3-minute average had the lowest error over
    mixed, gaming-then-idle and steady sessions (14.5% vs 16.0% for the old 1-minute
    average; 4.3% vs 6.7% for steady work). Longer memory and history lost when the
    load changed. The 1-minute average is kept for "at the current load".
    """

    def __init__(self):
        self.fast = PowerSmoother(SMOOTHING_SECONDS)
        self.pattern = PowerSmoother(PATTERN_SECONDS)

    def update(self, status, power, now):
        """(estimate, fast) in watts, either None while there is nothing to go on."""
        return self.pattern.update(status, power, now), self.fast.update(status, power, now)


class BatteryMonitor:
    """Stateful readings: adds power_avg (the estimate), power_fast, power_steady (to display)
    and charger_power, and fills in missing power."""

    def __init__(self, estimator=None):
        self.estimator = estimator or Estimator()
        self.steady = PowerSmoother(STEADY_SECONDS)
        self.anchor = None          # (time, energy_now, status) for deriving power

    def read(self, now=None):
        now = time.monotonic() if now is None else now
        info = core.combined_info()
        if not info:
            return None
        status, energy, power = info["status"], info["energy_now"], info["power"]

        # Some firmware never reports power (or reports 0 while clearly charging or
        # draining): use the energy change over at least DERIVE_MIN_SECONDS.
        if (not power) and status in ("Charging", "Discharging") and energy is not None:
            if self.anchor and self.anchor[2] == status:
                elapsed = now - self.anchor[0]
                if elapsed >= DERIVE_MIN_SECONDS:
                    derived = abs(energy - self.anchor[1]) * 3600 / elapsed
                    if derived > 0:
                        info["power"] = power = derived
                    self.anchor = (now, energy, status)
            else:
                self.anchor = (now, energy, status)
        else:
            self.anchor = (now, energy, status) if energy is not None else None

        info["power_avg"], info["power_fast"] = self.estimator.update(status, power, now)
        info["power_steady"] = self.steady.update(status, power, now)
        # Only worth the extra reads with a charger connected.
        info["charger_power"] = core.charger_power() if info["ac"] else None
        return info


def changed(old, new):
    """Whether a new reading differs from the last one in anything worth redrawing."""
    if (old is None) != (new is None):
        return True
    if new is None:
        return False
    keys = ("status", "capacity", "ac", "energy_now", "power", "temperature")
    for key in keys:
        a, b = old.get(key), new.get(key)
        if isinstance(a, float) and isinstance(b, float):
            if abs(a - b) > 0.05:
                return True
        elif a != b:
            return True
    return False


class LiveBattery:
    """App-wide live reading, driven by kernel events plus on-demand polling (GLib).

    subscribe(callback) gets callback(info) after every reading that changed.
    want(key, seconds) asks for polling at least that often until release(key).
    """

    def __init__(self):
        from gi.repository import GLib
        self.GLib = GLib
        self.monitor = BatteryMonitor()
        self.events = UeventMonitor()
        self.listeners = []
        self.wants = {}
        self.info = None
        self.timer = 0
        self.debounce = 0
        self.watch = 0
        if self.events.fileno() is not None:
            self.watch = GLib.unix_fd_add_full(GLib.PRIORITY_DEFAULT, self.events.fileno(),
                                               GLib.IOCondition.IN, self._on_event)
        self.refresh()
        self._schedule()

    @property
    def interval(self):
        if self.wants:
            return max(MIN_INTERVAL, min(self.wants.values()))
        # Without events every change has to be polled for, so keep it moderate.
        return HEARTBEAT if self.watch else 15.0

    def subscribe(self, callback):
        self.listeners.append(callback)

    def want(self, key, seconds):
        if self.wants.get(key) != seconds:
            self.wants[key] = seconds
            self.refresh()
            self._schedule()

    def release(self, key):
        if self.wants.pop(key, None) is not None:
            self._schedule()

    def refresh(self, force=False):
        info = self.monitor.read()
        if force or changed(self.info, info):
            self.info = info
            for callback in list(self.listeners):
                callback(info)
        else:
            # Keep smoothed values current without redrawing anything.
            self.info = info
        return info

    def _schedule(self):
        if self.timer:
            self.GLib.source_remove(self.timer)
        self.timer = self.GLib.timeout_add(int(self.interval * 1000), self._tick)

    def _tick(self):
        self.refresh()
        return self.GLib.SOURCE_CONTINUE

    def _on_event(self, _fd, _condition):
        if self.events.drain() and not self.debounce:
            self.debounce = self.GLib.timeout_add(DEBOUNCE_MS, self._after_event)
        return self.GLib.SOURCE_CONTINUE

    def _after_event(self):
        self.debounce = 0
        self.refresh(force=True)
        self._schedule()        # restart the interval from this fresh reading
        return self.GLib.SOURCE_REMOVE

    def close(self):
        for source in (self.timer, self.debounce, self.watch):
            if source:
                self.GLib.source_remove(source)
        self.timer = self.debounce = self.watch = 0
        self.events.close()
