---
name: Meeting Notes
description: A calm, familiar productivity app where the meeting notes are the page. Neutral surfaces, one blue accent, Inter, Light/Dark/System.
colors:
  accent: "#3b5bdb"
  accent-hover: "#3352c7"
  accent-active: "#2c47ad"
  accent-dark-fill: "#4263eb"
  accent-dark-text: "#8fa4ff"
  page: "#ffffff"
  sidebar: "#f6f7f9"
  subtle: "#f3f4f6"
  border: "#e4e7ec"
  border-strong: "#d0d5dd"
  text: "#171a1f"
  text-2: "#4a5262"
  text-3: "#5f6b7d"
  success-dot: "#2b9a66"
  success-text: "#15703f"
  warning-text: "#8a5a00"
  danger-dot: "#d13b34"
  danger-text: "#c0322c"
  track-them: "#98a2b3"
  page-dark: "#0f1012"
  sidebar-dark: "#131417"
  raised-dark: "#1a1b1f"
  border-dark: "#26282d"
  border-strong-dark: "#363940"
  text-dark: "#e8e9ec"
  text-2-dark: "#a6abb5"
  text-3-dark: "#8a909c"
typography:
  title:
    fontFamily: "Inter, ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, sans-serif"
    fontSize: "1.5rem"
    fontWeight: 600
    lineHeight: 1.25
    letterSpacing: "-0.012em"
  headline:
    fontFamily: "Inter, ui-sans-serif, system-ui, sans-serif"
    fontSize: "1.25rem"
    fontWeight: 600
    lineHeight: 1.3
    letterSpacing: "-0.006em"
  section:
    fontFamily: "Inter, ui-sans-serif, system-ui, sans-serif"
    fontSize: "1rem"
    fontWeight: 600
    lineHeight: 1.3
    letterSpacing: "-0.006em"
  reading:
    fontFamily: "Inter, ui-sans-serif, system-ui, sans-serif"
    fontSize: "1rem"
    fontWeight: 400
    lineHeight: 1.65
  body:
    fontFamily: "Inter, ui-sans-serif, system-ui, sans-serif"
    fontSize: "0.875rem"
    fontWeight: 400
    lineHeight: 1.5
    fontFeature: "cv11"
  label:
    fontFamily: "Inter, ui-sans-serif, system-ui, sans-serif"
    fontSize: "0.8125rem"
    fontWeight: 500
    lineHeight: 1
  caption:
    fontFamily: "Inter, ui-sans-serif, system-ui, sans-serif"
    fontSize: "0.75rem"
    fontWeight: 500
    lineHeight: 1.4
  micro:
    fontFamily: "Inter, ui-sans-serif, system-ui, sans-serif"
    fontSize: "0.6875rem"
    fontWeight: 600
    lineHeight: 1.4
  row-title-phone:
    fontFamily: "Inter, ui-sans-serif, system-ui, sans-serif"
    fontSize: "0.9375rem"
    fontWeight: 500
    lineHeight: 1.5
  mono:
    fontFamily: "ui-monospace, SF Mono, Cascadia Mono, Menlo, Consolas, monospace"
    fontSize: "0.8125rem"
    fontWeight: 400
    lineHeight: 1.55
rounded:
  hair: "2px"
  xs: "4px"
  menu-item: "5px"
  sm: "6px"
  md: "8px"
  lg: "10px"
  xl: "12px"
  pill: "11px"
spacing:
  xs: "4px"
  sm: "8px"
  md: "12px"
  lg: "16px"
  xl: "24px"
  page-x: "40px"
  page-x-phone: "16px"
