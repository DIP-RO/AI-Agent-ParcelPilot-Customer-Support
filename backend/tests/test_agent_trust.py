"""Agent trust-channel and history-hygiene helpers (no LLM calls)."""

from app import agent, auth


def principal(persona_id: str):
    return auth.resolve_token(auth.issue_token(persona_id))


# --- Trusted note channel --------------------------------------------------

def test_trusted_note_requires_valid_signature():
    p = principal("support-rohit")
    assert agent._trusted_note(None, p) is None
    assert agent._trusted_note("garbage.sig", p) is None


def test_trusted_note_roundtrip_and_persona_binding():
    rohit = principal("support-rohit")
    meera = principal("northstar-meera")
    token = auth.sign_blob({"t": "exec_note", "persona_id": rohit.persona_id, "text": "done: CRD-1"})
    assert agent._trusted_note(token, rohit) == "done: CRD-1"
    # A note signed for one persona is not trusted for another.
    assert agent._trusted_note(token, meera) is None


def test_non_exec_note_blob_rejected():
    p = principal("support-rohit")
    # A valid session token is a signed blob, but not of type exec_note.
    session = auth.issue_token("support-rohit")
    assert agent._trusted_note(session, p) is None


# --- Replay-safe history trimming -----------------------------------------

def test_replay_safe_strips_dangling_tool_use():
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "x", "input": {}}]},
    ]
    trimmed = agent._replay_safe(messages)
    assert len(trimmed) == 1
    assert trimmed[-1]["role"] == "user"


def test_replay_safe_keeps_text_only_assistant():
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": [{"type": "text", "text": "partial answer"}]},
    ]
    assert agent._replay_safe(messages) == messages


def test_replay_safe_keeps_completed_tool_turn():
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "x", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]},
    ]
    assert agent._replay_safe(messages) == messages
