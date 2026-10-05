"""Commercial-intent score (§3b).

Start at 1.0 and multiply by each lexicon group's weight once if any of its
terms appear, then cap. Reads the words only, never the author. Same text plus
same lexicon version always gives the same score and terms, so a stored score
can be recomputed from the row (B12).
"""

import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from functools import cache

from jokr.config import IntentLexicon

_QUANTUM = Decimal("0.001")


@dataclass(frozen=True)
class IntentResult:
    score: Decimal
    terms: tuple[str, ...]
    version: str


@cache
def _pattern(term: str) -> re.Pattern[str]:
    words = [re.escape(w) for w in term.split()]
    # Whole words, any whitespace between them, and a simple plural on the last word.
    # The lookarounds stand in for \b so terms ending in symbols ("c++") still work.
    return re.compile(r"(?<![a-z0-9])" + r"\s+".join(words) + r"(?:e?s)?(?![a-z0-9])")


def score_intent(text: str, lexicon: IntentLexicon) -> IntentResult:
    haystack = text.casefold()
    score = Decimal(1)
    matched: list[str] = []
    for group in lexicon.groups:
        hits = [t for t in group.terms if _pattern(t).search(haystack)]
        if hits:
            score *= Decimal(str(group.weight))
            matched.extend(hits)
    score = min(score, Decimal(str(lexicon.cap))).quantize(_QUANTUM, rounding=ROUND_HALF_UP)
    return IntentResult(score, tuple(sorted(matched)), lexicon.version)
