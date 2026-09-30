"""Release checklist: a release cannot ship without its compatibility fixtures.

When you bump ``meeting_notes.__version__`` this fails until the version is in
``compat.RELEASES`` and a frozen copy of that client's network layer exists in
``clients/`` (README.md, "Adding a release").
"""

from __future__ import annotations

import compat_support as support
from meeting_notes import __version__
from meeting_notes.server import compat

REQUIRED_MODULES = ("wire", "api", "streamer", "queue", "update", "resample")


def test_current_version_is_registered_in_releases():
    assert __version__ in compat.RELEASES, (
        f"meeting_notes.__version__ = {__version__} is not in compat.RELEASES. Add it (and its "
        "tests/compat/clients/ fixture) as part of the release."
    )
    assert compat.RELEASES == sorted(compat.RELEASES, key=compat.version_key)
    assert len(set(compat.RELEASES)) == len(compat.RELEASES)


def test_every_supported_version_has_a_frozen_client_fixture():
    folders = {support.folder_version(p): p for p in support.fixture_dirs()}
    missing = [v for v in compat.supported_versions() if v not in folders]
    assert not missing, (
        f"no tests/compat/clients/ fixture for supported release(s) {missing}. Copy that release's "
        "client network modules (README.md, 'Adding a release')."
    )
    # min(5, available): the window never asks for more fixtures than releases exist.
    assert len(folders) >= min(compat.SUPPORTED_CLIENT_WINDOW, len(compat.released_versions()))
    for version in compat.supported_versions():
        client = support.load_client(folders[version])
        assert client.version == version, f"{folders[version].name}/__init__.py says {client.version}"
        for module in REQUIRED_MODULES:
            assert hasattr(client, module), f"{folders[version].name} is missing {module}.py"


def test_no_fixture_for_an_unknown_release():
    known = set(compat.released_versions())
    stray = [p.name for p in support.fixture_dirs() if support.folder_version(p) not in known]
    assert not stray, f"fixture folders not listed in compat.RELEASES: {stray}"
