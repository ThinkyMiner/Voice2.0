"""Role labelling: which diarized speaker is the bot (PLAN_LOOP §L2.2, §L9). The fixtures are synthetic
two-speaker transcripts in Sarvam's batch output format; none is a real call."""

from abtest.loop.contracts import DiarizedSegment, DiarizedTranscript
from abtest.loop.roles import SELLER_ANCHORS, label_roles, opening_lines
from abtest.loop.sarvam import StubProvider


def _diarized(call_id: str):
    return StubProvider().transcribe_batch([None], [call_id])[0]


def _seg(speaker, start, end, text):
    return DiarizedSegment(speaker_id=speaker, start_s=start, end_s=end, text=text)


def _dt(*segments):
    return DiarizedTranscript(call_id="c", language="hi-IN", segments=tuple(segments), stt_model="m",
                              audio_seconds=30.0)


# --- fixtures through the stub STT ---

def test_other_party_speaks_first_the_anchor_still_finds_the_bot():
    t = label_roles(_diarized("fx_other_first"), SELLER_ANCHORS)
    assert t.role_confidence == 0.9
    assert t.turns[0].role == "buyer" and t.turns[0].text == "हाँ।"
    assert t.turns[1].role == "bot" and "क्या मेरी बात" in t.turns[1].text
    assert t.source == "corpus" and t.stt_model == "stub" and t.language == "hi-IN"


def test_consecutive_segments_of_one_role_merge_into_one_turn():
    t = label_roles(_diarized("fx_other_first"), SELLER_ANCHORS)
    assert [x.role for x in t.turns] == ["buyer", "bot", "buyer", "bot", "buyer"]
    merged = t.turns[3]
    assert "माल खुद बनाते" in merged.text and "इंडियामार्ट" in merged.text
    assert (merged.start_s, merged.end_s) == (5.0, 12.5)


def test_bot_first_is_labelled_with_the_anchor():
    t = label_roles(_diarized("fx_bot_first"), SELLER_ANCHORS)
    assert t.role_confidence == 0.9
    assert [x.role for x in t.turns] == ["bot", "buyer", "bot", "buyer"]


def test_no_anchor_falls_back_to_the_speaker_with_most_words():
    t = label_roles(_diarized("fx_no_anchor"), SELLER_ANCHORS)
    assert t.role_confidence == 0.6
    bot_text = " ".join(x.text for x in t.turns if x.role == "bot")
    assert "खरीदारों" in bot_text and "फ्री होंगे" in bot_text
    assert t.turns[0].role == "buyer"


def test_single_speaker_is_all_bot_with_low_confidence():
    t = label_roles(_diarized("fx_single_speaker"), SELLER_ANCHORS)
    assert t.role_confidence == 0.3
    assert [x.role for x in t.turns] == ["bot"]
    assert "क्या आप मुझे सुन" in t.turns[0].text


def test_source_is_passed_through():
    assert label_roles(_diarized("fx_bot_first"), SELLER_ANCHORS, source="dogfood").source == "dogfood"


def test_no_segments_gives_no_turns_and_zero_confidence():
    t = label_roles(_dt(), SELLER_ANCHORS)
    assert t.turns == () and t.role_confidence == 0.0


# --- the matching rule itself ---

def test_anchor_in_the_speakers_second_segment_counts_but_the_third_does_not():
    second = _dt(_seg("0", 0, 1, "हाँ।"), _seg("1", 1, 2, "ठीक है"), _seg("0", 2, 3, "बोलिए"),
                 _seg("1", 3, 4, "यह कॉल इंडियामार्ट से है"))
    assert label_roles(second, SELLER_ANCHORS).role_confidence == 0.9
    third = _dt(_seg("0", 0, 1, "हाँ।"), _seg("1", 1, 2, "ठीक है"), _seg("0", 2, 3, "बोलिए"),
                _seg("1", 3, 4, "अच्छा जी"), _seg("0", 4, 5, "जी बोलिए"), _seg("1", 5, 7, "यह कॉल इंडियामार्ट से है"))
    assert label_roles(third, SELLER_ANCHORS).role_confidence == 0.6


def test_when_both_speakers_match_the_earlier_match_wins():
    t = label_roles(_dt(_seg("1", 0, 2, "Hello, I am the virtual assistant from IndiaMART"),
                        _seg("0", 2, 4, "आप इंडियामार्ट से बोल रहे हैं?")), SELLER_ANCHORS)
    assert t.turns[0].role == "bot"


def test_segments_are_ordered_by_start_time():
    t = label_roles(_dt(_seg("1", 3, 5, "ठीक है तो कल मिलते हैं"),
                        _seg("0", 0, 3, "नमस्ते, क्या मेरी बात आपसे हो रही है?")), SELLER_ANCHORS)
    assert [x.role for x in t.turns] == ["bot", "buyer"]
    assert t.turns[0].start_s == 0


def test_a_long_prompt_line_matches_by_word_overlap():
    prompt_line = "Namaste, I am the virtual assistant calling from IndiaMART about your enquiry"
    t = label_roles(_dt(_seg("0", 0, 2, "हेलो"),
                        _seg("1", 2, 6, "Namaste I am the virtual assistant from IndiaMART regarding your enquiry")),
                    [prompt_line])
    assert t.role_confidence == 0.9 and t.turns[1].role == "bot"


def test_anchor_matching_ignores_case_and_punctuation():
    t = label_roles(_dt(_seg("0", 0, 2, "indiamart, virtual ASSISTANT!"), _seg("1", 2, 3, "ok")), SELLER_ANCHORS)
    assert t.role_confidence == 0.9 and t.turns[0].role == "bot"


def test_seller_anchors_are_the_measured_set():
    assert SELLER_ANCHORS == ("क्या मेरी बात", "से हो रही है", "IndiaMART", "इंडियामार्ट", "virtual assistant", "वर्चुअल")


# --- opening_lines ---

PROMPT = """You are Asha, a voice assistant.
- Greet the buyer: "Namaste, main IndiaMART ki virtual assistant bol rahi hoon."
Do not discuss price.
Introduce yourself as \ufeff bot_name\ufeffbriefly.
नमस्ते! यह पहली पंक्ति है।
Hello again
"""


def test_opening_lines_finds_greeting_like_lines_in_prompt_order():
    lines = opening_lines(PROMPT)
    assert lines[0] == "Namaste, main IndiaMART ki virtual assistant bol rahi hoon."   # the quote, not the instruction
    assert any(l.startswith("Introduce yourself as") for l in lines)
    assert "नमस्ते! यह पहली पंक्ति है।" in lines
    assert "Do not discuss price." not in lines


def test_opening_lines_respects_n_and_drops_variable_markers():
    assert len(opening_lines(PROMPT, n=2)) == 2
    assert all("\ufeff" not in line for line in opening_lines(PROMPT))


def test_opening_lines_of_a_prompt_without_greetings_is_empty():
    assert opening_lines("Ask about quantity.\nAsk about city.") == []


def test_prompt_derived_lines_can_label_roles():
    lines = opening_lines('Say "Namaste, main IndiaMART ki virtual assistant bol rahi hoon" first.')
    t = label_roles(_dt(_seg("0", 0, 2, "हाँ बोलिए"),
                        _seg("1", 2, 5, "Namaste, main IndiaMART ki virtual assistant bol rahi hoon")), lines)
    assert t.role_confidence == 0.9 and t.turns[1].role == "bot"
