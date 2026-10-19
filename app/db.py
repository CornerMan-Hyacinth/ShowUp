import json
import os
from datetime import datetime, timezone

from sqlalchemy import (Column, DateTime, Float, ForeignKey, Index, Integer,
                        String, Text, create_engine, event, text)
from sqlalchemy.orm import declarative_base, sessionmaker

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./salon.db")
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False, "timeout": 30})


@event.listens_for(engine, "connect")
def _pragmas(conn, _):
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")


SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
Base = declarative_base()


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Salon(Base):
    __tablename__ = "salons"
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    deposit_amount = Column(Float, nullable=False, default=20.00)
    no_show_fee_cap = Column(Float, nullable=False, default=50.00)


class Client(Base):
    __tablename__ = "clients"
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    email = Column(String)
    phone = Column(String)


class Booking(Base):
    __tablename__ = "bookings"
    id = Column(Integer, primary_key=True)
    salon_id = Column(Integer, ForeignKey("salons.id"), nullable=False)
    client_id = Column(Integer, ForeignKey("clients.id"), nullable=False)
    service_name = Column(String, nullable=False)
    start_time = Column(DateTime, nullable=False)
    status = Column(String, nullable=False, default="booked")  # booked|completed|no_show|cancelled
    deposit_payment_id = Column(Integer)
    notes = Column(Text)  # free text, may contain hostile content


class Payment(Base):
    __tablename__ = "payments"
    id = Column(Integer, primary_key=True)
    booking_id = Column(Integer, ForeignKey("bookings.id"), nullable=False)
    type = Column(String, nullable=False)  # deposit|no_show_fee|refund
    amount = Column(Float, nullable=False)
    status = Column(String, nullable=False, default="pending")  # pending|succeeded|failed|refunded
    gateway_ref = Column(String)
    idempotency_key = Column(String, nullable=False, unique=True)
    created_at = Column(DateTime, default=utcnow)
    # One deposit and one no-show fee per booking (refunds may be many: partial refunds)
    __table_args__ = (
        Index("uq_payment_booking_type", "booking_id", "type", unique=True,
              sqlite_where=text("type != 'refund'")),
    )


class FeeEvent(Base):
    __tablename__ = "fee_events"
    id = Column(Integer, primary_key=True)
    booking_id = Column(Integer, ForeignKey("bookings.id"), nullable=False)
    type = Column(String, nullable=False)  # charged|waived|refunded
    amount = Column(Float, nullable=False)
    reason = Column(Text)
    actor = Column(String)
    created_at = Column(DateTime, default=utcnow)


class InboundMessage(Base):
    __tablename__ = "inbound_messages"
    id = Column(Integer, primary_key=True)
    sender_type = Column(String, nullable=False)  # client|salon
    sender_id = Column(Integer, nullable=False)
    body = Column(Text, nullable=False)
    received_at = Column(DateTime, default=utcnow)
    processed_at = Column(DateTime)


class Approval(Base):
    __tablename__ = "approvals"
    id = Column(Integer, primary_key=True)
    action_type = Column(String, nullable=False)  # refund|waive_fee
    target_id = Column(Integer, nullable=False)   # payment id (refund) / booking id (waive_fee)
    params_json = Column(Text)
    agent_reasoning = Column(Text)
    status = Column(String, nullable=False, default="pending")  # pending|approved|rejected
    decided_by = Column(String)
    created_at = Column(DateTime, default=utcnow)
    decided_at = Column(DateTime)


class AuditLog(Base):
    __tablename__ = "audit_log"
    id = Column(Integer, primary_key=True)
    timestamp = Column(DateTime, default=utcnow)
    actor = Column(String, nullable=False)  # agent|human|system
    action = Column(String, nullable=False)
    target_type = Column(String)
    target_id = Column(String)
    params_json = Column(Text)
    reason = Column(Text)
    model = Column(String)
    prompt_version = Column(String)
    outcome = Column(String, nullable=False, default="success")  # success|denied|error


def write_audit(db, actor, action, target_type=None, target_id=None, params=None, reason=None,
                outcome="success", model=None, prompt_version=None):
    """Adds an audit row to the session. Caller commits."""
    row = AuditLog(actor=actor, action=action, target_type=target_type,
                   target_id=None if target_id is None else str(target_id),
                   params_json=json.dumps(params, default=str) if params is not None else None,
                   reason=reason, model=model, prompt_version=prompt_version, outcome=outcome)
    db.add(row)
    return row
