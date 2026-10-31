"""Privileged entry point: `set` (via pkexec from the GUI/CLI), `apply` and `daemon` (systemd)."""

import argparse
import json
import os
import sys
import time

from . import core

# Firmware thresholds hold on their own, so the daemon only checks for drift.
# The software limit is enforced by the daemon itself and needs a faster loop.
POLL_SECONDS = 30
SOFTWARE_POLL_SECONDS = 10
LOG_SECONDS = 60


def _bool(text):
    if text.lower() in ("1", "true", "yes", "on"):
        return True
    if text.lower() in ("0", "false", "no", "off"):
        return False
    raise argparse.ArgumentTypeError("expected on/off")


def _percent(text):
    value = int(text)
    if not 0 <= value <= 100:
        raise argparse.ArgumentTypeError("expected 0-100")
    return value


def cmd_set(args):
    backend = core.detect()
    config = core.load_config()

    if args.limit_enabled is not None:
        config["limit_enabled"] = args.limit_enabled
    if args.end is not None:
        config["end"] = args.end
    if args.start is not None:
        config["start"] = args.start
    if config["start"] is not None and config["start"] >= config["end"]:
        config["start"] = max(config["end"] - 5, 0)
    if args.top_up is not None:
        config["top_up"] = args.top_up
    if args.behaviour is not None:
        if args.behaviour not in backend.behaviours() and args.behaviour != "auto":
            sys.exit(f"Charge behaviour '{args.behaviour}' is not supported here")
        config["behaviour"] = args.behaviour
    if args.charge_type is not None:
        if args.charge_type not in backend.charge_types():
            sys.exit(f"Charge type '{args.charge_type}' is not supported here")
        config["charge_type"] = args.charge_type
    if args.calibration is not None:
        backend.set_calibration(args.calibration)
        config["calibrating"] = args.calibration

    core.save_config(config)
    core.apply(backend, config)
    if backend.software:
        info = core.combined_info()
        core.enforce_software(backend, config, info and info["capacity"])


def cmd_platform_profile(args):
    choices, _ = core.platform_profiles()
    if args.value not in choices:
        sys.exit(f"Unknown platform profile '{args.value}'")
    core.write(core.PLATFORM_PROFILE, args.value)


def cmd_epp(args):
    choices, _ = core.epp_choices()
    if args.value not in choices:
        sys.exit(f"Unknown energy preference '{args.value}'")
    core.set_epp(args.value)


def cmd_apply(args):
    core.apply(core.detect(), core.load_config())


def cmd_release(args):
    core.release(core.detect())


def write_state(state):
    os.makedirs(os.path.dirname(core.STATE_PATH), exist_ok=True)
    tmp = core.STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.chmod(tmp, 0o644)
    os.replace(tmp, core.STATE_PATH)


def cmd_daemon(args):
    last_ac = None
    last_log = 0
    while True:
        error = None
        backend = None
        try:
            config = core.load_config()
            backend = core.detect()
            info = core.combined_info()
            ac = info["ac"] if info else core.ac_online()

            # "Charge to 100% once" ends when the charger is unplugged.
            if config["top_up"] and last_ac and ac is False:
                config["top_up"] = False
                core.save_config(config)

            if core.behaviour_override(config, info):
                core.save_config(config)

            # Re-applied every cycle: covers boot, resume and firmware that
            # forgets its thresholds, and only writes on mismatch.
            core.apply(backend, config)
            if backend.software:
                core.enforce_software(backend, config, info and info["capacity"])

            if info and time.time() - last_log >= LOG_SECONDS:
                core.log_sample(info)
                last_log = time.time()
            last_ac = ac
        except Exception as exc:  # keep the daemon alive, surface the error in the GUI
            error = f"{type(exc).__name__}: {exc}"
            print(error, file=sys.stderr, flush=True)

        try:
            write_state({
                "updated": time.time(),
                "backend": backend.id if backend else None,
                "error": error,
            })
        except OSError as exc:
            print(f"Cannot write state: {exc}", file=sys.stderr, flush=True)
        time.sleep(SOFTWARE_POLL_SECONDS if backend and backend.software else POLL_SECONDS)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="chargekeeper-helper")
    sub = parser.add_subparsers(dest="command", required=True)

    s = sub.add_parser("set", help="change settings and apply them")
    s.add_argument("--limit-enabled", type=_bool)
    s.add_argument("--end", type=_percent)
    s.add_argument("--start", type=_percent)
    s.add_argument("--top-up", type=_bool)
    s.add_argument("--behaviour", choices=("auto", "inhibit-charge", "force-discharge"))
    s.add_argument("--charge-type")
    s.add_argument("--calibration", type=_bool)
    s.set_defaults(func=cmd_set)

    p = sub.add_parser("platform-profile")
    p.add_argument("value")
    p.set_defaults(func=cmd_platform_profile)

    e = sub.add_parser("epp")
    e.add_argument("value")
    e.set_defaults(func=cmd_epp)

    sub.add_parser("apply").set_defaults(func=cmd_apply)
    sub.add_parser("release").set_defaults(func=cmd_release)
    sub.add_parser("daemon").set_defaults(func=cmd_daemon)

    args = parser.parse_args(argv)
    if os.geteuid() != 0:
        sys.exit("chargekeeper-helper must run as root")
    try:
        args.func(args)
    except OSError as exc:
        sys.exit(f"Firmware rejected the change: {exc.strerror or exc}")


if __name__ == "__main__":
    main()
