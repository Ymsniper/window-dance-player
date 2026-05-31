"""
image_warp.py — Full-terminal image display with Yaris physics warp.

Overview
--------
An optional user image is rendered into the lower portion of the terminal.
When Yaris mode is active the image is distorted in real-time using the same
scale_y / position values produced by YarisPhysics:

  scale_y < 1.0  → vertical squash  (rows sampled from a narrower band)
  scale_y > 1.0  → vertical stretch (rows sampled from a wider band)
  horizontal wave shear is added proportional to |scale_y - 1|

The render pipeline:
  1. PIL image loaded & pre-scaled to the display pixel grid (once).
  2. Per-frame: PIL LANCZOS resize (if squashing) then MESH wave transform.
  3. Half-block unicode (▀) with ANSI truecolor escapes → string rows.
  4. Rows written directly to the tty fd after curses.refresh() using
     ANSI cursor positioning (\x1b[row;colH) — this bypasses curses colour
     limits while staying in sync with the curses repaint cycle.

Terminal support
----------------
Half-block + ANSI truecolor works in virtually every modern terminal
(kitty, alacritty, wezterm, foot, konsole, gnome-terminal, iterm2, etc.).
Falls back gracefully — the ANSI sequence is just ignored on very old vt100s
and the worst case is garbage characters, never a crash.
"""
from __future__ import annotations

import io
import math
import os
import sys
import threading
from pathlib import Path
from typing import Optional

try:
    from PIL import Image
    import numpy as np
    _PIL_OK = True
except ImportError:
    _PIL_OK = False

# Default number of character rows the image occupies
IMG_CHAR_ROWS: int = 12

# Horizontal wave amplitude scale factor (pixels per unit scale deviation)
WAVE_SCALE: float = 18.0

# Mesh tile height in pixels (smaller = smoother wave, more CPU)
MESH_TILE: int = 4


