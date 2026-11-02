"""Live battery engine and daemon scheduling. Run: python3 -m unittest discover tests"""

import os
import socket
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from chargekeeper import core, helper, power  # noqa: E402


class SmootherTests(unittest.TestCase):
    def test_converges_and_resets(self):
        s = power.PowerSmoother(tau=60)
        self.assertEqual(s.update("Discharging", 10, 0), 10)
        value = s.update("Discharging", 20, 60)          # one time constant: ~63% of the step
        self.assertAlmostEqual(value, 10 + 10 * (1 - 1 / 2.718281828), places=2)
        self.assertEqual(s.update("Charging", 40, 61), 40)  # new state starts over

    def test_spike_is_damped(self):
        s = power.PowerSmoother(tau=60)
        for t in range(0, 120, 2):
            s.update("Discharging", 8, t)
        self.assertLess(s.update("Discharging", 40, 122), 11)   # a 2 s spike barely moves it


class MonitorTests(unittest.TestCase):
    def run_monitor(self, readings):
        original = core.combined_info
        feed = iter(readings)
        core.combined_info = lambda: dict(next(feed))
        try:
            m = power.BatteryMonitor()
            return [m.read(now=t) for t, _ in zip(range(0, 1000, 30), readings)]
        finally:
            core.combined_info = original

    def test_power_derived_from_energy(self):
        base = {"status": "Discharging", "power": None, "capacity": 50, "ac": False}
        out = self.run_monitor([dict(base, energy_now=30.0), dict(base, energy_now=29.9),
                                dict(base, energy_now=29.8)])
        self.assertIsNone(out[0]["power"])
        self.assertAlmostEqual(out[1]["power"], 0.1 * 3600 / 30, places=3)   # 12 W

    def test_reported_power_kept(self):
        base = {"status": "Discharging", "capacity": 50, "energy_now": 30.0, "ac": False}
        out = self.run_monitor([dict(base, power=9.0), dict(base, power=11.0)])
        self.assertEqual(out[1]["power"], 11.0)
        self.assertTrue(9.0 < out[1]["power_avg"] < 11.0)

    def test_steady_power_follows_load_but_damps_a_spike(self):
        base = {"status": "Discharging", "capacity": 50, "energy_now": 30.0, "ac": False}
        monitor = power.BatteryMonitor()
        original = core.combined_info
        self.addCleanup(setattr, core, "combined_info", original)
        readings = iter([20.0] * 5 + [60.0] + [20.0] * 5 + [60.0] * 30)
        core.combined_info = lambda: dict(base, power=next(readings))
        out = [monitor.read(now=t * 2) for t in range(41)]
        self.assertEqual(out[4]["power_steady"], 20.0)
        self.assertLess(out[5]["power_steady"], 30.0)       # one 2 s spike barely shows
        self.assertGreater(out[40]["power_steady"], 58.0)   # a real change arrives within a minute

    def test_charger_only_read_with_a_charger(self):
        base = {"status": "Not charging", "capacity": 80, "energy_now": 30.0, "power": 0.0}
        original = core.combined_info, core.charger_power
        self.addCleanup(lambda: (setattr(core, "combined_info", original[0]),
                                 setattr(core, "charger_power", original[1])))
        core.charger_power = lambda: 33.0
        core.combined_info = lambda: dict(base, ac=True)
        self.assertEqual(power.BatteryMonitor().read(now=0)["charger_power"], 33.0)
        core.combined_info = lambda: dict(base, ac=False)
        self.assertIsNone(power.BatteryMonitor().read(now=0)["charger_power"])

    def test_changed(self):
        a = {"status": "Charging", "capacity": 50, "ac": True, "energy_now": 30.0, "power": 20.0}
        self.assertFalse(power.changed(a, dict(a, power=20.01)))
        self.assertTrue(power.changed(a, dict(a, capacity=51)))
        self.assertTrue(power.changed(a, None))


class TimeRemainingTests(unittest.TestCase):
    def test_uses_estimate(self):
        info = {"status": "Discharging", "energy_now": 40.0, "energy_full": 80.0, "power": 40.0, "power_avg": 10.0}
        self.assertAlmostEqual(core.time_remaining(info), 4.0)
        self.assertAlmostEqual(core.time_remaining(info, power=20.0), 2.0)     # "at the current load"

    def test_charging_slows_above_80_percent(self):
        info = {"status": "Charging", "energy_now": 50.0, "energy_full": 100.0, "power": 40.0}
        # 50 -> 80 Wh at 40 W, then 80 -> 100 Wh at half the rate.
        self.assertAlmostEqual(core.time_remaining(info), 30 / 40 + 20 / 20)
        self.assertAlmostEqual(core.time_remaining(info, limit=80), 30 / 40)
        tapering = dict(info, energy_now=90.0, power=10.0)          # measured rate already reflects it
        self.assertAlmostEqual(core.time_remaining(tapering), 1.0)
        self.assertEqual(core.time_remaining(dict(info, energy_now=85.0), limit=80), 0.0)


