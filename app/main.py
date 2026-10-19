import threading
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Literal, Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError, OperationalError

from app.db import (AuditLog, Base, Booking, Client, FeeEvent, Payment, Salon,
                    SessionLocal, engine, write_audit)
from app.gateway import GatewayError, gateway

_lock = threading.Lock()  # guards refund / waiver / webhook read-modify-write sections


@asynccontextmanager
async def lifespan(_):
    from seed import seed_if_empty
    Base.metadata.create_all(engine)
    seed_if_empty()
    yield


app = FastAPI(title="Salon Booking & Payments", lifespan=lifespan)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def row(o):
    return {c.name: getattr(o, c.name) for c in o.__table__.columns}


# ---------- schemas ----------
class BookingIn(BaseModel):
    salon_id: int
    client_id: int
    service_name: str
    start_time: datetime
    notes: Optional[str] = None


class WaiveIn(BaseModel):
    reason: str = Field(min_length=3)
    actor: str = "system"


class RefundIn(BaseModel):
    amount: float = Field(gt=0)
    reason: str = Field(min_length=3)
    actor: str = "system"


class WebhookIn(BaseModel):
    event_id: str
    gateway_ref: str
    status: Literal["succeeded", "failed"]


# ---------- helpers ----------
def _refunded_total(db, payment_id):
    rows = db.query(Payment).filter(
        Payment.type == "refund", Payment.status.in_(["pending", "succeeded"]),
        Payment.idempotency_key.like(f"refund:{payment_id}:%")).all()
    return round(sum(r.amount for r in rows), 2)


def _do_refund(db, pay, amount, reason, actor, key_suffix=None):
    """Caller must hold _lock. Refund payments are keyed 'refund:<orig_id>:<suffix>' so they link to the original."""
    key = f"refund:{pay.id}:{key_suffix or uuid.uuid4().hex[:12]}"
    existing = db.query(Payment).filter_by(idempotency_key=key).first()
    if existing:
        return existing, True
    amount = round(amount, 2)
    if pay.type not in ("deposit", "no_show_fee"):
        raise HTTPException(409, "only deposits and no-show fees can be refunded")
    if pay.status != "succeeded":
        raise HTTPException(409, f"payment is {pay.status}; cannot refund")
    remaining = round(pay.amount - _refunded_total(db, pay.id), 2)
    if amount <= 0:
        raise HTTPException(422, "amount must be positive")
    if amount > remaining:
        raise HTTPException(422, f"refund {amount} exceeds refundable amount {remaining}")
    r = Payment(booking_id=pay.booking_id, type="refund", amount=amount, status="pending", idempotency_key=key)
    db.add(r)
    db.commit()
    try:
        ref = gateway.refund(pay.gateway_ref, amount)
    except GatewayError as e:
        r.status = "failed"
        write_audit(db, "system", "refund_failed", "payment", pay.id, {"amount": amount}, str(e), "error")
        db.commit()
        raise HTTPException(502, f"gateway failure: {e}")
    r.status, r.gateway_ref = "succeeded", ref
    if round(remaining - amount, 2) == 0:
        pay.status = "refunded"
    db.add(FeeEvent(booking_id=pay.booking_id, type="refunded", amount=amount, reason=reason, actor=actor))
    write_audit(db, "system", "refund_executed", "booking", pay.booking_id,
                {"payment_id": pay.id, "amount": amount, "requested_by": actor}, reason)
    db.commit()
    return r, False


def _settle_fee_charge(db, pay):
    try:
        ref = gateway.charge(pay.amount, pay.idempotency_key)
    except GatewayError as e:
        pay.status = "failed"
        write_audit(db, "system", "payment_failed", "booking", pay.booking_id, {"type": pay.type}, str(e), "error")
        db.commit()
        raise HTTPException(502, f"gateway failure: {e}")
    pay.status, pay.gateway_ref = "succeeded", ref
    db.add(FeeEvent(booking_id=pay.booking_id, type="charged", amount=pay.amount, reason="no-show fee", actor="system"))
    write_audit(db, "system", "no_show_fee_charged", "booking", pay.booking_id, {"amount": pay.amount})
    db.commit()


# ---------- endpoints ----------
@app.get("/")
def get_root():
    return {"status": "ok", "timestamp": datetime.utcnow().isoformat() + "Z"}

@app.post("/bookings", status_code=201)
def create_booking(body: BookingIn, idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
                   db=Depends(get_db)):
    if not idempotency_key:
        raise HTTPException(400, "Idempotency-Key header required")
    if db.query(Payment).filter_by(idempotency_key=idempotency_key).first():
        raise HTTPException(409, "Idempotency-Key already used")
    salon, client = db.get(Salon, body.salon_id), db.get(Client, body.client_id)
    if not salon or not client:
        raise HTTPException(404, "salon or client not found")
    booking = Booking(salon_id=salon.id, client_id=client.id, service_name=body.service_name,
                      start_time=body.start_time, status="booked", notes=body.notes)
    db.add(booking)
    db.flush()
    pay = Payment(booking_id=booking.id, type="deposit", amount=salon.deposit_amount,
                  status="pending", idempotency_key=idempotency_key)
    db.add(pay)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Idempotency-Key already used")
    try:
        ref = gateway.charge(pay.amount, idempotency_key)
    except GatewayError as e:
        pay.status, booking.status = "failed", "cancelled"
        write_audit(db, "system", "deposit_failed", "booking", booking.id, None, str(e), "error")
        db.commit()
        raise HTTPException(402, "deposit payment failed; booking cancelled. Retry with a new Idempotency-Key")
    pay.status, pay.gateway_ref = "succeeded", ref
    booking.deposit_payment_id = pay.id
    write_audit(db, "system", "booking_created", "booking", booking.id, {"deposit": pay.amount})
    db.commit()
    return {"booking": row(booking), "payment": row(pay)}


