"""The Processing section of the main window: where each recorded meeting is in upload and transcription.

It mirrors the server's "Processing status" checklist (Upload audio, Transcribe recording, Transcript
ready) in a compact form: one row per queued meeting (oldest first, at most ``MAX_ROWS``), each with a
status badge and a thin progress bar, and the first (current) meeting also shows the three-step
checklist. The panel is hidden when nothing is waiting, so it takes no space.

``describe_entry`` is the pure part (queue entry -> wording and states); the widgets only draw it.
Colours come from the theme tokens at paint time, so a theme change only needs a repaint.
"""

from __future__ import annotations

import json
import math
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from PySide6.QtCore import QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFontMetrics, QPainter
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from . import theme

MAX_ROWS = 3
PULSE_MS = 80
PULSE_PERIOD_SEC = 1.6

# dot / tone names: "ok" (green), "active" (accent, pulsing), "error" (red), "warn" (amber), "idle" (grey)
_DOT_TOKEN = {
    "ok": "ok_text",
    "active": "accent",
    "error": "danger",
    "warn": "warn_icon",
    "idle": "disabled",
}


# -- pure description of a queue entry -------------------------------------------


def _pct(value) -> int:
    try:
        return max(0, min(100, int(round(float(value or 0)))))
    except (TypeError, ValueError):
        return 0


def _clock(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%I:%M %p").lstrip("0")


def is_done(entry: dict) -> bool:
    """Fully uploaded and transcribed (nothing left to show)."""
    return (
        entry.get("status") != "failed"
        and entry.get("upload_state") == "complete"
        and entry.get("transcription_state") == "complete"
    )


def _opt_pct(value) -> Optional[int]:
    return None if value is None else _pct(value)


def transcribe_view(entry: dict):
    """(stage, transcribe_pct, speakers_pct) for an entry; speakers_pct is None when the job has no speaker step.

    Older servers send neither field, so transcribe_pct falls back to the overall percent.
    """
    speakers = _opt_pct(entry.get("speakers_percent"))
    stage = "speakers" if (entry.get("transcription_stage") == "speakers" and speakers is not None) else None
    tr = _opt_pct(entry.get("transcribe_percent"))
    if tr is None:
        tr = _pct(entry.get("transcription_percent"))
    return stage, tr, speakers


def describe_entry(entry: dict, now: float, short_error: Callable[[str], str]) -> dict:
    """What to show for one queue entry.

    Returns ``tone`` (badge/dot tone), ``badge`` text, ``percent`` (int or None), ``failed`` (bool,
    offers Retry), ``error`` (short text or ""), ``spoken`` (screen-reader phrase) and ``steps``, the
    checklist rows (three, or four when the job identifies speakers) as ``(name, tone, detail)``.
    """
    upload = entry.get("upload_state") or "pending"
    trans = entry.get("transcription_state") or "pending"
    failed = entry.get("status") == "failed"
    error = short_error(entry["last_error"]) if entry.get("last_error") else ""
    next_at = entry.get("next_attempt_at") or 0
    retry_at = (
        f"Retrying at {_clock(next_at)}"
        if (not failed and next_at and next_at > now and (entry.get("attempts") or error))
        else ""
    )
    up_pct = _pct(entry.get("upload_percent"))
    stage, tr_pct, sp_pct = transcribe_view(entry)
    percent: Optional[int] = None
    spoken = ""
    tone = "idle"

    if upload != "complete":
        if failed or upload == "error":
            tone, badge, spoken = "error", "Upload failed", "upload failed"
            up_step = ("error", f"Failed: {error}" if error else "Failed")
        elif upload == "uploading":
            tone, percent = "active", up_pct
            badge, spoken = f"Uploading {up_pct}%", f"uploading {up_pct} percent"
            up_step = ("active", f"Uploading {up_pct}%")
        elif retry_at:
            tone, badge, spoken = "warn", retry_at, retry_at.lower()
            up_step = ("warn", retry_at)
        else:
            badge, spoken = "Waiting to upload", "waiting to upload"
            up_step = ("idle", "Waiting")
        # The speaker step is only known once the job exists (after upload), so it is omitted here.
        steps = [("Upload audio", *up_step), ("Transcribe recording", "idle", "Waiting"), ("Transcript ready", "idle", "Waiting")]
    else:
        up_step = ("ok", "Complete")
        if trans == "complete":
            tone, badge, spoken = "ok", "Complete", "complete"
            tr_step, ready = ("ok", "Complete"), ("ok", "Ready")
            sp_step = ("ok", "Complete")
        elif failed or trans == "error":
            tone, badge, spoken = "error", "Needs attention", "needs attention"
            err_step = ("error", f"Failed: {error}" if error else "Needs attention")
            if stage == "speakers":
                tr_step, sp_step = ("ok", "Complete"), err_step
            else:
                tr_step, sp_step = err_step, ("idle", "Waiting")
            ready = ("idle", "Waiting")
        elif trans == "transcribing" and stage == "speakers":
            tone, percent = "active", sp_pct
            badge, spoken = f"Identifying speakers {sp_pct}%", f"identifying speakers {sp_pct} percent"
            tr_step, sp_step, ready = ("ok", "Complete"), ("active", f"Identifying {sp_pct}%"), ("idle", "Waiting")
        elif trans == "transcribing":
            tone, percent = "active", tr_pct
            badge, spoken = f"Transcribing {tr_pct}%", f"transcribing {tr_pct} percent"
            tr_step, sp_step, ready = ("active", f"Transcribing {tr_pct}%"), ("idle", "Waiting"), ("idle", "Waiting")
        else:
            tone = "active"
            badge, spoken = "Queued", "queued"
            tr_step, sp_step, ready = ("active", "Queued"), ("idle", "Waiting"), ("idle", "Waiting")
        steps = [("Upload audio", *up_step), ("Transcribe recording", *tr_step)]
        if sp_pct is not None:
            steps.append(("Identify speakers", *sp_step))
        steps.append(("Transcript ready", *ready))

    return {
        "tone": tone,
        "badge": badge,
        "percent": percent,
        "failed": failed,
        "error": error,
        "spoken": spoken + (f": {error}" if error and (failed or tone == "warn") else ""),
        "steps": steps,
    }


def summary_text(entries: List[dict]) -> str:
    n = len(entries)
    text = f"{n} meeting{'s' if n != 1 else ''}"
    failed = sum(1 for e in entries if e.get("status") == "failed")
    return f"{text} · {failed} failed" if failed else text


# -- small widgets ----------------------------------------------------------------


class StatusDot(QWidget):
    """A round status dot painted in the theme's colour for its tone; the active tone pulses."""

    SIZE = 10

    def __init__(self, tone: str = "idle", parent=None):
        super().__init__(parent)
        self.tone = tone
        self.opacity = 1.0
        self.setFixedSize(self.SIZE, self.SIZE)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)

    def set_opacity(self, value: float) -> None:
        if abs(value - self.opacity) > 0.01:
            self.opacity = value
            self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        colour = QColor(theme.tokens()[_DOT_TOKEN.get(self.tone, "disabled")])
        painter.setOpacity(self.opacity if self.tone == "active" else 1.0)
        painter.setPen(Qt.NoPen)
        painter.setBrush(colour)
        painter.drawEllipse(QRectF(1, 1, self.SIZE - 2, self.SIZE - 2))


