from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Integer, JSON, LargeBinary, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


ID_TYPE = BigInteger().with_variant(Integer, "sqlite")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Task(Base):
    __tablename__ = "tasks_v2"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_key: Mapped[str] = mapped_column(String(64), index=True)
    owner_mask: Mapped[str] = mapped_column(String(128))
    owner_value: Mapped[str] = mapped_column(String(160), default="")
    user_id: Mapped[int | None] = mapped_column(ID_TYPE, nullable=True, index=True)
    mode: Mapped[str] = mapped_column(String(32), index=True)
    mode_name: Mapped[str] = mapped_column(String(64), default="")
    status: Mapped[str] = mapped_column(String(32), index=True, default="queued")
    request_id: Mapped[str] = mapped_column(String(128), unique=True)
    concurrency: Mapped[int] = mapped_column(Integer, default=1)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    version: Mapped[int] = mapped_column(BigInteger, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    items: Mapped[list[TaskItem]] = relationship(back_populates="task", cascade="all, delete-orphan", lazy="selectin")


class QuotaCard(Base):
    __tablename__ = "quota_cards_v2"
    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    code_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    code_value: Mapped[str] = mapped_column(String(160), default="")
    code_mask: Mapped[str] = mapped_column(String(128))
    owner_user_id: Mapped[int | None] = mapped_column(ID_TYPE, nullable=True, index=True)
    bound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    quota: Mapped[int] = mapped_column(Integer)
    used_quota: Mapped[int] = mapped_column(Integer, default=0)
    reserved_quota: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(32), default="active", index=True)
    fusion_disabled: Mapped[bool] = mapped_column(default=False)
    remark: Mapped[str] = mapped_column(String(160), default="")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    version: Mapped[int] = mapped_column(BigInteger, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class CardFusion(Base):
    __tablename__ = "card_fusions_v2"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    new_card_id: Mapped[int] = mapped_column(ID_TYPE, index=True)
    new_card_mask: Mapped[str] = mapped_column(String(128))
    new_card_value: Mapped[str] = mapped_column(String(160), default="")
    source_card_ids: Mapped[list[int]] = mapped_column(JSON, default=list)
    source_masks: Mapped[list[str]] = mapped_column(JSON, default=list)
    source_values: Mapped[list[str]] = mapped_column(JSON, default=list)
    total_remaining: Mapped[int] = mapped_column(Integer)
    remark: Mapped[str] = mapped_column(String(160), default="CDK 融合")
    actor: Mapped[str] = mapped_column(String(32), default="user")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class TaskItem(Base):
    __tablename__ = "task_items_v2"
    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks_v2.id", ondelete="CASCADE"), index=True)
    item_index: Mapped[int] = mapped_column(Integer)
    email: Mapped[str] = mapped_column(String(320), default="")
    at_mask: Mapped[str] = mapped_column(String(256), default="")
    credential: Mapped[bytes] = mapped_column(LargeBinary)
    status: Mapped[str] = mapped_column(String(32), index=True, default="queued")
    stage: Mapped[str] = mapped_column(String(64), default="")
    stage_detail: Mapped[str] = mapped_column(String(512), default="")
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    link: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    version: Mapped[int] = mapped_column(BigInteger, default=0)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    task: Mapped[Task] = relationship(back_populates="items")
    __table_args__ = (UniqueConstraint("task_id", "item_index", name="uq_task_item_index"),)


class TaskEvent(Base):
    __tablename__ = "task_events_v2"
    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(64), index=True)
    item_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    event_type: Mapped[str] = mapped_column(String(96), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class OutboxEvent(Base):
    __tablename__ = "outbox_events"
    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    topic: Mapped[str] = mapped_column(String(128))
    message_key: Mapped[str] = mapped_column(String(128))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class QuotaLedger(Base):
    __tablename__ = "quota_ledger_v2"
    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    request_id: Mapped[str] = mapped_column(String(128))
    owner_key: Mapped[str] = mapped_column(String(64), index=True)
    task_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    item_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    delta: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (UniqueConstraint("request_id", "kind", name="uq_quota_request_kind"),)


Index("ix_task_owner_created", Task.owner_key, Task.created_at)


class RuntimeConfig(Base):
    __tablename__ = "runtime_configs_v2"
    key: Mapped[str] = mapped_column(String(96), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class AdminAnnouncement(Base):
    __tablename__ = "admin_announcements_v2"
    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(160), default="系统公告")
    content: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(24), default="published", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class AuditLog(Base):
    __tablename__ = "audit_logs_v2"
    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    level: Mapped[str] = mapped_column(String(16), default="info", index=True)
    action: Mapped[str] = mapped_column(String(96), index=True)
    message: Mapped[str] = mapped_column(String(512))
    task_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    mode: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class User(Base):
    __tablename__ = "users_v2"
    id: Mapped[int] = mapped_column(ID_TYPE, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(256))
    display_name: Mapped[str] = mapped_column(String(120), default="")
    role: Mapped[str] = mapped_column(String(24), default="user", index=True)
    status: Mapped[str] = mapped_column(String(24), default="active", index=True)
    token_version: Mapped[int] = mapped_column(Integer, default=0)
    task_concurrency: Mapped[int] = mapped_column(Integer, default=3)
    max_accounts_per_task: Mapped[int] = mapped_column(Integer, default=100)
    allow_fusion: Mapped[bool] = mapped_column(default=True)
    notes: Mapped[str] = mapped_column(String(500), default="")
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
