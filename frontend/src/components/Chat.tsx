import { useEffect, useRef, useState } from "react";
import { confirmAction, streamChat } from "../api";
import { Markdown } from "../markdown";
import type { Entry, PendingAction, Session, StreamEvent, ToolEvent } from "../types";

const TOOL_ICONS: Record<string, string> = {
  search_documents: "🔍",
  read_document: "📄",
  get_account: "🏢",
  list_orders: "📦",
  get_order: "📦",
  list_tickets: "🎫",
  get_ticket: "🎫",
  evaluate_cancellation: "🧮",
  evaluate_service_credit: "🧮",
  evaluate_credit_terms: "🧮",
  check_sla: "⏱️",
  create_escalation: "🚨",
  create_support_ticket: "📝",
  update_ticket: "✏️",
  create_followup_task: "📌",
  apply_service_credit: "💰",
  get_ops_overview: "📡",
};

let clientIdCounter = 0;
function newClientId(): string {
  try {
    if (typeof crypto !== "undefined" && crypto.randomUUID) return crypto.randomUUID();
  } catch {
    /* fall through */
  }
  return `act-${Date.now()}-${clientIdCounter++}`;
}

function ToolChip({ tool }: { tool: ToolEvent }) {
  const [open, setOpen] = useState(false);
  return (
    <div className={`tool-chip ${tool.status} ${tool.is_error ? "error" : ""}`}>
      <button className="tool-chip-head" onClick={() => setOpen(!open)}>
        <span>{TOOL_ICONS[tool.name] ?? "🔧"}</span>
        <code>{tool.name}</code>
        {tool.status === "running" ? <span className="spinner" /> : tool.is_error ? "⚠️" : "✓"}
      </button>
      {open && (
        <div className="tool-detail">
          {tool.input !== undefined && (
            <>
              <div className="tool-detail-label">input</div>
              <pre>{JSON.stringify(tool.input, null, 2)}</pre>
            </>
          )}
          {tool.result !== undefined && (
            <>
              <div className="tool-detail-label">result</div>
              <pre>{JSON.stringify(tool.result, null, 2)}</pre>
            </>
          )}
        </div>
      )}
    </div>
  );
}

function ActionCard({
  action,
  onDecide,
  disabled,
}: {
  action: PendingAction;
  onDecide: (action: PendingAction, approve: boolean) => void;
  disabled: boolean;
}) {
  const status = action.ui_status ?? "pending";
  return (
    <div className={`action-card ${status}`}>
      <div className="action-card-title">
        ⚡ {action.label}
        <span className={`action-status ${status}`}>
          {status === "pending" ? "awaiting your confirmation" : status}
        </span>
      </div>
      <pre className="action-params">{JSON.stringify(action.params, null, 2)}</pre>
      {action.approval_note && <div className="action-note">{action.approval_note}</div>}
      {status === "pending" && (
        <div className="action-buttons">
          <button className="btn confirm" disabled={disabled} onClick={() => onDecide(action, true)}>
            Confirm — execute this action
          </button>
          <button className="btn cancel" disabled={disabled} onClick={() => onDecide(action, false)}>
            Cancel
          </button>
        </div>
      )}
      {action.result_summary && <div className="action-result">{action.result_summary}</div>}
    </div>
  );
}

