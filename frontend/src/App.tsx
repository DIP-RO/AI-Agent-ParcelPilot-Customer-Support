import { useCallback, useState } from "react";
import { Chat } from "./components/Chat";
import { Insights } from "./components/Insights";
import { Login } from "./components/Login";
import type { Session } from "./types";

export default function App() {
  const [session, setSession] = useState<Session | null>(null);
  const [tab, setTab] = useState<"chat" | "radar">("chat");
  const [prefill, setPrefill] = useState<string | null>(null);

  const discuss = useCallback((q: string) => {
    setPrefill(q);
    setTab("chat");
  }, []);
  const prefillUsed = useCallback(() => setPrefill(null), []);

  // Reset to chat on any session change so a customer never lands on (and gets
  // stuck behind) the staff-only Ops Radar tab left over from a staff session.
  const changeSession = useCallback((s: Session | null) => {
    setTab("chat");
    setSession(s);
  }, []);

  if (!session) return <Login onLogin={changeSession} />;

  const p = session.persona;
  return (
    <div className="shell">
      <header className="topbar">
        <div className="brand">📦 ParcelPilot Support Copilot</div>
        {p.kind === "staff" && (
          <nav className="tabs">
            <button className={tab === "chat" ? "active" : ""} onClick={() => setTab("chat")}>💬 Chat</button>
            <button className={tab === "radar" ? "active" : ""} onClick={() => setTab("radar")}>📡 Ops Radar</button>
          </nav>
        )}
        <div className="identity">
          <span className="scope-pill" title={session.scope}>
            {p.kind === "customer" ? `${p.org} · ${p.account_id}` : `Staff · ${p.role === "ops_manager" ? "Ops Manager" : "Support Agent"}`}
          </span>
          <span className="user-name">{p.display_name}</span>
          <button className="link" onClick={() => changeSession(null)}>Switch persona</button>
        </div>
      </header>
      <main>
        {tab === "chat" ? (
          <Chat session={session} prefill={prefill} onPrefillUsed={prefillUsed} />
        ) : (
          <Insights session={session} onDiscuss={discuss} />
        )}
      </main>
    </div>
  );
}
