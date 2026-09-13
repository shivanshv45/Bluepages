/* The trace view.
 *
 * A waterfall of the run's actual spans: every model call, what it cost, how
 * long it took, which model answered and whether it came from cache. This is
 * the screen that shows the thing is an agent rather than a form submit.
 *
 * The bars are positioned against the run's own wall clock, so a call that
 * took four seconds is four times the width of one that took one. Concurrency
 * is visible as overlap, which is the point: the fan-out really is parallel.
 */

import { useState } from "react";
import { durationOf, fmtCost, fmtDuration, ordered, totals, type Span, type TraceState } from "./trace";
import { departmentName } from "./components";

export function TraceView({
  trace,
  running,
  now,
}: {
  trace: TraceState;
  running: boolean;
  now: number;
}) {
  const [selected, setSelected] = useState<string>("");
  const spans = ordered(trace);

  if (!spans.length) {
    return (
      <div className="trace-empty">
        <p>No spans yet. Start a run and the tree fills in as work happens.</p>
      </div>
    );
  }

  const sums = totals(trace);
  const t0 = Math.min(...spans.map((s) => s.startedAt));
  const t1 = Math.max(now, ...spans.map((s) => s.endedAt || now));
  const span = t1 - t0;
  // A fully cached run finishes inside one clock tick, so every span carries
  // the same timestamp and there is no timeline to draw. Saying so beats
  // rendering a row of identical stubs that imply durations nobody measured.
  const noTimeline = span < 0.01;
  const window = Math.max(span, 0.001);
  const active = spans.find((s) => s.id === selected) ?? null;

  return (
    <div className="trace">
      <div className="trace-tree">
        <div className="trace-toolbar">
          <span className="trace-title mono">TRACE</span>
          <span className="trace-sum mono">
            {sums.calls} calls
            {sums.cached > 0 && <em> · {sums.cached} cached</em>}
            {!noTimeline && <em> · {fmtDuration(span)}</em>}
            <em> · {fmtCost(sums.cost)}</em>
          </span>
        </div>

        {noTimeline && (
          <div className="trace-note">
            Every call was a cache hit, so this run has no measurable duration.
            Clear the response cache to see real timings.
          </div>
        )}

        <div className="trace-rows">
          {spans.map((span) => (
            <SpanRow
              key={span.id}
              span={span}
              t0={t0}
              window={window}
              noTimeline={noTimeline}
              now={now}
              selected={span.id === selected}
              onSelect={() => setSelected(span.id === selected ? "" : span.id)}
            />
          ))}
        </div>

        {running && (
          <div className="trace-running">
            <span className="spin" aria-hidden="true" />
            working
          </div>
        )}
      </div>

      <Inspector span={active} now={now} />
    </div>
  );
}

function SpanRow({
  span,
  t0,
  window,
  noTimeline,
  now,
  selected,
  onSelect,
}: {
  span: Span;
  t0: number;
  window: number;
  noTimeline: boolean;
  now: number;
  selected: boolean;
  onSelect: () => void;
}) {
  const duration = durationOf(span, now);
  const left = ((span.startedAt - t0) / window) * 100;
  const width = Math.max((duration / window) * 100, 0.8);
  const isStage = span.kind === "stage";

  return (
    <button
      type="button"
      className={`span-row ${isStage ? "stage" : "child"} ${span.status} ${
        selected ? "on" : ""
      }`}
      onClick={onSelect}
      aria-pressed={selected}
    >
      <span className="span-label">
        <span className={`span-dot ${span.status}`} aria-hidden="true" />
        <span className="span-name">
          {span.kind === "agent" ? departmentName(span.name.toLowerCase()) : span.name}
        </span>
        {span.viaFallback && <span className="span-flag">fallback</span>}
        {span.status === "cached" && <span className="span-flag cached">cached</span>}
      </span>

      <span className="span-track">
        {!noTimeline && (
          <span
            className={`span-bar ${span.status}`}
            style={{ left: `${left}%`, width: `${width}%` }}
          />
        )}
      </span>

      <span className="span-time mono">
        {noTimeline ? "cached" : fmtDuration(duration)}
      </span>
    </button>
  );
}

/* The inspector.
 *
 * Selecting a span shows what it actually cost, the way a trace tool does.
 * Nothing here is computed for display: every figure comes off the event.
 */
function Inspector({ span, now }: { span: Span | null; now: number }) {
  if (!span) {
    return (
      <aside className="inspector empty">
        <p className="hint">Select a span to inspect it.</p>
      </aside>
    );
  }

  const duration = durationOf(span, now);

  return (
    <aside className="inspector">
      <header>
        <span className={`span-dot ${span.status}`} aria-hidden="true" />
        <strong>
          {span.kind === "agent" ? departmentName(span.name.toLowerCase()) : span.name}
        </strong>
        <span className={`span-status ${span.status}`}>{span.status}</span>
      </header>

      <dl className="kv">
        <div>
          <dt>Duration</dt>
          <dd className="mono">{fmtDuration(duration)}</dd>
        </div>
        {span.attempt > 1 && (
          <div>
            <dt>Attempt</dt>
            <dd className="mono">{span.attempt}</dd>
          </div>
        )}
      </dl>

      {span.detail && <p className="inspector-detail mono">{span.detail}</p>}
    </aside>
  );
}
