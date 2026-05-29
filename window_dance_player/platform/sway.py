# Copyright (C) 2024 Ymsniper
# SPDX-License-Identifier: GPL-3.0-or-later
"""
Sway IPC helpers — direct Unix socket using the i3-compatible binary protocol.

Avoids spawning swaymsg per frame by talking directly to the socket.
Protocol: 6-byte magic "i3-ipc" + 4-byte LE length + 4-byte LE type + payload.
Type 0 = RUN_COMMAND.

Globals
-------
_SWAY_SOCK      Path read from $SWAYSOCK at import time (empty if not set)
"""

import os
import socket as _sock
import struct

_SWAY_SOCK: str = os.environ.get("SWAYSOCK", "")


def _sway_command(cmd: str) -> None:
    """
    Send a RUN_COMMAND to Sway via its Unix IPC socket.

    Fire-and-forget; the reply is not read.
    No-op if $SWAYSOCK was not set at startup.
    """
    if not _SWAY_SOCK:
        return
    payload = cmd.encode()
    # i3 IPC header: magic(6) + length(4 LE) + type(4 LE)  type=0 → RUN_COMMAND
    msg = b"i3-ipc" + struct.pack("<II", len(payload), 0) + payload
    try:
        with _sock.socket(_sock.AF_UNIX, _sock.SOCK_STREAM) as s:
            s.settimeout(0.1)
            s.connect(_SWAY_SOCK)
            s.sendall(msg)
    except Exception:
        pass
