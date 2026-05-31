# Copyright (C) 2024 Ymsniper
# SPDX-License-Identifier: GPL-3.0-or-later
"""
WindowDancer — orchestrates beat-driven window animation.

Two execution paths:

Non-KDE Yaris
    A persistent _physics_loop() thread runs at display refresh rate.
    It calls YarisPhysics.step() each frame and pushes the result to the
    compositor via the fastest available backend (Hyprland batch IPC,
    Sway i3-protocol socket, X11 ctypes, or generic move_window()).
    All compositor-specific handles are pre-cached before the hot loop
    to minimise per-frame overhead.

KDE Yaris
    KWin scripting is ~175 ms round-trip so a real-time loop is impractical.
    Instead _fire_yaris_keyframes() schedules ~12 threading.Timer callbacks
    per beat using physics keyframes pre-computed by _sim_yaris_frames().

All other patterns (circle, bounce, zigzag, figure8, random)
    Positions are computed in on_beat() and sent via move_window().
    Auto-rotation cycles through non-yaris patterns every 32 beats.
"""

import ctypes as _ct
import math
import random
import socket as _sock_mod
import struct as _struct
import threading
import time

from .patterns import PATTERN_LABELS, PATTERNS
from .physics  import YarisPhysics, _sim_yaris_frames
from ..platform        import detect as _det
from ..platform        import hyprland as _hypr
from ..platform        import sway as _sway
from ..platform        import x11 as _x11_mod
from ..platform.window import get_window_geometry, move_window
from ..logger          import DBG, WARN, ERR

COMPOSITOR   = _det.COMPOSITOR
SESSION_TYPE = _det.SESSION_TYPE


