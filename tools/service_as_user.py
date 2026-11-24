#!/usr/bin/python3 -I
"""Run the real service loop (helper.cmd_daemon) as a normal user, to measure it.

    tools/service_as_user.py WORKDIR

It reads the real battery and the real /etc/kernelthrottle/config.json, but its own
files (history, usage, state) go to WORKDIR, and it never saves the config. As a
normal user it cannot write firmware settings: if the firmware already matches the
config nothing is written anyway, otherwise the write fails and is logged.
"""

import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from kernelthrottle import core, helper  # noqa: E402

work = os.path.abspath(sys.argv[1])
os.makedirs(work + "/run", exist_ok=True)
core.DATA_DIR = work + "/var"
core.HISTORY_PATH = core.DATA_DIR + "/history.csv"
core.HEALTH_PATH = core.DATA_DIR + "/health.csv"
core.USAGE_PATH = core.DATA_DIR + "/usage.json"
core.STATE_PATH = work + "/run/state.json"
core.save_config = lambda config: None
helper.cmd_daemon(None)
