"""Ingest hygiene: turn any connector's FetchedItem into a storable, untrusted signal.

Every source goes through the same steps, so P2 can rely on them:
clean text, flag injection-like content, hash the author, fingerprint for
dedupe, score commercial intent, and strip author fields from the raw payload.
Nothing here trusts the text; `trust` stays 'untrusted' in the database.
"""

import hashlib
import hmac
import json
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit

from jokr.agents.intent import score_intent
from jokr.config import IntentLexicon
from jokr.connectors.base import FetchedItem

MAX_TEXT_BYTES = 32 * 1024
MAX_TITLE_CHARS = 1024
MAX_URL_CHARS = 2048
MAX_EXTERNAL_ID_CHARS = 512
MAX_RAW_BYTES = 512 * 1024
MIN_SALT_BYTES = 32
_RAW_DEPTH = 32


class IngestRejected(ValueError):
    """The item cannot be stored as a signal. The message says why."""


# --- cleaning -------------------------------------------------------------------

# Invisible characters that can hide or reorder text: bidi controls, zero-width
# characters, the BOM, and Unicode tag characters (used to smuggle hidden prompts).
_HIDDEN = re.compile("[؜​-‏‪-‮⁠-⁤⁦-⁩﻿\U000e0000-\U000e007f]")
# C0 controls except tab and newline, DEL, and C1 controls.
_CONTROL = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f]")


@dataclass(frozen=True)
class Cleaned:
    text: str
    flags: frozenset[str]


def _cap_bytes(text: str, limit: int) -> str:
    data = text.encode()
    if len(data) <= limit:
        return text
    # Cutting bytes can split a character; drop the partial one rather than mangle it.
    return data[:limit].decode(errors="ignore")


def clean_text(text: str, limit: int = MAX_TEXT_BYTES) -> Cleaned:
    flags: set[str] = set()
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if _HIDDEN.search(text):
        flags.add("hidden_unicode")
        text = _HIDDEN.sub("", text)
    text = _CONTROL.sub("", text)
    text = unicodedata.normalize("NFC", text)
    capped = _cap_bytes(text, limit)
    if capped != text:
        flags.add("truncated")
    return Cleaned(capped, frozenset(flags))


# --- flags ----------------------------------------------------------------------

_FLAG_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "instruction_like",
        re.compile(
            r"\b(ignore|disregard|forget|override)\s+(all\s+|any\s+|the\s+)?"
            r"(previous|prior|above|earlier|preceding)\b"
            r"|\byou\s+are\s+now\b"
            r"|\b(system|developer)\s+prompt\b"
            r"|\bnew\s+instructions\s*:",
            re.IGNORECASE,
        ),
    ),
    (
        "role_marker",
        re.compile(
            r"(^|\n)\s*(system|assistant|user|developer)\s*:"
            r"|<\|im_(start|end)\|>|\[/?INST\]|</?(system|assistant)>|^#+\s*system\b",
            re.IGNORECASE | re.MULTILINE,
        ),
    ),
    (
        "tool_call_json",
        re.compile(r"\"(tool|tool_calls|function_call|tool_use)\"\s*:", re.IGNORECASE),
    ),
    ("markdown_image", re.compile(r"!\[[^\]]*\]\([^)]*\)")),
    ("markdown_link", re.compile(r"(?<!!)\[[^\]]+\]\(\s*[a-z][a-z0-9+.-]*:[^)]*\)", re.I)),
)


def scan_flags(text: str) -> frozenset[str]:
    """Deterministic markers for P2's prompt builder. A flag never drops the item."""
    return frozenset(name for name, pattern in _FLAG_PATTERNS if pattern.search(text))


# --- hashing --------------------------------------------------------------------


def author_hash(salt: bytes, source: str, author: str) -> str:
    """Keyed, per-source pseudonym. Rotating the salt breaks per-author counts."""
    return hmac.new(salt, f"{source}:{author}".encode(), hashlib.sha256).hexdigest()


def content_hash(title: str | None, text: str) -> str:
    normalized = " ".join(f"{title or ''}\n{text}".casefold().split())
    return hashlib.sha256(normalized.encode()).hexdigest()


_AUTHOR_KEY = re.compile(
    r"^(author|authors|author_.*|by|user|users|username|user_name|submitted_by|"
    r"created_by|owner|email|e_mail|maker|makers|hunter)$",
    re.IGNORECASE,
)


