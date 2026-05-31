# Copyright (C) 2024 Ymsniper
# SPDX-License-Identifier: GPL-3.0-or-later
"""
Cross-platform window control dispatch.

Public API
----------
get_window_id()            → opaque handle for the current terminal window
get_screen_size()          → (width, height) of the primary display
get_window_geometry(wid)   → (x, y, width, height) of a window
move_window(wid, x, y, w?, h?)  → move (and optionally resize) a window

Backend priority
----------------
Hyprland  →  direct Unix IPC socket (_hyprland_batch / _hyprland_dispatch)
Sway      →  direct Unix IPC socket (i3 binary protocol)
KDE       →  one-shot KWin scripting via qdbus
X11       →  libX11 ctypes (fast) → xdotool subprocess (fallback)
macOS     →  osascript / AppleScript
Windows   →  ctypes user32
"""

import ctypes as _ct
import json
import subprocess

from . import detect as _det
from . import hyprland as _hypr
from . import kde as _kde
from . import sway as _sway
from . import x11 as _x11
from ..logger import DBG, ERR, WARN

PLATFORM     = _det.PLATFORM
SESSION_TYPE = _det.SESSION_TYPE
COMPOSITOR   = _det.COMPOSITOR


# ── Window ID ────────────────────────────────────────────────────────────────

def get_window_id():
    """
    Return an opaque handle for the current terminal window.

    Returns
    -------
    str | int | None
        Wayland  → compositor sentinel: "kde:PID", "hyprland:ADDR", "sway"
        X11      → numeric xdotool window ID string
        None     → window control unavailable
    """
    if PLATFORM == "Darwin":
        return "macos"

    if PLATFORM == "Windows":
        try:
            import ctypes
            return ctypes.windll.user32.GetForegroundWindow()
        except Exception:
            return None

    if PLATFORM != "Linux":
        return None

    # ── Wayland ──────────────────────────────────────────────────────────────
    if SESSION_TYPE == "wayland":

        if COMPOSITOR == "hyprland":
            DBG("get_window_id: Hyprland path")
            raw = _hypr._hyprland_query("j/activewindow")
            DBG(f"activewindow IPC raw={raw[:200]!r}")
            try:
                addr = json.loads(raw).get("address", "")
            except Exception as e:
                ERR(f"IPC json parse failed: {e}")
                addr = ""

            if not addr:
                DBG("IPC gave no addr, trying hyprctl binary")
                try:
                    r = subprocess.run(
                        ["hyprctl", "activewindow", "-j"],
                        capture_output=True, text=True, timeout=2,
                    )
                    DBG(f"hyprctl stdout={r.stdout[:200]!r}")
                    addr = json.loads(r.stdout).get("address", "")
                except Exception as e:
                    ERR(f"hyprctl fallback failed: {e}")

            DBG(f"window addr={addr!r}")
            if addr:
                _hypr._hyprland_dispatch(f"setfloating address:{addr}")
                return f"hyprland:{addr}"
            ERR("Could not get window address — dance will not work")
            return "hyprland"

        if COMPOSITOR == "kde":
            tpid = _det.get_terminal_pid()
            DBG(f"KDE terminal PID={tpid}")
            return f"kde:{tpid}" if tpid else "kde:0"

        if COMPOSITOR == "sway":
            return COMPOSITOR

        return None

    # ── X11 / XWayland ───────────────────────────────────────────────────────
    try:
        r = subprocess.run(
            ["xdotool", "getactivewindow"],
            capture_output=True, text=True, timeout=2,
        )
        wid = r.stdout.strip()
        return wid if wid else None
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None


# ── Screen size ───────────────────────────────────────────────────────────────

def get_screen_size() -> "tuple[int, int]":
    """Return (width, height) of the primary display."""
    if PLATFORM == "Linux":
        # xrandr works on X11 and via XWayland on most compositors
        try:
            import re
            r = subprocess.run(
                ["xrandr", "--current"],
                capture_output=True, text=True, timeout=2,
            )
            for line in r.stdout.splitlines():
                if " connected" in line:
                    m = re.search(r"(\d+)x(\d+)\+0\+0", line)
                    if m:
                        return int(m.group(1)), int(m.group(2))
                    m = re.search(r"(\d+)x(\d+)\+", line)
                    if m:
                        return int(m.group(1)), int(m.group(2))
        except Exception:
            pass

        if COMPOSITOR == "hyprland":
            try:
                r = subprocess.run(
                    ["hyprctl", "monitors", "-j"],
                    capture_output=True, text=True, timeout=2,
                )
                monitors = json.loads(r.stdout)
                for m in monitors:
                    if m.get("focused"):
                        return m["width"], m["height"]
                if monitors:
                    return monitors[0]["width"], monitors[0]["height"]
            except Exception:
                pass

        if COMPOSITOR == "sway":
            try:
                r = subprocess.run(
                    ["swaymsg", "-t", "get_outputs"],
                    capture_output=True, text=True, timeout=2,
                )
                for o in json.loads(r.stdout):
                    if o.get("focused") and o.get("current_mode"):
                        return o["current_mode"]["width"], o["current_mode"]["height"]
            except Exception:
                pass

        if SESSION_TYPE == "x11":
            try:
                r = subprocess.run(
                    ["xdotool", "getdisplaygeometry"],
                    capture_output=True, text=True, timeout=2,
                )
                w, h = r.stdout.strip().split()
                return int(w), int(h)
            except Exception:
                pass

    elif PLATFORM == "Darwin":
        try:
            script = 'tell application "Finder" to get bounds of window of desktop'
            r = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True, text=True, timeout=2,
            )
            parts = r.stdout.strip().split(", ")
            return int(parts[2]), int(parts[3])
        except Exception:
            pass

    elif PLATFORM == "Windows":
        try:
            import ctypes
            u = ctypes.windll.user32
            return u.GetSystemMetrics(0), u.GetSystemMetrics(1)
        except Exception:
            pass

    return 1920, 1080   # last-resort fallback


