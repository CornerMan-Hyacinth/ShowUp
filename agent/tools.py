import json
from datetime import datetime

from app.db import (Approval, Booking, Client, FeeEvent, Payment, Salon, utcnow, write_audit)
from agent import policy

TOOL_SCHEMAS = [
    {"type": "function", "function": {
        "name": "lookup_booking", "description": "Read a booking (status, time, service, notes, salon policy).",
        "parameters": {"type": "object", "properties": {"booking_id": {"type": "integer"}}, "required": ["booking_id"]}}},
    {"type": "function", "function": {
        "name": "get_payment_timeline", "description": "Read the chronological payment/fee history of a booking, including payment ids.",
        "parameters": {"type": "object", "properties": {"booking_id": {"type": "integer"}}, "required": ["booking_id"]}}},
    {"type": "function", "function": {
        "name": "reply_to_client",
        "description": "Send a reply from an approved template: fee_explained, refund_offered, escalated_to_human.",
        "parameters": {"type": "object", "properties": {
            "message_id": {"type": "integer"},
            "template_name": {"type": "string", "enum": sorted(policy.APPROVED_TEMPLATES)},
            "params": {"type": "object", "properties": {"booking_id": {"type": "integer"}, "note": {"type": "string"}}}},
            "required": ["message_id", "template_name", "params"]}}},
    {"type": "function", "function": {
        "name": "request_refund_approval",
        "description": "Ask a human to approve a refund. NEVER executes the refund.",
        "parameters": {"type": "object", "properties": {"payment_id": {"type": "integer"}, "amount": {"type": "number"},
                                                         "reason": {"type": "string"}},
                       "required": ["payment_id", "amount", "reason"]}}},
    {"type": "function", "function": {
        "name": "request_waiver_approval",
        "description": "Ask a human to approve waiving a no-show fee. NEVER executes the waiver.",
        "parameters": {"type": "object", "properties": {"booking_id": {"type": "integer"}, "reason": {"type": "string"}},
                       "required": ["booking_id", "reason"]}}},
    {"type": "function", "function": {
        "name": "escalate", "description": "Hand the message to a human. Use whenever unsure or out of policy.",
        "parameters": {"type": "object", "properties": {"message_id": {"type": "integer"}, "summary": {"type": "string"}},
                       "required": ["message_id", "summary"]}}},
]
TOOL_NAMES = {t["function"]["name"] for t in TOOL_SCHEMAS}

TEMPLATES = {
    "fee_explained": ("Hi {name}, here are the details for your {service_name} booking (#{booking_id}): "
                      "scheduled for {start_time}, status: {status}.{fee_line}{note}"),
    "refund_offered": ("Hi {name}, we've asked a team member to approve a refund of ${amount:.2f} for booking "
                       "#{booking_id}. We'll confirm once it has been reviewed."),
    "escalated_to_human": ("Hi {name}, thanks for your message. We've passed it to a member of the salon team, "
                           "who will follow up with you directly.{note}"),
}