components:
  button-primary:
    backgroundColor: "{colors.accent}"
    textColor: "#ffffff"
    typography: "{typography.label}"
    rounded: "{rounded.sm}"
    height: "32px"
    padding: "0 12px"
  button-primary-hover:
    backgroundColor: "{colors.accent-hover}"
  button-primary-active:
    backgroundColor: "{colors.accent-active}"
  button-secondary:
    backgroundColor: "{colors.page}"
    textColor: "{colors.text}"
    typography: "{typography.label}"
    rounded: "{rounded.sm}"
    height: "32px"
    padding: "0 12px"
  button-ghost:
    textColor: "{colors.text-2}"
    rounded: "{rounded.sm}"
    height: "32px"
  button-danger:
    backgroundColor: "{colors.page}"
    textColor: "{colors.danger-text}"
    rounded: "{rounded.sm}"
    height: "32px"
  input:
    backgroundColor: "{colors.page}"
    textColor: "{colors.text}"
    typography: "{typography.body}"
    rounded: "{rounded.sm}"
    height: "36px"
    padding: "6px 10px"
  badge:
    backgroundColor: "{colors.page}"
    textColor: "{colors.text-2}"
    typography: "{typography.caption}"
    rounded: "{rounded.pill}"
    height: "22px"
  meeting-row:
    backgroundColor: "{colors.page}"
    textColor: "{colors.text}"
    height: "56px"
    padding: "9px 12px"
  sidebar:
    backgroundColor: "{colors.sidebar}"
    width: "240px"
  side-panel:
    backgroundColor: "{colors.sidebar}"
    rounded: "{rounded.md}"
    padding: "20px"
---

# Design System: Meeting Notes

Documented from the built code (2026-09-29): `meeting_notes/server/static/app.css`, `static/icons.js`, `server/web.py` (web) and `client/ui/theme.py` (Windows Qt client). The frontmatter holds the light-theme values plus the dark values that differ; the CSS custom properties are the source of truth.

## Overview

**Creative North Star: "The Category Standard, Played Straight"**

A calm, familiar productivity app at the craft bar of Linear, Notion and Granola. Neutral cool-grey surfaces, near-black text, hairline 1px borders, one restrained blue accent. The meeting notes are the page: chrome recedes, the document reads like a Notion page (760px measure, 1rem/1.65 text) and the meetings library reads like a clean list. No themed metaphor, no decorative illustration, no signature gimmick; the standard, done precisely.

It is dense but quiet: 32px controls, 14px body, 56px list rows. Light, Dark and System are equally designed (theme is a user setting on web and client). Color carries meaning only: blue means primary, selected, focus or link; green, amber and red appear only as status.

**Key Characteristics:**
- One accent (blue `#3b5bdb`), used for the primary action, selection, focus and links; nothing else is colored at rest.
- One family (Inter, self-hosted), compact rem scale, tabular figures for times, counts and lengths.
- Flat by default: borders and tonal surfaces separate things; shadow only on floating layers.
- Lucide-weight outline icons, one set, 1.5px stroke.
- Status as small neutral badges with a colored dot.
- Same system on phone: sidebar becomes a top bar, nav becomes a bottom tab bar, controls grow to 40-44px.

## Colors

Cool neutral greys with a single indigo-blue accent; status colors are held back for badges, dots, banners and destructive actions.

### Primary
- **Meeting Blue** (`#3b5bdb`, dark fill `#4263eb`): primary buttons, brand mark tile, selected-row tint base, checked controls, progress bar, live-strip "You" lane. Hover `#3352c7`, active `#2c47ad`.
- **Meeting Blue (as text)** (`#3b5bdb` light, `#8fa4ff` dark, token `--accent-fg`): links, active tab underline, active tab-bar item, focus ring and focus border. Text and icons in dark theme use this lighter step, not the fill.
- **Accent Wash** (light `rgba(59,91,219,.09)`, dark `rgba(120,145,255,.14)`): selected list rows, "You" avatar, search-hit transcript rows, focus halo (3px), reveal panels. Accent border `.32` light / `.38` dark.

### Neutral
- **Page** (`#ffffff` / dark `#0f1012`): the main surface and the document overlay.
- **Sidebar Grey** (`#f6f7f9` / `#131417`): sidebar, side panels, notes rail, subsections, table headers, tab bar, sign-in backdrop.
- **Subtle Fill** (`#f3f4f6` / `#1a1b1f`): inline code, pre blocks, neutral banners. Raised surfaces (menus, bulk bar) use `#ffffff` / `#1a1b1f`.
- **Hover / Active Overlays** (`rgba(16,24,40,.05/.08)` light, `rgba(255,255,255,.055/.09)` dark): hover and pressed/current states, segmented-control track, skeleton bars. They are translucent so they work on any surface.
- **Hairline** (`#e4e7ec` / `#26282d`) for dividers and card borders; **Strong Hairline** (`#d0d5dd` / `#363940`) for inputs, secondary buttons, menus, bulk bar.
- **Text** (`#171a1f` / `#e8e9ec`), **Text 2** (`#4a5262` / `#a6abb5`) for secondary and meta, **Text 3** (`#5f6b7d` / `#8a909c`) for placeholders, timestamps, column heads (kept at or above 4.5:1).

