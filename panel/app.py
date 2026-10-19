"""Human approval panel. Run from repo root:  streamlit run panel/app.py"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import httpx  # noqa: E402
import streamlit as st  # noqa: E402

from app.db import Approval, AuditLog, Payment, SessionLocal  # noqa: E402
from panel.actions import approve, reject  # noqa: E402

st.set_page_config(page_title="Ops Agent Approvals", layout="wide")
st.title("Ops Agent — Human Approval Panel")
reviewer = st.sidebar.text_input("Reviewer name", "salon-manager")
api = httpx.Client(base_url=os.getenv("API_URL", "http://localhost:8000"), timeout=30)
db = SessionLocal()

tab_pending, tab_audit, tab_sec = st.tabs(["Pending approvals", "Audit log", "Security events"])

with tab_pending:
    pending = db.query(Approval).filter_by(status="pending").order_by(Approval.id).all()
    if not pending:
        st.info("No pending approvals.")
    for a in pending:
        params = json.loads(a.params_json or "{}")
        booking_id = params.get("booking_id")
        if booking_id is None and a.action_type == "refund":
            p = db.get(Payment, a.target_id)
            booking_id = p.booking_id if p else None
        with st.expander(f"#{a.id}  {a.action_type}  target {a.target_id}", expanded=True):
            c1, c2 = st.columns(2)
            with c1:
                st.subheader("Proposed action")
                st.json({"action_type": a.action_type, "target_id": a.target_id, **params})
                st.subheader("Agent reasoning")
                st.write(a.agent_reasoning)
                b1, b2 = st.columns(2)
                if b1.button("Approve", key=f"ok{a.id}", type="primary"):
                    ok, msg = approve(db, a.id, reviewer, api)
                    (st.success if ok else st.error)(msg)
                    if ok:
                        st.rerun()
                if b2.button("Reject", key=f"no{a.id}"):
                    ok, msg = reject(db, a.id, reviewer)
                    st.warning(msg)
                    st.rerun()
            with c2:
                st.subheader(f"Booking #{booking_id} timeline")
                r = api.get(f"/bookings/{booking_id}/timeline")
                if r.status_code == 200:
                    t = r.json()
                    st.json(t["booking"])
                    st.dataframe(t["events"], use_container_width=True)
                else:
                    st.error(f"could not load timeline: {r.status_code}")

def _rows(q):
    return [{"id": x.id, "timestamp": x.timestamp, "actor": x.actor, "action": x.action,
             "target": f"{x.target_type}:{x.target_id}", "outcome": x.outcome, "model": x.model,
             "prompt_version": x.prompt_version, "reason": x.reason, "params": x.params_json} for x in q]

with tab_audit:
    st.dataframe(_rows(db.query(AuditLog).order_by(AuditLog.id.desc()).limit(500)), use_container_width=True)
with tab_sec:
    sec = db.query(AuditLog).filter_by(action="security_event").order_by(AuditLog.id.desc()).all()
    st.caption(f"{len(sec)} security event(s)")
    st.dataframe(_rows(sec), use_container_width=True)
db.close()
