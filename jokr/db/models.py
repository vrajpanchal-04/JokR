"""ORM models. Tables of §6 land per phase: decisions_log (P0), Scout's tables (P1)."""

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class DecisionLog(Base):
    """Every agent decision, with its reason and evidence (C10). Insert-only."""

    __tablename__ = "decisions_log"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    agent: Mapped[str] = mapped_column(Text)
    action: Mapped[str] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text)
    evidence_ids: Mapped[list[int]] = mapped_column(
        ARRAY(BigInteger), server_default="{}", default=list
    )
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


# Scout (P1, migration 0002). Check constraints and grants live in the migration;
# the models carry what Alembic compares (columns, keys, indexes) and what code reads.


class SourceRecord(Base):
    """Mirror of config/sources.yaml. Only `jokr sources sync` (the owner role) writes it."""

    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    name: Mapped[str] = mapped_column(Text, unique=True)
    type: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean)
    tos_url: Mapped[str | None] = mapped_column(Text)
    allowed_hosts: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default="{}")
    min_interval_s: Mapped[float | None] = mapped_column(Float)
    max_requests: Mapped[int | None] = mapped_column(Integer)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Run(Base):
    """One row per source per Scout invocation."""

    __tablename__ = "runs"
    __table_args__ = (
        UniqueConstraint("id", "source_id", name="runs_id_source"),
        Index("ix_runs_source_started", "source_id", text("started_at DESC")),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    agent: Mapped[str] = mapped_column(Text)
    source_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("sources.id", ondelete="RESTRICT")
    )
    status: Mapped[str] = mapped_column(Text, server_default="running")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    n_fetched: Mapped[int] = mapped_column(Integer, server_default="0")
    n_new: Mapped[int] = mapped_column(Integer, server_default="0")
    n_skipped: Mapped[int] = mapped_column(Integer, server_default="0")
    tokens: Mapped[int] = mapped_column(Integer, server_default="0")
    cost: Mapped[Decimal] = mapped_column(Numeric(12, 6), server_default="0")
    error: Mapped[str | None] = mapped_column(Text)
    rejections: Mapped[list[dict[str, str]]] = mapped_column(JSONB, server_default="[]")


class Signal(Base):
    """One fetched item. Insert-only for every app role; `trust` is always 'untrusted'."""

    __tablename__ = "signals"
    __table_args__ = (
        ForeignKeyConstraint(
            ["run_id", "source_id"],
            ["runs.id", "runs.source_id"],
            name="signals_run_same_source",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("source_id", "external_id", name="signals_source_external_id"),
        Index(
            "ix_signals_rank",
            text("intent_score DESC"),
            text("points DESC NULLS LAST"),
            text("num_comments DESC NULLS LAST"),
            "id",
        ),
        Index(
            "ix_signals_content_hash",
            "content_hash",
            postgresql_include=["source_id", "posted_at", "fetched_at"],
        ),
        Index(
            "ix_signals_url_canonical",
            "url_canonical",
            postgresql_include=["source_id", "posted_at", "fetched_at"],
            postgresql_where=text("url_canonical IS NOT NULL"),
        ),
        Index("ix_signals_run_id", "run_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    source_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("sources.id", ondelete="RESTRICT")
    )
    run_id: Mapped[int] = mapped_column(BigInteger)
    external_id: Mapped[str] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(Text)
    url_canonical: Mapped[str | None] = mapped_column(Text)
    locator: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    text: Mapped[str] = mapped_column(Text)
    author_hash: Mapped[str | None] = mapped_column(Text)
    points: Mapped[int | None] = mapped_column(Integer)
    num_comments: Mapped[int | None] = mapped_column(Integer)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    content_hash: Mapped[str] = mapped_column(Text)
    trust: Mapped[str] = mapped_column(Text, server_default="untrusted")
    flags: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default="{}")
    raw: Mapped[dict[str, Any]] = mapped_column(JSONB)
    intent_score: Mapped[Decimal] = mapped_column(Numeric(6, 3))
    intent_terms: Mapped[list[str]] = mapped_column(ARRAY(Text))
    intent_version: Mapped[str] = mapped_column(Text)