### Status
- **Success**: dot `#2b9a66` / `#3fb984`, text `#15703f` / `#6fd3a5`, soft `#eef8f2`.
- **Warning**: text `#8a5a00` / `#f0b955`, soft `#fff7e6`, border `#f1d9a3`.
- **Danger**: dot `#d13b34` / `#ef6f68`, text `#c0322c` / `#ff908a`, soft `#fdf0ef`, border `#f0c4c1`; error toast `#b42b25`.
- **Them lane** (`#98a2b3` / `#585d68`): the non-accent audio track.
- **Toast** inverts the theme (`#171a1f` on light, `#e8e9ec` on dark).

### Named Rules
**The One Accent Rule.** Blue is the only chromatic color at rest. If a new element wants a second hue, it is a status and must be a dot or a banner, not a fill.
**The Status Dot Rule.** State is a neutral pill plus a 6px colored dot (green done, blue pulsing running, red error, hollow ring for none). Never a colored pill fill.
**The Tuned Dark Rule.** Dark is not inverted light: fills and text use separate accent steps (`--accent` vs `--accent-fg`), and every token has a dark value.

## Typography

**Font:** Inter (self-hosted variable woff2, `/static/fonts/`; no CDN) with `ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto` fallback. Feature `cv11` on. **Mono:** `ui-monospace, "SF Mono", "Cascadia Mono", Menlo, Consolas`.

**Character:** One workhorse sans at a compact scale, weight and size doing all the hierarchy. Tabular figures on times, counts, versions and lengths.

### Hierarchy
- **Document title** (600, 1.5rem, 1.25, -0.012em; 1.25rem on phone): the meeting title, click-to-rename.
- **Page title / notes heading** (600, 1.25rem, 1.3): page heads, notes head, install section heads (h1/h2 in `.doc`).
- **Section heading** (600, 1rem, 1.3): notes sections (Summary, Decisions, Action items), settings sections, side panels.
- **Reading text** (400, 1rem, 1.6-1.65, 70-72ch cap): transcript text and notes body; also the 1rem floor on phone.
- **Body** (400, 0.875rem, 1.5): default UI text, list titles (500), inputs, menus.
- **Label** (500, 0.8125rem, 1): buttons, field names, tabs (0.875rem), sub-lines, meta.
- **Caption** (500-600, 0.75rem): badges, pills, column heads, timeline labels and axis, timestamps, rail headings (600, `--text-3`). Sentence case, no tracking, no uppercase.
- **Micro** (600, 0.6875rem): initials in avatars and the `/` key hint only.
- **Row title, phone** (500, 0.9375rem): meeting-row title at phone width.
- **Mono** (0.8125rem, 1.55): notes textarea, commands, code, log views.

### Named Rules
**The Sentence Case Rule.** Every label, heading and column head is sentence case. No all-caps kickers, no letter-spaced eyebrows.
**The Readable Measure Rule.** Prose and transcripts stop at 70-72ch; the notes document at 760px.

## Layout

App frame is a two-column grid: **240px sticky sidebar** + fluid main. Main is centered at max **1240px** with **32px 40px 96px** padding; the sidebar holds brand, search (with `/` shortcut hint), primary nav (32px rows), and at the bottom the theme control, version and log out. Spacing rhythm is a 4/8-based scale: 4, 6, 8, 12, 16, 20, 24, 32, 40, 48, 56.

- **Meetings list:** grid rows `24px select | name+sub | 158px status | 230px notes | 16px chevron`, 56px tall, 14px column gap, hairline separators, no cards. Selection checkbox swaps in for the row icon on hover/focus or when any row is selected; bulk bar floats bottom-center.
- **Meeting document:** full-height overlay right of the sidebar. 48px doc bar (back, status, then on the right the note type picker + Regenerate, divided by a hairline from Copy / Download and the menu; the picker shows only with 2+ note types), a thin Notion line on its own bar under it (state, Open, and a ghost "..." menu for Send again and the danger-styled Remove this note from Notion), then a 1128px column: title, meta line, Notes/Transcript tabs. Notes is a two-column grid `minmax(0,760px) 280px` (document + sticky rail for participants and key points, gap 48px); transcript is 760px.
- **Home:** `1fr | 340px` grid (recent meetings + upload panel), gap 40px.
- **Settings:** `168px section nav | 720px content`, gap 56px, each section a bordered 8px card with a Sidebar Grey header band (hairline under it, 16px between cards), sticky save bar. The Install guide (`/install`) is a Settings sub-page: no sidebar entry, reached from the Installation section, with a `Settings / Install` breadcrumb.

