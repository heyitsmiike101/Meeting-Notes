"""Unit tests for the faster-whisper backend, with no real model involved.

``faster_whisper`` is IS installed in this environment, but every model it
knows about lives on the Hugging Face Hub, which is unreachable here -- so no
test in this file may let a real ``WhisperModel`` get constructed. Instead we
inject a fake ``faster_whisper`` module into ``sys.modules`` before calling
into ``faster_whisper_backend``, which only imports the real package lazily,
inside ``load()`` (see the module docstring there). That's the seam these
tests exploit.

Every fixture that touches ``sys.modules`` goes through ``monkeypatch``, which
restores whatever was there before (nothing, since the import is lazy) once
the test ends -- so these fakes can never leak into a test file that runs
after this one and genuinely wants the real package.
"""

from __future__ import annotations

import sys
import types
from typing import List, Optional

import pytest

from meeting_notes.transcribe import faster_whisper_backend as fwb
from meeting_notes.transcribe.protocol import BackendUnavailableError, Segment


class FakeSegment:
    """Stands in for faster_whisper's segment objects: only these three
    attributes are ever read by the backend."""

    def __init__(self, start: float, end: float, text: str):
        self.start = start
        self.end = end
        self.text = text


class FakeInfo:
    """Stands in for faster_whisper's TranscriptionInfo."""

    def __init__(self, duration: float = 10.0, duration_after_vad: float = 10.0):
        self.duration = duration
        self.duration_after_vad = duration_after_vad


class FakeWhisperModel:
    """Fake faster_whisper.WhisperModel.

    Records every constructor call and every transcribe() call (as plain
    dicts) on the class itself, so tests can assert on exact kwarg plumbing
    without needing to reach into instances they didn't keep a handle on.
    Class-level state is reset by the ``reset_fake_model`` autouse fixture
    below, so nothing here can bleed from one test into the next.
    """

    # Set by a test to make __init__ raise when it's called with
    # local_files_only == this value -- used to script the offline-first
    # retry path without a real cache miss.
    fail_local_only: Optional[bool] = None

    segments_script: List[FakeSegment] = []
    info: FakeInfo = FakeInfo()

    init_calls: List[dict] = []
    transcribe_calls: List[dict] = []
    instances: List["FakeWhisperModel"] = []

    def __init__(self, model_size, **kwargs):
        call = {"model_size": model_size, **kwargs}
        FakeWhisperModel.init_calls.append(call)
        if (
            FakeWhisperModel.fail_local_only is not None
            and kwargs.get("local_files_only") == FakeWhisperModel.fail_local_only
        ):
            raise RuntimeError("simulated model load failure")
        FakeWhisperModel.instances.append(self)

    def transcribe(self, audio_path, **kwargs):
        FakeWhisperModel.transcribe_calls.append({"audio_path": audio_path, **kwargs})
        # A real generator (not a list) so tests can prove it was actually
        # iterated by the backend rather than materialized elsewhere.
        gen = (seg for seg in FakeWhisperModel.segments_script)
        self.last_generator = gen
        return gen, FakeWhisperModel.info


@pytest.fixture(autouse=True)
def reset_fake_model():
    FakeWhisperModel.fail_local_only = None
    FakeWhisperModel.segments_script = []
    FakeWhisperModel.info = FakeInfo()
    FakeWhisperModel.init_calls = []
    FakeWhisperModel.transcribe_calls = []
    FakeWhisperModel.instances = []
    yield


@pytest.fixture
def fake_faster_whisper(monkeypatch):
    """Injects a fake faster_whisper module for the duration of one test.

    monkeypatch.setitem removes the sys.modules entry again on teardown
    (there wasn't one before, since the real import is lazy), which is what
    guarantees this fake can't affect any other test even though the real
    faster-whisper package is installed alongside it.
    """
    module = types.ModuleType("faster_whisper")
    module.WhisperModel = FakeWhisperModel
    monkeypatch.setitem(sys.modules, "faster_whisper", module)
    return module


# -- 1. compute_type resolution ----------------------------------------------


