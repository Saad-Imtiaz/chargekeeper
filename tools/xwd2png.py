#!/usr/bin/python3 -I
"""Convert an X window dump (xwd -root) to PNG, optionally cropped.

    xwd -root -display :9 -silent > screen.xwd
    python3 tools/xwd2png.py screen.xwd out.png [X Y WIDTH HEIGHT]

Handles 32-bit and packed 24-bit pixels: Xephyr uses either, and reading 24-bit
data as 32-bit gives a stretched image with colour stripes.
"""

import struct
import sys

from PIL import Image

data = open(sys.argv[1], "rb").read()
header = struct.unpack(">25I", data[:100])
header_size, width, height = header[0], header[4], header[5]
bits_per_pixel, bytes_per_line, colours = header[11], header[12], header[19]
start = header_size + colours * 12
mode, raw = ("RGBX", "BGRX") if bits_per_pixel == 32 else ("RGB", "BGR")
image = Image.frombuffer(mode, (width, height), data[start:start + bytes_per_line * height],
                         "raw", raw, bytes_per_line, 1).convert("RGB")
if len(sys.argv) > 3:
    x, y, w, h = (int(v) for v in sys.argv[3:7])
    image = image.crop((x, y, x + w, y + h))
image.save(sys.argv[2], optimize=True)
print(image.size)
