"""Monitor parsing and selection logic. Run: python3 -m unittest discover tests"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from chargekeeper import monitor  # noqa: E402

SS = """0      0      10.0.0.2:46516 1.2.3.4:443 users:(("firefox",pid=10,fd=18))
\t cubic bytes_sent:5000 bytes_acked:4000 bytes_received:9000 segs_out:7
0      0      10.0.0.2:46520 1.2.3.4:443 users:(("curl",pid=11,fd=3))
\t cubic bytes_acked:100 bytes_received:200
0      0      10.0.0.2:46530 1.2.3.4:443
\t cubic bytes_acked:1 bytes_received:1
"""


class SsTests(unittest.TestCase):
    def test_parse(self):
        sockets = monitor.parse_ss(SS)
        self.assertEqual(sockets[("10.0.0.2:46516", "1.2.3.4:443")], (10, 4000, 9000))
        self.assertEqual(len(sockets), 2)   # the socket without an owner is skipped

    def test_rates(self):
        m = monitor.Monitor()
        m.pid_names = {10: "firefox", 11: "curl"}
        self.assertEqual(m.network_apps(SS), [])            # first call is the baseline
        m.prev_ss_time -= 2                                   # pretend two seconds passed
        later = SS.replace("bytes_received:9000", "bytes_received:19000")
        apps = m.network_apps(later)
        self.assertEqual(apps[0][0], "firefox")
        self.assertAlmostEqual(apps[0][1], 5000, delta=50)


class SelectionTests(unittest.TestCase):
    def sensor(self, name, chip, kind, label, value):
        return {"name": name, "chip": chip, "kind": kind, "label": label, "value": value}

    def test_highlights(self):
        sensors = [
            self.sensor("coretemp", "CPU", "cpu", "Core 0", 50),
            self.sensor("coretemp", "CPU", "cpu", "Package id 0", 55),
            self.sensor("acpitz", "Motherboard (ACPI)", "board", "", 41),
            self.sensor("nct6775", "Motherboard", "board", "SYSTIN", 35),
            self.sensor("nvme", "Lexar", "storage", "Composite", 38),
        ]
        tiles = {t[0]: t for t in monitor.highlights(sensors, 24.0)}
        self.assertEqual(tiles["cpu"][2:], (55, "CPU package"))
        self.assertEqual(tiles["battery"][2], 24.0)
        self.assertEqual(tiles["board"][2], 35)              # Super I/O beats ACPI
        self.assertEqual(tiles["storage"][2], 38)
        self.assertNotIn("gpu", tiles)

    def test_missing_sensors(self):
        tiles = {t[0]: t for t in monitor.highlights([], None)}
        self.assertIsNone(tiles["cpu"][2])
        self.assertEqual(tiles["battery"][3], "Not reported")

    def test_level(self):
        self.assertEqual(monitor.level("cpu", 60), "ok")
        self.assertEqual(monitor.level("cpu", 80), "warm")
        self.assertEqual(monitor.level("battery", 52), "hot")
        self.assertEqual(monitor.level("cpu", None), "ok")


class FormatTests(unittest.TestCase):
    def test_bytes(self):
        self.assertEqual(monitor.fmt_bytes(512), "512 B")
        self.assertEqual(monitor.fmt_bytes(1500, rate=True), "1.5 KB/s")
        self.assertEqual(monitor.fmt_bytes(4.1e9), "4.1 GB")
        self.assertEqual(monitor.fmt_bytes(250e6), "250 MB")


class NameTests(unittest.TestCase):
    def test_versioned_binary(self):
        names = monitor.Names()
        original = monitor.core.read
        monitor.core.read = lambda path, default=None: "/home/u/.local/share/claude/versions/2.1.3\0--print"
        try:
            self.assertEqual(names.get(1, "2.1.3", 5), "claude")
            self.assertEqual(names.get(2, "Web Content", 5), "firefox")
        finally:
            monitor.core.read = original


class LiveTests(unittest.TestCase):
    def test_sample_on_this_machine(self):
        m = monitor.Monitor()
        m.sample()
        r = m.sample()
        self.assertGreater(r["mem_total"], 0)
        self.assertTrue(0 <= r["cpu_pct"] <= 100)
        self.assertLessEqual(len(r["top_mem"]), 3)


if __name__ == "__main__":
    unittest.main()
