#!/bin/bash
# Install Chargekeeper system-wide.
#   sudo ./install.sh           ask for the laptop's maker in a menu, then install or update
#   sudo ./install.sh --acer    the same without asking (any maker: see --help)
set -euo pipefail

ACER_REPO="https://github.com/frederik-h/acer-wmi-battery.git"
ACER_COMMIT="9f90d75cc9237aeed7964622d10dbdf4d2c7b518"
ACER_VERSION="0.1.0"

SRC="$(cd "$(dirname "$0")" && pwd)"

# key | name in the menu | what to expect, shown once chosen
MAKERS=(
    "acer|Acer (also builds the acer-wmi-battery driver)|80% limit on or off, and calibration, through the acer-wmi-battery driver."
    "asus|ASUS|Any limit; some models only honour 60%, 80% or 100%."
    "dell|Dell|Limit 55-100%, resume level 50-95%, at least 5% apart."
    "framework|Framework|Any limit, with a resume level where the firmware has one."
    "gigabyte|Gigabyte|Most models have no charge control in Linux: you get an unplug reminder instead."
    "hp|HP|Most models have no charge control in Linux: you get an unplug reminder instead."
    "huawei|Huawei|Any limit, with a resume level where the firmware has one."
    "lenovo|Lenovo (ThinkPad, IdeaPad, Legion, Yoga)|ThinkPad: any limit and a resume level. IdeaPad, Legion, Yoga: conservation mode holds 60-80%."
    "lg|LG|The limit is 80% or off: the firmware accepts nothing else."
    "microsoft|Microsoft Surface|Most models have no charge control in Linux: you get an unplug reminder instead."
    "msi|MSI|Any limit; the firmware always resumes charging 10% below it."
    "razer|Razer|Most models have no charge control in Linux: you get an unplug reminder instead."
    "samsung|Samsung|Galaxy Book: any limit. Older models: 80% on or off."
    "sony|Sony VAIO|Battery care limiter: 50%, 80% or 100%."
    "system76|System76|Any limit, with a resume level where the firmware has one."
    "toshiba|Toshiba / Dynabook|The limit is 80% or off: the firmware accepts nothing else."
    "other|Other or not sure|Chargekeeper finds out what your firmware supports by itself."
)

usage() {
    cat <<EOF
Install or update Chargekeeper.

  sudo ./install.sh             pick your laptop's maker from a menu
  sudo ./install.sh --MAKER     no menu, where MAKER is one of:
$(for entry in "${MAKERS[@]}"; do printf '      --%-11s %s\n' "${entry%%|*}" "$(cut -d'|' -f2 <<<"$entry")"; done)
                                (--thinkpad, --ideapad, --legion, --yoga, --surface and
                                --dynabook also work; so does --maker NAME)
  ./install.sh --dry-run ...    only show what would be done (no root needed)
  ./install.sh --help           this text
EOF
}

# The key for what the user typed, or nothing. Accepts aliases and any capitalisation.
maker_key() {
    local name="${1,,}" entry
    case "$name" in
        thinkpad|ideapad|legion|yoga) name=lenovo ;;
        surface) name=microsoft ;;
        dynabook) name=toshiba ;;
        vaio) name=sony ;;
    esac
    for entry in "${MAKERS[@]}"; do
        if [ "${entry%%|*}" = "$name" ]; then
            echo "$name"
            return
        fi
    done
}

maker_field() {     # key, field number
    local entry
    for entry in "${MAKERS[@]}"; do
        if [ "${entry%%|*}" = "$1" ]; then
            cut -d'|' -f"$2" <<<"$entry"
            return
        fi
    done
}

# The maker the firmware reports, as a key; "other" if it's one we don't list.
detect_maker() {
    local vendor
    vendor=$(cat "$VENDOR_FILE" 2>/dev/null || true)
    case "${vendor,,}" in
        acer*) echo acer ;;
        asus*) echo asus ;;
        dell*) echo dell ;;
        framework*) echo framework ;;
        gigabyte*) echo gigabyte ;;
        hp|hewlett*) echo hp ;;
        huawei*) echo huawei ;;
        lenovo*) echo lenovo ;;
        lg*) echo lg ;;
        microsoft*) echo microsoft ;;
        micro-star*|msi*) echo msi ;;
        razer*) echo razer ;;
        samsung*) echo samsung ;;
        sony*) echo sony ;;
        system76*) echo system76 ;;
        toshiba*|dynabook*) echo toshiba ;;
        *) echo other ;;
    esac
}