# ── Screen scale factor ────────────────────────────────────────────────────────

def get_scale_factor() -> float:
    """
    Return the KDE display scale factor (1.0 on non-KDE or when unset).

    On KDE Plasma Wayland, xrandr (via XWayland) reports the **physical**
    pixel resolution, but KWin's frameGeometry API uses **logical** pixels
    (physical / scale).  Reading the scale here lets callers convert:

        logical_size = physical_size / get_scale_factor()

    Priority:
      1. kwinrc  → [Xwayland]  Scale       (set by KDE display settings)
      2. kdeglobals → [KScreen] ScaleFactor (older KDE / fallback)
      3. 1.0  (no scaling or non-KDE platform)
    """
    if PLATFORM != "Linux" or COMPOSITOR != "kde":
        return 1.0
    from pathlib import Path
    from configparser import ConfigParser

    # kwinrc: preferred — written by KDE display settings on Wayland
    try:
        cp = ConfigParser()
        cp.read(str(Path.home() / ".config" / "kwinrc"))
        if "Xwayland" in cp and "Scale" in cp["Xwayland"]:
            v = float(cp["Xwayland"]["Scale"])
            DBG(f"get_scale_factor: kwinrc [Xwayland] Scale={v}")
            return v
    except Exception as exc:
        DBG(f"get_scale_factor: kwinrc read failed: {exc}")

    # kdeglobals: fallback for older KDE setups
    try:
        cp2 = ConfigParser()
        cp2.read(str(Path.home() / ".config" / "kdeglobals"))
        if "KScreen" in cp2 and "ScaleFactor" in cp2["KScreen"]:
            v = float(cp2["KScreen"]["ScaleFactor"])
            DBG(f"get_scale_factor: kdeglobals [KScreen] ScaleFactor={v}")
            return v
    except Exception as exc:
        DBG(f"get_scale_factor: kdeglobals read failed: {exc}")

    DBG("get_scale_factor: no KDE scale config found, using 1.0")
    return 1.0


# ── Window geometry ───────────────────────────────────────────────────────────

def get_window_geometry(window_id) -> "tuple[int, int, int, int]":
    """
    Return (x, y, width, height) of the given window.

    Falls back to (100, 100, 800, 500) if the query fails.
    """
    if PLATFORM == "Linux":
        if (
            SESSION_TYPE == "x11"
            and window_id
            and not str(window_id).startswith("hyprland")
            and window_id not in ("kde", "sway")
        ):
            try:
                r = subprocess.run(
                    ["xdotool", "getwindowgeometry", str(window_id)],
                    capture_output=True, text=True, timeout=2,
                )
                x = y = w = h = None
                for line in r.stdout.splitlines():
                    if "Position" in line:
                        pos = line.split(":")[-1].strip().split(",")
                        x = int(pos[0].strip())
                        y = int(pos[1].strip().split()[0])
                    if "Geometry" in line:
                        geom = line.split(":")[-1].strip()
                        w, h = (int(v) for v in geom.split("x"))
                if None not in (x, y, w, h):
                    return x, y, w, h
            except Exception:
                pass

        if COMPOSITOR == "hyprland":
            try:
                r = subprocess.run(
                    ["hyprctl", "activewindow", "-j"],
                    capture_output=True, text=True, timeout=2,
                )
                data = json.loads(r.stdout)
                at   = data.get("at",   [100, 100])
                size = data.get("size", [800, 500])
                return at[0], at[1], size[0], size[1]
            except Exception:
                pass

        if COMPOSITOR == "sway":
            try:
                r = subprocess.run(
                    ["swaymsg", "-t", "get_tree"],
                    capture_output=True, text=True, timeout=2,
                )

                def _find_focused(node):
                    if node.get("focused"):
                        rect = node.get("rect", {})
                        return (
                            rect.get("x", 100), rect.get("y", 100),
                            rect.get("width", 800), rect.get("height", 500),
                        )
                    for child in (
                        node.get("nodes", []) + node.get("floating_nodes", [])
                    ):
                        result = _find_focused(child)
                        if result:
                            return result
                    return None

                result = _find_focused(json.loads(r.stdout))
                if result:
                    return result
            except Exception:
                pass

    return 100, 100, 800, 500


