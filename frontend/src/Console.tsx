/* The console.
 *
 * One screen instead of four tabs. The four things the app knows (the graph,
 * the trace, the revision and the element inventory) were never sequential
 * steps, so putting them behind tabs hid the one fact that matters most: they
 * are all views of the same run, happening at once.
 *
 * The layout is a mission-console grid. Left rail carries the run controls and
 * the ambient counters. The centre is the agent graph over the execution
 * trace, which is the machine working. The right rail carries what it decided,
 * which is the part a 1st AD actually reads.
 */

import { useCallback, useRef, useState } from "react";
import type { Production, ProductionRow } from "./api";
import { GraphView } from "./GraphView";
import { TraceView } from "./TraceView";
import { Inventory } from "./Inventory";
import { Stream } from "./Stream";
import { NodeDetail } from "./NodeDetail";
import { Mark, Row, departmentName, label, timeOf } from "./components";
import type { RunState } from "./runState";
import { totals, type TraceState } from "./trace";
import type { GraphNode, GraphState } from "./graph";
import { RipplePanels, useLiveRipples } from "./Ripples";
import { NowWorking } from "./NowWorking";

type RightPane = "decisions" | "approvals" | "revision" | "inventory" | "history";

const PANE_LABEL: Record<RightPane, string> = {
  decisions: "Decisions",
  approvals: "Approvals",
  revision: "Revision",
  inventory: "Elements",
  history: "History",
};

export function Console({
  run,
  trace,
  graph,
  now,
  production,
  productions,
  title,
  scene,
  onPick,
  onSample,
  onUpload,
  onApprove,
  onSend,
  onGraphResize,
  decisionKey,
  recipients,
  onDecided,
  onAddRecipient,
}: {
  run: RunState;
  trace: TraceState;
  graph: GraphState;
  now: number;
  production: Production | null;
  productions: ProductionRow[] | null;
  title: string;
  scene?: string;
  onPick: (t: string) => void;
  onSample: () => void;
  onUpload: (before: File | null, after: File, production: string) => void;
  onApprove: () => void;
  onSend: () => void;
  onGraphResize: (width: number, height: number) => void;
  decisionKey: number;
  recipients: Record<string, string>;
  onDecided: () => void;
  onAddRecipient: (department: string, email: string) => Promise<void>;
}) {
  const [pane, setPane] = useState<RightPane>("decisions");
  const [picked, setPicked] = useState<GraphNode | null>(null);
  // Reported up by the decision stream, so the Approvals tab can show how
  // many are waiting without fetching the log a second time.
  const [awaiting, setAwaiting] = useState(0);
  const running = run.status === "running";
  const sums = totals(trace);

  const ripples = useLiveRipples(run.feed);

  const reports = production?.reports ?? [];
  const pending = reports.filter((r) => !r.approved_at).length;
  const approvedUnsent = reports.filter((r) => r.approved_at && !r.sent_at).length;
  const urgent = reports.reduce(
    (n, r) => n + r.notes.filter((note) => note.urgent).length,
    0,
  );

  return (
    <div className="console">
      {scene && (
        <div className="scene-banner">
          <span className="mono">Sc. {scene}</span>
          <span className="scene-banner-sep">·</span>
          <span>
            {production?.scenes.find((s) => s.number === scene)?.heading ?? "This scene"}
          </span>
        </div>
      )}

      <aside className="rail">
        <Ingest
          onSample={onSample}
          onUpload={onUpload}
          running={running}
          title={title}
          productions={productions}
          onPick={onPick}
        />

        <div className="rail-ripples">
          <h3 className="rail-title">Also checking</h3>
          <RipplePanels ripples={ripples} docked />
        </div>
      </aside>

      <section className="stage-col">
        <Panel
          title="Agent graph"
          sub={running ? "routing live" : "how the last run routed"}
        >
          <GraphView
            graph={graph}
            now={now}
            running={running}
            onResize={onGraphResize}
            onSelect={setPicked}
          />
          {picked && (
            <NodeDetail
              node={picked}
              production={production}
              recipient={recipients[picked.label] ?? ""}
              title={title}
              onClose={() => setPicked(null)}
              onAddRecipient={onAddRecipient}
            />
          )}
        </Panel>

        <Panel
          title="Execution trace"
          sub="every span, how long it took"
          badge={<span className="mono dim">{sums.calls} calls</span>}
        >
          <TraceView trace={trace} running={running} now={now} />
        </Panel>

        <Panel title="Activity" sub="the raw event stream" collapsible>
          <div className="feed">
            {run.feed.length === 0 && (
              <div className="feed-line">
                <span className="t mono">--:--</span>
                <span className="m">Nothing running. Start a revision to see events.</span>
              </div>
            )}
            {run.feed.map((event, i) => (
              <div key={i} className={`feed-line ${feedClass(event.kind)}`}>
                <span className="t mono">{timeOf(event.ts)}</span>
                <span className="m">{event.message}</span>
              </div>
            ))}
          </div>
        </Panel>
      </section>

      <aside className="results">
        <div className="results-head">
          <NowWorking graph={graph} running={running} />

          <div className="pane-tabs" role="tablist">
          {(Object.keys(PANE_LABEL) as RightPane[]).map((key) => (
            <button
              key={key}
              role="tab"
              aria-selected={pane === key}
              className="pane-tab"
              onClick={() => setPane(key)}
            >
              {PANE_LABEL[key]}
              {key === "approvals" && awaiting > 0 && (
                <span className="pane-count">{awaiting}</span>
              )}
              {key === "revision" && pending > 0 && (
                <span className="pane-count">{pending}</span>
              )}
            </button>
          ))}
          </div>
        </div>

        <div className="pane-body">
          {pane === "decisions" && (
            <Stream
              title={title}
              refreshKey={decisionKey}
              recipients={recipients}
              onChanged={onDecided}
              running={running}
              feed={run.feed}
              onAwaiting={setAwaiting}
            />
          )}
          {pane === "approvals" && (
            <Stream
              title={title}
              refreshKey={decisionKey}
              recipients={recipients}
              onChanged={onDecided}
              running={running}
              feed={run.feed}
              onAwaiting={setAwaiting}
              onlyAwaiting
            />
          )}
          {pane === "revision" && (
            <RevisionPane
              production={production}
              pending={pending}
              approvedUnsent={approvedUnsent}
              urgent={urgent}
              onApprove={onApprove}
              onSend={onSend}
            />
          )}
          {pane === "inventory" && <Inventory title={title} />}
          {pane === "history" && <HistoryPane production={production} />}
        </div>
      </aside>
    </div>
  );
}

