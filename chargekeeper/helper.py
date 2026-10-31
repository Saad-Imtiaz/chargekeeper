"""Privileged entry point: `set` (via pkexec from the GUI/CLI), `apply` and `daemon` (systemd)."""

import argparse
import json
import os
import select
import sys
import time

from . import core, power

# The daemon sleeps until a kernel power_supply event (charger plugged or unplugged,
# charging state or level changed, resume) or until the next deadline below.
IDLE_SECONDS = 60           # firmware thresholds hold on their own; this only catches drift
ACTIVE_SECONDS = 30         # calibration or a charge to full is running
SOFTWARE_SECONDS = 20       # software limit while charging...
SOFTWARE_NEAR_SECONDS = 5   # ...and within SOFTWARE_NEAR_PERCENT of the limit
SOFTWARE_NEAR_PERCENT = 3
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
        config["calibration_started"] = time.time() if args.calibration else None

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


def next_interval(backend, config, info):
    """Seconds until the next check when no kernel event arrives first."""
    if backend and backend.software and info and info["status"] == "Charging":
        wanted = core.target(config, backend)
        if wanted and wanted[0] < 100 and info["capacity"] is not None:
            # The daemon is the only thing stopping the charge here: look closely near the limit.
            if info["capacity"] >= wanted[0] - SOFTWARE_NEAR_PERCENT:
                return SOFTWARE_NEAR_SECONDS
            return SOFTWARE_SECONDS
    if config and (config["calibrating"] or config["top_up"]):
        return ACTIVE_SECONDS
    return IDLE_SECONDS


def wait_for_event(events, timeout):
    """Sleep up to timeout seconds; return early (True) on a power_supply event."""
    deadline = time.monotonic() + timeout
    fd = events.fileno()
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        if fd is None:
            time.sleep(remaining)
            return False
        ready, _, _ = select.select([fd], [], [], remaining)
        # Other subsystems' events wake us too; keep waiting unless one was ours.
        if ready and events.drain():
            time.sleep(power.DEBOUNCE_MS / 1000)     # let a burst of events settle
            events.drain()
            return True


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
    full_since = None
    sampler = core.CpuSampler()
    events = power.UeventMonitor()
    if events.fileno() is None:
        print("Kernel events unavailable; polling instead", file=sys.stderr, flush=True)
    while True:
        error = None
        backend = config = info = None
        try:
            config = core.load_config()
            backend = core.detect()
            info = core.combined_info()
            ac = info["ac"] if info else core.ac_online()

            # "Charge to 100% once" ends when the charger is unplugged.
            if config["top_up"] and last_ac and ac is False:
                config["top_up"] = False
                core.save_config(config)

            full_since = (full_since or time.time()) if config["top_up"] and core.is_full(info) else None
            if full_since and time.time() - full_since > core.TOP_UP_FULL_HOURS * 3600:
                config["top_up"] = False
                core.save_config(config)
                full_since = None

            if core.behaviour_override(config, info):
                core.save_config(config)

            if config["calibrating"]:
                if not config["calibration_started"]:
                    config["calibration_started"] = time.time()
                    core.save_config(config)
                reason = core.calibration_stop_reason(backend, config, info)
                if reason:
                    print(f"Calibration stopped: {reason}", file=sys.stderr, flush=True)
                    if backend.calibration:
                        backend.set_calibration(False)
                    config["calibrating"] = False
                    config["calibration_started"] = None
                    core.save_config(config)

            # Re-applied every cycle: covers boot, resume and firmware that
            # forgets its thresholds, and only writes on mismatch.
            core.apply(backend, config)
            if backend.software:
                core.enforce_software(backend, config, info and info["capacity"])

            if info and time.time() - last_log >= LOG_SECONDS:
                core.log_sample(info)
                # Sample every time so the baseline stays fresh; keep it only on battery.
                apps = sampler.sample()
                if ac is False and last_log:
                    core.add_usage(apps, round(time.time() - last_log))
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
        try:
            interval = next_interval(backend, config, info)
        except Exception:   # never let scheduling stop enforcement
            interval = SOFTWARE_NEAR_SECONDS
        until_log = last_log + LOG_SECONDS - time.time()
        wait_for_event(events, max(1.0, min(interval, until_log)))


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
