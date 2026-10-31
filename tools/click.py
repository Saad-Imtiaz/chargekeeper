#!/usr/bin/python3 -I
"""Click (or hover) at X Y on the nested test display :9 using XTEST.

    python3 tools/click.py X Y [hover]

Only display :9 is opened, so your real pointer and desktop are never touched.
The tray icon in tools/nested-gnome.sh sits at about (1478, 15).
"""

import ctypes
import sys
import time

x11 = ctypes.CDLL("libX11.so.6")
xtst = ctypes.CDLL("libXtst.so.6")
x11.XOpenDisplay.restype = ctypes.c_void_p
display = x11.XOpenDisplay(b":9")
if not display:
    sys.exit("Display :9 is not running (start tools/nested-gnome.sh first)")
display = ctypes.c_void_p(display)
x, y = int(sys.argv[1]), int(sys.argv[2])
xtst.XTestFakeMotionEvent(display, 0, x, y, 0)
x11.XFlush(display)
time.sleep(0.3)
if len(sys.argv) < 4 or sys.argv[3] != "hover":
    xtst.XTestFakeButtonEvent(display, 1, 1, 0)
    x11.XFlush(display)
    time.sleep(0.05)
    xtst.XTestFakeButtonEvent(display, 1, 0, 0)
    x11.XFlush(display)
x11.XCloseDisplay(display)
