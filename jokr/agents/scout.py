"""Scout: run every enabled source once, store new signals, and log what happened.

Each source runs in its own try with its own runs row, advisory lock and time
limit, so one broken or slow source never stops the others (B5). Every run and
every C3 violation lands in decisions_log (C10). Counts in runs come only from
what this code actually stored (C5).
"""

import asyncio
import logging
import traceback
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal

from sqlalchemy import func, insert, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import DataError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from jokr.agents.scout_ingest import IngestRejected, SignalRow, prepare, scrub_identity
from jokr.config import IntentLexicon, Source
from jokr.connectors.base import Connector, FetchedItem, Rejection
from jokr.db.models import DecisionLog, Run, Signal, SourceRecord
from jokr.guards.ratelimit import BudgetExhausted
from jokr.guards.redact import redact
from jokr.guards.tos import HostNotAllowed

log = logging.getLogger(__name__)

AGENT = "scout"
BATCH_SIZE = 100
MAX_REJECTIONS = 100  # runs_rejections_shape caps the stored list
MAX_ERROR_CHARS = 4000

RunStatus = Literal["ok", "partial", "failed"]
# Opens the connector for a source (and its HTTP client), closing both afterwards.
OpenConnector = Callable[[Source], AbstractAsyncContextManager[Connector]]


@dataclass
class SourceOutcome:
    name: str
    status: RunStatus
    run_id: int | None = None
    n_fetched: int = 0
    n_new: int = 0
    n_skipped: int = 0
    error: str | None = None
    c3_violation: bool = False
    rejections: list[dict[str, str]] = field(default_factory=list)
    unstored: str | None = None  # set when the final write failed during another error
    incomplete: str | None = None  # first reason part of the window went unread

    def reject(self, locator: str, reason: str) -> None:
        self.n_skipped += 1
        # One slot is kept for the "N more" marker, so a cut list says it was cut.
        if len(self.rejections) < MAX_REJECTIONS - 1:
            # Locators can be URLs or file names that carry someone's handle.
            locator = scrub_identity(locator).text
            self.rejections.append({"locator": locator[:300], "reason": reason[:300]})

    def stored_rejections(self) -> list[dict[str, str]]:
        extra = self.n_skipped - len(self.rejections)
        if extra <= 0:
            return self.rejections
        return [
            *self.rejections,
            {"locator": "scout", "reason": f"{extra} more rejections not listed"},
        ]


@dataclass(frozen=True)
class ScoutResult:
    status: RunStatus
    exit_code: int
    outcomes: tuple[SourceOutcome, ...]


@dataclass(frozen=True)
class ScoutDeps:
    salt: bytes
    lexicon: IntentLexicon
    open_connector: OpenConnector


def _error_text(exc: BaseException) -> str:
    return redact(f"{type(exc).__name__}: {exc}")[:MAX_ERROR_CHARS]


def _where(exc: BaseException) -> str:
    """File and line the error came from. No traceback text: frames can hold secrets."""
    frames = traceback.extract_tb(exc.__traceback__)
    return f" (at {Path(frames[-1].filename).name}:{frames[-1].lineno})" if frames else ""


async def _source_id(conn: AsyncConnection, name: str) -> int | None:
    row = (
        await conn.execute(
            select(SourceRecord.id).where(
                SourceRecord.name == name, SourceRecord.retired_at.is_(None)
            )
        )
    ).first()
    return int(row[0]) if row else None


async def _watermark(conn: AsyncConnection, source_id: int) -> datetime | None:
    """When the last fully successful run started.

    Not the newest stored post: a partial or failed run stores its newest items
    first, so their dates would skip the older ones it never reached, and one
    future-dated post would hide everything before it.
    """
    return await conn.scalar(
        select(func.max(Run.started_at)).where(
            Run.source_id == source_id, Run.agent == AGENT, Run.status == "ok"
        )
    )


def _values(source_id: int, run_id: int, r: SignalRow) -> dict[str, object]:
    return {
        "source_id": source_id,
        "run_id": run_id,
        "external_id": r.external_id,
        "url": r.url,
        "url_canonical": r.url_canonical,
        "locator": r.locator,
        "title": r.title,
        "text": r.text,
        "author_hash": r.author_hash,
        "points": r.points,
        "num_comments": r.num_comments,
        "posted_at": r.posted_at,
        "content_hash": r.content_hash,
        "flags": list(r.flags),
        "raw": r.raw,
        "intent_score": r.intent_score,
        "intent_terms": list(r.intent_terms),
        "intent_version": r.intent_version,
    }


async def _insert(
    engine: AsyncEngine, source_id: int, run_id: int, rows: Sequence[SignalRow]
) -> int:
    stmt = (
        pg_insert(Signal)
        .on_conflict_do_nothing(constraint="signals_source_external_id")
        .returning(Signal.id)
    )
    values = [_values(source_id, run_id, r) for r in rows]
    async with engine.begin() as conn:
        # Only rows actually inserted come back, so n_new is a count of real rows.
        return len((await conn.execute(stmt, values)).all())


