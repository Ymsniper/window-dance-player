# Copyright (C) 2024 Ymsniper
# SPDX-License-Identifier: GPL-3.0-or-later
"""
Fast X11 window control via libX11 ctypes.

Opens a persistent Display* connection at startup so every move/resize call
is a direct Xlib call — no subprocess spawn or fork overhead per frame.
Falls back gracefully if libX11.so.6 is not loadable.

Globals
-------
_x11lib     ctypes CDLL handle for libX11, or None
_x11dpy     Display* as a plain int, or 0
"""

import ctypes as _ct

from .detect import SESSION_TYPE
from ..logger import DBG

_x11lib = None   # type: _ct.CDLL | None
_x11dpy: int = 0   # Display* as plain int


def _init_x11_fast() -> None:
    """
    Open a persistent libX11 connection for per-frame move/resize calls.

    Must be called once from main() before the physics loop starts.
    No-op on non-X11 sessions.
    """
    global _x11lib, _x11dpy
    if SESSION_TYPE != "x11":
        return
    try:
        lib = _ct.CDLL("libX11.so.6", use_errno=True)
        lib.XInitThreads()                          # required for multi-thread safety
        lib.XOpenDisplay.restype  = _ct.c_void_p
        lib.XOpenDisplay.argtypes = [_ct.c_char_p]
        dpy = lib.XOpenDisplay(None)
        if not dpy:
            DBG("fast X11: XOpenDisplay returned NULL — staying with xdotool")
            return
        _x11lib = lib
        _x11dpy = dpy
        DBG(f"fast X11: libX11 ctypes ready, display={dpy:#x}")
    except Exception as e:
        DBG(f"fast X11 init skipped ({e}) — will use xdotool")
