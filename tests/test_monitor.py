"""Monitor parsing and selection logic. Run: python3 -m unittest discover tests"""

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from powerkeeper import monitor  # noqa: E402

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


class TopTrackerTests(unittest.TestCase):
    def test_first_reading_is_used_as_is(self):
        tracker = monitor.TopTracker()
        self.assertEqual(tracker.update({"a": 5.0, "b": 9.0}, 0), [("b", 9.0), ("a", 5.0)])

    def test_apps_taking_turns_do_not_reshuffle(self):
        """The bug: nearly equal apps swapped rows on every sample."""
        tracker = monitor.TopTracker(floor=0.05)
        names = ["firefox", "shell", "claude", "xorg"]
        orders = []
        for tick in range(60):
            # Each app is busy in turn, so the instantaneous ranking changes every sample.
            current = {name: (8.0 if tick % 4 == i else 2.0) for i, name in enumerate(names)}
            orders.append([n for n, _ in tracker.update(current, 2)])
        settled = orders[10:]
        changes = sum(a != b for a, b in zip(settled, settled[1:]))
        self.assertLessEqual(changes, 2)

    def test_a_clear_winner_overtakes(self):
        tracker = monitor.TopTracker()
        tracker.update({"a": 10.0, "b": 8.0}, 0)
        for _ in range(20):
            shown = tracker.update({"a": 10.0, "b": 30.0}, 2)
        self.assertEqual(shown[0][0], "b")

    def test_a_newcomer_replaces_the_weakest_only_with_a_lead(self):
        tracker = monitor.TopTracker(n=2)
        tracker.update({"a": 10.0, "b": 8.0}, 0)
        for _ in range(20):
            shown = tracker.update({"a": 10.0, "b": 8.0, "c": 8.5}, 2)
        self.assertEqual([n for n, _ in shown], ["a", "b"])
        for _ in range(20):
            shown = tracker.update({"a": 10.0, "b": 8.0, "c": 20.0}, 2)
        self.assertEqual(shown[0][0], "c")
        self.assertEqual(len(shown), 2)

    def test_a_steady_small_app_reaches_the_list(self):
        tracker = monitor.TopTracker(floor=1.0)
        tracker.update({"a": 50.0}, 0)
        for _ in range(10):
            shown = tracker.update({"a": 50.0, "small": 2.0}, 2)
        self.assertIn("small", [n for n, _ in shown])

    def test_an_app_that_stops_fades_out_and_is_forgotten(self):
        tracker = monitor.TopTracker(floor=1.0)
        tracker.update({"burst": 100.0}, 0)
        self.assertEqual(tracker.update({}, 2)[0][0], "burst")      # stays for now: no flicker
        for _ in range(30):
            shown = tracker.update({}, 2)
        self.assertEqual(shown, [])
        self.assertEqual(tracker.values, {})

    def test_a_long_pause_trusts_the_new_values(self):
        tracker = monitor.TopTracker()
        tracker.update({"old": 50.0}, 0)
        self.assertAlmostEqual(tracker.update({"old": 1.0}, 3600)[0][1], 1.0)


class ScaleTests(unittest.TestCase):
    def test_nice_ceiling(self):
        cases = {0.3: 0.5, 1: 1, 1.1: 2, 2: 2, 3: 5, 5: 5, 7: 10, 10: 10, 110: 200, 1234567: 2000000}
        for value, expected in cases.items():
            self.assertAlmostEqual(monitor.nice_ceiling(value, 0.1), expected)
        self.assertEqual(monitor.nice_ceiling(0, 10000), 10000)

    def test_scale_holds_still_under_noise(self):
        tops = {monitor.nice_ceiling(v * 1000) for v in (2100, 2900, 3700, 4400, 4900)}
        self.assertEqual(tops, {5000000})

    def test_average(self):
        average = monitor.Average(4)
        self.assertEqual(average.update(10, 0), 10)
        self.assertLess(average.update(50, 2), 30)      # a single spike is damped
        for _ in range(20):
            value = average.update(50, 2)
        self.assertAlmostEqual(value, 50, places=0)