/* Ingest.
 *
 * The pipeline takes any pair of drafts, so the UI has to as well. The sample
 * is one button next to it, not the only way in.
 */
function Ingest({
  onSample,
  onUpload,
  running,
  title,
  productions,
  onPick,
}: {
  onSample: () => void;
  onUpload: (before: File | null, after: File, production: string) => void;
  running: boolean;
  title: string;
  productions: ProductionRow[] | null;
  onPick: (t: string) => void;
}) {
  const [before, setBefore] = useState<File | null>(null);
  const [after, setAfter] = useState<File | null>(null);
  const [name, setName] = useState("");
  const beforeRef = useRef<HTMLInputElement>(null);
  const afterRef = useRef<HTMLInputElement>(null);

  const submit = useCallback(() => {
    if (!after) return;
    onUpload(before, after, name || title || after.name.replace(/\.[^.]+$/, ""));
    setBefore(null);
    setAfter(null);
    if (beforeRef.current) beforeRef.current.value = "";
    if (afterRef.current) afterRef.current.value = "";
  }, [before, after, name, title, onUpload]);

  return (
    <div className="rail-block ingest">
      <h3 className="rail-title">Ingest a revision</h3>

      <label className="drop">
        <input
          ref={afterRef}
          type="file"
          accept=".fdx,.pdf"
          onChange={(e) => setAfter(e.target.files?.[0] ?? null)}
        />
        <span className="drop-key mono">NEW</span>
        <span className="drop-name">{after?.name ?? "Drop the new draft"}</span>
      </label>

      <label className="drop optional">
        <input
          ref={beforeRef}
          type="file"
          accept=".fdx,.pdf"
          onChange={(e) => setBefore(e.target.files?.[0] ?? null)}
        />
        <span className="drop-key mono">OLD</span>
        <span className="drop-name">
          {before?.name ?? "Previous draft (only if it is not on file)"}
        </span>
      </label>

      <input
        className="text-input"
        placeholder="Production name"
        value={name}
        onChange={(e) => setName(e.target.value)}
      />

      <button
        className="btn primary wide"
        onClick={submit}
        disabled={!after || running}
      >
        {running ? "Running…" : "Run this revision"}
      </button>

      {/* Hidden for now: the drop flow (DropCapture -> App's ?run=1) starts
          the same sample run automatically, so this manual trigger is
          currently redundant. Left in place, not deleted, in case the manual
          button is wanted back later. */}
      <button className="btn wide" onClick={onSample} disabled={running} style={{ display: "none" }}>
        Run the sample pair
      </button>

      {productions && productions.length > 0 && (
        <select
          className="btn wide"
          value={title}
          onChange={(e) => onPick(e.target.value)}
          aria-label="Production"
        >
          {productions.map((row) => (
            <option key={row.id} value={row.title}>
              {row.title}
            </option>
          ))}
        </select>
      )}
    </div>
  );
}


