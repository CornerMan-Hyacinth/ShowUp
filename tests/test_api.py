import threading

from app.db import FeeEvent, Payment, Booking
from app.gateway import gateway

BOOKING = {"salon_id": 1, "client_id": 1, "service_name": "Trim", "start_time": "2026-12-01T10:00:00"}


# ---------- happy paths ----------
def test_create_booking_charges_deposit(client):
    r = client.post("/bookings", json=BOOKING, headers={"Idempotency-Key": "k1"})
    assert r.status_code == 201
    b, p = r.json()["booking"], r.json()["payment"]
    assert p["type"] == "deposit" and p["amount"] == 20.0 and p["status"] == "succeeded"
    assert b["deposit_payment_id"] == p["id"] and b["status"] == "booked"


def test_create_booking_requires_idempotency_key(client):
    assert client.post("/bookings", json=BOOKING).status_code == 400


def test_no_show_charges_min_of_deposit_and_cap(client, db):
    r = client.post("/bookings/1/no-show")
    assert r.status_code == 200
    assert r.json()["booking"]["status"] == "no_show"
    assert r.json()["payment"]["amount"] == 20.0 and r.json()["payment"]["status"] == "succeeded"


def test_no_show_fee_respects_cap(client, db):
    from app.db import Salon
    s = db.get(Salon, 1)
    s.deposit_amount, s.no_show_fee_cap = 80.0, 50.0
    db.commit()
    assert client.post("/bookings/1/no-show").json()["payment"]["amount"] == 50.0


def test_waive_fee_requires_reason_and_refunds_collected_fee(client, db):
    assert client.post("/bookings/4/waive-fee", json={}).status_code == 422
    r = client.post("/bookings/4/waive-fee", json={"reason": "family emergency", "actor": "human"})
    assert r.status_code == 200 and r.json()["payment"]["status"] == "refunded"
    types = [f.type for f in db.query(FeeEvent).filter_by(booking_id=4)]
    assert "waived" in types and "refunded" in types
    assert client.post("/bookings/4/waive-fee", json={"reason": "again"}).json()["already_waived"] is True


def test_full_refund(client):
    r = client.post("/payments/6/refund", json={"amount": 20, "reason": "client was present"})
    assert r.status_code == 200
    assert r.json()["payment"]["status"] == "refunded" and r.json()["refund"]["type"] == "refund"


def test_timeline_is_chronological_and_complete(client):
    client.post("/payments/6/refund", json={"amount": 20, "reason": "client was present"})
    t = client.get("/bookings/4/timeline").json()
    stamps = [e["timestamp"] for e in t["events"]]
    assert stamps == sorted(stamps)
    kinds = {(e["kind"], e["type"]) for e in t["events"]}
    assert ("payment", "deposit") in kinds and ("payment", "no_show_fee") in kinds
    assert ("fee_event", "charged") in kinds and ("fee_event", "refunded") in kinds
    assert ("payment", "refund") in kinds
    assert client.get("/bookings/999/timeline").status_code == 404


def test_webhook_happy_path(client):
    r = client.post("/webhooks/payment", json={"event_id": "e1", "gateway_ref": "mock_seed_1", "status": "succeeded"})
    assert r.status_code == 200 and r.json()["status"] == "processed"
    assert client.post("/webhooks/payment", json={"event_id": "e2", "gateway_ref": "nope", "status": "failed"}).status_code == 404


# ---------- idempotency ----------
def test_duplicate_idempotency_key_returns_409(client, db):
    assert client.post("/bookings", json=BOOKING, headers={"Idempotency-Key": "dup"}).status_code == 201
    assert client.post("/bookings", json=BOOKING, headers={"Idempotency-Key": "dup"}).status_code == 409
    assert db.query(Booking).count() == 6  # 5 seeded + 1


def test_duplicate_no_show_charges_once(client, db):
    first = client.post("/bookings/1/no-show").json()
    second = client.post("/bookings/1/no-show").json()
    assert first["already_processed"] is False and second["already_processed"] is True
    assert db.query(Payment).filter_by(booking_id=1, type="no_show_fee").count() == 1
    assert first["payment"]["id"] == second["payment"]["id"]


def test_duplicate_webhook_is_idempotent(client):
    body = {"event_id": "evt-7", "gateway_ref": "mock_seed_2", "status": "succeeded"}
    assert client.post("/webhooks/payment", json=body).json()["status"] == "processed"
    r = client.post("/webhooks/payment", json=body)
    assert r.status_code == 200 and r.json()["status"] == "duplicate_ignored"


def test_refund_idempotency_key_replays_same_refund(client, db):
    h = {"Idempotency-Key": "r1"}
    a = client.post("/payments/6/refund", json={"amount": 5, "reason": "partial"}, headers=h).json()
    b = client.post("/payments/6/refund", json={"amount": 5, "reason": "partial"}, headers=h).json()
    assert b["duplicate"] is True and a["refund"]["id"] == b["refund"]["id"]
    assert db.query(Payment).filter_by(type="refund").count() == 1


# ---------- refund validation ----------
def test_over_refund_rejected(client):
    assert client.post("/payments/6/refund", json={"amount": 20.01, "reason": "too much"}).status_code == 422
    assert client.post("/payments/6/refund", json={"amount": 0, "reason": "zero"}).status_code == 422


def test_partial_refund_state_transitions(client):
    r1 = client.post("/payments/6/refund", json={"amount": 5, "reason": "part one"}).json()
    assert r1["payment"]["status"] == "succeeded"          # still partially collected
    assert client.post("/payments/6/refund", json={"amount": 16, "reason": "too much"}).status_code == 422
    r2 = client.post("/payments/6/refund", json={"amount": 15, "reason": "part two"}).json()
    assert r2["payment"]["status"] == "refunded"           # fully refunded
    assert client.post("/payments/6/refund", json={"amount": 1, "reason": "again"}).status_code == 409


# ---------- concurrency ----------
def test_concurrent_no_show_charges_exactly_once(client, db, monkeypatch):
    calls = []
    orig = gateway.charge
    monkeypatch.setattr(gateway, "charge", lambda *a, **k: (calls.append(1), orig(*a, **k))[1])
    results = []
    barrier = threading.Barrier(2)

    def hit():
        barrier.wait()
        results.append(client.post("/bookings/2/no-show"))

    threads = [threading.Thread(target=hit) for _ in range(2)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert all(r.status_code == 200 for r in results), [r.text for r in results]
    assert db.query(Payment).filter_by(booking_id=2, type="no_show_fee").count() == 1
    assert len(calls) == 1                                   # gateway charged once, not twice
    assert sorted(r.json()["already_processed"] for r in results) == [False, True]
