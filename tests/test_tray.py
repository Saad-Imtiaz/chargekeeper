"""Tray status line. Run: python3 -m unittest discover tests"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from kernelthrottle import tray  # noqa: E402


class FakeBackend:
    available = True

    def __init__(self, limit):
        self._limit = limit

    def limit(self):
        return self._limit


class StatusLineTests(unittest.TestCase):
    config = {"calibrating": False, "top_up": False}

    def line(self, limit=80, **info):
        base = {"capacity": 70, "status": "Discharging", "ac": False, "power": 24.6, "energy_now": 40.0,
                "energy_full": 90.0}
        return tray.status_line(dict(base, **info), FakeBackend(limit), self.config)

    def test_on_battery_says_how_much_it_draws(self):
        self.assertIn("On battery, 25 W", self.line())

    def test_whole_watts_so_small_changes_send_no_signal(self):
        self.assertEqual(self.line(power=24.6, energy_now=None), self.line(power=25.4, energy_now=None))

    def test_charging_says_it_goes_into_the_battery(self):
        line = self.line(status="Charging", ac=True, power=41.0)
        self.assertEqual(line, "70% · Charging to 80%, 41 W into the battery")

    def test_plugged_in_but_draining_is_not_called_on_battery_alone(self):
        self.assertIn("Plugged in, running on battery", self.line(ac=True))

    def test_held_reports_the_real_level(self):
        # Matches the Overview: resting between the resume level and the limit.
        self.assertEqual(self.line(status="Not charging", ac=True, capacity=78), "78% · Plugged in, held at 78%")


if __name__ == "__main__":
    unittest.main()
