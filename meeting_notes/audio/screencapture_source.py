"""System-audio capture on macOS 13+ through ScreenCaptureKit (no BlackHole).

macOS has no loopback *device*, but ScreenCaptureKit can hand an app the mix of
everything the system is playing as a stream of ``CMSampleBuffer`` audio
buffers. This module turns that into the same ``AudioSource`` / ``Reader``
protocol the rest of the recorder already speaks (see ``source.py``), so the
recorder, drift correction and WAV writer need no macOS special cases.

Layers, from pure to platform-bound (only the last one touches PyObjC):

* :func:`pcm_bytes_to_array` / :func:`extract_pcm` /
  :func:`sample_buffer_to_array` -- ``CMSampleBuffer`` -> ``(frames, channels)``
  float32. ``extract_pcm`` takes the CoreMedia module as an argument, so tests
  drive it with a fake and never need macOS.
* :class:`BlockQueue` -- the blocking ``read(n)`` the recorder expects, fed by
  the capture callback thread. It keeps the stream aligned with the wall clock:
  ScreenCaptureKit may go quiet when nothing is playing, and a recorder that
  simply waited for frames would then run its timeline slower than real time.
  Missing frames are padded with silence and later-arriving frames are
  discarded up to the same amount ("debt"), so frame position keeps meaning
  wall-clock time -- the invariant the transcript merge relies on.
* :class:`ScreenCaptureKitSource` -- the ``AudioSource``. ``open()`` starts a
  stream through a factory (real: :class:`ObjcStream`; tests: a fake).
* :class:`ObjcStream` -- the PyObjC bridge. ScreenCaptureKit, CoreMedia and
  Quartz are imported lazily inside it, so importing this module is safe on
  Windows and Linux.

Permission: capturing needs "Screen & System Audio Recording" for the app that
runs this code (the ``Meeting Notes.app`` bundle when packaged). Without it,
:class:`SystemAudioPermissionError` carries the message shown in the UI.
"""

from __future__ import annotations

import collections
import logging
import os
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable, ContextManager, Deque, Iterator, List, Optional, Tuple

import numpy as np

log = logging.getLogger("meeting_notes.audio.screencapture_source")

SAMPLE_RATE = 48000
CHANNELS = 2
SOURCE_NAME = "System audio (ScreenCaptureKit)"

PERMISSION_MESSAGE = (
    "Meeting Notes is not allowed to record system audio. Allow it in System "
    "Settings → Privacy & Security → Screen & System Audio Recording, "
    "then quit and reopen Meeting Notes."
)

# SCStreamErrorUserDeclined
_SC_USER_DECLINED = -3801

# How long a read may lag the wall-clock timeline before the missing frames
# are replaced by silence.
_SLACK_SECONDS = 0.35


class SystemAudioPermissionError(RuntimeError):
    """Screen & System Audio Recording has not been granted to this app."""

    def __init__(self, message: str = PERMISSION_MESSAGE):
        super().__init__(message)


class SystemAudioUnavailable(RuntimeError):
    """ScreenCaptureKit cannot be used here (old macOS, no PyObjC, no display)."""


# -- platform probes ---------------------------------------------------------------


def macos_version() -> Tuple[int, ...]:
    import platform

    try:
        return tuple(int(p) for p in platform.mac_ver()[0].split(".") if p.isdigit())
    except Exception:  # noqa: BLE001
        return ()


def available() -> Tuple[bool, str]:
    """Whether ScreenCaptureKit audio capture can be attempted. Never raises.

    This says nothing about permission; see :func:`permission_granted`.
    """
    if sys.platform != "darwin":
        return False, "ScreenCaptureKit is macOS-only"
    version = macos_version()
    if version and version < (13,):
        return False, "ScreenCaptureKit audio capture needs macOS 13 or newer"
    try:
        import ScreenCaptureKit  # noqa: F401
        import CoreMedia  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        return False, f"PyObjC ScreenCaptureKit bindings are not installed ({type(exc).__name__})"
    return True, ""


def permission_granted() -> Optional[bool]:
    """Screen & System Audio Recording state without prompting; None if unknown."""
    if sys.platform != "darwin":
        return None
    try:
        import Quartz

        return bool(Quartz.CGPreflightScreenCaptureAccess())
    except Exception:  # noqa: BLE001
        return None