class WindowDancer:
    """Controls window position / size in sync with detected beats."""

    def __init__(
        self,
        window_id,
        screen_w: int,
        screen_h: int,
        win_w: int,
        win_h: int,
        scale: float = 1.0,
    ) -> None:
        self.window_id    = window_id
        self.sw, self.sh  = screen_w, screen_h
        self.ww, self.wh  = win_w, win_h
        self.beat_count   = 0
        self.pattern_idx  = 0
        self.pattern      = PATTERNS[0]
        self.enabled      = True
        self.angle        = 0.0

        # On KDE Wayland, xrandr returns physical pixels but KWin's
        # frameGeometry API uses logical pixels (physical ÷ scale).
        # All position calculations and bounds-clamping use these logical
        # dimensions so the window never exits the screen at any scale %.
        self.screen_scale = max(0.1, scale)
        self.lsw          = int(screen_w / self.screen_scale)
        self.lsh          = int(screen_h / self.screen_scale)

        # Yaris physics (non-KDE path)
        self._phys:        "YarisPhysics | None"  = None
        self._phys_stop    = threading.Event()
        self._phys_thread: "threading.Thread | None" = None

        # Yaris keyframe timers (KDE path)
        self._keyframe_timers: list = []

    # ── Physics FPS ───────────────────────────────────────────────────────────

    @staticmethod
    def _physics_fps() -> int:
        """
        Target frames-per-second for the physics / IPC loop.

        KDE caps at 4 FPS because KWin scripting round-trips are ~175 ms.
        All other backends (Hyprland socket, Sway socket, X11 ctypes) match
        the display refresh rate for pixel-perfect smooth motion.
        """
        if COMPOSITOR == "kde":
            return 4
        return _det._detect_display_hz()

    # ── Physics thread lifecycle ──────────────────────────────────────────────

    def _start_physics(self) -> None:
        self._stop_physics()
        x, y, _, _ = get_window_geometry(self.window_id)
        # get_window_geometry falls back to (100,100,800,500) for KDE Wayland.
        # If that fallback is detected, start the physics from screen centre
        # so the first bounce launches from a sensible position.
        if x == 100 and y == 100 and str(self.window_id).startswith("kde"):
            x = max(0, (self.lsw - self.ww) // 2)
            y = max(0, (self.lsh - self.wh) // 2)
        self._phys = YarisPhysics(
            base_x=float(x), base_y=float(y),
            sw=self.lsw, sh=self.lsh,   # logical screen bounds
            ww=self.ww,  wh=self.wh,
        )
        self._phys_stop.clear()
        self._phys_thread = threading.Thread(target=self._physics_loop, daemon=True)
        self._phys_thread.start()

    def _stop_physics(self) -> None:
        self._phys_stop.set()
        if self._phys_thread and self._phys_thread.is_alive():
            self._phys_thread.join(timeout=0.5)
        self._phys        = None
        self._phys_thread = None

    def _physics_loop(self) -> None:
        """
        High-frequency physics + IPC hot loop.

        Pre-caches all compositor-specific handles and format strings so the
        inner iteration has zero per-frame conditional overhead and no
        attribute lookups through module chains.
        """
        fps       = self._physics_fps()
        target_dt = 1.0 / fps
        prev_t    = time.perf_counter()
        _first    = True

        wid = self.window_id

        # ── Pre-cache compositor handles (resolved once before the loop) ──────

        # Hyprland: pre-extract address + build batch template as a string
        _hypr_addr = wid.split(":", 1)[1] if (wid and wid.startswith("hyprland:")) else ""
        _use_hypr  = bool(_hypr_addr and _hypr._HYPR_SOCK)
        if _use_hypr:
            _hsock = _hypr._HYPR_SOCK
            _htmpl = (
                f"[[BATCH]]dispatch resizewindowpixel exact {{w}} {{h}},"
                f"address:{_hypr_addr};"
                f"dispatch movewindowpixel exact {{x}} {{y}},"
                f"address:{_hypr_addr}"
            )

        # Sway: just need the socket path pre-cached
        _use_sway = not _use_hypr and COMPOSITOR == "sway" and bool(_sway._SWAY_SOCK)

        # X11 ctypes: pre-wrap Display* and Window as ctypes objects
        _x11_wid_int = 0
        if wid and not _use_hypr and not _use_sway and SESSION_TYPE == "x11":
            try:
                _x11_wid_int = int(wid)
            except ValueError:
                pass
        _use_x11 = bool(_x11_wid_int and _x11_mod._x11lib and _x11_mod._x11dpy)
        if _use_x11:
            _dpy_ptr = _ct.c_void_p(_x11_mod._x11dpy)
            _win_ul  = _ct.c_ulong(_x11_wid_int)

        # ── Hot loop ──────────────────────────────────────────────────────────
        while not self._phys_stop.is_set():
            frame_start = time.perf_counter()

            # Wall-clock dt so physics stays in sync even when IPC runs long.
            # Clamped to 2× target to absorb scheduling hiccups without a
            # sudden large jump in position.
            actual_dt = frame_start - prev_t
            dt        = min(actual_dt, target_dt * 2.0)
            prev_t    = frame_start

            if self._phys:
                px, py, sy = self._phys.step(dt)

                # Inverse horizontal scale: squash one axis, stretch the other
                sx = max(0.6, min(2.0 - sy, 1.4))
                cw = int(self._phys.ww * sx)
                ch = int(self._phys.wh * sy)

                # Anchor bottom-centre so the "floor" stays in place
                draw_x = int(px + (self._phys.ww - cw) / 2)
                draw_y = int(py + self._phys.wh - ch)

                if _first:
                    DBG(
                        f"physics first frame ({draw_x},{draw_y}) {cw}×{ch} "
                        f"fps={fps} wid={wid!r}"
                    )
                    _first = False

                # ── Inline IPC — zero function-call overhead in the hot path ──
                if _use_hypr:
                    _payload = _htmpl.format(w=cw, h=ch, x=draw_x, y=draw_y).encode()
                    try:
                        with _sock_mod.socket(
                            _sock_mod.AF_UNIX, _sock_mod.SOCK_STREAM
                        ) as _s:
                            _s.settimeout(target_dt * 3)
                            _s.connect(_hsock)
                            _s.sendall(_payload)
                    except Exception:
                        pass

                elif _use_sway:
                    _cmd  = (
                        f"[focused] resize set {cw} {ch}; "
                        f"[focused] move position {draw_x} {draw_y}"
                    ).encode()
                    _smsg = b"i3-ipc" + _struct.pack("<II", len(_cmd), 0) + _cmd
                    try:
                        with _sock_mod.socket(
                            _sock_mod.AF_UNIX, _sock_mod.SOCK_STREAM
                        ) as _s:
                            _s.settimeout(target_dt * 3)
                            _s.connect(_sway._SWAY_SOCK)
                            _s.sendall(_smsg)
                    except Exception:
                        pass

                elif _use_x11:
                    try:
                        _x11_mod._x11lib.XMoveResizeWindow(
                            _dpy_ptr, _win_ul,
                            _ct.c_int(draw_x), _ct.c_int(draw_y),
                            _ct.c_uint(cw),    _ct.c_uint(ch),
                        )
                        _x11_mod._x11lib.XFlush(_dpy_ptr)
                    except Exception:
                        pass

                else:
                    # Generic slow path (KDE keyframes, xdotool, etc.)
                    move_window(wid, draw_x, draw_y, cw, ch)

            elapsed   = time.perf_counter() - frame_start
            remaining = target_dt - elapsed
            if remaining > 0.0002:
                time.sleep(remaining)

    # ── Pattern label ─────────────────────────────────────────────────────────

    @property
    def pattern_label(self) -> str:
        return PATTERN_LABELS.get(self.pattern, self.pattern)

    # ── Live physics scale (used by image overlay) ────────────────────────────

    @property
    def yaris_scale(self) -> "tuple[float, float]":
        """
        Return the live ``(scale_y, scale_x)`` from the Yaris physics engine.

        Thread-safe: acquires ``_phys._lock`` for the read.

        Returns ``(1.0, 1.0)`` when:
        - the current pattern is not ``"yaris"``
        - the physics thread has not started yet
        - running on the KDE keyframe path (no real-time ``_phys`` object)
        """
        if self._phys is not None and self.pattern == "yaris":
            with self._phys._lock:
                sy = float(self._phys.scale_y)
            sx = max(0.6, min(2.0 - sy, 1.4))
            return sy, sx
        return 1.0, 1.0

    # ── Pattern selection ─────────────────────────────────────────────────────

    def set_pattern(self, idx: int) -> None:
        """Switch to pattern at *idx* (wraps around PATTERNS list)."""
        self.pattern_idx = idx % len(PATTERNS)
        new_pat = PATTERNS[self.pattern_idx]
        if new_pat != "yaris":
            self._stop_physics()
        self.pattern = new_pat
        self.angle   = 0.0
        if self.pattern == "yaris" and self.enabled and self.window_id:
            if COMPOSITOR != "kde":
                self._start_physics()

    # ── KDE Yaris keyframe animation ──────────────────────────────────────────

    def _cancel_keyframe_timers(self) -> None:
        for t in self._keyframe_timers:
            t.cancel()
        self._keyframe_timers = []

    def _fire_yaris_keyframes(self) -> None:
        """
        Schedule ~12 KDE move/resize calls for one beat via threading.Timer.

        Keyframe positions are derived from _sim_yaris_frames() so the
        shape closely mirrors the real-time physics on other backends.
        """
        self._cancel_keyframe_timers()
        wid    = self.window_id
        sw, sh = self.lsw, self.lsh   # logical screen bounds
        try:
            bx, by, bw, bh = get_window_geometry(wid)
        except Exception:
            bx, by, bw, bh = 100, 100, 800, 500

        _UP_SCALE = 4.0   # amplify physics travel to match meme amplitude
        frames = _sim_yaris_frames()

        def _frame(up_px: float, sy: float) -> None:
            sx = max(0.60, min(2.0 - sy, 1.55))
            sy = max(0.55, min(sy, 1.55))
            cw = int(bw * sx)
            ch = int(bh * sy)
            fx = int(bx + (bw - cw) / 2)
            fy = int(by + bh - ch - int(up_px * _UP_SCALE))
            fx = max(0, min(fx, sw - cw - 5))
            fy = max(0, min(fy, sh - ch - 5))
            move_window(wid, fx, fy, cw, ch)

        for delay_ms, up_px, sy in frames:
            t = threading.Timer(delay_ms / 1000.0, _frame, args=(up_px, sy))
            t.daemon = True
            t.start()
            self._keyframe_timers.append(t)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def stop(self) -> None:
        """Cancel all pending timers and stop the physics thread."""
        self._cancel_keyframe_timers()
        self._stop_physics()

    # ── Beat callback ─────────────────────────────────────────────────────────

    def on_beat(self) -> None:
        """
        Called by the Player beat-watcher on each detected beat.

        Dispatches to the active pattern handler.  Non-yaris patterns
        auto-rotate every 32 beats (yaris is excluded from auto-rotation).
        """
        if not self.enabled or not self.window_id:
            DBG(f"on_beat suppressed: enabled={self.enabled} window_id={self.window_id!r}")
            return

        self.beat_count += 1
        if self.beat_count <= 5 or self.beat_count % 20 == 0:
            DBG(f"on_beat #{self.beat_count} pattern={self.pattern}")

        # Auto-rotate non-yaris patterns every 32 beats
        if self.pattern != "yaris" and self.beat_count % 32 == 0:
            next_idx = (self.pattern_idx + 1) % len(PATTERNS)
            if PATTERNS[next_idx] == "yaris":   # skip yaris in auto-rotation
                next_idx = (next_idx + 1) % len(PATTERNS)
            self.set_pattern(next_idx)

        if self.pattern == "yaris":
            if COMPOSITOR == "kde":
                self._fire_yaris_keyframes()
            else:
                if self._phys is None and self.enabled and self.window_id:
                    self._start_physics()
                if self._phys:
                    self._phys.impulse()
            return

        x, y = self._next_position()
        # Clamp to logical screen bounds — on KDE Wayland the compositor
        # expects logical coords (physical ÷ scale), so we use lsw/lsh here.
        x = max(0, min(int(x), self.lsw - self.ww - 10))
        y = max(0, min(int(y), self.lsh - self.wh - 10))
        move_window(self.window_id, x, y)

    # ── Position calculators ──────────────────────────────────────────────────

    def _next_position(self) -> "tuple[float, float]":
        b         = self.beat_count
        sw, sh    = self.lsw, self.lsh   # logical screen dimensions
        ww, wh    = self.ww, self.wh
        cx        = sw / 2 - ww / 2
        cy        = sh / 2 - wh / 2
        r         = min(sw, sh) * 0.28

        if self.pattern == "circle":
            self.angle += math.pi / 4    # 45° per beat → full circle in 8 beats
            return cx + r * math.cos(self.angle), cy + r * math.sin(self.angle)

        if self.pattern == "bounce":
            corners = [
                (30,           30),
                (sw - ww - 30, 30),
                (sw - ww - 30, sh - wh - 30),
                (30,           sh - wh - 30),
            ]
            return corners[b % 4]

        if self.pattern == "zigzag":
            cols = 4
            rows = 3
            col  = b % cols
            row  = (b // cols) % rows
            return (
                col * (sw - ww) / (cols - 1),
                row * (sh - wh) / (rows - 1),
            )

        if self.pattern == "figure8":
            self.angle += 0.35
            t = self.angle
            return (
                cx + r * math.sin(t),
                cy + r * math.sin(2 * t) / 2,
            )

        if self.pattern == "random":
            return (
                random.randint(0, max(0, sw - ww)),
                random.randint(0, max(0, sh - wh)),
            )

        return cx, cy
