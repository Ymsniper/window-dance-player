# Copyright (C) 2024 Ymsniper
# SPDX-License-Identifier: GPL-3.0-or-later
"""
Terminal UI — curses TUI + flicker-free image overlay mode.

Image mode design (why we do NOT use stdscr.getch() while the image is live)
-----------------------------------------------------------------------------
ncurses wgetch() can call wrefresh() / doupdate() internally before returning
a key code — even in nodelay mode, on certain ncurses builds and Python
versions.  If we let that happen while ANSI image pixels are on screen,
ncurses overwrites them with its own virtual-screen model every frame,
causing a full-screen flicker at 25 Hz.

Solution: when image mode is active we bypass ncurses for I/O entirely.

  Rendering  — overlay.render() writes the complete frame (image + status
               bar) as a single os.write() to tty_fd, bracketed by
               BSU/ESU (DEC mode 2026) so the terminal flips each frame
               atomically — zero row-by-row tearing during warp.

  Input      — select() + os.read() on stdin_fd.  curses has already put
               the terminal into cbreak/raw mode so single bytes arrive
               immediately.  We set O_NONBLOCK so the read never blocks,
               then restore the flag when returning to TUI.

  Terminal size — os.get_terminal_size(tty_fd) is used instead of
               stdscr.getmaxyx() while in image mode.  curses only updates
               its internal size when getch() processes a KEY_RESIZE event,
               which we bypass in image mode.  During window dancing the
               terminal can resize many times per second; using the ioctl
               directly gives the TRUE current dimensions every frame so
               the image always fills the full screen without squashing or
               cropping.

  Transition — entering image mode: os.write() hard-clear (\\x1b[2J]).
               leaving image mode: stdscr.clearok(True) forces ncurses to
               do a hard clear + full repaint on the next TUI refresh so no
               image artefacts remain.
"""

import curses
import fcntl
import math
import os
import select
import sys
import time
from pathlib import Path
from typing import Optional

from .image import ImageOverlay
from .player import Player


# ── Formatting helpers ────────────────────────────────────────────────────────

def fmt_time(secs: float) -> str:
    secs = max(0, int(secs))
    return f"{secs // 60:02d}:{secs % 60:02d}"


def bar(ratio: float, width: int, full: str = "█", empty: str = "░") -> str:
    n = max(0, min(int(width * ratio), width))
    return full * n + empty * (width - n)


def safe_addstr(win, y: int, x: int, text: str, attr: int = 0) -> None:
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


# ── Raw stdin I/O (image mode only — zero ncurses) ────────────────────────────

def _set_nonblocking(fd: int) -> int:
    """Set O_NONBLOCK on *fd*; return old flags for later restore."""
    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
    return flags


def _restore_flags(fd: int, old: int) -> None:
    fcntl.fcntl(fd, fcntl.F_SETFL, old)


def _read_key_raw(stdin_fd: int) -> int:
    """
    Non-blocking single-byte read from stdin.

    Returns the first byte as int, or -1 if nothing is waiting.
    Uses select() with a zero timeout — guaranteed non-blocking and
    completely independent of ncurses.
    """
    try:
        r, _, _ = select.select([stdin_fd], [], [], 0)
        if r:
            raw = os.read(stdin_fd, 8)
            if raw:
                return raw[0]
    except OSError:
        pass
    return -1


# ── Terminal size helpers ─────────────────────────────────────────────────────

def _get_term_size(tty_fd: int, stdscr) -> tuple[int, int]:
    """
    Return (rows, cols) — always the TRUE current terminal dimensions.

    In image mode we query the tty fd directly via ioctl (TIOCGWINSZ) so we
    get the real size even when the window has been resized between curses
    getch() calls (which is how window-dance mode works).

    Tries tty_fd first, then stdin (fd 0), then stdout (fd 1), then curses.
    TIOCGWINSZ works on any fd open on a terminal regardless of open mode.
    """
    for fd in (tty_fd, sys.stdin.fileno(), sys.stdout.fileno()):
        try:
            ts = os.get_terminal_size(fd)
            if ts.columns > 0 and ts.lines > 0:
                return ts.lines, ts.columns
        except OSError:
            continue
    return stdscr.getmaxyx()


# ── Inline path prompt (TUI mode only) ───────────────────────────────────────

