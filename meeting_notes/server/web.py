"""HTML rendering for the browser-facing side of the server.

No template engine (Jinja2 is deliberately not a dependency here -- see
``docs/architecture.md``): every page is built by small Python functions that
return strings, escaping anything server-rendered with ``html.escape``. The
meetings library and the meeting view are thin shells around inline vanilla
JS that fetches the JSON API (``/v1/sessions``, ``/v1/sessions/{id}``) and
renders client-side: the JSON API is what stays correct as the number of
sessions grows, so the HTML side defers to it rather than re-deriving its own
(unpaginated, un-indexed) view of the same data.

``app.py`` only calls the ``render_*`` functions and wires them to routes; it
never builds HTML itself, so every string of markup lives in exactly one
place.

Visual system (see ``.impeccable/surfaces/meeting-notes-server-web-py.md``):
the category standard, played straight. A slim left sidebar, a calm list of
meetings and a Notion-style document view, in neutral greys with one blue
accent. Light, dark and system themes are all designed (``data-theme`` on
``<html>`` is rendered server-side from the ``appearance`` setting, so there is
no flash). Inter is self-hosted from ``/static/fonts``.
"""

from __future__ import annotations

from meeting_notes.server import settings as settings_mod  # noqa: F401 - reached as ``web.settings_mod`` by tests

# The renderers live in ``webpages/`` (one module per page or page group). This module stays the public
# import path: every name below is re-exported unchanged, private ones included (tests and app.py use them).

from .webpages.shell import (  # noqa: F401
    _ACTIVE_ALIASES, _ai_login_banner, _appearance_control, _APPEARANCE_VALUES, _APPEARANCES, _asset_href,
    _CSS_HREF, _icon, _ICON_PATHS, _ICONS_SRC, _NAV, _nav_items, _normalize_appearance, _page_title,
    _people_nav, set_people_nav, _shell, _SHELL_JS, _STATIC_DIR, stylesheet_text, _theme_color_metas,
    _THEME_COLORS, _update_banner, _update_footer_link
)
from .webpages.shared import (  # noqa: F401
    _CONFIRM_DIALOG_HTML, _JS_HELPERS, _JS_HELPERS_SRC, _NOTE_TYPE_TIP
)
from .webpages.home import (  # noqa: F401
    render_home_page, render_login_page
)
from .webpages.meetings import (  # noqa: F401
    render_transcriptions_page
)
from .webpages.trash import (  # noqa: F401
    render_trash_page
)
from .webpages.people import (  # noqa: F401
    _NP_DIALOG_HTML, _PEOPLE_JS, _PEOPLE_LINK_JS, _PEOPLE_LIST_JS, _PEOPLE_SORT_LABELS, _PERSON_JS,
    _PICK_DIALOG_HTML, _PPICK_DIALOG_HTML, render_people_page, render_person_page
)
from .webpages.recorders import (  # noqa: F401
    _RECORDERS_JS, render_recorders_page
)
from .webpages.queue import (  # noqa: F401
    _QUEUE_JS, render_queue_page
)
from .webpages.installer import (  # noqa: F401
    _RECORDINGS_GUARD_PS, render_client_installer, render_client_uninstaller, render_install_page,
    render_mac_installer, render_mac_uninstaller
)
from .webpages.ingestion import (  # noqa: F401
    _ingestion_chart_html, _INGESTION_JS, _INGESTION_STEPS, _SETTINGS_NAV_HTML, _SETTINGS_PAGE_ALIASES,
    _SETTINGS_PAGES
)
from .webpages.note_styles import (  # noqa: F401
    _note_style_item, _note_styles_html, _NOTE_STYLES_JS
)
from .webpages.gpu import (  # noqa: F401
    _GPU_HELPERS_JS, _GPU_RECORDERS_JS, _GPU_SETTINGS_JS
)
from .webpages.settings import (  # noqa: F401
    _BEAM_SIZE_TIP, _DIARIZATION_WITHOUT_HELPER_CHOICES, _ECHO_REMOVAL_CHOICES, _EFFORT_HINT, _EFFORT_LABELS,
    _effort_options, _GPU_HELPER_TIP, _IN_PERSON_DIARIZATION_CHOICES, _info_tip, _model_options,
    render_settings_page, _SETTINGS_IMMEDIATE_HTML, _SETTINGS_IMMEDIATE_JS
)
