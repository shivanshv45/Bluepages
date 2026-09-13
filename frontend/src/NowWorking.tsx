/* Now working.
 *
 * The graph shows the whole shape of a run; this shows the one node doing
 * something right now, close up. Picked straight off the same graph state
 * NodeDetail reads, so the log lines here are exactly what a click on the
 * node would show, just surfaced without the click.
 */

import { departmentName } from "./components";
import type { GraphNode, GraphState } from "./graph";

function pickActive(graph: GraphState): GraphNode | null {
  const nodes = graph.order.map((id) => graph.nodes[id]);
  // A substep is the most specific thing happening, so it wins over its
  // parent department. Otherwise any other active node will do.
  const substep = nodes.find((n) => n.kind === "substep" && n.state === "active");
  if (substep) return substep;
  const dept = nodes.find((n) => n.kind === "department" && n.state === "active");
  if (dept) return dept;
  return nodes.find((n) => n.state === "active") ?? null;
}

export function NowWorking({
  graph,
  running,
}: {
  graph: GraphState;
  running: boolean;
}) {
  if (!running) return null;

  const active = pickActive(graph);
  if (!active) {
    return (
      <div className="now-working idle">
        <span className="spin" aria-hidden="true" />
        <span className="now-working-label">Starting up…</span>
      </div>
    );
  }

  // A substep's own log is one line long by design; its parent carries the
  // rest of the department's history, so show both, newest first.
  const parent = active.parent ? graph.nodes[active.parent] : null;
  const lines = (parent ? [...parent.log, ...active.log] : active.log).slice(-5).reverse();
  const label =
    active.kind === "department"
      ? departmentName(active.label)
      : active.kind === "substep" && parent
        ? departmentName(parent.label)
        : active.label;

  return (
    <div className="now-working" aria-live="polite">
      <header>
        <span className="spin" aria-hidden="true" />
        <span className="now-working-label">{label}</span>
        {active.step && <span className="now-working-step mono">{active.step}</span>}
      </header>
      {lines.length > 0 && (
        <ul className="now-working-feed">
          {lines.map((line, i) => (
            <li key={`${line.ts}-${i}`} className={i === 0 ? "current" : ""}>
              {line.text}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
