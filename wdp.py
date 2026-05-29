#!/usr/bin/env python3
# Copyright (C) 2024 Ymsniper
# SPDX-License-Identifier: GPL-3.0-or-later
"""
Convenience launcher — run from the project root:

    python wdp.py song.mp3
    python wdp.py *.mp3

Equivalent to:  python -m window_dance_player song.mp3
"""
from window_dance_player.main import main

if __name__ == "__main__":
    main()
