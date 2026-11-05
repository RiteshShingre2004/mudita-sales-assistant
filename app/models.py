"""Database tables.

Design rules encoded here:
- A Contact is unique per (channel, external_id). Same person on two channels
  = two contacts. We never merge across channels without verified identity.
- A Message is unique per (channel, channel_message_id), so a re-delivered
  webhook or a re-polled email can never be stored twice.
- A Booking is unique per idempotency_key, so a repeated confirmation can
  never create a second booking row (and the same key is used as the
  calendar event id in Step 4).
"""
from datetime import datetime, timezone
from enum import StrEnum

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Channel(StrEnum):
    EMAIL = "email"
    WHATSAPP = "whatsapp"
    INSTAGRAM = "instagram"


class LeadStatus(StrEnum):
    NEW = "new"
    NEEDS_INFO = "needs_info"
    QUALIFIED = "qualified"
    NOT_QUALIFIED = "not_qualified"


class BookingStatus(StrEnum):
    PENDING = "pending"          # create request about to be sent
    CONFIRMED = "confirmed"      # calendar returned the event
    UNCERTAIN = "uncertain"      # request sent, outcome unknown -> reconcile, never blind-retry
    CONFLICT = "conflict"        # slot became busy at recheck
    FAILED = "failed"            # definite failure, nothing created


class Contact(Base):
    __tablename__ = "contacts"
    __table_args__ = (UniqueConstraint("channel", "external_id", name="uq_contact_channel_ext"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    channel: Mapped[str] = mapped_column(String(20))
    external_id: Mapped[str] = mapped_column(String(255))  # email address / WA phone / IG scoped id
    display_name: Mapped[str] = mapped_column(String(255), default="")
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False)  # seeded sample data, never a real integration
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    conversations: Mapped[list["Conversation"]] = relationship(back_populates="contact")


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(primary_key=True)
    contact_id: Mapped[int] = mapped_column(ForeignKey("contacts.id"))
    channel: Mapped[str] = mapped_column(String(20))
    thread_ref: Mapped[str] = mapped_column(String(255), default="")  # e.g. Gmail threadId
    subject: Mapped[str] = mapped_column(String(500), default="")
    automation_paused: Mapped[bool] = mapped_column(Boolean, default=False)  # human takeover
    paused_by: Mapped[str] = mapped_column(String(100), default="")
    paused_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_message_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    contact: Mapped[Contact] = relationship(back_populates="conversations")
    messages: Mapped[list["Message"]] = relationship(
        back_populates="conversation", order_by="Message.created_at"
    )
    lead: Mapped["Lead | None"] = relationship(back_populates="conversation", uselist=False)


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (UniqueConstraint("channel", "channel_message_id", name="uq_message_channel_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id"))
    channel: Mapped[str] = mapped_column(String(20))
    channel_message_id: Mapped[str] = mapped_column(String(255))
    direction: Mapped[str] = mapped_column(String(10))  # "in" | "out"
    author: Mapped[str] = mapped_column(String(20))  # "lead" | "assistant" | "human"
    body: Mapped[str] = mapped_column(Text, default="")
    send_status: Mapped[str] = mapped_column(String(20), default="received")  # received|sent|failed
    error: Mapped[str] = mapped_column(Text, default="")
    meta: Mapped[dict] = mapped_column(JSON, default=dict)  # channel specifics, e.g. email subject/Message-ID
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    conversation: Mapped[Conversation] = relationship(back_populates="messages")


class Rep(Base):
    """Mirror of reps in the active policy. The LLM never writes here."""

    __tablename__ = "reps"

    id: Mapped[str] = mapped_column(String(50), primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    email: Mapped[str] = mapped_column(String(255))
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Lead(Base):
    __tablename__ = "leads"

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id"), unique=True)
    status: Mapped[str] = mapped_column(String(20), default=LeadStatus.NEW)
    fields: Mapped[dict] = mapped_column(JSON, default=dict)     # extracted values
    evidence: Mapped[dict] = mapped_column(JSON, default=dict)   # field -> quoted source text
    reasons: Mapped[list] = mapped_column(JSON, default=list)    # rule-engine explanations
    assigned_rep_id: Mapped[str | None] = mapped_column(ForeignKey("reps.id"), nullable=True)
    assignment_reason: Mapped[str] = mapped_column(Text, default="")
    policy_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    conversation: Mapped[Conversation] = relationship(back_populates="lead")
    assigned_rep: Mapped[Rep | None] = relationship()


class SlotOffer(Base):
    __tablename__ = "slot_offers"

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id"))
    rep_id: Mapped[str] = mapped_column(ForeignKey("reps.id"))
    label: Mapped[str] = mapped_column(String(5))  # "1", "2", "3" shown to the lead
    start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), default="active")  # active|superseded|booked
    offered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Booking(Base):
    __tablename__ = "bookings"

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id"))
    rep_id: Mapped[str] = mapped_column(ForeignKey("reps.id"))
    idempotency_key: Mapped[str] = mapped_column(String(100), unique=True)
    start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), default=BookingStatus.PENDING)
    calendar_event_id: Mapped[str] = mapped_column(String(255), default="")
    calendar_link: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class ActionLog(Base):
    __tablename__ = "action_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int | None] = mapped_column(ForeignKey("conversations.id"), nullable=True)
    actor: Mapped[str] = mapped_column(String(30))   # system | llm | rules | human:<name> | webhook | poller
    action: Mapped[str] = mapped_column(String(60))
    level: Mapped[str] = mapped_column(String(10), default="info")  # info | warn | error
    summary: Mapped[str] = mapped_column(Text, default="")
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Connection(Base):
    """Health and stored OAuth tokens for each external provider."""

    __tablename__ = "connections"

    provider: Mapped[str] = mapped_column(String(30), primary_key=True)
    status: Mapped[str] = mapped_column(String(20), default="not_connected")  # ok|expired|error|not_connected
    account: Mapped[str] = mapped_column(String(255), default="")
    credentials: Mapped[dict] = mapped_column(JSON, default=dict)  # never returned by the API
    last_error: Mapped[str] = mapped_column(Text, default="")
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    state: Mapped[dict] = mapped_column(JSON, default=dict)  # e.g. Gmail history cursor


class PolicyVersion(Base):
    __tablename__ = "policy_versions"

    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_yaml: Mapped[str] = mapped_column(Text)
    data: Mapped[dict] = mapped_column(JSON)
    created_by: Mapped[str] = mapped_column(String(100), default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