**Breakpoints:**
- `<= 1180px`: list drops to `150px | 200px`, chevron hidden.
- `<= 1100px`: notes rail, home panel and settings nav collapse under/over the content (settings nav becomes a horizontal sticky strip).
- `<= 860px` (phone): sidebar becomes a top bar (brand mark, search, logout), nav moves to a fixed bottom tab bar (safe-area aware, active item in accent), overlay goes full-screen, list rows wrap to two lines with status/notes badges below the name, bulk bar becomes a full-width sheet above the tab bar, tables become stacked cards, doc-bar buttons go icon-only at 40px. Buttons 40px (small 32px), inputs 44px min at 1rem to avoid iOS zoom, page padding 20px 16px. Every workflow must remain usable here; also check 1920x1080 and 3440 ultrawide (content stays centered at 1240px).

## Elevation & Depth

Flat and tonal. Depth comes from 1px hairlines and a stepped set of neutral surfaces (page, sidebar grey, subtle fill), not shadows. Shadows appear only on layers that float above content.

### Shadow Vocabulary
- **Popover** (`--shadow-pop`: `0 1px 2px rgba(16,24,40,.06), 0 8px 24px rgba(16,24,40,.12)`; dark `0 1px 2px rgba(0,0,0,.4), 0 12px 32px rgba(0,0,0,.55)`): menus, bulk-action bar, toast.
- **Segmented thumb** (`0 0 0 1px var(--border), 0 1px 2px rgba(16,24,40,.08)`): the selected segment.
- **Focus halo** (`0 0 0 3px var(--accent-soft)`) on focused fields; **timeline selection ring** (`0 0 0 2px bg, 0 0 0 3.5px text`).
- Phone bulk bar casts an upward `0 -6px 20px rgba(16,24,40,.12)`.

### Named Rules
**The Flat-At-Rest Rule.** Nothing casts a shadow until it floats. Cards, rows, panels and inputs are bordered, never shadowed.

## Shapes

Softly rounded rectangles on a small scale: **2px** (progress track, timeline blocks), **4px** (code, checkboxes, timeline ends), **5px** (menu items, file-picker button), **6px** (buttons, inputs, nav rows, brand-mark tile), **8px** (banners, panels, cards, menus, tables, notes rail), **10px** (bulk bar), **12px** (sign-in card), **11px full pill** (badges), **50%** (dots, avatars). Borders are 1px hairlines; the dashed 1px line is reserved for transcript gap markers and empty ledgers. The audio-gap band in the timeline is a diagonal hairline hatch. Selected list rows are a tint, never a left-edge stripe.

## Components

### Buttons
- **Shape:** 6px radius, 32px tall (26px `sm`, 36px `block`, 40px on phone), 0 12px padding, 13px/500 label, 8px icon gap, 1px border.
- **Secondary (default):** page fill, strong hairline, text color; hover hover-overlay; active active-overlay.
- **Primary:** accent fill and border, white text; hover `#3352c7`, active `#2c47ad`. One per view.
- **Ghost:** transparent, `--text-2`; hover overlay + full text color. Used for nav-adjacent and toolbar actions.
- **Danger:** page fill, danger border and text; hover danger-soft. Destructive always explicit; filled red is used only for the client's Stop recording.
- **Disabled/busy:** 50% opacity, `not-allowed` / `progress`. **Focus:** `:focus-visible` 2px `--accent-fg` outline, 2px offset. Transitions 150ms on background, border, color, opacity.

### Fields
- **Style:** 36px min, 6px radius, strong hairline, page fill, 14px Inter; textarea is mono 13px.
- **Hover:** border to `--text-3`. **Focus:** no outline, border `--accent-fg` + 3px accent-soft halo. **Invalid:** danger-dot border. **Disabled:** 55% opacity.
- Field name above (13px/500), 6px gap, 16px between fields. Checkboxes/radios 16px, native `accent-color`.

