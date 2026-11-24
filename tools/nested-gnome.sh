#!/bin/bash
# A private GNOME Shell in a Xephyr window, for screenshots and tray testing.
#
#   tools/nested-gnome.sh [SECONDS] [-- run_app.py arguments]
#   e.g. tools/nested-gnome.sh 300 -- --tray
#        tools/nested-gnome.sh 300 -- --pages overview,usage,monitor,charging,health,system
#
# Starts Xephyr on display :9 and GNOME Shell (Ubuntu mode) inside it with its own
# D-Bus session and throwaway settings (GSETTINGS_BACKEND=keyfile), so your real
# desktop settings are never changed. The AppIndicator extension is on there, so the
# tray icon shows. Then runs tools/run_app.py with the given arguments.
#
# Afterwards, from another terminal:
#   tools/capture.sh out.png          crop the app window from :9
#   python3 tools/xwd2png.py ...      whole screen (see capture.sh)
#   python3 tools/click.py X Y        click inside :9 only (never your real pointer)
#   export DBUS_SESSION_BUS_ADDRESS=$(cat /tmp/kernelthrottle-nested/bus.txt)   talk to its bus
#
# Why not Broadway (GDK_BACKEND=broadway)? It has no font DPI setting, so libadwaita's
# "sp" sizes and breakpoints collapse: pages render as narrow columns that real
# desktops never show. Use Broadway only for quick CPU measurements, never for layout.
set -u
DURATION=${1:-300}
shift || true
[ "${1:-}" = "--" ] && shift
REPO="$(cd "$(dirname "$0")/.." && pwd)"
WORK=/tmp/kernelthrottle-nested
rm -rf "$WORK"; mkdir -p "$WORK"/{config,data,cache,runtime}; chmod 700 "$WORK/runtime"
# Our symbolic icons, as an install would provide them.
mkdir -p "$WORK/data/icons/hicolor/symbolic/apps"
cp "$REPO"/data/icons/hicolor/symbolic/apps/*.svg "$WORK/data/icons/hicolor/symbolic/apps/"
cp /usr/share/icons/hicolor/index.theme "$WORK/data/icons/hicolor/"

for pid in $(pgrep -x Xephyr); do kill "$pid"; done
DISPLAY=${DISPLAY:-:0} timeout $((DURATION + 20)) Xephyr :9 -screen 1600x1200 -ac -br -noreset > "$WORK/xephyr.log" 2>&1 &
sleep 2

cat > "$WORK/session.sh" <<EOF
#!/bin/bash
export XDG_CONFIG_HOME=$WORK/config XDG_DATA_HOME=$WORK/data XDG_CACHE_HOME=$WORK/cache XDG_RUNTIME_DIR=$WORK/runtime
export GSETTINGS_BACKEND=keyfile DISPLAY=:9 GDK_BACKEND=x11 GSK_RENDERER=cairo
export XDG_SESSION_TYPE=x11 XDG_CURRENT_DESKTOP=ubuntu:GNOME
unset XDG_SESSION_ID WAYLAND_DISPLAY NOTIFY_SOCKET
gsettings set org.gnome.desktop.interface color-scheme \${SCHEME:-prefer-dark}
gsettings set org.gnome.desktop.interface gtk-theme Yaru-dark
gsettings set org.gnome.desktop.interface icon-theme Yaru-dark
gsettings set org.gnome.desktop.background picture-options none
gsettings set org.gnome.desktop.background primary-color '#2b3540'
gsettings set org.gnome.desktop.background color-shading-type solid
gsettings set org.gnome.shell welcome-dialog-last-shown-version '99'
gsettings set org.gnome.shell enabled-extensions "['ubuntu-appindicators@ubuntu.com']"
gsettings set org.gnome.shell disabled-extensions "['ding@rastersoft.com']"
echo "\$DBUS_SESSION_BUS_ADDRESS" > $WORK/bus.txt
gnome-shell --x11 --mode=ubuntu --replace > $WORK/shell.log 2>&1 &
sleep 10
python3 -I $REPO/tools/run_app.py $* > $WORK/app.log 2>&1 &
echo \$! > $WORK/app.pid
sleep $DURATION
EOF
chmod +x "$WORK/session.sh"
timeout $((DURATION + 15)) dbus-run-session -- "$WORK/session.sh" > "$WORK/session.log" 2>&1 &
echo "Nested GNOME Shell on :9 for $DURATION s. App starts in about 12 s. Files: $WORK"