def test_resolve_compute_type_defaults_to_int8_on_cpu():
    # faster-whisper's own default ("default") means float32 on CPU, which is
    # correct but needlessly slow -- int8 is the documented CPU recommendation
    # and is what makes small.en/turbo usable without a GPU. If this regresses
    # to "default" or "float32", every CPU transcription silently gets slower.
    assert fwb._resolve_compute_type("auto", "cpu") == "int8"


def test_resolve_compute_type_uses_float16_on_cuda():
    # float16 is only safe once we know we're actually on CUDA -- picking it
    # blindly would break on CPU, where it's both slower than int8 and not
    # universally supported.
    assert fwb._resolve_compute_type("auto", "cuda") == "float16"


def test_resolve_compute_type_passes_through_explicit_value():
    # A user who explicitly asks for float32 (e.g. for debugging accuracy
    # regressions) must get exactly that, not have "auto" logic override it.
    assert fwb._resolve_compute_type("float32", "cpu") == "float32"


# -- 2. kwarg plumbing into WhisperModel(...) --------------------------------


def test_model_construction_kwargs_are_plumbed_through(fake_faster_whisper, tmp_path):
    transcriber = fwb.FasterWhisperTranscriber(
        model_size="small.en",
        device="cpu",  # explicit, so this test doesn't depend on whether a
        # GPU happens to be visible in whatever environment runs it
        compute_type="auto",
        threads=4,
        download_root="/tmp/model-cache",
        local_files_only=True,
    )
    transcriber.transcribe(tmp_path / "mic.wav", "mic")

    assert len(FakeWhisperModel.init_calls) == 1
    call = FakeWhisperModel.init_calls[0]
    assert call["model_size"] == "small.en"
    assert call["device"] == "cpu"
    assert call["compute_type"] == "int8"  # "auto" resolved for cpu
    assert call["cpu_threads"] == 4
    assert call["download_root"] == "/tmp/model-cache"
    assert call["local_files_only"] is True


# -- 3. vad_filter is ALWAYS passed explicitly -------------------------------


def test_vad_filter_defaults_true_and_is_always_explicit(fake_faster_whisper, tmp_path):
    # This is the most important plumbing assertion in this file: vad_filter
    # defaulted to False in faster-whisper 1.0.x and flipped to True in 1.2.
    # If the backend ever stopped passing it explicitly and relied on the
    # library default instead, upgrading faster-whisper would silently change
    # behaviour under us -- worse, downgrading it would mean decoding hours of
    # silence and inviting hallucinated text over dead air, with no code
    # change on our side to explain why transcripts got slower and worse.
    transcriber = fwb.FasterWhisperTranscriber(device="cpu")
    transcriber.transcribe(tmp_path / "mic.wav", "mic")

    call = FakeWhisperModel.transcribe_calls[0]
    assert "vad_filter" in call
    assert call["vad_filter"] is True


def test_vad_filter_false_is_forwarded_faithfully(fake_faster_whisper, tmp_path):
    transcriber = fwb.FasterWhisperTranscriber(device="cpu", vad_filter=False)
    transcriber.transcribe(tmp_path / "mic.wav", "mic")

    call = FakeWhisperModel.transcribe_calls[0]
    assert "vad_filter" in call
    assert call["vad_filter"] is False


# -- 4. condition_on_previous_text / beam_size / language / initial_prompt --


def test_condition_on_previous_text_defaults_false_and_is_forwarded(
    fake_faster_whisper, tmp_path
):
    # Off by default: Whisper's context carry-over is the known mechanism
    # behind runaway repetition loops, which in a meeting transcript is worse
    # than the modest coherence it buys. Confirm the default AND that turning
    # it on actually reaches transcribe() -- either could drift independently.
    transcriber = fwb.FasterWhisperTranscriber(device="cpu")
    transcriber.transcribe(tmp_path / "mic.wav", "mic")
    assert FakeWhisperModel.transcribe_calls[0]["condition_on_previous_text"] is False

    FakeWhisperModel.transcribe_calls.clear()
    transcriber_on = fwb.FasterWhisperTranscriber(
        device="cpu", condition_on_previous_text=True
    )
    transcriber_on.transcribe(tmp_path / "mic.wav", "mic")
    assert FakeWhisperModel.transcribe_calls[0]["condition_on_previous_text"] is True