### Segmented control (theme switcher)
Inset track (active overlay, 8px radius, 2px padding), 28px options at 6px radius; selected option is a page-colored chip with the segmented-thumb shadow. Hidden radios keep native arrow-key behavior. Sidebar footer shows the icon+label form; compact form is icons only.

### Badges and pills
- **Badge:** 22px pill, 1px hairline, page fill, 12px/500 `--text-2`, 6px dot at left. Running dot pulses (1.6s); none is a hollow ring; live is red pulsing.
- **Pill** (action-item owner/due): 20px, active-overlay fill, no border.

### Meeting row (signature list pattern)
56px row, 14px title (500) over a 13px `--text-2` sub-line, status and notes badges at right, hover overlay, chevron fades in on hover, selected = accent wash. "Generate notes" affordance is hidden until hover/focus but always visible on touch (`hover: none`) and phone. Skeleton rows use pulsing bars.

### Navigation
Sidebar links 32px, 6px radius, 10px icon gap, `--text-2` 500; hover overlay; current page = active overlay + full text color (`aria-current="page"`). Phone tab bar: equal-flex items, icon over 12px label, current item in accent text. Document tabs: 14px/500 underline tabs, 2px accent underline on active, hairline baseline.

### Notes document
Sections separated by 32px, headings 1rem/600, body 1rem/1.65 up to 72ch, list markers `--text-3`. Action items are a 16px outlined box (1.5px, 4px radius) + text + owner/due pills. The right rail (Sidebar Grey, 8px radius, 16px 18px padding) holds participants (24px initial avatars) and key points under 12px/600 `--text-3` headings. "Processing details" sit at the bottom in a collapsed disclosure.

### Recorder remote (Recorders page)
Each live recorder is drawn as a replica of the Windows client's main window (`client/ui/main_window.py`), one per recorder, stacked and centred at the client's own 900px width (`.cw`, 902px with its frame, 10px radius, no shadow). It uses the client's palette, not the web tokens: `--cl-*` in all three theme blocks are the `LIGHT` / `DARK` tables of `client/ui/theme.py` value for value (a test keeps them equal), so Light / Dark / System work as for the rest of the page. Layout, top to bottom, with the client's sizes: a 58px top bar (the computer's name 15px/600 with "Windows 11 · v0.7.8" where the client shows its version; ghost buttons Upload, History, Settings and a "..." button); strips (device errors solid red, device back green, token rejected red, unreachable / recordings-folder amber, "Update available: x" blue with an Update now button, the red unsupported-version strip, plus a remote-only amber strip when control is off or the page lost its link); the 10px panel card with the 40px clock (grey idle, full ink recording), the "You: / Them:" device lines, the "Meeting name (optional)" field, the 40px "Note type" select (`.cw-type`, 170px; shown only for a recorder that advertises the `note_type` capability when the server has 2+ note types, filled from `/v1/note-templates`, following the recorder's own choice) and the 40px Start recording (accent) / Stop recording (red) / Finishing... button; under them, for an auto-recorded call, the quiet "Auto end at 3:00 PM" line with a Disable auto end button (`auto_end` capability); two 115px lanes ("You · Microphone", "Them · System audio", % readout, Preview / Muted / No signal / Clipping badges, a `<canvas>` envelope trace drawn as `waveform.py` does, greyed for the idle input preview) each beside a 108px Mute / Unmute button of exactly the lane's height; the "Live preview" label and 160px panel of plain "You: ... / Them: ..." lines; the status line ("Ready." / "Recording. live preview connected  |  2 uploads pending"). The client's call prompt and stop suggestion are pop-up cards in its screen corner; here they float over the window's bottom-right corner.

Each button sends the command the client's own button would run: Start / Stop recording `start` / `stop` (Stop asks first; renaming while recording is `set_name`), the Note type select `set_note_type`, Disable auto end `disable_auto_end`, Mute / Unmute `mute` / `unmute`, Update now `install_update` (idle only), Refresh audio devices `refresh_devices`, Not now / Record / Keep recording / Stop `dismiss_call_prompt` / `accept_call_prompt` / `keep_recording` / `stop_suggested`. The rest map to the server's own pages: Upload opens Home's "Add a meeting" panel, History opens Meetings filtered by the computer's name, "Open recordings folder" and "Re-upload a saved recording..." open the Recordings panel, "Logs..." opens Settings > Client logs. Settings cannot be changed remotely (no command exists), so its button opens a popover saying so, with links to Settings > Recorders and Client logs. Disabled buttons carry the reason as a tooltip (remote control off, reconnecting, finish the recording first). Narrower than 600px the header buttons keep only their icons (the client has no narrow layout; its minimum is 720px). The lane traces are fed by the ~4-5 Hz state / `levels` frames, so they step more coarsely than the client's 30 fps. The live meeting is joined by `state.meeting.session_id` (else, for a recorder that does not report one, by computer name when only one live meeting comes from it) from `GET /v1/live`, polled every 2.5 s only while a recorder is recording.

