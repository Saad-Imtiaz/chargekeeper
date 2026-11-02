# Changelog

Every change to Chargekeeper, newest first. Commit IDs point to the full details (`git show ID`).
Rules learned from these changes are collected in [MAINTAINING.md](MAINTAINING.md#rules-learned-the-hard-way).

## How to add an entry

Add new entries at the top of **Unreleased**, one per commit or related group of commits:

```markdown
### YYYY-MM-DD: Short title (commit ID)
- **Added / Changed / Fixed / Removed / Security:** what changed, in user terms.
- **Why:** the problem it solves; measurements before and after when relevant.
- **Lesson:** only if the cause wasn't obvious; also add it as a rule in MAINTAINING.md.
```

When releasing, rename **Unreleased** to the version and date (see MAINTAINING.md, "Releasing").

## Unreleased (since 1.1.0)

## 1.1.0 (2026-11-03)

### 2026-11-03: First public release
- **Added:** `tools/release.sh` builds and tests the archive from the tag; release notes in
  `docs/releases/`; refreshed README screenshots, a new one of the update speed menu, and a note on
  which laptop is the reference.
- **Changed:** the README's install steps start from the release download. Its opening no longer says
  the default limit is 80%: the installer sets 85%, rounded down to what the laptop supports.

### 2026-11-03: The installer asks for the laptop's maker (3ecf525)
- **Added:** `sudo ./install.sh --acer` (or `--dell`, `--lenovo` and so on) skips the question; with
  no flag a menu opens with the firmware's maker, or the last choice, selected. `--help`, `--dry-run`.
- **Changed:** choosing Acer in the menu builds the driver, and the menu says so. Without a terminal
  the driver is built only with `--acer`, because it's third-party code. An update no longer rebuilds
  a driver that's already installed for this kernel at the same commit.
- **Fixed:** a failed driver build or a module refused by Secure Boot stopped the installer before the
  service started. It now finishes, explains what to do, and exits non-zero.

### 2026-11-03: Monitor update speed and smooth animation (19be613)
- **Added:** a menu on the Monitor tab: every 0.5, 1, 2 or 5 s, and Smooth animation, saved per user.
  The default goes from 2 s to 1 s with animation on. Desktop-wide "animations off" is respected.
- **Changed:** only the system totals follow the fast speeds (0.2 ms a reading). The per-app scan
  (4.5 ms) runs at most every 2 s, temperatures every 2 s, `ss` every 6 s. Graphs keep their two
  minutes when the speed changes.
- **Fixed:** the temperature tiles averaged using the time since the last sample, not the last
  temperature reading, so they lagged behind.
- **Measured** (`tools/monitor_stability.py`, GPU renderer, on battery): CPU at 1 s plain 3.73%, 1 s
  smooth 5.17%, 0.5 s smooth 5.82% of one core; the old 2 s page took 3.3% (nested, cairo). Battery
  power 18.5 W for all of them, within 0.1 W of noise, in two of three rounds; the first round had a
  spike to 30 W while other commands ran, so it's discarded. Page jumps 0 at every speed. Memory at
  0.5 s smooth over 6 min: 196.8 → 197.2 MB, slowing to about 35 KB a minute. Rendering with cairo:
  21% at 30 frames a second, 9% at 10, so software rendering uses 10.
  `tools/soak.sh 600`, on battery, old → new: tray 0.0167% → 0.02% (one clock tick apart), 4 wakeups a
  minute on both; service 0.0233% on both, 1 wakeup a minute; memory flat.
- **Lesson:** schedule by time once the tick rate can change, and judge animation on the real renderer.
  Rules 45 and 46.

### 2026-11-02: Power from charger and battery, and a Monitor tab that holds still (19b6098)
- **Added:** a Power section on the Overview and Monitor tabs: from charger, from or into battery,
  laptop use. Also in the tray status and `chargekeeper status`. The battery figure is measured.
  Without a charger sensor, which most laptops lack, the charger figure is marked ≈ (typical draw on
  battery plus charging power) or ≥ (a lower bound).
- **Changed:** the power chart separates drawing from the battery and charging it. Usage shows energy
  put into the battery, plus real app names and icons. "Charge to full once" is the one name
  everywhere and only stands out while plugged in. The tray setting moved out of Notifications.
- **Fixed (Monitor):** the page jumped whenever an app started or stopped using the disk or network,
  because empty rows were hidden and that resized all four cards. Rows now keep their space, numbers
  have fixed widths, and sampling moved off the main thread (reading two NVMe sensors took 68 ms).
  Headline numbers are lightly averaged; graph scales snap to labelled round numbers; coming back to
  the tab starts a new measurement. A small app no longer fills its bar on an idle laptop.
- **Fixed:** the health chart never drew its line. A broken history line hid everything after it. Any
  paused charge said "fully charged". A sensor that stopped answering left a frozen number. The
  window polled while minimized and on tabs with no live numbers. Half-step axis labels were wrong
  (2.5 W showed as 2 W). The tray said "held at 80%" when the battery was at 78%.
- **Measured:** `tools/monitor_stability.py` (new), 70 s of disk bursts, old → new: page jumps 7 → 0,
  top-app list changes 32 → 9, main-loop stalls over 16 ms 10 → 2, CPU 5.85% → 3.48% of one core.
  `tools/soak.sh 600`, on battery: tray 0.0233% → 0.0183%, service 0.0283% → 0.0233%, memory flat.
  The soak difference is within run-to-run noise; the point is that nothing got worse.
- **Lesson:** live data must not change the layout; rankings need hysteresis; never show an unmeasured
  value as measured. Rules 38–44.

### 2026-11-02: Small fixes from a review (557de64)
- **Fixed:** the LG/Toshiba and MSI notes on the Charging tab were overwritten by the general text. A
  failed tray-extension enable showed no message. Changing the power mode blocked the window on a
  D-Bus call. The desktop file listed two main categories.

### 2026-11-02: Steady top-app lists and sensor fixes (b34eff4)
- **Changed:** top apps are averaged over time and only overtake with a clear lead, in the window and
  the tray. The SSD tile shows the hottest drive.
- **Fixed:** two identical drives shared one row under All sensors. Names cut to 15 characters by the
  kernel (`gnome-system-mo`) now match their app.

### 2026-10-31: Maintenance guide, changelog and test tools
- **Added:** MAINTAINING.md (architecture, feature list, rules learned, pre-commit checks),
  this changelog, CLAUDE.md, and `tools/` (run from source, soak test, nested GNOME Shell,
  screenshots, D-Bus leak test), all checked working.

### 2026-10-31: Per-app usage scanned only on battery (333d7ac)
- **Changed:** the service scans `/proc` for per-app usage only on battery; the first scan after
  unplugging is the baseline.
- **Why:** a 10-minute test showed the service at 0.025% of one core, mostly this scan, whose results
  are only recorded on battery. Now 0.017%, 1 wakeup a minute, 11 MB.
- **Measured (10 minutes, all running):** tray 0.015% of one core and 1 wakeup/min (138 MB resident,
  50 MB its own); window open 0.16%; memory flat.

### 2026-10-31: Time remaining from the recent usage pattern (47ea86b)
- **Changed:** time remaining uses a 3-minute power average instead of 1 minute.
- **Added:** "at the current load" hint on the Overview; charging time allows for slower charging
  above 80%; accuracy regression test.
- **Why:** simulated mixed, gaming-then-idle and steady sessions: mean error 16.0% → 14.5%, steady
  work 6.7% → 4.3%.
- **Lesson:** the first design (fast/slow blend plus last week's typical power) was no better than
  the old average, and history made gaming-then-idle much worse. Simulate before shipping.

### 2026-10-31: README for users, screenshots, license (4e7062a)
- **Added:** full README (install per distribution, Acer driver and Secure Boot, tray setup, every
  screen, supported laptops, how it works, safety, what doesn't work, troubleshooting), screenshots
  from a real GNOME Shell session, LICENSE (GPL-3.0, as the About dialog declared).
- **Lesson:** Broadway screenshots showed squeezed layouts that real desktops don't have; use the
  nested GNOME Shell.

### 2026-10-31: Event-driven, low-cost live battery readings (bbbaf3f)
- **Changed:** batteries are read from one `uevent` file per supply (0.11 ms instead of 4.4 ms);
  slow sensors cached 30 s; kernel power_supply events drive updates; polling only while something
  on screen needs it (2 s), else a 60 s heartbeat; the service sleeps until an event or an adaptive
  deadline (60 s idle, 30 s calibration/top-up, 20 s and 5 s near the limit for the software limit).
- **Added:** `power.py` (UeventMonitor, LiveBattery, smoothing; power derived from energy change
  when the firmware reports none); sanity ranges for readings.
- **Why:** measured against the previous version: tray 0.05% → 0.017%, window 0.375% → 0.125% while
  updating faster, service cycle 4.49 → 0.17 ms with half the wakeups.
- **Lesson:** Acer's battery temperature is a 4 ms WMI firmware call; it was 95% of each reading.

### 2026-10-31: Firmware quirks and tray sampling documented (8957421)
- **Changed:** README sections on per-maker firmware rules, tray sampling and memory figures.

### 2026-10-31: Memory leaks, live tray readings, other makers' quirks (ec58a71)
- **Fixed:** tray D-Bus leak, about 4 KB per call (PyGObject `register_object` with GLib < 2.84);
  answered from a message filter on its own connection instead. 600 menu cycles now flat.
- **Fixed:** window leak, about 7.5 MB per reopen from the tray; the window is hidden and reused.
- **Fixed:** per-app memory overstated about 2.7× (summed RSS); now private memory.
- **Fixed:** per-app CPU share used all CPUs including offline ones; now online CPUs.
- **Fixed (safety):** MSI wrote the start threshold, which moves the limit; "limit off" would have
  capped charging at 10%. Start is never written on MSI.
- **Fixed:** LG gram and Toshiba/Dynabook only accept 80 or 100; the default 85 failed every cycle.
- **Fixed:** usage history counted plugged-in time as on battery on laptops without adapter state.
- **Fixed:** Dell/ThinkPad board temperature could show the CPU; Ryzen used the offset `Tctl`; ARM
  CPU sensors weren't recognised; bogus battery temperatures weren't filtered.
- **Changed:** tray menu readings on demand (fresh on opening, every 2 s while open, nothing while
  closed: 0.03% of one core); `ss` runs asynchronously.
- **Added:** clear error when the firmware rejects a value.

### 2026-10-31: Tray glitches found in real GNOME Shell; screenshots (5229b5a)
- **Fixed:** the "100%" label never appeared (the extension shows labels only on change).
- **Fixed:** Processor row stuck greyed out (stale `enabled: false` from a reused menu ID).
- **Fixed:** idle categories greyed out (submenus with only disabled rows).
- **Fixed:** the 30-second refresh collapsed open submenus and moved rows under the pointer.
- **Added:** README screenshots of the tray.

### 2026-10-31: System monitor in the tray menu (7d0d6e2)
- **Added:** System monitor section in the tray: processor, memory, disk, network rows with top-3
  app submenus, temperatures, Open System Monitor.
- **Changed:** app names and icons from one shared lookup; file-based icons sent as PNG.

### 2026-10-31: Live system monitor page (ea77263)
- **Added:** Monitor tab: processor, memory, disk and network cards with sparklines and top 3
  apps; temperature tiles (CPU, battery, motherboard, SSD, GPU) and all sensors; own symbolic icons.
- **Changed:** "System" tab renamed "Settings"; wider default window.
- **Lesson:** `Adw.Clamp` collapsed the FlowBox to its minimum width; wrapping labels prevented two
  cards per row; Ubuntu's patched purple was invisible on dark cards; NVMe temperature reads cost
  10–30 ms and wake the drive.

### 2026-10-31: Tray icon with a quick charge-limit menu (91061ac)
- **Added:** tray icon (StatusNotifierItem + dbusmenu) with the battery level, limit radios, charge
  to full once, Open and Quit; "Run in background" became "Tray icon"; button to enable GNOME's
  AppIndicator extension; errors from the tray arrive as notifications.

### 2026-10-31: Battery usage monitoring (4b62b2e)
- **Added:** Usage tab (time and energy on battery, average power, runtime of a full charge, time
  on the charger, top apps by processor time on battery); per-app CPU sampling in the service.
- **Fixed:** missing `temperature-symbolic` icon.

### 2026-10-31: Charge to full button with an automatic end (5319fd9)
- **Added:** Charge to full on the Overview; the limit returns on unplug or 6 hours after full;
  `chargekeeper full`.

### 2026-10-31: Permissions, calibration auto-stop, firmware ranges (00ac4ce)
- **Security:** polkit no longer lets every local user change settings without a password; only
  administrators at the active seat.
- **Added:** calibration stops when the firmware finishes, on unplug, or after 24 hours.
- **Fixed:** Dell ranges and minimum gap; Lenovo conservation mode shown as 60–80%.
- **Added:** accessibility labels for the ring and charts; chart values on hover; installer support
  for dnf, pacman and zypper.

### 2026-10-31: CPU busy-loop fixed; charging safety guards (97ab724)
- **Fixed:** the app spun a CPU core at 100% (fans on) because a one-shot idle callback repeated
  forever; now 0% when idle.
- **Fixed (safety):** "pause charging" and "run on battery" could stay on and drain a plugged-in
  laptop; they now end at 20% or on unplug, and the service resets charging when it stops.
- **Fixed:** software-limit laptops reported a limit while it was switched off; battery details
  missing on some laptops; changes lost while the password prompt was open; wrong time during
  charge to full.
- **Changed:** service checks every 30 s instead of 10 s; hidden windows stop refreshing.

## 1.0.0 (18f8d9a)

Initial version: charge limits through kernel thresholds, Acer, Lenovo, Samsung, Sony and the
software limit; charge to 100% once; calibration on Acer; health and history; notifications;
power settings; diagnostics; CLI; installer.
