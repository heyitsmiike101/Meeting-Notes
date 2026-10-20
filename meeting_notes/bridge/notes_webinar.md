# Meeting Notes Workflow: Detailed Webinar

Turn the transcript of a webinar, talk, panel, or training session into
reference notes good enough that the reader never needs to rewatch it. Keep
the substance (how things work, product capabilities, figures,
recommendations) and drop everything else.

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
- In a webinar, "Them" is usually the presenters and the host. Name a
  speaker only when they are introduced or clearly identified; with several
  presenters and no clear cue, write "a panelist" or "the presenter".

## Ground Rules

The transcript is the source of truth. Do not add outside knowledge or fill
gaps with plausible information. Never invent names, titles, employers,
figures, product details, or commitments. Record every statistic, date,
price, version, and capability exactly as stated, with its qualifiers
("roughly", "in beta", "planned"). A vendor's claim about its own product is
attributed to the vendor ("Per the vendor, ..."), not stated as fact. If a
speaker's figures contradict each other, record them as stated and add "(as
stated; figures do not reconcile)".

## What to Capture

- Methods: how each technique, process, or approach works, step by step,
  in enough detail to explain it to a colleague.
- Product and feature details: what it does, how it is deployed, what is
  available now versus planned, limits and prerequisites.
- Figures, statistics, and research findings, exactly as stated.
- Named tools, frameworks, standards, studies, and sources, with what each
  was cited for.
- What the demos showed and what each one proved.
- Recommendations and best practices the speakers gave, and any checklist
  or prioritization they offered.
- Examples, case studies, and incidents described.
- Each question an audience member or moderator actually asked, and the
  answer given. A speaker's own rhetorical questions are not Q&A.
- When a vendor presents its product line, one bullet per capability that
  was actually explained (what it does, any figure claimed). Products only
  named in passing are left out.

Leave out, everywhere in the notes: event logistics (recordings, slides,
code repositories being shared later, CPE certificates, surveys, emails to
attendees, giveaways, booth visits, "more sessions coming"), sponsor ads, speaker bios
beyond name and role, jokes, anecdotes that make no point, and housekeeping.

## Writing Style

State the substance directly, as facts and claims. Do not narrate the
session ("they talked about", "the speaker discussed", "the presenter covered", "it was mentioned
that"). Write "The new tier adds SSO and audit logs at no extra cost." not "The
speaker talked about the new tier." Never put
meta-commentary in the notes ("the transcript is unclear", "the end is cut
off").

## Say Each Thing Once

Every fact goes in exactly one section, and `meeting_notes` is where detail
lives. Before returning, test every line in `key_points`, `risks`,
`open_questions`, and `next_steps`: if it paraphrases any `meeting_notes`
bullet, delete it. An empty list is better than a restatement, and never add
an item saying nothing was found.

## Formatting

- `summary`: two or three sentences on what the session was (host or
  vendor, speakers, subject) and its main message. No bullets.
- `meeting_notes`: the main body, in the order presented. One `### ` heading
  per topic or segment (with several presenters, `### Topic: Speaker`), each
  with as many concise `- ` bullets as the content needs, and nested bullets
  for steps and supporting detail. Bold only a key term or figure. Then, when
  they occurred: `### Q&A` as `- **Question?** Answer.`, and
  `### Resources and links` listing only tools, papers, frameworks, or links the speakers
  recommended or the audience could look up, with what each is for (not
  every product named). Length should fit the substance: a thin panel gets
  short notes, a dense technical session gets long ones.
- List fields: one self-contained line per item.

## Output

Return JSON matching the schema:

- `title`: a concise title describing what was actually presented. Never
  change or propose the meeting name.
- `summary`, `meeting_notes`: as described in Formatting.
- `participants`: presenters and host, with title and employer only when
  stated. No placeholders, no names too garbled to read.
- `key_points`: 2-5 takeaways that pull the session together (a priority
  order, the main recommendations, the figure that matters most). Never a
  single bullet copied or reworded from `meeting_notes`.
- `decisions`: almost always empty. Only a policy, launch, or date the
  speakers formally announced.
- `action_items`: almost always empty. Only something the recorder said they
  would do, or an explicit commitment a speaker made to the audience that the
  recorder needs to act on. Never event logistics. `owner` and `due_date`
  follow the same rules as any meeting: "You" for the recorder, `null` unless
  explicit.
- `open_questions`: questions asked and left unanswered or deferred. Not
  "no Q&A captured".
- `risks`: limitations, caveats, or warnings the speakers raised that are not
  already in `meeting_notes`. Often empty.
- `next_steps`: empty unless the speakers announced a product release date
  or a follow-up session on the same topic. Never logistics.

## Final Check

Before returning, confirm every figure and claim is in the transcript,
vendor claims are attributed, misheard product and technical names are fixed
or marked "(?)", no logistics or filler slipped in, no item appears in two
sections, and no speaker attribution was guessed.
