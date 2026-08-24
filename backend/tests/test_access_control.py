"""Access control is enforced in the data/tool layer, not the prompt."""

import pytest

from app import auth, datastore, retrieval, tools
from app.corpus import read_document
from app.datastore import AccessDenied


def principal(persona_id: str):
    return auth.resolve_token(auth.issue_token(persona_id))


NORTHSTAR = "northstar-meera"
LUMEN = "lumenworks-vikram"
STAFF = "support-rohit"
MANAGER = "ops-anita"


# --- Structured data scoping ----------------------------------------------

def test_customer_cannot_read_other_accounts_order():
    """Out-of-scope reads return the SAME not-found error as a missing id, so a
    customer cannot distinguish 'exists but not yours' from 'does not exist'."""
    p = principal(NORTHSTAR)
    with pytest.raises(KeyError):
        datastore.get_order("ORD-2001", p.account_scope)


def test_out_of_scope_looks_like_not_found():
    """An out-of-scope order raises the same KeyError format as a missing id, with
    no 'belongs to another account' signal that would confirm existence."""
    p = principal(NORTHSTAR)
    with pytest.raises(KeyError) as foreign:
        datastore.get_order("ORD-2001", p.account_scope)  # exists, other account
    assert str(foreign.value) == repr("No order with id 'ORD-2001'")
    assert "different account" not in str(foreign.value)


def test_customer_cannot_read_other_accounts_ticket():
    p = principal(LUMEN)
    with pytest.raises(KeyError):
        datastore.get_ticket("TKT-501", p.account_scope)


def test_customer_cannot_list_other_accounts_data():
    p = principal(NORTHSTAR)
    with pytest.raises(AccessDenied):
        datastore.list_orders(p.account_scope, account_id="ACCT-002")
    orders = datastore.list_orders(p.account_scope)
    assert {o["account_id"] for o in orders} == {"ACCT-001"}


def test_staff_reads_across_accounts():
    p = principal(STAFF)
    assert datastore.get_order("ORD-2001", p.account_scope)["account_id"] == "ACCT-002"
    assert len(datastore.list_accounts(p.account_scope)) == 4


# --- Document scoping ------------------------------------------------------

def test_customer_cannot_read_other_customers_agreement():
    """Uniform not-found, and the error must not name other accounts' docs."""
    p = principal(NORTHSTAR)
    with pytest.raises(KeyError) as exc:
        read_document("lumenworks-agreement", p.account_scope)
    # The requested id is echoed (caller already knows it), but the valid-ids
    # hint must not list it as an existing document.
    assert "Valid ids: " in str(exc.value)
    hint = str(exc.value).split("Valid ids: ", 1)[1]
    assert "lumenworks-agreement" not in hint


def test_read_document_error_hides_foreign_doc_ids():
    p = principal(NORTHSTAR)
    with pytest.raises(KeyError) as exc:
        read_document("does-not-exist", p.account_scope)
    msg = str(exc.value)
    assert "northstar-agreement" in msg  # own agreement is a valid hint
    assert "lumenworks-agreement" not in msg  # other account's is not disclosed


def test_search_never_returns_foreign_agreements():
    p = principal(NORTHSTAR)
    res = retrieval.search("service credit pickup threshold", p.account_scope)
    doc_ids = {r["doc_id"] for r in res["results"]}
    assert "lumenworks-agreement" not in doc_ids


def test_deprecated_quarantined_by_default():
    p = principal(STAFF)
    res = retrieval.search("support policy response targets severity", p.account_scope)
    assert all(r["status"] != "deprecated" for r in res["results"])
    with pytest.raises(AccessDenied):
        read_document("support-policy-v2", p.account_scope)


def test_staff_can_opt_into_deprecated_with_labels():
    p = principal(STAFF)
    doc = read_document("support-policy-v2", p.account_scope, allow_deprecated=True)
    assert doc["status"] == "deprecated"
    res = retrieval.search("support policy v2 response targets", p.account_scope, include_deprecated=True)
    dep = [r for r in res["results"] if r["status"] == "deprecated"]
    assert dep and all("DEPRECATED" in r["warning"] for r in dep)


def test_customer_include_deprecated_flag_is_ignored():
    """The tool layer drops the staff-only flag for customer principals."""
    p = principal(NORTHSTAR)
    result_json, is_error, raw = tools.dispatch(
        "search_documents", {"query": "response targets", "include_deprecated": True}, p
    )
    assert not is_error
    assert all(r["status"] != "deprecated" for r in raw["results"])


# --- Tool registry by role -------------------------------------------------

def test_customer_has_no_staff_tools():
    p = principal(NORTHSTAR)
    names = {t["name"] for t in tools.tool_specs_for(p)}
    assert "get_ops_overview" not in names
    assert "apply_service_credit" not in names
    assert "update_ticket" not in names


def test_dispatching_unavailable_tool_fails_closed():
    p = principal(NORTHSTAR)
    _, is_error, _ = tools.dispatch("get_ops_overview", {}, p)
    assert is_error


def test_tool_dispatch_scopes_customer_reads():
    p = principal(NORTHSTAR)
    _, is_error, _ = tools.dispatch("get_order", {"order_id": "ORD-2002"}, p)
    assert is_error


# --- Token integrity -------------------------------------------------------

def test_tampered_token_rejected():
    token = auth.issue_token(NORTHSTAR)
    body, sig = token.rsplit(".", 1)
    with pytest.raises(auth.AuthError):
        auth.resolve_token(body + "." + ("0" * len(sig)))


def test_historical_resolution_carries_warning():
    p = principal(STAFF)
    t = datastore.get_ticket("TKT-450", p.account_scope)
    assert "may be incorrect" in t["historical_resolution_warning"]