class MonitorRestartTests(unittest.TestCase):
    def test_restart_forgets_rates_and_rankings(self):
        m = monitor.Monitor()
        m.sample()
        m.trackers["cpu"].update({"a": 1.0}, 0)
        m.restart()
        self.assertIsNone(m.prev)
        self.assertEqual(m.trackers["cpu"].values, {})
        self.assertEqual(m.sample()["cpu_pct"], 0.0)    # the first sample after it is the baseline


class SelectionTests(unittest.TestCase):
    def sensor(self, name, chip, kind, label, value):
        return {"name": name, "chip": chip, "kind": kind, "label": label, "value": value}

    def test_hottest_drive_is_shown(self):
        sensors = [self.sensor("nvme", "Cool", "storage", "Composite", 35),
                   self.sensor("nvme", "Hot", "storage", "Composite", 61)]
        tile = {t[0]: t for t in monitor.highlights(sensors, None)}["storage"]
        self.assertEqual(tile[2:], (61, "Hot"))

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


class VendorSensorTests(unittest.TestCase):
    """Sensor layouts from other manufacturers, classified the way sensors() does."""

    def readings(self, chip_name, labelled):
        chip, chip_kind = monitor._chip_label("/nonexistent", chip_name)
        return [{"chip": chip, "name": chip_name, "label": label,
                 "kind": chip_kind or monitor.classify(label), "value": value}
                for label, value in labelled]

    def test_dell_smm_mixed_chip(self):
        sensors = self.readings("dell_smm", [("CPU", 71), ("GPU", 60), ("SODIMM", 44), ("Ambient", 38)])
        sensors += self.readings("acpitz", [("", 50)])
        tiles = {t[0]: t for t in monitor.highlights(sensors, 30.0)}
        self.assertEqual(tiles["cpu"][2], 71)
        self.assertEqual(tiles["board"][2], 44)       # an EC board reading, never the CPU
        self.assertEqual(tiles["gpu"][2], 60)

    def test_thinkpad_unlabelled_cpu_is_not_board(self):
        sensors = self.readings("thinkpad", [("", 68), ("", 40)]) + self.readings("acpitz", [("", 45)])
        sensors += self.readings("coretemp", [("Package id 0", 66)])
        tiles = {t[0]: t for t in monitor.highlights(sensors, None)}
        self.assertEqual(tiles["board"][2], 45)
        self.assertEqual(tiles["cpu"][2], 66)

    def test_ryzen_prefers_tdie(self):
        sensors = self.readings("k10temp", [("Tctl", 80), ("Tdie", 60)])
        self.assertEqual(monitor.highlights(sensors, None)[0][2], 60)

    def test_framework_cros_ec_battery_fallback(self):
        sensors = self.readings("cros_ec", [("cpu_f75303@4d", 55), ("battery", 31), ("local_f75303@4d", 40)])
        tiles = {t[0]: t for t in monitor.highlights(sensors, None)}
        self.assertEqual(tiles["battery"][2], 31)
        self.assertEqual(tiles["board"][2], 40)

    def test_arm_thermal_zone_names(self):
        sensors = self.readings("cpu0_thermal", [("", 52)]) + self.readings("gpuss0_thermal", [("", 47)])
        tiles = {t[0]: t for t in monitor.highlights(sensors, None)}
        self.assertEqual(tiles["cpu"][2], 52)
        self.assertEqual(tiles["gpu"][2], 47)


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


