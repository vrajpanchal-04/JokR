"""Scout: run every enabled source once, store new signals, and log what happened.

Each source runs in its own try with its own runs row, advisory lock and time
limit, so one broken or slow source never stops the others (B5). Every run and
every C3 violation lands in decisions_log (C10). Counts in runs come only from
what this code actually stored (C5).
"""

import asyncio
import logging
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from sqlalchemy import func, insert, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from jokr.agents.scout_ingest import IngestRejected, SignalRow, prepare
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

    def reject(self, locator: str, reason: str) -> None:
        self.n_skipped += 1
        if len(self.rejections) < MAX_REJECTIONS:
            self.rejections.append({"locator": locator[:300], "reason": reason[:300]})


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
    # Same shape as ix_signals_source_posted, so it is an index lookup.
    row = (
        await conn.execute(
            select(Signal.posted_at)
            .where(Signal.source_id == source_id)
            .order_by(Signal.posted_at.desc().nulls_last())
            .limit(1)
        )
    ).first()
    return row[0] if row else None


async def _store(
    engine: AsyncEngine, source_id: int, run_id: int, rows: Sequence[SignalRow]
) -> int:
    if not rows:
        return 0
    values = [
        {
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
        for r in rows
    ]
    stmt = (
        pg_insert(Signal)
        .on_conflict_do_nothing(constraint="signals_source_external_id")
        .returning(Signal.id)
    )
    async with engine.begin() as conn:
        # Only rows actually inserted come back, so n_new is a count of real rows.
        return len((await conn.execute(stmt, values)).all())


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
    try:
        async for item in items:
            if isinstance(item, Rejection):
                outcome.reject(item.locator, item.reason)
                continue
            outcome.n_fetched += 1
            if item.external_id in seen:
                continue  # same item twice in one run; the DB would drop it anyway
            seen.add(item.external_id)
            try:
                row = prepare(item, source=source.name, salt=deps.salt, lexicon=deps.lexicon)
            except IngestRejected as exc:
                outcome.reject(item.locator or item.url or item.external_id or "?", str(exc))
                continue
            pending.append(row)
            if len(pending) >= BATCH_SIZE:
                outcome.n_new += await _store(engine, source_id, run_id, pending)
                pending.clear()
    finally:
        # Keep what was fetched even when the source fails, times out or hits its cap.
        # Shielded so a timeout's cancellation can't abort the final write.
        if pending:
            outcome.n_new += await asyncio.shield(_store(engine, source_id, run_id, pending))


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
                rejections=outcome.rejections,
            )
        )


async def _log(engine: AsyncEngine, action: str, reason: str, evidence: list[int]) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            insert(DecisionLog).values(
                agent=AGENT, action=action, reason=reason[:MAX_ERROR_CHARS], evidence_ids=evidence
            )
        )


async def _run_source(engine: AsyncEngine, source: Source, deps: ScoutDeps) -> SourceOutcome:
    outcome = SourceOutcome(source.name, "ok")
    async with engine.connect() as lock_conn:
        # Session-level lock, held on this connection until it closes.
        locked = await lock_conn.scalar(
            text("SELECT pg_try_advisory_lock(hashtext(:key))"),
            {"key": f"jokr.scout.{source.name}"},
        )
        if not locked:
            return SourceOutcome(source.name, "failed", error="another Scout run holds this source")
        source_id = await _source_id(lock_conn, source.name)
        if source_id is None:
            return SourceOutcome(
                source.name, "failed", error="not in the sources table; run `jokr sources sync`"
            )
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
        try:
            async with asyncio.timeout(source.max_runtime_s):
                async with deps.open_connector(source) as connector:
                    items = connector.fetch(since)
                    await _collect(engine, source, source_id, run_id, outcome, deps, items)
        except BudgetExhausted as exc:
            # The cap did its job: what was fetched is stored, the rest waits for next run.
            outcome.status, outcome.error = "partial", _error_text(exc)
        except TimeoutError:
            outcome.status = "failed"
            outcome.error = f"time limit of {source.max_runtime_s}s reached"
        except HostNotAllowed as exc:
            outcome.status, outcome.error, outcome.c3_violation = "failed", _error_text(exc), True
        except Exception as exc:  # one source must never take the others down (B5)
            outcome.status, outcome.error = "failed", _error_text(exc)
            # No traceback: exception text from a source can carry secrets. The
            # redacted message is logged here and stored on the run.
            log.error("source %s failed: %s", source.name, outcome.error)
        await _finish(engine, outcome)
    return outcome


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
