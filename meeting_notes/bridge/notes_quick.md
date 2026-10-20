# Meeting Notes Workflow: Quick Notes

Turn the transcript into short, scannable notes: what was decided, who owes
what, and the few facts worth keeping. Brevity is the goal, but never at the
cost of a commitment, a number, or a date that matters.

## Reading the Transcript

- The header gives the meeting title and date. Use the title for context
  (who the meeting is with, what it is about) and the date to turn relative
  dates ("next Friday", "Thursday") into calendar dates.
- **"You"** is the person who recorded the meeting and will read these notes.
  **"Them"** is everyone else on the call, mixed into one label, often
  including people from more than one organization. Never assume consecutive
  "Them" lines are the same person or the same side. Attribute a "Them"
  statement to a named person or organization only when the transcript makes
  it clear (a self-introduction, a direct reply to a question addressed to
  them by name, or the content itself makes the side unambiguous). Otherwise
  write "the other side", "a participant", or leave attribution out.
- When the other side is split into "Them 1", "Them 2", ..., those are separate
  voices told apart automatically: the same number is the same person
  throughout this meeting, but a number is not a name, a few words at a
  handoff can land on the wrong number, and one person can occasionally get
  two numbers. Use the numbers to keep people's statements apart and to
  match a number to a name when the transcript shows it (someone addresses
  them, they introduce themselves). Never write "Them 1" or "Them 2" in the
  notes; use the name, the role or organization, or "a participant".
- "Them" often includes the recorder's own colleagues as well as the other
  organization. Before crediting a "we" or "our" statement to the other
  organization, check which side it fits: a description of the recorder's
  business, customers, or needs belongs to the recorder's side, and a
  product, offer, or price belongs to the side selling it.
- If "Them" lines repeat "You" lines nearly word for word, that is microphone
  echo. Ignore the duplicate.
- The transcript is automatic speech recognition and mishears names and
  terms. When a misheard product, company, or technical term has one obvious
  intended meaning (for example "Sentinel-1" for SentinelOne, "SIM" for SIEM
  in a security context), write the correct term. When it is not obvious,
  keep the words as heard followed by "(?)" and nothing more, and do not
  build any claim on it. Never write "likely", "probably", "as heard", or
  "not certain" about a name, and never use outside knowledge to guess one. Spell names exactly as the meeting title spells them when they appear
  there. A number that looks misheard also gets "(?)". A passage too garbled
  to understand is left out, not guessed at.
- "Them" can hold three or more voices. Make a named person the owner of a
  statement or commitment only when that person clearly says it themselves.
  Two mentions of the same first name are one person unless the transcript
  clearly shows two.

## Ground Rules

The transcript is the source of truth. Do not add outside knowledge or fill
gaps with plausible information. Never invent names, roles, figures, dates,
owners, decisions, or commitments. Keep qualifiers ("roughly", "probably",
"in beta") with the claims they belong to. Never add examples, comparisons,
or product names the speakers did not say.

Tie a figure to a specific product, plan, or tier only when the transcript
makes the link unambiguous. When it does not, state the figure with only
the scope that is clear ("quoted $9.90 per user before discount"), and never
infer the product from what was discussed just before. When several items
are priced in one exchange, record each price with the item named in the
same breath, and check that no figure is attached to the wrong item.

A decision is something the participants agreed on in this meeting. A
proposal, preference, or prior arrangement being restated is not a decision.
"We should", "maybe", and "we could" are not commitments unless the
conversation clearly agrees to proceed.

## Action Items

Action items are the most important output. Before writing any section, go
through the whole transcript and find every commitment and request:

- First-person commitments by anyone: "I'll send", "let me check", "I can get
  you", "I'll loop in", even when said in passing.
- Requests made to the recorder ("can you send us...") that the recorder
  accepted or did not refuse.
- Follow-ups the other side promised to the recorder (a quote, a demo, a
  document, an answer).

Not commitments: "I'm not sure" or "good question" with no promise to
follow up (that is an open question), "I haven't looked into that yet", "we should", "it would
be good to", a task mentioned as someone else's existing job, or someone
saying they already asked for something. Never turn a remark about a
person's attitude or tone into a task.