@app.post("/bookings/{booking_id}/no-show")
def no_show(booking_id: int, db=Depends(get_db)):
    b = db.get(Booking, booking_id)
    if not b:
        raise HTTPException(404, "booking not found")
    if b.status not in ("booked", "no_show"):
        raise HTTPException(409, f"booking is {b.status}; cannot mark no-show")
    salon = db.get(Salon, b.salon_id)
    fee = round(min(salon.deposit_amount, salon.no_show_fee_cap), 2)
    existing = db.query(Payment).filter_by(booking_id=b.id, type="no_show_fee").first()
    pay = None
    if existing is None:
        try:  # the unique index on (booking_id, type) is the real concurrency guard
            b.status = "no_show"
            pay = Payment(booking_id=b.id, type="no_show_fee", amount=fee, status="pending",
                          idempotency_key=f"no_show_fee:{b.id}")
            db.add(pay)
            write_audit(db, "system", "no_show_marked", "booking", b.id)
            db.commit()
        except (IntegrityError, OperationalError):
            db.rollback()
            pay = None
            existing = db.query(Payment).filter_by(booking_id=b.id, type="no_show_fee").first()
            if existing is None:
                raise HTTPException(503, "concurrent update; retry")
    if pay is not None:
        _settle_fee_charge(db, pay)
        return {"booking": row(b), "payment": row(pay), "already_processed": False}
    if existing.status == "failed":  # previous attempt hit a gateway failure: retry same row
        existing.status = "pending"
        db.commit()
        _settle_fee_charge(db, existing)
        return {"booking": row(b), "payment": row(existing), "already_processed": False}
    db.refresh(b)
    return {"booking": row(b), "payment": row(existing), "already_processed": True}


@app.post("/bookings/{booking_id}/waive-fee")
def waive_fee(booking_id: int, body: WaiveIn, db=Depends(get_db)):
    """Not exposed to the agent as a tool: only the human approval panel executes this."""
    with _lock:
        b = db.get(Booking, booking_id)
        if not b:
            raise HTTPException(404, "booking not found")
        fee = db.query(Payment).filter_by(booking_id=b.id, type="no_show_fee").first()
        if not fee:
            raise HTTPException(409, "no fee exists on this booking")
        if db.query(FeeEvent).filter_by(booking_id=b.id, type="waived").first():
            return {"already_waived": True, "payment": row(fee)}
        if fee.status == "succeeded":  # fee was collected: waiving means returning it
            remaining = round(fee.amount - _refunded_total(db, fee.id), 2)
            if remaining > 0:
                _do_refund(db, fee, remaining, f"fee waived: {body.reason}", body.actor, key_suffix=f"waive{b.id}")
        db.add(FeeEvent(booking_id=b.id, type="waived", amount=fee.amount, reason=body.reason, actor=body.actor))
        write_audit(db, "system", "fee_waived", "booking", b.id, {"amount": fee.amount, "by": body.actor}, body.reason)
        db.commit()
        db.refresh(fee)
        return {"already_waived": False, "payment": row(fee)}


@app.post("/payments/{payment_id}/refund")
def refund(payment_id: int, body: RefundIn, idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
           db=Depends(get_db)):
    with _lock:
        pay = db.get(Payment, payment_id)
        if not pay:
            raise HTTPException(404, "payment not found")
        r, dup = _do_refund(db, pay, body.amount, body.reason, body.actor, key_suffix=idempotency_key)
        db.refresh(pay)
        return {"refund": row(r), "payment": row(pay), "duplicate": dup}


@app.get("/bookings/{booking_id}/timeline")
def timeline(booking_id: int, db=Depends(get_db)):
    b = db.get(Booking, booking_id)
    if not b:
        raise HTTPException(404, "booking not found")
    salon, client = db.get(Salon, b.salon_id), db.get(Client, b.client_id)
    pays = db.query(Payment).filter_by(booking_id=b.id).order_by(Payment.created_at, Payment.id).all()
    fes = db.query(FeeEvent).filter_by(booking_id=b.id).all()
    audits = db.query(AuditLog).filter(AuditLog.target_type == "booking", AuditLog.target_id == str(b.id),
                                       AuditLog.actor.in_(["system", "human"])).all()
    events = [{"timestamp": p.created_at, "kind": "payment", "type": p.type, "payment_id": p.id,
               "amount": p.amount, "status": p.status} for p in pays]
    events += [{"timestamp": f.created_at, "kind": "fee_event", "type": f.type, "amount": f.amount,
                "reason": f.reason, "actor": f.actor} for f in fes]
    events += [{"timestamp": a.timestamp, "kind": "booking_event", "type": a.action, "actor": a.actor,
                "reason": a.reason} for a in audits]
    events.sort(key=lambda e: e["timestamp"])
    return {"booking": {**row(b), "client_name": client.name}, "salon": row(salon),
            "payments": [row(p) for p in pays], "events": events}


@app.post("/webhooks/payment")
def payment_webhook(body: WebhookIn, db=Depends(get_db)):
    with _lock:
        if db.query(AuditLog).filter_by(action="webhook_event", target_id=body.event_id).first():
            return {"status": "duplicate_ignored"}
        pay = db.query(Payment).filter_by(gateway_ref=body.gateway_ref).first()
        if not pay:
            raise HTTPException(404, "unknown gateway_ref")
        if pay.status == "pending":
            pay.status = body.status
        write_audit(db, "system", "webhook_event", "webhook", body.event_id,
                    {"gateway_ref": body.gateway_ref, "status": body.status, "payment_id": pay.id})
        db.commit()
        return {"status": "processed", "payment": row(pay)}
