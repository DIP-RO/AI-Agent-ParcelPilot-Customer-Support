import { useEffect, useState } from "react";
import { fetchPersonas, login } from "../api";
import type { Persona, Session } from "../types";

export function Login({ onLogin }: { onLogin: (s: Session) => void }) {
  const [personas, setPersonas] = useState<Persona[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  useEffect(() => {
    fetchPersonas().then(setPersonas).catch((e) => setError(String(e)));
  }, []);

  const pick = async (p: Persona) => {
    setBusy(p.persona_id);
    try {
      onLogin(await login(p.persona_id));
    } catch (e) {
      setError(String(e));
      setBusy(null);
    }
  };

  const customers = personas.filter((p) => p.kind === "customer");
  const staff = personas.filter((p) => p.kind === "staff");

  return (
    <div className="login">
      <div className="login-hero">
        <h1>📦 ParcelPilot Support Copilot</h1>
        <p>
          Pick a persona to see the copilot from their side. Authentication is mocked for the demo,
          but every data access below is enforced server-side for the persona you choose.
        </p>
        {error && <div className="error-banner">{error}</div>}
      </div>
      <div className="login-columns">
        <section>
          <h2>Customer view</h2>
          {customers.map((p) => (
            <button key={p.persona_id} className="persona-card" onClick={() => pick(p)} disabled={busy !== null}>
              <div className="persona-name">
                {busy === p.persona_id ? "Signing in…" : p.display_name}
                <span className="persona-org">{p.org}</span>
              </div>
              <div className="persona-blurb">{p.blurb}</div>
            </button>
          ))}
        </section>
        <section>
          <h2>Internal view (ParcelPilot staff)</h2>
          {staff.map((p) => (
            <button key={p.persona_id} className="persona-card staff" onClick={() => pick(p)} disabled={busy !== null}>
              <div className="persona-name">
                {busy === p.persona_id ? "Signing in…" : p.display_name}
                <span className="persona-org">{p.role === "ops_manager" ? "Ops Manager" : "Support Agent"}</span>
              </div>
              <div className="persona-blurb">{p.blurb}</div>
            </button>
          ))}
        </section>
      </div>
    </div>
  );
}
