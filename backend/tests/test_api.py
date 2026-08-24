"""HTTP-layer tests (no LLM calls): auth, insights gating, action endpoints."""

from fastapi.testclient import TestClient

from app import actions, auth
from app.main import app

client = TestClient(app)


def login(persona_id: str) -> dict:
    r = client.post("/api/login", json={"persona_id": persona_id})
    assert r.status_code == 200
    token = r.json()["token"]
    return {"Authorization": f"Bearer {token}"}


def test_health():
    r = client.get("/api/health")
    assert r.status_code == 200
    assert "2026-08-16" in r.json()["snapshot"]


def test_personas_listed():
    r = client.get("/api/personas")
    ids = {p["persona_id"] for p in r.json()["personas"]}
    assert {"northstar-meera", "support-rohit", "ops-anita"} <= ids


def test_login_unknown_persona():
    assert client.post("/api/login", json={"persona_id": "nope"}).status_code == 400


def test_chat_requires_auth():
    assert client.post("/api/chat", json={"history": [], "message": "hi"}).status_code == 401


def test_insights_staff_only():
    assert client.get("/api/insights", headers=login("northstar-meera")).status_code == 403
    r = client.get("/api/insights", headers=login("support-rohit"))
    assert r.status_code == 200
    body = r.json()
    assert body["sla_board"]
    assert body["known_issue_clusters"]


def test_confirm_endpoint_round_trip():
    headers = login("support-rohit")
    principal = auth.resolve_token(headers["Authorization"][7:])
    pending = actions.prepare("create_followup_task", {"title": "Check KI-208 fix"}, principal)
    r = client.post("/api/actions/confirm", json={"signed_payload": pending["signed_payload"]}, headers=headers)
    assert r.status_code == 200
    assert r.json()["record"]["record_id"].startswith("TASK-")
    log = client.get("/api/actions/log", headers=headers).json()["actions"]
    assert len(log) == 1


def test_confirm_endpoint_rejects_other_user():
    rohit_headers = login("support-rohit")
    principal = auth.resolve_token(rohit_headers["Authorization"][7:])
    pending = actions.prepare("create_followup_task", {"title": "x"}, principal)
    meera_headers = login("northstar-meera")
    r = client.post(
        "/api/actions/confirm", json={"signed_payload": pending["signed_payload"]}, headers=meera_headers
    )
    assert r.status_code == 403


def test_confirm_endpoint_rejects_tampered_payload():
    headers = login("support-rohit")
    r = client.post("/api/actions/confirm", json={"signed_payload": "abc.def"}, headers=headers)
    assert r.status_code == 403