def request_permission() -> bool:
    """Ask macOS for the permission (shows the system prompt the first time).

    ``MEETING_NOTES_NO_PERMISSION_PROMPT=1`` turns this into a no-op so
    unattended test runs never leave a dialog waiting on someone's screen.
    """
    if sys.platform != "darwin" or os.environ.get("MEETING_NOTES_NO_PERMISSION_PROMPT"):
        return False
    try:
        import Quartz

        return bool(Quartz.CGRequestScreenCaptureAccess())
    except Exception:  # noqa: BLE001
        return False


# -- CMSampleBuffer -> numpy -------------------------------------------------------


@dataclass(frozen=True)
class PcmFormat:
    sample_rate: float
    channels: int
    is_float: bool
    bits: int
    non_interleaved: bool


_FLAG_FLOAT = 1 << 0
_FLAG_NON_INTERLEAVED = 1 << 5


def format_from_asbd(asbd) -> PcmFormat:
    """Read a CoreAudio ``AudioStreamBasicDescription`` (struct or object)."""
    flags = int(asbd.mFormatFlags)
    return PcmFormat(
        sample_rate=float(asbd.mSampleRate),
        channels=max(1, int(asbd.mChannelsPerFrame)),
        is_float=bool(flags & _FLAG_FLOAT),
        bits=int(asbd.mBitsPerChannel) or 32,
        non_interleaved=bool(flags & _FLAG_NON_INTERLEAVED),
    )


