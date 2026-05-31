# Copyright (C) 2024 Ymsniper
# SPDX-License-Identifier: GPL-3.0-or-later
"""
Player — manages audio playback, beat detection, and WindowDancer lifecycle.

Threading model
---------------
Main thread      pygame audio + curses UI (tui loop)
_analyze thread  librosa beat detection (can take several seconds)
_watch thread    polls playback position, fires dancer.on_beat() at the right time

The beat-watcher thread fires beats slightly early (the _lead offset) to
compensate for IPC round-trip latency and compositor frame pipeline delay,
so the visual effect aligns perceptually with the audio beat.
"""

import threading
import time

from .audio.analysis   import analyze_audio
from .dance.dancer     import WindowDancer
from .platform.window  import get_window_geometry, get_scale_factor, move_window
from .platform.detect  import COMPOSITOR, SESSION_TYPE
from .logger           import DBG, ERR


def _optimal_dance_size(lsw: int, lsh: int) -> "tuple[int, int]":
    """
    Return (width, height) — the largest terminal window that lets every
    built-in dance pattern sweep the full screen without any part of the
    window ever exiting the display.

    The binding constraint is the circle / figure-8 orbit radius
        r = min(lsw, lsh) * 0.28
    For a window centred at screen-centre the furthest its right edge
    ever reaches is  lsw/2 + ww/2 + r.  Keeping that ≤ lsw gives:
        ww ≤ lsw − 2r       (same logic applies to height)

    A small safety margin is subtracted so the window never even grazes
    the edge, regardless of integer-rounding in the position clamp.
    """
    MARGIN = 24          # px safety pad on each side
    r   = min(lsw, lsh) * 0.28
    ww  = max(320, int(lsw - 2 * r) - MARGIN)
    wh  = max(180, int(lsh - 2 * r) - MARGIN)
    return ww, wh