class ThinBar(QWidget):
    """A 4px rounded progress bar: accent fill on a subtle track."""

    def __init__(self, percent: int = 0, parent=None):
        super().__init__(parent)
        self.percent = percent
        self.setFixedHeight(4)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        tokens = theme.tokens()
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(tokens["border"]))
        painter.drawRoundedRect(QRectF(self.rect()), 2, 2)
        width = self.width() * max(0, min(100, self.percent)) / 100.0
        if width > 0:
            painter.setBrush(QColor(tokens["accent"]))
            painter.drawRoundedRect(QRectF(0, 0, max(width, 4.0), self.height()), 2, 2)


class ElidedLabel(QLabel):
    """A one-line label that shortens with an ellipsis instead of forcing the window wider."""

    def __init__(self, text: str = "", parent=None):
        super().__init__(parent)
        self._full = text
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.setToolTip(text)

    def set_full_text(self, text: str) -> None:
        self._full = text
        self.setToolTip(text)
        self.update()

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return QSize(20, super().sizeHint().height())

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(QFontMetrics(self.font()).horizontalAdvance(self._full) + 2, super().sizeHint().height())

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setFont(self.font())
        painter.setPen(QColor(theme.tokens()["text"]))
        shown = QFontMetrics(self.font()).elidedText(self._full, Qt.ElideRight, self.width())
        painter.drawText(self.rect(), Qt.AlignLeft | Qt.AlignVCenter, shown)


