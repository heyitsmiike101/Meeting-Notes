---
version: 1
slug: "meeting-notes-server-web-py"
primary_target: "meeting_notes/server/web.py"
related_targets: ["meeting_notes/client/ui/theme.py"]
---

# Meeting Notes web app (server UI)

Scope: every page rendered by `meeting_notes/server/web.py` (sign-in, Home, Meetings library + meeting view with
notes/transcript, Settings incl. AI access and client logs, Install), desktop 1280–3440 wide and phone 360–430 wide.
Visitor mode: **Operate**. The Windows client (`meeting_notes/client/ui/theme.py`) follows the same system in Qt.

Audience/job: the owner, right after a meeting, reads the notes (summary, decisions, action items) and copies them;
later finds past meetings by name/date; manages in bulk; tunes settings. Every workflow must work on a phone.

## Direction contract

THESIS: The category standard, played straight, at the craft level of Linear, Notion and Granola: a calm, familiar
productivity app where the meeting notes are the page. It refuses any themed metaphor (no tape boxes, board
numbers as decoration, lane legends, ruled track-sheet grids, grease-pencil ticks).

OWN-WORLD: Neutral surfaces with one restrained blue accent (primary actions, selection, focus, links). Light theme:
white page, very light grey sidebar/panels, near-black text, subtle 1px borders. Dark theme: near-black neutral page,
slightly raised panels, soft white text, same accent tuned for dark. Theme is Light, Dark or System (a setting).
Inter-style workhorse sans (self-hosted; no CDN), one family, compact rem scale, tabular figures for times and
lengths. Status as small neutral badges with a coloured dot. Lucide-weight 1.5px outline icons, one set.

STORY: He sees recent meetings first, knows at a glance which have notes, opens one and reads summary, decisions
and action items in a Notion-like document, copies or downloads them, and jumps into the transcript when needed.

FIRST VIEWPORT: Desktop: a slim left sidebar (Meetings, Home, Settings, Install; search at the top, theme and log
out at the bottom) and a main list of meetings as clean rows (name, date, length, notes status, quick actions).
Opening a meeting shows a document view: title, meta line, tabs Notes / Transcript, summary then decisions and
action items. Phone: top bar with search, bottom tab bar, full-screen meeting document.

FORM: The category standard (canon card), chosen by the owner over the rolled directions (seed 9071004b).
Signature interaction: none themed; the standard done precisely: instant keyboard-friendly list, smooth
notes/transcript switching, the You/Them session timeline kept as a quiet minimal strip.

FINISH: unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, docs/design.md, and every shipping raster carrying its provenance

## Unresolved

- None.