def test_beam_size_language_and_initial_prompt_are_forwarded(fake_faster_whisper, tmp_path):
    transcriber = fwb.FasterWhisperTranscriber(
        device="cpu",
        language="en",
        beam_size=3,
        initial_prompt="Weekly sync, attendees: Ana, Bo.",
    )
    transcriber.transcribe(tmp_path / "mic.wav", "mic")

    call = FakeWhisperModel.transcribe_calls[0]
    assert call["beam_size"] == 3
    assert call["language"] == "en"
    assert call["initial_prompt"] == "Weekly sync, attendees: Ana, Bo."


# -- 5. lazy generator consumption + progress reporting ----------------------


def test_transcribe_consumes_generator_and_reports_monotonic_progress(
    fake_faster_whisper, tmp_path
):
    # model.transcribe() returns a LAZY generator in real faster-whisper --
    # decoding only happens as it's iterated. If the backend ever stopped
    # iterating it (e.g. returned it unconsumed, or wrapped it in something
    # that doesn't force evaluation), no audio would actually get decoded and
    # transcripts would come back empty with no error.
    FakeWhisperModel.segments_script = [
        FakeSegment(0.0, 2.0, "one"),
        FakeSegment(2.0, 10.0, "two"),
        FakeSegment(10.0, 20.0, "three"),
    ]
    FakeWhisperModel.info = FakeInfo(duration=20.0, duration_after_vad=5.0)

    progress_calls = []

    def on_progress(track, fraction, speech_duration):
        progress_calls.append((track, fraction, speech_duration))

    transcriber = fwb.FasterWhisperTranscriber(device="cpu", on_progress=on_progress)
    transcriber.transcribe(tmp_path / "mic.wav", "mic")

    # Proof of consumption: the exact generator object model.transcribe()
    # handed back is now exhausted, which is only possible if the backend's
    # for-loop actually ran it to completion.
    instance = FakeWhisperModel.instances[0]
    with pytest.raises(StopIteration):
        next(instance.last_generator)

    assert all(track == "mic" for track, _, _ in progress_calls)
    fractions = [f for _, f, _ in progress_calls]
    assert fractions[0] == 0.0
    assert fractions[-1] == 1.0
    assert fractions == sorted(fractions)  # non-decreasing throughout

    # The reported "how much audio is there" figure must come from the
    # VAD-trimmed duration, not the raw file length -- that's what lets users
    # tell "this is working" from "this is hung" on a mostly-silent track.
    assert all(speech == 5.0 for _, _, speech in progress_calls)


# -- 6. segment conversion ----------------------------------------------------


def test_segments_are_converted_and_blank_ones_dropped(fake_faster_whisper, tmp_path):
    FakeWhisperModel.segments_script = [
        FakeSegment(0.0, 1.5, "  Hello there  "),
        FakeSegment(1.5, 1.6, ""),  # pure silence artifact: must be dropped
        FakeSegment(1.6, 1.7, "   "),  # whitespace-only: must be dropped
        FakeSegment(2.0, 3.25, "Second line.\n"),
    ]
    FakeWhisperModel.info = FakeInfo(duration=3.25, duration_after_vad=3.25)

    transcriber = fwb.FasterWhisperTranscriber(device="cpu")
    segments = transcriber.transcribe(tmp_path / "system.wav", "system")

    assert segments == [
        Segment(start=0.0, end=1.5, text="Hello there", track="system"),
        Segment(start=2.0, end=3.25, text="Second line.", track="system"),
    ]


# -- 7. offline-first loading --------------------------------------------------


def test_local_files_only_default_retries_online_on_cache_miss(
    fake_faster_whisper, tmp_path
):
    # WhisperModel always routes through huggingface_hub, which contacts the
    # Hub even for a fully cached model unless local_files_only is set -- so
    # without the offline-first-then-fallback dance, every transcription would
    # need working network, and a plane/hotel-wifi session would hang or fail
    # for no reason. Simulate a genuine cache miss: the first (offline)
    # attempt fails, and the backend must retry allowing a download rather
    # than giving up.
    FakeWhisperModel.fail_local_only = True  # the local_files_only=True attempt fails
    transcriber = fwb.FasterWhisperTranscriber(device="cpu")  # local_files_only=True default
    transcriber.transcribe(tmp_path / "mic.wav", "mic")

    assert len(FakeWhisperModel.init_calls) == 2
    assert FakeWhisperModel.init_calls[0]["local_files_only"] is True
    assert FakeWhisperModel.init_calls[1]["local_files_only"] is False


