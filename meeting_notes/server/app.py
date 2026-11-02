"""The FastAPI application: the network-facing half of the server.

``create_app`` is a factory rather than a module-level ``app`` object because
tests need an isolated store (temp directory) and an injectable stub
transcriber (no real Whisper model can run in most test environments, this
one included -- no network to fetch weights). ``app.py:app`` at the bottom is
the module-level instance uvicorn actually serves in Docker, wired from
environment variables.
"""

from __future__ import annotations

from urllib.parse import urlencode
import logging
import os
import shutil  # noqa: F401 - tests patch meeting_notes.server.app.shutil
import threading
from typing import Callable, Dict, Optional

import httpx  # noqa: F401 - tests patch meeting_notes.server.app.httpx
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse

from . import ai_health
from . import auth
from . import compat
from . import live as live_mod
from . import live_remote as live_remote_mod
from . import names as names_mod
from . import retention as retention_mod
from . import voice_recheck as voice_recheck_mod
from . import settings as settings_mod
from . import store as store_mod
from . import updates as updates_mod
from . import web
from .agent import install_agent_access
from . import compute as compute_mod
from . import compute_api
from . import helper_pack as helper_pack_mod
from . import helper_pack_api
from .client_logs import MAX_BYTES as _CLIENT_LOG_MAX
from . import client_packages
from . import mobile_stream as mobile_stream_mod
from . import mobile_upload as mobile_upload_mod
from .mobile_web import install_mobile_web
from .jobs import DiarizerFactory, JobQueue, TranscriberFactory
from .notion import NotionSync
from .recorder_recordings import install_recorder_recordings
from .recorders import install_recorders
from . import people as people_mod
from .people_api import install_people
from .splitmerge_api import install_split_merge
from .intro_names_api import install_intro_names
from .ai_api import bridge_verifier, install_ai
from .export_api import install_exports
from .installer_api import UPDATE_INSTALLER_MAC_PATH, UPDATE_INSTALLER_PS1_PATH, install_installers  # noqa: F401 - re-exported
from .notes_api import install_notes
from .notion_routes import install_notion
from .ops_api import install_ops
from .pages_api import install_pages
from .recording_api import install_recording
from .route_helpers import _safe_next, ai_enabled_reader
from .sessions_api import install_session_actions, install_sessions
from .settings_api import install_settings_json, install_settings_pages
from .shell_api import LOGIN_COOKIE_MAX_AGE, install_shell  # noqa: F401 - LOGIN_COOKIE_MAX_AGE is re-exported
from .trash_api import install_trash

logger = logging.getLogger("meeting_notes.server.app")
try:
    _MAX_RECORDING_BYTES = int(os.environ.get("MEETING_NOTES_MAX_UPLOAD_BYTES", str(8 * 1024**3)))
except (TypeError, ValueError):
    _MAX_RECORDING_BYTES = 8 * 1024**3


def _settings_transcriber_factory(store: store_mod.Store) -> TranscriberFactory:
    """Build the production transcriber factory: reads model/beam_size from
    the persisted settings (settings.py) at CALL time, not at server-start
    time.

    ``settings.py``'s own default already falls back to ``MEETING_NOTES_MODEL``
    when no settings.json exists yet, so this single factory covers both "an
    operator set the env var and never touched the settings page" and "an
    operator changed the model through the settings page" -- see
    ``create_app`` for the one case this does NOT cover (no model configured
    anywhere), which gets no factory at all rather than one that would fail.

    Reading settings at call time (not once, up front) is what lets a model
    or beam-size change made through the settings page or
    ``PUT /v1/settings`` take effect on the very next job or live-preview
    utterance -- no restart needed. (``LivePreview`` still caches the
    *instance* it builds across calls, which is why
    ``live_preview.reset_transcriber()`` also has to be called after a save;
    ``JobQueue`` never caches an instance, so jobs pick up a change for free.)
    """
    device = os.environ.get("MEETING_NOTES_DEVICE", "auto")

    def factory(**kwargs):
        # Imported lazily, at call time: importing meeting_notes.server.app
        # must not require faster-whisper to be installed at all, the same
        # seam meeting_notes.transcribe.protocol uses.
        from ..transcribe.protocol import get_transcriber

        current = settings_mod.load_settings(store.root)
        opts = {"model_size": current.model, "device": device, "beam_size": current.beam_size}
        # Correct spellings of people's names (from saved participant edits)
        # bias recognition. Empty glossary: no extra argument at all. The live
        # preview caches its instance, so it picks changes up on its next rebuild.
        bias = names_mod.hotwords(store.root)
        if bias:
            opts["hotwords"] = bias
        opts.update(kwargs)
        return get_transcriber("faster-whisper", **opts)

    return factory