def strip_author_fields(raw: Mapping[str, Any]) -> dict[str, Any]:
    """A copy of `raw` without author-identifying keys, at any depth."""

    def walk(value: Any, depth: int) -> Any:
        if depth > _RAW_DEPTH:
            return None
        if isinstance(value, Mapping):
            return {
                str(k): walk(v, depth + 1)
                for k, v in value.items()
                if not _AUTHOR_KEY.match(str(k))
            }
        if isinstance(value, list | tuple):
            return [walk(v, depth + 1) for v in value]
        return value

    result: dict[str, Any] = walk(raw, 0)
    return result


# --- URL canonicalization -------------------------------------------------------

_TRACKING = re.compile(
    r"^(utm_.*|ref|ref_src|ref_url|fbclid|gclid|dclid|msclkid|mc_cid|mc_eid|"
    r"igshid|yclid|_hsenc|_hsmi|si)$",
    re.IGNORECASE,
)


def canonical_url(url: str | None) -> str | None:
    """A dedupe key only; the original URL is always stored unchanged."""
    if not url:
        return None
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        return None
    host = parts.hostname.lower().removeprefix("www.").removesuffix(".")
    port = f":{parts.port}" if parts.port not in (None, 80, 443) else ""
    path = parts.path.rstrip("/")
    params = sorted(
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not _TRACKING.match(k)
    )
    query = f"?{urlencode(params)}" if params else ""
    return f"{host}{port}{path}{query}"


# --- prepare --------------------------------------------------------------------


@dataclass(frozen=True)
class SignalRow:
    """Exactly the columns Scout inserts into `signals` (minus source_id and run_id)."""

    external_id: str
    url: str | None
    url_canonical: str | None
    locator: str | None
    title: str | None
    text: str
    author_hash: str | None
    points: int | None
    num_comments: int | None
    posted_at: datetime | None
    content_hash: str
    flags: tuple[str, ...]
    raw: dict[str, Any]
    intent_score: Decimal
    intent_terms: tuple[str, ...]
    intent_version: str


def _checked_ids(item: FetchedItem) -> None:
    if not 1 <= len(item.external_id) <= MAX_EXTERNAL_ID_CHARS:
        raise IngestRejected(f"external_id must be 1-{MAX_EXTERNAL_ID_CHARS} characters")
    if item.url is not None and len(item.url) > MAX_URL_CHARS:
        raise IngestRejected(f"url over {MAX_URL_CHARS} characters")
    if not item.url and not item.locator:
        raise IngestRejected("needs a url or locator")
    for name in ("points", "num_comments"):
        value = getattr(item, name)
        if value is not None and value < 0:
            raise IngestRejected(f"{name} is negative")


def _safe_raw(raw: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
    stripped = strip_author_fields(raw)
    size = len(json.dumps(stripped, default=str, ensure_ascii=False).encode())
    if size > MAX_RAW_BYTES:
        return {"_omitted": "raw over 512 KB"}, True
    return stripped, False


def prepare(item: FetchedItem, *, source: str, salt: bytes, lexicon: IntentLexicon) -> SignalRow:
    """Clean, flag, hash and score one item, or raise IngestRejected."""
    if len(salt) < MIN_SALT_BYTES:
        raise ValueError(f"author salt must be at least {MIN_SALT_BYTES} bytes")
    _checked_ids(item)

    body = clean_text(item.text)
    title = clean_text(item.title, limit=4 * MAX_TITLE_CHARS) if item.title is not None else None
    title_text = title.text[:MAX_TITLE_CHARS] if title else None
    if not body.text.strip() and not title_text:
        raise IngestRejected("empty after cleaning")

    flags = set(body.flags) | (set(title.flags) if title else set())
    if title and title.text != title_text:
        flags.add("truncated")
    combined = f"{title_text or ''}\n{body.text}"
    flags |= scan_flags(combined)
    raw, omitted = _safe_raw(item.raw)
    if omitted:
        flags.add("raw_omitted")
    intent = score_intent(combined, lexicon)

    return SignalRow(
        external_id=item.external_id,
        url=item.url,
        url_canonical=canonical_url(item.url),
        locator=item.locator,
        title=title_text,
        text=body.text,
        author_hash=author_hash(salt, source, item.author) if item.author else None,
        points=item.points,
        num_comments=item.num_comments,
        posted_at=item.posted_at,
        content_hash=content_hash(title_text, body.text),
        flags=tuple(sorted(flags)),
        raw=raw,
        intent_score=intent.score,
        intent_terms=intent.terms,
        intent_version=intent.version,
    )