ask_maker() {       # default key; prints the choice on stdout, the menu on stderr
    local default="$1" i=1 entry key choice default_number=1
    {
        echo "Which company made this laptop?"
        echo
        for entry in "${MAKERS[@]}"; do
            key="${entry%%|*}"
            if [ "$key" = "$default" ]; then
                default_number=$i
                printf ' > %2d) %s\n' "$i" "$(cut -d'|' -f2 <<<"$entry")"
            else
                printf '   %2d) %s\n' "$i" "$(cut -d'|' -f2 <<<"$entry")"
            fi
            i=$((i + 1))
        done
        echo
    } >&2
    while true; do
        printf 'Type a number or a name, or press Enter for %s: ' "$default_number" >&2
        IFS= read -r choice || { echo >&2; return 1; }      # Ctrl-D: don't install anything
        choice="${choice//[[:space:]]/}"
        choice="${choice#--}"
        if [ -z "$choice" ]; then
            echo "$default"
            return
        fi
        if [[ "$choice" =~ ^[0-9]+$ ]] && [ "$choice" -ge 1 ] && [ "$choice" -le "${#MAKERS[@]}" ]; then
            entry="${MAKERS[$((choice - 1))]}"
            echo "${entry%%|*}"
            return
        fi
        key=$(maker_key "$choice")
        if [ -n "$key" ]; then
            echo "$key"
            return
        fi
        echo "\"$choice\" isn't on the list." >&2
    done
}

MAKER=""
DRY_RUN=0
while [ $# -gt 0 ]; do
    case "$1" in
        -h|--help) usage; exit 0 ;;
        --dry-run) DRY_RUN=1 ;;
        --maker=*) wanted="${1#--maker=}" ;;
        --maker)
            shift
            wanted="${1:-}"
            ;;
        --*) wanted="${1#--}" ;;
        *) wanted="$1" ;;
    esac
    if [ "$1" != "--dry-run" ]; then
        key=$(maker_key "${wanted:-}")
        if [ -z "$key" ]; then
            echo "Unknown option or maker: $1 (see ./install.sh --help)" >&2
            exit 2
        fi
        if [ -n "$MAKER" ] && [ "$MAKER" != "$key" ]; then
            echo "Choose one maker, not both $MAKER and $key." >&2
            exit 2
        fi
        MAKER="$key"
    fi
    shift
done

VENDOR_FILE=/sys/class/dmi/id/sys_vendor
SAVED_MAKER=/etc/chargekeeper/maker
if [ "$DRY_RUN" -eq 1 ]; then       # tests only; a real install never reads these
    VENDOR_FILE="${CHARGEKEEPER_TEST_VENDOR_FILE:-$VENDOR_FILE}"
    SAVED_MAKER="${CHARGEKEEPER_TEST_SAVED_MAKER:-$SAVED_MAKER}"
elif [ "$(id -u)" -ne 0 ]; then
    echo "Run with sudo: sudo $0${MAKER:+ --$MAKER}" >&2
    exit 1
fi

DETECTED=$(detect_maker)
# The driver is third-party code, so it's only built when someone chose Acer: by flag or menu.
WANT_ACER=0
if [ -z "$MAKER" ]; then
    default=$(maker_key "$(cat "$SAVED_MAKER" 2>/dev/null || true)")
    default="${default:-$DETECTED}"
    if [ -t 0 ]; then
        MAKER=$(ask_maker "$default") || { echo "Nothing was installed." >&2; exit 1; }
        [ "$MAKER" = acer ] && WANT_ACER=1
    else
        MAKER="$default"
        echo "No terminal to ask in, so using: $(maker_field "$MAKER" 2)"
        [ "$MAKER" = acer ] && echo "The Acer driver isn't built unless you ask: sudo ./install.sh --acer"
    fi
elif [ "$MAKER" = acer ]; then
    WANT_ACER=1
fi

echo "==> Installing for: $(maker_field "$MAKER" 2)"
echo "    $(maker_field "$MAKER" 3)"
if [ "$MAKER" != other ] && [ "$DETECTED" != other ] && [ "$MAKER" != "$DETECTED" ]; then
    echo "    Note: this laptop's firmware says it's made by \"$(cat "$VENDOR_FILE")\"."
fi
if [ "$DRY_RUN" -eq 1 ]; then
    [ "$WANT_ACER" -eq 1 ] && echo "Would build the acer-wmi-battery driver."
    echo "Dry run: nothing was changed."
    exit 0
fi

echo "==> Checking dependencies"
missing=()
python3 -c "import gi; gi.require_version('Gtk','4.0'); gi.require_version('Adw','1')" 2>/dev/null \
    || missing+=(gir1.2-gtk-4.0 gir1.2-adw-1)
