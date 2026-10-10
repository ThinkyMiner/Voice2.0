"""PII masking: digit runs, spoken numbers, e-mails and known names (PLAN_LOOP §L1.9)."""

import pytest

from abtest.loop.pii import mask


@pytest.mark.parametrize("text, expected", [
    ("call 9876543210 now", "call [NUMBER] now"),
    ("call 98765 43210 now", "call [NUMBER] now"),
    ("call 98765-43210 now", "call [NUMBER] now"),
    ("call +91 98765 43210 now", "call [NUMBER] now"),
    ("call ९८७६५४३२१० now", "call [NUMBER] now"),
    ("call ९८७६५ ४३२१० now", "call [NUMBER] now"),
    ("1234567", "[NUMBER]"),
    ("12-34-567", "[NUMBER]"),
])
def test_digit_runs_of_seven_or_more_are_masked(text, expected):
    assert mask(text) == expected


@pytest.mark.parametrize("text", [
    "pin 123456", "I want 500 pieces", "order 1234 and 56", "rate is 12.50 per piece", "1,00,000 rupees", "",
])
def test_short_numbers_are_left_alone(text):
    assert mask(text) == text


@pytest.mark.parametrize("text, expected", [
    ("my number is nine eight seven six five four three two one zero", "my number is [NUMBER]"),
    ("Nine Eight Seven Six Five Four Three", "[NUMBER]"),
    ("नौ आठ सात छह पाँच चार तीन", "[NUMBER]"),
    ("नौ, आठ, सात, छह, पांच, चार, तीन, दो", "[NUMBER]"),
    ("शून्य एक दो तीन चार पांच छह", "[NUMBER]"),
    ("nau aath saat chhe paanch char teen do", "[NUMBER]"),
    ("ek do teen char paanch chhe saat", "[NUMBER]"),
    ("number hai nine eight seven six five four three. Thanks", "number hai [NUMBER]. Thanks"),
    ("नंबर है नौ आठ सात छह पाँच चार तीन। धन्यवाद", "नंबर है [NUMBER]। धन्यवाद"),
])
def test_runs_of_seven_number_words_are_masked(text, expected):
    assert mask(text) == expected


@pytest.mark.parametrize("text", [
    "one two three four five six",
    "नौ आठ सात छह पाँच चार",
    "ek do teen",
    "I have one product and two options, three sizes",
    "नौकरी एकता दोनों तीनों चारों पांचवां",   # words that merely start with a number word
])
def test_fewer_than_seven_number_words_are_left_alone(text):
    assert mask(text) == text


def test_emails_are_masked_before_digit_runs_can_split_them():
    assert mask("write to buyer.name+x@example.co.in please") == "write to [EMAIL] please"
    assert mask("x1234567@y.com") == "[EMAIL]"


def test_known_names_are_masked_case_insensitively_on_word_boundaries():
    assert mask("Rahul ji bol raha hoon, RAHUL here", ["Rahul"]) == "[NAME] ji bol raha hoon, [NAME] here"
    assert mask("Rahulesh and Rahul", ["Rahul"]) == "Rahulesh and [NAME]"
    assert mask("Mr. Rahul-Kumar", ["Rahul"]) == "Mr. [NAME]-Kumar"


def test_multi_word_and_devanagari_names():
    assert mask("this is Rahul   Sharma speaking", ["Rahul Sharma"]) == "this is [NAME] speaking"
    assert mask("राजेश जी बोल रहे हैं", ["राजेश"]) == "[NAME] जी बोल रहे हैं"
    assert mask("राजेशकुमार जी", ["राजेश"]) == "राजेशकुमार जी"


def test_the_longer_name_is_masked_whole_first():
    assert mask("Rahul Sharma and Rahul", ["Rahul", "Rahul Sharma"]) == "[NAME] and [NAME]"


def test_names_with_regex_characters_and_blank_names_are_safe():
    assert mask("call (Mr. X) now", ["(Mr. X)", "", "  "]) == "call [NAME] now"
    assert mask("anything", ["", " "]) == "anything"


def test_everything_together():
    text = "Rahul here, 98765 43210, rahul@example.com, nine eight seven six five four three"
    assert mask(text, ["Rahul"]) == "[NAME] here, [NUMBER], [EMAIL], [NUMBER]"


def test_empty_and_none_like_input():
    assert mask("") == ""
    assert mask(None) == ""   # type: ignore[arg-type]
