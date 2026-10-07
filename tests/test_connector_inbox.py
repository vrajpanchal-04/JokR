"""Inbox connector: Luca's own files, read-only, every row validated before ingest."""

import json
import os
from pathlib import Path
from typing import Any

import pytest

from jokr.config import InboxParams
from jokr.connectors.base import FetchedItem, Rejection
from jokr.connectors.inbox import InboxConnector, InboxRow


def _params(**overrides: Any) -> InboxParams:
    values: dict[str, Any] = {"kind": "inbox"}
    values.update(overrides)
    return InboxParams.model_validate(values)


async def _collect(root: Path, **overrides: Any) -> list[FetchedItem | Rejection]:
    connector = InboxConnector(root, _params(**overrides))
    return [item async for item in connector.fetch(None)]


def _items(results: list[FetchedItem | Rejection]) -> list[FetchedItem]:
    return [r for r in results if isinstance(r, FetchedItem)]


def _rejections(results: list[FetchedItem | Rejection]) -> list[Rejection]:
    return [r for r in results if isinstance(r, Rejection)]


async def test_markdown_file_is_one_item(tmp_path: Path) -> None:
    (tmp_path / "yc-rfs.md").write_text("# Request for Startups\n\nAI for payroll compliance.\n")
    (item,) = _items(await _collect(tmp_path))
    assert item.title == "Request for Startups"
    assert "payroll compliance" in item.text
    assert item.locator == "yc-rfs.md"
    assert item.external_id.startswith("sha256:")
    assert item.url is None and item.author is None


async def test_text_file_uses_file_name_as_title(tmp_path: Path) -> None:
    (tmp_path / "note.txt").write_text("customers hate our invoicing")
    (item,) = _items(await _collect(tmp_path))
    assert item.title == "note.txt"


async def test_csv_rows_are_validated(tmp_path: Path) -> None:
    (tmp_path / "pains.csv").write_text(
        "title,text,url,points\n"
        "Billing,Billing breaks weekly,https://example.com/a,3\n"
        ",,https://example.com/b,1\n"  # no text
        "Payroll,Payroll is slow,ftp://example.com/c,1\n"  # bad scheme
        "Ok,Fine row,,-4\n"  # negative points
    )
    results = await _collect(tmp_path)
    items = _items(results)
    assert [i.title for i in items] == ["Billing"]
    assert items[0].locator == "pains.csv:2"
    assert items[0].points == 3
    reasons = {r.locator: r.reason for r in _rejections(results)}
    assert set(reasons) == {"pains.csv:3", "pains.csv:4", "pains.csv:5"}


async def test_csv_unknown_column_rejects_rows(tmp_path: Path) -> None:
    (tmp_path / "x.csv").write_text("text,author\nhello,someone\n")
    results = await _collect(tmp_path)
    assert not _items(results)
    assert "author" in _rejections(results)[0].reason


async def test_json_list_and_items_object(tmp_path: Path) -> None:
    (tmp_path / "a.json").write_text(json.dumps([{"text": "one"}, {"text": "two", "x": 1}]))
    (tmp_path / "b.json").write_text(json.dumps({"items": [{"text": "three", "points": 2}]}))
    results = await _collect(tmp_path)
    assert sorted(i.text for i in _items(results)) == ["one", "three"]
    assert [r.locator for r in _rejections(results)] == ["a.json:2"]


async def test_deep_json_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "deep.json").write_text("[" * 5000 + "]" * 5000)
    results = await _collect(tmp_path)
    assert not _items(results)
    assert "deep" in _rejections(results)[0].reason or "nest" in _rejections(results)[0].reason


async def test_same_content_in_two_files_has_one_id(tmp_path: Path) -> None:
    """The id is the normalized text, so a pain saved twice is stored once."""
    (tmp_path / "a.txt").write_text("Same  pain\n")
    (tmp_path / "b.md").write_text("same pain")
    a, b = _items(await _collect(tmp_path))
    assert a.external_id == b.external_id


async def test_symlinks_and_escapes_are_never_followed(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("do not read")
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "link.txt").symlink_to(outside / "secret.txt")
    (inbox / "dirlink").symlink_to(outside, target_is_directory=True)
    results = await _collect(inbox)
    assert not _items(results)
    assert all("do not read" not in str(r) for r in results)
    assert {r.locator for r in _rejections(results)} == {"link.txt", "dirlink"}


async def test_hidden_files_and_dirs_are_skipped(tmp_path: Path) -> None:
    (tmp_path / ".DS_Store").write_text("x")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "a.txt").write_text("x")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "b.txt").write_text("x")
    assert await _collect(tmp_path) == []


async def test_depth_cap(tmp_path: Path) -> None:
    deep = tmp_path / "a" / "b" / "c"
    deep.mkdir(parents=True)
    (tmp_path / "a" / "b" / "ok.txt").write_text("depth two")
    (deep / "too-deep.txt").write_text("depth three")
    results = await _collect(tmp_path, max_depth=2)
    assert [i.text for i in _items(results)] == ["depth two"]


