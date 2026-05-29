# Copyright (C) 2024 Ymsniper
# SPDX-License-Identifier: GPL-3.0-or-later
"""
KDE / KWin window control via one-shot scripting over D-Bus.

KWin exposes a scripting API that lets us load a JS snippet, run it, then
unload it.  This is the only reliable way to move/resize windows on KDE
Wayland without a compositor-specific protocol.

Downside: each _kwin_script() call takes ~175 ms round-trip, so the physics
loop caps at 4 FPS on this backend.

Requires qdbus6 (KDE 6) or qdbus (KDE 5) to be on $PATH.
"""

import os
import subprocess
import tempfile

from ..logger import DBG, ERR

_qdbus_bin: "str | None" = None


def _qdbus() -> "str | None":
    """
    Locate the qdbus binary (tries qdbus6 first, then qdbus).

    Result is cached after the first successful probe.
    """
    global _qdbus_bin
    if _qdbus_bin:
        return _qdbus_bin
    for b in ("qdbus6", "qdbus"):
        try:
            if subprocess.run([b, "--version"], capture_output=True, timeout=1).returncode == 0:
                _qdbus_bin = b
                return b
        except FileNotFoundError:
            pass
    return None


def _kwin_script(js: str) -> None:
    """
    Execute a one-shot KWin script snippet via D-Bus.

    Workflow:
      1. Write the JS to a temp file.
      2. Call org.kde.kwin.Scripting.loadScript → get numeric script ID.
      3. Call Script<ID>.run.
      4. Call Script<ID>.stop.
      5. Delete the temp file.

    Errors are logged but not re-raised; a failed move just means a dropped
    frame, which is preferable to crashing the player.
    """
    qdbus = _qdbus()
    if not qdbus:
        ERR("_kwin_script: no qdbus binary found (tried qdbus6, qdbus)")
        return
    DBG(f"kwin_script js={js!r}")
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".js", prefix="/tmp/wdp_", delete=False,
    ) as f:
        f.write(js)
        tmp = f.name
    try:
        r = subprocess.run(
            [qdbus, "org.kde.KWin", "/Scripting",
             "org.kde.kwin.Scripting.loadScript", tmp],
            capture_output=True, text=True, timeout=3,
        )
        sid = r.stdout.strip()
        DBG(f"kwin loadScript → sid={sid!r} err={r.stderr.strip()!r}")
        if sid.isdigit():
            r2 = subprocess.run(
                [qdbus, "org.kde.KWin", f"/Scripting/Script{sid}",
                 "org.kde.kwin.Script.run"],
                capture_output=True, text=True, timeout=2,
            )
            DBG(f"kwin Script.run → out={r2.stdout.strip()!r} err={r2.stderr.strip()!r}")
            subprocess.run(
                [qdbus, "org.kde.KWin", f"/Scripting/Script{sid}",
                 "org.kde.kwin.Script.stop"],
                capture_output=True, timeout=1,
            )
        else:
            ERR(f"kwin loadScript non-numeric sid={sid!r}")
            ERR(f"  stdout={r.stdout!r} stderr={r.stderr!r}")
    except Exception as e:
        ERR(f"_kwin_script exception: {e}")
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
