/* The agent graph.
 *
 * The run's real topology: the pipeline stages feed the reasoner, the reasoner
 * emits findings, and each finding routes to the department agents that Layer
 * 3.4 decided it belongs to. Those edges come off `change.detected`, which
 * already carries `departments[]`, so nothing here is invented for the
 * picture. An edge exists because a finding really was routed down it.
 *
 * The layout is force-directed rather than a fixed diagram because the shape
 * depends on the revision: a draft that only touches props should visibly
 * pull toward props.
 */

import type { RunEvent } from "./api";

export type NodeKind = "core" | "stage" | "department" | "scene" | "substep";
export type NodeState = "idle" | "active" | "done" | "urgent";

export interface GraphNode {
  id: string;
  label: string;
  kind: NodeKind;
  state: NodeState;
  /* Position and velocity, integrated by the simulation. */
  x: number;
  y: number;
  vx: number;
  vy: number;
  /* Fixed nodes anchor the layout so it does not drift or spin. */
  fixed: boolean;
  radius: number;
  hits: number;
  detail: string;
  /* Stable vertical lane, assigned on arrival, so layout does not reshuffle. */
  lane: number;
  /* Wall-clock of the last traffic, used to fade the pulse out. */
  litAt: number;
  /* Substeps only: the department node they orbit while working, and the
     step text shown at the satellite. They exist only while their parent is
     active, so a node clicked open shows real, current work rather than a
     static "1 note" summary. */
  parent?: string;
  step?: string;
  /* The full sequence of things this node has done, oldest first. Kept on
     the node itself so NodeDetail can show "what happened inside" without a
     second data source. */
  log: { ts: number; text: string }[];
}

export interface GraphEdge {
  from: string;
  to: string;
  count: number;
  litAt: number;
}

export interface GraphState {
  nodes: Record<string, GraphNode>;
  edges: Record<string, GraphEdge>;
  order: string[];
}

export const emptyGraph = (): GraphState => ({ nodes: {}, edges: {}, order: [] });

/* The spine every run has, laid out left to right. These are fixed so the
   graph has a readable backbone and only the departments float. */
const SPINE: { id: string; label: string; kind: NodeKind; x: number; y: number }[] = [
  { id: "draft", label: "Draft 2", kind: "core", x: 0.08, y: 0.5 },
  { id: "parse", label: "Parse", kind: "stage", x: 0.22, y: 0.5 },
  { id: "align", label: "Align", kind: "stage", x: 0.35, y: 0.5 },
  { id: "reason", label: "Reason", kind: "stage", x: 0.5, y: 0.5 },
];

const SPINE_EDGES: [string, string][] = [
  ["draft", "parse"],
  ["parse", "align"],
  ["align", "reason"],
];

export function seedGraph(width: number, height: number): GraphState {
  const state = emptyGraph();
  for (const spec of SPINE) {
    state.nodes[spec.id] = {
      id: spec.id,
      label: spec.label,
      kind: spec.kind,
      state: "idle",
      x: spec.x * width,
      y: spec.y * height,
      vx: 0,
      vy: 0,
      fixed: true,
      radius: spec.kind === "core" ? 26 : 21,
      hits: 0,
      detail: "",
      lane: 0,
      litAt: 0,
      log: [],
    };
    state.order.push(spec.id);
  }
  for (const [from, to] of SPINE_EDGES) {
    state.edges[`${from}>${to}`] = { from, to, count: 0, litAt: 0 };
  }
  return state;
}

function ensureDepartment(
  state: GraphState,
  key: string,
  width: number,
  height: number,
  ts: number,
): GraphNode {
  const id = `dept:${key}`;
  const existing = state.nodes[id];
  if (existing) return existing;

  // New departments enter on the right, spread vertically, then the
  // simulation settles them.
  const seen = state.order.filter((n) => n.startsWith("dept:")).length;
  const node: GraphNode = {
    id,
    label: key,
    kind: "department",
    state: "idle",
    x: width * (0.68 + (seen % 2) * 0.14),
    y: height * (0.1 + seen * 0.1),
    vx: 0,
    vy: 0,
    fixed: false,
    radius: 18,
    hits: 0,
    detail: "",
    lane: seen,
    litAt: ts,
    log: [],
  };
  state.nodes[id] = node;
  state.order.push(id);
  state.edges[`reason>${id}`] = { from: "reason", to: id, count: 0, litAt: 0 };
  return node;
}

