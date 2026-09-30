# COMPAT FIXTURE - do not edit. Verbatim copy of meeting_notes/client/resample.py from the 0.7.4 client
# (release/0.7.4), with only the meeting_notes.* imports rewritten to be package-relative.
"""Converting captured audio (any device rate) to the 16 kHz the wire protocol carries.

Why this can't just be "keep every Nth sample": naive decimation aliases.
Downsampling 48 kHz audio to 16 kHz by keeping every third sample folds any
energy above the new Nyquist frequency (8 kHz) back down into the audible
band -- a 10 kHz tone would reappear at 6 kHz, and in speech that shows up as
harsh, un-removable noise smeared across everything. The fix is the standard
one: low-pass filter out everything above (just under) 8 kHz *before*
decimating, so there is nothing left above Nyquist to fold down.

The filter is a windowed-sinc FIR, built with numpy only (no scipy dependency
for what is a couple dozen lines of DSP). It runs causally over streaming
blocks rather than the whole signal at once, because live capture hands us
audio a block at a time and can't wait for the end of the meeting to filter
it.
"""

from __future__ import annotations

from typing import Optional

import numpy as np

from .wire import STREAM_SAMPLE_RATE

# Odd tap count => an integer group delay (the filter is exactly symmetric
# around its centre sample), which keeps the streaming bookkeeping below
# simple integer arithmetic instead of fractional sample offsets.
_DEFAULT_NUM_TAPS = 129
# How far below the new Nyquist (target_rate / 2) the filter's cutoff sits.
# Leaving a margin means audio right at the edge of the target band (e.g. a
# real 7 kHz consonant) is still passed at close to full strength, while the
# transition band still finishes comfortably below Nyquist so nothing above
# it aliases in.
_CUTOFF_FRACTION = 0.94


def _design_lowpass(cutoff_hz: float, source_rate: int, num_taps: int) -> np.ndarray:
    """A windowed-sinc low-pass FIR with unity DC gain.

    Sinc is the ideal brick-wall low-pass response; windowing (Hamming) trades
    a little transition sharpness for a filter of finite length with no
    ringing that grows unboundedly, and normalizing by the tap sum makes sure
    silence in stays silence out and a steady tone keeps its amplitude.
    """
    if num_taps % 2 == 0:
        num_taps += 1
    half = (num_taps - 1) / 2.0
    n = np.arange(num_taps, dtype=np.float64) - half
    fc = cutoff_hz / source_rate  # cutoff as a fraction of the source rate
    # np.sinc(x) = sin(pi*x)/(pi*x), already normalized -- this is the sampled
    # ideal low-pass impulse response, scaled to 2*fc so its DC gain is 1.
    taps = 2.0 * fc * np.sinc(2.0 * fc * n)
    taps *= np.hamming(num_taps)
    taps /= taps.sum()
    return taps