command -v pkexec >/dev/null || missing+=(pkexec)
if [ ${#missing[@]} -gt 0 ]; then
    if command -v apt-get >/dev/null; then
        apt-get install -y python3-gi "${missing[@]}"
    elif command -v dnf >/dev/null; then
        dnf install -y python3-gobject gtk4 libadwaita polkit
    elif command -v pacman >/dev/null; then
        pacman -S --needed --noconfirm python-gobject gtk4 libadwaita polkit
    elif command -v zypper >/dev/null; then
        zypper --non-interactive install python3-gobject typelib-1_0-Gtk-4_0 typelib-1_0-Adw-1 polkit
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
install -Dm 644 "$SRC/data/dev.chargekeeper.Chargekeeper.rules" \
    /usr/share/polkit-1/rules.d/50-dev.chargekeeper.Chargekeeper.rules
install -Dm 644 "$SRC/data/chargekeeper.service" /etc/systemd/system/chargekeeper.service
install -Dm 755 "$SRC/data/chargekeeper-resume" /usr/lib/systemd/system-sleep/chargekeeper
install -Dm 644 "$SRC/data/dev.chargekeeper.Chargekeeper.desktop" \
    /usr/share/applications/dev.chargekeeper.Chargekeeper.desktop
install -Dm 644 "$SRC/data/dev.chargekeeper.Chargekeeper.svg" \
    /usr/share/icons/hicolor/scalable/apps/dev.chargekeeper.Chargekeeper.svg
install -Dm 644 -t /usr/share/icons/hicolor/symbolic/apps "$SRC"/data/icons/hicolor/symbolic/apps/*.svg
gtk-update-icon-cache -q /usr/share/icons/hicolor 2>/dev/null || true
update-desktop-database -q /usr/share/applications 2>/dev/null || true

# NVIDIA Dynamic Boost: some distributions (Ubuntu) ship nvidia-powerd without its service
# and D-Bus policy. Add ours, not enabled at boot: the service starts it only while
# maximum performance is on.
if [ -x /usr/bin/nvidia-powerd ] && ! systemctl cat nvidia-powerd.service >/dev/null 2>&1; then
    echo "==> Adding the NVIDIA Dynamic Boost service (nvidia-powerd)"
    install -Dm 644 "$SRC/data/nvidia-powerd.service" /etc/systemd/system/nvidia-powerd.service
    install -Dm 644 "$SRC/data/chargekeeper-nvidia-dbus.conf" \
        /usr/share/dbus-1/system.d/chargekeeper-nvidia-dbus.conf
fi

# Runs in a subshell with its own "set -e", so a failure here doesn't stop the service
# from being started: the app still works without charge control, and says what's missing.
build_acer_driver() {
    if command -v apt-get >/dev/null; then
        command -v dkms >/dev/null || apt-get install -y dkms
        command -v git >/dev/null || apt-get install -y git
        [ -d "/lib/modules/$(uname -r)/build" ] || apt-get install -y "linux-headers-$(uname -r)"
    fi
    for tool in dkms git; do
        command -v "$tool" >/dev/null || { echo "Install $tool first" >&2; return 1; }
    done
    [ -d "/lib/modules/$(uname -r)/build" ] || { echo "Install the kernel headers for $(uname -r) first" >&2; return 1; }
    dest="/usr/src/acer-wmi-battery-$ACER_VERSION"
    # An update shouldn't rebuild (and, with Secure Boot, re-sign) a driver that's already right.
    if [ "$(cat "$dest/.chargekeeper-commit" 2>/dev/null)" = "$ACER_COMMIT" ] \
        && dkms status -m acer-wmi-battery -v "$ACER_VERSION" -k "$(uname -r)" | grep -q installed; then
        echo "    Already built for this kernel."
    else
        echo "==> Building acer-wmi-battery $ACER_COMMIT with DKMS"
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
        echo "$ACER_COMMIT" > "$dest/.chargekeeper-commit"
    fi
    echo acer-wmi-battery > /etc/modules-load.d/acer-wmi-battery.conf
    modprobe acer-wmi-battery
}

DRIVER_FAILED=0
if [ "$WANT_ACER" -eq 1 ]; then
    set +e
    ( set -e; build_acer_driver )
    DRIVER_FAILED=$?
    set -e
fi

install -d /etc/chargekeeper
echo "$MAKER" > "$SAVED_MAKER"     # the menu's default next time
if [ ! -f /etc/chargekeeper/config.json ]; then
    cat > /etc/chargekeeper/config.json <<'EOF'
{
  "limit_enabled": true,
  "end": 85,
  "start": 80,
  "top_up": false,
  "behaviour": "auto",
  "charge_type": null,
  "calibrating": false,
  "calibration_started": null
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
if [ "$DRIVER_FAILED" -ne 0 ]; then
    cat >&2 <<EOF
Chargekeeper is installed, but the Acer driver isn't working yet, so the charge limit can't be set.
- Secure Boot on: if you were asked for a password, reboot and enroll the key (MOK), then it loads.
- Otherwise read the messages above, fix what they name and run: sudo ./install.sh --acer
EOF
    exit 1
fi
echo "Done. Open Chargekeeper from the app menu, or run 'chargekeeper'."
