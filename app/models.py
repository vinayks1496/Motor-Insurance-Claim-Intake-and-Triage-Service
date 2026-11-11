from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class ClaimRun(Base):
    __tablename__ = "claim_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    correlation_id: Mapped[str] = mapped_column(String(36), index=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), unique=True, nullable=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    fnol: Mapped[dict] = mapped_column(JSON)
    intake: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RunEvent(Base):
    __tablename__ = "run_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("claim_runs.id"), index=True)
    correlation_id: Mapped[str] = mapped_column(String(36), index=True)
    agent: Mapped[str] = mapped_column(String(48), index=True)
    event_type: Mapped[str] = mapped_column(String(48), index=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    prompt_version: Mapped[str | None] = mapped_column(String(48), nullable=True)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    latency_ms: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, index=True)


class QuarantineRecord(Base):
    __tablename__ = "quarantine_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    correlation_id: Mapped[str] = mapped_column(String(36), index=True)
    field_errors: Mapped[list] = mapped_column(JSON)
    payload_excerpt: Mapped[dict | list | str | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, index=True)
    agent_run_started: Mapped[bool] = mapped_column(Boolean, default=False)


class FeedbackRecord(Base):
    __tablename__ = "feedback_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("claim_runs.id"), index=True)
    satisfactory: Mapped[bool] = mapped_column(Boolean)
    reason: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, index=True)


class QualitySample(Base):
    __tablename__ = "quality_samples"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("claim_runs.id"), index=True)
    groundedness: Mapped[float] = mapped_column(Float)
    context_relevance: Mapped[float] = mapped_column(Float)
    faithfulness: Mapped[float] = mapped_column(Float)
    hallucination_rate: Mapped[float] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, index=True)
