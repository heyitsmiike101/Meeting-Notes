# Meeting Notes Bridge Workflow: Quick Notes

Transform the supplied transcript into short, scannable notes: what happened,
what was decided, and who owes what. Brevity is the goal; leave out anything
that is not needed to act on the meeting.

## Ground Rules

The transcript is the source of truth. Summarize only what was actually said;
do not add outside knowledge or fill gaps with plausible information. Never
invent names, roles, dates, owners, deadlines, decisions, or commitments.

Distinguish discussion and proposals from actual decisions. Mark something as
an action item only when the transcript establishes agreed work. Do not infer
an action owner from context or responsibility. Use `null` for an action owner
or due date unless explicitly established by the transcript. Do not turn
suggestions such as “we should,” “maybe,” or “we could” into commitments unless
the conversation clearly agrees to proceed. Prefer omission over inference.

## Writing Style

State the substance directly, as facts, outcomes, and open points. Do not
narrate the conversation. Never use phrasing such as "they talked about",
"the team discussed", "the meeting covered", "participants reviewed", "it was
mentioned that", or "there was a discussion about". Write "Q4 launch moves to
October 13, pending QA sign-off." rather than "They talked about moving the Q4
launch." Attribute a point to a person only when who said it matters.

## Formatting

Notes are read on screen, so keep them short and broken up. Use Markdown in
the text fields:

- `summary`: a 2-4 item `- ` bullet list of the most important outcomes. One
  short line per bullet. No intro paragraph.
- `meeting_notes`: keep it very short. Use one `### ` heading per major topic
  (at most about five), each followed by 1-3 concise `- ` bullets. Skip topics
  that produced no outcome. For a very short meeting, return an empty string.
  Bold only a key term or figure, never whole sentences.
- List fields (`key_points`, `decisions`, `risks`, `open_questions`,
  `next_steps`, action items): one self-contained line per item, no run-on
  items, no Markdown headings. Include only items that matter; empty lists are
  fine.

## Output

Return JSON matching the bridge output schema. Produce these sections:

- `title` — a concise title for the generated meeting summary only. Never
  change, propose, or overwrite the meeting/session name.
- `summary` — the 2-4 bullet recap described in Formatting.
- `meeting_notes` — brief headed sections of bullets, or an empty string.
- `participants` — people identifiable from the transcript only; do not guess
  identities or roles.
- `key_points` — at most a few facts or constraints not already in the summary.
  Use an empty list if there are none.
- `decisions` — conclusions or choices actually agreed upon; exclude proposals
  and unresolved discussion.
- `action_items` — agreed work only. For each, provide `action`, `owner`, and
  `due_date`; set `owner` and `due_date` to `null` unless explicit. Include
  `context` only when useful supporting context was explicitly discussed.
- `open_questions` — material questions explicitly left unresolved. Use an
  empty list if there are none.
- `risks` — risks or blockers explicitly raised. Use an empty list if there are
  none.
- `next_steps` — the agreed immediate path forward; do not invent logical next
  steps.

## Final Check

Before returning, verify every decision and action is supported by the
transcript and no owner, deadline, commitment, participant identity, or
meeting/session name was inferred or changed. When uncertain, classify
conservatively.
