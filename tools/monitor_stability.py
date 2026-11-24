#!/usr/bin/python3 -I
"""Count what moves on the Monitor tab while the disk is busy in bursts.

    tools/monitor_stability.py [SECONDS] [REPO] [INTERVAL] [smooth|plain] [FRAME_MS]
                                    default 60 s, this source tree, the app's default speed

Run it inside the nested shell (DISPLAY=:9 and its bus, see nested-gnome.sh), with
XDG_CONFIG_HOME pointing at a scratch folder. Pass another checkout as REPO to compare
before and after a change. Pass an update speed to measure its cost. The temperature grid should move 0 times and the card height
should stay one value; every move is the whole page jumping under the reader's eyes.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import time

SECONDS = int(sys.argv[1]) if len(sys.argv) > 1 else 60
REPO = sys.argv[2] if len(sys.argv) > 2 else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from powerkeeper import gui  # noqa: E402
from gi.repository import GLib  # noqa: E402

gui.sync_autostart = lambda enabled: None     # never add a test run to the login autostart
SETTLE = 3      # seconds for the first reading to arrive
scratch = tempfile.mkdtemp(prefix="powerkeeper-stability-")
target = os.path.join(scratch, "burst.bin")
seen = {"moves": 0, "heights": set(), "list_changes": 0, "y": None, "names": None, "stalls": 0, "worst": 0.0,
        "last": time.monotonic(), "start": None, "cpu": 0.0}


class App(gui.App):
    def __init__(self):
        super().__init__()
        self.set_application_id("dev.powerkeeper.Stability")
        self.prefs["background"] = False
        if len(sys.argv) > 3:
            self.prefs["monitor_interval"] = float(sys.argv[3])
        if len(sys.argv) > 4:
            self.prefs["monitor_smooth"] = sys.argv[4] == "smooth"

    def do_activate(self):
        if not self.window:
            self.window = gui.Window(self)
            self.window.set_default_size(900, 920)
        self.window.present()
        self.window.stack.set_visible_child_name("monitor")

    def set_pref(self, key, value):
        self.prefs[key] = value


if len(sys.argv) > 5:
    from powerkeeper import monitor_page  # noqa: E402
    monitor_page.FRAME_MS = monitor_page.SOFTWARE_FRAME_MS = int(sys.argv[5])
app = App()


def listed(card):
    return tuple(line.name.get_label() for line in card.lines if line.get_visible() and line.get_opacity() > 0)


def settled():
    return seen["start"] is not None and time.monotonic() - seen["start"] > SETTLE


def probe():
    window = app.window
    if not window or not window.get_mapped():
        return GLib.SOURCE_CONTINUE
    page = window.monitor_page
    if seen["start"] is None:
        seen["start"], seen["cpu"] = time.monotonic(), sum(os.times()[:2])
    ok, bounds = page.temp_grid.compute_bounds(page.get_child())
    y = round(bounds.get_y()) if ok else None
    if settled():
        seen["heights"].add(page.cpu.get_height())
        names = listed(page.cpu), listed(page.disk)
        seen["moves"] += seen["y"] is not None and y != seen["y"]
        seen["list_changes"] += seen["names"] is not None and names != seen["names"]
        seen["names"] = names
    seen["y"] = y
    return GLib.SOURCE_CONTINUE


def heartbeat():
    now = time.monotonic()
    late = now - seen["last"] - 0.02
    seen["last"] = now
    if settled():
        seen["worst"] = max(seen["worst"], late)
        seen["stalls"] += late > 0.016
    return GLib.SOURCE_CONTINUE


def burst():
    subprocess.Popen(["dd", "if=/dev/zero", f"of={target}", "bs=1M", "count=30", "conv=fdatasync"],
                     stderr=subprocess.DEVNULL)
    return GLib.SOURCE_CONTINUE


def finish():
    with open("/proc/self/status") as f:
        print(next(line for line in f if line.startswith("VmRSS")).strip())
    elapsed = time.monotonic() - seen["start"]
    cpu = 100 * (sum(os.times()[:2]) - seen["cpu"]) / elapsed
    print(f"page jumps {seen['moves']}, card heights {sorted(seen['heights'])}, app list changes "
          f"{seen['list_changes']}, main-loop stalls over 16 ms {seen['stalls']} (worst "
          f"{seen['worst'] * 1000:.0f} ms), CPU {cpu:.2f}% of one core")
    app.quit()
    shutil.rmtree(scratch, ignore_errors=True)      # a burst may still be writing
    return GLib.SOURCE_REMOVE


GLib.timeout_add(100, probe)
GLib.timeout_add(20, heartbeat)
GLib.timeout_add_seconds(7, burst)
GLib.timeout_add_seconds(SECONDS, finish)
sys.exit(app.run([sys.argv[0]]))
