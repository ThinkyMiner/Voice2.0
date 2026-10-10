"""Speaker roles: batch diarization labels speakers "0" and "1", never bot or person; this module
decides which one is the bot (PLAN_LOOP.md §L2.2 Roles, §L7.2, §L9).

MEASURED on real seller-flow calls (10 Oct): the bot opens with "नमस्ते, क्या मेरी बात <company> से
हो रही है?", but the other party sometimes speaks first ("हाँ।"), so "the first speaker" is the
wrong rule. The rule is:
  1. the bot is the speaker whose first two segments contain an opening anchor (a substring match
     for short anchors, a word-overlap match for the long lines `opening_lines` extracts from a
     prompt), confidence 0.9; if both speakers match, the one that matches first wins;
  2. otherwise the speaker with the most words, confidence 0.6;
  3. a single speaker is labelled bot throughout, confidence 0.3 (nothing to tell apart).
Role accuracy is part of the human audit, so the confidence is stored, not hidden.
"""

import re
from collections.abc import Sequence

from abtest.loop.contracts import DiarizedTranscript, Source, Transcript, Turn

# Anchors for the seller flow (personalised_vani): how the bot opens, and how it names itself.
SELLER_ANCHORS = ("क्या मेरी बात", "से हो रही है", "IndiaMART", "इंडियामार्ट", "virtual assistant", "वर्चुअल")

_GREETING = re.compile(r"namaste|नमस्ते|hello|greet|introduce", re.IGNORECASE)
_QUOTED = re.compile(r"[\"“«'‘]([^\"”»'’]{3,300})[\"”»'’]")
_MARKER = re.compile(r"\ufeff[^\ufeff]*\ufeff")   # the prompt's variable markers
_BULLET = re.compile(r"^[\s\-*•>#\d.)]+")
_PUNCT = re.compile(r"[^\w\u0900-\u097f\s]")
_MAX_LINE = 300
_ANCHOR_WORD_OVERLAP = 0.6
_BOT_CONFIDENCE = {"anchor": 0.9, "words": 0.6, "single": 0.3}


def opening_lines(prompt_text: str, n: int = 5) -> list[str]:
    """Short lines the bot is told to open with: lines of the prompt that look like a greeting
    (they contain Namaste, नमस्ते, Hello, greet or introduce), in prompt order, at most `n`. A
    quoted phrase inside such a line is preferred to the whole line, because the quote is what the
    bot actually says. Variable markers are dropped; over-long lines are skipped."""
    found: list[str] = []
    for raw in prompt_text.splitlines():
        line = _BULLET.sub("", _MARKER.sub(" ", raw)).strip()
        if not line or not _GREETING.search(line):
            continue
        quoted = [q.strip() for q in _QUOTED.findall(line) if _GREETING.search(q)]
        for candidate in (quoted or [line]):
            candidate = re.sub(r"\s+", " ", candidate)
            if len(candidate) <= _MAX_LINE and candidate not in found:
                found.append(candidate)
        if len(found) >= n:
            break
    return found[:n]


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", _PUNCT.sub(" ", text.casefold())).strip()


def _matches(segment_text: str, anchor: str) -> bool:
    """Short anchor: substring after normalisation. A long anchor (3+ words, a prompt line): most of
    its words appear in the segment, which tolerates the paraphrase STT and prompt variables cause."""
    text, anc = _norm(segment_text), _norm(anchor)
    if not anc:
        return False
    if anc in text:
        return True
    words = anc.split()
    if len(words) < 3:
        return False
    present = set(text.split())
    return sum(w in present for w in words) / len(words) >= _ANCHOR_WORD_OVERLAP


def label_roles(diarized: DiarizedTranscript, opening_lines: Sequence[str],
                source: Source = "corpus") -> Transcript:
    """Role-labelled transcript; consecutive segments of one role merge into one turn."""
    segments = sorted(diarized.segments, key=lambda s: s.start_s)   # stable: ties keep STT order
    speakers = list(dict.fromkeys(s.speaker_id for s in segments))

    if len(speakers) <= 1:
        bot, confidence = (speakers[0] if speakers else None), (_BOT_CONFIDENCE["single"] if speakers else 0.0)
    else:
        bot, confidence = None, 0.0
        best_index = None
        for spk in speakers:
            own = [s for s in segments if s.speaker_id == spk][:2]
            hits = [i for i, seg in enumerate(own) if any(_matches(seg.text, a) for a in opening_lines)]
            if hits:
                first = next(i for i, s in enumerate(segments) if s is own[hits[0]])
                if best_index is None or first < best_index:
                    bot, best_index, confidence = spk, first, _BOT_CONFIDENCE["anchor"]
        if bot is None:
            words = {spk: sum(len(s.text.split()) for s in segments if s.speaker_id == spk) for spk in speakers}
            bot, confidence = max(speakers, key=lambda k: (words[k], -speakers.index(k))), _BOT_CONFIDENCE["words"]

    turns: list[Turn] = []
    for seg in segments:
        role = "bot" if seg.speaker_id == bot else "buyer"
        if turns and turns[-1].role == role:
            last = turns[-1]
            turns[-1] = Turn(role=role, start_s=last.start_s, end_s=max(last.end_s, seg.end_s),
                             text=f"{last.text} {seg.text}".strip())
        else:
            turns.append(Turn(role=role, start_s=seg.start_s, end_s=seg.end_s, text=seg.text.strip()))
    return Transcript(call_id=diarized.call_id, source=source, language=diarized.language, turns=tuple(turns),
                      role_confidence=confidence, stt_model=diarized.stt_model, audio_seconds=diarized.audio_seconds)
