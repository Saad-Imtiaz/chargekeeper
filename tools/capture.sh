#!/bin/bash
# Screenshot the app window on the nested display :9, without the black shadow area.
#
#   tools/capture.sh OUT.png
#
# The nested X11 session can't make the window shadow transparent, so the black
# margin around the window is trimmed.
set -eu
TOOLS="$(cd "$(dirname "$0")" && pwd)"
geometry=$(xwininfo -display :9 -root -tree | grep '"KernelThrottle"' | head -1 \
    | grep -oE '[0-9]+x[0-9]+\+[0-9]+\+[0-9]+' | head -1)
[ -n "$geometry" ] || { echo "No KernelThrottle window on :9" >&2; exit 1; }
w=${geometry%%x*}; rest=${geometry#*x}; h=${rest%%+*}; rest=${rest#*+}; x=${rest%%+*}; y=${rest#*+}
dump=$(mktemp --suffix=.xwd)
xwd -root -display :9 -silent > "$dump"
python3 -I "$TOOLS/xwd2png.py" "$dump" "$1" "$x" "$y" "$w" "$h" > /dev/null
rm -f "$dump"
python3 -I - "$1" <<'EOF'
import sys
from PIL import Image
image = Image.open(sys.argv[1]).convert("RGB")
mask = image.convert("L").point(lambda v: 255 if v > 8 else 0)
image.crop(mask.getbbox()).save(sys.argv[1], optimize=True)
print(sys.argv[1], image.crop(mask.getbbox()).size)
EOF
