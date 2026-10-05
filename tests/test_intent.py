"""Commercial-intent score (§3b): deterministic, versioned, capped, text only."""

from decimal import Decimal
from pathlib import Path

import pytest
import yaml
from hypothesis import given
from hypothesis import strategies as st

from jokr.agents.intent import IntentResult, score_intent
from jokr.config import ConfigError, IntentLexicon, load_intent

REPO_CONFIG = Path(__file__).resolve().parents[1] / "config"


def _lexicon(**overrides: object) -> IntentLexicon:
    data: dict[str, object] = {
        "version": "test.1",
        "cap": 3.0,
        "groups": [
            {"name": "money", "weight": 1.5, "terms": ["payroll", "invoice", "client"]},
            {"name": "pain", "weight": 1.3, "terms": ["latency", "outage"]},
            {"name": "buying", "weight": 1.4, "terms": ["budget for", "alternative to"]},
        ],
    }
    data.update(overrides)
    return IntentLexicon.model_validate(data)


LEX = _lexicon()


def test_repo_lexicon_loads_and_is_versioned() -> None:
    lex = load_intent(REPO_CONFIG)
    assert lex.version
    names = {g.name for g in lex.groups}
    assert {"money", "production_pain", "buying"} <= names


def test_empty_text_scores_one() -> None:
    assert score_intent("", LEX) == IntentResult(Decimal("1.000"), (), "test.1")


def test_one_group_multiplies_once_however_many_hits() -> None:
    result = score_intent("payroll payroll invoice for every client", LEX)
    assert result.score == Decimal("1.500")
    assert result.terms == ("client", "invoice", "payroll")


def test_groups_multiply() -> None:
    result = score_intent("Payroll outage, we have budget for an alternative to it", LEX)
    assert result.score == Decimal("2.730")  # 1.5 * 1.3 * 1.4
    assert result.terms == ("alternative to", "budget for", "outage", "payroll")


def test_cap_holds() -> None:
    lex = _lexicon(cap=2.0)
    assert score_intent("payroll outage budget for", lex).score == Decimal("2.000")


def test_matches_whole_words_and_simple_plurals() -> None:
    assert score_intent("our clients", LEX).terms == ("client",)
    assert score_intent("invoices pile up", LEX).terms == ("invoice",)
    assert score_intent("clientele", LEX).terms == ()
    assert score_intent("subpayroll", LEX).terms == ()


def test_multi_word_terms_tolerate_any_whitespace() -> None:
    assert score_intent("budget\n  for tooling", LEX).terms == ("budget for",)


def test_regex_metacharacters_in_terms_are_literal() -> None:
    lex = _lexicon(groups=[{"name": "odd", "weight": 2.0, "terms": ["c++ build"]}])
    assert score_intent("our c++ build is slow", lex).terms == ("c++ build",)
    assert score_intent("our cc build is slow", lex).terms == ()


@pytest.mark.parametrize(
    "overrides",
    [
        {"version": ""},
        {"cap": 0.5},
        {"cap": 1000},
        {"groups": []},
        {"groups": [{"name": "a", "weight": 0.9, "terms": ["x"]}]},
        {"groups": [{"name": "a", "weight": 1.2, "terms": []}]},
        {"groups": [{"name": "a", "weight": 1.2, "terms": ["Upper"]}]},
        {
            "groups": [
                {"name": "a", "weight": 1.2, "terms": ["x"]},
                {"name": "a", "weight": 1.2, "terms": ["y"]},
            ]
        },
        {
            "groups": [
                {"name": "a", "weight": 1.2, "terms": ["x"]},
                {"name": "b", "weight": 1.2, "terms": ["x"]},
            ]
        },
        {"unknown": 1},
    ],
    ids=[
        "empty-version",
        "cap-below-1",
        "cap-too-big",
        "no-groups",
        "weight-below-1",
        "no-terms",
        "uppercase-term",
        "duplicate-group",
        "term-in-two-groups",
        "unknown-key",
    ],
)
def test_bad_lexicons_are_rejected(overrides: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        _lexicon(**overrides)


def test_bad_lexicon_file_names_the_file(tmp_path: Path) -> None:
    (tmp_path / "intent.yaml").write_text(yaml.safe_dump({"version": "x"}))
    with pytest.raises(ConfigError, match=r"intent\.yaml"):
        load_intent(tmp_path)


@given(st.text(max_size=400))
def test_score_is_bounded_and_reproducible(text: str) -> None:
    first = score_intent(text, LEX)
    assert Decimal(1) <= first.score <= Decimal("3.000")
    assert score_intent(text, LEX) == first
    assert first.score == first.score.quantize(Decimal("0.001"))
