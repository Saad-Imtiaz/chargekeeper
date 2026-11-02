# Maintaining Chargekeeper

Read this before changing the app. It explains how the app fits together, lists every feature,
records the mistakes already made (so they aren't made again), and gives the checks to run before
every commit. The history of changes is in [CHANGELOG.md](CHANGELOG.md); user documentation is in
[README.md](README.md).

**Keep these files current.** Every change adds a CHANGELOG entry. A change that adds or removes a
feature updates the feature list below. A bug whose cause wasn't obvious adds a rule to
[Rules learned the hard way](#rules-learned-the-hard-way).

## Summary

Chargekeeper limits battery charging on Linux laptops and shows battery health, usage and a live
system monitor. It's written in Python (GTK 4.14+ / libadwaita 1.5+ through PyGObject), with no
compiled code and no dependencies beyond what GNOME desktops ship.

```
 GTK app, tray, CLI (user)  ──pkexec──▶  chargekeeper-helper (root)  ──▶  /sys firmware interfaces
          │                                       ▲
          │ reads /sys, /proc, kernel events      │ same program, as a systemd service
          ▼                                       │
   live readings, monitor          chargekeeper.service: re-applies settings, enforces the
                                   software limit, logs history and per-app usage
```

| File | What it does |
|---|---|
| `chargekeeper/core.py` | Battery reading (one `uevent` read per supply), firmware backends and their quirks, config, history and usage logs, the per-app CPU sampler used by the service |
| `chargekeeper/power.py` | Live readings: kernel uevent listener, power estimator, `LiveBattery` (shared by window, tray, notifications) |
| `chargekeeper/helper.py` | Root entry point: `set`, `apply`, `release`, `platform-profile`, `epp`, and the `daemon` loop |
| `chargekeeper/gui.py` | The window (Overview, Usage, Charging, Health, Settings) and the `App` (tray, notifications) |
| `chargekeeper/monitor_page.py` | The Monitor tab (cards, sparklines, temperature tiles) |
| `chargekeeper/monitor.py` | Processor, memory, disk, network and temperature readings; app names and icons |
| `chargekeeper/tray.py` | Tray icon and menu: StatusNotifierItem + dbusmenu over a raw D-Bus connection |
| `chargekeeper/cli.py` | `chargekeeper status / limit / full / calibrate` |
| `data/` | systemd unit, resume hook, polkit policy and rule, desktop file, icons |
| `install.sh`, `uninstall.sh` | System-wide install and removal |
| `tests/` | Unit tests (no root, no display needed) |
| `tools/` | Test tools: run from source, soak test, nested GNOME Shell, screenshots, leak test |

Where things live once installed: `/usr/lib/chargekeeper/` (code), `/etc/chargekeeper/config.json`
(limit settings), `/var/lib/chargekeeper/` (history, health, usage), `/run/chargekeeper/state.json`
(service status), `~/.config/chargekeeper/prefs.json` (per-user notification and tray settings).

## Features

Each line names where the code lives. Keep this list complete.

**Charge control** (`core.py` backends, `helper.py`)
- Charge limit with resume level through kernel thresholds (ThinkPad, ASUS, Dell, Framework, Huawei,
  Samsung Galaxy Book, System76, MSI, LG, Toshiba). `ThresholdBackend`
- Acer health mode (80%) and calibration via the acer-wmi-battery DKMS driver. `AcerBackend`,
  `install.sh --acer`
- Lenovo conservation mode, older Samsung battery life extender, Sony battery care limiter.
- Software limit for firmware that can only pause charging (`charge_behaviour`), enforced by the
  service. `SoftwareBackend`, `enforce_software`
- Per-maker firmware rules: Dell ranges and gap, LG/Toshiba 80-or-100 only, MSI fixed 10% gap.
  `ThresholdBackend.__init__`
- Limits rounded down, never up. `Backend.snap`
- Charge to full once: ends on unplug or 6 h after full. `helper.cmd_daemon`, Overview button,
  tray, `chargekeeper full`
- Pause charging / run on battery while plugged in, with automatic return to normal. `behaviour_override`
- Charging mode (`charge_types`) where available.
- Calibration auto-stop: firmware done, unplugged, or 24 h. `calibration_stop_reason`
- Unplug reminder notification on laptops without charge control.
- Settings re-applied after boot, resume (`data/chargekeeper-resume`) and charger changes (events).

**Battery information** (`core.py`, `power.py`, `gui.py`)
- Live level, time remaining, energy, temperature. Event-driven, polled only while the Overview or
  Monitor tab is on screen.
- Power flow: from charger, from or into battery, laptop use; measured, estimated or lower bound.
  `core.power_flow`, `core.charger_power`, `core.typical_draw`, `gui.PowerFlow`, tray status, CLI.
- Time remaining from a 3-minute power average; "at the current load" hint; charging time with the
  slowdown above 80%. `power.Estimator`, `core.time_remaining`
- Health: capacity versus design, wear, cycles, daily health history chart.
- History charts: charge level with plugged-in periods, battery power split into drawing and
  charging (6 h, 24 h, 7 days). `core.power_series`
- Usage tab: time and energy on battery, average power, runtime of a full charge, time on the
  charger, energy into the battery, top apps by processor time on battery with app names and icons
  (sampled by the service only while on battery).

**System monitor** (`monitor.py`, `monitor_page.py`, `tray.py`)
- Processor, memory (private memory per app), disk and network with sparklines and top 3 apps.
- Temperatures: CPU, battery, motherboard, SSD, GPU tiles plus every sensor; per-reading
  classification for mixed embedded-controller chips.
- Update speed menu (0.5, 1, 2 or 5 s, saved per user) on the Monitor tab. Sampling runs only while
  the tab is visible, in a short-lived worker thread. Only the cheap totals follow the fast speeds:
  `monitor.process_period`, `temperature_period`, `network_period`. `MonitorPage._build_menu`
- Smooth animation (a menu switch, off with the desktop's animations): numbers and bars glide
  (`monitor.Glide`), charts slide a cached drawing. `MonitorPage._frame`, `Sparkline.do_snapshot`
- A page that holds still: fixed layout, steady top-app lists (`monitor.TopTracker`), averaged
  headline numbers, round sparkline scales with a label (`monitor.nice_ceiling`), a gap in the line
  after time away.

**Tray** (`tray.py`)
- Battery icon and percentage label; menu with status, limit radios, Charge to full once, System
  monitor rows with top-app submenus and temperatures, Open, Quit.
- Readings taken when the menu opens and every 2 s while it stays open; nothing while closed.
- Offers to enable GNOME's AppIndicator extension when no tray host is running.

**App and system**
- Notifications: unplug reminder, limit reached, charge to full done, low battery, battery too hot.
- Power mode (power-profiles-daemon), firmware platform profile, CPU energy preference.
- Diagnostics: detects the missing Acer driver and conflicting TLP thresholds; copy report.
- Polkit: administrators at the active local seat need no password; others authenticate.
- CLI: status, limit, full (alias topup), calibrate.
- Installer for apt, dnf, pacman and zypper; uninstaller switches the limit off first.
- Installer maker choice: `--MAKER` flag, else a menu with the firmware's maker (or the last choice)
  selected. Choosing Acer builds the driver; a failed driver build still starts the service.
  `install.sh`, `tests/test_install.py`

## Rules learned the hard way

Each rule comes from a real bug in this project. The CHANGELOG entry has the details.

### CPU, power and memory

1. **A GLib idle or timeout callback that returns `True`/`SOURCE_CONTINUE` runs again.** A one-shot
   `GLib.idle_add` that returned `SOURCE_CONTINUE` spun a CPU core at 100% and made the fans run.
   One-shot callbacks return `SOURCE_REMOVE`. (`97ab724`)
2. **Nothing polls while nobody is looking.** Hidden or minimized windows, a closed tray menu and an
   invisible Monitor tab do no work. Use `LiveBattery.want()`/`release()` and check
   `is_suspended()`/`get_mapped()`.
3. **Read each battery once, from `uevent`.** About 20 single-file reads cost 4.4 ms; one `uevent`
   read costs 0.01 ms. (`bbbaf3f`)
4. **Some sysfs files are firmware calls.** Acer's battery temperature is a WMI call (4 ms); NVMe
   temperatures send a command to the drive (10–30 ms) and can keep it out of low-power states.
   Cache slow sensors (30 s) and time any new sysfs read before polling it.
5. **Prefer kernel events to polling.** `power.UeventMonitor` (netlink, no privileges). Keep a slow
   heartbeat for firmware that sends no events. The kernel caches battery values for 1 s, so
   polling faster than that is pointless.
6. **Never block the main loop on a subprocess.** `ss` took 40 ms synchronously; it now runs with
   `Gio.Subprocess` asynchronously.
7. **Don't collect data you won't record.** The service scanned `/proc` every minute on the charger
   although per-app usage is only recorded on battery. (`333d7ac`)
8. **PyGObject's `register_object` leaks about 4 KB per incoming D-Bus call with GLib < 2.84**
   (Ubuntu 24.04 has 2.80). The tray answers D-Bus calls from a message filter on its own
   connection instead. Never go back to `register_object`. (`ec58a71`)
9. **Destroying a window doesn't free it.** Signal handlers on child widgets refer back to the
   window through GTK, a cycle Python's collector can't see: about 7.5 MB per reopen. With the tray
   on, close hides the window and reuses it. (`ec58a71`)
10. **Per-app memory is private memory (resident minus shared, from `statm`).** Summing RSS counted
    shared libraries once per process (Firefox showed 1.4 GB instead of 0.5 GB).
11. **Count online CPUs from `/proc/stat`**, not `os.cpu_count()`, which includes CPUs that are off.

### Charging safety

12. **A forgotten "pause" or "run on battery" must not drain the laptop.** They switch back at 20%
    or on unplug, and `ExecStopPost=… release` resets charging when the service stops. (`97ab724`)
13. **Round limits down, never up.**
14. **Never assume a firmware accepts any value.** LG and Toshiba accept only 80/100; Dell has ranges
    and a minimum gap; some ASUS models only honour 60/80/100. Encode known rules in
    `ThresholdBackend.__init__`; turn `EINVAL` into an actionable message (`write_threshold`).
15. **On MSI never write the start threshold.** msi-ec stores one value: writing start sets
    end = start + 10, so "limit off" would have capped charging at 10%. (`ec58a71`)
16. **Write firmware only on mismatch**, and order start/end writes so the kernel never sees
    start ≥ end. Tests emulate the kernel rules (`tests/test_core.py`).
17. **Anything that keeps running needs an end.** Calibration (firmware done, unplug, 24 h) and
    charge to full (unplug, 6 h after full) stop by themselves.
18. **Some laptops report no adapter state.** Infer it from the battery status, or history logs
    everything as "on battery".
19. **Treat implausible readings as missing** (0 V, temperature 0 or Kelvin-based values, power
    above 300 W).

### Hardware and sensors

20. **Classify temperatures per reading, not per chip.** Dell `dell_smm` mixes CPU, GPU, SODIMM and
    ambient on one chip; ThinkPad's first reading is the CPU; Framework's `cros_ec` labels include
    the battery. ARM laptops name zones `cpu*_thermal`.
21. **On Ryzen prefer `Tdie` over `Tctl`**, which can carry a +20–27 °C offset.

### GTK and libadwaita

22. **`Adw.Clamp` gives a `FlowBox` only its minimum width** (libadwaita 1.5). The Monitor tab uses
    its own `MaxWidth` container.
23. **A wrapping label's natural width is its whole sentence.** Set `max_width_chars`, or cards
    can't sit side by side.
24. **Don't use libadwaita's named palette colours for meaning.** Ubuntu patches them (its purple is
    aubergine, invisible on dark cards). Use explicit hex values with light and dark variants.
25. **Custom symbolic icons need a theme root**, not a plain icon folder, or GTK won't recolour them.
26. **Always check that every icon name exists** (`temperature-symbolic` didn't).
41. **Live data must not change the layout.** Hiding empty rows resized all four homogeneous Monitor
    cards and moved everything below them, every time an app started or stopped using the disk. Keep
    the rows and blank them (`set_opacity(0)`), give changing numbers `width_chars`, and give cards a
    minimum width, or a longer number can flip a `FlowBox`'s column count. Check with
    `tools/monitor_stability.py`.
42. **Rankings of nearly equal values need hysteresis.** Apps taking turns reshuffled the top-3 list
    every 2 seconds. Average over time and overtake only with a clear lead (`monitor.TopTracker`).
43. **Read sensors off the main thread on anything that repeats.** Two NVMe drives took 68 ms every
    30 s, which stalled scrolling. The Monitor tab samples in a short-lived thread and only touches
    GTK from the `idle_add` that delivers the result (which returns `SOURCE_REMOVE`, rule 1).
44. **Don't touch a widget that didn't change.** Removing and re-adding the same CSS class restyles
    it, and `set_from_gicon` queues a resize even with the same icon. Compare first.

45. **Schedule by time, not by counting ticks, once the tick rate can change.** `ticks % 3` would have
    run `ss` four times as often at 0.5 s. An average must also get the time since *its own* last
    value: temperatures were given the time since the last sample, so they lagged more at fast speeds.
46. **Judge animation cost on the real renderer, in watts if you can.** `tools/nested-gnome.sh` forces
    cairo. There 30 frames a second cost 21% of a core; with the GPU on the reference laptop it cost
    about 1.4%, and battery power didn't measurably change. Keep per-frame work to moving a cached
    drawing: rebuilding paths point by point is thousands of PyGObject calls a frame.

### Tray (GNOME AppIndicator extension)

27. **The extension shows the label only after a change and drops values equal to its cache.** Clear
    and resend the label after registering. (`5229b5a`)
28. **It keeps a stale `enabled: false`** when a later layout omits the key. Always send `enabled`
    and `visible`; never reuse a menu ID for a different row.
29. **It greys out a submenu whose rows are all disabled.** Idle categories are plain rows.
30. **A new layout while the menu is open collapses submenus and moves rows under the pointer.**
    While open, update rows in place (`ItemsPropertiesUpdated`); defer structural changes until it
    closes, except the first reading after opening.
31. **Only leaf rows show icons** in this extension; submenu rows can't have them.

### Accuracy

32. **Prove an estimator better before shipping it.** The first "smart" time estimate (blended
    averages plus last week's history) was no better than a 1-minute average; simulation picked a
    plain 3-minute average. `EstimatorAccuracyTests` fails if accuracy regresses. (`47ea86b`)
38. **Never present what the hardware doesn't measure as a measurement.** Barrel adapters only report
    online/offline, and UCSI's USB-C voltage and current are the negotiated contract. Charger power
    comes from a real sensor (`power_now`, hwmon `power1_input`) or is labelled ≈ or ≥ on screen.
    Don't read RAPL for this: it's root-only by design (PLATYPUS) and misses the screen and GPU.
39. **A chart's line-break rule has to fit its sample rate.** The zoom-based gap on the 30-day health
    chart (9 h) was shorter than its daily samples, so the line never drew at all. Pass `max_gap`.
40. **One bad line must not lose the rest of a log.** A single `ValueError` ended `load_history`, so
    a line cut short by a power failure hid everything recorded after it. Catch per row.

### Testing and tooling

33. **`bin/chargekeeper` runs the installed copy** in `/usr/lib/chargekeeper` when there is one. Test
    source changes with `tools/run_app.py`; users need `sudo ./install.sh` to get them.
34. **Broadway (`GDK_BACKEND=broadway`) breaks libadwaita layout** (no font DPI, so `sp` sizes and
    breakpoints collapse). Never judge layout or take screenshots there; use
    `tools/nested-gnome.sh`.
35. **Xephyr may use 24-bit pixels.** `tools/xwd2png.py` handles both.
36. **`pkill -f PATTERN` can match your own shell** when the pattern is on its command line, and
    kill it. Kill by PID or with `pgrep -x`.
37. **Never touch the user's real settings from tests.** Use a scratch `XDG_CONFIG_HOME`; the nested
    GNOME Shell uses `GSETTINGS_BACKEND=keyfile`; `tools/run_app.py` never writes autostart.

## Checks before every commit

1. `python3 -m unittest discover tests`: all pass. Add a test for every bug fixed and feature added.
2. Run the change from source: `tools/run_app.py` (window) or `tools/run_app.py --tray`.
3. If it touches the UI: look at it in a real GNOME Shell (`tools/nested-gnome.sh`), in dark and
   light, wide and narrow. If it touches the Monitor tab: `tools/monitor_stability.py` must report
   0 page jumps.
4. If it touches timers, polling, reading, the tray or the service: `tools/soak.sh 600` before and
   after the change. The expected baseline on the reference laptop (Acer Predator PH315-55): tray
   about 0.015–0.02% of one core, service about 0.017%, each 1 wakeup a minute, memory flat. A
   higher number needs a reason.
5. If it touches the tray's D-Bus code: `tools/menu_cycles.py 600 PID` against
   `tools/run_app.py --tray`; memory must level off.
6. If it writes to firmware: think through limit on, limit off, top-up, calibration, service
   stopped, and each maker rule above. Add a test that emulates the kernel's behaviour.
7. Update CHANGELOG.md, this file's feature list and rules, and README.md if users see the change.
8. Commit with a message that says what changed and why, with measurements when relevant.

## Test tools

| Tool | Use |
|---|---|
| `tools/run_app.py [--tray] [--page NAME] [--pages A,B --size WxH --light/--dark]` | Run from source with its own app ID; never writes your preferences or autostart (also set `XDG_CONFIG_HOME` to a scratch folder). SIGUSR1 moves to the next of `--pages`. |
| `tools/service_as_user.py WORKDIR` | The real service loop as a normal user, with its files in WORKDIR. |
| `tools/soak.sh [SECONDS]` | CPU, wakeups and memory of the tray and the service, once a minute. |
| `tools/nested-gnome.sh [SECONDS] -- [run_app args]` | A private GNOME Shell in Xephyr (display `:9`) with the AppIndicator extension, running the app. |
| `tools/capture.sh OUT.png` | Screenshot of the app window on `:9`. |
| `tools/click.py X Y [hover]` | Click on `:9` only. The tray icon is at about (1478, 15). |
| `tools/xwd2png.py IN.xwd OUT.png [X Y W H]` | Convert `xwd -root -display :9` dumps. |
| `tools/menu_cycles.py [CYCLES] [PID]` | Open and close the tray menu over D-Bus to look for leaks. |
| `tools/monitor_stability.py [SECONDS] [REPO] [INTERVAL] [smooth\|plain] [FRAME_MS]` | Count page jumps, app list changes and main-loop stalls on the Monitor tab during disk bursts, with the CPU cost. Pass another checkout as REPO to compare. |
| `tools/release.sh` | Build and test the release archive from the version's tag. |

Tray screenshots: start `tools/nested-gnome.sh 300 -- --tray`, wait about 30 s, click the icon with
`tools/click.py 1478 15`, then dump and crop with `xwd` and `tools/xwd2png.py ... 1150 0 450 700`.

Window screenshots for the README: `tools/nested-gnome.sh 300 -- --pages
overview,usage,monitor,charging,health,system --size 900x920`, then for each tab `tools/capture.sh
docs/screenshots/NAME.png` followed by `kill -USR1 $(cat /tmp/chargekeeper-nested/app.pid)`.

## Releasing

The version is `VERSION` in `chargekeeper/__init__.py` (shown in the About dialog and diagnostics
report). To release:

1. Run every check above and refresh the README screenshots if the UI changed.
2. Move the CHANGELOG's "Unreleased" entries under a new version heading with the date, set `VERSION`,
   add `docs/releases/vX.Y.Z.md` (the text for the GitHub release page), and commit.
3. Tag it (`git tag vX.Y.Z`) and run `tools/release.sh`. It builds
   `dist/chargekeeper-X.Y.Z.tar.gz` and its checksum from the tag, and runs the tests and the
   installer's dry run from the unpacked archive.
4. Push the branch and tag, and attach both files to a GitHub release with the notes from step 2.
