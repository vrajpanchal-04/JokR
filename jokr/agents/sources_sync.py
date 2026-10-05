"""Mirror config/sources.yaml into the `sources` table (run by the owner role only).

The YAML is the authority (C3); the table exists so signals and runs can point
at a source with a foreign key. A source removed from the YAML is retired,
never deleted, because its signals still reference it.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from jokr.config import Source
from jokr.db.models import SourceRecord


@dataclass(frozen=True)
class SyncResult:
    upserted: tuple[str, ...]
    retired: tuple[str, ...]


async def sync_sources(engine: AsyncEngine, sources: Sequence[Source]) -> SyncResult:
    async with engine.begin() as conn:
        for s in sources:
            values = {
                "name": s.name,
                "type": s.type,
                "enabled": s.enabled,
                "tos_url": str(s.tos_url) if s.tos_url else None,
                "allowed_hosts": list(s.allowed_hosts),
                "min_interval_s": s.min_interval_s,
                "max_requests": s.max_requests,
            }
            stmt = pg_insert(SourceRecord).values(**values)
            await conn.execute(
                stmt.on_conflict_do_update(
                    index_elements=[SourceRecord.name],
                    set_={**values, "synced_at": func.now(), "retired_at": None},
                )
            )
        names = [s.name for s in sources]
        retired = (
            (
                await conn.execute(
                    update(SourceRecord)
                    .where(SourceRecord.name.not_in(names), SourceRecord.retired_at.is_(None))
                    .values(retired_at=func.now(), enabled=False)
                    .returning(SourceRecord.name)
                )
            )
            .scalars()
            .all()
        )
        # Read back inside the transaction so the result reflects what was written.
        current = (await conn.execute(select(SourceRecord.name))).scalars().all()
    return SyncResult(tuple(sorted(set(names) & set(current))), tuple(sorted(retired)))
