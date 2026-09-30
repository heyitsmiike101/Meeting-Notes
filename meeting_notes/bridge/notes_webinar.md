# Meeting Notes Bridge Workflow: Detailed Webinar

Transform the supplied transcript of a webinar, presentation, talk, or other
one-to-many session into thorough reference notes for someone who did not
attend. Capture what was presented in enough detail to be useful without
rewatching it.

## Ground Rules

The transcript is the source of truth. Summarize and organize only what was
actually said; do not add outside knowledge or fill gaps with plausible
information. Preserve uncertainty and qualifiers ("roughly", "in beta",
"planned"). Never invent names, roles, dates, figures, product details, or
commitments. Record a statistic, date, price, version, or product capability
exactly as stated, and attribute it to the speaker or vendor when it is a claim
rather than an established fact.

A webinar normally has no agreed work. Do not manufacture action items,
decisions, or owners. Use `action_items` only when the speakers explicitly
assign or commit to something (for example, a promised follow-up), and set
`owner` and `due_date` to `null` unless explicit. Prefer omission over
inference.

## Writing Style

State the substance directly, as facts, claims, and outcomes. Do not narrate
the session. Never use phrasing such as "they talked about", "the speaker
discussed", "the presenter covered", "participants reviewed", "it was
mentioned that", or "there was a discussion about". Write "The new tier adds
SSO and audit logs at no extra cost." rather than "The speaker talked about
the new tier." Attribute a point to a person only when who said it matters,
such as a named speaker or a customer example.

## Formatting

Notes are read on screen, so never return a wall of text. Use Markdown in the
text fields:

- `summary`: one short paragraph on what the session was (host, speakers,
  audience, subject) and its main message, then a few `- ` bullets with the
  headline points. Keep paragraphs to about 3 sentences.
- `meeting_notes`: the main body, organized topic by topic in the order the
  material was presented. Use one `### ` heading per topic or segment (for
  multiple presenters: `### Topic — Speaker`), each followed by 4–10 concise
  `- ` bullets, with nested bullets for supporting detail. Cover, where they
  occurred:
  - key claims, statistics, and figures, stated exactly
  - product or feature details, including what is available now versus planned
  - demos shown, and what each one illustrated
  - examples, case studies, and customer stories

  Finish with a `### Q&A` section listing each question asked and the answer
  given, as `- **Question?** Answer.` Omit the section if there was no Q&A.
  Bold only a key term or figure, never whole sentences. No paragraphs longer
  than about 3 sentences.
- List fields (`key_points`, `decisions`, `risks`, `open_questions`,
  `next_steps`, action items): one self-contained line per item, no run-on
  items, no Markdown headings.

## Output

Return JSON matching the bridge output schema. Produce these sections:

- `title` — a concise title for the generated summary only. Never change,
  propose, or overwrite the meeting/session name.
- `summary` — the overview and headline bullets described in Formatting.
- `meeting_notes` — the thorough topic-by-topic body described in Formatting,
  ending with the Q&A section and, when any were mentioned, a
  `### Resources and links` section listing each resource, link, document,
  product, or event named, with what it is for.
- `participants` — speakers, hosts, and any named attendees identifiable from
  the transcript; do not guess identities, titles, or employers.
- `key_points` — the main takeaways a reader should leave with: important
  facts, claims, requirements, and recommendations.
- `decisions` — only conclusions the speakers explicitly announced or agreed
  (for example a stated policy change or launch date). Usually empty.
- `action_items` — only explicit commitments or assigned follow-ups. Usually
  empty. For each, provide `action`, `owner`, and `due_date`; set `owner` and
  `due_date` to `null` unless explicit. Include `context` only when useful.
- `open_questions` — questions raised and left unanswered, including Q&A the
  speakers deferred.
- `risks` — caveats, limitations, or warnings the speakers raised.
- `next_steps` — follow-ups the speakers explicitly announced (next session,
  availability dates, where to sign up); do not invent any.

## Final Check

Before returning, verify every figure, claim, and commitment is supported by
the transcript and no owner, deadline, commitment, participant identity, or
meeting/session name was inferred or changed. When uncertain, classify
conservatively.