### macOS permissions panel (client only)
Shown only on macOS when Microphone, Screen & System Audio Recording or Local Network access is missing: a `permCard` (raised fill, 1px strong border, 12px radius, 640px max) centred over the client's recorder card and everything below it, with the window's own `bg` at ~85% opacity as a scrim; the header and alert strips above stay visible. Title 15px/600, one muted line, then one `permRow` per permission (name 13px/600, a `recBadge` status pill: green Granted, amber Not granted, muted Unknown; the steps as a numbered list; Open System Settings / Allow microphone / Quit and reopen as secondary buttons) and a footer with Not now and the default (accent) Check again. Granted rows collapse to name and badge. Dismissed, it becomes an amber `warnBar` strip, "Permissions needed: ..." with a Fix button. A missing server password is a red `alertBar` strip with an Enter password button. All colours are existing theme tokens.

### Live transcript (Home card, live overlay, recorder card)
Presented as the client's Live preview: plain lines "You: text" / "Them: text" (name 600, You in accent text), 14px/1.55, no avatars or timestamps, in a hairline-bordered panel, with the client's placeholder text until speech arrives. Lines are sorted by meeting time (`start`) because one track's lines can arrive 30-40 s late. The Home card shows the last 6 lines; the overlay and the recorder card keep the whole transcript, newest at the bottom, and follow new lines only while the reader is already at the bottom. The client shows a brief inverted-theme toast (`toast_bg`/`toast_text` tokens in `LIGHT` and `DARK`) when the server acts.

### Recordings panel (Recorders page, per card)
A modal sheet (`dialog.rec-panel`, 880px max, full screen on phone) opened from each card's Recordings button: a summary line ("42 recordings · 2 not on server · 1 failed"), search, a status filter, Refresh, then flat rows (checkbox, name 600, date · length · size, a status badge, Re-upload / Delete from this computer / Open on server). Status badges follow the Status Dot Rule by tone: green uploaded and transcript ready, blue uploading / waiting / transcribing, amber not on server / in server trash / partly uploaded / transcription failed, red upload failed / can't upload; the label is always text. Deleting asks in the shared confirm dialog; when the server has no copy it adds a red `dialog-warning` line. A sticky bulk bar appears on selection. The client's own Re-upload window uses the same pills and wording.

### Transcript and You/Them timeline
Transcript row: 24px initial avatar (You = accent wash + accent text, Them = neutral), 13px/600 name, 12px tabular timestamp, 1rem/1.6 text, 8px radius hit highlight. Above it, the session timeline is a quiet strip: two 8px lanes (You accent at 80% opacity, Them `--track-them`), labels in 12px `--text-2`, tabular axis ticks, hatch gaps, and 44px-tall hit targets via extended pseudo-element. Selected block gets the double ring.

### Overlays and feedback
- **Menu:** 8px radius, hairline, popover shadow, 4px padding, items 7px 10px with 16px icons, separators 1px; danger item in danger text.
- **Banner:** 8px radius, 1px border, 16px icon, neutral / `.warn` / `.ok` / `.err` tints from the soft status tokens.
- **Toast:** bottom-center, inverted theme, 8px radius, popover shadow; error variant `#b42b25`.
- **Bulk bar:** floating raised bar, 10px radius, selection count + buttons; full-width sheet on phone.
- **Empty states** are teaching text (centered, `--text-2`, max 460px), not illustrations.
- **Sign-in:** centered 380px card (12px radius, 28px padding) on Sidebar Grey, 32px brand tile.

### Icons
One set: Lucide-weight outline icons inlined as SVG (`icons.js` on the client-rendered pages, `_ICON_PATHS` in `web.py` server-side; keep the two in step). 24x24 viewBox, rendered at 16px by default, `stroke: currentColor`, 1.5px stroke, round caps and joins, no fill, `aria-hidden`. Icons inherit text color (`--text-2` in menus and banners, `--text-3` on quiet row icons). Brand mark is a waveform glyph in a 24px accent tile (6px radius, white glyph). No emoji, no glyph or icon fonts, no second icon family.

