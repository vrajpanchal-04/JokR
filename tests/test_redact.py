"""Secrets never reach logs or exception text."""

import logging

import pytest
from hypothesis import given
from hypothesis import strategies as st

from jokr.guards.redact import REDACTED, RedactingFilter, Secret, redact

# Built at runtime so secret scanners don't flag the fixture.
TOKEN = "tok" + "En-" + "a1B2c3D4e5F6g7H8i9J0"


@pytest.mark.parametrize(
    "text",
    [
        f"Authorization: Bearer {TOKEN}",
        f"authorization: bearer {TOKEN}",
        f"headers={{'Authorization': 'Bearer {TOKEN}'}}",
        f'{{"access_token": "{TOKEN}", "token_type": "bearer"}}',
        f"access_token={TOKEN}&expires_in=3600",
        f"client_secret={TOKEN}",
        f"https://u:{TOKEN}@example.com/x",
        f"Authorization: Basic {TOKEN}",
    ],
)
def test_known_secret_shapes_are_masked(text: str) -> None:
    out = redact(text)
    assert TOKEN not in out
    assert REDACTED in out


def test_ordinary_text_is_untouched() -> None:
    text = "fetched 42 hits from hn.algolia.com in 0.3s"
    assert redact(text) == text


@given(
    st.text(
        alphabet=st.characters(min_codepoint=33, max_codepoint=126, blacklist_characters="\"',&;}"),
        min_size=8,
        max_size=60,
    )
)
def test_any_bearer_value_is_masked(value: str) -> None:
    assert value not in redact(f"Authorization: Bearer {value}")


def test_secret_holder_never_prints_its_value() -> None:
    s = Secret(TOKEN)
    for text in (repr(s), str(s), f"{s}", f"{s!r}", str([s]), str({"t": s})):
        assert TOKEN not in text
    assert s.reveal() == TOKEN


def test_secret_holder_refuses_pickling() -> None:
    import pickle

    with pytest.raises(TypeError):
        pickle.dumps(Secret(TOKEN))


def test_logging_filter_masks_message_and_args(caplog: pytest.LogCaptureFixture) -> None:
    log = logging.getLogger("jokr.test.redact")
    log.addFilter(RedactingFilter())
    with caplog.at_level(logging.INFO, logger="jokr.test.redact"):
        log.info("got %s", f"Bearer {TOKEN}")
        log.info(f"raw access_token={TOKEN}")
    assert TOKEN not in caplog.text
    assert caplog.text.count(REDACTED) == 2


def test_logging_filter_masks_exception_text(caplog: pytest.LogCaptureFixture) -> None:
    log = logging.getLogger("jokr.test.redact.exc")
    log.addFilter(RedactingFilter())
    with caplog.at_level(logging.ERROR, logger="jokr.test.redact.exc"):
        try:
            raise RuntimeError(f"upstream said Authorization: Bearer {TOKEN}")
        except RuntimeError:
            log.exception("boom")
    assert TOKEN not in caplog.text
