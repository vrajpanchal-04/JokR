"""Inbox: files Luca drops into data/inbox/ (saved pages, notes, CSV or JSON exports).

Read-only by design: files are never moved, renamed or deleted, and compose
mounts the folder read-only. Only regular files inside the inbox are read;
symlinks are never followed. Every row is checked by `InboxRow` before ingest,
and anything refused comes back as a Rejection so the run records why.
"""

import csv
import hashlib
import io
import json
import os
from collections.abc import AsyncIterator, Iterator
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from jokr.config import InboxParams
from jokr.connectors.base import FetchedItem, Rejection

ALLOWED_SUFFIXES = frozenset({".csv", ".json", ".md", ".txt"})
SKIPPED_DIRS = frozenset({"node_modules", "__pycache__"})
MAX_FIELD_CHARS = 32 * 1024
MAX_JSON_DEPTH = 20


class InboxRow(BaseModel):
    """One row from a CSV or JSON file. Unknown keys are refused, never ignored."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    text: Annotated[str, Field(min_length=1, max_length=MAX_FIELD_CHARS)]
    title: Annotated[str, Field(max_length=1024)] | None = None
    url: Annotated[str, Field(max_length=2048)] | None = None
    posted_at: datetime | None = None
    points: Annotated[int, Field(ge=0)] | None = None
    num_comments: Annotated[int, Field(ge=0)] | None = None

    @field_validator("title", "url", "posted_at", "points", "num_comments", mode="before")
    @classmethod
    def _blank_is_none(cls, value: Any) -> Any:
        # CSV has no null: an empty cell means "not given".
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator("url")
    @classmethod
    def _web_url_only(cls, value: str | None) -> str | None:
        if value is not None and not value.lower().startswith(("https://", "http://")):
            raise ValueError("url must start with http:// or https://")
        return value


def _row_id(text: str) -> str:
    normalized = " ".join(text.casefold().split())
    return "sha256:" + hashlib.sha256(normalized.encode()).hexdigest()


def _depth(value: Any, limit: int) -> bool:
    """True if `value` nests deeper than `limit`, checked without recursion."""
    stack = [(value, 1)]
    while stack:
        node, depth = stack.pop()
        if depth > limit:
            return True
        if isinstance(node, dict):
            stack.extend((v, depth + 1) for v in node.values())
        elif isinstance(node, list):
            stack.extend((v, depth + 1) for v in node)
    return False


def _first_heading(text: str) -> str | None:
    for line in text.splitlines():
        if line.startswith("#"):
            heading = line.lstrip("#").strip()
            if heading:
                return heading[:1024]
    return None


class InboxConnector:
    def __init__(self, root: Path, params: InboxParams) -> None:
        self._root = root
        self._params = params

    async def fetch(self, since: datetime | None) -> AsyncIterator[FetchedItem | Rejection]:
        # The inbox is re-read whole each run; the DB's unique key drops repeats.
        if not self._root.is_dir():
            return
        root = self._root.resolve()
        files: list[Path] = []
        for entry in self._walk(root, 0):
            if isinstance(entry, Rejection):
                yield entry
            else:
                files.append(entry)
        files.sort()
        for path in files[: self._params.max_files]:
            for item in self._read(root, path):
                yield item
        deferred = len(files) - self._params.max_files
        if deferred > 0:
            yield Rejection(
                "inbox", f"file limit {self._params.max_files} reached; {deferred} deferred"
            )

    def _walk(self, directory: Path, depth: int) -> Iterator[Path | Rejection]:
        with os.scandir(directory) as entries:
            for entry in sorted(entries, key=lambda e: e.name):
                if entry.name.startswith(".") or entry.name in SKIPPED_DIRS:
                    continue
                path = Path(entry.path)
                rel = path.relative_to(self._root.resolve()).as_posix()
                if entry.is_symlink():
                    yield Rejection(rel, "symlink skipped")
                elif entry.is_dir(follow_symlinks=False):
                    if depth < self._params.max_depth:
                        yield from self._walk(path, depth + 1)
                elif entry.is_file(follow_symlinks=False):
                    yield path
                else:
                    yield Rejection(rel, "not a regular file")

    def _read(self, root: Path, path: Path) -> Iterator[FetchedItem | Rejection]:
        rel = path.relative_to(root).as_posix()
        # Belt and braces: the walk never follows links, but never read outside root.
        if not path.resolve().is_relative_to(root):
            yield Rejection(rel, "outside the inbox")
            return
        if path.suffix.lower() not in ALLOWED_SUFFIXES:
            yield Rejection(rel, f"file type {path.suffix or '(none)'} not accepted")
            return
        size = path.lstat().st_size
        if size > self._params.max_file_bytes:
            yield Rejection(rel, f"file is {size} bytes, over {self._params.max_file_bytes}")
            return
        text = path.read_bytes().decode("utf-8", errors="replace")
        suffix = path.suffix.lower()
        if suffix in (".md", ".txt"):
            yield from self._document(rel, path.name, text)
        elif suffix == ".csv":
            yield from self._rows(rel, csv.DictReader(io.StringIO(text)), first_row=2)
        else:
            yield from self._json(rel, text)

    def _document(self, rel: str, name: str, text: str) -> Iterator[FetchedItem | Rejection]:
        title = _first_heading(text) if name.lower().endswith(".md") else None
        yield from self._validated(rel, {"text": text[:MAX_FIELD_CHARS], "title": title or name})

    def _json(self, rel: str, text: str) -> Iterator[FetchedItem | Rejection]:
        try:
            data = json.loads(text)
        except RecursionError:
            yield Rejection(rel, "JSON nested too deep")
            return
        except ValueError as exc:
            yield Rejection(rel, f"invalid JSON: {exc.args[0] if exc.args else exc}"[:200])
            return
        if _depth(data, MAX_JSON_DEPTH):
            yield Rejection(rel, f"JSON nested deeper than {MAX_JSON_DEPTH}")
            return
        rows = data.get("items") if isinstance(data, dict) else data
        if not isinstance(rows, list):
            yield Rejection(rel, "JSON must be a list or an object with an 'items' list")
            return
        yield from self._rows(rel, iter(rows), first_row=1)

    def _rows(
        self, rel: str, rows: Iterator[Any], first_row: int
    ) -> Iterator[FetchedItem | Rejection]:
        try:
            for n, row in enumerate(rows):
                if n >= self._params.max_rows_per_file:
                    yield Rejection(rel, f"row limit {self._params.max_rows_per_file} reached")
                    return
                locator = f"{rel}:{first_row + n}"
                if not isinstance(row, dict):
                    yield Rejection(locator, "row is not an object")
                    continue
                yield from self._validated(locator, row)
        except csv.Error as exc:
            yield Rejection(rel, f"invalid CSV: {exc}"[:200])

    def _validated(self, locator: str, row: dict[Any, Any]) -> Iterator[FetchedItem | Rejection]:
        if None in row:  # csv puts surplus cells under the None key
            yield Rejection(locator, "row has more cells than the header")
            return
        try:
            valid = InboxRow.model_validate(row)
        except ValidationError as exc:
            problems = "; ".join(
                f"{'.'.join(str(p) for p in e['loc']) or 'row'}: {e['msg']}" for e in exc.errors()
            )
            yield Rejection(locator, problems[:300])
            return
        yield FetchedItem(
            external_id=_row_id(valid.text),
            title=valid.title,
            text=valid.text,
            url=valid.url,
            locator=locator,
            points=valid.points,
            num_comments=valid.num_comments,
            posted_at=valid.posted_at,
            raw={"locator": locator},
        )
