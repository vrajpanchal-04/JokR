"""Ingest hygiene: turn any connector's FetchedItem into a storable, untrusted signal.

Every source goes through the same steps, so P2 can rely on them:
clean text, flag injection-like content, hash the author, fingerprint for
dedupe, score commercial intent, and strip author fields from the raw payload.
Nothing here trusts the text; `trust` stays 'untrusted' in the database.
"""

import hashlib
import hmac
import json
import math
import re
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit

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

# Characters that can hide or reorder text. Rather than a hand-kept list, drop
# whole categories: format (bidi controls, zero-width, tags, soft hyphen),
# private-use, surrogates and unassigned. Plus the visible-but-blank fillers and
# variation selectors that are used to smuggle text past a reader.
_HIDDEN_CATEGORIES = frozenset({"Cf", "Co", "Cs", "Cn"})
_HIDDEN_EXTRA = re.compile(
    "[\u034f\u115f\u1160\u17b4\u17b5\u180b-\u180f\u3164\ufe00-\ufe0f\uffa0\U000e0100-\U000e01ef]"
)
# Line and paragraph separators become plain newlines.
_SEPARATORS = re.compile("[\u2028\u2029]")
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


def _strip_hidden(text: str) -> str:
    text = _HIDDEN_EXTRA.sub("", text)
    if text.isascii():
        return text
    return "".join(c for c in text if unicodedata.category(c) not in _HIDDEN_CATEGORIES)


def clean_text(text: str, limit: int = MAX_TEXT_BYTES) -> Cleaned:
    flags: set[str] = set()
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _CONTROL.sub("", text)
    separated = _SEPARATORS.sub("\n", text)
    # NFKC folds fullwidth and other look-alike forms, so a fullwidth "ignore" or
    # "@" is flagged and scrubbed like its plain form. Stripping runs again after
    # folding because folding can turn one filler into another.
    folded = _strip_hidden(unicodedata.normalize("NFKC", _strip_hidden(separated)))
    if separated != text or len(folded) < len(unicodedata.normalize("NFKC", separated)):
        flags.add("hidden_unicode")
    capped = _cap_bytes(folded, limit)
    if capped != folded:
        flags.add("truncated")
    return Cleaned(capped, frozenset(flags))


# --- flags ----------------------------------------------------------------------

# Every pattern here is linear: no nested or adjacent unbounded repeats that can
# backtrack, and character classes are bounded where a run could be long.
_FLAG_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "instruction_like",
        re.compile(
            r"\b(?:ignore|disregard|forget|override)\s{1,20}"
            r"(?:(?:all|any|the|your|my|of|every|everything|these|those|its)\s{1,20}){0,3}"
            r"(?:previous|prior|above|earlier|preceding|instructions|rules|guidelines)\b"
            r"|\byou\s{1,20}are\s{1,20}now\b"
            r"|\b(?:system|developer)\s{1,20}prompt\b"
            r"|\bnew\s{1,20}instructions[ \t]{0,20}:",
            re.IGNORECASE,
        ),
    ),
    (
        "role_marker",
        re.compile(
            r"^[ \t]*(?:system|assistant|user|developer)[ \t]*:"
            r"|<\|im_(?:start|end)\|>|\[/?INST\]|</?(?:system|assistant)>|^#+[ \t]*system\b",
            re.IGNORECASE | re.MULTILINE,
        ),
    ),
    (
        "tool_call_json",
        re.compile(r"[\"'](?:tool|tool_calls|function_call|tool_use)[\"'][ \t]*:", re.IGNORECASE),
    ),
    (
        "html_active",
        re.compile(
            r"<[ \t]{0,20}/?[ \t]{0,20}(?:script|iframe|object|embed|svg|img|style|link|meta|form"
            r"|base)\b|\bon[a-z]{3,20}[ \t]{0,20}=[ \t]{0,20}[\"']",
            re.IGNORECASE,
        ),
    ),
    ("markdown_image", re.compile(r"!\[[^\]\n]{0,500}\]\([^)\n]{0,2000}\)")),
    (
        "markdown_link",
        re.compile(
            r"(?<!!)\[[^\]\n]{1,500}\]\([ \t]*[a-z][a-z0-9+.-]{0,20}:[^)\n]{0,2000}\)", re.I
        ),
    ),
)


def scan_flags(text: str) -> frozenset[str]:
    """Deterministic markers for P2's prompt builder. A flag never drops the item."""
    return frozenset(name for name, pattern in _FLAG_PATTERNS if pattern.search(text))


