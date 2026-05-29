# Copyright (C) 2024 Ymsniper
# SPDX-License-Identifier: GPL-3.0-or-later
"""
Platform and compositor detection — evaluated once at import time.

Exported constants
------------------
PLATFORM        "Linux" | "Darwin" | "Windows"
SESSION_TYPE    "x11"   | "wayland"
COMPOSITOR      ""      | "hyprland" | "sway" | "kde" | "gnome" | "unknown"
"""

import json
import os
import platform
import subprocess

from ..logger import DBG

# ── Core constants ────────────────────────────────────────────────────────────
PLATFORM     = platform.system()
SESSION_TYPE = os.environ.get("XDG_SESSION_TYPE", "x11").lower()


def _detect_compositor() -> str:
    """Identify the running Wayland compositor from environment variables."""
    if SESSION_TYPE != "wayland":
        return ""
    if os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"):
        return "hyprland"
    if os.environ.get("SWAYSOCK"):
        return "sway"
    desktop = os.environ.get("XDG_CURRENT_DESKTOP", "").lower()
    if "kde" in desktop or "plasma" in desktop:
        return "kde"
    if "gnome" in desktop:
        return "gnome"
    return "unknown"


COMPOSITOR = _detect_compositor()

# ── Terminal PID ──────────────────────────────────────────────────────────────

def get_terminal_pid() -> int:
    """
    Walk the process tree to find the PID of the enclosing terminal emulator.

    Uses /proc directly — no psutil dependency required.
    Returns 0 if the terminal cannot be identified.
    """
    known = {
        "konsole", "kitty", "alacritty", "wezterm", "foot",
        "xterm", "gnome-terminal", "tilix", "terminator", "urxvt", "st",
    }
    pid = os.getpid()
    for _ in range(10):
        try:
            with open(f"/proc/{pid}/status") as f:
                name = ppid = None
                for line in f:
                    if line.startswith("Name:"):
                        name = line.split()[1].lower()
                    elif line.startswith("PPid:"):
                        ppid = int(line.split()[1])
                if name in known:
                    return pid
                if ppid is None or ppid <= 1:
                    break
                pid = ppid
        except Exception:
            break
    return 0


# ── Display refresh rate ──────────────────────────────────────────────────────

_DISPLAY_HZ: int = 0   # cached after first call


def _detect_display_hz() -> int:
    """
    Return the primary display's refresh rate in Hz.

    Called once at startup; result is cached in _DISPLAY_HZ.
    Uses hyprctl / swaymsg / xrandr depending on the active compositor.
    """
    global _DISPLAY_HZ
    if _DISPLAY_HZ:
        return _DISPLAY_HZ

    hz = 60   # safe fallback
    try:
        if COMPOSITOR == "hyprland":
            r = subprocess.run(
                ["hyprctl", "monitors", "-j"],
                capture_output=True, text=True, timeout=2,
            )
            mons = json.loads(r.stdout)
            mon = (
                next((m for m in mons if m.get("focused")), None)
                or (mons[0] if mons else None)
            )
            if mon:
                hz = int(round(float(mon.get("refreshRate", 60))))

        elif COMPOSITOR == "sway":
            r = subprocess.run(
                ["swaymsg", "-t", "get_outputs"],
                capture_output=True, text=True, timeout=2,
            )
            for o in json.loads(r.stdout):
                if o.get("focused") and o.get("current_mode"):
                    # Sway reports refresh in mHz (60 Hz → 60000)
                    hz = int(round(o["current_mode"].get("refresh", 60000) / 1000))
                    break

        elif SESSION_TYPE == "x11":
            import re
            r = subprocess.run(
                ["xrandr", "--current"],
                capture_output=True, text=True, timeout=2,
            )
            for line in r.stdout.splitlines():
                if "*" in line:   # active mode is marked with *
                    m = re.search(r"(\d+\.\d+)\*", line)
                    if m:
                        hz = int(round(float(m.group(1))))
                        break

    except Exception as e:
        DBG(f"_detect_display_hz failed ({e}), using {hz} Hz")

    hz = max(30, min(hz, 360))   # sanity clamp
    _DISPLAY_HZ = hz
    DBG(f"display Hz detected: {hz}")
    return hz