def _prompt_image_path(stdscr, W: int, H: int, CYAN: int, WHITE: int) -> Optional[str]:
    """
    Character-by-character path prompt with horizontal scrolling.

    Blocking; uses curses getch() here because we are in TUI mode (safe).
    Returns the stripped path string, or None on Escape / empty entry.
    """
    curses.curs_set(1)
    stdscr.nodelay(False)

    prompt = " 🖼  Image path (ESC to cancel): "
    inp:    list[str] = []
    scroll: int       = 0

    while True:
        # Use the live size every redraw; during rapid mode switches the cached
        # H/W passed in from the caller can be briefly stale.
        live_h, live_w = stdscr.getmaxyx()
        if live_h <= 0 or live_w <= 0:
            try:
                stdscr.erase()
                stdscr.refresh()
            except curses.error:
                pass
            continue

        # Keep the prompt on the last usable rows, but never go negative.
        sep_row   = max(0, live_h - 2)
        prompt_row = max(0, live_h - 1)

        # Redraw prompt rows
        try:
            stdscr.move(sep_row, 0)
            stdscr.clrtoeol()
            if live_h >= 2:
                stdscr.move(prompt_row, 0)
                stdscr.clrtoeol()
        except curses.error:
            # If curses thinks the cursor is out of bounds, hard-clear and try
            # once more on the next loop iteration instead of crashing.
            try:
                stdscr.erase()
                stdscr.refresh()
            except curses.error:
                pass
            continue

        safe_addstr(stdscr, sep_row, 0, "─" * max(0, live_w - 1), WHITE)

        text_w   = max(4, live_w - len(prompt) - 2)
        full_inp = "".join(inp)
        if len(full_inp) - scroll >= text_w:
            scroll = len(full_inp) - text_w + 1
        scroll   = max(0, min(scroll, len(full_inp)))
        visible  = full_inp[scroll: scroll + text_w]

        safe_addstr(stdscr, prompt_row, 0, prompt, CYAN)
        safe_addstr(stdscr, prompt_row, len(prompt),
                    visible + " " * (text_w - len(visible)), WHITE)
        try:
            stdscr.move(prompt_row, len(prompt) + min(len(full_inp) - scroll, text_w))
        except curses.error:
            pass
        stdscr.refresh()

        ch = stdscr.getch()
        if ch == 27:                                # Escape
            inp = []; break
        elif ch in (10, 13):                        # Enter
            break
        elif ch in (curses.KEY_BACKSPACE, 127, 8):  # Backspace
            if inp:
                inp.pop()
                if scroll > 0:
                    scroll -= 1
        elif 32 <= ch < 127:                        # Printable ASCII
            inp.append(chr(ch))

    curses.curs_set(0)
    stdscr.nodelay(True)
    result = "".join(inp).strip()
    return result if result else None


# ── Status line for image mode ────────────────────────────────────────────────

def _image_status(player: Player, overlay: ImageOverlay, W: int) -> str:
    """One-line status string rendered as plain text in image mode."""
    f    = player.current_file
    name = Path(f).stem[:28] if f else "—"

    state = "⏸" if player.paused else ("▶" if player.playing else "⏹")
    bpm_s = f" {player.bpm:.0f}bpm" if player.bpm > 0 else ""
    pos_s = fmt_time(player.position())
    dur_s = fmt_time(player.duration) if player.duration > 0 else "--:--"
    img_s = f"  🖼 {overlay.status_label()}" if overlay.status_label() else ""

    left  = f" {state} {name}{bpm_s}  {pos_s}/{dur_s}{img_s}"
    right = " [I]=remove  [SPACE]=pause  [N/P]=skip  [D]=dance  [Q]=quit "
    pad   = max(0, W - len(left) - len(right) - 1)
    return (left + " " * pad + right)[: W - 1]


# ── Main TUI loop ─────────────────────────────────────────────────────────────

