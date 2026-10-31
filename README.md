# Chargekeeper

Battery charge limits, health tracking and power tools for Linux laptops. GTK 4 / libadwaita app,
a small root service, and a CLI.

## Install

```sh
sudo ./install.sh          # app + service
sudo ./install.sh --acer   # Acer laptops: also builds the acer-wmi-battery driver with DKMS
```

Remove with `sudo ./uninstall.sh` (add `--acer` to remove the driver too). Uninstalling switches
the limit off first.

## What it does

- **Charge limit** with a start (resume) threshold, using whatever the firmware supports:

  | Backend | Laptops | Limit |
  |---|---|---|
  | `thresholds` | ThinkPad, ASUS, Dell, Framework, MSI, Huawei, LG, Samsung Galaxy Book, System76, Toshiba | any value, plus start threshold where present |
  | `acer` | Acer (via [acer-wmi-battery](https://github.com/frederik-h/acer-wmi-battery)) | fixed 80% |
  | `ideapad` | Lenovo IdeaPad/Legion conservation mode | fixed (80% or 60%, model dependent) |
  | `samsung` | older Samsung | fixed 80% |
  | `sony` | Sony VAIO | 50 / 80 / 100% |
  | `software` | firmware that can pause charging (`charge_behaviour`) but has no threshold | any value, enforced by the service |
  | none | everything else | unplug reminder notification at your chosen level |

  A requested limit the firmware can't do is rounded **down** to the nearest supported value, so
  the battery is never charged past what you asked for.
- **Charge to 100% once**: lifts the limit until you unplug, then restores it.
- **Pause charging / run on battery while plugged in**, where the kernel exposes `charge_behaviour`.
- **Charging mode** (Standard, Adaptive, Long Life, …) on Dell and others with `charge_types`.
- **Calibration** on Acer.
- **Health**: full-charge vs design capacity, wear, cycles, temperature, a daily health history.
- **History charts** of charge level (with plugged-in periods) and power draw: 6 h, 24 h, 7 days.
- **Notifications**: unplug reminder, limit reached, top-up done, low battery, battery too hot.
- **Power**: power-profiles-daemon mode, firmware platform profile, CPU energy preference.
- **Re-applies settings** every 30 s (10 s for the software limit) and after resume, for firmware
  that forgets them.
- **Safety**: "pause charging" and "run on battery" switch back to normal charging when the
  battery drops to 20% or the charger is unplugged, and when the service stops.
- **Diagnostics**: detects missing drivers and conflicting TLP thresholds, copies a report.

## CLI

```
chargekeeper status
chargekeeper limit 85        # or: off
chargekeeper topup           # charge to 100% until unplugged
chargekeeper calibrate on    # Acer
```

## Layout

| Path | What |
|---|---|
| `/usr/lib/chargekeeper/` | Python package and `chargekeeper-helper` (root) |
| `/etc/chargekeeper/config.json` | limit settings (system-wide) |
| `/var/lib/chargekeeper/` | history and health logs |
| `~/.config/chargekeeper/prefs.json` | notification preferences (per user) |

The app runs as your user. Changes go through `pkexec chargekeeper-helper`. The polkit policy lets
the active local user change battery settings without a password. The helper only accepts
validated values (percentages and choices the firmware lists).

## Tests

```sh
python3 -m unittest discover tests
```