async def test_file_limits(tmp_path: Path) -> None:
    for n in range(5):
        (tmp_path / f"{n}.txt").write_text(f"note {n}")
    (tmp_path / "big.txt").write_text("x" * 2000)
    (tmp_path / "doc.pdf").write_bytes(b"%PDF")
    results = await _collect(tmp_path, max_files=3, max_file_bytes=1000)
    assert len(_items(results)) == 3
    reasons = " | ".join(r.reason for r in _rejections(results))
    assert "file limit" in reasons
    results = await _collect(tmp_path, max_files=100, max_file_bytes=1000)
    by_file = {r.locator: r.reason for r in _rejections(results)}
    assert "big.txt" in by_file and "doc.pdf" in by_file


async def test_row_limit(tmp_path: Path) -> None:
    rows = "\n".join(f"row {n}" for n in range(10))
    (tmp_path / "x.csv").write_text("text\n" + rows + "\n")
    results = await _collect(tmp_path, max_rows_per_file=4)
    assert len(_items(results)) == 4
    assert any("row limit" in r.reason for r in _rejections(results))


async def test_invalid_utf8_is_replaced_not_fatal(tmp_path: Path) -> None:
    (tmp_path / "bad.txt").write_bytes(b"caf\xe9 pain")
    (item,) = _items(await _collect(tmp_path))
    assert item.text == "caf� pain"


async def test_files_are_never_modified(tmp_path: Path) -> None:
    path = tmp_path / "x.csv"
    path.write_text("text\nhello\n")
    before = (path.read_bytes(), os.stat(path).st_mtime_ns)
    await _collect(tmp_path)
    assert (path.read_bytes(), os.stat(path).st_mtime_ns) == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["x.csv"]


def test_inbox_row_is_strict() -> None:
    with pytest.raises(ValueError):
        InboxRow.model_validate({"text": "x", "author": "y"})
    with pytest.raises(ValueError):
        InboxRow.model_validate({"text": ""})
    assert InboxRow.model_validate({"text": "ok", "points": "3"}).points == 3


# --- review hardening: unreadable or odd files are reported, never fatal ---------


async def test_missing_inbox_dir_is_reported(tmp_path: Path) -> None:
    (rejection,) = _rejections(await _collect(tmp_path / "missing"))
    assert rejection.locator == "inbox"
    assert "not found" in rejection.reason


async def test_unreadable_file_is_a_rejection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "a.txt").write_text("fine")
    (tmp_path / "b.txt").write_text("locked")
    real_open = os.open

    def deny(path: Any, flags: int, *args: Any) -> int:
        if str(path).endswith("b.txt"):
            raise PermissionError(13, "Permission denied")
        return real_open(path, flags, *args)

    monkeypatch.setattr(os, "open", deny)
    results = await _collect(tmp_path)
    assert [i.title for i in _items(results)] == ["a.txt"]
    (rejection,) = _rejections(results)
    assert rejection.locator == "b.txt"
    assert "unreadable" in rejection.reason


async def test_file_swapped_for_a_symlink_is_not_followed(tmp_path: Path) -> None:
    secret = tmp_path.parent / f"{tmp_path.name}-secret.txt"
    secret.write_text("outside")
    (tmp_path / "a.txt").symlink_to(secret)
    results = await _collect(tmp_path)
    assert _items(results) == []


async def test_file_that_grew_past_the_cap_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("x" * 50)
    results = await _collect(tmp_path, max_file_bytes=10)
    (rejection,) = _rejections(results)
    assert "over 10" in rejection.reason


async def test_replaced_bytes_and_truncation_are_flagged(tmp_path: Path) -> None:
    (tmp_path / "bad.txt").write_bytes(b"caf\xe9 pain")
    (tmp_path / "long.md").write_text("# Long\n" + "x" * (40 * 1024))
    by_title = {i.title: i for i in _items(await _collect(tmp_path))}
    assert "encoding_replaced" in by_title["bad.txt"].flags
    assert "truncated" in by_title["Long"].flags


def test_reader_refuses_a_symlink_even_if_the_walk_missed_it(tmp_path: Path) -> None:
    from jokr.connectors.inbox import _read_regular_file

    target = tmp_path / "real.txt"
    target.write_text("x")
    link = tmp_path / "link.txt"
    link.symlink_to(target)
    with pytest.raises(OSError):
        _read_regular_file(link, 100)


async def test_folders_past_max_depth_are_reported(tmp_path: Path) -> None:
    deep = tmp_path / "a" / "b"
    deep.mkdir(parents=True)
    (deep / "x.txt").write_text("hidden away")
    results = await _collect(tmp_path, max_depth=1)
    assert _items(results) == []
    (rejection,) = _rejections(results)
    assert rejection.locator == "a/b"
    assert "max_depth" in rejection.reason


async def test_hard_links_are_refused(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside.txt"
    outside.write_text("not for the inbox")
    os.link(outside, tmp_path / "a.txt")
    results = await _collect(tmp_path)
    assert _items(results) == []
    assert "unreadable" in _rejections(results)[0].reason