class ImageWarp:
    """Renders a PIL image into the terminal using half-block unicode + ANSI colours.

    The image can be warped per-frame by setting scale_y (vertical squash/stretch)
    and enabling yaris_on (activates wave shear proportional to scale deviation).
    """

    def __init__(self) -> None:
        self._original: Optional[Image.Image] = None
        self._display:  Optional[Image.Image] = None
        self._path:     Optional[Path] = None
        self._lock = threading.Lock()
        self.scale_y: float = 1.0
        self.enabled: bool = True
        self.yaris_on: bool = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def load(self, path: str) -> None:
        """Load an image file.  Raises RuntimeError if Pillow not installed."""
        if not _PIL_OK:
            raise RuntimeError(
                "Pillow (PIL) is required for image display: pip install pillow"
            )
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(str(p))
        img = Image.open(p).convert("RGB")
        with self._lock:
            self._original = img
            self._display = None   # invalidate cached display size
            self._path = p

    def clear(self) -> None:
        """Unload the current image."""
        with self._lock:
            self._original = None
            self._display = None
            self._path = None

    @property
    def path(self) -> Optional[Path]:
        return self._path

    @property
    def loaded(self) -> bool:
        return self._original is not None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_display_image(self, char_cols: int, char_rows: int) -> Image.Image:
        """Return a PIL image sized exactly (char_cols, char_rows*2) px.

        The image is letter-boxed (centred on black) and cached; it is only
        rebuilt when the terminal size changes.
        """
        target_px_w = char_cols
        target_px_h = char_rows * 2          # 2 pixel rows per character row

        with self._lock:
            if self._original is None:
                raise RuntimeError("No image loaded")

            # Return cached version if size matches
            if (self._display is not None
                    and self._display.size == (target_px_w, target_px_h)):
                return self._display

            orig_w, orig_h = self._original.size
            scale = min(target_px_w / orig_w, target_px_h / orig_h)
            fit_w = max(1, int(orig_w * scale))
            fit_h = max(1, int(orig_h * scale))

            fitted = self._original.resize(
                (fit_w, fit_h), Image.Resampling.LANCZOS
            )
            canvas = Image.new("RGB", (target_px_w, target_px_h), (0, 0, 0))
            off_x = (target_px_w - fit_w) // 2
            off_y = (target_px_h - fit_h) // 2
            canvas.paste(fitted, (off_x, off_y))
            self._display = canvas
            return self._display

    @staticmethod
    def _mesh_warp(img: Image.Image, scale_y: float, wave_amp: float) -> Image.Image:
        """Apply vertical squeeze + horizontal wave to a PIL image.

        Anti-aliasing strategy
        ----------------------
        When scale_y < 1 (Yaris squash), a naive bilinear mesh transform
        undersamples the source: each output row only blends 2 adjacent
        source pixels while skipping the intermediate rows entirely, which
        produces visible "chopped" horizontal banding.

        Fix: for downscaling we first resize the image to the squeezed height
        using PIL's LANCZOS filter (which correctly integrates over the full
        source footprint for each output pixel), paste it centred on a black
        canvas, then apply the mesh transform solely for the wave shear with
        scale_y=1.0.  This completely eliminates the aliasing artefacts.

        For upscaling (scale_y > 1) bilinear is fine — no source rows are
        skipped — so we pass through the existing mesh path unchanged.
        """
        w, h = img.size
        cy = h / 2.0

        # --- Anti-aliasing pre-scale for downscaling ---
        if scale_y < 0.98:
            new_h = max(1, int(round(h * scale_y)))
            # LANCZOS integrates over the full (1/scale_y)-pixel source
            # footprint for each output pixel → no source rows skipped.
            scaled = img.resize((w, new_h), Image.Resampling.LANCZOS)
            canvas = Image.new("RGB", (w, h), (0, 0, 0))
            canvas.paste(scaled, (0, (h - new_h) // 2))
            img = canvas
            # Mesh now only needs to apply the wave (no vertical scaling).
            mesh_sy = 1.0
        else:
            mesh_sy = scale_y

        # --- Build mesh for wave + (optional) upscale ---
        mesh = []
        for ty in range(0, h, MESH_TILE):
            by = min(ty + MESH_TILE, h)

            src_ty = cy + (ty - cy) / mesh_sy
            src_by = cy + (by - cy) / mesh_sy
            src_ty = max(0.0, min(float(src_ty), h - 1))
            src_by = max(0.0, min(float(src_by), h - 1))

            # Horizontal wave offset (pixels)
            wx = wave_amp * math.sin(math.pi * ty / max(h - 1, 1) * 3.0)

            mesh.append((
                (0,  ty, w,    by),               # destination quad
                (wx, src_ty,  w + wx, src_ty,     # source quad (4 corners)
                 w + wx, src_by, wx,  src_by),
            ))

        return img.transform(
            img.size,
            Image.Transform.MESH,
            mesh,
            Image.Resampling.BILINEAR,
        )

    @staticmethod
    def _to_halfblock(img: Image.Image) -> list[str]:
        """Convert a PIL RGB image to a list of ANSI half-block unicode rows.

        Each character row encodes two pixel rows using the ▀ glyph:
          foreground = top pixel colour (▀ upper half)
          background = bottom pixel colour (▀ lower half)
        """
        w, h = img.size
        if h % 2:
            h -= 1           # must be even

        arr = np.array(img)[:h]
        rows: list[str] = []
        for y in range(0, h, 2):
            top = arr[y]
            bot = arr[y + 1]
            parts = [
                "\x1b[38;2;{};{};{}m\x1b[48;2;{};{};{}m\u2580".format(
                    top[x, 0], top[x, 1], top[x, 2],
                    bot[x, 0], bot[x, 1], bot[x, 2],
                )
                for x in range(w)
            ]
            rows.append("".join(parts) + "\x1b[0m")
        return rows

    # ------------------------------------------------------------------
    # Main render entry point
    # ------------------------------------------------------------------

    def render_frame(
        self,
        term_w: int,
        term_h: int,
        img_char_rows: int,
    ) -> list[str]:
        """Return a list of ANSI-encoded terminal rows for the current frame.

        Returns [] if no image is loaded, PIL is missing, or the terminal is
        too small.  Never raises — exceptions are caught and return [].
        """
        if not (self.loaded and _PIL_OK):
            return []

        img_char_rows = min(img_char_rows, max(4, term_h // 3))
        if term_w < 20 or term_h < 10:
            return []

        try:
            base = self._get_display_image(term_w, img_char_rows)

            sy = max(0.4, min(self.scale_y, 2.0))
            wave_amp = abs(sy - 1.0) * WAVE_SCALE if self.yaris_on else 0.0
            apply_warp = self.yaris_on and abs(sy - 1.0) > 0.02

            warped = self._mesh_warp(base, sy, wave_amp) if apply_warp else base
            return self._to_halfblock(warped)
        except Exception:
            return []

    # ------------------------------------------------------------------
    # Terminal output
    # ------------------------------------------------------------------

    def write_at(self, rows: list[str], start_row: int) -> None:
        """Write rendered rows to stdout using ANSI cursor positioning.

        start_row is 0-based; rows[0] is written at terminal row start_row+1.
        """
        if not rows:
            return
        buf = io.StringIO()
        for i, row in enumerate(rows):
            buf.write(f"\x1b[{start_row + i + 1};1H{row}")
        buf.write("\x1b[0m")
        sys.stdout.write(buf.getvalue())
        sys.stdout.flush()