### Theme mechanism
- **Web:** `<html data-theme="system|light|dark">` is written server-side from `settings.appearance` (no flash); `color-scheme` and `<meta name="theme-color">` (`#ffffff` / `#0f1012`) follow. Light tokens are `:root`. Dark tokens exist twice: `:root[data-theme="dark"]` and, for System, inside `@media (prefers-color-scheme: dark)` for `[data-theme="system"]` and unset. Switching (System/Light/Dark radios in sidebar footer and Settings) sets `dataset.theme` instantly and `PUT /v1/appearance`; a new token must be added to all three blocks.
- **Client (Qt):** `theme.py` holds `LIGHT` and `DARK` token dicts as the single source for stylesheet, `QPalette`, painted meters, icons and history. Appearance is System/Light/Dark in client config; System follows `QStyleHints.colorScheme()` (registry fallback) and live-restyles on OS change; the Windows title bar follows via DWM. Fonts: bundled Inter Regular/Medium/SemiBold/Bold with Segoe UI fallback; 13px base. Client shapes match the web (6px controls, 8px banners/lists, 10px record card, 12px prompt card, 1px hairlines, 2px accent focus border). Client-specific: 15px brand/headings, 40px clock (tabular), `record` button in accent and the `recording` (Stop) state in destructive red, meters You blue / Them grey.

### Motion
Restrained. 120-150ms color/background/border transitions; 150ms opacity fade-in for overlays, toasts and the timeline (250ms); 1.6-1.8s opacity pulse for running/live dots and skeletons; progress bar 200ms ease-out. No movement, parallax or scroll effects. `prefers-reduced-motion: reduce` disables all animation and transitions.

## Do's and Don'ts

### Do:
- **Do** add new colors as tokens in all three theme blocks (light, dark, system-dark) and, for the client, in both `LIGHT` and `DARK` dicts; never hard-code hex in components.
- **Do** use `--accent` for fills and `--accent-fg` for text, icons and focus so dark theme keeps its contrast.
- **Do** keep controls at 32px on desktop and 40-44px on phone, with 1rem text in phone inputs.
- **Do** separate with 1px hairlines and tonal surfaces; reserve shadow for menus, the bulk bar and toasts.
- **Do** show state as a neutral badge with a colored dot; use banners for page-level status.
- **Do** cap prose at 70-72ch, use tabular numerals for times and counts, sentence-case every label.
- **Do** keep every action reachable on touch: hover-revealed affordances must be visible under `(hover: none)` and on phone.
- **Do** verify new screens in Light, Dark and System, at 1920x1080, phone width and the 3440 ultrawide.

### Don't:
- **Don't** introduce themed metaphors or decoration (tape boxes, board numbers, lane legends, ruled track-sheet grids, grease-pencil ticks); the direction is the category standard, played straight.
- **Don't** add a second accent hue, gradients, colored pill fills, or colored left-edge stripes on rows.
- **Don't** add a second font family, an icon font, emoji, or a second icon set; icons are 1.5px Lucide-weight outlines.
- **Don't** use uppercase or letter-spaced kickers/eyebrows, or side-stripe accents.
- **Don't** pull in a CDN, web font host, or front-end framework; the server must work offline on the LAN.
- **Don't** shadow cards, rows, panels or inputs at rest.
- **Don't** make the Windows client drift from the web system (same accent, radii, hairlines, Inter); change both together.
- **Don't** put real meeting transcripts in screenshots, fixtures or commits.

## Known Drift (recorded, not canonized)

- The web and client palettes are close but not identical: web uses cool slate greys (`#f6f7f9`, `#0f1012`) and client uses zinc greys (`#f8f8f9`, `#111113`); client dark accent fill `#3f5ce0` vs web `#4263eb`, and client danger fills are `#d92d20`. Unify only if the two are being edited together.
- `app.css` duplicates the dark token block (explicit and system) by hand, so the two can diverge.
- Legacy `.card`, `.table-wrap`, `.overlay-head`, `.notes-toolbar`, `.markdown-document` rules remain for old markup and are not part of the system.
- The `.blk.on` double-ring and `.gap-band` hatch are timeline-specific and should not spread to other components.
