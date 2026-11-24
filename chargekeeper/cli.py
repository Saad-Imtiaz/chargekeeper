"""Terminal interface: `chargekeeper status|limit|full|calibrate|performance|fan|unlock`."""

import os
import subprocess
import sys
import time

from . import core, tuning

HELPER = "/usr/lib/chargekeeper/chargekeeper-helper"
COMMANDS = ("status", "limit", "full", "topup", "calibrate", "performance", "fan", "unlock", "help")


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


def fmt_watts(value, kind="measured"):
    """A power reading with its certainty: 24.6 W, ≈ 24 W (estimate), ≥ 18 W (at least)."""
    if value is None:
        return "—"
    if value == 0:
        return "0 W"
    if kind == "estimate":
        return f"≈ {value:.0f} W"
    if kind == "at_least":
        return f"≥ {value:.0f} W"
    return f"{value:.1f} W"


PERFORMANCE_NAMES = {"off": "usual settings", "on": "maximum",
                     "ac": "maximum while plugged in"}


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
    typical = core.typical_draw(core.load_history(time.time() - core.TYPICAL_DAYS * 86400))
    info["charger_power"] = core.charger_power() if info["ac"] else None
    flow = core.power_flow(info, typical)
    print(f"Charger     {fmt_watts(flow['from_charger'], flow['charger_kind'])}"
          + {"estimate": "  (estimate: no charger sensor)", "at_least": "  (no charger sensor)",
             "unknown": "  (no charger sensor)"}.get(flow["charger_kind"], ""))
    if flow["into_battery"] is not None:
        print(f"Battery in  {fmt_watts(flow['into_battery'])}")
    else:
        print(f"Battery out {fmt_watts(flow['from_battery'])}")
    print(f"Laptop uses {fmt_watts(flow['laptop'], flow['laptop_kind'])}")
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
            print("Limit       off until unplugged (charge to full)")
        elif limit is not None and limit < 100:
            start = backend.start()
            print(f"Limit       {limit}%" + (f" (resume below {start}%)" if start else ""))
        else:
            print("Limit       off")
    print()
    print(f"Performance {PERFORMANCE_NAMES[config['performance']]}")
    found = tuning.fans()
    modes = tuning.fan_modes(found)
    if modes:
        mode = config["fan_mode"]
        print(f"Fans        " + (f"fixed at {config['fan_percent']}%" if mode == "fixed"
                                  else {"auto": "automatic", "max": "full speed"}[mode]))
    for label, rpm in tuning.fan_speeds(found):
        if rpm is not None:
            print(f"  {label:<10}{rpm} RPM")
    for unlock in tuning.unlocks():
        print(f"\n! {unlock['title']}\n  Turn on with: chargekeeper unlock {unlock['id']}")
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
    if command == "performance" and len(rest) == 1 and rest[0] in ("on", "off", "ac"):
        return subprocess.call(helper_command("set", "--performance", rest[0]))
    if command == "fan" and len(rest) == 1:
        if rest[0] in ("auto", "max"):
            return subprocess.call(helper_command("set", "--fan-mode", rest[0]))
        if rest[0].rstrip("%").isdigit():
            return subprocess.call(helper_command("set", "--fan-mode", "fixed",
                                                  "--fan-percent", rest[0].rstrip("%")))
    if command == "unlock" and len(rest) == 1:
        return subprocess.call(helper_command("unlock", rest[0]))
    print("usage: chargekeeper [status]\n"
          "       chargekeeper limit <20-100|off>\n"
          "       chargekeeper full [on|off]       charge to 100% once, until unplugged\n"
          "       chargekeeper calibrate <on|off>\n"
          "       chargekeeper performance <on|off|ac>  maximum performance (ac: only plugged in)\n"
          "       chargekeeper fan <auto|max|20-100>    fan control (a number: fixed speed in %)\n"
          "       chargekeeper unlock <id>             turn on a maker's hidden driver features\n"
          "       chargekeeper                     (no arguments in a desktop: open the app)",
          file=sys.stderr)
    return 2
