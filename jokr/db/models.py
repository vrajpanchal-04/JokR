"""ORM models. P0 has only the append-only decisions log; the rest of §6 lands per phase."""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Identity, Text, func
from sqlalchemy.dialects.postgresql import ARRAY
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
