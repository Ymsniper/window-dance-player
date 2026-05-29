# Copyright (C) 2024 Ymsniper
# SPDX-License-Identifier: GPL-3.0-or-later
"""
Hyprland IPC helpers — direct Unix socket communication.

All commands are fire-and-forget (no reply awaited) so the physics thread
never blocks on Hyprland's response.  The socket path is resolved once at
startup via _init_hyprland_sock() and cached in _HYPR_SOCK.

Globals
-------
_HYPR_SOCK      Resolved IPC socket path (empty string until initialised)
"""

import os
import socket as _sock

from ..logger import DBG, ERR, WARN

_HYPR_SOCK: str = ""   # set by _init_hyprland_sock()


def _hyprland_socket_path(suffix: str = ".socket.sock") -> str:
    """
    Resolve the Hyprland IPC socket path.

    Newer Hyprland (≥ 0.28, shipped on CachyOS) moved the socket from
    /tmp/hypr/$SIG/ to $XDG_RUNTIME_DIR/hypr/$SIG/.
    XDG_RUNTIME_DIR is tried first; /tmp is the legacy fallback.
    """
    sig = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE", "")
    xdg = os.environ.get("XDG_RUNTIME_DIR", "")
    DBG(f"socket_path: sig={sig!r} xdg={xdg!r}")
    if not sig:
        ERR("HYPRLAND_INSTANCE_SIGNATURE not set — Hyprland IPC unavailable")
        return ""
    if xdg:
        p = f"{xdg}/hypr/{sig}/{suffix}"
        DBG(f"trying XDG socket: {p}  exists={os.path.exists(p)}")
        if os.path.exists(p):
            return p
    p = f"/tmp/hypr/{sig}/{suffix}"
    DBG(f"trying legacy socket: {p}  exists={os.path.exists(p)}")
    if os.path.exists(p):
        return p
    best = f"{xdg}/hypr/{sig}/{suffix}" if xdg else f"/tmp/hypr/{sig}/{suffix}"
    WARN(f"socket not found at any known path; guessing {best}")
    return best


def _init_hyprland_sock() -> None:
    """
    Resolve and cache the IPC socket path at startup.

    Must be called once from main() before any IPC is attempted.
    """
    global _HYPR_SOCK
    _HYPR_SOCK = _hyprland_socket_path(".socket.sock")


def _hyprland_dispatch(cmd: str) -> None:
    """
    Send a single dispatch command via the Hyprland Unix IPC socket.

    Fire-and-forget — the reply is not awaited, which removes the blocking
    wait for Hyprland to echo "ok" and cuts per-call latency roughly in half.
    """
    if not _HYPR_SOCK:
        return
    try:
        with _sock.socket(_sock.AF_UNIX, _sock.SOCK_STREAM) as s:
            s.settimeout(0.3)
            s.connect(_HYPR_SOCK)
            s.sendall(f"dispatch {cmd}".encode())
    except Exception as e:
        ERR(f"dispatch {cmd!r} failed: {e}")


def _hyprland_batch(cmds: "list[str]") -> None:
    """
    Send multiple dispatch commands in a single socket round-trip via [[BATCH]].

    Approximately 2× faster than calling _hyprland_dispatch() twice because
    only one connect + sendall is needed for the entire batch.
    """
    if not _HYPR_SOCK or not cmds:
        return
    payload = ("[[BATCH]]" + ";".join(f"dispatch {c}" for c in cmds)).encode()
    try:
        with _sock.socket(_sock.AF_UNIX, _sock.SOCK_STREAM) as s:
            s.settimeout(0.3)
            s.connect(_HYPR_SOCK)
            s.sendall(payload)
    except Exception as e:
        ERR(f"_hyprland_batch {cmds} failed: {e}")


def _hyprland_query(cmd: str) -> str:
    """
    Send a query command to Hyprland IPC and return the full response string.

    Used for read-only queries (e.g. activewindow, monitors) where a reply
    is required.  Blocks until the socket is closed by Hyprland.
    """
    sock_path = _HYPR_SOCK or _hyprland_socket_path(".socket.sock")
    if not sock_path:
        return ""
    try:
        with _sock.socket(_sock.AF_UNIX, _sock.SOCK_STREAM) as s:
            s.settimeout(1.0)
            s.connect(sock_path)
            s.sendall(cmd.encode())
            chunks = []
            while True:
                chunk = s.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
            return b"".join(chunks).decode(errors="replace")
    except Exception:
        return ""
