#!/usr/bin/python3 -I
"""Open and close the tray menu over D-Bus many times, to look for leaks.

    python3 tools/menu_cycles.py [CYCLES] [PID]

Finds the tray item (org.kde.StatusNotifierItem-*) on the session bus and, for
each cycle, sends what GNOME's extension sends when the menu opens and closes:
Event(opened), AboutToShow, GetLayout, Event(closed). With PID, prints that
process's resident memory every 100 cycles; it should level off, not keep rising.
Run against tools/run_app.py --tray. Each cycle waits 0.6 s so every opening
takes a fresh reading.
"""

import sys
import time

from gi.repository import Gio, GLib

cycles = int(sys.argv[1]) if len(sys.argv) > 1 else 300
pid = sys.argv[2] if len(sys.argv) > 2 else None
bus = Gio.bus_get_sync(Gio.BusType.SESSION)
names = bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "ListNames",
                      None, None, 0, -1, None).unpack()[0]
items = [n for n in names if n.startswith("org.kde.StatusNotifierItem-")]
if not items:
    sys.exit("No tray item on the session bus (start tools/run_app.py --tray)")
name = items[-1]


def call(method, args, signature):
    return bus.call_sync(name, "/MenuBar", "com.canonical.dbusmenu", method,
                         GLib.Variant(signature, args), None, 0, 5000, None)


def rss():
    with open(f"/proc/{pid}/status") as f:
        return next(int(line.split()[1]) // 1024 for line in f if line.startswith("VmRSS"))


print(f"{name}: {cycles} cycles" + (f", RSS {rss()} MB" if pid else ""))
for i in range(1, cycles + 1):
    call("Event", (0, "opened", GLib.Variant("i", 0), 0), "(isvu)")
    call("AboutToShow", (0,), "(i)")
    call("GetLayout", (0, -1, []), "(iias)")
    time.sleep(0.6)
    call("Event", (0, "closed", GLib.Variant("i", 0), 0), "(isvu)")
    if pid and i % 100 == 0:
        print(f"after {i}: RSS {rss()} MB", flush=True)