# --- identity scrubbing --------------------------------------------------------

USER = "[user]"
EMAIL = "[email]"
_NAME = r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,38}"
# A host must start a URL or a word, so "dropbox.com" never matches "x.com".
# Any subdomain may come first: uk.linkedin.com, np.reddit.com, old.reddit.com.
_HOST_START = r"(?<![\w.-])(?:[a-z0-9-]{1,63}\.){0,3}"
_IDENTITY: tuple[tuple[re.Pattern[str], str], ...] = (
    # Profile links. Only the name is replaced; the rest of the link stays readable.
    (re.compile(_HOST_START + rf"((?:twitter|x)\.com/){_NAME}", re.I), rf"\1{USER}"),
    (re.compile(_HOST_START + rf"(linkedin\.com/(?:in|pub)/){_NAME}", re.I), rf"\1{USER}"),
    (re.compile(_HOST_START + rf"(reddit\.com/(?:u|user)/){_NAME}", re.I), rf"\1{USER}"),
    (
        re.compile(_HOST_START + rf"((?:instagram|facebook|fb)\.com/){_NAME}", re.I),
        rf"\1{USER}",
    ),
    (re.compile(_HOST_START + rf"(bsky\.app/profile/){_NAME}", re.I), rf"\1{USER}"),
    (re.compile(_HOST_START + rf"(t\.me/){_NAME}", re.I), rf"\1{USER}"),
    (re.compile(_HOST_START + rf"(nitter\.[a-z.]{{2,30}}/){_NAME}", re.I), rf"\1{USER}"),
    (
        re.compile(_HOST_START + rf"(youtube\.com/(?:c|user|channel)/){_NAME}", re.I),
        rf"\1{USER}",
    ),
    # GitHub/GitLab: a bare /name is a profile; /org/repo/... is a project and stays.
    (
        re.compile(
            _HOST_START
            + r"((?:github|gitlab)\.com/)[A-Za-z0-9-]{1,39}(?=/?(?:[\s?#)\]>\"',.;:!]|$))",
            re.I,
        ),
        rf"\1{USER}",
    ),
    (
        re.compile(
            r"(news\.ycombinator\.com/(?:user|threads|submitted|favorites)\?id=)[A-Za-z0-9_-]+",
            re.I,
        ),
        rf"\1{USER}",
    ),
    # Names passed as query parameters: twitter.com/intent/user?screen_name=bob.
    (
        re.compile(r"([?&](?:screen_name|user_?name|handle)=)[^&#\s]{1,64}", re.I),
        rf"\1{USER}",
    ),
    # /@name on any site: Mastodon, Threads, TikTok, YouTube, Medium.
    (re.compile(r"(?<=/)@[A-Za-z0-9_.]{1,30}"), f"@{USER}"),
    # The lookbehind starts a match only at the start of a run, and the possessive
    # local part never backtracks, so a long run without "@" stays linear.
    (
        re.compile(
            r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]++@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}"
        ),
        EMAIL,
    ),
    # Reddit-style u/name and /user/name in prose.
    (re.compile(r"(?<![\w/])/?u/[A-Za-z0-9_-]{3,20}"), f"u/{USER}"),
    (re.compile(r"(?<![\w])/user/[A-Za-z0-9_-]{3,20}"), f"/user/{USER}"),
    # @handle, but not "python@3.12", "a @ b" or the placeholder itself.
    (re.compile(r"(?<![\w@./+-])@[A-Za-z_][A-Za-z0-9_.]{1,30}"), f"@{USER}"),
)
MIN_NAME_CHARS = 3  # shorter author names ("ab", "pg") are too often ordinary words


@dataclass(frozen=True)
class Scrubbed:
    text: str
    changed: bool


def _name_pattern(names: Sequence[str]) -> re.Pattern[str] | None:
    usable = sorted({n for n in names if len(n) >= MIN_NAME_CHARS}, key=len, reverse=True)
    if not usable:
        return None
    return re.compile(r"(?<![\w])(?:" + "|".join(re.escape(n) for n in usable) + r")(?![\w])", re.I)


def scrub_identity(text: str, names: Sequence[str] = ()) -> Scrubbed:
    """Replace handles, emails, profile links and the given author names.

    Authors are hashed, and nobody else is stored: a name quoted in a reply
    ("thanks, patio11") is as identifying as the author field itself.
    """
    out = text
    for pattern, replacement in _IDENTITY:
        out = pattern.sub(replacement, out)
    named = _name_pattern(names)
    if named is not None:
        out = named.sub(USER, out)
    return Scrubbed(out, out != text)