function lightEdge(state: GraphState, key: string, ts: number): void {
  const edge = state.edges[key];
  if (!edge) return;
  edge.count += 1;
  edge.litAt = ts;
}

/* Log a line of what a node actually did, capped so a long run does not
   grow the state without bound. NodeDetail reads this to show live work. */
function logTo(node: GraphNode, ts: number, text: string): void {
  node.log = [...node.log, { ts, text }].slice(-12);
}

/* A satellite step orbiting a department while it works: "checking the
   brand database", "drafting the brief". Placed on a small ring around the
   parent and removed once the parent finishes, so the graph shows depth
   happening without permanently multiplying its node count. */
function openSubstep(
  state: GraphState,
  parentId: string,
  key: string,
  text: string,
  ts: number,
): void {
  const id = `sub:${parentId}:${key}`;
  const parent = state.nodes[parentId];
  if (!parent) return;

  const existing = state.nodes[id];
  if (existing) {
    existing.step = text;
    existing.state = "active";
    existing.litAt = ts;
    logTo(existing, ts, text);
    return;
  }

  const siblingCount = state.order.filter(
    (n) => n.startsWith(`sub:${parentId}:`) && state.nodes[n]?.state !== "done",
  ).length;
  const angle = (siblingCount * 137.5 * Math.PI) / 180; // golden-angle spread, never overlaps neatly
  const node: GraphNode = {
    id,
    label: text,
    kind: "substep",
    state: "active",
    x: parent.x + Math.cos(angle) * 46,
    y: parent.y + Math.sin(angle) * 46,
    vx: 0,
    vy: 0,
    fixed: false,
    radius: 7,
    hits: 0,
    detail: "",
    lane: 0,
    litAt: ts,
    parent: parentId,
    step: text,
    log: [{ ts, text }],
  };
  state.nodes[id] = node;
  state.order.push(id);
  state.edges[`${parentId}>${id}`] = { from: parentId, to: id, count: 1, litAt: ts };
}

/* Close every open substep on a department, once it finishes. Their history
   already landed in the parent's own log (closeSubsteps copies it across),
   so nothing is lost when the satellites disappear. */
function closeSubsteps(state: GraphState, parentId: string): void {
  const parent = state.nodes[parentId];
  const prefix = `sub:${parentId}:`;
  const toRemove: string[] = [];
  for (const id of state.order) {
    if (!id.startsWith(prefix)) continue;
    const sub = state.nodes[id];
    if (parent) for (const line of sub.log) logTo(parent, line.ts, line.text);
    const edgeKey = `${parentId}>${id}`;
    delete state.edges[edgeKey];
    toRemove.push(id);
  }
  if (!toRemove.length) return;
  state.order = state.order.filter((id) => !toRemove.includes(id));
  for (const id of toRemove) delete state.nodes[id];
}

/* Fold one run event into the graph. Mutates a copy, like the other reducers,
   because these maps get touched on every event. */