class UpdateSpeedTests(unittest.TestCase):
    def test_a_bad_saved_speed_falls_back(self):
        self.assertEqual(monitor.valid_interval(2), 2)
        self.assertEqual(monitor.valid_interval(0.001), monitor.DEFAULT_INTERVAL)
        self.assertEqual(monitor.valid_interval("fast"), monitor.DEFAULT_INTERVAL)

    def test_expensive_readings_do_not_follow_the_fast_speeds(self):
        self.assertEqual(monitor.process_period(0.5), 2)
        self.assertEqual(monitor.temperature_period(0.5), 2)
        self.assertEqual(monitor.network_period(0.5), 6)

    def test_slow_speed_stretches_every_reading(self):
        self.assertEqual(monitor.process_period(5), 5)
        self.assertEqual(monitor.temperature_period(5), 10)
        self.assertEqual(monitor.network_period(5), 6)

    def test_faster_speed_averages_less(self):
        taus = [monitor.number_tau(i) for i in monitor.INTERVALS]
        self.assertEqual(taus, sorted(taus))
        self.assertLessEqual(taus[0], 1)

    def test_totals_alone_do_not_scan_processes(self):
        m = monitor.Monitor()
        first = m.sample()
        self.assertTrue(first["scanned"])
        scans = []
        original = m._processes
        m._processes = lambda: scans.append(1) or original()
        reading = m.sample(processes=False)
        self.assertEqual(scans, [])
        self.assertFalse(reading["scanned"])
        self.assertEqual(reading["top_mem"], first["top_mem"])      # the last scan's list, not an empty one
        m.sample()
        self.assertEqual(scans, [1])

    def test_first_sample_always_scans(self):
        self.assertTrue(monitor.Monitor().sample(processes=False)["scanned"])

    def test_app_rates_cover_the_time_since_the_last_scan(self):
        """Rates divided by the time since the last totals would be four times too high at 0.5 s."""
        m = monitor.Monitor()
        m._cpu_times = lambda: (0, 0, 1)
        m._disk_bytes = m._net_bytes = lambda: (0, 0)
        procs = {1: ("app", 0, 0, None, 5)}
        m._processes = lambda: dict(procs)
        with patch.object(monitor.time, "monotonic", side_effect=[100.0, 101.0, 102.0]):
            m.sample()
            m.sample(processes=False)
            procs[1] = ("app", m.hz, 0, None, 5)      # one second of CPU time, over two seconds
            reading = m.sample()
        self.assertAlmostEqual(reading["top_cpu"][0][1], 50.0)


class GlideTests(unittest.TestCase):
    def test_moves_in_a_straight_line_and_stops(self):
        g = monitor.Glide().to(10, 0)
        g.to(20, 100, 2)
        self.assertEqual(g.at(100), 10)
        self.assertEqual(g.at(101), 15)
        self.assertEqual(g.at(102), 20)
        self.assertEqual(g.at(500), 20)

    def test_a_new_target_starts_from_where_it_is_now(self):
        g = monitor.Glide().to(0, 0)
        g.to(10, 0, 2)
        g.to(0, 1, 2)       # at 5 when the new reading arrives: no jump
        self.assertEqual(g.at(1), 5)
        self.assertEqual(g.at(2), 2.5)

    def test_without_duration_it_jumps(self):
        g = monitor.Glide().to(1, 0)
        self.assertEqual(g.to(9, 1).at(1), 9)

    def test_first_value_doesnt_glide_from_nothing(self):
        g = monitor.Glide()
        self.assertIsNone(g.at(0))
        self.assertEqual(g.to(5, 0, 2).at(0), 5)


class ChartLinesTests(unittest.TestCase):
    W, H, TOP = 240, 102, 100

    def lines(self, times, values, now):
        return monitor.chart_lines(times, values, now, self.W, self.H, self.TOP, window=120)

    def test_time_maps_to_x_with_now_at_the_right_edge(self):
        [line] = self.lines([0, 60, 120], [0, 50, 100], 120)
        self.assertEqual([x for x, _y in line], [0, 120, 240])
        self.assertEqual([y for _x, y in line], [101, 51, 1])

    def test_none_breaks_the_line(self):
        lines = self.lines([0, 1, 2, 3, 4], [5, 5, None, 5, 5], 4)
        self.assertEqual(len(lines), 2)

    def test_values_are_capped_at_the_top(self):
        [line] = self.lines([0, 1], [500, 500], 1)
        self.assertEqual(line[0][1], 1)


if __name__ == "__main__":
    unittest.main()