async def _store(
    engine: AsyncEngine,
    source_id: int,
    run_id: int,
    rows: Sequence[SignalRow],
    outcome: SourceOutcome,
) -> int:
    """Insert a batch. If Postgres refuses a value, retry row by row so only that row is lost."""
    if not rows:
        return 0
    try:
        return await _insert(engine, source_id, run_id, rows)
    except (DataError, IntegrityError):
        if len(rows) == 1:
            raise
    inserted = 0
    for row in rows:
        try:
            inserted += await _insert(engine, source_id, run_id, [row])
        except (DataError, IntegrityError) as exc:
            # The SQLSTATE only: the driver's message can quote the row's values.
            code = getattr(exc.orig, "sqlstate", None) or type(exc.orig).__name__
            outcome.reject(row.locator or row.url or row.external_id, f"database refused: {code}")
    return inserted


async def _save(
    engine: AsyncEngine,
    source_id: int,
    run_id: int,
    rows: Sequence[SignalRow],
    outcome: SourceOutcome,
) -> None:
    """Store and count a batch. Shielded, so a timeout can't lose a committed write's count."""
    write = asyncio.ensure_future(_store(engine, source_id, run_id, rows, outcome))
    try:
        outcome.n_new += await asyncio.shield(write)
    except asyncio.CancelledError:
        # The time limit fired mid-write. The write goes on, so wait for it and
        # count its rows before letting the cancel through.
        outcome.n_new += await write
        raise


async def _collect(
    engine: AsyncEngine,
    source: Source,
    source_id: int,
    run_id: int,
    outcome: SourceOutcome,
    deps: ScoutDeps,
    items: AsyncIterator[FetchedItem | Rejection],
) -> None:
    pending: list[SignalRow] = []
    seen: set[str] = set()
    failure: BaseException | None = None
    try:
        async for item in items:
            if isinstance(item, Rejection):
                outcome.reject(item.locator, item.reason)
                if item.incomplete and outcome.incomplete is None:
                    outcome.incomplete = item.reason
                continue
            if item.external_id in seen:
                continue  # same item twice in one run; counted once
            seen.add(item.external_id)
            outcome.n_fetched += 1
            locator = item.locator or item.url or item.external_id or "?"
            try:
                row = prepare(item, source=source.name, salt=deps.salt, lexicon=deps.lexicon)
            except IngestRejected as exc:
                outcome.reject(locator, str(exc))
                continue
            except Exception as exc:  # one odd item must not fail the source
                outcome.reject(locator, f"could not be prepared: {type(exc).__name__}")
                continue
            pending.append(row)
            if len(pending) >= BATCH_SIZE:
                batch, pending = pending, []
                await _save(engine, source_id, run_id, batch, outcome)
    except BaseException as exc:
        failure = exc
        raise
    finally:
        # Keep what was fetched even when the source fails, times out or hits its cap.
        if pending:
            try:
                await _save(engine, source_id, run_id, pending, outcome)
            except Exception as exc:
                if failure is None:
                    raise
                # Don't let the write error replace the error that stopped the source.
                outcome.unstored = (
                    f"{len(pending)} fetched item(s) could not be stored: {_error_text(exc)}"
                )


async def _finish(engine: AsyncEngine, outcome: SourceOutcome) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            update(Run)
            .where(Run.id == outcome.run_id)
            .values(
                status=outcome.status,
                finished_at=func.now(),
                n_fetched=outcome.n_fetched,
                n_new=outcome.n_new,
                n_skipped=outcome.n_skipped,
                error=outcome.error,
                rejections=outcome.stored_rejections(),
            )
        )


async def _log(engine: AsyncEngine, action: str, reason: str, evidence: list[int]) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            insert(DecisionLog).values(
                agent=AGENT, action=action, reason=reason[:MAX_ERROR_CHARS], evidence_ids=evidence
            )
        )


async def _reap_abandoned(conn: AsyncConnection, source_id: int) -> None:
    """Close runs a killed process left at 'running'. Safe: we hold this source's lock."""
    await conn.execute(
        update(Run)
        .where(Run.source_id == source_id, Run.agent == AGENT, Run.status == "running")
        .values(status="failed", finished_at=func.now(), error="abandoned: the process stopped")
    )


async def _run_source(engine: AsyncEngine, source: Source, deps: ScoutDeps) -> SourceOutcome:
    try:
        return await _run_locked(engine, source, deps)
    except Exception as exc:  # setup or bookkeeping failed; the other sources still run (B5)
        error = _error_text(exc) + _where(exc)
        log.error("source %s could not run: %s", source.name, error)
        return SourceOutcome(source.name, "failed", error=error)


