"""The Upload dialog: add a meeting from an audio recording, a transcript file, or pasted text.

One dialog, three clear choices:

* **Audio recording**: the server transcribes it (as before).
* **Transcript file** (``.txt``, ``.vtt``, ``.srt``): the transcript is used as it is; nothing is transcribed
  again. Times and speaker names are kept when the file has them (the server parses them).
* **Paste a transcript**: the same, from text copied out of Teams, Zoom or anywhere else.

The dialog only collects and validates the choice. ``request`` holds an :class:`UploadRequest` after Upload is
pressed; the main window sends it off the GUI thread.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QDateTime, Qt
from PySide6.QtWidgets import (
    QButtonGroup,
    QDateTimeEdit,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from meeting_notes.client.api import SUPPORTED_RECORDING_TYPES
from meeting_notes.client.ui.theme import make_sheet

log = logging.getLogger("meeting_notes.client.ui.upload")

AUDIO = "audio"
TRANSCRIPT_FILE = "file"
TRANSCRIPT_PASTE = "paste"

TRANSCRIPT_EXTENSIONS = (".txt", ".vtt", ".srt")
MAX_TRANSCRIPT_BYTES = 2 * 1024 * 1024  # the server's limit; checked here so the message is immediate
AUDIO_FILTER = "Audio recordings (" + " ".join(f"*{e}" for e in SUPPORTED_RECORDING_TYPES) + ");;All files (*)"
TRANSCRIPT_FILTER = "Transcripts (" + " ".join(f"*{e}" for e in TRANSCRIPT_EXTENSIONS) + ");;All files (*)"

HINTS = {
    AUDIO: "The server transcribes the recording. MP3, WAV, M4A, MP4, FLAC, OGG, Opus, AAC and WebM work.",
    TRANSCRIPT_FILE: (
        "Already have a transcript from Teams or Zoom? Choose the .txt, .vtt or .srt file. Nothing is "
        "transcribed again; times and speaker names are kept when the file has them."
    ),
    TRANSCRIPT_PASTE: (
        "Paste a transcript copied from Teams, Zoom or anywhere else. Nothing is transcribed again; lines "
        "like \"[00:12:34] Jane: ...\" keep their times and speakers."
    ),
}
HINT_WIDTH = 460


@dataclass
class UploadRequest:
    """What the person chose to upload."""

    kind: str  # "audio" or "transcript"
    name: str = ""
    path: Optional[Path] = None  # audio: the recording file
    text: str = ""  # transcript: the text to upload
    started_at: Optional[float] = None  # transcript: when the meeting happened (unix seconds)
    source: str = ""  # transcript: "file" or "pasted"
    filename: str = ""  # transcript from a file: its name

    @property
    def label(self) -> str:
        """Short description for status messages."""
        if self.path is not None:
            return self.path.name
        return self.name or (self.filename or "the pasted transcript")


def read_transcript_file(path: Path) -> str:
    """Read a transcript file as text. Raises ``ValueError`` with a message fit to show."""
    path = Path(path)
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ValueError(f"Could not open {path.name}: {exc.strerror or exc}") from exc
    if size > MAX_TRANSCRIPT_BYTES:
        raise ValueError(
            f"{path.name} is over the {MAX_TRANSCRIPT_BYTES // (1024 * 1024)} MB limit for transcripts."
        )
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ValueError(f"Could not read {path.name}: {exc.strerror or exc}") from exc
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        text = raw.decode("utf-16", errors="replace")
    else:
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("cp1252", errors="replace")
    if not text.strip():
        raise ValueError(f"{path.name} is empty.")
    return text


def _fit_hint(label: QLabel) -> None:
    """Fixed width + the height that width needs (wrapped labels in a form layout get clipped otherwise)."""
    label.ensurePolished()
    # heightForWidth() never reports less than the current fixed height: release it before measuring.
    label.setMinimumHeight(0)
    label.setMaximumHeight(16777215)
    label.setFixedWidth(HINT_WIDTH)
    label.setFixedHeight(label.heightForWidth(HINT_WIDTH) + label.fontMetrics().descent())


class UploadDialog(QDialog):
    def __init__(self, parent=None, start_dir: str = ""):
        super().__init__(parent)
        self.setWindowTitle("Upload")
        self.setMinimumWidth(540)
        self.request: Optional[UploadRequest] = None
        self._start_dir = start_dir
        self._file: Optional[Path] = None

        layout = make_sheet(self, "Upload")

        question = QLabel("What do you want to upload?")
        question.setObjectName("heading")
        layout.addWidget(question)

        choices = QHBoxLayout()
        choices.setSpacing(18)
        self.mode_group = QButtonGroup(self)
        self.audio_radio = QRadioButton("Audio recording")
        self.file_radio = QRadioButton("Transcript file")
        self.paste_radio = QRadioButton("Paste a transcript")
        for button, mode in (
            (self.audio_radio, AUDIO),
            (self.file_radio, TRANSCRIPT_FILE),
            (self.paste_radio, TRANSCRIPT_PASTE),
        ):
            button.setProperty("mode", mode)
            self.mode_group.addButton(button)
            choices.addWidget(button)
        choices.addStretch(1)
        layout.addLayout(choices)

        self.hint = QLabel("")
        self.hint.setObjectName("subtle")
        self.hint.setWordWrap(True)
        layout.addWidget(self.hint)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        form.setHorizontalSpacing(18)
        form.setVerticalSpacing(10)
        form.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)

        # -- file (audio recording, or transcript file) ------------------------------
        self.file_edit = QLineEdit()
        self.file_edit.setReadOnly(True)
        self.file_edit.setPlaceholderText("No file chosen")
        self.file_edit.setAccessibleName("Chosen file")
        self.browse_button = QPushButton("Choose file...")
        self.browse_button.setAutoDefault(False)
        self.browse_button.clicked.connect(self._browse)
        self.file_row = QWidget()
        file_layout = QHBoxLayout(self.file_row)
        file_layout.setContentsMargins(0, 0, 0, 0)
        file_layout.addWidget(self.file_edit, 1)
        file_layout.addWidget(self.browse_button)
        self.file_label = QLabel("File")
        form.addRow(self.file_label, self.file_row)

        # -- pasted text -------------------------------------------------------------
        self.text_edit = QPlainTextEdit()
        self.text_edit.setAccessibleName("Transcript text")
        self.text_edit.setPlaceholderText("Jane: Hello everyone\nBob: Thanks for joining")
        self.text_edit.setMinimumHeight(150)
        self.text_edit.textChanged.connect(self._sync)
        self.text_label = QLabel("Transcript")
        self.text_label.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        form.addRow(self.text_label, self.text_edit)

        # -- name and when (transcripts) ------------------------------------------------------
        self.name_edit = QLineEdit()
        self.name_edit.setAccessibleName("Meeting name")
        self.name_edit.setPlaceholderText("Optional, for example Weekly standup")
        self.name_edit.setMaxLength(200)
        self.name_label = QLabel("Meeting name")
        form.addRow(self.name_label, self.name_edit)

        self.when_edit = QDateTimeEdit(QDateTime.currentDateTime())
        self.when_edit.setCalendarPopup(True)
        self.when_edit.setDisplayFormat("MMM d, yyyy  h:mm AP")
        self.when_edit.setAccessibleName("Meeting date and time")
        self.when_label = QLabel("Date and time")
        form.addRow(self.when_label, self.when_edit)
        self._form = form
        layout.addLayout(form)

        self.error_label = QLabel("")
        self.error_label.setObjectName("connResult")
        self.error_label.setProperty("state", "error")
        self.error_label.setWordWrap(True)
        self.error_label.setVisible(False)
        layout.addWidget(self.error_label)
        layout.addStretch(1)

        buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        self.upload_button = buttons.addButton("Upload", QDialogButtonBox.AcceptRole)
        self.upload_button.setDefault(True)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.mode_group.buttonToggled.connect(lambda *_a: self._on_mode_changed())
        self.audio_radio.setChecked(True)
        self._on_mode_changed()

    # -- modes -----------------------------------------------------------------------------

    def mode(self) -> str:
        checked = self.mode_group.checkedButton()
        return str(checked.property("mode")) if checked is not None else AUDIO

    def set_mode(self, mode: str) -> None:
        for button in self.mode_group.buttons():
            if button.property("mode") == mode:
                button.setChecked(True)

    def _show_row(self, label: QWidget, field: QWidget, visible: bool) -> None:
        label.setVisible(visible)
        field.setVisible(visible)

    def _on_mode_changed(self) -> None:
        mode = self.mode()
        self.hint.setText(HINTS[mode])
        _fit_hint(self.hint)
        self.error_label.setVisible(False)
        files = mode in (AUDIO, TRANSCRIPT_FILE)
        self._show_row(self.file_label, self.file_row, files)
        self._show_row(self.text_label, self.text_edit, mode == TRANSCRIPT_PASTE)
        transcript = mode != AUDIO
        self._show_row(self.when_label, self.when_edit, transcript)
        self.name_edit.setPlaceholderText(
            "Optional: the file name is used" if mode == TRANSCRIPT_FILE else
            "Optional, for example Weekly standup"
        )
        # A file chosen for the other kind of upload does not carry over.
        if files and self._file is not None and self._kind_of(self._file) != (AUDIO if mode == AUDIO else TRANSCRIPT_FILE):
            self._clear_file()
        self.file_label.setText("Recording" if mode == AUDIO else "Transcript file")
        self._sync()

    @staticmethod
    def _kind_of(path: Path) -> Optional[str]:
        suffix = Path(path).suffix.lower()
        if suffix in SUPPORTED_RECORDING_TYPES:
            return AUDIO
        if suffix in TRANSCRIPT_EXTENSIONS:
            return TRANSCRIPT_FILE
        return None

    # -- file ----------------------------------------------------------------------------------

    def _browse(self) -> None:
        audio = self.mode() == AUDIO
        chosen, _ = QFileDialog.getOpenFileName(
            self,
            "Choose a recording" if audio else "Choose a transcript",
            self._start_dir,
            AUDIO_FILTER if audio else TRANSCRIPT_FILTER,
        )
        if chosen:
            self.choose_file(Path(chosen))

    def _clear_file(self) -> None:
        self._file = None
        self.file_edit.clear()

    def choose_file(self, path: Path) -> None:
        """Use ``path``; switches to the matching choice when it is the other kind of file."""
        path = Path(path)
        self.error_label.setVisible(False)
        kind = self._kind_of(path)
        if kind is not None and kind != (AUDIO if self.mode() == AUDIO else TRANSCRIPT_FILE):
            self.set_mode(kind)
        self._file = path
        self.file_edit.setText(str(path))
        self.file_edit.setCursorPosition(0)  # show the start of a long path, not its tail
        if self.mode() != AUDIO:
            try:
                modified = path.stat().st_mtime
            except OSError:
                modified = None
            if modified:
                self.when_edit.setDateTime(QDateTime.fromSecsSinceEpoch(int(modified)))
        self._sync()

    # -- state -----------------------------------------------------------------------------------

    def _sync(self) -> None:
        mode = self.mode()
        if mode == TRANSCRIPT_PASTE:
            ready = bool(self.text_edit.toPlainText().strip())
        else:
            ready = self._file is not None
        self.upload_button.setEnabled(ready)

    def _fail(self, message: str) -> None:
        self.error_label.setText(message)
        self.error_label.setVisible(True)

    def _started_at(self) -> float:
        value = self.when_edit.dateTime()
        return float(value.toSecsSinceEpoch())

    # -- accept ----------------------------------------------------------------------------------

    def accept(self) -> None:  # noqa: D102
        mode = self.mode()
        name = self.name_edit.text().strip()
        if mode == AUDIO:
            if self._file is None:
                self._fail("Choose a recording first.")
                return
            if self._kind_of(self._file) != AUDIO:
                self._fail("That file type is not a supported audio format.")
                return
            self.request = UploadRequest(kind="audio", name=name, path=self._file)
            super().accept()
            return
        if mode == TRANSCRIPT_FILE:
            if self._file is None:
                self._fail("Choose a transcript file first.")
                return
            try:
                text = read_transcript_file(self._file)
            except ValueError as exc:
                self._fail(str(exc))
                return
            self.request = UploadRequest(
                kind="transcript",
                name=name or self._file.stem,
                text=text,
                started_at=self._started_at(),
                source="file",
                filename=self._file.name,
            )
            super().accept()
            return
        text = self.text_edit.toPlainText()
        if not text.strip():
            self._fail("Paste the transcript first.")
            return
        if len(text.encode("utf-8", "ignore")) > MAX_TRANSCRIPT_BYTES:
            self._fail(f"That is over the {MAX_TRANSCRIPT_BYTES // (1024 * 1024)} MB limit for transcripts.")
            return
        self.request = UploadRequest(
            kind="transcript",
            name=name or f"Pasted transcript {dt.datetime.fromtimestamp(self._started_at()):%Y-%m-%d %H:%M}",
            text=text,
            started_at=self._started_at(),
            source="pasted",
        )
        super().accept()
