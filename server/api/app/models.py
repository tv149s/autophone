"""多租户数据模型 —— 每张业务表都带 user_id 做行级隔离。"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean, DateTime, ForeignKey, Integer, String, Text, JSON, func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def _uuid() -> str:
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)   # Google 用户无密码
    provider: Mapped[str] = mapped_column(String(32), default="password")           # password | google
    google_sub: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    totp_secret: Mapped[str | None] = mapped_column(String(64), nullable=True)
    totp_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    plan: Mapped[str] = mapped_column(String(32), default="free")
    default_greeting: Mapped[str] = mapped_column(Text, default="")   # 默认接听问候语(陌生来电)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Device(Base):
    __tablename__ = "devices"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(128), default="")
    kind: Mapped[str] = mapped_column(String(32), default="mac")   # mac|android|ios|linux
    hostname: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)  # 客户端自助注册幂等键
    token: Mapped[str] = mapped_column(String(64), unique=True, index=True, default=_uuid)
    online: Mapped[bool] = mapped_column(Boolean, default=False)
    last_seen: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    phone_registry: Mapped[list] = mapped_column(JSON, default=list)   # 该边缘端可切换的电话清单
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Phone(Base):
    __tablename__ = "phones"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    device_id: Mapped[str] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), index=True)  # 控制它的边缘端
    pkey: Mapped[str] = mapped_column(String(64), default="")      # 边缘端给这台电话的稳定标识
    label: Mapped[str] = mapped_column(String(128), default="")
    platform: Mapped[str] = mapped_column(String(16), default="ios")   # ios | android
    udid: Mapped[str] = mapped_column(String(128), default="")
    number: Mapped[str] = mapped_column(String(32), default="")
    wda_ready: Mapped[bool] = mapped_column(Boolean, default=False)
    locked: Mapped[bool] = mapped_column(Boolean, default=False)
    front_app: Mapped[str] = mapped_column(String(64), default="")   # 当前前台应用(动态)
    auto_answer: Mapped[bool] = mapped_column(Boolean, default=True)
    channels: Mapped[list] = mapped_column(JSON, default=list)     # [{kind,name,status,enabled,answer,outbound}]
    overrides: Mapped[dict] = mapped_column(JSON, default=dict)    # 用户覆盖 {kind:{enabled,asr,reply_language}},不被边缘上报冲掉
    last_report: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Channel(Base):
    __tablename__ = "channels"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(32))                  # web|telegram|imessage
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Soul(Base):
    __tablename__ = "souls"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(128))
    body: Mapped[str] = mapped_column(Text, default="")
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Voice(Base):
    __tablename__ = "voices"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    label: Mapped[str] = mapped_column(String(128), default="")
    sample_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="pending")   # pending|ready|failed
    consent: Mapped[bool] = mapped_column(Boolean, default=False)        # 本人声音同意声明
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Task(Base):
    __tablename__ = "tasks"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    device_id: Mapped[str | None] = mapped_column(ForeignKey("devices.id", ondelete="SET NULL"), nullable=True)
    source: Mapped[str] = mapped_column(String(32), default="web")       # web|telegram|imessage
    direction: Mapped[str] = mapped_column(String(16), default="outbound")
    card: Mapped[dict] = mapped_column(JSON, default=dict)               # 任务卡
    status: Mapped[str] = mapped_column(String(32), default="pending")   # pending|running|done|failed|canceled
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    recording_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class Contact(Base):
    __tablename__ = "contacts"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(128), default="")
    phone: Mapped[str] = mapped_column(String(32), index=True, default="")      # E.164 归一化(App联系人可空)
    phone_raw: Mapped[str] = mapped_column(String(64), default="")
    channel: Mapped[str] = mapped_column(String(16), default="", index=True)    # ""=电话 / wechat / telegram / qq …
    title: Mapped[str] = mapped_column(String(16), default="")                  # 先生/女士/Dr…
    relationship: Mapped[str] = mapped_column(String(32), default="")           # friend/colleague/customer/family/vendor/org/other
    birthday: Mapped[str | None] = mapped_column(String(16), nullable=True)     # YYYY-MM-DD 或 MM-DD
    age_range: Mapped[str] = mapped_column(String(32), default="")
    preferences: Mapped[str] = mapped_column(Text, default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    tags: Mapped[list] = mapped_column(JSON, default=list)
    org: Mapped[str] = mapped_column(String(128), default="")
    soul_id: Mapped[str | None] = mapped_column(String(32), nullable=True)      # 该联系人指定的身份(可选)
    source: Mapped[str] = mapped_column(String(32), default="manual")           # manual/vcard/csv/google/android/call
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class ContactMemory(Base):
    __tablename__ = "contact_memories"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    contact_id: Mapped[str] = mapped_column(ForeignKey("contacts.id", ondelete="CASCADE"), index=True)
    call_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    summary: Mapped[str] = mapped_column(Text, default="")        # 本次通话提炼的要点
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class CallLog(Base):
    __tablename__ = "call_logs"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    device_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    direction: Mapped[str] = mapped_column(String(16), default="inbound")
    number: Mapped[str] = mapped_column(String(64), default="")
    transcript: Mapped[str] = mapped_column(Text, default="")
    category: Mapped[str] = mapped_column(String(32), default="")
    recording_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    action: Mapped[str] = mapped_column(String(128))
    ip: Mapped[str] = mapped_column(String(64), default="")
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
