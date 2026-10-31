#!/bin/bash
# Install Chargekeeper system-wide.
#   sudo ./install.sh          install or update the app and service
#   sudo ./install.sh --acer   also build the acer-wmi-battery driver (Acer laptops)
set -euo pipefail

ACER_REPO="https://github.com/frederik-h/acer-wmi-battery.git"
ACER_COMMIT="9f90d75cc9237aeed7964622d10dbdf4d2c7b518"
ACER_VERSION="0.1.0"

SRC="$(cd "$(dirname "$0")" && pwd)"
WANT_ACER=0
for arg in "$@"; do
    case "$arg" in
        --acer) WANT_ACER=1 ;;
        *) echo "unknown option: $arg" >&2; exit 2 ;;
    esac
done

if [ "$(id -u)" -ne 0 ]; then
    echo "Run with sudo: sudo $0 $*" >&2
    exit 1
fi

echo "==> Checking dependencies"
missing=()
python3 -c "import gi; gi.require_version('Gtk','4.0'); gi.require_version('Adw','1')" 2>/dev/null \
    || missing+=(gir1.2-gtk-4.0 gir1.2-adw-1)
command -v pkexec >/dev/null || missing+=(pkexec)
if [ ${#missing[@]} -gt 0 ]; then
    if command -v apt-get >/dev/null; then
        apt-get install -y python3-gi "${missing[@]}"
    else
        echo "Install these packages first: python3-gobject, gtk4 (4.14+), libadwaita (1.5+), polkit" >&2
        exit 1
    fi
fi

echo "==> Installing files"
install -d /usr/lib/chargekeeper/chargekeeper
rm -f /usr/lib/chargekeeper/chargekeeper/*.py
install -m 644 "$SRC"/chargekeeper/*.py /usr/lib/chargekeeper/chargekeeper/
install -m 755 "$SRC/bin/chargekeeper-helper" /usr/lib/chargekeeper/chargekeeper-helper
install -m 755 "$SRC/bin/chargekeeper" /usr/bin/chargekeeper
install -Dm 644 "$SRC/data/dev.chargekeeper.Chargekeeper.policy" \
    /usr/share/polkit-1/actions/dev.chargekeeper.Chargekeeper.policy
install -Dm 644 "$SRC/data/chargekeeper.service" /etc/systemd/system/chargekeeper.service
install -Dm 755 "$SRC/data/chargekeeper-resume" /usr/lib/systemd/system-sleep/chargekeeper
install -Dm 644 "$SRC/data/dev.chargekeeper.Chargekeeper.desktop" \
    /usr/share/applications/dev.chargekeeper.Chargekeeper.desktop
install -Dm 644 "$SRC/data/dev.chargekeeper.Chargekeeper.svg" \
    /usr/share/icons/hicolor/scalable/apps/dev.chargekeeper.Chargekeeper.svg
gtk-update-icon-cache -q /usr/share/icons/hicolor 2>/dev/null || true
update-desktop-database -q /usr/share/applications 2>/dev/null || true

if [ "$WANT_ACER" -eq 1 ]; then
    echo "==> Building acer-wmi-battery $ACER_COMMIT with DKMS"
    command -v dkms >/dev/null || apt-get install -y dkms
    [ -d "/lib/modules/$(uname -r)/build" ] || apt-get install -y "linux-headers-$(uname -r)"
    dest="/usr/src/acer-wmi-battery-$ACER_VERSION"
    if dkms status -m acer-wmi-battery -v "$ACER_VERSION" | grep -q .; then
        dkms remove -m acer-wmi-battery -v "$ACER_VERSION" --all || true
    fi
    rm -rf "$dest"
    git clone -q "$ACER_REPO" "$dest"
    git -C "$dest" -c advice.detachedHead=false checkout -q "$ACER_COMMIT"
    rm -rf "$dest/.git"
    cat > "$dest/dkms.conf" <<EOF
PACKAGE_NAME="acer-wmi-battery"
PACKAGE_VERSION="$ACER_VERSION"
BUILT_MODULE_NAME[0]="acer-wmi-battery"
DEST_MODULE_LOCATION[0]="/updates/dkms"
MAKE[0]="make -C \${kernel_source_dir} M=\${dkms_tree}/\${PACKAGE_NAME}/\${PACKAGE_VERSION}/build modules"
CLEAN="make -C \${kernel_source_dir} M=\${dkms_tree}/\${PACKAGE_NAME}/\${PACKAGE_VERSION}/build clean"
AUTOINSTALL="yes"
EOF
    dkms install -m acer-wmi-battery -v "$ACER_VERSION"
    echo acer-wmi-battery > /etc/modules-load.d/acer-wmi-battery.conf
    modprobe acer-wmi-battery
fi

if [ ! -f /etc/chargekeeper/config.json ]; then
    install -d /etc/chargekeeper
    cat > /etc/chargekeeper/config.json <<'EOF'
{
  "limit_enabled": true,
  "end": 85,
  "start": 80,
  "top_up": false,
  "behaviour": "auto",
  "charge_type": null,
  "calibrating": false
}
EOF
fi

echo "==> Starting service"
systemctl daemon-reload
systemctl enable chargekeeper.service >/dev/null
systemctl restart chargekeeper.service
sleep 2

echo
/usr/bin/chargekeeper status || true
echo
echo "Done. Open Chargekeeper from the app menu, or run 'chargekeeper'."