async def _run_locked(engine: AsyncEngine, source: Source, deps: ScoutDeps) -> SourceOutcome:
    key = {"key": f"jokr.scout.{source.name}"}
    async with engine.connect() as lock_conn:
        # Session-level lock on this connection. The pool keeps connections open,
        # so it is released explicitly below, never left to the connection closing.
        if not await lock_conn.scalar(text("SELECT pg_try_advisory_lock(hashtext(:key))"), key):
            return SourceOutcome(source.name, "failed", error="another Scout run holds this source")
        try:
            return await _run_held(engine, lock_conn, source, deps)
        finally:
            try:
                await lock_conn.rollback()
                await lock_conn.execute(text("SELECT pg_advisory_unlock(hashtext(:key))"), key)
                await lock_conn.commit()
            except Exception:
                # Discarding the connection ends its session, which drops the lock.
                await lock_conn.invalidate()


async def _run_held(
    engine: AsyncEngine, lock_conn: AsyncConnection, source: Source, deps: ScoutDeps
) -> SourceOutcome:
    outcome = SourceOutcome(source.name, "ok")
    source_id = await _source_id(lock_conn, source.name)
    if source_id is None:
        return SourceOutcome(
            source.name, "failed", error="not in the sources table; run `jokr sources sync`"
        )
    await _reap_abandoned(lock_conn, source_id)
    since = await _watermark(lock_conn, source_id)
    await lock_conn.commit()
    async with engine.begin() as conn:
        run_id = int(
            (
                await conn.execute(
                    insert(Run).values(agent=AGENT, source_id=source_id).returning(Run.id)
                )
            ).scalar_one()
        )
    outcome.run_id = run_id
    await _fetch_into(engine, source, source_id, run_id, outcome, deps, since)
    await _finish(engine, outcome)
    return outcome


async def _fetch_into(
    engine: AsyncEngine,
    source: Source,
    source_id: int,
    run_id: int,
    outcome: SourceOutcome,
    deps: ScoutDeps,
    since: datetime | None,
) -> None:
    limit = asyncio.timeout(source.max_runtime_s)
    try:
        async with limit, deps.open_connector(source) as connector:
            items = connector.fetch(since)
            try:
                await _collect(engine, source, source_id, run_id, outcome, deps, items)
            finally:
                # Close the generator now, while its client is still open, rather
                # than whenever the garbage collector gets to it.
                aclose = getattr(items, "aclose", None)
                if aclose is not None:
                    await aclose()
    except BudgetExhausted as exc:
        # The cap did its job: what was fetched is stored, the rest waits for next run.
        outcome.status, outcome.error = "partial", _error_text(exc)
    except HostNotAllowed as exc:
        outcome.status, outcome.error, outcome.c3_violation = "failed", _error_text(exc), True
    except Exception as exc:  # one source must never take the others down (B5)
        if isinstance(exc, TimeoutError) and limit.expired():
            outcome.status = "failed"
            outcome.error = f"time limit of {source.max_runtime_s}s reached"
        else:
            outcome.status, outcome.error = "failed", _error_text(exc) + _where(exc)
            # No traceback: exception text from a source can carry secrets. The
            # redacted message and its file:line are logged here and stored on the run.
            log.error("source %s failed: %s", source.name, outcome.error)
    if outcome.status == "ok" and outcome.incomplete:
        outcome.status, outcome.error = "partial", f"not fully read: {outcome.incomplete}"
    if outcome.unstored:
        outcome.error = f"{outcome.error}; {outcome.unstored}"[:MAX_ERROR_CHARS]


def _overall(outcomes: Sequence[SourceOutcome]) -> tuple[RunStatus, int]:
    if any(o.c3_violation for o in outcomes):
        return "failed", 2
    if not outcomes or all(o.status == "failed" for o in outcomes):
        return "failed", 1
    if all(o.status == "ok" for o in outcomes):
        return "ok", 0
    return "partial", 0


def _summary(outcomes: Sequence[SourceOutcome]) -> str:
    if not outcomes:
        return "no enabled sources"
    parts = []
    for o in outcomes:
        part = f"{o.name} {o.status}: {o.n_new} new / {o.n_fetched} fetched / {o.n_skipped} skipped"
        parts.append(f"{part} ({o.error})" if o.error else part)
    return "; ".join(parts)


async def run_scout(
    engine: AsyncEngine, sources: Sequence[Source], deps: ScoutDeps, *, only: str | None = None
) -> ScoutResult:
    """Run each enabled source (or just `only`) and record the outcome."""
    selected = [s for s in sources if s.enabled and (only is None or s.name == only)]
    outcomes: list[SourceOutcome] = []
    for source in selected:
        outcome = await _run_source(engine, source, deps)
        outcomes.append(outcome)
        if outcome.c3_violation:
            await _log(
                engine,
                "c3_violation",
                f"{source.name}: {outcome.error}",
                [outcome.run_id] if outcome.run_id else [],
            )
    status, exit_code = _overall(outcomes)
    run_ids = [o.run_id for o in outcomes if o.run_id is not None]
    await _log(engine, f"scout_run_{status}", _summary(outcomes), run_ids)
    return ScoutResult(status, exit_code, tuple(outcomes))
