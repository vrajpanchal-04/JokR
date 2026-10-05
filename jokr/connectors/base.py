"""What every connector hands to ingest, and the shape of a connector.

Connectors only fetch and map. Cleaning, hashing, scoring and storing happen in
jokr/agents/scout_ingest.py, the same way for every source.
"""

from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol


@dataclass(frozen=True)
class FetchedItem:
    """One item as the source gave it. Every text field here is untrusted."""

    external_id: str
    text: str
    title: str | None = None
    url: str | None = None
    locator: str | None = None
    author: str | None = None
    points: int | None = None
    num_comments: int | None = None
    posted_at: datetime | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)
    # Problems the connector noticed but kept the item for (e.g. "encoding_replaced").
    # Ingest adds them to the signal's flags.
    flags: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Rejection:
    """An item the connector refused before ingest (inbox rows mostly)."""

    locator: str
    reason: str


class MalformedResponse(ValueError):
    """The source answered, but not in the shape its API documents.

    Raised rather than treated as "no results", so a changed or broken API
    fails the run visibly instead of looking like a quiet day.
    """


class Connector(Protocol):
    def fetch(self, since: datetime | None) -> AsyncIterator[FetchedItem | Rejection]: ...
