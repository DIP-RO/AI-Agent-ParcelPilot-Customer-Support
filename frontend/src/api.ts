import type { Persona, Session, StreamEvent } from "./types";

async function json<T>(res: Response): Promise<T> {
  if (!res.ok) {
    const body = await res.text();
    let detail = body;
    try {
      detail = JSON.parse(body).detail ?? body;
    } catch {
      /* plain text */
    }
    throw new Error(detail || `HTTP ${res.status}`);
  }
  return res.json() as Promise<T>;
}

export async function fetchPersonas(): Promise<Persona[]> {
  const data = await json<{ personas: Persona[] }>(await fetch("/api/personas"));
  return data.personas;
}

export async function login(personaId: string): Promise<Session> {
  return json<Session>(
    await fetch("/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ persona_id: personaId }),
    })
  );
}

export async function confirmAction(
  token: string,
  signedPayload: string
): Promise<{ executed: boolean; record: { record_id: string; summary: string }; note_token: string }> {
  return json(
    await fetch("/api/actions/confirm", {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
      body: JSON.stringify({ signed_payload: signedPayload }),
    })
  );
}

export async function fetchInsights(token: string): Promise<Record<string, any>> {
  return json(await fetch("/api/insights", { headers: { Authorization: `Bearer ${token}` } }));
}

/** POST /api/chat and invoke onEvent for every SSE frame. */
export async function streamChat(
  token: string,
  body: { history: unknown[]; message?: string; note_token?: string },
  onEvent: (event: StreamEvent) => void,
  signal?: AbortSignal
): Promise<void> {
  const res = await fetch("/api/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
    body: JSON.stringify(body),
    signal,
  });
  if (!res.ok || !res.body) {
    const text = await res.text().catch(() => "");
    throw new Error(text || `HTTP ${res.status}`);
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let sep;
    while ((sep = buffer.indexOf("\n\n")) >= 0) {
      const frame = buffer.slice(0, sep);
      buffer = buffer.slice(sep + 2);
      const line = frame.split("\n").find((l) => l.startsWith("data: "));
      if (line) onEvent(JSON.parse(line.slice(6)) as StreamEvent);
    }
  }
}
