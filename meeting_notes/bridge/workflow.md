# Meeting Notes Bridge Workflow

Transform the supplied transcript into accurate, structured notes for the
meeting-notes library.

## Ground Rules

The transcript is the source of truth. Summarize and organize only what was
actually said; do not add outside knowledge or fill gaps with plausible
information. Preserve uncertainty and disagreement. Never invent names,
roles, dates, owners, deadlines, decisions, or commitments.

Distinguish discussion and proposals from actual decisions. Mark something as
an action item only when the transcript establishes agreed work. Do not infer
an action owner from context or responsibility. Use `null` for an action owner
or due date unless explicitly established by the transcript. Do not turn
suggestions such as “we should,” “maybe,” or “we could” into commitments unless
the conversation clearly agrees to proceed. Prefer omission or uncertainty
over inference.

## Writing Style

State the substance of the meeting directly, as facts, outcomes, and open
points. Do not narrate the conversation. Never use phrasing such as "they
talked about", "the team discussed", "the meeting covered", "participants
reviewed", "it was mentioned that", or "there was a discussion about".
Write "Q4 launch moves to October 13, pending QA sign-off." rather than
"They talked about moving the Q4 launch." This applies to every section,
especially `summary`, `meeting_notes`, and `key_points`. Attribute a point to
a person only when who said it matters.

## Output

Return JSON matching the bridge output schema. Produce these sections:

- `title` — a concise title for the generated meeting summary only. Never
  change, propose, or overwrite the meeting/session name.
- `summary` — a concise overview of what was decided, learned, and left open,
  stated directly (see Writing Style).
- `meeting_notes` — a polished, concise narrative organized by topic rather
  than transcript order. Capture material context, discussion, outcomes, and
  unresolved issues while removing filler and repetition.
- `participants` — people identifiable from the transcript only; do not guess
  identities or roles.
- `key_points` — important facts, context, requirements, constraints, or
  discussion points.
- `decisions` — conclusions or choices actually agreed upon; exclude proposals
  and unresolved discussion.
- `action_items` — agreed work only. For each, provide `action`, `owner`, and
  `due_date`; set `owner` and `due_date` to `null` unless explicit. Include
  `context` only when useful supporting context was explicitly discussed.
- `open_questions` — material questions explicitly raised or left unresolved.
- `risks` — risks, blockers, dependencies, or concerns discussed.
- `next_steps` — the agreed immediate path forward; do not invent logical next
  steps.

## Final Check

Before returning, verify every decision and action is supported by the
transcript and no owner, deadline, commitment, participant identity, or
meeting/session name was inferred or changed. When uncertain, classify
conservatively.