# ── Move / resize ─────────────────────────────────────────────────────────────

def move_window(
    window_id,
    x: float,
    y: float,
    w: "float | None" = None,
    h: "float | None" = None,
) -> None:
    """
    Move (and optionally resize) a window to screen position (x, y).

    Parameters
    ----------
    window_id   Opaque handle returned by get_window_id()
    x, y        Target top-left corner in screen pixels
    w, h        Optional new size; omit to keep current size
    """
    if not window_id:
        return

    if PLATFORM == "Linux":

        # ── KDE Wayland via KWin scripting ───────────────────────────────
        if window_id == "kde" or (
            isinstance(window_id, str) and window_id.startswith("kde:")
        ):
            tpid  = int(window_id.split(":")[1]) if ":" in window_id else 0
            new_w = int(w) if w else None
            new_h = int(h) if h else None
            geom  = (
                f"{{x:{int(x)},y:{int(y)},width:{new_w},height:{new_h}}}"
                if new_w and new_h
                else f"{{x:{int(x)},y:{int(y)},width:g.width,height:g.height}}"
            )
            find = (
                f"var wins=workspace.windowList(),tgt=null;"
                f"for(var i=0;i<wins.length;i++){{"
                f"if(wins[i].pid==={tpid}){{tgt=wins[i];break;}}}}"
                f"var w=tgt;"
                if tpid
                else "var w=workspace.activeWindow;"
            )
            DBG(f"KDE: KWin script pid={tpid} move to {int(x)},{int(y)} size={new_w}x{new_h}")
            _kde._kwin_script(
                f"{find}"
                f"if(w){{"
                f"  var g=w.frameGeometry;"
                f"  w.frameGeometry={geom};"
                f"}}"
            )
            return

        # ── Hyprland via IPC socket ───────────────────────────────────────
        if window_id == "hyprland" or window_id.startswith("hyprland:"):
            addr = window_id.split(":", 1)[1] if ":" in window_id else ""
            if not addr:
                WARN("move_window: no stored address, querying active window")
                raw = _hypr._hyprland_query("j/activewindow")
                try:
                    addr = json.loads(raw).get("address", "")
                except Exception as e:
                    ERR(f"move_window fallback failed: {e}")
            if addr:
                if w is not None and h is not None:
                    _hypr._hyprland_batch([
                        f"resizewindowpixel exact {int(w)} {int(h)},address:{addr}",
                        f"movewindowpixel exact {int(x)} {int(y)},address:{addr}",
                    ])
                else:
                    _hypr._hyprland_dispatch(
                        f"movewindowpixel exact {int(x)} {int(y)},address:{addr}"
                    )
            else:
                ERR("move_window: still no address from active window query")
            return

        # ── Sway ─────────────────────────────────────────────────────────
        if window_id == "sway":
            try:
                if w is not None and h is not None:
                    subprocess.run(
                        ["swaymsg", f"[focused] resize set {int(w)} {int(h)}"],
                        capture_output=True, timeout=1,
                    )
                subprocess.run(
                    ["swaymsg", f"[focused] move position {int(x)} {int(y)}"],
                    capture_output=True, timeout=1,
                )
            except Exception:
                pass
            return

        # ── X11 / XWayland — fast ctypes path ────────────────────────────
        try:
            wid_int = int(window_id)
            if _x11._x11lib and _x11._x11dpy and wid_int:
                dpy = _ct.c_void_p(_x11._x11dpy)
                win = _ct.c_ulong(wid_int)
                if w is not None and h is not None:
                    _x11._x11lib.XMoveResizeWindow(
                        dpy, win,
                        _ct.c_int(int(x)), _ct.c_int(int(y)),
                        _ct.c_uint(int(w)), _ct.c_uint(int(h)),
                    )
                else:
                    _x11._x11lib.XMoveWindow(
                        dpy, win, _ct.c_int(int(x)), _ct.c_int(int(y))
                    )
                _x11._x11lib.XFlush(dpy)
                return
        except Exception:
            pass

        # ── X11 — xdotool subprocess fallback ────────────────────────────
        try:
            if w is not None and h is not None:
                subprocess.run(
                    [
                        "xdotool",
                        "windowsize", str(window_id), str(int(w)), str(int(h)),
                        "windowmove", str(window_id), str(int(x)), str(int(y)),
                    ],
                    capture_output=True, timeout=1,
                )
            else:
                subprocess.run(
                    ["xdotool", "windowmove", str(window_id), str(int(x)), str(int(y))],
                    capture_output=True, timeout=1,
                )
        except Exception:
            pass
        return

    if PLATFORM == "Darwin":
        try:
            script = (
                f'tell application "System Events"\n'
                f'    set position of first window of '
                f'(first process whose frontmost is true) to {{{int(x)}, {int(y)}}}\n'
                f'end tell'
            )
            subprocess.run(["osascript", "-e", script], capture_output=True, timeout=1)
        except Exception:
            pass
        return

    if PLATFORM == "Windows":
        try:
            import ctypes
            ctypes.windll.user32.SetWindowPos(window_id, 0, int(x), int(y), 0, 0, 0x0001)
        except Exception:
            pass