class Downsampler:
    """Streaming converter from one source rate to ``target_rate`` (default 16 kHz).

    Call ``process`` once per captured block, in order. Filter state (the
    trailing samples a convolution needs from the previous block) and the
    decimation phase both carry over between calls, so consecutive calls on
    consecutive blocks are equivalent to filtering the whole signal at once --
    there is no click, step or repeated/dropped sample at block boundaries.

    Not thread-safe: use one instance per track/stream, called from a single
    thread, matching how ``TrackRecorder`` already hands blocks to callbacks.
    """

    def __init__(
        self,
        source_rate: int,
        target_rate: int = STREAM_SAMPLE_RATE,
        *,
        num_taps: int = _DEFAULT_NUM_TAPS,
    ):
        if source_rate <= 0 or target_rate <= 0:
            raise ValueError("sample rates must be positive")
        self.source_rate = int(source_rate)
        self.target_rate = int(target_rate)
        self._ratio = self.source_rate / self.target_rate

        if self.target_rate >= self.source_rate:
            # Nothing above the target Nyquist can exist in the source, so
            # there is nothing to alias and no filtering is needed. Also
            # covers the common no-op case (source already 16 kHz) cheaply.
            self._taps: Optional[np.ndarray] = None
        else:
            cutoff = _CUTOFF_FRACTION * (self.target_rate / 2.0)
            self._taps = _design_lowpass(cutoff, self.source_rate, num_taps)

        tail_len = (len(self._taps) - 1) if self._taps is not None else 0
        self._raw_tail = np.zeros(tail_len, dtype=np.float64)

        ratio_rounded = round(self._ratio)
        self._integer_ratio = (
            ratio_rounded > 0 and abs(self._ratio - ratio_rounded) < 1e-9
        )
        self._int_n = int(ratio_rounded) if self._integer_ratio else 0
        # Constant shift introduced by centring the (odd-length, symmetric)
        # filter kernel -- see _design_lowpass. Folding it into the phase
        # calculation once here means the per-call decimation offset only
        # needs a single modulo, not a search for the "right" sample.
        self._center_shift = (len(self._taps) - 1) // 2 if self._taps is not None else 0
        self._raw_sample_count = 0  # integer-ratio path: samples seen so far

        # Non-integer ratio path: a small buffer of filtered-but-not-yet-used
        # samples, plus a fractional cursor into it.
        self._interp_buffer = np.zeros(0, dtype=np.float64)
        self._interp_cursor = 0.0

        self._input_dtype: Optional[np.dtype] = None

    def _to_float(self, block: np.ndarray) -> np.ndarray:
        arr = np.asarray(block)
        if self._input_dtype is None:
            self._input_dtype = arr.dtype
        if np.issubdtype(arr.dtype, np.integer):
            # Matches wav_io's int16 <-> float scaling convention.
            return arr.astype(np.float64) / 32767.0
        return arr.astype(np.float64)

    def _from_float(self, values: np.ndarray) -> np.ndarray:
        if self._input_dtype is not None and np.issubdtype(self._input_dtype, np.integer):
            scaled = np.round(np.clip(values, -1.0, 1.0) * 32767.0)
            return scaled.astype(self._input_dtype)
        return values.astype(np.float32)

    def _filter(self, block: np.ndarray) -> np.ndarray:
        if self._taps is None:
            return block
        padded = np.concatenate([self._raw_tail, block])
        # 'valid' keeps only output points fully covered by real samples
        # (tail + this block), which is exactly what makes the tail-carry
        # trick correct: nothing here depends on samples we haven't seen yet.
        filtered = np.convolve(padded, self._taps, mode="valid") if block.size else np.zeros(0)
        tail_len = len(self._raw_tail)
        if tail_len:
            self._raw_tail = padded[-tail_len:] if len(padded) >= tail_len else np.concatenate(
                [self._raw_tail, block]
            )[-tail_len:]
        return filtered

    def _decimate_integer(self, filtered: np.ndarray, block_len: int) -> np.ndarray:
        n = self._int_n
        # Which index in `filtered` lands on a global sample position that is
        # both a multiple of n and downstream of the filter's centring shift.
        # See the comment on _center_shift.
        start = (self._center_shift - self._raw_sample_count) % n
        out = filtered[start::n]
        self._raw_sample_count += block_len
        return out

    def _decimate_fractional(self, filtered: np.ndarray) -> np.ndarray:
        combined = np.concatenate([self._interp_buffer, filtered])
        pos = self._interp_cursor
        outs = []
        while pos + 1.0 < len(combined):
            i0 = int(np.floor(pos))
            frac = pos - i0
            outs.append(combined[i0] * (1.0 - frac) + combined[i0 + 1] * frac)
            pos += self._ratio
        # `pos` can land PAST the end of `combined`: the loop exits once
        # pos+1 >= len, but the final step still advances by up to `ratio`
        # (2.76 at 44.1kHz). Slicing with an out-of-range index silently
        # clamps to empty and the overshoot is lost, so each block skipped
        # nearly a sample too few and the output ran long -- about 0.13% fast,
        # which is ~4.5 seconds of drift over an hour on a 44.1kHz device.
        # Clamp the index but keep the overshoot in the cursor instead.
        keep_from = min(max(0, int(np.floor(pos))), len(combined))
        self._interp_buffer = combined[keep_from:]
        self._interp_cursor = pos - keep_from
        return np.asarray(outs, dtype=np.float64)

    def process(self, block: np.ndarray) -> np.ndarray:
        """Filter and decimate one block. Returns audio at ``target_rate``.

        May return an empty array (e.g. the very first, short block of a
        stream, before enough samples have accumulated to produce any output
        yet) -- callers should treat that as "nothing to send this time", not
        an error.
        """
        arr = self._to_float(np.asarray(block).reshape(-1))
        filtered = self._filter(arr)
        if self.target_rate >= self.source_rate:
            out = filtered
        elif self._integer_ratio:
            out = self._decimate_integer(filtered, arr.size)
        else:
            out = self._decimate_fractional(filtered)
        return self._from_float(out)


def downsample_to_16k(block: np.ndarray, source_rate: int) -> np.ndarray:
    """One-shot convenience wrapper: no state kept, filters ``block`` alone.

    Fine for a single WAV file already in memory (e.g. the upload-time
    conversion in ``queue.py`` works block-by-block through ``Downsampler``
    instead, for large files). Not click-free across repeated calls -- use
    ``Downsampler`` directly for that.
    """
    return Downsampler(source_rate, STREAM_SAMPLE_RATE).process(block)
