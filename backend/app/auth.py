"""Mock authentication with real enforcement.

Identity is mocked (pick a persona, no passwords — appropriate for the
assessment), but everything downstream is enforced for real:

- Session tokens are HMAC-signed server-side; a tampered token is rejected.
- The token maps to a Principal whose account scope / role is applied in the
  data layer (datastore/corpus), not in the prompt.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass

from . import config, datastore


class AuthError(Exception):
    pass


@dataclass(frozen=True)
class Principal:
    persona_id: str
    display_name: str
    org: str
    kind: str  # "customer" | "staff"
    account_id: str | None  # customers only
    role: str | None  # staff only: "support_agent" | "ops_manager"

    @property
    def account_scope(self) -> str | None:
        """Data-layer scope: customers see only their account; staff see all."""
        return self.account_id if self.kind == "customer" else None

    @property
    def is_staff(self) -> bool:
        return self.kind == "staff"

    @property
    def is_ops_manager(self) -> bool:
        return self.role == "ops_manager"


def _sign(payload: bytes) -> str:
    return hmac.new(config.SESSION_SECRET.encode(), payload, hashlib.sha256).hexdigest()


def sign_blob(data: dict) -> str:
    """Generic signed, url-safe blob (used for tokens and pending actions)."""
    payload = json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    body = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    return f"{body}.{_sign(payload)}"


def verify_blob(blob: str) -> dict:
    try:
        body, signature = blob.rsplit(".", 1)
        payload = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
    except Exception as exc:
        raise AuthError("Malformed token") from exc
    if not hmac.compare_digest(_sign(payload), signature):
        raise AuthError("Invalid signature")
    return json.loads(payload)


def issue_token(persona_id: str) -> str:
    persona = _find_persona(persona_id)
    return sign_blob({"t": "session", "persona_id": persona["persona_id"]})


def resolve_token(token: str) -> Principal:
    data = verify_blob(token)
    if data.get("t") != "session":
        raise AuthError("Not a session token")
    persona = _find_persona(data["persona_id"])
    return Principal(
        persona_id=persona["persona_id"],
        display_name=persona["display_name"],
        org=persona["org"],
        kind=persona["kind"],
        account_id=persona.get("account_id"),
        role=persona.get("role"),
    )


def _find_persona(persona_id: str) -> dict:
    for persona in datastore.personas():
        if persona["persona_id"] == persona_id:
            return persona
    raise AuthError(f"Unknown persona {persona_id!r}")


def trusted_note(note_token: str | None, principal: Principal) -> str | None:
    """Return the text of a note ONLY if it is a server-signed exec note for
    this principal. Client-supplied free text can never reach this channel,
    so neither the Claude agent nor the local SLM fallback can be tricked into
    asserting a fake action execution. Shared by app/agent.py and
    app/fallback_agent.py so the two "brains" enforce the exact same check.
    """
    if not note_token:
        return None
    try:
        data = verify_blob(note_token)
    except AuthError:
        return None
    if data.get("t") != "exec_note" or data.get("persona_id") != principal.persona_id:
        return None
    return data.get("text")