def test_explicit_local_files_only_false_does_not_retry_on_failure(
    fake_faster_whisper, tmp_path
):
    # When the caller has already opted out of offline-first (local_files_only
    # explicitly False), a failure is a real failure -- there is no "more
    # online" fallback to retry into, and retrying identically would just
    # double the wait before reporting the same error.
    FakeWhisperModel.fail_local_only = False  # the only attempt made fails
    transcriber = fwb.FasterWhisperTranscriber(device="cpu", local_files_only=False)

    with pytest.raises(BackendUnavailableError):
        transcriber.transcribe(tmp_path / "mic.wav", "mic")

    assert len(FakeWhisperModel.init_calls) == 1
    assert FakeWhisperModel.init_calls[0]["local_files_only"] is False


# -- 8. missing dependency -----------------------------------------------------


def test_missing_faster_whisper_raises_backend_unavailable_with_install_hint(
    monkeypatch, tmp_path
):
    # Setting sys.modules["faster_whisper"] to None is the standard way to
    # force `import faster_whisper` to raise ImportError deterministically,
    # without needing the package to actually be absent (it IS installed in
    # this environment). monkeypatch.setitem restores the real state after.
    monkeypatch.setitem(sys.modules, "faster_whisper", None)

    transcriber = fwb.FasterWhisperTranscriber(device="cpu")
    with pytest.raises(BackendUnavailableError) as excinfo:
        transcriber.transcribe(tmp_path / "mic.wav", "mic")

    # A user hitting this needs to know the actual fix, not a bare
    # "ModuleNotFoundError: No module named 'faster_whisper'".
    assert "pip install" in str(excinfo.value)
    assert "faster-whisper" in str(excinfo.value)


# -- 9. warn_if_language_unsupported ------------------------------------------


def test_warn_if_language_unsupported_flags_english_only_model_with_other_language():
    # faster-whisper only logs through the logging module and decodes as
    # English anyway -- from a CLI that's invisible, so this warning is the
    # only thing standing between a user and confident, fluent, wrong text.
    transcriber = fwb.FasterWhisperTranscriber(
        device="cpu", model_size="base.en", language="fr"
    )
    warning = transcriber.warn_if_language_unsupported()
    assert warning is not None
    assert "base.en" in warning
    assert "fr" in warning


@pytest.mark.parametrize("language", [None, "en"])
def test_warn_if_language_unsupported_silent_for_english_only_model_in_english(language):
    transcriber = fwb.FasterWhisperTranscriber(
        device="cpu", model_size="base.en", language=language
    )
    assert transcriber.warn_if_language_unsupported() is None


def test_warn_if_language_unsupported_silent_for_multilingual_model():
    # large-v3-turbo is multilingual (doesn't end in ".en"), so a non-English
    # --language must not trip the English-only warning.
    transcriber = fwb.FasterWhisperTranscriber(
        device="cpu", model_size="large-v3-turbo", language="fr"
    )
    assert transcriber.warn_if_language_unsupported() is None


# -- 10. model reuse across tracks --------------------------------------------


def test_model_is_loaded_once_and_reused_across_tracks(fake_faster_whisper, tmp_path):
    # Loading is the expensive part (seconds to tens of seconds, plus real
    # RAM) -- loading it again per track would double meeting-processing time
    # for no benefit, since the same model transcribes both the mic and
    # system tracks.
    transcriber = fwb.FasterWhisperTranscriber(device="cpu")
    transcriber.transcribe(tmp_path / "mic.wav", "mic")
    transcriber.transcribe(tmp_path / "system.wav", "system")

    assert len(FakeWhisperModel.init_calls) == 1
    assert len(FakeWhisperModel.transcribe_calls) == 2
    assert FakeWhisperModel.transcribe_calls[0]["audio_path"] == str(tmp_path / "mic.wav")
    assert FakeWhisperModel.transcribe_calls[1]["audio_path"] == str(tmp_path / "system.wav")
