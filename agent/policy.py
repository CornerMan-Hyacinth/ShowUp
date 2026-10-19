"""Hard rules, enforced in code (not in prompts)."""
import re

APPROVED_TEMPLATES = {"fee_explained", "refund_offered", "escalated_to_human"}
MAX_TOOL_CALLS = 10
REDACTED = "[REDACTED: suspected prompt injection]"

_INJECTION = [
    r"ignore\s+(all\s+|any\s+|the\s+)?(previous\s+|prior\s+|above\s+)?(instructions|rules|prompts?)",
    r"disregard\s+(all\s+|any\s+|the\s+)?(previous\s+|prior\s+|above\s+)?(instructions|rules)",
    r"^\s*(system|assistant|developer)\s*:",
    r"\byou\s+are\s+now\b",
    r"new\s+(system\s+)?instructions?\s*:",
    r"reveal\s+(your\s+|the\s+)?(system\s+)?prompt",
    r"override\s+(the\s+)?(policy|rules|guardrails)",
    r"</?\s*(system|instructions?)\s*>",
]
_INJECTION_RE = [re.compile(p, re.IGNORECASE | re.MULTILINE) for p in _INJECTION]


def detect_injection(text) -> bool:
    return isinstance(text, str) and any(r.search(text) for r in _INJECTION_RE)


def sender_owns(message, client_id, salon_id) -> bool:
    if message.sender_type == "client":
        return client_id == message.sender_id
    if message.sender_type == "salon":
        return salon_id == message.sender_id
    return False


def check_refund(message, payment, booking, amount, remaining):
    """Returns (allowed, reason)."""
    if not sender_owns(message, booking.client_id, booking.salon_id):
        return False, "payment does not belong to the sender"
    if payment.type not in ("deposit", "no_show_fee") or payment.status != "succeeded":
        return False, f"payment of type {payment.type} with status {payment.status} is not refundable"
    if amount <= 0 or round(amount, 2) > round(remaining, 2):
        return False, f"amount {amount} exceeds refundable amount {remaining}"
    if payment.type == "no_show_fee":
        return True, "refund of a no-show fee is allowed"
    if booking.status == "no_show":
        if message.sender_type == "salon":
            return True, "salon requested deposit refund after no-show"
        return False, "deposit refund after a no-show is not allowed unless the salon requests it"
    return False, "deposit refund outside the no-show case is not covered by policy"


def check_waiver(message, booking, fee_payment, already_waived):
    if not sender_owns(message, booking.client_id, booking.salon_id):
        return False, "booking does not belong to the sender"
    if booking.status != "no_show" or fee_payment is None:
        return False, "no no-show fee exists on this booking"
    if already_waived:
        return False, "fee already waived"
    return True, "waiver may be requested"