def _label(text: str, name: str = "", tone: str = "") -> QLabel:
    label = QLabel(text)
    if name:
        label.setObjectName(name)
    if tone:
        label.setProperty("tone", tone)
    return label


# -- the panel --------------------------------------------------------------------


class ProcessingPanel(QFrame):
    """Compact per-meeting upload / transcription progress; hidden when the queue holds nothing."""

    retry_requested = Signal()

    def __init__(self, short_error: Callable[[str], str], parent=None, clock: Callable[[], float] = time.time):
        super().__init__(parent)
        self.setObjectName("processCard")
        self._short_error = short_error
        self._clock = clock
        self._names: Dict[str, str] = {}
        self._key: Optional[tuple] = None
        self._dots: List[StatusDot] = []
        self.active = False  # has something to show (independent of the window being on screen)
        self.rebuilds = 0
        self.rows: List[QWidget] = []
        self.badges: List[QLabel] = []
        self.bars: List[ThinBar] = []
        self.retry_buttons: List[QPushButton] = []
        self.checklist_rows: List[Tuple[StatusDot, QLabel, QLabel]] = []
        self.more_label: Optional[QLabel] = None
        self._shown: List[dict] = []
        self._compact = False  # a short window: the checklist starts folded
        self._user_details: Optional[bool] = None  # set once the person clicks the toggle

        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(14, 10, 14, 10)
        self._layout.setSpacing(6)
        head = QHBoxLayout()
        head.setSpacing(8)
        self.title_label = _label("Processing", "section")
        self.summary_label = _label("", "subtle")
        head.addWidget(self.title_label)
        self.details_button = QPushButton("")
        self.details_button.setObjectName("processToggle")
        self.details_button.setCursor(Qt.PointingHandCursor)
        self.details_button.setFlat(True)
        self.details_button.clicked.connect(self._toggle_details)
        head.addWidget(self.details_button)
        head.addStretch(1)
        head.addWidget(self.summary_label)
        self._layout.addLayout(head)
        self._body = QVBoxLayout()
        self._body.setSpacing(12)
        self._layout.addLayout(self._body)

        self._pulse = QTimer(self)
        self._pulse.setInterval(PULSE_MS)
        self._pulse.timeout.connect(self._tick_pulse)
        theme.manager().changed.connect(lambda _name: self._repaint_all())
        self.setVisible(False)

    @property
    def details_open(self) -> bool:
        """Whether the current meeting's three-step checklist is showing."""
        return self._user_details if self._user_details is not None else not self._compact

    def set_compact(self, compact: bool) -> None:
        """Fold the checklist by default in a short window (a person's own toggle still wins)."""
        if compact != self._compact:
            self._compact = compact
            self._refresh_layout()

    def _toggle_details(self) -> None:
        self._user_details = not self.details_open
        self._refresh_layout()

    def _refresh_layout(self) -> None:
        if self._key is not None:
            self._rebuild(self._shown, self._clock())

    # -- data in -------------------------------------------------------------------

    def meeting_name(self, entry: dict) -> str:
        """The meeting's name from its session.json, cached per session folder."""
        session_dir = str(entry.get("session_dir") or entry.get("id") or "")
        cached = self._names.get(session_dir)
        if cached is not None:
            return cached
        name = ""
        try:
            data = json.loads((Path(session_dir) / "session.json").read_text(encoding="utf-8"))
            name = str(data.get("name") or "").strip()
        except Exception:  # noqa: BLE001 - a missing or half-written file just means "use the folder"
            name = ""
        if not name:
            # The folder may be gone (delete-after-upload): the queue entry keeps the name it had.
            name = str(entry.get("name") or "").strip()
        if not name:
            name = Path(session_dir).name or "Meeting"
        self._names[session_dir] = name
        return name

    def update_entries(self, entries: List[dict]) -> bool:
        """Show these queue entries; rebuilds only when something visible changed. Returns ``active``."""
        shown = [e for e in entries if not is_done(e)]
        now = self._clock()
        key = tuple(
            (
                e.get("id"),
                e.get("status"),
                e.get("upload_state"),
                _pct(e.get("upload_percent")),
                e.get("transcription_state"),
                _pct(e.get("transcription_percent")),
                e.get("transcription_stage"),
                e.get("transcribe_percent"),
                e.get("speakers_percent"),
                e.get("attempts"),
                e.get("last_error"),
                e.get("next_attempt_at"),
                bool((e.get("next_attempt_at") or 0) > now),
            )
            for e in shown
        ) + (self.details_open,)
        if key == self._key:
            return self.active
        self._key = key
        self._rebuild(shown, now)
        return self.active

    # -- build ---------------------------------------------------------------------

    def _clear(self) -> None:
        while self._body.count():
            item = self._body.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        self._dots = []
        self.rows, self.badges, self.bars = [], [], []
        self.retry_buttons, self.checklist_rows = [], []
        self.more_label = None

    def _rebuild(self, shown: List[dict], now: float) -> None:
        self.rebuilds += 1
        self._clear()
        self._shown = shown
        self.active = bool(shown)
        if not shown:
            self._pulse.stop()
            self.setVisible(False)
            return
        self.summary_label.setText(summary_text(shown))
        self.details_button.setText("Hide details" if self.details_open else "Show details")
        self.details_button.setAccessibleName(
            "Hide upload and transcription steps" if self.details_open else "Show upload and transcription steps"
        )
        self.setAccessibleName(f"Processing: {summary_text(shown)}")
        for index, entry in enumerate(shown[:MAX_ROWS]):
            self._body.addWidget(self._build_row(entry, now, with_checklist=index == 0))
        extra = len(shown) - MAX_ROWS
        if extra > 0:
            self.more_label = _label(f"and {extra} more", "subtle")
            self._body.addWidget(self.more_label)
        self.setVisible(True)
        if any(d.tone == "active" for d in self._dots):
            self._pulse.start()
        else:
            self._pulse.stop()

    def _build_row(self, entry: dict, now: float, with_checklist: bool) -> QWidget:
        info = describe_entry(entry, now, self._short_error)
        name = self.meeting_name(entry)
        row = QWidget()
        row.setObjectName("processRow")
        row.setAccessibleName(f"{name}: {info['spoken']}")
        box = QVBoxLayout(row)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(4)

        line = QHBoxLayout()
        line.setSpacing(8)
        title = ElidedLabel(name)
        title.setObjectName("processName")
        line.addWidget(title, 1)
        dot = StatusDot(info["tone"])
        self._dots.append(dot)
        line.addWidget(dot, 0, Qt.AlignVCenter)
        badge = _label(info["badge"], "processBadge", info["tone"])
        badge.setAccessibleName(f"{name}: {info['spoken']}")
        self.badges.append(badge)
        line.addWidget(badge)
        if info["failed"]:
            button = QPushButton("Retry")
            button.setObjectName("processRetry")
            button.setCursor(Qt.PointingHandCursor)
            button.setAccessibleName("Retry uploads")
            button.clicked.connect(self.retry_requested)
            self.retry_buttons.append(button)
            line.addWidget(button)
        box.addLayout(line)

        if info["percent"] is not None:
            bar = ThinBar(info["percent"])
            self.bars.append(bar)
            box.addWidget(bar)
        if info["error"] and not with_checklist:
            box.addWidget(_label(info["error"], "subtle"))
        if with_checklist and self.details_open:
            box.addLayout(self._build_checklist(info))
        self.rows.append(row)
        return row

    def _build_checklist(self, info: dict) -> QVBoxLayout:
        col = QVBoxLayout()
        col.setContentsMargins(0, 2, 0, 0)
        col.setSpacing(3)
        for step, tone, detail in info["steps"]:
            line = QHBoxLayout()
            line.setSpacing(8)
            dot = StatusDot(tone)
            self._dots.append(dot)
            name = _label(step, "processStep")
            text = _label(detail, "processDetail", tone)
            text.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            text.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            text.setMinimumWidth(20)
            line.addWidget(dot, 0, Qt.AlignVCenter)
            line.addWidget(name)
            line.addStretch(1)
            line.addWidget(text, 3)
            self.checklist_rows.append((dot, name, text))
            col.addLayout(line)
        return col

    # -- painting ------------------------------------------------------------------

    def _tick_pulse(self) -> None:
        phase = (time.monotonic() % PULSE_PERIOD_SEC) / PULSE_PERIOD_SEC
        opacity = 0.7 + 0.3 * math.cos(phase * 2 * math.pi)
        for dot in self._dots:
            if dot.tone == "active":
                dot.set_opacity(opacity)

    def _repaint_all(self) -> None:
        for child in self.findChildren(QWidget):
            child.update()
        self.update()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        if any(d.tone == "active" for d in self._dots):
            self._pulse.start()

    def hideEvent(self, event) -> None:  # noqa: N802
        self._pulse.stop()
        super().hideEvent(event)
