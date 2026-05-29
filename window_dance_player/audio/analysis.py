# Copyright (C) 2024 Ymsniper
# SPDX-License-Identifier: GPL-3.0-or-later
"""
Audio beat analysis using librosa.

analyze_audio() is intentionally blocking and designed to run in a daemon
thread so it does not stall the UI or audio playback.  It can take several
seconds on long tracks.
"""

from pathlib import Path


def analyze_audio(filepath: "str | Path") -> "tuple[float, float, object]":
    """
    Load an audio file and extract beat information.

    Parameters
    ----------
    filepath
        Path to any audio format supported by librosa (MP3, WAV, FLAC, OGG…).

    Returns
    -------
    (duration_secs, bpm, beat_times_array)
        duration_secs   Total track length in seconds.
        bpm             Estimated tempo in beats per minute.
        beat_times_array
                        NumPy array of beat timestamps in seconds, suitable for
                        direct index-scan in the beat-watcher thread.
    """
    import librosa

    y, sr = librosa.load(str(filepath), sr=None, mono=True)
    duration = len(y) / sr

    tempo, beat_frames = librosa.beat.beat_track(y=y, sr=sr)
    beat_times = librosa.frames_to_time(beat_frames, sr=sr)

    # librosa ≥ 0.10 returns tempo as a 1-element array; older versions return
    # a scalar.  Normalise to a plain float in both cases.
    bpm = float(tempo[0]) if hasattr(tempo, "__len__") else float(tempo)

    return duration, bpm, beat_times
