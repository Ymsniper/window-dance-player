# Copyright (C) 2024 Ymsniper
# SPDX-License-Identifier: GPL-3.0-or-later
"""
Centralised logging for Window Dance Player.

All modules import DBG, WARN, ERR from here so every debug line
ends up in the same wdp_debug.log file with consistent formatting.
"""

import logging

_LOG = logging.getLogger("wdp")
_LOG.setLevel(logging.DEBUG)

_fh = logging.FileHandler("wdp_debug.log", mode="w")
_fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s  %(message)s"))
_LOG.addHandler(_fh)

DBG  = _LOG.debug
WARN = _LOG.warning
ERR  = _LOG.error