export function reduceGraph(
  previous: GraphState,
  event: RunEvent,
  width: number,
  height: number,
): GraphState {
  const state: GraphState = {
    nodes: { ...previous.nodes },
    edges: { ...previous.edges },
    order: [...previous.order],
  };
  for (const id of state.order) state.nodes[id] = { ...state.nodes[id] };
  for (const key of Object.keys(state.edges)) state.edges[key] = { ...state.edges[key] };

  const data = event.data ?? {};
  const ts = event.ts;

  const touch = (id: string, next: NodeState) => {
    const node = state.nodes[id];
    if (!node) return;
    node.state = next;
    node.litAt = ts;
  };

  switch (event.kind) {
    case "run.started":
      touch("draft", "done");
      touch("parse", "active");
      lightEdge(state, "draft>parse", ts);
      break;

    case "scene.parsed": {
      const node = state.nodes.parse;
      if (node) {
        node.hits += 1;
        // The count alone reads as a handful of pages out of a much longer
        // script. The highest scene number actually parsed says the same
        // thing the decision cards do: this run reaches as far as scene 16.
        const number = String(data.number ?? "");
        const seen = parseInt(number.replace(/\D/g, ""), 10);
        const highest = parseInt((node.detail.match(/\d+/) ?? ["0"])[0], 10);
        if (!Number.isNaN(seen) && seen > highest) {
          node.detail = `through scene ${seen}`;
        }
        node.litAt = ts;
      }
      lightEdge(state, "draft>parse", ts);
      break;
    }

    case "parse.finished":
      touch("parse", "done");
      touch("align", "active");
      lightEdge(state, "parse>align", ts);
      break;

    case "scene.aligned":
      lightEdge(state, "parse>align", ts);
      break;

    case "change.detected": {
      // Only a semantic finding is a real routing decision. The other three
      // emitters of this kind are counted elsewhere, per runState.ts.
      if (!data.semantic) break;
      touch("align", "done");
      touch("reason", "active");
      lightEdge(state, "align>reason", ts);

      const node = state.nodes.reason;
      if (node) {
        node.hits += 1;
        node.detail = `${node.hits} findings`;
      }

      const departments = Array.isArray(data.departments) ? data.departments : [];
      for (const raw of departments) {
        if (typeof raw !== "string") continue;
        const dept = ensureDepartment(state, raw, width, height, ts);
        dept.hits += 1;
        dept.detail = `${dept.hits} routed`;
        dept.state = dept.state === "urgent" ? "urgent" : "active";
        dept.litAt = ts;
        lightEdge(state, `reason>dept:${raw}`, ts);
      }
      break;
    }

    case "agent.started": {
      const key = String(data.department ?? "");
      if (!key) break;
      const dept = ensureDepartment(state, key, width, height, ts);
      dept.state = "active";
      dept.litAt = ts;
      logTo(dept, ts, String(data.detail ?? "starting"));
      lightEdge(state, `reason>dept:${key}`, ts);
      break;
    }

    // A department does more than one thing while it works: it checks a
    // database, drafts a brief, sources a purchase. Each shows as a small
    // satellite node orbiting the department, so opening the department mid
    // run shows the actual step in progress rather than a static label.
    case "agent.step": {
      const key = String(data.department ?? "");
      const step = String(data.step ?? "");
      if (!key || !step) break;
      const parentId = `dept:${key}`;
      if (!state.nodes[parentId]) ensureDepartment(state, key, width, height, ts);
      openSubstep(state, parentId, String(data.step_key ?? step), step, ts);
      break;
    }

    case "agent.finished": {
      const key = String(data.department ?? "");
      if (!key) break;
      const dept = ensureDepartment(state, key, width, height, ts);
      const urgent = Number(data.urgent ?? 0);
      dept.state = urgent > 0 ? "urgent" : "done";
      dept.detail =
        `${Number(data.notes ?? 0)} note${Number(data.notes ?? 0) === 1 ? "" : "s"}` +
        (urgent > 0 ? `, ${urgent} urgent` : "");
      dept.litAt = ts;
      logTo(dept, ts, String(data.detail ?? "finished"));
      closeSubsteps(state, `dept:${key}`);
      break;
    }

    case "run.finished":
    case "run.failed": {
      for (const id of state.order) {
        const node = state.nodes[id];
        if (node.state === "active") node.state = "done";
      }
      break;
    }
  }

  return state;
}

/* Place the departments.
 *
 * Deterministic, not a force simulation. The node count is small and the fan
 * shape is known, so solving it directly is both simpler and the only way to
 * guarantee labels never collide, which a spring layout cannot promise.
 *
 * Departments sit on an arc to the right of the reasoner, evenly divided over
 * the available height. Nodes ease toward their slot so arrivals still animate.
 */
