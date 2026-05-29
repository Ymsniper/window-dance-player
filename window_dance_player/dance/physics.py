# Copyright (C) 2024 Ymsniper
# SPDX-License-Identifier: GPL-3.0-or-later
"""
Yaris physics engine.

Simulates the Toyota Yaris lowrider bounce meme using an underdamped
spring + gravity system.

On every beat the window:
  1. Snaps to the floor instantly
  2. Gets an instantaneous squash (scale_y = 0.64)
  3. A spring kick (vel_scale_y = 3.0) causes the spring to overshoot from
     squash all the way past 1.0 into stretch territory — this is the
     key visual effect
  4. Gravity launches the window upward (vel_y = LAUNCH_VEL)
  5. The window falls back, lands, squashes on impact, and repeats

Critical constraint
-------------------
The scale spring MUST be underdamped:  SCALE_DAMP < 2 * sqrt(SCALE_SPRING)
If overdamped (e.g. SCALE_DAMP = 9 > 2*sqrt(18) = 8.49) the spring
only slowly returns to 1.0 — no overshoot, no stretch, no drama.

Current: 2 * sqrt(80) ≈ 17.9 > SCALE_DAMP = 5  → ζ ≈ 0.28 (bouncy)
"""

import math
import threading

from ..logger import DBG


class YarisPhysics:
    """Physics state machine for the Yaris bounce animation."""

    GRAVITY       = 1600.0   # px / s²
    LAUNCH_VEL    = -380.0   # px / s  (negative = up)
    HORIZ_IMPULSE =   30.0   # max random horizontal nudge per beat
    HORIZ_SPRING  =   12.0   # spring constant pulling back to base_x
    HORIZ_DAMP    =    5.0   # horizontal drag coefficient

    # Underdamped scale spring
    SCALE_SPRING  = 80.0
    SCALE_DAMP    =  5.0

    def __init__(
        self,
        base_x: float,
        base_y: float,
        sw: int,
        sh: int,
        ww: int,
        wh: int,
    ) -> None:
        self.base_x, self.base_y = base_x, base_y
        self.sw, self.sh         = sw, sh
        self.ww, self.wh         = ww, wh

        self.pos_x       = base_x
        self.pos_y       = base_y
        self.vel_x       = 0.0
        self.vel_y       = 0.0
        self.scale_y     = 1.0
        self.vel_scale_y = 0.0
        self._lock       = threading.Lock()

    def impulse(self) -> None:
        """
        Trigger a beat impulse.

        Snaps to the floor, applies instantaneous squash, kicks the spring
        toward stretch, and launches the window upward.  Designed so every
        beat looks identical regardless of where the window is mid-flight
        — critical for BPM > ~100 where in-flight beats are common.
        """
        import random
        with self._lock:
            self.pos_y       = self.base_y          # snap to floor
            self.scale_y     = 0.64                 # instant squash
            self.vel_scale_y = 3.0                  # kick spring toward stretch
            self.vel_y       = self.LAUNCH_VEL      # launch upward
            self.vel_x      += random.uniform(-self.HORIZ_IMPULSE, self.HORIZ_IMPULSE)
            self.vel_x       = max(-70.0, min(70.0, self.vel_x))

    def step(self, dt: float) -> "tuple[float, float, float]":
        """
        Advance physics by dt seconds using fixed sub-stepping.

        Sub-steps of ≤ 6 ms keep the integration stable and smooth even
        when the display loop runs at variable frame times.

        Returns
        -------
        (pos_x, pos_y, scale_y)
        """
        n_sub  = max(1, int(math.ceil(dt / 0.006)))
        sub_dt = dt / n_sub

        with self._lock:
            for _ in range(n_sub):
                # Horizontal spring-damper (returns to base_x)
                dx          = self.pos_x - self.base_x
                ax          = -self.HORIZ_SPRING * dx - self.HORIZ_DAMP * self.vel_x
                self.vel_x += ax * sub_dt
                self.pos_x += self.vel_x * sub_dt

                # Vertical: gravity
                self.vel_y += self.GRAVITY * sub_dt
                self.pos_y += self.vel_y * sub_dt

                # Hard floor + landing squash
                if self.pos_y >= self.base_y:
                    landing_vy       = self.vel_y
                    self.pos_y       = self.base_y
                    self.vel_y       = 0.0
                    if landing_vy > 150:
                        impact           = min(landing_vy / 700.0, 0.40)
                        self.scale_y     = max(0.55, 1.0 - impact)
                        self.vel_scale_y = 0.0

                # Scale spring-damper (underdamped → overshoots into stretch)
                acc_s = (
                    -self.SCALE_SPRING * (self.scale_y - 1.0)
                    - self.SCALE_DAMP  * self.vel_scale_y
                )
                self.vel_scale_y += acc_s * sub_dt
                self.scale_y     += self.vel_scale_y * sub_dt
                self.scale_y      = max(0.50, min(self.scale_y, 1.55))

            # Screen bounds (applied once after all substeps)
            self.pos_x = max(0.0, min(self.pos_x, float(self.sw - self.ww - 10)))
            self.pos_y = max(0.0, min(self.pos_y, float(self.sh - self.wh - 10)))
            return self.pos_x, self.pos_y, self.scale_y


def _sim_yaris_frames(beat_period_ms: float = 500.0, fps: float = 25.0) -> list:
    """
    Simulate one beat of Yaris physics offline and return sampled keyframes.

    Runs at 0.5 ms resolution (SIM_DT) for accurate integration, then
    samples at *fps* Hz for the KDE keyframe timer list.

    Used only on the KDE backend where a real-time physics thread is
    impractical (~175 ms KWin round-trips).

    Parameters
    ----------
    beat_period_ms  Duration to simulate (default: 500 ms = 120 BPM)
    fps             Sample rate for output keyframes (default: 25 Hz)

    Returns
    -------
    list of (delay_ms, up_px, scale_y)
        delay_ms  Milliseconds after the beat to fire this keyframe
        up_px     Pixels the window has risen above the floor (≥ 0)
        scale_y   Vertical scale factor (< 1 squash, > 1 stretch)
    """
    G  = YarisPhysics.GRAVITY
    LV = YarisPhysics.LAUNCH_VEL
    SS = YarisPhysics.SCALE_SPRING
    SD = YarisPhysics.SCALE_DAMP

    SIM_DT  = 0.0005       # 0.5 ms
    frame_s = 1.0 / fps

    pos_y = 0.0;  vel_y = LV
    sy    = 0.64; vsy   = 3.0

    frames: list = []
    t      = 0.0
    next_t = 0.0

    while t < beat_period_ms / 1000.0:
        if t >= next_t - 1e-9:
            frames.append((int(round(next_t * 1000)), max(0.0, -pos_y), sy))
            next_t += frame_s

        vel_y += G * SIM_DT
        pos_y += vel_y * SIM_DT

        if pos_y >= 0.0:
            lv    = vel_y
            pos_y = 0.0
            vel_y = 0.0
            if lv > 150:
                impact = min(lv / 700.0, 0.40)
                sy     = max(0.55, 1.0 - impact)
                vsy    = 0.0

        a    = -SS * (sy - 1.0) - SD * vsy
        vsy += a   * SIM_DT
        sy  += vsy * SIM_DT
        sy   = max(0.50, min(sy, 1.55))

        t += SIM_DT

    return frames
