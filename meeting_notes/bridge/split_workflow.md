# Split Suggestions Workflow

The owner may want to break one long recording into separate meetings. Find
the moments where the conversation clearly moves from one distinct topic,
meeting, or agenda item to another.

## Input

The transcript has one line per utterance: `[123.4s] Speaker: text`. The number
in brackets is the utterance's start time in seconds from the start of the
recording.

## Rules

- Suggest a split only where the subject truly changes (a new agenda item, a
  different project, a different set of participants, a clear "let's move on"),
  not for every sub-point or change of speaker.
- Set `time_sec` to the start time (in seconds, copied from the transcript
  brackets) of the first utterance of the new topic.
- Give each suggestion a short `title` (at most 8 words) naming the topic that
  begins at that point. Use plain nouns; do not narrate ("they discussed").
- Return between 0 and 15 suggestions, in time order. Return an empty list when
  the recording is one continuous topic. Fewer, clearer splits beat many weak
  ones. Never suggest a point in the first or last 30 seconds.
- Do not infer or invent anything that is not in the transcript.

## Output

Return JSON matching the schema: `{"suggestions": [{"time_sec": 1234.5, "title": "Budget review"}]}`.
