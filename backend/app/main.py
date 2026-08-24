"""FastAPI application: auth, chat (SSE), action confirmation, insights."""

from __future__ import annotations

from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import actions, agent, auth, config, datastore, insights
from .auth import AuthError, Principal

app = FastAPI(title="ParcelPilot Support Copilot")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def current_principal(request: Request) -> Principal:
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        raise HTTPException(401, "Missing bearer token")
    try:
        return auth.resolve_token(header[7:].strip())
    except AuthError as exc:
        raise HTTPException(401, str(exc)) from exc


# ---------------------------------------------------------------------------
# Auth / personas

class LoginRequest(BaseModel):
    persona_id: str


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "model": config.MODEL, "snapshot": str(datastore.snapshot_now())}


@app.get("/api/personas")
def personas() -> dict:
    """Login screen data. Mocked identity — see docs/ARCHITECTURE.md."""
    return {"personas": datastore.personas()}


@app.post("/api/login")
def login(body: LoginRequest) -> dict:
    try:
        token = auth.issue_token(body.persona_id)
        principal = auth.resolve_token(token)
    except AuthError as exc:
        raise HTTPException(400, str(exc)) from exc
    persona = next(p for p in datastore.personas() if p["persona_id"] == body.persona_id)
    return {
        "token": token,
        "persona": persona,
        "scope": (
            f"Customer scope: only {principal.account_id} data"
            if principal.kind == "customer"
            else f"Staff ({principal.role}): all accounts"
        ),
    }


# ---------------------------------------------------------------------------
# Chat

class ChatRequest(BaseModel):
    history: list[dict] = []
    message: str | None = None
    # A server-signed execution note from /api/actions/confirm. Free-text notes
    # are NOT accepted here — the trusted channel requires a valid signature.
    note_token: str | None = None


@app.post("/api/chat")
async def chat(body: ChatRequest, principal: Principal = Depends(current_principal)):
    stream = agent.run_agent_stream(principal, body.history, body.message, body.note_token)
    return StreamingResponse(
        stream,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# Actions (phase 2: human confirmation executes)

class ConfirmRequest(BaseModel):
    signed_payload: str


@app.post("/api/actions/confirm")
def confirm_action(body: ConfirmRequest, principal: Principal = Depends(current_principal)) -> dict:
    try:
        record = actions.confirm(body.signed_payload, principal)
    except (actions.ActionError, AuthError) as exc:
        raise HTTPException(403, str(exc)) from exc
    except datastore.AccessDenied as exc:
        raise HTTPException(403, str(exc)) from exc
    # Sign a note the chat endpoint will trust: this is the only way an
    # "action executed" statement can enter the agent's trusted channel.
    note_token = auth.sign_blob(
        {
            "t": "exec_note",
            "persona_id": principal.persona_id,
            "text": (
                f"The user confirmed the action and it has now been executed. "
                f"Result: {record['summary']} (record {record['record_id']}). "
                "Acknowledge briefly and state any next steps."
            ),
        }
    )
    return {"executed": True, "record": record, "note_token": note_token}


@app.get("/api/actions/log")
def get_action_log(principal: Principal = Depends(current_principal)) -> dict:
    return {"actions": actions.action_log(principal)}


# ---------------------------------------------------------------------------
# Ops Radar (internal only — enforced here AND invisible to customer tokens)

@app.get("/api/insights")
def get_insights(principal: Principal = Depends(current_principal)) -> dict:
    if not principal.is_staff:
        raise HTTPException(403, "Ops Radar is restricted to ParcelPilot staff.")
    return insights.ops_overview()


# ---------------------------------------------------------------------------
# Static frontend (single-service deploys / local `npm run build`)

_DIST = Path(config.REPO_ROOT) / "frontend" / "dist"
if _DIST.exists():
    _DIST_ROOT = _DIST.resolve()
    app.mount("/assets", StaticFiles(directory=_DIST / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str):
        # Confine to the dist directory: resolve() collapses `..`/symlinks so a
        # traversal like /..%2f..%2fbackend%2fapp%2fconfig.py cannot escape and
        # leak source or secrets. Anything outside falls back to the SPA shell.
        if path:
            target = (_DIST / path).resolve()
            if (target == _DIST_ROOT or _DIST_ROOT in target.parents) and target.is_file():
                return FileResponse(target)
        return FileResponse(_DIST / "index.html")