def pcm_bytes_to_array(data, fmt: PcmFormat, frames: Optional[int] = None) -> np.ndarray:
    """Decode raw PCM bytes to a ``(frames, channels)`` float32 array in [-1, 1].

    Non-interleaved (planar) data is channel-major: all of channel 0, then all
    of channel 1, ... which is how a ScreenCaptureKit block buffer lays out an
    ``AudioBufferList`` of separate channel buffers.
    """
    if fmt.is_float and fmt.bits == 32:
        dtype, scale = np.dtype("<f4"), None
    elif fmt.is_float and fmt.bits == 64:
        dtype, scale = np.dtype("<f8"), None
    elif not fmt.is_float and fmt.bits == 16:
        dtype, scale = np.dtype("<i2"), 32768.0
    elif not fmt.is_float and fmt.bits == 32:
        dtype, scale = np.dtype("<i4"), 2147483648.0
    else:
        raise ValueError(f"unsupported PCM format: {fmt}")
    payload = bytes(data)
    raw = np.frombuffer(payload[: len(payload) // dtype.itemsize * dtype.itemsize], dtype=dtype)
    per_frame = fmt.channels
    usable = (len(raw) // per_frame) * per_frame
    raw = raw[:usable]
    if frames is not None and frames * per_frame <= len(raw):
        raw = raw[: frames * per_frame]
    n = len(raw) // per_frame
    if fmt.non_interleaved:
        arr = raw.reshape(fmt.channels, n).T
    else:
        arr = raw.reshape(n, fmt.channels)
    out = np.ascontiguousarray(arr, dtype=np.float32)
    if scale is not None:
        out /= np.float32(scale)
    return out


def extract_pcm(sample_buffer, cm=None) -> Tuple[PcmFormat, bytes, int]:
    """(format, raw bytes, frame count) of a CoreMedia audio sample buffer.

    ``cm`` is the ``CoreMedia`` module; tests pass a fake exposing the same five
    functions.
    """
    if cm is None:
        import CoreMedia as cm  # type: ignore[no-redef]
    desc = cm.CMSampleBufferGetFormatDescription(sample_buffer)
    if desc is None:
        raise ValueError("sample buffer has no format description")
    asbd = cm.CMAudioFormatDescriptionGetStreamBasicDescription(desc)
    if isinstance(asbd, tuple):  # some bindings return (asbd,) or (err, asbd)
        asbd = asbd[-1]
    fmt = format_from_asbd(asbd)
    frames = int(cm.CMSampleBufferGetNumSamples(sample_buffer))
    block = cm.CMSampleBufferGetDataBuffer(sample_buffer)
    if block is None:
        raise ValueError("sample buffer has no data buffer")
    length = int(cm.CMBlockBufferGetDataLength(block))
    result = cm.CMBlockBufferCopyDataBytes(block, 0, length, None)
    status, data = result if isinstance(result, tuple) else (0, result)
    if status != 0:
        raise ValueError(f"CMBlockBufferCopyDataBytes failed ({status})")
    return fmt, bytes(data), frames


def conform(block: np.ndarray, fmt: PcmFormat, *, rate: int = SAMPLE_RATE, channels: int = CHANNELS) -> np.ndarray:
    """Force a decoded block to the source's declared rate and channel count."""
    if block.shape[1] != channels:
        if block.shape[1] == 1:
            block = np.repeat(block, channels, axis=1)
        elif block.shape[1] > channels:
            block = block[:, :channels]
        else:
            block = np.concatenate(
                [block, np.repeat(block[:, -1:], channels - block.shape[1], axis=1)], axis=1
            )
    if int(round(fmt.sample_rate)) != rate and len(block) > 1:
        # ScreenCaptureKit honours the requested 48 kHz, so this is only a
        # safety net; linear interpolation is plenty for speech.
        n_out = max(1, int(round(len(block) * rate / fmt.sample_rate)))
        x_old = np.linspace(0.0, 1.0, len(block), endpoint=False)
        x_new = np.linspace(0.0, 1.0, n_out, endpoint=False)
        block = np.stack(
            [np.interp(x_new, x_old, block[:, c]) for c in range(block.shape[1])], axis=1
        ).astype(np.float32)
    return np.ascontiguousarray(block, dtype=np.float32)


def sample_buffer_to_array(sample_buffer, cm=None) -> np.ndarray:
    """CMSampleBuffer -> ``(frames, 2)`` float32 at 48 kHz."""
    fmt, data, frames = extract_pcm(sample_buffer, cm)
    return conform(pcm_bytes_to_array(data, fmt, frames), fmt)


# -- wall-clock aligned block queue --------------------------------------------------


class BlockQueue:
    """Thread-safe FIFO of audio frames with the recorder's ``read(n)`` contract."""

    def __init__(
        self,
        *,
        rate: int = SAMPLE_RATE,
        channels: int = CHANNELS,
        clock: Callable[[], float] = time.monotonic,
        slack: float = _SLACK_SECONDS,
    ):
        self.rate = rate
        self.channels = channels
        self._clock = clock
        self._slack = slack
        self._cond = threading.Condition()
        self._chunks: Deque[np.ndarray] = collections.deque()
        self._buffered = 0
        self._error: Optional[BaseException] = None
        self._closed = False
        self._t0: Optional[float] = None
        self._delivered = 0
        self._debt = 0
        self._warnings: List[str] = []
        self.padded_frames = 0
        self.dropped_frames = 0

    # producer side (capture thread)
    def feed(self, block: np.ndarray) -> None:
        if block is None or len(block) == 0:
            return
        with self._cond:
            if self._debt > 0:
                drop = min(self._debt, len(block))
                self._debt -= drop
                self.dropped_frames += drop
                block = block[drop:]
                if len(block) == 0:
                    return
            self._chunks.append(block)
            self._buffered += len(block)
            self._cond.notify_all()

    def fail(self, error: BaseException) -> None:
        with self._cond:
            self._error = error
            self._cond.notify_all()

    def close(self) -> None:
        with self._cond:
            self._closed = True
            self._cond.notify_all()

    # consumer side (track thread)
    def read(self, numframes: int) -> np.ndarray:
        numframes = max(1, int(numframes))
        with self._cond:
            if self._t0 is None:
                self._t0 = self._clock()
            deadline = self._t0 + (self._delivered + numframes) / self.rate + self._slack
            while self._buffered < numframes and self._error is None and not self._closed:
                remaining = deadline - self._clock()
                if remaining <= 0:
                    break
                self._cond.wait(min(remaining, 0.25))
            if self._error is not None and self._buffered == 0:
                error, self._error = self._error, None
                raise error
            take = min(numframes, self._buffered)
            out = np.zeros((numframes, self.channels), dtype=np.float32)
            got = 0
            while got < take:
                chunk = self._chunks[0]
                need = take - got
                if len(chunk) <= need:
                    out[got : got + len(chunk)] = chunk
                    got += len(chunk)
                    self._chunks.popleft()
                else:
                    out[got : got + need] = chunk[:need]
                    self._chunks[0] = chunk[need:]
                    got += need
            self._buffered -= take
            short = numframes - take
            if short > 0 and not self._closed:
                self._debt += short
                self.padded_frames += short
                self._warnings.append(
                    f"no system audio from ScreenCaptureKit for {short / self.rate:.2f}s; padded with silence"
                )
            self._delivered += numframes
            return out

    def drain_warnings(self) -> List[str]:
        with self._cond:
            warnings, self._warnings = self._warnings, []
        # Padding is normal while nothing is playing; only surface real stalls.
        return [w for w in warnings if _seconds_in(w) >= 1.0]


def _seconds_in(message: str) -> float:
    try:
        return float(message.split("for ")[1].split("s;")[0])
    except (IndexError, ValueError):
        return 0.0


class _Reader:
    def __init__(self, queue: BlockQueue):
        self._queue = queue

    def read(self, numframes: int) -> np.ndarray:
        return self._queue.read(numframes)

    def drain_warnings(self) -> List[str]:
        return self._queue.drain_warnings()


# -- the AudioSource --------------------------------------------------------------------


class StreamHandle:
    """What a stream factory returns: something that can be stopped."""

    def stop(self) -> None:  # pragma: no cover - interface
        raise NotImplementedError


StreamFactory = Callable[[Callable[[np.ndarray], None], Callable[[BaseException], None]], StreamHandle]


class ScreenCaptureKitSource:
    """An ``AudioSource`` for the system-output mix, backed by ScreenCaptureKit."""

    def __init__(
        self,
        *,
        stream_factory: Optional[StreamFactory] = None,
        samplerate: int = SAMPLE_RATE,
        channels: int = CHANNELS,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._factory = stream_factory or _default_stream_factory
        self._samplerate = samplerate
        self._channels = channels
        self._clock = clock

    @property
    def name(self) -> str:
        return SOURCE_NAME

    @property
    def channels(self) -> int:
        return self._channels

    @property
    def samplerate(self) -> int:
        return self._samplerate

    @contextmanager
    def open(self) -> Iterator[_Reader]:
        queue = BlockQueue(rate=self._samplerate, channels=self._channels, clock=self._clock)
        handle = self._factory(queue.feed, queue.fail)
        try:
            yield _Reader(queue)
        finally:
            queue.close()
            try:
                handle.stop()
            except Exception:  # noqa: BLE001 - shutting down; nothing to recover
                log.debug("ScreenCaptureKit stop failed", exc_info=True)


def _default_stream_factory(on_audio, on_error) -> StreamHandle:
    return ObjcStream.start(on_audio, on_error)


# -- PyObjC bridge (macOS only) ------------------------------------------------------------


def _error_is_permission(error) -> bool:
    try:
        code = int(error.code())
    except Exception:  # noqa: BLE001
        return False
    text = ""
    try:
        text = str(error.localizedDescription()).lower()
    except Exception:  # noqa: BLE001
        pass
    return code == _SC_USER_DECLINED or "declined" in text or "not authorized" in text


class ObjcStream(StreamHandle):
    """A running ``SCStream`` capturing display audio. Create with :meth:`start`."""

    def __init__(self):
        self._stream = None
        self._handler = None

    @classmethod
    def start(cls, on_audio, on_error, *, timeout: float = 10.0) -> "ObjcStream":
        ok, why = available()
        if not ok:
            raise SystemAudioUnavailable(why)
        if permission_granted() is False:
            # Triggers the one-time system prompt and lists the app under
            # Privacy & Security so the user can switch it on.
            request_permission()
            raise SystemAudioPermissionError()

        import ScreenCaptureKit as SCK
        import CoreMedia as CM
        import objc
        from Foundation import NSObject

        done = threading.Event()
        box: dict = {}

        def got_content(content, error):
            box["content"], box["error"] = content, error
            done.set()

        SCK.SCShareableContent.getShareableContentWithCompletionHandler_(got_content)
        if not done.wait(timeout):
            raise SystemAudioUnavailable("ScreenCaptureKit did not answer (timed out)")
        if box.get("error") is not None:
            if _error_is_permission(box["error"]):
                raise SystemAudioPermissionError()
            raise SystemAudioUnavailable(str(box["error"].localizedDescription()))
        displays = list(box["content"].displays())
        if not displays:
            raise SystemAudioUnavailable("no display is available for ScreenCaptureKit")

        content_filter = SCK.SCContentFilter.alloc().initWithDisplay_excludingWindows_(displays[0], [])
        config = SCK.SCStreamConfiguration.alloc().init()
        config.setCapturesAudio_(True)
        config.setExcludesCurrentProcessAudio_(True)
        config.setSampleRate_(SAMPLE_RATE)
        config.setChannelCount_(CHANNELS)
        # We only want audio, but a stream cannot be audio-only: keep the video
        # side as cheap as possible (tiny frames, one per second) and ignore it.
        config.setWidth_(2)
        config.setHeight_(2)
        config.setMinimumFrameInterval_(CM.CMTimeMake(1, 1))
        config.setShowsCursor_(False)

        Handler = _handler_class()
        handler = Handler.alloc().init()
        handler._on_audio = on_audio
        handler._on_error = on_error

        stream = SCK.SCStream.alloc().initWithFilter_configuration_delegate_(content_filter, config, handler)
        for output_type in (SCK.SCStreamOutputTypeAudio, SCK.SCStreamOutputTypeScreen):
            added, err = stream.addStreamOutput_type_sampleHandlerQueue_error_(handler, output_type, None, None)
            if not added:
                raise SystemAudioUnavailable(f"could not attach the stream output: {err}")

        started = threading.Event()
        start_box: dict = {}

        def on_started(error):
            start_box["error"] = error
            started.set()

        stream.startCaptureWithCompletionHandler_(on_started)
        if not started.wait(timeout):
            raise SystemAudioUnavailable("ScreenCaptureKit did not start (timed out)")
        if start_box.get("error") is not None:
            if _error_is_permission(start_box["error"]):
                raise SystemAudioPermissionError()
            raise SystemAudioUnavailable(str(start_box["error"].localizedDescription()))

        self = cls()
        self._stream = stream
        self._handler = handler
        log.info("ScreenCaptureKit audio stream started")
        return self

    def stop(self) -> None:
        stream, self._stream = self._stream, None
        if stream is None:
            return
        if self._handler is not None:
            self._handler._on_audio = None
            self._handler._on_error = None
        finished = threading.Event()
        stream.stopCaptureWithCompletionHandler_(lambda error: finished.set())
        finished.wait(3.0)
        self._handler = None


_HANDLER_CLASS = None


def _handler_class():
    """The NSObject subclass ScreenCaptureKit calls back into (built lazily)."""
    global _HANDLER_CLASS
    if _HANDLER_CLASS is not None:
        return _HANDLER_CLASS
    import ScreenCaptureKit as SCK
    import objc
    from Foundation import NSObject

    protocols = [objc.protocolNamed("SCStreamOutput"), objc.protocolNamed("SCStreamDelegate")]

    class MeetingNotesStreamHandler(NSObject, protocols=protocols):
        _on_audio = None
        _on_error = None

        def stream_didOutputSampleBuffer_ofType_(self, stream, sample_buffer, output_type):
            if output_type != SCK.SCStreamOutputTypeAudio:
                return  # the (tiny) video frames are ignored
            callback = self._on_audio
            if callback is None:
                return
            try:
                callback(sample_buffer_to_array(sample_buffer))
            except Exception:  # noqa: BLE001 - never raise into ScreenCaptureKit
                log.debug("dropped an undecodable audio buffer", exc_info=True)

        def stream_didStopWithError_(self, stream, error):
            callback = self._on_error
            if callback is None:
                return
            if error is not None and _error_is_permission(error):
                callback(SystemAudioPermissionError())
            else:
                detail = str(error.localizedDescription()) if error is not None else "stopped"
                callback(RuntimeError(f"ScreenCaptureKit stream stopped: {detail}"))

    _HANDLER_CLASS = MeetingNotesStreamHandler
    return _HANDLER_CLASS
