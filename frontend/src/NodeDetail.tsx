/* Clicking a node in the graph.
 *
 * A department node knows more than its label: which findings routed to it,
 * what its agent wrote, whether anything is urgent, and who the brief is
 * emailed to. A graph you can only look at is a diagram; one you can open is
 * an interface onto the run.
 */

import { useState } from "react";
import { Link } from "react-router-dom";
import type { Production } from "./api";
import type { GraphNode } from "./graph";
import { departmentName } from "./components";

export function NodeDetail({
  node,
  production,
  recipient,
  title,
  onClose,
  onAddRecipient,
}: {
  node: GraphNode;
  production: Production | null;
  recipient: string;
  title: string;
  onClose: () => void;
  onAddRecipient: (department: string, email: string) => Promise<void>;
}) {
  const [email, setEmail] = useState("");
  const [saving, setSaving] = useState(false);

  const isDepartment = node.kind === "department";
  const key = node.label;
  const report = isDepartment
    ? (production?.reports ?? []).find((r) => r.department === key)
    : undefined;
  const routed = isDepartment
    ? (production?.changes ?? []).filter((c) => c.departments.includes(key))
    : [];

  const save = async () => {
    if (!email.includes("@")) return;
    setSaving(true);
    try {
      await onAddRecipient(key, email);
      setEmail("");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="node-detail" role="dialog" aria-label={`${key} detail`}>
      <header>
        <strong>{isDepartment ? departmentName(key) : node.label}</strong>
        {node.detail && <span className="mono node-detail-sub">{node.detail}</span>}
        <button className="node-close" onClick={onClose} aria-label="Close">
          ×
        </button>
      </header>

      <div className="node-body">
        {!isDepartment && (
          <p className="node-stage">
            Pipeline stage. {node.hits > 0 ? `${node.hits} processed.` : "Idle."}
          </p>
        )}

        {isDepartment && (
          <>
            {node.log.length > 0 && (
              <div className="node-log">
                <span className="node-log-title">
                  {node.state === "active" ? "Working" : "What it did"}
                </span>
                <ul>
                  {node.log.map((line, i) => (
                    <li key={i} className={i === node.log.length - 1 && node.state === "active" ? "current" : ""}>
                      {line.text}
                    </li>
                  ))}
                </ul>
              </div>
            )}

            <div className="node-to">
              {recipient ? (
                <>
                  Brief goes to <span className="mono">{recipient}</span>
                </>
              ) : (
                <div className="node-addr">
                  <span className="hint">No recipient set. The brief cannot send.</span>
                  <div className="node-addr-row">
                    <input
                      className="text-input"
                      placeholder="head@production.film"
                      value={email}
                      onChange={(e) => setEmail(e.target.value)}
                      onKeyDown={(e) => e.key === "Enter" && save()}
                    />
                    <button className="btn" onClick={save} disabled={saving}>
                      {saving ? "…" : "Add"}
                    </button>
                  </div>
                </div>
              )}
            </div>

            {report && (
              <p className="node-summary">{report.summary}</p>
            )}

            {report && report.notes.length > 0 && (
              <ul className="node-notes">
                {report.notes.slice(0, 4).map((note, i) => (
                  <li key={i}>
                    {note.scene_number && (
                      <span className="mono node-scene">Sc. {note.scene_number}</span>
                    )}
                    <span>{note.note}</span>
                    {note.urgent === 1 && <span className="node-urgent">urgent</span>}
                  </li>
                ))}
              </ul>
            )}

            {!report && routed.length > 0 && (
              <p className="hint">
                {routed.length} finding{routed.length === 1 ? "" : "s"} routed here.
              </p>
            )}

            {!report && routed.length === 0 && (
              <p className="hint">This revision changes nothing for {departmentName(key)}.</p>
            )}

            {report && (
              <Link
                className="btn wide node-open-inbox"
                to={`/inbox/${encodeURIComponent(key)}?title=${encodeURIComponent(title)}`}
              >
                Open as {departmentName(key)} sees it
              </Link>
            )}
          </>
        )}
      </div>
    </div>
  );
}