class Player:
    """Audio player with beat-synchronised window dancing."""

    def __init__(self, files: list) -> None:
        self.files = list(files)
        self.idx   = 0

        self.playing        = False
        self.paused         = False
        self.dance_on       = True

        self._stop_evt     = threading.Event()
        self._start_time   = 0.0
        self._pause_offset = 0.0

        self.duration   = 0.0
        self.bpm        = 0.0
        self.beat_times = []

        self.analyzing           = False
        self.error: "str | None" = None

        self.dancer: "WindowDancer | None" = None
        self.last_beat_time                = 0.0

        import pygame
        pygame.mixer.pre_init(44100, -16, 2, 1024)
        pygame.mixer.init()

    # ── Helpers ───────────────────────────────────────────────────────────────

    @property
    def current_file(self):
        """The Path of the currently loaded track, or None."""
        if self.files:
            return self.files[self.idx % len(self.files)]
        return None

    def position(self) -> float:
        """Current playback position in seconds."""
        if self.paused:
            return self._pause_offset
        if not self.playing:
            return 0.0
        return time.time() - self._start_time

    @property
    def yaris_scale(self) -> "tuple[float, float]":
        """
        Live ``(scale_y, scale_x)`` from Yaris physics, forwarded from the dancer.

        Returns ``(1.0, 1.0)`` when no dancer is active or pattern ≠ yaris.
        Callers (e.g. the image overlay) use this to warp the image in sync
        with the window squash / bounce animation.
        """
        d = self.dancer
        if d is not None:
            return d.yaris_scale
        return 1.0, 1.0

    # ── Playback control ──────────────────────────────────────────────────────

    def play(self, window_id, screen_w: int, screen_h: int) -> None:
        """
        Load the current track, start pygame playback, and launch
        the analysis + beat-watcher daemon threads.
        """
        import pygame

        self._stop_evt.clear()
        self.playing   = True
        self.paused    = False
        self.analyzing = True
        self.error     = None
        self.beat_times = []
        self.bpm       = 0.0
        self.duration  = 0.0

        f = self.current_file
        if not f:
            self.playing = False
            return

        try:
            pygame.mixer.music.load(str(f))
            pygame.mixer.music.play()
            self._start_time = time.time()
        except Exception as e:
            self.error   = str(e)
            self.playing = False
            return

        # ── Beat analysis thread ──────────────────────────────────────────────
        def _analyze():
            try:
                dur, bpm, beats = analyze_audio(f)
                self.duration   = dur
                self.bpm        = bpm
                self.beat_times = beats
            except Exception as e:
                self.error = f"Analysis failed: {e}"
            finally:
                self.analyzing = False

        threading.Thread(target=_analyze, daemon=True).start()

        # ── Beat watcher thread ───────────────────────────────────────────────
        def _watch():
            # Wait for analysis to finish before setting up the dancer
            while self.analyzing and not self._stop_evt.is_set():
                time.sleep(0.02)

            if self._stop_evt.is_set() or self.error:
                return

            # ── Detect display scale (KDE Wayland: physical ÷ scale = logical) ──
            scale = get_scale_factor()
            lsw   = int(screen_w / scale) if scale > 0.1 else screen_w
            lsh   = int(screen_h / scale) if scale > 0.1 else screen_h

            # ── Auto-resize window to the perfect dance size ───────────────────
            # Calculates the largest window that lets every pattern sweep
            # the whole screen without any part exiting the display edge.
            dance_ww, dance_wh = _optimal_dance_size(lsw, lsh)
            dance_cx = max(0, (lsw - dance_ww) // 2)
            dance_cy = max(0, (lsh - dance_wh) // 2)
            move_window(window_id, dance_cx, dance_cy, dance_ww, dance_wh)
            time.sleep(0.4)   # wait for compositor to apply the resize

            # Re-read actual geometry; use computed values if KDE falls back
            # to its (100,100,800,500) default (KDE geometry queries need
            # xdotool which isn't available under pure Wayland).
            _, _, cur_ww, cur_wh = get_window_geometry(window_id)
            if cur_ww == 800 and cur_wh == 500:
                # Almost certainly the fallback — use our computed size
                ww, wh = dance_ww, dance_wh
            else:
                ww, wh = cur_ww, cur_wh

            self.dancer = WindowDancer(
                window_id, screen_w, screen_h, ww, wh, scale=scale
            )
            self.dancer.enabled = self.dance_on

            beat_idx = 0
            while not self._stop_evt.is_set():
                if self.paused:
                    time.sleep(0.02)
                    continue

                pos = self.position()

                # Lead time compensates for IPC latency + compositor pipeline.
                # Without it the visual squash arrives noticeably after the beat.
                if COMPOSITOR == "kde":
                    _lead = 0.175   # KWin scripting ≈ 175 ms round-trip
                elif SESSION_TYPE == "x11":
                    _lead = 0.045   # xdotool subprocess + X11 compositing
                elif COMPOSITOR in ("hyprland", "sway"):
                    _lead = 0.025   # fast socket + one Wayland frame (~16 ms)
                else:
                    _lead = 0.020

                while (
                    beat_idx < len(self.beat_times)
                    and pos + _lead >= self.beat_times[beat_idx]
                ):
                    self.dancer.on_beat()
                    self.last_beat_time = time.time()
                    beat_idx += 1

                if not pygame.mixer.music.get_busy() and not self.paused:
                    self.playing = False
                    break

                time.sleep(0.002)   # 2 ms poll — tight beat timing, low jitter

        threading.Thread(target=_watch, daemon=True).start()

    def toggle_pause(self) -> None:
        import pygame
        if self.paused:
            self.paused = False
            pygame.mixer.music.unpause()
            self._start_time = time.time() - self._pause_offset
        else:
            self._pause_offset = self.position()
            self.paused = True
            pygame.mixer.music.pause()

    def stop(self) -> None:
        import pygame
        self._stop_evt.set()
        self.playing = False
        pygame.mixer.music.stop()
        if self.dancer:
            self.dancer.stop()
            self.dancer = None

    def next(self, window_id, sw: int, sh: int) -> None:
        self.stop()
        self.idx = (self.idx + 1) % len(self.files)
        time.sleep(0.08)
        self.play(window_id, sw, sh)

    def prev(self, window_id, sw: int, sh: int) -> None:
        self.stop()
        self.idx = (self.idx - 1) % len(self.files)
        time.sleep(0.08)
        self.play(window_id, sw, sh)

    def toggle_dance(self) -> None:
        self.dance_on = not self.dance_on
        if self.dancer:
            self.dancer.enabled = self.dance_on
