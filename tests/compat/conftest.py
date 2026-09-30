"""Fixtures for the client-compatibility contract tests (see README.md)."""

from __future__ import annotations

import hashlib
import io
import zipfile
from types import SimpleNamespace

import pytest

import compat_support as support
from meeting_notes.server.app import create_app
from meeting_notes.transcribe.protocol import Segment

TOKEN = "compat-suite-token"


class _StubTranscriber:
    def transcribe(self, wav_path, track):
        return [Segment(start=0.0, end=0.4, text=f"hello from {track}", track=track)]


def _stub_factory(**_kwargs):
    return _StubTranscriber()


# Module scope on purpose: it sets MEETING_NOTES_TOKEN, which must not leak into other test modules.
@pytest.fixture(scope="module")
def compat_server(tmp_path_factory):
    """The CURRENT server on a real socket, with a token, one stub transcriber
    and a published (fake) Windows package so the manifest exists."""
    root = tmp_path_factory.mktemp("compat-server")
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("MEETING_NOTES_TOKEN", TOKEN)
        app = create_app(
            transcriber_factory=_stub_factory,
            data_root=str(root / "data"),
            media_root=str(root / "media"),
        )
        store = app.state.store
        package = store.root / "client" / "MeetingNotes-Windows.zip"
        package.parent.mkdir(parents=True, exist_ok=True)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("MeetingNotes.exe", b"not a real exe")
        package.write_bytes(buf.getvalue())
        mac_package = store.root / "client" / "MeetingNotes-macOS.zip"
        mac_buf = io.BytesIO()
        with zipfile.ZipFile(mac_buf, "w") as zf:
            zf.writestr("Meeting Notes.app/Contents/MacOS/Meeting Notes", b"not a real app")
        mac_package.write_bytes(mac_buf.getvalue())
        live = support.LiveServer(app)
        base_url = live.start()
        try:
            yield SimpleNamespace(
                base_url=base_url,
                token=TOKEN,
                app=app,
                store=store,
                package_sha=hashlib.sha256(buf.getvalue()).hexdigest(),
                mac_package_sha=hashlib.sha256(mac_buf.getvalue()).hexdigest(),
            )
        finally:
            live.stop()


@pytest.fixture(params=support.fixture_dirs(), ids=lambda p: support.folder_version(p))
def client(request):
    """One frozen historical recorder per parametrized run."""
    return support.load_client(request.param)