class EstimatorAccuracyTests(unittest.TestCase):
    """The estimate must predict time to empty better than the old 1-minute average.

    Synthetic sessions with noise and 20 s bursts every 10 minutes: mixed use with a
    video call, gaming then idle, and steady work. Error is against the real remaining
    time of the same session, scored once a minute.
    """

    def session(self, kind, seed):
        import random
        rnd = random.Random(seed)

        def load(t):
            m = t / 60
            base = {"mixed": 16 if 60 <= m < 85 else (6 if m >= 130 else 8),
                    "gaming-then-idle": 35 if m < 40 else 7, "steady": 9}[kind]
            return max(1, base + (18 if t % 600 < 20 else 0) + rnd.gauss(0, 1.2))
        return [(t, load(t)) for t in range(0, 3 * 3600, 5)]

    def mean_error(self, make_estimate):
        errors = []
        for kind in ("mixed", "gaming-then-idle", "steady"):
            trace = self.session(kind, 1)
            left = [0.0] * len(trace)
            total = 0.0
            for i in range(len(trace) - 1, -1, -1):
                total += trace[i][1] * 5 / 3600
                left[i] = total
            estimate = make_estimate()
            for i, (t, p) in enumerate(trace):
                watts = estimate(t, p)
                if i % 12 == 0 and t >= 300 and left[i] > 2:
                    true_hours = (len(trace) - i) * 5 / 3600
                    errors.append(abs(left[i] / watts - true_hours) / true_hours)
        return sum(errors) / len(errors)

    def test_beats_one_minute_average(self):
        def old():
            s = power.PowerSmoother(60)
            return lambda t, p: s.update("Discharging", p, t)

        def new():
            e = power.Estimator()
            return lambda t, p: e.update("Discharging", p, t)[0]
        self.assertLess(self.mean_error(new), self.mean_error(old) * 0.97)


class DaemonScheduleTests(unittest.TestCase):
    def test_intervals(self):
        hw = core.ThresholdBackend([], [])
        sw = core.SoftwareBackend([])
        idle = dict(core.DEFAULT_CONFIG, end=80, start=70)
        charging = {"status": "Charging", "capacity": 60}
        self.assertEqual(helper.next_interval(hw, idle, charging), helper.IDLE_SECONDS)
        self.assertEqual(helper.next_interval(hw, dict(idle, top_up=True), charging), helper.ACTIVE_SECONDS)
        self.assertEqual(helper.next_interval(sw, idle, charging), helper.SOFTWARE_SECONDS)
        self.assertEqual(helper.next_interval(sw, idle, dict(charging, capacity=78)), helper.SOFTWARE_NEAR_SECONDS)
        self.assertEqual(helper.next_interval(sw, idle, dict(charging, status="Discharging")), helper.IDLE_SECONDS)
        self.assertEqual(helper.next_interval(None, None, None), helper.IDLE_SECONDS)

    def test_wait_wakes_on_power_event_only(self):
        class FakeEvents:
            def __init__(self):
                self.r, self.w = socket.socketpair()
                self.r.setblocking(False)

            def fileno(self):
                return self.r.fileno()

            def drain(self):
                relevant = False
                while True:
                    try:
                        relevant |= b"power_supply" in self.r.recv(4096)
                    except BlockingIOError:
                        return relevant

        events = FakeEvents()
        self.addCleanup(events.r.close)
        self.addCleanup(events.w.close)
        threading.Timer(0.2, lambda: events.w.send(b"ACTION=change\0SUBSYSTEM=usb")).start()
        threading.Timer(0.4, lambda: events.w.send(b"ACTION=change\0SUBSYSTEM=power_supply")).start()
        start = time.monotonic()
        self.assertTrue(helper.wait_for_event(events, 5))
        self.assertLess(time.monotonic() - start, 1.5)          # woke for the power event...
        self.assertGreater(time.monotonic() - start, 0.35)      # ...not for the USB one
        start = time.monotonic()
        self.assertFalse(helper.wait_for_event(events, 0.3))    # nothing pending: times out


class UeventTests(unittest.TestCase):
    def test_listener_opens_or_degrades(self):
        events = power.UeventMonitor()
        self.assertFalse(events.drain())      # nothing pending, and never blocks
        events.close()


class SupplyPropsTests(unittest.TestCase):
    def test_uevent_parsing_and_sanity(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            bat = os.path.join(tmp, "BAT0")
            os.makedirs(bat)
            with open(os.path.join(bat, "uevent"), "w") as f:
                f.write("POWER_SUPPLY_NAME=BAT0\nPOWER_SUPPLY_STATUS=Discharging\nPOWER_SUPPLY_CAPACITY=55\n"
                        "POWER_SUPPLY_VOLTAGE_NOW=0\nPOWER_SUPPLY_POWER_NOW=9500000\n"
                        "POWER_SUPPLY_ENERGY_NOW=30000000\nPOWER_SUPPLY_ENERGY_FULL=60000000\n"
                        "POWER_SUPPLY_ENERGY_FULL_DESIGN=65000000\nPOWER_SUPPLY_TEMP=0\n")
            info = core.battery_info(bat)
            self.assertEqual((info["status"], info["capacity"], info["power"]), ("Discharging", 55, 9.5))
            self.assertIsNone(info["voltage"])         # 0 V is a glitch, not a reading
            self.assertAlmostEqual(info["health"], 100 * 60 / 65)


if __name__ == "__main__":
    unittest.main()
