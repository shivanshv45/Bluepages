/* The department inbox.
 *
 * The other end of the fan-out: what a head of department actually opens.
 * Not the console, not the trace, none of the machinery. One brief, in their
 * own vocabulary, with the scenes it touches and nothing about the seven
 * other departments who also got paged.
 */

import { useEffect, useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import { api, type Production } from "./api";
import { Mark, Row, departmentName, label } from "./components";

export function DepartmentInbox() {
  const { department = "" } = useParams();
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const title = params.get("title") ?? "";

  const [production, setProduction] = useState<Production | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!title) return;
    api
      .production(title)
      .then(setProduction)
      .catch((e: Error) => setError(e.message));
  }, [title]);

  const report = production?.reports.find((r) => r.department === department);
  const changes = (production?.changes ?? []).filter((c) => c.departments.includes(department));

  return (
    <div className="inbox">
      <header className="inbox-nav">
        <button className="linkish" onClick={() => navigate(-1)}>
          ← Back to the console
        </button>
        <span className="inbox-tag mono">department view</span>
      </header>

      <div className="inbox-head">
        <span className="inbox-dept mono">{departmentName(department)}</span>
        <h1>{title || "Production"}</h1>
        {report && (
          <div className="inbox-meta">
            <span>
              From Bluepages
              {report.sent_at && <> · sent {new Date(report.sent_at).toLocaleString()}</>}
            </span>
            {report.via_fallback === 1 && <span className="mono dim">answered by a fallback model</span>}
          </div>
        )}
      </div>

      {error && <div className="err banner">{error}</div>}

      {!production && !error && <div className="notice">Reading the inbox…</div>}

      {production && !report && (
        <div className="notice">
          Nothing for {departmentName(department)} in the latest revision. Bluepages only
          writes when there is something to act on.
        </div>
      )}

      {report && (
        <>
          <p className="inbox-summary">{report.summary}</p>

          <div className="rows">
            {report.notes.map((note, i) => (
              <Row key={i} id={note.scene_number ? note.scene_number : "--"}>
                <div className="row-note">{note.note}</div>
                {note.action && <div className="row-action">&rarr; {note.action}</div>}
                {note.urgent === 1 && (
                  <div className="row-meta">
                    <Mark kind="urgent">before it shoots</Mark>
                  </div>
                )}
              </Row>
            ))}
          </div>

          {changes.length > 0 && (
            <div className="inbox-section">
              <h2 className="pane-title">What changed for this department</h2>
              <div className="rows">
                {changes.map((change) => (
                  <Row
                    key={change.id}
                    id={
                      change.from_scene && change.from_scene !== change.scene_number
                        ? `${change.from_scene}→${change.scene_number}`
                        : (change.scene_number ?? "--")
                    }
                  >
                    <div className="row-note">{change.summary}</div>
                    {change.reasoning && <div className="row-why">{change.reasoning}</div>}
                    <div className="row-meta">
                      <Mark>{label(change.kind)}</Mark>
                    </div>
                  </Row>
                ))}
              </div>
            </div>
          )}

          <div className="inbox-foot">
            <span className="hint">
              This brief was drafted by Bluepages and released by the 1st AD. Replies go to the
              production, not to the agent.
            </span>
          </div>
        </>
      )}
    </div>
  );
}
