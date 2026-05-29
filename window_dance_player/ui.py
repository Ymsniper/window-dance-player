# Copyright (C) 2024 Ymsniper
# SPDX-License-Identifier: GPL-3.0-or-later
"""
Terminal UI — curses-based interface for Window Dance Player.

Renders at ~25 FPS.  All drawing goes through safe_addstr() which silently
ignores curses errors caused by a terminal that is too small.
"""

import curses
import math
import time
from pathlib import Path

from .player import Player


# ── Formatting helpers ────────────────────────────────────────────────────────

def fmt_time(secs: float) -> str:
    """Format a duration in seconds as MM:SS."""
    secs = max(0, int(secs))
    return f"{secs // 60:02d}:{secs % 60:02d}"


def bar(ratio: float, width: int, full: str = "█", empty: str = "░") -> str:
    """Return a fixed-width progress bar string."""
    n = max(0, min(int(width * ratio), width))
    return full * n + empty * (width - n)


def safe_addstr(win, y: int, x: int, text: str, attr: int = 0) -> None:
    """
    curses addstr that silently ignores out-of-bounds / resize errors.

    Clips *text* to the available width so partial strings are drawn instead
    of raising curses.error.
    """
    h, w = win.getmaxyx()
    if y < 0 or y >= h or x < 0 or x >= w:
        return
    text = text[: w - x - 1]
    if not text:
        return
    try:
        win.addstr(y, x, text, attr)
    except curses.error:
        pass


# ── Main TUI loop ─────────────────────────────────────────────────────────────