function Panel({
  title,
  sub,
  badge,
  collapsible,
  children,
}: {
  title: string;
  sub?: string;
  badge?: React.ReactNode;
  collapsible?: boolean;
  children: React.ReactNode;
}) {
  const [open, setOpen] = useState(true);
  return (
    <section className="panel">
      <header className="panel-head">
        <h2>{title}</h2>
        {sub && <span className="sub">{sub}</span>}
        <div className="panel-right">
          {badge}
          {collapsible && (
            <button
              className="panel-toggle"
              onClick={() => setOpen((v) => !v)}
              aria-expanded={open}
            >
              {open ? "hide" : "show"}
            </button>
          )}
        </div>
      </header>
      {open && children}
    </section>
  );
}

function RevisionPane({
  production,
  pending,
  approvedUnsent,
  urgent,
  onApprove,
  onSend,
}: {
  production: Production | null;
  pending: number;
  approvedUnsent: number;
  urgent: number;
  onApprove: () => void;
  onSend: () => void;
}) {
  if (!production) {
    return (
      <div className="pane-empty">
        <p>
          Nothing ingested yet. Upload a revision pair, or run the sample, and
          the departments fill in here.
        </p>
      </div>
    );
  }

  return (
    <>
      {pending > 0 ? (
        <div className="gate">
          <span className="grow">
            <strong>{pending}</strong> report{pending === 1 ? "" : "s"} awaiting
            approval
            {urgent > 0 && (
              <>
                {" · "}
                <Mark kind="urgent">{urgent} before it shoots</Mark>
              </>
            )}
          </span>
          <button className="btn primary" onClick={onApprove}>
            Approve
          </button>
        </div>
      ) : approvedUnsent > 0 ? (
        <div className="gate">
          <span className="grow">
            <strong>{approvedUnsent}</strong> approved, not sent
          </span>
          <button className="btn primary" onClick={onSend}>
            Send
          </button>
        </div>
      ) : null}

      {production.changes.length > 0 && (
        <div className="pane-section">
          <h4 className="pane-title">
            What changed <span className="mono dim">{production.changes.length}</span>
          </h4>
          <div className="rows">
            {production.changes.map((change) => (
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
                  {change.departments.map((d) => (
                    <Mark key={d} kind="dept">
                      {departmentName(d)}
                    </Mark>
                  ))}
                </div>
              </Row>
            ))}
          </div>
        </div>
      )}

      <div className="pane-section">
        <h4 className="pane-title">
          Departments <span className="mono dim">{production.reports.length}</span>
        </h4>
        {production.reports.map((report) => (
          <div key={report.id} className="dept">
            <div className="dept-head">
              <strong>{departmentName(report.department)}</strong>
              <span className="dept-state">
                {report.sent_at ? (
                  <Mark kind="ok">sent</Mark>
                ) : report.approved_at ? (
                  <Mark kind="warn">approved</Mark>
                ) : (
                  <Mark>pending</Mark>
                )}
              </span>
            </div>
            <div className="rows">
              {report.notes.map((note, i) => (
                <Row key={i} id={note.scene_number ? `${note.scene_number}` : "--"}>
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
          </div>
        ))}
      </div>
    </>
  );
}

function HistoryPane({ production }: { production: Production | null }) {
  const runs = production?.runs ?? [];
  if (!runs.length) {
    return (
      <div className="pane-empty">
        <p>No runs recorded yet. This fills in as revisions are processed.</p>
      </div>
    );
  }
  return (
    <table>
      <thead>
        <tr>
          <th>started</th>
          <th className="num">scenes</th>
          <th className="num">findings</th>
          <th className="num">tokens</th>
          <th className="num">elapsed</th>
        </tr>
      </thead>
      <tbody>
        {runs.map((row) => (
          <tr key={row.id}>
            <td className="mono">{timeOf(Date.parse(row.started_at) / 1000)}</td>
            <td className="num">{row.scenes_parsed}</td>
            <td className="num">{row.findings}</td>
            <td className="num">
              {(row.input_tokens + row.output_tokens).toLocaleString()}
            </td>
            <td className="num mono">{elapsedOf(row)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/* Wall clock from the run's own timestamps, not an estimate: a run still in
   progress has no finished_at yet and shows as still running. */
function elapsedOf(row: { started_at: string; finished_at: string | null }): string {
  if (!row.finished_at) return "running";
  const seconds = (Date.parse(row.finished_at) - Date.parse(row.started_at)) / 1000;
  if (seconds < 1) return `${Math.round(seconds * 1000)}ms`;
  return `${seconds.toFixed(1)}s`;
}

function feedClass(kind: string): string {
  if (kind === "change.detected") return "change";
  if (kind === "parse.warning" || kind === "model.fallback") return "warn";
  if (kind === "run.failed") return "fail";
  return "";
}
