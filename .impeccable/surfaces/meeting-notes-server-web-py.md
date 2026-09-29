---
version: 1
slug: "meeting-notes-server-web-py"
primary_target: "meeting_notes/server/web.py"
related_targets: ["meeting_notes/client/ui/theme.py"]
---

# Meeting Notes web app (server UI)

Scope: every page rendered by `meeting_notes/server/web.py` (sign-in, Home, Meetings library + meeting overlay with
notes/transcript, Settings, Install), desktop 1280–3440 wide and phone 360–430 wide. Visitor mode: **Operate**.
The Windows client (`meeting_notes/client/ui/theme.py`) inherits the same world in Qt.

Audience/job: the owner, right after a meeting, reads the notes (summary, decisions, action items) and copies them;
later finds past meetings by name/date; manages in bulk (build notes, retranscribe, delete audio/meetings); tunes
settings. Every workflow must be comfortable on a phone.

## Direction contract

THESIS: Each meeting is a recording session. The library is a shelf of tape boxes, and a meeting opens as its track
sheet: the notes are the log card, the transcript is two ruled lanes, You and Them. It refuses the category default of
a sidebar, a grey table and a chat-bubble transcript.

OWN-WORLD: Console graphite chrome (#1d1f22 family) around tape-box card stock (#f1ead9 ground, kraft #d9c8a5 for
spines, rules and secondary panels). Grease-pencil red (#c8372d) is the only accent: primary actions, selection,
state ticks, focus. Hairline ruled grids like a printed track sheet, numbered lane headers set in condensed legend
caps, tabular figures for every timecode, date and length. Self-hosted Barlow and Barlow Condensed, no CDN.

STORY: He sees the newest sessions first, knows at a glance which have notes ready, opens one and reads decisions and
action items without scrolling past chrome, then copies, downloads or jumps to the moment in the transcript.

FIRST VIEWPORT: Graphite console bar on top (brand, Meetings, Home, Settings, Install), with search as the widest
control. Below it, the meetings shelf: ruled rows like tape-box spines, each with a board number (M-0142), name, date,
a length bar drawn to scale, transcript and notes state ticks, and the primary action "Open". A selected meeting opens
as a full track-sheet panel, notes first.

FORM: Studio track sheet and tape-box labels, candidate 1 of my ordered list, picked by the owner (seed 9071004b).
Signature interaction: the two-lane session strip (You over Them, drawn to scale from transcript segments) on the
meeting sheet; clicking a segment jumps to that moment in the transcript.

FINISH: unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, DESIGN.md, and every shipping raster carrying its provenance

## Unresolved

- Dark variant: not in scope; the world is graphite chrome with light card stock in all lighting.