def tui(stdscr, player: Player, window_id, sw: int, sh: int) -> None:
    """
    Curses main loop.

    Called via curses.wrapper() from main().  Starts playback immediately,
    then redraws at ~25 FPS and handles keyboard input.
    """
    curses.curs_set(0)
    stdscr.nodelay(True)
    curses.start_color()
    curses.use_default_colors()

    curses.init_pair(1, curses.COLOR_CYAN,    -1)
    curses.init_pair(2, curses.COLOR_GREEN,   -1)
    curses.init_pair(3, curses.COLOR_YELLOW,  -1)
    curses.init_pair(4, curses.COLOR_RED,     -1)
    curses.init_pair(5, curses.COLOR_MAGENTA, -1)
    curses.init_pair(6, curses.COLOR_WHITE,   -1)

    CYAN    = curses.color_pair(1) | curses.A_BOLD
    GREEN   = curses.color_pair(2) | curses.A_BOLD
    YELLOW  = curses.color_pair(3) | curses.A_BOLD
    RED     = curses.color_pair(4) | curses.A_BOLD
    MAGENTA = curses.color_pair(5) | curses.A_BOLD
    WHITE   = curses.color_pair(6)
    DIM     = curses.color_pair(6) | curses.A_DIM

    player.play(window_id, sw, sh)

    while True:
        stdscr.erase()
        H, W = stdscr.getmaxyx()

        # ── Header ───────────────────────────────────────────────────────────
        header = "♪  WINDOW DANCE PLAYER  ♪"
        safe_addstr(stdscr, 0, (W - len(header)) // 2, header, CYAN)
        safe_addstr(stdscr, 1, 0, "─" * W, WHITE)

        # ── Now playing ───────────────────────────────────────────────────────
        f    = player.current_file
        name = Path(f).stem if f else "—"
        safe_addstr(stdscr, 2, 0, f" ♫  {name}"[:W - 1], GREEN)

        # ── Status row ────────────────────────────────────────────────────────
        if player.analyzing:
            status, col = "⚙  Analyzing beats…", YELLOW
        elif player.error:
            status, col = f"✗  {player.error}", RED
        elif player.paused:
            status, col = "⏸  Paused", YELLOW
        elif player.playing:
            status, col = "▶  Playing", GREEN
        else:
            status, col = "⏹  Stopped", DIM
        safe_addstr(stdscr, 3, 1, status, col)

        if player.bpm > 0:
            safe_addstr(stdscr, 3, 22, f"  {player.bpm:.1f} BPM", MAGENTA)

        # ── Beat flash ────────────────────────────────────────────────────────
        beat_age    = time.time() - player.last_beat_time
        flash_chars = ["♥", "♦", "♣", "♠"]
        flash_char  = flash_chars[(player.dancer.beat_count if player.dancer else 0) % 4]
        if beat_age < 0.08:
            safe_addstr(stdscr, 3, 34, f"  {flash_char} BEAT!", RED)

        # ── Waveform-ish beat visualiser ──────────────────────────────────────
        if player.dancer and player.dancer.beat_count > 0 and H > 12:
            beat_fade = max(0.0, 1.0 - beat_age / 0.4)
            viz_w     = min(W - 4, 40)
            cols      = [
                beat_fade * (
                    0.5 + 0.5 * math.sin(
                        (i / viz_w) * math.pi * 2
                        + player.dancer.beat_count * 0.8
                    )
                )
                for i in range(viz_w)
            ]
            levels = " ▁▂▃▄▅▆▇█"
            viz    = "".join(levels[max(0, min(int(c * 8), 8))] for c in cols)
            safe_addstr(stdscr, 4, 2, viz, CYAN if beat_fade > 0.5 else DIM)

        # ── Progress bar ──────────────────────────────────────────────────────
        pos      = player.position()
        dur      = player.duration if player.duration > 0 else 1.0
        time_str = f" {fmt_time(pos)} / {fmt_time(dur)} "
        safe_addstr(stdscr, 6, 0, time_str, WHITE)
        bar_x = len(time_str)
        bar_w = W - bar_x - 1
        if bar_w > 4:
            safe_addstr(stdscr, 6, bar_x, bar(min(1.0, pos / dur), bar_w), GREEN)

        safe_addstr(stdscr, 7, 0, "─" * W, WHITE)

        # ── Dance info ────────────────────────────────────────────────────────
        dance_status = "ON " if player.dance_on else "OFF"
        dance_color  = GREEN if player.dance_on else DIM
        pname        = player.dancer.pattern_label if player.dancer else "—"
        beat_num     = player.dancer.beat_count    if player.dancer else 0
        safe_addstr(stdscr, 8, 1,  f"Window Dance [{dance_status}]", dance_color)
        safe_addstr(stdscr, 8, 22, f"Pattern: {pname}  beat #{beat_num}", MAGENTA)

        safe_addstr(stdscr, 9, 0, "─" * W, WHITE)

        # ── Playlist ──────────────────────────────────────────────────────────
        safe_addstr(stdscr, 10, 1, "PLAYLIST", CYAN)
        for i, fp in enumerate(player.files):
            row_y  = 11 + i
            if row_y >= H - 3:
                break
            is_cur = i == player.idx % len(player.files)
            marker = "► " if is_cur else "  "
            c      = GREEN if is_cur else WHITE
            entry  = f"{marker}{i + 1:2d}.  {Path(fp).stem}"
            safe_addstr(stdscr, row_y, 1, entry[:W - 2], c)

        # ── Controls bar ──────────────────────────────────────────────────────
        ctrl = (
            " [SPACE] Play/Pause  [N] Next  [P] Prev  "
            "[D] Dance  [1-6] Pattern (6=🚗YARIS)  [Q] Quit "
        )
        safe_addstr(stdscr, H - 2, 0, "─" * W, WHITE)
        safe_addstr(
            stdscr, H - 1, max(0, (W - len(ctrl)) // 2), ctrl[:W - 1], DIM
        )

        stdscr.refresh()

        # ── Keyboard input ────────────────────────────────────────────────────
        key = stdscr.getch()

        if key in (ord("q"), ord("Q")):
            player.stop()
            break

        elif key == ord(" "):
            if player.playing or player.paused:
                player.toggle_pause()

        elif key in (ord("n"), ord("N")):
            player.next(window_id, sw, sh)

        elif key in (ord("p"), ord("P")):
            player.prev(window_id, sw, sh)

        elif key in (ord("d"), ord("D")):
            player.toggle_dance()

        elif key in range(ord("1"), ord("7")):   # 1–6 → pattern select
            if player.dancer:
                player.dancer.set_pattern(key - ord("1"))

        # ── Auto-advance when track ends ──────────────────────────────────────
        if not player.playing and not player.analyzing and player.files:
            if len(player.files) > 1:
                player.next(window_id, sw, sh)

        time.sleep(0.04)   # ~25 FPS refresh