export function Chat({ session, prefill, onPrefillUsed }: {
  session: Session;
  prefill: string | null;
  onPrefillUsed: () => void;
}) {
  const [entries, setEntries] = useState<Entry[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const historyRef = useRef<unknown[]>([]);
  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (prefill) {
      setInput(prefill);
      onPrefillUsed();
    }
  }, [prefill, onPrefillUsed]);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [entries]);

  const patchLastAssistant = (fn: (e: Extract<Entry, { kind: "assistant" }>) => void) => {
    setEntries((prev) => {
      const next = [...prev];
      for (let i = next.length - 1; i >= 0; i--) {
        const e = next[i];
        if (e.kind === "assistant") {
          const copy = { ...e, tools: [...e.tools], actions: [...e.actions] };
          fn(copy);
          next[i] = copy;
          break;
        }
      }
      return next;
    });
  };

  const handleEvent = (event: StreamEvent) => {
    switch (event.type) {
      case "text_delta":
        patchLastAssistant((e) => {
          e.text += event.text;
        });
        break;
      case "tool_start":
        patchLastAssistant((e) => {
          e.tools = [...e.tools, { name: event.name, status: "running" }];
        });
        break;
      case "tool_call":
        // Immutable update: replace the first unbound placeholder (or append).
        // Mutating shared tool objects breaks under React StrictMode's double render.
        patchLastAssistant((e) => {
          const idx = e.tools.findIndex((t) => t.name === event.name && t.id === undefined);
          if (idx >= 0) {
            e.tools = e.tools.map((t, i) =>
              i === idx ? { ...t, id: event.id, input: event.input } : t
            );
          } else {
            e.tools = [...e.tools, { id: event.id, name: event.name, input: event.input, status: "running" }];
          }
        });
        break;
      case "tool_result":
        patchLastAssistant((e) => {
          e.tools = e.tools.map((t) =>
            t.id === event.id
              ? { ...t, result: event.result, is_error: event.is_error, status: "done" as const }
              : t
          );
        });
        break;
      case "pending_action":
        patchLastAssistant((e) => {
          e.actions = [...e.actions, { ...event.action, client_id: newClientId(), ui_status: "pending" }];
        });
        break;
      case "turn_done":
        historyRef.current = event.history;
        patchLastAssistant((e) => {
          e.streaming = false;
          e.tools = e.tools.map((t) => ({ ...t, status: "done" as const }));
        });
        break;
      case "error":
        setEntries((prev) => [...prev, { kind: "error", text: event.message }]);
        // The backend also emits turn_done on error with a replay-safe history,
        // so the model keeps this turn's context; nothing else to do here.
        break;
    }
  };

  const run = async (body: { message?: string; note_token?: string }) => {
    setBusy(true);
    setEntries((prev) => [...prev, { kind: "assistant", text: "", tools: [], actions: [], streaming: true }]);
    try {
      await streamChat(session.token, { history: historyRef.current, ...body }, handleEvent);
    } catch (e) {
      setEntries((prev) => [...prev, { kind: "error", text: String(e) }]);
    } finally {
      patchLastAssistant((e) => {
        e.streaming = false;
      });
      setBusy(false);
    }
  };

  const send = () => {
    const text = input.trim();
    if (!text || busy) return;
    setInput("");
    setEntries((prev) => [...prev, { kind: "user", text }]);
    void run({ message: text });
  };

  const decideAction = async (action: PendingAction, approve: boolean) => {
    if (busy) return;
    if (!approve) {
      patchAction(action, { ui_status: "declined" });
      setEntries((prev) => [...prev, { kind: "note", text: `Action declined: ${action.label}` }]);
      // Plain user text — declines don't need the trusted channel.
      void run({ message: `I've declined the pending "${action.label}" action. Please don't execute it.` });
      return;
    }
    try {
      const res = await confirmAction(session.token, action.signed_payload);
      patchAction(action, { ui_status: "executed", result_summary: res.record.summary });
      setEntries((prev) => [...prev, { kind: "note", text: `✅ ${res.record.summary}` }]);
      // Server-signed note: the only way an "executed" statement is trusted.
      void run({ note_token: res.note_token });
    } catch (e) {
      patchAction(action, { ui_status: "failed", result_summary: String(e) });
      setEntries((prev) => [...prev, { kind: "error", text: `Action failed: ${String(e)}` }]);
      void run({ message: `The action I confirmed failed to execute: ${String(e)}` });
    }
  };

  const patchAction = (action: PendingAction, patch: Partial<PendingAction>) => {
    setEntries((prev) =>
      prev.map((e) =>
        e.kind === "assistant"
          ? {
              ...e,
              actions: e.actions.map((a) =>
                a.client_id === action.client_id ? { ...a, ...patch } : a
              ),
            }
          : e
      )
    );
  };

  return (
    <div className="chat">
      <div className="chat-scroll" ref={scrollRef}>
        {entries.length === 0 && (
          <div className="empty-state">
            <h3>
              {session.persona.kind === "customer"
                ? `Hi ${session.persona.display_name.split(" ")[0]} — how can I help with your shipments?`
                : "What would you like to investigate?"}
            </h3>
            <p className="scope-note">{session.scope}. Try one of these:</p>
            <div className="suggestions">
              {session.persona.suggested_prompts.map((s) => (
                <button key={s} onClick={() => setInput(s)}>
                  {s}
                </button>
              ))}
            </div>
          </div>
        )}
        {entries.map((entry, i) => {
          if (entry.kind === "user") {
            return (
              <div key={i} className="bubble user">
                {entry.text}
              </div>
            );
          }
          if (entry.kind === "note") {
            return (
              <div key={i} className="note">
                {entry.text}
              </div>
            );
          }
          if (entry.kind === "error") {
            return (
              <div key={i} className="error-banner">
                {entry.text}
              </div>
            );
          }
          return (
            <div key={i} className="bubble assistant">
              {entry.tools.length > 0 && (
                <div className="tool-row">
                  {entry.tools.map((t, j) => (
                    <ToolChip key={j} tool={t} />
                  ))}
                </div>
              )}
              {entry.text ? (
                <Markdown text={entry.text} />
              ) : entry.streaming ? (
                <div className="thinking">thinking…</div>
              ) : null}
              {entry.actions.map((a, j) => (
                <ActionCard key={j} action={a} onDecide={decideAction} disabled={busy} />
              ))}
            </div>
          );
        })}
      </div>
      <div className="composer">
        <textarea
          value={input}
          placeholder={busy ? "Working…" : "Ask about orders, credits, SLAs, policies…"}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              send();
            }
          }}
          rows={2}
          disabled={busy}
        />
        <button className="btn send" onClick={send} disabled={busy || !input.trim()}>
          Send
        </button>
      </div>
    </div>
  );
}