def tui(
    stdscr,
    player:        Player,
    window_id,
    sw:            int,
    sh:            int,
    initial_image: Optional[Path] = None,
) -> None:
    """
    Curses main loop.  Called via curses.wrapper() from main().

    Parameters
    ----------
    initial_image
        Pre-load this image on startup (from the ``--image`` CLI flag).
        None starts with the normal TUI.
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

    # Open /dev/tty directly — avoids any stdout buffering or pipe redirection
    try:
        tty_fd = os.open("/dev/tty", os.O_WRONLY | os.O_NOCTTY)
    except OSError:
        tty_fd = sys.stdout.fileno()   # fallback (e.g. macOS / restricted env)

    stdin_fd             = sys.stdin.fileno()
    _stdin_old_flags: Optional[int] = None   # saved when entering image mode

    # ── Overlay setup ─────────────────────────────────────────────────────────
    overlay           = ImageOverlay(tty_fd)
    overlay_msg       = ""
    overlay_msg_until = 0.0

    if initial_image is not None:
        ok_d, hint = ImageOverlay.deps_ok()
        if not ok_d:
            overlay_msg       = hint
            overlay_msg_until = time.time() + 8.0
        else:
            ok, err = overlay.load(initial_image)
            if not ok:
                overlay_msg       = f"🖼  {err}"
                overlay_msg_until = time.time() + 6.0

    player.play(window_id, sw, sh)

    # State: True while in image mode (no curses calls at all)
    _img_mode = False

    while True:
        # ── Terminal size ─────────────────────────────────────────────────────
        # In image mode: query the tty fd directly via ioctl — gives the TRUE
        # current size even during rapid window-dance resizes.  curses only
        # updates its size on KEY_RESIZE inside getch(), which we bypass.
        # In TUI mode: curses getmaxyx() is fine (getch() processes SIGWINCH).
        if overlay.active:
            H, W = _get_term_size(tty_fd, stdscr)
        else:
            H, W = stdscr.getmaxyx()

        # ══════════════════════════════════════════════════════════════════════
        # IMAGE MODE — zero ncurses calls, zero flickering
        # ══════════════════════════════════════════════════════════════════════
        if overlay.active:

            # ── Entering image mode ───────────────────────────────────────────
            if not _img_mode:
                _img_mode = True
                # Make stdin non-blocking so os.read() returns immediately
                _stdin_old_flags = _set_nonblocking(stdin_fd)
                # Hard-clear the screen so no TUI residue bleeds under the image
                try:
                    os.write(tty_fd, b"\x1b[2J\x1b[H\x1b[?25l")
                except OSError:
                    pass

            # ── Beat flash ────────────────────────────────────────────────────
            if time.time() - player.last_beat_time < 0.06:
                overlay.on_beat()

            # ── Render: full screen via ANSI, zero curses ─────────────────────
            # H, W come from os.get_terminal_size(tty_fd) above — always current.
            sy, sx = player.yaris_scale
            overlay.render(
                term_cols   = W,
                term_rows   = max(1, H),   # full window — status overlaid on last row
                scale_y     = sy,
                scale_x     = sx,
                status_text = _image_status(player, overlay, W),
            )

            # ── Input: raw non-blocking read — NO stdscr.getch() ─────────────
            key = _read_key_raw(stdin_fd)

        # ══════════════════════════════════════════════════════════════════════
        # TUI MODE — normal curses rendering
        # ══════════════════════════════════════════════════════════════════════
        else:

            # ── Leaving image mode ────────────────────────────────────────────
            if _img_mode:
                _img_mode = False
                if _stdin_old_flags is not None:
                    _restore_flags(stdin_fd, _stdin_old_flags)
                    _stdin_old_flags = None
                try:
                    os.write(tty_fd, b"\x1b[?25h")   # restore cursor
                except OSError:
                    pass
                # clearok forces ncurses to hard-clear + full redraw next refresh
                # (erases any image pixels that escaped the bottom of the frame)
                stdscr.clearok(True)

            stdscr.erase()

            # ── Header ────────────────────────────────────────────────────────
            header = "♪  WINDOW DANCE PLAYER  ♪"
            safe_addstr(stdscr, 0, (W - len(header)) // 2, header, CYAN)
            safe_addstr(stdscr, 1, 0, "─" * W, WHITE)

            # ── Now playing ───────────────────────────────────────────────────
            f    = player.current_file
            name = Path(f).stem if f else "—"
            safe_addstr(stdscr, 2, 0, f" ♫  {name}"[:W - 1], GREEN)

            # ── Status row ────────────────────────────────────────────────────
            if player.analyzing:
                status_s, sc = "⚙  Analyzing beats…", YELLOW
            elif player.error:
                status_s, sc = f"✗  {player.error}", RED
            elif player.paused:
                status_s, sc = "⏸  Paused", YELLOW
            elif player.playing:
                status_s, sc = "▶  Playing", GREEN
            else:
                status_s, sc = "⏹  Stopped", DIM
            safe_addstr(stdscr, 3, 1, status_s, sc)

            if player.bpm > 0:
                safe_addstr(stdscr, 3, 22, f"  {player.bpm:.1f} BPM", MAGENTA)

            # ── Beat flash ────────────────────────────────────────────────────
            beat_age    = time.time() - player.last_beat_time
            flash_chars = ["♥", "♦", "♣", "♠"]
            flash_char  = flash_chars[
                (player.dancer.beat_count if player.dancer else 0) % 4
            ]
            if beat_age < 0.08:
                safe_addstr(stdscr, 3, 34, f"  {flash_char} BEAT!", RED)

            # ── Waveform visualiser ───────────────────────────────────────────
            if player.dancer and player.dancer.beat_count > 0 and H > 12:
                beat_fade = max(0.0, 1.0 - beat_age / 0.4)
                viz_w     = min(W - 4, 40)
                cols_v    = [
                    beat_fade * (0.5 + 0.5 * math.sin(
                        (i / viz_w) * math.pi * 2 + player.dancer.beat_count * 0.8
                    ))
                    for i in range(viz_w)
                ]
                levels = " ▁▂▃▄▅▆▇█"
                viz    = "".join(levels[max(0, min(int(c * 8), 8))] for c in cols_v)
                safe_addstr(stdscr, 4, 2, viz, CYAN if beat_fade > 0.5 else DIM)

            # ── Yaris scale readout ────────────────────────────────────────────
            if player.dancer and player.dancer.pattern == "yaris":
                sy, _ = player.yaris_scale
                if abs(sy - 1.0) > 0.01:
                    sb = f" ⤢ scale {sy:+.3f}"
                    safe_addstr(stdscr, 4, W - len(sb) - 1, sb, MAGENTA)

            # ── Progress bar ──────────────────────────────────────────────────
            pos      = player.position()
            dur      = player.duration if player.duration > 0 else 1.0
            time_str = f" {fmt_time(pos)} / {fmt_time(dur)} "
            safe_addstr(stdscr, 6, 0, time_str, WHITE)
            bar_x = len(time_str)
            bar_w = W - bar_x - 1
            if bar_w > 4:
                safe_addstr(stdscr, 6, bar_x, bar(min(1.0, pos / dur), bar_w), GREEN)

            safe_addstr(stdscr, 7, 0, "─" * W, WHITE)

            # ── Dance info ────────────────────────────────────────────────────
            dance_status = "ON " if player.dance_on else "OFF"
            dance_color  = GREEN if player.dance_on else DIM
            pname  = player.dancer.pattern_label if player.dancer else "—"
            bcount = player.dancer.beat_count    if player.dancer else 0
            safe_addstr(stdscr, 8, 1,  f"Window Dance [{dance_status}]", dance_color)
            safe_addstr(stdscr, 8, 22, f"Pattern: {pname}  beat #{bcount}", MAGENTA)

            safe_addstr(stdscr, 9, 0, "─" * W, WHITE)

            # ── Overlay message (errors / confirmations) ───────────────────────
            if overlay_msg and time.time() < overlay_msg_until:
                safe_addstr(stdscr, 10, 1, overlay_msg[: W - 2], RED)
                pl_start = 11
            else:
                overlay_msg = ""
                pl_start    = 10

            # ── Playlist ──────────────────────────────────────────────────────
            if pl_start < H - 3:
                safe_addstr(stdscr, pl_start, 1, "PLAYLIST", CYAN)
            for i, fp in enumerate(player.files):
                row_y  = pl_start + 1 + i
                if row_y >= H - 3:
                    break
                is_cur = i == player.idx % len(player.files)
                marker = "► " if is_cur else "  "
                c      = GREEN if is_cur else WHITE
                entry  = f"{marker}{i + 1:2d}.  {Path(fp).stem}"
                safe_addstr(stdscr, row_y, 1, entry[: W - 2], c)

            # ── Controls bar ──────────────────────────────────────────────────
            img_hint = "🖼 Remove image" if overlay.active else "🖼 Add image"
            ctrl = (
                f" [SPACE] Play/Pause  [N] Next  [P] Prev  "
                f"[D] Dance  [1-6] Pattern (6=🚗YARIS)  [I] {img_hint}  [Q] Quit "
            )
            safe_addstr(stdscr, H - 2, 0, "─" * W, WHITE)
            safe_addstr(stdscr, H - 1, max(0, (W - len(ctrl)) // 2), ctrl[: W - 1], DIM)

            stdscr.refresh()

            # curses getch is safe here — we are in TUI mode, not image mode
            key = stdscr.getch()

        # ══════════════════════════════════════════════════════════════════════
        # Unified key handling for both modes
        # ══════════════════════════════════════════════════════════════════════

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

        elif key in range(ord("1"), ord("7")):
            if player.dancer:
                player.dancer.set_pattern(key - ord("1"))

        elif key in (ord("i"), ord("I")):
            if overlay.active:
                # Remove: unload image → _img_mode flips off on next iteration
                overlay.unload()
            else:
                # Add: show path prompt (we are in TUI mode, curses getch is fine)
                ok_d, hint = ImageOverlay.deps_ok()
                if not ok_d:
                    overlay_msg       = hint
                    overlay_msg_until = time.time() + 6.0
                else:
                    path_str = _prompt_image_path(stdscr, W, H, CYAN, WHITE)
                    if path_str:
                        p = Path(path_str).expanduser()
                        ok, err = overlay.load(p)
                        if not ok:
                            overlay_msg       = f"🖼  {err}"
                            overlay_msg_until = time.time() + 6.0

        # ── Auto-advance when track ends ──────────────────────────────────────
        if not player.playing and not player.analyzing and player.files:
            if len(player.files) > 1:
                player.next(window_id, sw, sh)

        time.sleep(0.04)   # ~25 FPS
