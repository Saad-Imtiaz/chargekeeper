# Chargekeeper

Battery charge-limit and monitoring app for Linux laptops (Python, GTK 4 / libadwaita, root helper
via polkit, systemd service).

Before changing anything, read [MAINTAINING.md](MAINTAINING.md): architecture, the feature list,
50 rules learned from real bugs in this project, and the checks to run before every commit.
History is in [CHANGELOG.md](CHANGELOG.md).

- Tests: `python3 -m unittest discover tests` (no root, no display).
- Run from source: `tools/run_app.py` or `tools/run_app.py --tray`. `bin/chargekeeper` runs the
  installed copy instead.
- Never use Broadway for layout or screenshots; use `tools/nested-gnome.sh`.
- Power cost is a feature: measure with `tools/soak.sh` before and after changes to timers, polling,
  reading, the tray or the service.
- Firmware writes: round limits down, write only on mismatch, follow the per-maker rules, and add a
  test that emulates the kernel.
- Every change: a CHANGELOG entry; update the MAINTAINING.md feature list and rules when they change.
- Git: commit per feature or fix; the repository is local only (no remote) until the owner says
  otherwise.
