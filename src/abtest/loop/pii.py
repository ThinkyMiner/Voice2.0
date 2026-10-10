"""PII masking for text that leaves the transcript store: excerpts, persona cards, anything a model
or a report might see (PLAN_LOOP.md §L1.9, §L2.5, §L4 L1 agent A).

Regex only, no model call, applied in this order so one mask never hides another:
  1. e-mail addresses                             -> [EMAIL]
  2. digit runs of 7 or more digits               -> [NUMBER]  (ASCII and Devanagari digits, single
                                                     spaces or hyphens between digits, optional +)
  3. 7 or more consecutive spoken number words    -> [NUMBER]  (English, Hindi in Devanagari and
                                                     romanised Hindi; commas, hyphens and spaces between)
  4. known names (case-insensitive, whole words)  -> [NAME]

It errs on the side of masking: an 8-digit date such as 2026-10-06 is masked too. Names are only
what the caller passes in (the buyer name column, a name extracted from the excerpt); a name that
appears nowhere in `known_names` is not found.
"""

import re
from collections.abc import Sequence

_DIGIT = "[0-9\u0966-\u096f]"
_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+")
# A run is a digit, then at least six more, each optionally preceded by one space or hyphen.
_DIGIT_RUN = re.compile(rf"(?<!{_DIGIT})\+?{_DIGIT}(?:[ \-]?{_DIGIT}){{6,}}(?!{_DIGIT})")

# Devanagari letters, matras and digits, but not the danda (U+0964/0965) that ends a sentence.
_LETTER = r"\w\u0900-\u0963\u0966-\u097f"

_NUMBER_WORDS = (
    "zero one two three four five six seven eight nine "
    "शून्य एक दो तीन चार पांच पाँच छह छे छः सात आठ नौ "
    "shunya ek do teen char chaar paanch panch chhe chhah saat aath nau"
).split()
_WORD = "|".join(re.escape(w) for w in sorted(set(_NUMBER_WORDS), key=len, reverse=True))
_SEP = r"[\s,\-]+"
_NUMBER_WORD_RUN = re.compile(rf"(?<![{_LETTER}])(?:{_WORD})(?:{_SEP}(?:{_WORD})){{6,}}(?![{_LETTER}])", re.IGNORECASE)


def mask(text: str, known_names: Sequence[str] = ()) -> str:
    """Replace e-mails, long digit runs, long spoken-number runs and known names with placeholders."""
    if not text:
        return ""
    out = _EMAIL.sub("[EMAIL]", text)
    out = _DIGIT_RUN.sub("[NUMBER]", out)
    out = _NUMBER_WORD_RUN.sub("[NUMBER]", out)
    # Longest first, so "Rahul Sharma" is masked whole before a separate "Rahul" could split it.
    for name in sorted({n.strip() for n in known_names if n and n.strip()}, key=len, reverse=True):
        words = r"\s+".join(re.escape(w) for w in name.split())
        out = re.sub(rf"(?<![{_LETTER}]){words}(?![{_LETTER}])", "[NAME]", out, flags=re.IGNORECASE)
    return out
