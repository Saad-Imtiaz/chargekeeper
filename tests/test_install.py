"""The installer's choice of maker. Everything here is a dry run: no root, nothing changes."""

import os
import pty
import select
import subprocess
import tempfile
import unittest

INSTALL = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "install.sh")


class InstallerMakerTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.vendor = os.path.join(self.dir.name, "vendor")
        self.saved = os.path.join(self.dir.name, "saved")
        self.env = {**os.environ, "KERNELTHROTTLE_TEST_VENDOR_FILE": self.vendor,
                    "KERNELTHROTTLE_TEST_SAVED_MAKER": self.saved}
        self.set_vendor("Dell Inc.")

    def set_vendor(self, text):
        with open(self.vendor, "w") as f:
            f.write(text + "\n")

    def run_flags(self, *args):
        return subprocess.run([INSTALL, "--dry-run", *args], env=self.env, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=20)

    def run_menu(self, typed, *args):
        """Run with a real terminal, typing the lines given. Returns (exit code, all output)."""
        master, slave = pty.openpty()
        proc = subprocess.Popen([INSTALL, "--dry-run", *args], env=self.env, stdin=slave, stdout=slave,
                                stderr=slave, close_fds=True)
        os.close(slave)
        os.write(master, typed.encode())
        output = b""
        while True:
            ready, _, _ = select.select([master], [], [], 20)
            self.assertTrue(ready, "the installer hung")
            try:
                chunk = os.read(master, 4096)
            except OSError:     # Linux reports the far end closing as EIO
                break
            if not chunk:
                break
            output += chunk
        os.close(master)
        return proc.wait(timeout=5), output.decode()

    def test_flag_skips_the_menu(self):
        result = self.run_flags("--dell")
        self.assertEqual(result.returncode, 0)
        self.assertIn("Installing for: Dell", result.stdout)
        self.assertNotIn("Which company", result.stdout + result.stderr)

    def test_flag_spellings(self):
        for args in (["--ACER"], ["--maker", "acer"], ["--maker=acer"], ["acer"]):
            self.assertIn("Installing for: Acer", self.run_flags(*args).stdout, args)
        self.assertIn("Lenovo", self.run_flags("--thinkpad").stdout)
        self.assertIn("Toshiba", self.run_flags("--dynabook").stdout)

    def test_only_acer_builds_the_driver(self):
        self.assertIn("Would build the acer-wmi-battery driver", self.run_flags("--acer").stdout)
        self.assertNotIn("acer-wmi-battery", self.run_flags("--dell").stdout)

    def test_unknown_maker_is_refused(self):
        result = self.run_flags("--nokia")
        self.assertEqual(result.returncode, 2)
        self.assertIn("--nokia", result.stderr)

    def test_two_makers_are_refused(self):
        self.assertEqual(self.run_flags("--acer", "--dell").returncode, 2)

    def test_mismatch_with_firmware_is_mentioned(self):
        self.assertIn('made by "Dell Inc."', self.run_flags("--acer").stdout)
        self.assertNotIn("made by", self.run_flags("--dell").stdout)

    def test_enter_picks_the_detected_maker(self):
        self.set_vendor("LENOVO")
        code, output = self.run_menu("\n")
        self.assertEqual(code, 0)
        self.assertIn("Which company made this laptop?", output)
        self.assertRegex(output, r">\s+\d+\) Lenovo")
        self.assertIn("Installing for: Lenovo", output)

    def test_menu_by_number_and_by_name(self):
        self.assertIn("Would build", self.run_menu("1\n")[1])
        self.assertIn("Installing for: MSI", self.run_menu("msi\n")[1])
        self.assertIn("Installing for: MSI", self.run_menu("--MSI\n")[1])

    def test_detected_acer_says_a_driver_is_built_before_it_is_accepted(self):
        self.set_vendor("Acer")
        code, output = self.run_menu("\n")
        self.assertEqual(code, 0)
        self.assertRegex(output, r">\s+1\) Acer \(also builds the acer-wmi-battery driver\)")
        self.assertIn("Would build", output)

    def test_bad_answers_ask_again(self):
        code, output = self.run_menu("99\nnokia\n3\n")
        self.assertEqual(code, 0)
        self.assertIn('"99" isn\'t on the list', output)
        self.assertIn('"nokia" isn\'t on the list', output)
        self.assertIn("Installing for: Dell", output)

    def test_ctrl_d_installs_nothing(self):
        code, output = self.run_menu("\x04")
        self.assertEqual(code, 1)
        self.assertIn("Nothing was installed", output)
        self.assertNotIn("Installing for", output)

    def test_unknown_vendor_defaults_to_other(self):
        self.set_vendor("Some Small Brand")
        self.assertIn("Installing for: Other", self.run_menu("\n")[1])

    def test_last_choice_is_the_default_next_time(self):
        with open(self.saved, "w") as f:
            f.write("msi\n")
        self.assertIn("Installing for: MSI", self.run_menu("\n")[1])

    def test_without_a_terminal_no_third_party_driver_is_built(self):
        self.set_vendor("Acer")
        result = self.run_flags()
        self.assertEqual(result.returncode, 0)
        self.assertIn("Installing for: Acer", result.stdout)
        self.assertIn("isn't built unless you ask", result.stdout)
        self.assertNotIn("Would build", result.stdout)

    def test_help_lists_every_maker(self):
        result = subprocess.run([INSTALL, "--help"], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0)
        for flag in ("--acer", "--lenovo", "--system76", "--other"):
            self.assertIn(flag, result.stdout)

    def test_needs_root_to_really_install(self):
        if os.getuid() == 0:
            self.skipTest("running as root")
        result = subprocess.run([INSTALL, "--acer"], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 1)
        self.assertIn("sudo", result.stderr)
