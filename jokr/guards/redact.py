"""Keep credentials out of logs and exception text.

Pattern-based, so it is a safety net, not a guarantee: the first line of defence
is never putting a secret into a message at all. `Secret` makes that the easy
path, because printing one shows nothing.
"""

import logging
import re
from typing import NoReturn

REDACTED = "[REDACTED]"

_VALUE = r"[^\s'\",}&;]+"
_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Authorization: Bearer x / 'Authorization': 'Basic x' / authorization=x
    (
        re.compile(
            rf"(authorization['\"]?\s*[:=]\s*['\"]?)((?:bearer|basic|token)\s+)?{_VALUE}",
            re.IGNORECASE,
        ),
        rf"\1\2{REDACTED}",
    ),
    (re.compile(rf"\b(bearer\s+){_VALUE}", re.IGNORECASE), rf"\1{REDACTED}"),
    # Token-ish fields in JSON, form bodies and query strings.
    (
        re.compile(
            r"((?:access_token|refresh_token|id_token|client_secret|api_key|password)"
            rf"['\"]?\s*[:=]\s*['\"]?){_VALUE}",
            re.IGNORECASE,
        ),
        rf"\1{REDACTED}",
    ),
    # scheme://user:password@host
    (re.compile(r"(://[^/\s:@]+:)[^@\s/]+@"), rf"\1{REDACTED}@"),
)


def redact(text: str) -> str:
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


class Secret:
    """Holds a credential in memory. Prints as [REDACTED]; `reveal()` is the only way out."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return f"Secret({REDACTED!r})"

    def __str__(self) -> str:
        return REDACTED

    def __reduce__(self) -> NoReturn:
        # Pickling would write the value to disk or a queue; refuse outright.
        raise TypeError("Secret cannot be pickled")


class RedactingFilter(logging.Filter):
    """Rewrites a record's final text, including any traceback, before handlers see it."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        if record.stack_info:
            record.stack_info = redact(record.stack_info)
        return True
