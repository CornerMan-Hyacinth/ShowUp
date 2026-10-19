"""Human decision logic. Approving executes the action against Layer 1; the agent never does."""
import json
import os

from app.db import Approval, utcnow, write_audit


def _api(api):
    if api is not None:
        return api
    import httpx
    return httpx.Client(base_url=os.getenv("API_URL", "http://localhost:8000"), timeout=30)


def approve(db, approval_id, decided_by, api=None):
    a = db.get(Approval, approval_id)
    if a is None or a.status != "pending":
        return False, "approval not found or already decided"
    params = json.loads(a.params_json or "{}")
    api = _api(api)
    if a.action_type == "refund":
        r = api.post(f"/payments/{a.target_id}/refund",
                     json={"amount": params["amount"], "reason": params["reason"], "actor": f"human:{decided_by}"},
                     headers={"Idempotency-Key": f"approval-{a.id}"})
    elif a.action_type == "waive_fee":
        r = api.post(f"/bookings/{a.target_id}/waive-fee",
                     json={"reason": params["reason"], "actor": f"human:{decided_by}"})
    else:
        return False, f"unknown action_type {a.action_type}"
    if r.status_code != 200:
        write_audit(db, "human", f"execute_{a.action_type}", "approval", a.id, params, f"{r.status_code}: {r.text[:300]}",
                    "error")
        db.commit()
        return False, f"Layer 1 rejected the action ({r.status_code}): {r.text[:200]}"
    a.status, a.decided_by, a.decided_at = "approved", decided_by, utcnow()
    write_audit(db, "human", f"approve_{a.action_type}", "approval", a.id,
                {**params, "target_id": a.target_id}, f"approved by {decided_by}", "success")
    db.commit()
    return True, "executed"


def reject(db, approval_id, decided_by):
    a = db.get(Approval, approval_id)
    if a is None or a.status != "pending":
        return False, "approval not found or already decided"
    a.status, a.decided_by, a.decided_at = "rejected", decided_by, utcnow()
    write_audit(db, "human", f"reject_{a.action_type}", "approval", a.id, json.loads(a.params_json or "{}"),
                f"rejected by {decided_by}", "success")
    db.commit()
    return True, "rejected"
