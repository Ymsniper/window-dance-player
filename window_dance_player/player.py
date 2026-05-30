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
from .platform.window  import get_window_geometry, get_scale_factor
from .platform.detect  import COMPOSITOR, SESSION_TYPE
from .logger           import DBG, ERR


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

            _, _, ww, wh = get_window_geometry(window_id)
            screen_scale =  get_scale_factor(window_id)
            self.dancer  = WindowDancer(window_id, screen_w, screen_h, ww, wh, screen_scale)
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
