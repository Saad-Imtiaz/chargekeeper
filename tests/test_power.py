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
        base = {"status": "Discharging", "power": None, "capacity": 50}
        out = self.run_monitor([dict(base, energy_now=30.0), dict(base, energy_now=29.9),
                                dict(base, energy_now=29.8)])
        self.assertIsNone(out[0]["power"])
        self.assertAlmostEqual(out[1]["power"], 0.1 * 3600 / 30, places=3)   # 12 W

    def test_reported_power_kept(self):
        base = {"status": "Discharging", "capacity": 50, "energy_now": 30.0}
        out = self.run_monitor([dict(base, power=9.0), dict(base, power=11.0)])
        self.assertEqual(out[1]["power"], 11.0)
        self.assertTrue(9.0 < out[1]["power_avg"] < 11.0)

    def test_changed(self):
        a = {"status": "Charging", "capacity": 50, "ac": True, "energy_now": 30.0, "power": 20.0}
        self.assertFalse(power.changed(a, dict(a, power=20.01)))
        self.assertTrue(power.changed(a, dict(a, capacity=51)))
        self.assertTrue(power.changed(a, None))


class TimeRemainingTests(unittest.TestCase):
    def test_uses_smoothed_power(self):
        info = {"status": "Discharging", "energy_now": 40.0, "energy_full": 80.0, "power": 40.0, "power_avg": 10.0}
        self.assertAlmostEqual(core.time_remaining(info), 4.0)


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
