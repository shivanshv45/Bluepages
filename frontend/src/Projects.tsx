/* The project picker.
 *
 * Opens before the console. A production office does not pick a project from
 * a grid of glossy cards; it pulls a folder off a shelf. Each block reads like
 * a slate: title, latest draft colour, scene count, when it last moved. No
 * shadows, no gradients, no cover art. The ruled-row primitive, just wide.
 */

import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, type ProductionRow } from "./api";
import { ago } from "./components";

export function Projects() {
  const navigate = useNavigate();
  const [rows, setRows] = useState<ProductionRow[] | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    api
      .productions()
      .then(setRows)
      .catch((e: Error) => setError(e.message));
  }, []);

  const open = (title: string) => {
    if (!title) {
      navigate(`/dashboard?title=`);
      return;
    }
    navigate(`/scenes?title=${encodeURIComponent(title)}`);
  };

  return (
    <div className="projects">
      <header className="projects-nav">
        <div className="wordmark">
          blue<span>pages</span>
        </div>
        <span className="projects-tag mono">select a production</span>
      </header>

      {error && <div className="err banner">{error}</div>}

      {rows === null && !error && (
        <div className="projects-loading mono">reading the production list…</div>
      )}

      {rows && rows.length > 0 && (
        <div className="slate-grid">
          {rows.map((row) => (
            <SlateBlock key={row.id} row={row} onOpen={() => open(row.title)} />
          ))}
          <NewSlate onOpen={() => open("")} />
        </div>
      )}

      {rows && rows.length === 0 && (
        <div className="slate-grid">
          <NewSlate onOpen={() => open("")} />
        </div>
      )}
    </div>
  );
}

function SlateBlock({ row, onOpen }: { row: ProductionRow; onOpen: () => void }) {
  const lastRun = row.runs?.[0] ?? null;
  const pending = lastRun && lastRun.findings > 0 && !lastRun.finished_at;

  return (
    <button className="slate" onClick={onOpen}>
      <div className="slate-band slate-blue" aria-hidden="true" />
      <div className="slate-body">
        <div className="slate-top">
          <h3>{row.title}</h3>
          <span className="slate-colour mono slate-blue">Blue pages</span>
        </div>
        <dl className="slate-stats">
          <div>
            <dt>Drafts</dt>
            <dd className="mono">{row.drafts}</dd>
          </div>
          <div>
            <dt>Scenes</dt>
            <dd className="mono">{row.latest_draft?.scene_count ?? "--"}</dd>
          </div>
          <div>
            <dt>Last run</dt>
            <dd className="mono">
              {lastRun ? ago(lastRun.finished_at ?? lastRun.started_at) : "never"}
            </dd>
          </div>
        </dl>
        <div className="slate-status">
          {pending ? (
            <span className="slate-pip warn">awaiting approval</span>
          ) : lastRun ? (
            <span className="slate-pip ok">watching</span>
          ) : (
            <span className="slate-pip">no runs yet</span>
          )}
        </div>
      </div>
    </button>
  );
}

function NewSlate({ onOpen }: { onOpen: () => void }) {
  return (
    <button className="slate slate-new" onClick={onOpen}>
      <div className="slate-band slate-empty" aria-hidden="true" />
      <div className="slate-body">
        <div className="slate-top">
          <h3>New production</h3>
        </div>
        <p className="slate-new-copy">
          Ingest a first draft and Bluepages starts watching for the next one.
        </p>
      </div>
    </button>
  );
}
