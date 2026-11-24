"""Measured processor and graphics power, against emulated sysfs trees (no root, no hardware)."""

import os
import tempfile
import threading
import time
import unittest
from unittest import mock

from kernelthrottle import meter


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(str(text) + "\n")


class Tree(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        for name, sub in (("POWERCAP", "powercap"), ("PCI", "pci")):
            patcher = mock.patch.object(meter, name, os.path.join(self.root, sub))
            patcher.start()
            self.addCleanup(patcher.stop)

    def zone(self, name, label, energy, wrap=262143328850):
        base = os.path.join(self.root, "powercap", name)
        write(base + "/name", label)
        write(base + "/energy_uj", energy)
        write(base + "/max_energy_range_uj", wrap)
        return base

    def gpu(self, pci, vendor, status="active", boot_vga=None, hwmon_power=None):
        base = os.path.join(self.root, "pci", pci)
        write(base + "/class", "0x030000")
        write(base + "/vendor", vendor)
        write(base + "/power/runtime_status", status)
        if boot_vga is not None:
            write(base + "/boot_vga", boot_vga)
        if hwmon_power is not None:
            write(base + "/hwmon/hwmon9/power1_average", hwmon_power)
        return base


class RaplTests(Tree):
    def test_only_package_zones_count(self):
        # Intel: package-0 with core/uncore subzones, and the same package again over MMIO.
        self.zone("intel-rapl:0", "package-0", 1_000_000)
        self.zone("intel-rapl:0:0", "core", 500_000)
        self.zone("intel-rapl:0:1", "uncore", 100_000)
        self.zone("intel-rapl-mmio:0", "package-0", 1_000_000)
        self.zone("intel-rapl:1", "psys", 9_000_000)
        self.assertEqual([p for p, _ in meter.rapl_packages()],
                         [os.path.join(self.root, "powercap/intel-rapl:0/energy_uj")])

    def test_watts_from_energy_change(self):
        base = self.zone("intel-rapl:0", "package-0", 1_000_000)
        m = meter.RaplMeter()
        self.assertIsNone(m.read(now=100.0))            # the baseline
        write(base + "/energy_uj", 1_000_000 + 24_500_000)  # 24.5 J over 2 s
        self.assertEqual(m.read(now=102.0), 12.2)
        self.assertEqual(m.span, 2.0)

    def test_too_soon_repeats_the_last_value(self):
        # Never hand out an average over less than a second: that is what side channels need.
        base = self.zone("intel-rapl:0", "package-0", 0)
        m = meter.RaplMeter()
        m.read(now=0.0)
        write(base + "/energy_uj", 10_000_000)
        self.assertEqual(m.read(now=1.0), 10.0)
        write(base + "/energy_uj", 90_000_000)
        self.assertEqual(m.read(now=1.2), 10.0)

    def test_counter_wrap(self):
        base = self.zone("intel-rapl:0", "package-0", 999_000_000, wrap=1_000_000_000)
        m = meter.RaplMeter()
        m.read(now=0.0)
        write(base + "/energy_uj", 9_000_000)           # wrapped: 1 J + 9 J
        self.assertEqual(m.read(now=1.0), 10.0)

    def test_two_packages_add_up(self):
        a = self.zone("intel-rapl:0", "package-0", 0)
        b = self.zone("intel-rapl:1", "package-1", 0)
        m = meter.RaplMeter()
        m.read(now=0.0)
        write(a + "/energy_uj", 20_000_000)
        write(b + "/energy_uj", 15_000_000)
        self.assertEqual(m.read(now=1.0), 35.0)

    def test_no_counter(self):
        self.assertIsNone(meter.RaplMeter().read())
        self.assertIsNone(meter.MeterServer(os.path.join(self.root, "m.sock")).fileno())


class ServerTests(Tree):
    def serve(self, server):
        import select
        stop = threading.Event()

        def loop():
            while not stop.is_set():
                if select.select([server.fileno()], [], [], 0.05)[0]:
                    server.handle()
        thread = threading.Thread(target=loop, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(stop.set)

    def test_round_trip(self):
        base = self.zone("intel-rapl:0", "package-0", 0)
        path = os.path.join(self.root, "run", "meter.sock")
        server = meter.MeterServer(path)
        self.addCleanup(server.close)
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o666)     # any user may ask
        server.meter.last = (time.monotonic() - 2.0, [0])
        write(base + "/energy_uj", 30_000_000)                      # 30 J over ~2 s
        self.serve(server)
        watts, span = meter._ask(path)
        self.assertAlmostEqual(watts, 15.0, delta=0.5)
        self.assertAlmostEqual(span, 2.0, delta=0.2)

    def test_stale_average_is_replaced_by_a_fresh_one(self):
        answers = [(10.0, 600.0), (25.0, 1.0)]      # an hour-old average, then a 1 s one
        with mock.patch.object(meter, "_ask", side_effect=answers), \
                mock.patch.object(meter.time, "sleep") as sleep:
            self.assertEqual(meter.cpu_power("x"), 25.0)
        sleep.assert_called_once_with(meter.METER_WINDOW)

    def test_recent_average_is_used_as_is(self):
        with mock.patch.object(meter, "_ask", return_value=(12.0, 2.0)), \
                mock.patch.object(meter.time, "sleep") as sleep:
            self.assertEqual(meter.cpu_power("x"), 12.0)
        sleep.assert_not_called()

    def test_no_service(self):
        self.assertIsNone(meter.cpu_power(os.path.join(self.root, "missing.sock")))


class GpuTests(Tree):
    def setUp(self):
        super().setUp()
        meter._last.clear()
        self.addCleanup(meter._last.clear)

    def nvml(self, *answers):
        nvml = mock.Mock()
        nvml.read.side_effect = list(answers)
        patcher = mock.patch.object(meter, "_nvml", nvml)
        patcher.start()
        self.addCleanup(patcher.stop)
        return nvml

    def test_sleeping_nvidia_is_not_woken(self):
        self.gpu("0000:00:02.0", "0x8086", boot_vga=1)
        self.gpu("0000:01:00.0", "0x10de", status="suspended")
        nvml = self.nvml()
        self.assertEqual(meter.gpu_power(), [{"name": "NVIDIA graphics", "watts": 0.0, "state": "asleep"}])
        nvml.read.assert_not_called()

    def test_busy_nvidia_is_read_every_time(self):
        self.gpu("0000:01:00.0", "0x10de")
        nvml = self.nvml((61.234, 87), (64.0, 90))
        self.assertEqual(meter.gpu_power(now=0), [{"name": "NVIDIA graphics", "watts": 61.2, "state": "measured"}])
        self.assertEqual(meter.gpu_power(now=2)[0]["watts"], 64.0)
        nvml.read.assert_called_with("0000:01:00.0")

    def test_idle_nvidia_is_left_alone_so_it_can_sleep(self):
        # Every NVML call restarts the card's idle timer: asking every 2 s kept an idle
        # RTX 3060 awake at 10-22 W forever.
        base = self.gpu("0000:01:00.0", "0x10de")
        nvml = self.nvml((10.1, 0), (10.0, 0))
        for t in (0, 2, 10, 28):
            self.assertEqual(meter.gpu_power(now=t)[0], {"name": "NVIDIA graphics", "watts": 10.1, "state": "idle"})
        self.assertEqual(nvml.read.call_count, 1)
        meter.gpu_power(now=31)
        self.assertEqual(nvml.read.call_count, 2)
        write(base + "/power/runtime_status", "suspended")      # it fell asleep
        self.assertEqual(meter.gpu_power(now=40)[0]["state"], "asleep")
        self.assertEqual(nvml.read.call_count, 2)

    def test_nvidia_without_a_meter(self):
        self.gpu("0000:01:00.0", "0x10de")
        self.nvml((None, None))
        self.assertEqual(meter.gpu_power()[0]["state"], "unknown")

    def test_amd_card_counted_but_not_the_processors_own(self):
        self.gpu("0000:05:00.0", "0x1002", boot_vga=1, hwmon_power=8_000_000)     # APU graphics
        card = self.gpu("0000:03:00.0", "0x1002", boot_vga=0, hwmon_power=31_250_000)
        write(card + "/gpu_busy_percent", 40)
        self.assertEqual(meter.gpu_power(), [{"name": "AMD graphics", "watts": 31.2, "state": "measured"}])

    def test_intel_graphics_are_part_of_the_package(self):
        self.gpu("0000:00:02.0", "0x8086", boot_vga=1)
        self.assertEqual(meter.gpu_power(), [])


if __name__ == "__main__":
    unittest.main()
