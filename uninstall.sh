#!/bin/bash
# Remove Chargekeeper. The firmware limit is switched off first so the battery charges normally again.
#   sudo ./uninstall.sh          keep the acer-wmi-battery driver
#   sudo ./uninstall.sh --acer   remove the driver too
set -uo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "Run with sudo: sudo $0 $*" >&2
    exit 1
fi

/usr/lib/chargekeeper/chargekeeper-helper set --limit-enabled off 2>/dev/null
/usr/lib/chargekeeper/chargekeeper-helper release 2>/dev/null
systemctl disable --now chargekeeper.service 2>/dev/null
rm -rf /usr/lib/chargekeeper /etc/chargekeeper /var/lib/chargekeeper /run/chargekeeper
rm -f /usr/bin/chargekeeper /etc/systemd/system/chargekeeper.service \
    /usr/lib/systemd/system-sleep/chargekeeper \
    /usr/share/polkit-1/actions/dev.chargekeeper.Chargekeeper.policy \
    /usr/share/polkit-1/rules.d/50-dev.chargekeeper.Chargekeeper.rules \
    /usr/share/applications/dev.chargekeeper.Chargekeeper.desktop \
    /usr/share/icons/hicolor/scalable/apps/dev.chargekeeper.Chargekeeper.svg \
    /usr/share/icons/hicolor/symbolic/apps/ck-{cpu,memory,temperature,board,fan}-symbolic.svg \
    /etc/modprobe.d/chargekeeper-*.conf
# Only the NVIDIA Dynamic Boost service this installer added (it carries our D-Bus policy).
if [ -f /usr/share/dbus-1/system.d/chargekeeper-nvidia-dbus.conf ]; then
    systemctl stop nvidia-powerd.service 2>/dev/null
    rm -f /etc/systemd/system/nvidia-powerd.service /usr/share/dbus-1/system.d/chargekeeper-nvidia-dbus.conf
fi
systemctl daemon-reload

if [ -n "${SUDO_USER:-}" ]; then
    rm -f "$(getent passwd "$SUDO_USER" | cut -d: -f6)/.config/autostart/dev.chargekeeper.Chargekeeper.desktop"
fi

if [ "${1:-}" = "--acer" ]; then
    modprobe -r acer-wmi-battery 2>/dev/null
    dkms remove -m acer-wmi-battery -v 0.1.0 --all 2>/dev/null
    rm -rf /usr/src/acer-wmi-battery-0.1.0 /etc/modules-load.d/acer-wmi-battery.conf
fi

echo "Chargekeeper removed. Per-user settings stay in ~/.config/chargekeeper; delete them by hand if wanted."