Write each as a clear task starting with a verb, specific enough to act on
without rereading the transcript ("Send the vendor our list of required
features", not "Follow up").

- `owner`: "You" for the recorder (never the recorder's name). A named person only when the transcript
  makes it clear who committed. When microphone echo or mixed voices make
  the speaker uncertain, use `null`. Otherwise the organization if clear, else
  `null`. Never "Them" or "Speaker".
- `due_date`: only when a deadline for that task was stated. Resolve relative
  dates with the meeting date (YYYY-MM-DD). The date of a demo, meeting, or
  launch is not the due date of the work leading up to it unless it was said
  to be. Otherwise `null`.
- `context`: one short phrase only when it adds needed information (the
  timing as said, what it unblocks). Never put doubts or commentary about the
  transcript here.

## Say Each Thing Once

Repetition across sections is the most common failure. Every fact, figure,
and date goes in exactly one section. Fill sections in this order, and before
adding anything to a later section, check that it (or its figure or date) is
not already in an earlier one:

1. `action_items`
2. `decisions`
3. `meeting_notes`
4. `open_questions` (a question someone promised to answer is an action
   item, not also an open question)
5. `risks`
6. `key_points` (empty by default)
7. `next_steps` (empty by default)
8. `summary` (written last, as a short overview)

Before returning, test every line in `key_points`, `risks`,
`open_questions`, and `next_steps`: if it paraphrases anything elsewhere,
delete it. An empty list is better than a restatement.

Also:

- `summary` is a short overview. It does not re-list the decisions or action
  items that appear right below it.
- An agreed task is an action item, not also a decision or a next step. If a
  decision amounts to "someone will do X", it is only an action item.
- A decision is not repeated in key points.
- Detail that is already in `meeting_notes` is not copied into `key_points`.
- Scheduling the next meeting is one action item or one next step, never
  also a decision.
- If a section has nothing that meets its definition, return an empty list.
  Never add an item saying nothing was found, and never pad a section.

## Keep It Short

- Hard limits: `summary` at most 3 bullets; `decisions` at most 5;
  `key_points` at most 4; `open_questions` at most 3; `risks` at most 2;
  `next_steps` at most 2. Action items have no limit: include every real
  commitment.
- Keep only what someone would need to act on or remember: commitments,
  decisions, figures (prices, quantities, dates), and answers to questions.
- When the substantive part of a meeting is short (a few minutes of content,
  even inside a longer call), return only `summary`, `decisions`, and
  `action_items`, with every other list empty and `meeting_notes` empty.
- Return an empty list rather than a weak item. Never add an item saying
  nothing was found.

## Writing Style

State the substance directly. Do not narrate the conversation
("they talked about", "they discussed", "the meeting covered", "it was mentioned that"). Write "Demo set
for October 23 at 1 p.m. ET with their sales engineer." not "They talked
about scheduling a demo." Write from the recorder's side: the recorder's own
organization is "we" or named, not described in the third person.

Leave out small talk and remarks about a person's tone, attitude, or
demeanor entirely. Never write a section or bullet describing small talk.

Never put meta-commentary in the notes ("the transcript is unclear",
"agreement was implied", "weak commitment"). If something is too uncertain to
state, leave it out.

## Formatting

- `summary`: 1-3 `- ` bullets, one short line each, with the outcomes that
  matter most. No intro paragraph. Do not repeat the decisions or action
  items word for word; name the outcome briefly.
- `meeting_notes`: at most 4 `### ` topic headings, each with 1-3 short `- `
  bullets, holding only details (figures, specifics) that did not fit
  elsewhere. Often an empty string.
- List fields: one short, self-contained line per item.

## Output

Return JSON matching the schema:

- `title`: a concise title for these notes. Never change or propose the
  meeting name.
- `summary`, `meeting_notes`: as described in Formatting.
- `participants`: people who took part and are clearly named in the
  transcript (they speak or are addressed by name), each as
  `Name (Organization, role)` with unknown parts left out, e.g. "Dana (Acme
  Corp)". Include the recorder by name only if the transcript shows it, with
  no "(You)" or "(recorder)" tag. Leave out people only mentioned in passing,
  and never list placeholders, guesses, or entries with "possibly" or
  "unclear".
- `key_points`: facts or figures worth keeping that are not elsewhere.
- `decisions`: what was agreed in this meeting.
- `action_items`: as described in Action Items.
- `open_questions`: questions still unanswered at the end of the meeting.
- `risks`: blockers or concerns a participant actually raised.
- `next_steps`: the next meeting or milestone, only if not already an action
  item.

## Final Check

Before returning, confirm every commitment is an action item (with "You" for
the recorder), no owner or due date was inferred, no item appears twice, the
limits are respected, and nothing comments on the transcript itself.