def _settings_diarizer_factory(store: store_mod.Store) -> DiarizerFactory:
    """Lazy, cached production diarizer controlled by persisted settings."""
    cached = {"key": None, "instance": None}

    def factory():
        current = settings_mod.load_settings(store.root)
        if not current.diarization_enabled:
            return None
        device = os.environ.get("MEETING_NOTES_DIARIZATION_DEVICE", "cpu")
        token = os.environ.get("HUGGINGFACE_TOKEN") or os.environ.get("HF_TOKEN")
        key = (
            current.diarization_model,
            device,
            current.diarization_min_speakers,
            current.diarization_max_speakers,
        )
        if cached["key"] != key:
            from ..transcribe.pyannote_backend import PyannoteDiarizer

            cached["instance"] = PyannoteDiarizer(
                model=current.diarization_model,
                token=token,
                device=device,
                min_speakers=current.diarization_min_speakers,
                max_speakers=current.diarization_max_speakers,
            )
            cached["key"] = key
        return cached["instance"]

    return factory


# Apps whose background workers are still running: id -> stop_workers (one entry in production).
_UNSTOPPED: Dict[int, Callable[[], None]] = {}


def create_app(
    transcriber_factory: Optional[TranscriberFactory] = None,
    diarizer_factory: Optional[DiarizerFactory] = None,
    data_root: Optional[str] = None,
    media_root: Optional[str] = None,
    enable_mcp: bool = True,
    notion_options: Optional[dict] = None,
    update_checker: Optional[updates_mod.UpdateChecker] = None,
    ai_login_verify: Optional[Callable[[str], Optional[dict]]] = None,
) -> FastAPI:
    store = store_mod.Store(data_root, media_root)
    client_packages.configure(store.root)
    # The "update available" check (updates.py). Off under pytest unless a test
    # passes its own checker (with a fake fetch), so no test touches the network.
    if update_checker is None:
        update_checker = updates_mod.UpdateChecker(
            enabled=False if "PYTEST_CURRENT_TEST" in os.environ else None
        )
    updates_mod.set_active(update_checker)
    explicit_factory = transcriber_factory is not None
    if not explicit_factory:
        # A model configured only via settings.json (no MEETING_NOTES_MODEL,
        # saved later through the settings page) is treated the same as one
        # configured via the env var: settings.py's default already reads the
        # env var, so "no model anywhere" and "settings.model is empty" are
        # the same condition. See settings.py's _default_model for why this
        # deliberately does NOT fall back to a curated default model on its
        # own -- that would silently turn transcription on for an operator
        # who never asked for it.
        bootstrap_settings = settings_mod.load_settings(store.root)
        transcriber_factory = (
            _settings_transcriber_factory(store) if bootstrap_settings.model else None
        )

    # uvicorn configures only its own loggers; ours propagate to a root
    # logger with no handler, so Python's last-resort handler drops anything
    # below WARNING. That hid every "retention: deleted audio for ..." line
    # from `docker compose logs` on a real run. Wire the package's logger to
    # stderr at INFO once, and only if nobody (a test, an embedding app) has
    # already configured it.
    package_logger = logging.getLogger("meeting_notes")
    if not package_logger.handlers and not logging.getLogger().handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(levelname)s:     %(name)s: %(message)s"))
        package_logger.addHandler(handler)
        package_logger.setLevel(logging.INFO)

    live_factory = transcriber_factory
    if not explicit_factory and transcriber_factory is not None:
        # The live preview skips word-level timestamps: they add an alignment
        # pass per utterance and the preview shows each utterance's own bounds.
        _base_factory = transcriber_factory

        def live_factory():
            # Lighter live model with greedy decoding (see live.py); if it can't be loaded (not in the
            # model cache on an offline box, say), fall back to the configured model so live still works.
            current = settings_mod.load_settings(store.root)  # read now: a Settings change applies on the next rebuild
            choice = live_mod.live_model_choice(current)
            live_beam = max(1, int(getattr(current, "live_beam_size", live_mod.LIVE_BEAM_SIZE) or 1))
            if choice:
                try:
                    lighter = _base_factory(word_timestamps=False, model_size=choice, beam_size=live_beam)
                    load = getattr(lighter, "load", None)
                    if callable(load):
                        load()  # the model loads lazily: load it now so a missing model falls back here
                    return lighter
                except Exception:  # noqa: BLE001
                    logger.warning("live preview model %r unavailable; using the configured model", choice,
                                   exc_info=True)
            return _base_factory(word_timestamps=False, beam_size=live_beam)
    # Live sentences go to a connected GPU helper first when allowed (live_remote.py), else this server's CPU.
    live_remote = live_remote_mod.LiveRemote(lambda: settings_mod.load_settings(store.root))
    live_preview = live_mod.LivePreview(live_factory, remote=live_remote)
    live_sessions: dict = {}
    live_sessions_lock = threading.Lock()
    # A live websocket can close just before the recorder's queued finalize
    # request arrives. Keep a rename made from the web UI long enough for that
    # finalize request to use it as the authoritative meeting name.
    live_name_overrides: dict = {}
    live_people_overrides: dict = {}
    if diarizer_factory is None:
        diarizer_factory = _settings_diarizer_factory(store)
    # GPU helpers (compute.py): key registry + offer/lease logic. Inert until a helper registers and the
    # compute_offload_enabled setting is on.
    compute_registry = compute_mod.HelperRegistry(store.root)
    # The files helpers install from (helper_pack.py): prepared on demand from Settings, served only to helpers.
    helper_pack = helper_pack_mod.HelperPackBuilder(
        store.root, settings_provider=lambda: settings_mod.load_settings(store.root)
    )
    compute_registry.pack_version_provider = helper_pack.current_version
    compute_registry.on_helper_dropped = live_remote.close_helper  # a revoked or replaced key closes its live socket
    compute_registry.live_stats_provider = live_remote.stats
    job_queue = JobQueue(store, transcriber_factory, diarizer_factory, compute=compute_registry)
    job_queue.resume_interrupted()
    compute_sweeper = compute_mod.ComputeSweeper(compute_registry)
    retention_worker = retention_mod.RetentionWorker(store)
    voice_recheck = voice_recheck_mod.VoiceRecheckWorker(store)
    # Copies finished notes into Notion on its own thread (notion.py). Options
    # (transport, sleep, ...) exist for tests; production passes none.
    notion = NotionSync(store, **(notion_options or {}))
    notion.resume_interrupted()
    # The workers this app starts here, in start order; ``stop_workers`` stops them (the sweeper first, then live
    # sockets, then the rest). The update and login checkers start in the lifespan, the mobile worker in its
    # installer.
    workers = [job_queue, compute_sweeper, retention_worker, voice_recheck, notion]
    for worker in workers:
        worker.start()

    stopped = threading.Event()

    def stop_workers() -> None:
        """Stop this app's background threads (once). The lifespan's shutdown calls it; so do the tests, for an
        app whose TestClient never ran a shutdown -- leaked workers from earlier tests waking up on their
        timeouts once starved a late test of the GIL for minutes on macOS."""
        if stopped.is_set():
            return
        stopped.set()
        _UNSTOPPED.pop(id(stopped), None)
        compute_sweeper.stop()
        live_remote.close_all()
        for worker in workers:  # the sweeper is already stopped: a no-op
            worker.stop()
        mobile = getattr(app.state, "mobile_upload", None)
        if mobile is not None:
            mobile.stop()

    _UNSTOPPED[id(stopped)] = stop_workers

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        update_checker.start()
        login_checker.start()
        try:
            # install_agent_access (below, once the app exists) parks its context
            # on app.state: it runs the MCP session manager for the app's life.
            agent_cm = getattr(_app.state, "agent_lifespan", None)
            if agent_cm is not None:
                async with agent_cm:
                    yield
            else:
                yield
        finally:
            # Runs on a clean shutdown. Both worker threads are also daemon
            # threads, so an unclean process exit doesn't hang either way --
            # this just lets an in-flight job (or sweep) finish (up to the
            # timeout) instead of being cut off mid-write.
            stop_workers()
            update_checker.stop()
            login_checker.stop()

    app = FastAPI(title="meeting-notes server", lifespan=lifespan)
    # Exposed for tests and for anything that wants to reach past the routes
    # (e.g. to inspect job_queue directly) without a second construction path.
    app.state.store = store
    app.state.stop_workers = stop_workers
    app.state.live_preview = live_preview
    app.state.job_queue = job_queue
    app.state.compute_registry = compute_registry
    app.state.live_remote = live_remote
    app.include_router(compute_api.router)
    app.state.helper_pack = helper_pack
    app.include_router(helper_pack_api.make_router(store.root, helper_pack))
    app.state.notion = notion
    app.state.retention_worker = retention_worker
    app.state.voice_recheck = voice_recheck
    app.state.transcriber_factory = transcriber_factory
    app.state.live_sessions = live_sessions
    app.state.agent_lifespan = install_agent_access(
        app,
        store=store,
        base_url_getter=lambda: settings_mod.load_settings(store.root).server_address,
        enable_mcp=enable_mcp,
        notion=notion,
    )

    # Live recorder presence + remote control (in memory; only the seen registry persists; recorders.py).
    recorder_hub = install_recorders(app, data_root=store.root)
    # Recordings saved on a recorder, joined with the server's own knowledge (recorder_recordings.py).
    install_recorder_recordings(app, recorder_hub, store)
    # Stream tickets + live fence for the mobile recorder's optional live transcript (mobile_stream.py).
    mobile_stream = mobile_stream_mod.install_mobile_stream(app, store=store)
    app.state.mobile_upload = mobile_upload_mod.install_mobile_upload(
        app, store=store, job_queue=job_queue, data_root=store.root)
    install_mobile_web(app, store=store)

    # Recorder version reporting and the client compatibility window (compat.py).
    app.add_middleware(compat.ClientVersionMiddleware)

    class PeopleNavMiddleware:
        """Tell the page renderer whether to show the People nav item (voice profiles on) for this request."""

        def __init__(self, inner):
            self.inner = inner

        async def __call__(self, scope, receive, send):
            if scope["type"] == "http" and not str(scope.get("path", "")).startswith(("/static/", "/v1/")):
                web.set_people_nav(people_mod.is_enabled(store.root))
            await self.inner(scope, receive, send)

    app.add_middleware(PeopleNavMiddleware)

    @app.exception_handler(compat.ClientTooOld)
    async def _client_too_old(_request: Request, exc: compat.ClientTooOld):
        return JSONResponse(exc.payload(), status_code=426)

    _bridge_verify = bridge_verifier(store)

    # The AI login check (ai_health.py). Off under pytest unless a test passes its own verify.
    login_checker = ai_health.LoginChecker(
        store.root,
        lambda: settings_mod.load_settings(store.root).ai_provider,
        ai_login_verify or _bridge_verify,
        enabled=ai_login_verify is not None or "PYTEST_CURRENT_TEST" not in os.environ,
    )
    app.state.ai_login_checker = login_checker
    ai_health.set_active(store.root)  # the page banner reads it

    def _ai_login_failed(error: str) -> bool:
        """A job failed on the provider's login: remember it (holds the queue) and check again soon.
        False when the provider has no login (the job then fails as usual)."""
        provider = settings_mod.load_settings(store.root).ai_provider
        if provider not in ai_health.LOGIN_PROVIDERS:
            return False
        ai_health.mark_expired(store.root, provider, error)
        login_checker.wake()
        return True

    def _ai_login_works() -> None:
        ai_health.mark_ok(store.root, settings_mod.load_settings(store.root).ai_provider)

    app.state.ai_login_failed = _ai_login_failed

    @app.exception_handler(auth.WebAuthRequired)
    async def _web_auth_required(request: Request, _exc: auth.WebAuthRequired):
        # An HTML route with no valid cookie/header lands on a sign-in form,
        # not the bare JSON 401 an API caller would get from require_token.
        # It comes back to the page it asked for afterwards (e.g. the phone recorder at /record/).
        target = request.url.path + (("?" + request.url.query) if request.url.query else "")
        if request.method == "GET" and _safe_next(target) not in ("", "/"):
            return RedirectResponse(url="/login?" + urlencode({"next": target}), status_code=303)
        return RedirectResponse(url="/login", status_code=303)

    install_recording(
        app, store=store, live_preview=live_preview, live_sessions=live_sessions,
        live_sessions_lock=live_sessions_lock, live_name_overrides=live_name_overrides,
        live_people_overrides=live_people_overrides, compute_registry=compute_registry, job_queue=job_queue,
        mobile_stream=mobile_stream, _max_recording_bytes=lambda: _MAX_RECORDING_BYTES,
    )

    install_shell(app, store=store)

    _ai_enabled = ai_enabled_reader(store)

    # Split / combine meetings (routes, plus the bridge side of the optional AI
    # topic-shift job) live in splitmerge_api.py; everything is web/admin token only.
    split_ai = install_split_merge(
        app, store=store, live_sessions=live_sessions,
        live_sessions_lock=live_sessions_lock, ai_enabled=_ai_enabled,
    )

    # Voice profiles ("People"): /v1/people*, admin token only, 404 while the feature is off.
    install_people(app, store=store, after_change=lambda: voice_recheck.request_run("person"))

    # Naming unnamed voices from introductions (the bridge's third job kind, offered last): the queue belongs to the
    # job queue, which enqueues after a transcript's speaker labels are recorded; the routes are the bridge's side.
    intro_names = install_intro_names(
        app, store=store, queue=job_queue.intro_names, after_change=lambda: voice_recheck.request_run("person"))

    install_pages(app, store=store)

    install_installers(app, store=store)

    install_exports(app, store=store)

    install_settings_pages(
        app, store=store, live_preview=live_preview, compute_registry=compute_registry,
        helper_pack=helper_pack, retention_worker=retention_worker, voice_recheck=voice_recheck,
        notion=notion,
    )

    install_sessions(
        app, store=store, live_sessions=live_sessions, live_sessions_lock=live_sessions_lock,
        live_name_overrides=live_name_overrides, live_people_overrides=live_people_overrides, notion=notion,
        recorder_hub=recorder_hub,
    )

    install_trash(app, store=store)

    install_session_actions(app, store=store, job_queue=job_queue)

    install_notes(
        app, store=store, live_preview=live_preview, _ai_login_failed=_ai_login_failed,
        _ai_login_works=_ai_login_works, split_ai=split_ai, intro_names=intro_names,
    )

    install_ai(app, store=store, login_checker=login_checker)

    install_ops(app, store=store, _client_log_max=lambda: _CLIENT_LOG_MAX)

    install_notion(app, store=store, notion=notion)

    install_settings_json(
        app, update_checker=update_checker, store=store, live_preview=live_preview,
        retention_worker=retention_worker, voice_recheck=voice_recheck, notion=notion,
    )

    return app


# The module-level instance `uvicorn meeting_notes.server.app:app` serves in
# Docker. Configuration comes entirely from the environment (MEETING_NOTES_*)
# since there's no CLI to pass a factory through at that point.
app = create_app()
