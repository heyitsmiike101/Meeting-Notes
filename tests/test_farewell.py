"""The goodbye detector behind Auto end "When people say goodbye": pure text, no Qt."""

from __future__ import annotations

import pytest

from meeting_notes.client.farewell import find_farewell, is_farewell, words

FAREWELLS = [
    "Bye",
    "bye bye",
    "Bye now!",
    "Okay, bye everyone.",
    "BYE!!",
    "Goodbye",
    "good bye",
    "Goodbye, everyone.",
    "See you",
    "See ya",
    "See you later",
    "see you soon!",
    "See you next time",
    "see you tomorrow",
    "See you next week.",
    "Alright, see you all later",
    "See you on Monday",
    "talk to you later",
    "Talk to you soon.",
    "Talk soon",
    "Take care",
    "take care, everybody",
    "Have a good one",
    "Have a good day",
    "have a great weekend",
    "Have a good night",
    "Have a good rest of your day",
    "have a nice evening",
    "Catch you later",
    "Later everyone",
    "Thanks everyone",
    "Thank you everyone.",
    "thanks all",
    "Thanks guys",
    "thanks everyone, bye",
    "Cheers",
    "Cheers!",
    "That is all from me, thanks, see you later",
    "it\u2019s been great, bye",
]

NOT_FAREWELLS = [
    "",
    "   ",
    "by the way",
    "by",
    "buy",
    "I want to buy it",
    "bypass the cache",
    "maybe",
    "maybe later",
    "byte size",
    "see you're right",
    "I see you have a point",
    "can you see you on the call",
    "take care of the deploy",
    "Let's take care of that after",
    "goodbye to the old process",
    "good by itself",
    "have a good plan",
    "talk about it later",
    "later in the meeting",
    "cheerful",
    "thanks everyone for joining today we will start with the roadmap and then the budget",
    "thanks for the update",
    "thank you",
    "all good",
    "let's see",
]


@pytest.mark.parametrize("text", FAREWELLS)
def test_farewells_are_heard(text):
    assert is_farewell(text), text


@pytest.mark.parametrize("text", NOT_FAREWELLS)
def test_everyday_speech_is_not_a_farewell(text):
    assert not is_farewell(text), text


def test_find_farewell_names_the_phrase_and_never_returns_the_transcript():
    assert find_farewell("Okay everyone, I think that is it, bye bye") == "bye bye"
    assert find_farewell("see you next week.") == "see you next week"
    assert find_farewell("nothing to see here") is None


def test_a_long_partial_counts_only_when_the_phrase_is_at_its_end():
    filler = " ".join(["and"] * 20)
    assert not is_farewell(f"bye {filler}")                    # a goodbye far from the end
    assert not is_farewell(f"goodbye {filler} and then more")
    assert is_farewell(f"{filler} talk soon")
    assert is_farewell(f"{filler} okay thanks bye")
    assert is_farewell(f"{filler} have a good one")            # 4 words, inside the last 6
    assert not is_farewell(f"{filler} bye " + " ".join(["more"] * 6))


def test_a_short_partial_counts_anywhere():
    assert is_farewell("bye and then one more thing")
    assert is_farewell("so yeah that is it for me bye")  # 9 words, below the long limit


def test_thanks_the_room_needs_a_short_partial_and_to_close_it():
    assert is_farewell("thanks everyone")
    assert is_farewell("ok thanks everyone so much")
    assert not is_farewell("thanks everyone for joining")
    assert not is_farewell("so thanks everyone and welcome to the weekly sync call today team")


def test_words_ignores_punctuation_and_keeps_apostrophes():
    assert words("Bye-bye, y\u2019all!") == ["bye", "bye", "y'all"]
    assert words(None) == []
