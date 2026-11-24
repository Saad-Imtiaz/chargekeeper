#!/bin/bash
# Remove KernelThrottle and every earlier version of it (Chargekeeper, Powerkeeper), with all
# their settings, history and driver options. The charge limit is switched off first so the
# battery charges normally again, and fans and performance go back to the firmware.
#   sudo ./uninstall.sh          keep the acer-wmi-battery driver
#   sudo ./uninstall.sh --acer   remove the driver too
set -uo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "Run with sudo: sudo $0 $*" >&2
    exit 1
fi

# name | app ID | icon prefix
NAMES=(
    "kernelthrottle|dev.kernelthrottle.KernelThrottle|kt"
    "powerkeeper|dev.powerkeeper.Powerkeeper|pk"
    "chargekeeper|dev.chargekeeper.Chargekeeper|ck"
)

HOMES=()
if [ -n "${SUDO_USER:-}" ]; then
    HOMES+=("$(getent passwd "$SUDO_USER" | cut -d: -f6)")
fi

found=0
for entry in "${NAMES[@]}"; do
    IFS="|" read -r name app_id icon <<< "$entry"
    helper="/usr/lib/$name/$name-helper"
    if [ -e "/usr/lib/$name" ] || [ -e "/etc/$name" ] || [ -e "/var/lib/$name" ] \
            || [ -e "/etc/systemd/system/$name.service" ] || [ -e "/usr/bin/$name" ]; then
        found=1
        echo "==> Removing $name"
    fi
    if [ -x "$helper" ]; then
        "$helper" set --limit-enabled off 2>/dev/null
        "$helper" release 2>/dev/null
    fi
    systemctl disable --now "$name.service" 2>/dev/null
    pkill -x "$name" 2>/dev/null
    rm -rf "/usr/lib/$name" "/etc/$name" "/var/lib/$name" "/run/$name"
    rm -f "/usr/bin/$name" "/etc/systemd/system/$name.service" \
        "/usr/lib/systemd/system-sleep/$name" \
        "/usr/share/polkit-1/actions/$app_id.policy" \
        "/usr/share/polkit-1/rules.d/50-$app_id.rules" \
        "/usr/share/applications/$app_id.desktop" \
        "/usr/share/icons/hicolor/scalable/apps/$app_id.svg" \
        /usr/share/icons/hicolor/symbolic/apps/"$icon"-{cpu,memory,temperature,board,fan}-symbolic.svg \
        /etc/modprobe.d/"$name"-*.conf
    # The NVIDIA Dynamic Boost service, only where our installer added it.
    if [ -f "/usr/share/dbus-1/system.d/$name-nvidia-dbus.conf" ]; then
        systemctl stop nvidia-powerd.service 2>/dev/null
        rm -f /etc/systemd/system/nvidia-powerd.service "/usr/share/dbus-1/system.d/$name-nvidia-dbus.conf"
    fi
    for home in "${HOMES[@]}"; do
        rm -rf "$home/.config/$name"
        rm -f "$home/.config/autostart/$app_id.desktop"
    done
done
systemctl daemon-reload
gtk-update-icon-cache -q /usr/share/icons/hicolor 2>/dev/null || true
update-desktop-database -q /usr/share/applications 2>/dev/null || true

if [ "${1:-}" = "--acer" ]; then
    echo "==> Removing the acer-wmi-battery driver"
    modprobe -r acer-wmi-battery 2>/dev/null
    dkms remove -m acer-wmi-battery -v 0.1.0 --all 2>/dev/null
    rm -rf /usr/src/acer-wmi-battery-0.1.0 /etc/modules-load.d/acer-wmi-battery.conf
fi

if [ "$found" -eq 0 ]; then
    echo "Nothing to remove: no KernelThrottle, Powerkeeper or Chargekeeper found."
else
    echo "Removed, with all settings and history. Driver options written by \"Turn on\" end at"
    echo "the next restart."
fi
