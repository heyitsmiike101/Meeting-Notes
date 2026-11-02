# Name From Introductions Workflow

Some voices in this meeting are not named yet (the transcript calls them "Them 1", "In room 2", "Speaker 3" and
so on). Your only job is to say, for each of those unnamed speakers, whether they gave their own name or were
introduced by someone else. You are not writing notes and you are not summarising anything.

## Input

The transcript file starts with a short header:

- the **unnamed voices**: the only labels you may name (the candidates);
- the voices that are **already named** (leave them alone);
- who "You" is: the person who recorded the meeting.

Below the header is a transcript excerpt (the first part of the meeting plus later lines that look like
introductions), one line per utterance: `[123.4s] Speaker: text`.

## Rules

- Name a candidate only when the transcript shows it directly: the speaker says their own name ("Hi, I'm Dana from
  Acme", "My name is Dana") or someone else introduces them and it is clear who ("This is Dana, she runs ops" followed
  by that speaker answering, or "Dana, go ahead" followed by the candidate speaking and the context making it plain).
- Never guess from context, job titles, topics, tone, email addresses or who usually speaks. If it is not clear
  which unnamed speaker a name belongs to, leave it out.
- Never name "You", and never name a voice that is not in the unnamed list.
- At most one entry per label. Use the label exactly as written in the header.
- `name` is the person's name as spoken (first name alone is fine). `company` only when it is stated in the
  transcript, otherwise null.
- `quote` must be the exact words from the transcript that show it (copy them; do not paraphrase or merge lines).
- `confidence` is `high` ONLY for an introduction: the speaker says their own name ("I'm Dana", "my name is Dana",
  "Dana here", "it's Dana from Acme") or someone introduces them ("this is Dana, she runs ops", "let me introduce
  Dana") and that speaker then talks. Everything else is `low` at most.
- Being spoken to by name is NOT an introduction and is never `high`: "Thanks, Amanda", "Matt, where are we with
  this?", "over to Craig", "Andrew, you want to start?". The name may belong to the previous speaker, the next one or
  someone who never speaks; give it `low` only when the candidate clearly answers right after, otherwise leave it out.
  A name that looks misheard (not a usual first name, e.g. "Spot") is left out.
- Return an empty list when nobody is introduced. Fewer, clearer entries beat many weak ones.

## Output

Return JSON matching the schema:
`{"names": [{"label": "Them 2", "name": "Dana", "company": "Acme", "quote": "Hi, I'm Dana from Acme", "confidence": "high"}]}`
