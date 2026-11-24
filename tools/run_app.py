#!/usr/bin/python3 -I
"""Run the app from this source tree under its own app ID, for testing.

The installed launcher (bin/powerkeeper) prefers /usr/lib/powerkeeper, so it
does not test your changes. This loads the source tree directly, uses a separate
application ID so it never collides with an installed instance, and does not
touch your preferences or autostart (set XDG_CONFIG_HOME to a scratch folder).

    tools/run_app.py                         window on the Overview
    tools/run_app.py --page monitor          window on another tab
    tools/run_app.py --pages overview,usage  window; SIGUSR1 moves to the next tab
    tools/run_app.py --tray                  background mode: tray icon, no window
    tools/run_app.py --light / --dark        force a colour scheme

Tab names: overview, usage, monitor, charging, health, system.
"""

import argparse
import os
import signal
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from powerkeeper import gui  # noqa: E402  (sets the GTK and libadwaita versions)
from gi.repository import Adw, GLib  # noqa: E402

parser = argparse.ArgumentParser()
parser.add_argument("--tray", action="store_true", help="background mode with the tray icon")
parser.add_argument("--page", default="overview")
parser.add_argument("--pages", help="comma-separated tabs; SIGUSR1 moves to the next one")
parser.add_argument("--size", default="900x920")
parser.add_argument("--light", action="store_true")
parser.add_argument("--dark", action="store_true")
parser.add_argument("--app-id", default="dev.powerkeeper.Test")
args = parser.parse_args()
pages = (args.pages or args.page).split(",")
# Never add a test run to the login autostart.
gui.sync_autostart = lambda enabled: None
width, height = (int(v) for v in args.size.split("x"))


class TestApp(gui.App):
    def __init__(self):
        super().__init__()
        self.set_application_id(args.app_id)
        self.prefs["background"] = args.tray
        if args.tray:
            self.start_hidden = True

    def do_activate(self):
        if self.start_hidden:
            gui.App.do_activate(self)
            return
        if self.window is None:
            self.window = gui.Window(self)
            self.window.set_default_size(width, height)
        if args.light:
            Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_LIGHT)
        if args.dark:
            Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK)
        self.window.present()
        self.window.stack.set_visible_child_name(pages[0])

    def set_pref(self, key, value):
        self.prefs[key] = value      # keep test runs out of the real preferences file


app = TestApp()
position = {"index": 0}


def next_page():
    if app.window:
        position["index"] = (position["index"] + 1) % len(pages)
        app.window.stack.set_visible_child_name(pages[position["index"]])
    return GLib.SOURCE_CONTINUE


GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGUSR1, next_page)
sys.exit(app.run([sys.argv[0]]))
