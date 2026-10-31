"""Backend logic against a fake sysfs tree. Run: python3 -m unittest discover tests"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from chargekeeper import core  # noqa: E402



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
                for capacity, expected in ((60, "auto"), (80, "inhibit-charge"), (75, "inhibit-charge"),
                                           (70, "auto"), (75, "auto")):
                    core.enforce_software(backend, config, capacity)
                    self.assertEqual(core.parse_choices(core.read(path))[1], expected, capacity)
            finally:
                core.write = original


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
