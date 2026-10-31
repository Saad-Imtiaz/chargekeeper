"""Terminal interface: `chargekeeper status|limit|topup|calibrate`."""

import os
import subprocess
import sys

from . import core

HELPER = "/usr/lib/chargekeeper/chargekeeper-helper"
COMMANDS = ("status", "limit", "full", "topup", "calibrate", "help")


def helper_command(*args):
    path = HELPER if os.path.exists(HELPER) else os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", "chargekeeper-helper")
    prefix = [] if os.geteuid() == 0 else ["pkexec"]
    return prefix + [path, *args]


def fmt_hours(hours):
    if hours is None:
        return "—"
    h, m = divmod(round(hours * 60), 60)
    return f"{h} h {m:02d} min" if h else f"{m} min"


def status():
    info = core.combined_info()
    backend = core.detect()
    config = core.load_config()
    if not info:
        print("No battery found.")
        return
    limit = backend.limit()
    print(f"Battery     {info['name']}  {info['capacity']}%  {info['status']}")
    print(f"Adapter     {'plugged in' if info['ac'] else 'on battery'}")
    if info["power"] is not None:
        print(f"Power       {info['power']:.1f} W")
    remaining = core.time_remaining(info, limit or 100)
    if remaining is not None:
        verb = "to empty" if info["status"] == "Discharging" else "to target"
        print(f"Time        {fmt_hours(remaining)} {verb}")
    if info["health"] is not None:
        print(f"Health      {info['health']:.0f}%  ({info['energy_full']:.1f} of "
              f"{info['energy_design']:.1f} Wh design)")
    if info["cycle_count"]:
        print(f"Cycles      {info['cycle_count']}")
    if info["temperature"] is not None:
        print(f"Temperature {info['temperature']:.1f} °C")
    print()
    print(f"Control     {backend.name}")
    if backend.available:
        if config["calibrating"]:
            print("Limit       calibration running")
        elif config["top_up"]:
            print("Limit       off until unplugged (top-up)")
        elif limit is not None and limit < 100:
            start = backend.start()
            print(f"Limit       {limit}%" + (f" (resume below {start}%)" if start else ""))
        else:
            print("Limit       off")
    for hint in core.hints():
        print(f"\n! {hint['title']}\n  {hint['body']}")
    state = core.daemon_state()
    if backend.available and not (state and state["alive"]):
        print("\n! chargekeeper.service is not running; settings will not survive a reboot.")


def main(argv):
    command = argv[0] if argv else "status"
    rest = argv[1:]
    if command == "status":
        status()
        return 0
    if command == "limit" and len(rest) == 1:
        if rest[0] == "off":
            args = ["--limit-enabled", "off", "--top-up", "off"]
        else:
            args = ["--limit-enabled", "on", "--top-up", "off", "--end", rest[0]]
        return subprocess.call(helper_command("set", *args))
    if command in ("full", "topup"):
        return subprocess.call(helper_command("set", "--top-up", rest[0] if rest else "on"))
    if command == "calibrate" and len(rest) == 1:
        return subprocess.call(helper_command("set", "--calibration", rest[0]))
    print("usage: chargekeeper [status]\n"
          "       chargekeeper limit <20-100|off>\n"
          "       chargekeeper full [on|off]       charge to 100% once, until unplugged\n"
          "       chargekeeper calibrate <on|off>\n"
          "       chargekeeper                     (no arguments in a desktop: open the app)",
          file=sys.stderr)
    return 2