class ToolBox:
    """Executes tools for ONE inbound message. Every call is audited with model + prompt version."""

    def __init__(self, db, api, message, model, prompt_version):
        self.db, self.api, self.message = db, api, message
        self.model, self.prompt_version = model, prompt_version
        self.refund_approvals, self.escalated, self.replied, self.requested = [], False, False, False
        self._flagged = set()

    # ----- audit helpers -----
    def audit(self, action, target_type, target_id, params, reason, outcome):
        write_audit(self.db, "agent", action, target_type, target_id, params, reason, outcome,
                    self.model, self.prompt_version)
        self.db.commit()

    def security_event(self, source, text, target_type, target_id):
        if text in self._flagged:
            return
        self._flagged.add(text)
        self.audit("security_event", target_type, target_id,
                   {"source": source, "offending_text": text, "message_id": self.message.id},
                   "untrusted text contained instructions aimed at the agent; ignored", "denied")

    def sanitize(self, obj, source, target_type, target_id):
        """Recursively redact strings that look like instructions; log each as a security_event."""
        if isinstance(obj, str):
            if policy.detect_injection(obj):
                self.security_event(source, obj, target_type, target_id)
                return policy.REDACTED
            return obj
        if isinstance(obj, dict):
            return {k: self.sanitize(v, source, target_type, target_id) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self.sanitize(v, source, target_type, target_id) for v in obj]
        return obj

    # ----- reads (sender-scoped) -----
    def _timeline(self, booking_id):
        r = self.api.get(f"/bookings/{int(booking_id)}/timeline")
        if r.status_code != 200:
            return None
        data = r.json()
        b = data["booking"]
        if not policy.sender_owns(self.message, b["client_id"], b["salon_id"]):
            return None  # same answer as not-found: don't reveal other people's bookings
        return data

    def lookup_booking(self, booking_id):
        data = self._timeline(booking_id)
        if data is None:
            return {"error": "booking_not_found"}, "error"
        res = {"booking": data["booking"], "salon_policy": data["salon"]}
        return self.sanitize(res, "booking_data", "booking", booking_id), "success"

    def get_payment_timeline(self, booking_id):
        data = self._timeline(booking_id)
        if data is None:
            return {"error": "booking_not_found"}, "error"
        res = {"booking_id": int(booking_id), "payments": data["payments"], "events": data["events"]}
        return self.sanitize(res, "timeline_data", "booking", booking_id), "success"

    # ----- writes -----
    def reply_to_client(self, message_id, template_name, params):
        if int(message_id) != self.message.id:
            return {"error": "message_id_mismatch"}, "denied"
        if template_name not in policy.APPROVED_TEMPLATES:
            return {"error": "template_not_approved", "allowed": sorted(policy.APPROVED_TEMPLATES)}, "denied"
        params = params or {}
        note = str(params.get("note") or "")[:200]
        if policy.detect_injection(note):
            self.security_event("agent_reply_note", note, "message", self.message.id)
            note = ""
        name = self._sender_name()
        ctx = {"name": name, "note": (" " + note) if note else "", "booking_id": params.get("booking_id")}
        if template_name == "refund_offered":
            if not self.refund_approvals:
                return {"error": "no_refund_approval_requested_in_this_run"}, "denied"
            ctx["amount"] = self.refund_approvals[-1]["amount"]
            bid = self.refund_approvals[-1]["booking_id"]
            ctx["booking_id"] = bid
        elif template_name == "fee_explained":
            b = self._owned_booking(params.get("booking_id"))
            if b is None:
                return {"error": "booking_not_found"}, "error"
            fee = self.db.query(Payment).filter_by(booking_id=b.id, type="no_show_fee").first()
            fee_line = (f" A no-show fee of ${fee.amount:.2f} applies (the lesser of the deposit and the salon's cap)."
                        if fee else "")
            ctx.update(service_name=b.service_name, booking_id=b.id, status=b.status, fee_line=fee_line,
                       start_time=f"{b.start_time:%A %d %B %Y at %H:%M}")
        text = TEMPLATES[template_name].format(**ctx)
        self.replied = True
        return {"sent": True, "rendered_text": text}, "success"

    def _sender_name(self):
        if self.message.sender_type == "client":
            c = self.db.get(Client, self.message.sender_id)
            return c.name if c else "there"
        s = self.db.get(Salon, self.message.sender_id)
        return s.name if s else "team"

    def _owned_booking(self, booking_id):
        try:
            b = self.db.get(Booking, int(booking_id))
        except (TypeError, ValueError):
            return None
        if b and policy.sender_owns(self.message, b.client_id, b.salon_id):
            return b
        return None

    def request_refund_approval(self, payment_id, amount, reason):
        pay = self.db.get(Payment, int(payment_id))
        booking = self.db.get(Booking, pay.booking_id) if pay else None
        if not pay or not booking:
            return {"error": "payment_not_found"}, "error"
        from app.main import _refunded_total
        remaining = pay.amount - _refunded_total(self.db, pay.id)
        ok, why = policy.check_refund(self.message, pay, booking, float(amount), remaining)
        if not ok:
            return {"error": "policy_denied", "reason": why, "instruction": "Do not retry. Call escalate."}, "denied"
        existing = self.db.query(Approval).filter_by(action_type="refund", target_id=pay.id, status="pending").first()
        if existing is None:
            self.db.add(Approval(action_type="refund", target_id=pay.id, status="pending", agent_reasoning=reason,
                                 params_json=json.dumps({"amount": round(float(amount), 2), "reason": reason,
                                                         "booking_id": booking.id, "message_id": self.message.id})))
            self.db.commit()
        self.refund_approvals.append({"amount": round(float(amount), 2), "booking_id": booking.id})
        self.requested = True
        return {"approval_requested": True, "status": "pending_human_decision", "policy": why}, "success"

    def request_waiver_approval(self, booking_id, reason):
        b = self._owned_booking(booking_id)
        if b is None:
            return {"error": "booking_not_found"}, "error"
        fee = self.db.query(Payment).filter_by(booking_id=b.id, type="no_show_fee").first()
        waived = self.db.query(FeeEvent).filter_by(booking_id=b.id, type="waived").first() is not None
        ok, why = policy.check_waiver(self.message, b, fee, waived)
        if not ok:
            return {"error": "policy_denied", "reason": why, "instruction": "Do not retry. Call escalate."}, "denied"
        existing = self.db.query(Approval).filter_by(action_type="waive_fee", target_id=b.id, status="pending").first()
        if existing is None:
            self.db.add(Approval(action_type="waive_fee", target_id=b.id, status="pending", agent_reasoning=reason,
                                 params_json=json.dumps({"reason": reason, "booking_id": b.id,
                                                         "message_id": self.message.id})))
            self.db.commit()
        self.requested = True
        return {"approval_requested": True, "status": "pending_human_decision"}, "success"

    def escalate(self, message_id, summary):
        self.escalated = True
        return {"escalated": True, "message_id": self.message.id}, "success"

    # ----- dispatcher -----
    def call(self, name, args):
        if name not in TOOL_NAMES:
            self.audit(name, "tool", name, args, "unsupported action requested by model", "denied")
            return {"error": "unknown_tool", "available": sorted(TOOL_NAMES)}
        target_type = {"request_refund_approval": "payment"}.get(name, "booking" if "booking" in name else "message")
        target_id = args.get("payment_id") or args.get("booking_id") or args.get("message_id")
        try:
            result, outcome = getattr(self, name)(**args)
        except TypeError as e:
            result, outcome = {"error": f"bad_arguments: {e}"}, "error"
        except Exception as e:  # never let a tool crash the run
            result, outcome = {"error": f"tool_failure: {e}"}, "error"
        self.audit(name, target_type, target_id, args, json.dumps(result, default=str)[:500], outcome)
        return result
