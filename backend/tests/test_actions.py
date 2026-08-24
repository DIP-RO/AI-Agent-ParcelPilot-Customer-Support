"""Two-phase action flow: prepare never executes; confirm verifies and executes."""

import pytest

from app import actions, auth, datastore
from app.actions import ActionError


def principal(persona_id: str):
    return auth.resolve_token(auth.issue_token(persona_id))


def test_prepare_does_not_execute():
    p = principal("support-rohit")
    pending = actions.prepare("create_escalation", {"ticket_id": "TKT-501", "reason": "P1 SLA breach"}, p)
    assert pending["status"] == "awaiting_user_confirmation"
    assert actions.action_log(p) == []
    assert not datastore.get_ticket("TKT-501", None).get("escalated")


def test_confirm_executes_and_mutates():
    p = principal("support-rohit")
    pending = actions.prepare("create_escalation", {"ticket_id": "TKT-501", "reason": "P1 SLA breach"}, p)
    record = actions.confirm(pending["signed_payload"], p)
    assert record["status"] == "completed"
    assert record["record_id"].startswith("ESC-")
    ticket = datastore.get_ticket("TKT-501", None)
    assert ticket["escalated"] is True
    assert any("ESC-" in n for n in ticket["internal_notes"])
    assert len(actions.action_log(p)) == 1


def test_tampered_payload_rejected():
    p = principal("support-rohit")
    pending = actions.prepare("create_escalation", {"reason": "x"}, p)
    body, sig = pending["signed_payload"].rsplit(".", 1)
    with pytest.raises(auth.AuthError):
        actions.confirm(body + "." + ("0" * len(sig)), p)


def test_confirm_requires_same_principal():
    """A pending action prepared for one user cannot be confirmed by another."""
    rohit = principal("support-rohit")
    meera = principal("northstar-meera")
    pending = actions.prepare("create_escalation", {"reason": "x"}, rohit)
    with pytest.raises(ActionError):
        actions.confirm(pending["signed_payload"], meera)


def test_customer_cannot_prepare_staff_actions():
    meera = principal("northstar-meera")
    with pytest.raises(ActionError):
        actions.prepare("update_ticket", {"ticket_id": "TKT-501", "status": "closed"}, meera)
    with pytest.raises(ActionError):
        actions.prepare("apply_service_credit", {"account_id": "ACCT-001", "amount_inr": 100, "reason": "x"}, meera)


def test_customer_action_scoped_to_own_account():
    meera = principal("northstar-meera")
    with pytest.raises(Exception):
        actions.prepare("create_escalation", {"ticket_id": "TKT-502", "reason": "other account's ticket"}, meera)
    pending = actions.prepare("create_escalation", {"ticket_id": "TKT-501", "reason": "ok"}, meera)
    assert pending["params"]["account_id"] == "ACCT-001"


def test_credit_above_1000_needs_ops_manager():
    rohit = principal("support-rohit")
    anita = principal("ops-anita")
    big = {"account_id": "ACCT-002", "amount_inr": 1500, "reason": "goodwill"}
    with pytest.raises(ActionError, match="manager"):
        actions.prepare("apply_service_credit", big, rohit)
    pending = actions.prepare("apply_service_credit", big, anita)
    assert "approval_note" in pending
    record = actions.confirm(pending["signed_payload"], anita)
    assert record["record_id"].startswith("CRD-")


def test_small_credit_allowed_for_support_agent():
    rohit = principal("support-rohit")
    pending = actions.prepare(
        "apply_service_credit",
        {"account_id": "ACCT-002", "order_id": "ORD-2002", "amount_inr": 300, "reason": "failed pickup"},
        rohit,
    )
    record = actions.confirm(pending["signed_payload"], rohit)
    assert "300" in record["summary"]


def test_customer_action_log_scoped():
    rohit = principal("support-rohit")
    meera = principal("northstar-meera")
    vikram = principal("lumenworks-vikram")
    pending = actions.prepare("create_escalation", {"ticket_id": "TKT-501", "reason": "x"}, rohit)
    actions.confirm(pending["signed_payload"], rohit)
    # Staff-created escalation on ACCT-001: visible to staff and to ACCT-001, not ACCT-002
    assert len(actions.action_log(rohit)) == 1
    assert len(actions.action_log(meera)) == 1
    assert len(actions.action_log(vikram)) == 0
