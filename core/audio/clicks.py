from __future__ import annotations
import math
from typing import Optional

import numpy as np


class ClickTrack:
    """
    Metronome clicks mixed into the player's output stream.

    Beats are positions on the track timeline. A click starts at the output frame where the
    render head reaches its beat and then plays at the device rate regardless of tempo, so it
    stays sample-aligned with the music without a second audio stream.
    """

    MAX_CLICKS_PER_BLOCK = 4

    def __init__(self, rate: int, channels: int):
        self.rate = int(rate)
        self.ch = int(channels)
        self._sample: Optional[np.ndarray] = None       # [L, ch] float32 at device rate
        self._beats = np.zeros(0, dtype=np.float64)     # beat positions (input frames, sorted)
        self._gains = np.zeros(0, dtype=np.float32)
        self._enabled = False
        self._offset = 0.0                              # input frames; positive = clicks lead the beat
        self._voices: list[list] = []                   # [read index into sample, gain]

    def set_sample(self, sample: Optional[np.ndarray]) -> None:
        """Click sound as [L, ch] float32 at the device rate (None disables)."""
        self._sample = None if sample is None else np.asarray(sample, dtype=np.float32)
        self._voices.clear()

    def set_beats(self, beats_sec, gains) -> None:
        if beats_sec is None or len(beats_sec) == 0:
            self._beats = np.zeros(0, dtype=np.float64)
            self._gains = np.zeros(0, dtype=np.float32)
            return
        self._beats = np.asarray(beats_sec, dtype=np.float64) * self.rate
        self._gains = np.asarray(gains, dtype=np.float32)

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = bool(enabled)
        if not self._enabled:
            self._voices.clear()

    def set_offset_sec(self, offset_sec: float) -> None:
        self._offset = float(offset_sec) * self.rate

    def clear_voices(self) -> None:
        self._voices.clear()

    def queue(self, in_start: float, step: float, n: int) -> None:
        """Start a click at the output frame where each beat inside this block is reached."""
        if not self._enabled or self._sample is None or self._beats.size == 0:
            return
        lo = in_start + self._offset
        hi = in_start + step * n + self._offset
        i0 = int(np.searchsorted(self._beats, lo, side="left"))
        i1 = int(np.searchsorted(self._beats, hi, side="left"))
        for i in range(max(i0, i1 - self.MAX_CLICKS_PER_BLOCK), i1):
            k = int(math.ceil((self._beats[i] - lo) / step))
            self._voices.append([-k, float(self._gains[i])])

    def mix(self, out: np.ndarray) -> np.ndarray:
        """Add sounding clicks to a block about to be written (returns a new array)."""
        if not self._voices:
            return out
        sample = self._sample
        if sample is None:
            self._voices.clear()
            return out
        n = out.shape[0]
        length = sample.shape[0]
        out = np.array(out, dtype=np.float32)
        keep = []
        for p, g in self._voices:
            a = max(0, -p)
            b = min(n, length - p)
            if b > a:
                out[a:b] += sample[p + a:p + b] * np.float32(g)
            p += n
            if p < length:
                keep.append([p, g])
        self._voices = keep
        return out
