# Copyright (C) 2024 Ymsniper
# SPDX-License-Identifier: GPL-3.0-or-later
"""
ImageOverlay — physics-warped full-terminal image renderer.

Architecture (combines best of both prototype versions)
-------------------------------------------------------
Zero curses calls.  Everything — image pixels AND status bar — is assembled
into a single bytearray and emitted in one os.write() to the TTY fd.
This is the only reliable way to avoid flickering: ncurses wgetch() can
call wrefresh() internally even in nodelay mode on some ncurses builds,
which would repaint the screen with its own virtual-screen model and erase
our image pixels between our own renders.

Rendering pipeline
------------------
1.  LANCZOS FILL base — on first render (or terminal resize) the source
    image is resized with LANCZOS to *exactly* (px_w × px_h) — the full
    pixel canvas — with no letterbox bars.  This one-time cost is paid
    infrequently and gives a sharp, high-quality foundation.

2.  NUMPY bilinear warp — every frame the base pixels are remapped via
    the live Yaris scale_y / scale_x values using bilinear interpolation
    over four corner samples, all in vectorised numpy.
    This is a TRUE FULL-SCREEN warp — ZERO black bars at any scale value.
    The image always fills every pixel of the canvas:
      scale_y < 1  → rows cluster toward centre (squash) — blended so
                     every source row contributes; no aliasing or line
                     skipping even at extreme squash (sy ≈ 0.64).
      scale_y > 1  → rows fan out from centre (stretch) — centre content
                     zooms / pans to fill the canvas edge-to-edge.
    Same logic on the column axis for scale_x.
    Cost: ~3 ms for a 200×100 px canvas vs ~0.4 ms for nearest-neighbor;
    both well within the 40 ms / 25 fps frame budget.
    No PIL round-trip, no bytearray copy, no Python loop over pixels.

3.  Half-block encoding — each terminal cell maps two pixel rows:
      top pixel  → ANSI 24-bit background colour   (▄ upper half)
      bottom pixel → ANSI 24-bit foreground colour  (▄ glyph)
    2× vertical pixel resolution at no column cost.  Works in any
    truecolour terminal (alacritty, kitty, foot, xterm-256color, …).

4.  Run-length colour optimisation — ANSI colour codes are only emitted
    when a component value actually changes.  For photographic images
    ~55–75 % of cells skip at least one code; for near-solid regions it
    approaches 100 % skip.  This roughly halves the output bytes vs.
    emitting codes unconditionally.

5.  Pre-computed decimal lookup — _B[0..255] maps each byte value to its
    ASCII-encoded bytes ('0' … '255'), eliminating str().encode() calls
    in the inner loop.  Row data is fetched via numpy .tolist() which
    converts a (W, 3) uint8 array to a plain Python list 10–50× faster
    than per-element numpy indexing.

6.  Synchronized Output (DEC mode 2026) — every frame is bracketed with
    BSU (\\x1b[?2026h) / ESU (\\x1b[?2026l).  Supported terminals buffer
    the entire frame and flip it atomically, eliminating all tearing.
    Terminals that do not support mode 2026 silently ignore the sequences.
    Supported: kitty, alacritty, foot, wezterm, Ghostty, Konsole, iTerm2,
    Windows Terminal, xterm (≥ 379), tmux (≥ 3.5).

7.  Single atomic os.write() — the entire frame (BSU + image + status bar
    + ESU) is assembled into one bytearray and sent in a single syscall,
    minimising partial-frame tearing from the terminal emulator.

8.  Beat flash — on_beat() brightens the image for ~10 frames, synced to
    the detected audio beat.

9.  Accurate terminal size — callers should pass (cols, rows) from
    os.get_terminal_size(tty_fd) rather than curses.getmaxyx() when in
    image mode; the latter can lag behind real size during window dancing.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional, Tuple

# ── Pre-computed decimal byte lookup (module-level; zero cost after import) ──
_B = [str(i).encode("ascii") for i in range(256)]

_LOWER_HALF = "▄".encode("utf-8")   # U+2584  LOWER HALF BLOCK

# Synchronized-output brackets (DEC private mode 2026).
# Terminals that don't support 2026 silently ignore these — safe to always send.
_BSU  = b"\x1b[?2026h"   # Begin Synchronized Update  — defer rendering
_ESU  = b"\x1b[?2026l"   # End   Synchronized Update  — flush atomically

_HOME    = b"\x1b[H"
_HIDE    = b"\x1b[?25l"
_SHOW    = b"\x1b[?25h"
_RESET   = b"\x1b[0m"
_DIM     = b"\x1b[2m"
_CLRLINE = b"\x1b[2K\r"
_NEWLINE = b"\r\n"


# ── Dependency flags ──────────────────────────────────────────────────────────
try:
    from PIL import Image as _PIL
    _PILLOW = True
except ImportError:
    _PILLOW = False

try:
    import numpy as _np
    _NUMPY = True
except ImportError:
    _NUMPY = False


try:
    from wcwidth import wcwidth as _wcwidth
except Exception:
    def _wcwidth(ch: str) -> int:
        # Conservative fallback: treat printable ASCII as width 1, everything
        # else as width 1 as well.  This keeps the renderer dependency-free.
        return 1 if ch and ord(ch) >= 32 else 0


def _clip_text_to_cols(text: str, max_cols: int) -> str:
    """Return *text* truncated to at most *max_cols* display cells."""
    if max_cols <= 0 or not text:
        return ""
    cols = 0
    out: list[str] = []
    for ch in text:
        w = _wcwidth(ch)
        if w < 0:
            w = 0
        if cols + w > max_cols:
            break
        out.append(ch)
        cols += w
    return "".join(out)


class ImageOverlay:
    """
    Full-terminal image renderer with Yaris-physics warp and beat flash.

    Typical lifecycle::

        overlay = ImageOverlay(tty_fd)
        ok, err = overlay.load(Path("cover.jpg"))
        # each 25-Hz frame:
        overlay.render(cols, rows, scale_y, scale_x, status_text)
        # on detected beat:
        overlay.on_beat()
        # when done:
        overlay.unload()

    Pass (cols, rows) from os.get_terminal_size(tty_fd) — NOT from
    curses.getmaxyx() — when in image mode.  curses only processes SIGWINCH
    during getch(), which is bypassed in image mode, so its size can be stale.
    """

    def __init__(self, tty_fd: int) -> None:
        self._fd   = tty_fd
        self._src: Optional[object] = None        # PIL Image RGB (original)
        self._path: Optional[Path]  = None

        # LANCZOS base: numpy uint8 (px_h, px_w, 3) for current terminal size.
        # Rebuilt only when terminal size changes.
        self._base:     Optional[object] = None   # np.ndarray
        self._base_key: Optional[Tuple]  = None   # (cols, rows)

        # Beat-flash brightness, 0.0–1.0; set to 1.0 on each beat, decays.
        self._flash: float = 0.0

        self.active: bool = False

    # ── Dependency check ──────────────────────────────────────────────────────

    @staticmethod
    def deps_ok() -> Tuple[bool, str]:
        if not _PILLOW:
            return False, "Pillow not installed — pip install Pillow --break-system-packages"
        if not _NUMPY:
            return False, "numpy not installed (unexpected — it ships with librosa)"
        return True, ""

    # ── Load / unload ─────────────────────────────────────────────────────────

    def load(self, path: Path) -> Tuple[bool, str]:
        """Load *path* as the overlay image.  Returns (True, '') or (False, reason)."""
        if not _PILLOW:
            return False, "Pillow not installed"
        try:
            img = _PIL.open(path).convert("RGB")
        except Exception as exc:
            return False, str(exc)
        self._src      = img
        self._path     = path
        self._base     = None    # invalidate cached base
        self._base_key = None
        self._flash    = 0.0
        self.active    = True
        return True, ""

    def unload(self) -> None:
        """Deactivate overlay and free image data."""
        self._src      = None
        self._path     = None
        self._base     = None
        self._base_key = None
        self.active    = False

    def on_beat(self) -> None:
        """Trigger a brief brightness flash synced to the detected beat."""
        self._flash = 1.0

    def status_label(self) -> str:
        return self._path.name if self._path else ""

    # ── Internal: LANCZOS FILL base ───────────────────────────────────────────

    def _ensure_base(self, cols: int, rows: int) -> None:
        """
        Resize source → base array (LANCZOS FILL, cached per terminal size).

        FILL (not letterbox): the source is stretched to EXACTLY (px_w × px_h).
        Every pixel is used; no black bars.  This gives maximum pixel density
        at any terminal size and a dramatic warp foundation.

        The half-block grid pixels are square (each occupies half a character
        cell height × one character width, and typical fonts make these equal
        in display size), so FILL produces no inherent distortion beyond what
        the terminal's own aspect ratio imposes — which is intentional, since
        the window itself is dancing into the same shape.
        """
        if cols <= 0 or rows <= 0:
            return
        key = (cols, rows)
        if self._base_key == key and self._base is not None:
            return

        px_w = cols
        px_h = rows * 2   # half-block: 2 pixel rows per terminal row

        # FILL: stretch to exact canvas size — no bars, maximum coverage
        fitted         = self._src.resize((px_w, px_h), _PIL.LANCZOS)  # type: ignore[union-attr]
        self._base     = _np.array(fitted, dtype=_np.uint8)             # type: ignore[union-attr]
        self._base_key = key

    # ── Full-screen numpy warp (bilinear) ────────────────────────────────────

    @staticmethod
    def _numpy_warp(
        base: "_np.ndarray",
        sy: float,
        sx: float,
    ) -> "_np.ndarray":
        """
        Always-full-screen bilinear warp via numpy advanced indexing.

        No PIL round-trip.  No black bars ever — the canvas is always 100%
        filled regardless of scale values.

        Why bilinear (not nearest-neighbor)
        ------------------------------------
        With nearest-neighbor, each output row maps to a single source row
        via integer floor.  At Yaris squash values (sy ≈ 0.64) consecutive
        output rows jump by ~1.56 source rows per step, so ~35% of source
        rows are SKIPPED entirely — they simply never appear in the output.
        Adjacent output rows can jump from source row N to source row N+2,
        skipping row N+1 completely.  This produces the visible horizontal
        "chopped lines" artefact during the squash/stretch cycle.

        Bilinear samples the four nearest source pixels and blends them by
        the fractional distance, so every source row contributes to the
        output proportionally.  The skip-and-jump artefact disappears.

        Cost: ~3 ms for a 200×100 px canvas (nearest-neighbor: ~0.4 ms).
        Both are well within the 40 ms / 25 fps frame budget.

        Algorithm
        ---------
        For each output row y the floating-point source row is:
            src_y = cy + (y - cy) / sy          clamped to [0, ph-1]

        Four corner pixels (y0,x0), (y0,x1), (y1,x0), (y1,x1) are looked up
        where y0 = floor(src_y), y1 = min(y0+1, ph-1), and fractional weight
        ty = src_y - y0.  The bilinear blend is:
            out = (1-ty)(1-tx)·q00 + ty(1-tx)·q10
                + (1-ty)tx·q01    + ty·tx·q11

        All four gather ops and the blend use vectorised numpy — no Python
        loops over pixels.
        """
        ph, pw = base.shape[:2]
        cy = (ph - 1) / 2.0
        cx = (pw - 1) / 2.0

        # ── Float source coordinates, clamped to valid pixel range ───────────
        src_y = _np.clip(
            cy + (_np.arange(ph, dtype=_np.float32) - cy) / sy,
            0.0, ph - 1.0,
        )
        src_x = _np.clip(
            cx + (_np.arange(pw, dtype=_np.float32) - cx) / sx,
            0.0, pw - 1.0,
        )

        # ── Integer floor coordinates and one-step-up neighbours ─────────────
        y0 = src_y.astype(_np.int32)
        y1 = _np.minimum(y0 + 1, ph - 1)
        x0 = src_x.astype(_np.int32)
        x1 = _np.minimum(x0 + 1, pw - 1)

        # ── Fractional weights, shaped for broadcast over (ph, pw, 3) ────────
        ty = (src_y - y0).astype(_np.float32)[:, _np.newaxis, _np.newaxis]
        tx = (src_x - x0).astype(_np.float32)[_np.newaxis, :, _np.newaxis]

        # ── Four corner samples via advanced indexing — shape (ph, pw, 3) ────
        b   = base.astype(_np.float32)
        q00 = b[y0[:, _np.newaxis], x0[_np.newaxis, :]]   # top-left
        q10 = b[y1[:, _np.newaxis], x0[_np.newaxis, :]]   # bottom-left
        q01 = b[y0[:, _np.newaxis], x1[_np.newaxis, :]]   # top-right
        q11 = b[y1[:, _np.newaxis], x1[_np.newaxis, :]]   # bottom-right

        # ── Bilinear blend → back to uint8 ───────────────────────────────────
        return (
            (1.0 - ty) * (1.0 - tx) * q00
            + ty        * (1.0 - tx) * q10
            + (1.0 - ty) * tx        * q01
            + ty         * tx        * q11
        ).astype(_np.uint8)   # type: ignore[return-value]

    # ── Render ────────────────────────────────────────────────────────────────

    def render(
        self,
        term_cols:   int,
        term_rows:   int,
        scale_y:     float = 1.0,
        scale_x:     float = 1.0,
        status_text: str   = "",
    ) -> None:
        """
        Warp the image and write the complete screen frame to the TTY fd.

        Parameters
        ----------
        term_cols, term_rows
            Terminal dimensions from os.get_terminal_size(tty_fd).
            Do NOT use curses.getmaxyx() here — it lags in image mode.
        scale_y, scale_x
            Yaris physics scale values (1.0 = no warp).
            scale_y < 1  → TRUE SQUASH: rows bunch toward the centre.
                           Canvas stays fully filled — no black bands.
            scale_y > 1  → TRUE STRETCH: centre content fans out to edges.
                           Canvas stays fully filled — no clipping artifacts.
        status_text
            One-line string overlaid (dimmed) on the last terminal row.

        Frame is bracketed by BSU/ESU (DEC mode 2026) so supporting terminals
        flip the entire frame atomically — zero inter-row tearing.
        """
        if self._src is None or not _PILLOW or not _NUMPY:
            return
        if term_cols <= 0 or term_rows <= 0:
            return

        # Reserve one terminal row for the status line when possible.
        image_rows = term_rows - 1 if term_rows > 1 else 1

        self._ensure_base(term_cols, image_rows)
        if self._base is None:
            return

        px_h = image_rows * 2
        px_w = term_cols

        # ── Per-frame warp: numpy row+col remap — always fills canvas ─────────
        sy = max(0.20, min(float(scale_y), 3.00))
        sx = max(0.20, min(float(scale_x), 3.00))

        if abs(sy - 1.0) < 0.005 and abs(sx - 1.0) < 0.005:
            # No warp — use the cached base directly (zero extra cost)
            frame = self._base
        else:
            frame = self._numpy_warp(self._base, sy, sx)

        # ── Beat flash: brief brightness boost (decays over ~10 frames) ──────
        if self._flash > 0.02:
            boost = int(self._flash * 38)
            frame = _np.clip(                                              # type: ignore[union-attr]
                frame.astype(_np.int16) + boost, 0, 255                   # type: ignore[union-attr]
            ).astype(_np.uint8)                                            # type: ignore[union-attr]
            self._flash *= 0.68   # ~10 frames to reach 0.02 at 25 FPS
        else:
            self._flash = 0.0

        # ── Numpy stride views — even rows = background, odd = foreground ────
        top = frame[0::2]   # shape (image_rows, px_w, 3)
        bot = frame[1::2]   # shape (image_rows, px_w, 3)

        # ── Assemble ANSI output into one bytearray ───────────────────────────
        # Wrapped in BSU/ESU so the terminal renders atomically (no tearing).
        buf = bytearray()
        buf += _BSU    # ← Begin Synchronized Update (mode 2026)
        buf += _HOME
        buf += _HIDE

        for row_idx in range(image_rows):
            # Explicit cursor placement for each image row avoids any reliance
            # on newline/autowrap behavior while the window is moving.
            buf += f"\x1b[{row_idx + 1};1H".encode()

            # .tolist() converts (W, 3) numpy → Python list-of-lists.
            # This is 10–50× faster than per-element numpy integer indexing.
            t_row = top[row_idx].tolist()   # [[R,G,B], [R,G,B], …]
            b_row = bot[row_idx].tolist()

            parts: list[bytes] = []
            prev_tr = prev_tg = prev_tb = -1
            prev_br = prev_bg = prev_bb = -1

            for (tr, tg, tb), (br, bg, bb) in zip(t_row, b_row):
                # Only emit colour codes when a component actually changes
                if tr != prev_tr or tg != prev_tg or tb != prev_tb:
                    parts += [b"\x1b[48;2;", _B[tr], b";", _B[tg], b";", _B[tb], b"m"]
                    prev_tr, prev_tg, prev_tb = tr, tg, tb
                if br != prev_br or bg != prev_bg or bb != prev_bb:
                    parts += [b"\x1b[38;2;", _B[br], b";", _B[bg], b";", _B[bb], b"m"]
                    prev_br, prev_bg, prev_bb = br, bg, bb
                parts.append(_LOWER_HALF)

            buf += b"".join(parts)
            buf += _RESET

        # ── Status bar: reserved final row when possible ─────────────────────
        status_row = image_rows + 1 if term_rows > 1 else 1
        buf += f"\x1b[{status_row};1H".encode()
        buf += _CLRLINE
        buf += _DIM
        # Clip by display cells, not raw bytes, so multibyte text cannot split.
        # The visible budget keeps the status from wrapping into the image.
        visible = _clip_text_to_cols(status_text, max(0, px_w - 1))
        buf += visible.encode("utf-8", errors="replace")
        buf += _RESET

        buf += _ESU    # ← End Synchronized Update — terminal flips atomically

        # ── Single atomic write — minimises partial-frame tearing ─────────────
        try:
            os.write(self._fd, bytes(buf))
        except OSError:
            pass
