#!/bin/bash
# Measure CPU, wakeups and memory of the tray app and the service loop over time.
#
#   tools/soak.sh [SECONDS]      default 600 (10 minutes)
#
# Runs, from this source tree:
#   tray     tools/run_app.py --tray on your real desktop session (no window; the
#            icon only shows if a tray host such as GNOME's AppIndicator is on)
#   service  tools/service_as_user.py (the real service loop, files in a temp dir)
# and prints one line a minute plus a summary. Compare a run before and after a
# change; memory that keeps growing minute after minute is a leak.
set -u
DURATION=${1:-600}
REPO="$(cd "$(dirname "$0")/.." && pwd)"
WORK=$(mktemp -d -t chargekeeper-soak.XXXXXX)
mkdir -p "$WORK/config"

XDG_CONFIG_HOME="$WORK/config" timeout $((DURATION + 60)) python3 -I "$REPO/tools/run_app.py" --tray \
    --app-id dev.chargekeeper.Soak > "$WORK/tray.log" 2>&1 &
tray_parent=$!
timeout $((DURATION + 60)) python3 -I "$REPO/tools/service_as_user.py" "$WORK/service" > "$WORK/service.log" 2>&1 &
service_parent=$!
sleep 15     # let both start up before measuring

pid_of() { pgrep -P "$1" python3 || echo "$1"; }
tray=$(pid_of $tray_parent)
service=$(pid_of $service_parent)
hz=$(getconf CLK_TCK)
cpu() { awk '{print $14+$15}' "/proc/$1/stat"; }
rss() { awk '/^VmRSS/{print int($2/1024)}' "/proc/$1/status"; }
wake() { awk '/^voluntary_ctxt_switches/{v=$2} /^nonvoluntary_ctxt_switches/{n=$2} END{print v+n}' "/proc/$1/status"; }
secs() { python3 -c "print(round(($1)/$hz, 2))"; }

tray_cpu=$(cpu $tray); tray_wake=$(wake $tray)
service_cpu=$(cpu $service); service_wake=$(wake $service)
echo "minute   tray: CPU s  RSS MB   service: CPU s  RSS MB"
for minute in $(seq 0 $((DURATION / 60))); do
    printf "%6s   %12s %7s   %15s %7s\n" "$minute" \
        "$(secs "$(cpu $tray) - $tray_cpu")" "$(rss $tray)" \
        "$(secs "$(cpu $service) - $service_cpu")" "$(rss $service)"
    [ "$minute" -lt $((DURATION / 60)) ] && sleep 60
done
echo
for name in tray service; do
    pid=${!name}; start_cpu=${name}_cpu; start_wake=${name}_wake
    used=$(python3 -c "print(($(cpu $pid) - ${!start_cpu}) / $hz)")
    echo "$name: $(python3 -c "print(round($used / $DURATION * 100, 4))")% of one core," \
         "$(( ($(wake $pid) - ${!start_wake}) * 60 / DURATION )) wakeups/min, $(rss $pid) MB resident"
done
kill $tray_parent $service_parent 2>/dev/null
wait 2>/dev/null
grep -hiE 'error|traceback' "$WORK"/*.log | grep -v WARNING | head -5
echo "Logs: $WORK"
