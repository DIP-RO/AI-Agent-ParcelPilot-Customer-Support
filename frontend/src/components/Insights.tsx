import { useEffect, useState } from "react";
import { fetchInsights } from "../api";
import type { Session } from "../types";

export function Insights({ session, onDiscuss }: { session: Session; onDiscuss: (q: string) => void }) {
  const [data, setData] = useState<Record<string, any> | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetchInsights(session.token).then(setData).catch((e) => setError(String(e)));
  }, [session.token]);

  if (error) return <div className="error-banner">{error}</div>;
  if (!data) return <div className="empty-state"><p>Scanning support activity…</p></div>;

  return (
    <div className="insights">
      <div className="insights-summary">
        <strong>📡 Ops Radar</strong> · as of {data.reference_time}
        <p>{data.summary}</p>
      </div>

      <section>
        <h3>First-response SLA board</h3>
        <table>
          <thead>
            <tr>
              <th>Ticket</th><th>Account</th><th>Subject</th><th>Sev*</th><th>Target</th><th>Due</th><th>Status</th>
            </tr>
          </thead>
          <tbody>
            {data.sla_board.map((r: any) => (
              <tr key={r.ticket_id} className={r.breached ? "breached" : ""}>
                <td>
                  <button className="link" onClick={() => onDiscuss(`Walk me through ${r.ticket_id}: severity, SLA position, and what we should do.`)}>
                    {r.ticket_id}
                  </button>
                </td>
                <td>{r.account}</td>
                <td>{r.subject}</td>
                <td>{r.suggested_severity}</td>
                <td>{r.first_response_target}</td>
                <td>{r.due_at}</td>
                <td>{r.breached ? `🔴 ${r.margin}` : `🟢 ${r.margin}`}{r.escalated ? " · escalated" : ""}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <p className="fine-print">*Suggested severity from transparent keyword heuristics — confirm against Support Policy v3 §2.</p>
      </section>

      <section>
        <h3>Known-issue clusters</h3>
        {data.known_issue_clusters.map((c: any) => (
          <div key={c.issue_id} className="cluster-card">
            <div className="cluster-head">
              <strong>{c.issue_id}</strong> — {c.title} <span className="pill">{c.issue_status}</span>
              {c.multi_account && <span className="pill warn">multi-account</span>}
              {c.tickets.length > 1 && <span className="pill warn">{c.tickets.length} tickets</span>}
            </div>
            <ul>
              {c.tickets.map((t: any) => (
                <li key={t.ticket_id}>
                  {t.ticket_id} ({t.account}, {t.status}) — {t.subject}
                </li>
              ))}
            </ul>
            {c.workaround && <div className="fine-print">Workaround/guidance: {c.workaround}</div>}
          </div>
        ))}
      </section>

      <section>
        <h3>Needs attention</h3>
        {data.attention_items.map((a: any, i: number) => (
          <div key={i} className="attention-card">
            <span className="pill">{a.kind.replaceAll("_", " ")}</span>
            <p>{a.detail}</p>
            <button
              className="link"
              onClick={() => onDiscuss(`About ${a.order_id} (${a.account}): ${a.detail} What should we do? Prepare the action if appropriate.`)}
            >
              Discuss in chat →
            </button>
          </div>
        ))}
      </section>

      <section>
        <h3>Historical answers that may be wrong</h3>
        {data.historical_answer_risks.map((h: any) => (
          <div key={h.ticket_id} className="attention-card">
            <span className="pill warn">{h.ticket_id} · {h.account}</span>
            <p>“{h.historical_resolution}”</p>
            <button
              className="link"
              onClick={() => onDiscuss(`Re-verify the historical resolution on ${h.ticket_id} against the current policy and this account's agreement. Was our past answer correct?`)}
            >
              Re-verify in chat →
            </button>
          </div>
        ))}
        <p className="fine-print">{data.caveats.join(" ")}</p>
      </section>
    </div>
  );
}