MAX_RAW_STRING_BYTES = 64 * 1024


def _clean_raw(value: Any, names: Sequence[str], flags: set[str]) -> Any:
    """Clean, flag and scrub every string in raw, the same as the text columns.

    `_safe_raw` has already bounded the depth, so this recursion is bounded too.
    """
    if isinstance(value, str):
        cleaned = clean_text(value, limit=MAX_RAW_STRING_BYTES)
        flags |= cleaned.flags | scan_flags(cleaned.text)
        scrubbed = scrub_identity(cleaned.text, names)
        if scrubbed.changed:
            flags.add("identity_scrubbed")
        return scrubbed.text
    if isinstance(value, dict):
        return {clean_text(k).text: _clean_raw(v, names, flags) for k, v in value.items()}
    if isinstance(value, list):
        return [_clean_raw(v, names, flags) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        # jsonb has no NaN or Infinity; one would fail the whole insert batch.
        flags.add("raw_value_dropped")
        return None
    return value


# --- hashing --------------------------------------------------------------------


def author_hash(salt: bytes, source: str, author: str) -> str:
    """Keyed, per-source pseudonym. Rotating the salt breaks per-author counts."""
    return hmac.new(salt, f"{source}:{author}".encode(), hashlib.sha256).hexdigest()


def content_hash(title: str | None, text: str) -> str:
    normalized = " ".join(f"{title or ''}\n{text}".casefold().split())
    return hashlib.sha256(normalized.encode()).hexdigest()


# Matched anywhere in the key, any casing or separator: authorName, user_id, screen-name.
_AUTHOR_KEY = re.compile(
    r"author|user|owner|creator|screen[_-]?name|handle|e[_-]?mail|nick|login|maker|hunter"
    r"|submitted[_-]?by|created[_-]?by|^by$",
    re.IGNORECASE,
)
# List values like HN's "_tags": ["story", "author_pg"] carry names too.
_AUTHOR_VALUE = re.compile(r"^author_", re.IGNORECASE)


def strip_author_fields(raw: Mapping[str, Any]) -> dict[str, Any]:
    """A copy of `raw` without author-identifying keys or tag values, at any depth."""

    def walk(value: Any, depth: int) -> Any:
        if depth > _RAW_DEPTH:
            return None
        if isinstance(value, Mapping):
            return {
                str(k): walk(v, depth + 1)
                for k, v in value.items()
                if not _AUTHOR_KEY.search(str(k))
            }
        if isinstance(value, list | tuple):
            return [
                walk(v, depth + 1)
                for v in value
                if not (isinstance(v, str) and _AUTHOR_VALUE.match(v))
            ]
        return value

    result: dict[str, Any] = walk(raw, 0)
    return result


def _too_deep(value: Any, limit: int) -> bool:
    stack = [(value, 0)]
    while stack:
        node, depth = stack.pop()
        if depth > limit:
            return True
        if isinstance(node, Mapping):
            stack.extend((v, depth + 1) for v in node.values())
        elif isinstance(node, list | tuple):
            stack.extend((v, depth + 1) for v in node)
    return False


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


FUTURE_SLACK = timedelta(days=1)
MAX_COUNT = 2**31 - 1  # the points and num_comments columns are int4
_FLAG_NAME = re.compile(r"[a-z_]{1,40}")


def _checked_ids(item: FetchedItem) -> None:
    if not 1 <= len(item.external_id) <= MAX_EXTERNAL_ID_CHARS:
        raise IngestRejected(f"external_id must be 1-{MAX_EXTERNAL_ID_CHARS} characters")
    if item.url is not None and len(item.url) > MAX_URL_CHARS:
        raise IngestRejected(f"url over {MAX_URL_CHARS} characters")
    if item.url and not item.url.lower().startswith(("http://", "https://")):
        raise IngestRejected("url must be http:// or https://")
    if not item.url and not item.locator:
        raise IngestRejected("needs a url or locator")
    if item.posted_at is not None and item.posted_at.utcoffset() is None:
        raise IngestRejected("posted_at has no timezone")
    for name in ("points", "num_comments"):
        value = getattr(item, name)
        if value is not None and not 0 <= value <= MAX_COUNT:
            raise IngestRejected(f"{name} must be 0-{MAX_COUNT}")
    for name in ("external_id", "url", "locator"):
        value = getattr(item, name)
        # Ids and links are stored as given, so they must already be clean: no
        # NUL (Postgres refuses it), lone surrogates, or hidden characters.
        if value is not None and clean_text(value).text != value:
            raise IngestRejected(f"{name} has control or hidden characters")


def _safe_raw(raw: Mapping[str, Any], names: Sequence[str]) -> tuple[dict[str, Any], set[str]]:
    if _too_deep(raw, _RAW_DEPTH):
        return {"_omitted": f"raw nested deeper than {_RAW_DEPTH}"}, {"raw_truncated"}
    stripped = strip_author_fields(raw)
    size = len(json.dumps(stripped, default=str, ensure_ascii=False).encode())
    if size > MAX_RAW_BYTES:
        return {"_omitted": "raw over 512 KB"}, {"raw_omitted"}
    flags: set[str] = set()
    cleaned: dict[str, Any] = _clean_raw(stripped, names, flags)
    return cleaned, flags


@dataclass(frozen=True)
class _Texts:
    title: str | None
    text: str
    flags: frozenset[str]


def _clean_title_and_text(item: FetchedItem, names: Sequence[str]) -> _Texts:
    body = clean_text(item.text)
    title = clean_text(item.title, limit=4 * MAX_TITLE_CHARS) if item.title is not None else None
    body_text = scrub_identity(body.text, names)
    title_scrub = scrub_identity(title.text[:MAX_TITLE_CHARS], names) if title else None
    flags = set(body.flags) | (set(title.flags) if title else set())
    if title and len(title.text) > MAX_TITLE_CHARS:
        flags.add("truncated")
    if body_text.changed or (title_scrub and title_scrub.changed):
        flags.add("identity_scrubbed")
    return _Texts(title_scrub.text if title_scrub else None, body_text.text, frozenset(flags))


def _clean_link(url: str | None) -> tuple[str | None, str | None, set[str]]:
    """The stored URL and its dedupe key.

    Only the structural patterns apply here, not the author's name: a commenter
    called "python" must not turn python.org into [user].org. A link that is
    itself a profile is scrubbed and gets no dedupe key, so two different
    people's profiles never count as the same page.
    """
    if url is None:
        return None, None, set()
    # Percent-encoding (twitter.com/%62ob) would hide a name from the patterns.
    scrubbed = scrub_identity(unquote(url))
    if scrubbed.changed:
        return scrubbed.text, None, {"identity_scrubbed", "url_not_canonical"}
    key = canonical_url(url)
    return url, key, set() if key else {"url_not_canonical"}


def prepare(
    item: FetchedItem,
    *,
    source: str,
    salt: bytes,
    lexicon: IntentLexicon,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> SignalRow:
    """Clean, flag, hash and score one item, or raise IngestRejected."""
    if len(salt) < MIN_SALT_BYTES:
        raise ValueError(f"author salt must be at least {MIN_SALT_BYTES} bytes")
    _checked_ids(item)
    names = (item.author,) if item.author else ()
    texts = _clean_title_and_text(item, names)
    if not texts.text.strip() and not texts.title:
        raise IngestRejected("empty after cleaning")

    combined = f"{texts.title or ''}\n{texts.text}"
    raw, raw_flags = _safe_raw(item.raw, names)
    url, url_canonical, url_flags = _clean_link(item.url)
    # Links and file names can carry injection text too; flag it, score only prose.
    scanned = f"{combined}\n{item.url or ''}\n{item.locator or ''}"
    flags = set(texts.flags) | raw_flags | url_flags | scan_flags(scanned)
    flags |= {f for f in item.flags if _FLAG_NAME.fullmatch(f)}
    if item.posted_at is not None and item.posted_at > now() + FUTURE_SLACK:
        flags.add("posted_in_future")
    intent = score_intent(combined, lexicon)

    return SignalRow(
        external_id=item.external_id,
        url=url,
        url_canonical=url_canonical,
        locator=scrub_identity(item.locator).text if item.locator is not None else None,
        title=texts.title,
        text=texts.text,
        author_hash=author_hash(salt, source, item.author) if item.author else None,
        points=item.points,
        num_comments=item.num_comments,
        posted_at=item.posted_at,
        content_hash=content_hash(texts.title, texts.text),
        flags=tuple(sorted(flags)),
        raw=raw,
        intent_score=intent.score,
        intent_terms=intent.terms,
        intent_version=intent.version,
    )
