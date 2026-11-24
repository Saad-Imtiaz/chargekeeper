#!/bin/bash
# Remove Powerkeeper. The firmware limit is switched off first so the battery charges normally again.
#   sudo ./uninstall.sh          keep the acer-wmi-battery driver
#   sudo ./uninstall.sh --acer   remove the driver too
set -uo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "Run with sudo: sudo $0 $*" >&2
    exit 1
fi

/usr/lib/powerkeeper/powerkeeper-helper set --limit-enabled off 2>/dev/null
/usr/lib/powerkeeper/powerkeeper-helper release 2>/dev/null
systemctl disable --now powerkeeper.service 2>/dev/null
rm -rf /usr/lib/powerkeeper /etc/powerkeeper /var/lib/powerkeeper /run/powerkeeper
rm -f /usr/bin/powerkeeper /etc/systemd/system/powerkeeper.service \
    /usr/lib/systemd/system-sleep/powerkeeper \
    /usr/share/polkit-1/actions/dev.powerkeeper.Powerkeeper.policy \
    /usr/share/polkit-1/rules.d/50-dev.powerkeeper.Powerkeeper.rules \
    /usr/share/applications/dev.powerkeeper.Powerkeeper.desktop \
    /usr/share/icons/hicolor/scalable/apps/dev.powerkeeper.Powerkeeper.svg \
    /usr/share/icons/hicolor/symbolic/apps/ck-{cpu,memory,temperature,board,fan}-symbolic.svg \
    /etc/modprobe.d/powerkeeper-*.conf
# Only the NVIDIA Dynamic Boost service this installer added (it carries our D-Bus policy).
if [ -f /usr/share/dbus-1/system.d/powerkeeper-nvidia-dbus.conf ]; then
    systemctl stop nvidia-powerd.service 2>/dev/null
    rm -f /etc/systemd/system/nvidia-powerd.service /usr/share/dbus-1/system.d/powerkeeper-nvidia-dbus.conf
fi
systemctl daemon-reload

if [ -n "${SUDO_USER:-}" ]; then
    rm -f "$(getent passwd "$SUDO_USER" | cut -d: -f6)/.config/autostart/dev.powerkeeper.Powerkeeper.desktop"
fi

if [ "${1:-}" = "--acer" ]; then
    modprobe -r acer-wmi-battery 2>/dev/null
    dkms remove -m acer-wmi-battery -v 0.1.0 --all 2>/dev/null
    rm -rf /usr/src/acer-wmi-battery-0.1.0 /etc/modules-load.d/acer-wmi-battery.conf
fi

echo "Powerkeeper removed. Per-user settings stay in ~/.config/powerkeeper; delete them by hand if wanted."