export function step(state: GraphState, width: number, height: number): void {
  const departments = state.order
    .filter((id) => id.startsWith("dept:"))
    .map((id) => state.nodes[id]);
  if (!departments.length) return;

  // Every label plus its detail line needs this much vertical room.
  const ROW = 58;
  const usable = height - 40;
  const spacing = Math.max(ROW, usable / departments.length);
  const top = height / 2 - ((departments.length - 1) * spacing) / 2;

  departments.forEach((node, i) => {
    // Two columns when there are more than five, so a full fan-out still fits
    // without shrinking the spacing below the label height.
    const columns = departments.length > 5 ? 2 : 1;
    const column = i % columns;
    const row = Math.floor(i / columns);
    const rows = Math.ceil(departments.length / columns);
    const stagger = columns > 1 && column === 1 ? 0.5 : 0;
    // The staggered column reaches half a row lower, so the block is centred
    // over rows + 0.5. Without this the last node sits below the canvas and
    // its label is clipped.
    const extent = rows - 1 + (columns > 1 ? 0.5 : 0);
    const columnSpacing = Math.max(ROW, usable / (extent + 1));
    const columnTop = height / 2 - (extent * columnSpacing) / 2;

    const targetX = width * (columns === 1 ? 0.78 : 0.66 + column * 0.24);
    const targetY =
      columns === 1
        ? top + i * spacing
        : columnTop + (row + stagger) * columnSpacing;

    node.vx = 0;
    node.vy = 0;
    // Ease in, so a department appearing mid-run slides to its slot. Slow
    // enough to be watched: this is the surface that shows work arriving.
    node.x += (targetX - node.x) * 0.055;
    node.y += (targetY - node.y) * 0.055;
  });

  // Substeps orbit wherever their parent has settled, not a fixed point, so
  // they stay attached as the department eases into its slot.
  for (const id of state.order) {
    if (!id.startsWith("sub:")) continue;
    const node = state.nodes[id];
    const parent = node.parent ? state.nodes[node.parent] : undefined;
    if (!parent) continue;
    const dx = node.x - parent.x;
    const dy = node.y - parent.y;
    const dist = Math.hypot(dx, dy) || 1;
    const targetX = parent.x + (dx / dist) * 46;
    const targetY = parent.y + (dy / dist) * 46;
    node.x += (targetX - node.x) * 0.12;
    node.y += (targetY - node.y) * 0.12;
  }
}

/* Reposition the fixed spine when the canvas resizes. */
export function relayout(state: GraphState, width: number, height: number): void {
  for (const spec of SPINE) {
    const node = state.nodes[spec.id];
    if (!node) continue;
    node.x = spec.x * width;
    node.y = spec.y * height;
  }
}

/* Rebuild the graph from the last run.
 *
 * The live graph is fed by the event stream, so a reload would leave it empty
 * while the rest of the screen shows a full run. That contradicts the rule in
 * PLAN.md that idle is "here is what happened last run", never a blank panel.
 *
 * The database keeps what the graph needs: every change carries the
 * departments it routed to, and every report carries the model that wrote it.
 * The result is the same shape the stream would have built, marked done.
 */
export function graphFromProduction(
  changes: { departments: string[] }[],
  reports: { department: string; notes: { urgent: number }[] }[],
  sceneCount: number,
  width: number,
  height: number,
): GraphState {
  const state = seedGraph(width, height);

  for (const id of ["draft", "parse", "align", "reason"]) {
    const node = state.nodes[id];
    if (node) node.state = "done";
  }
  state.nodes.parse.hits = sceneCount;
  state.nodes.parse.detail = `${sceneCount} scenes`;
  state.nodes.reason.hits = changes.length;
  state.nodes.reason.detail = `${changes.length} findings`;
  for (const key of ["draft>parse", "parse>align", "align>reason"]) {
    const edge = state.edges[key];
    if (edge) edge.count = 1;
  }

  for (const change of changes) {
    for (const key of change.departments ?? []) {
      const dept = ensureDepartment(state, key, width, height, 0);
      dept.hits += 1;
      dept.detail = `${dept.hits} routed`;
      const edge = state.edges[`reason>dept:${key}`];
      if (edge) edge.count += 1;
    }
  }

  // A report's own note count is more accurate than the routed count, and it
  // is what the live graph shows once an agent finishes.
  for (const report of reports) {
    const dept = ensureDepartment(state, report.department, width, height, 0);
    const urgent = report.notes.filter((note) => note.urgent).length;
    dept.state = urgent > 0 ? "urgent" : "done";
    dept.detail =
      `${report.notes.length} note${report.notes.length === 1 ? "" : "s"}` +
      (urgent > 0 ? `, ${urgent} urgent` : "");
  }

  // step() eases toward its targets, so settle it rather than showing the
  // first frame of an animation that will never run.
  for (let i = 0; i < 40; i++) step(state, width, height);
  return state;
}
