# Copyright (C) 2024 Ymsniper
# SPDX-License-Identifier: GPL-3.0-or-later
"""
Entry point for Window Dance Player.

Handles argument parsing, dependency checks, platform init, and launches
the curses wrapper.  All heavy lifting is delegated to the sub-modules.
"""

import argparse
import curses
import os
import sys
import time
from pathlib import Path

from .logger           import DBG
from .platform         import detect as _det
from .platform         import hyprland as _hypr
from .platform         import x11 as _x11
from .platform.detect  import COMPOSITOR, PLATFORM, SESSION_TYPE
from .platform.window  import get_screen_size, get_window_id
from .player           import Player
from .ui               import tui


def check_deps() -> list:
    """Return a list of missing Python package names."""
    missing = []
    for pkg in ("librosa", "pygame", "numpy"):
        try:
            __import__(pkg)
        except ImportError:
            missing.append(pkg)
    return missing


def _compositor_label() -> str:
    """Human-readable description of the active window-control backend."""
    if SESSION_TYPE != "wayland":
        return "X11"
    labels = {
        "kde":      "KDE Wayland (KWin scripting)",
        "hyprland": "Hyprland (direct IPC socket)",
        "sway":     "Sway (i3-protocol IPC socket)",
        "gnome":    "GNOME Wayland (unsupported — dance disabled)",
        "unknown":  "Unknown Wayland compositor (dance disabled)",
    }
    return labels.get(COMPOSITOR, f"Wayland/{COMPOSITOR}")

def get_files(current_path,file_list) -> None:
    for file in current_path:
        file_path=Path(file)
        if(file_path.is_dir()):
            get_files(file_path.iterdir(),file_list)
        elif(file_path.exists()):
            file_list.append(file_path)

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Window Dance Player — moves your terminal window on the beat",
        epilog=(
            "Controls: SPACE play/pause · N next · P prev · "
            "D toggle dance · 1-6 pattern · Q quit"
        ),
    )
    parser.add_argument(
        "files",
        nargs="+",
        help="Audio files to play (MP3, WAV, FLAC, OGG, …)",
    )
    args = parser.parse_args()
    files=[]

    get_files(args.files,files)

    if not files:
        print("Error: no valid audio files found.")
        sys.exit(1)

    missing = check_deps()
    if missing:
        print(f"Missing packages: {', '.join(missing)}")
        print(f"  pip install {' '.join(missing)} --break-system-packages")
        print("  (CachyOS / Arch: sudo pacman -S python-pygame python-numpy,")
        print("   then: pip install librosa --break-system-packages)")
        sys.exit(1)

    # ── Startup diagnostics (go to wdp_debug.log) ────────────────────────────
    DBG("=== startup ===")
    DBG(f"PLATFORM={PLATFORM} SESSION_TYPE={SESSION_TYPE} COMPOSITOR={COMPOSITOR}")
    DBG(f"HYPRLAND_INSTANCE_SIGNATURE={os.environ.get('HYPRLAND_INSTANCE_SIGNATURE', '<not set>')!r}")
    DBG(f"XDG_RUNTIME_DIR={os.environ.get('XDG_RUNTIME_DIR', '<not set>')!r}")
    DBG(f"XDG_CURRENT_DESKTOP={os.environ.get('XDG_CURRENT_DESKTOP', '<not set>')!r}")

    print(f"Session : {_compositor_label()}")
    print("Getting window info…", end="", flush=True)

    window_id = get_window_id()
    sw, sh    = get_screen_size()

    if not window_id:
        print("\nWarning: could not detect window — dance disabled.")
        if SESSION_TYPE == "wayland":
            if COMPOSITOR == "gnome":
                print("GNOME Wayland does not support programmatic window moving without an extension.")
            elif COMPOSITOR == "unknown":
                print("Unknown compositor. Supported: KDE Plasma, Hyprland, Sway.")
        else:
            print("Linux X11: install xdotool → sudo pacman -S xdotool")
    else:
        print(f" OK  (backend={window_id}, screen={sw}×{sh})")

    # ── Warm up IPC paths and fast X11 handle ────────────────────────────────
    if COMPOSITOR == "hyprland":
        _hypr._init_hyprland_sock()
        DBG(f"Hyprland socket cached: {_hypr._HYPR_SOCK!r}")
    if SESSION_TYPE == "x11":
        _x11._init_x11_fast()

    # Tighten GIL switch interval so the physics thread gets CPU time more
    # reliably.  Default is 5 ms; 1 ms reduces frame-time jitter noticeably.
    sys.setswitchinterval(0.001)

    if SESSION_TYPE == "wayland" and (
        window_id in ("kde", "sway")
        or (isinstance(window_id, str) and window_id.startswith("hyprland"))
    ):
        print("⚠  Make sure your terminal window is FLOATING for dance to work.")
        if COMPOSITOR == "hyprland":
            print("   Hyprland: Super+V toggles float.")
        elif COMPOSITOR == "sway":
            print("   Sway: $mod+Shift+Space toggles float.")
        elif COMPOSITOR == "kde":
            print("   KDE: right-click titlebar → More Actions → Float.")

    time.sleep(0.8)   # give the user a moment to read before curses takes over

    player = Player(files)
    try:
        curses.wrapper(tui, player, window_id, sw, sh)
    except KeyboardInterrupt:
        player.stop()
    finally:
        try:
            import pygame
            pygame.mixer.quit()
        except Exception:
            pass
        print("\n🎵  Thanks for dancing!")


if __name__ == "__main__":
    main()
