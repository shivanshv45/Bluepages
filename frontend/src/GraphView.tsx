/* The agent graph, drawn.
 *
 * Canvas rather than SVG: the edges carry travelling packets and everything
 * repaints every frame, which is a lot of DOM churn for no benefit. The whole
 * thing is one element and one loop.
 *
 * Nothing here decides anything. It draws the graph the reducer built, and the
 * reducer only ever adds an edge the pipeline actually routed down.
 */

import { useEffect, useRef, useState } from "react";
import { departmentName } from "./components";
import { relayout, step, type GraphNode, type GraphState } from "./graph";

const COLORS = {
  ground: "#0b0d12",
  line: "#222838",
  idle: "#5c6478",
  text: "#8b92a4",
  textBright: "#e8e9ed",
  blue: "#4c7df0",
  green: "#3e9e6b",
  red: "#d4574a",
  amber: "#d9a441",
};

function colorFor(node: GraphNode): string {
  if (node.state === "urgent") return COLORS.red;
  if (node.state === "active") return COLORS.blue;
  if (node.state === "done") return COLORS.green;
  return COLORS.idle;
}

export function GraphView({
  graph,
  now,
  running,
  onResize,
  onSelect,
}: {
  graph: GraphState;
  now: number;
  running: boolean;
  onResize: (width: number, height: number) => void;
  onSelect?: (node: GraphNode) => void;
}) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const wrapRef = useRef<HTMLDivElement>(null);
  const stateRef = useRef(graph);
  const nowRef = useRef(now);
  const runningRef = useRef(running);
  const resizeRef = useRef(onResize);
  const selectRef = useRef(onSelect);
  const [hover, setHover] = useState<GraphNode | null>(null);
  const hoverRef = useRef<string>("");

  stateRef.current = graph;
  nowRef.current = now;
  runningRef.current = running;
  resizeRef.current = onResize;
  selectRef.current = onSelect;

  useEffect(() => {
    const canvas = canvasRef.current;
    const wrap = wrapRef.current;
    if (!canvas || !wrap) return;
    const context = canvas.getContext("2d");
    if (!context) return;

    const reduced = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false;

    let width = 0;
    let height = 0;
    let raf = 0;

    const resize = () => {
      const ratio = window.devicePixelRatio || 1;
      width = wrap.clientWidth;
      height = wrap.clientHeight;
      canvas.width = width * ratio;
      canvas.height = height * ratio;
      canvas.style.width = `${width}px`;
      canvas.style.height = `${height}px`;
      context.setTransform(ratio, 0, 0, ratio, 0, 0);
      relayout(stateRef.current, width, height);
      resizeRef.current(width, height);
    };

    resize();
    const observer = new ResizeObserver(resize);
    observer.observe(wrap);

    const draw = () => {
      const state = stateRef.current;
      const now = nowRef.current;
      const running = runningRef.current;
      if (!reduced) step(state, width, height);

      context.clearRect(0, 0, width, height);

      // Edges first, so nodes sit on top of their own wires.
      for (const edge of Object.values(state.edges)) {
        const from = state.nodes[edge.from];
        const to = state.nodes[edge.to];
        if (!from || !to) continue;

        const age = now - edge.litAt;
        const hot = edge.litAt > 0 && age < 1.2;
        context.beginPath();
        context.moveTo(from.x, from.y);
        // A gentle curve reads as a wire rather than a wireframe.
        const midX = (from.x + to.x) / 2;
        context.quadraticCurveTo(midX, from.y, to.x, to.y);
        context.strokeStyle = hot ? COLORS.blue : COLORS.line;
        context.lineWidth = hot ? 1.6 : edge.count > 0 ? 1.1 : 0.7;
        context.globalAlpha = hot ? 0.9 : edge.count > 0 ? 0.55 : 0.3;
        context.stroke();
        context.globalAlpha = 1;

        // A packet travelling the wire, so routing is visible as movement.
        if (hot && !reduced) {
          const t = Math.min(age / 1.2, 1);
          const px = (1 - t) * (1 - t) * from.x + 2 * (1 - t) * t * midX + t * t * to.x;
          const py = (1 - t) * (1 - t) * from.y + 2 * (1 - t) * t * from.y + t * t * to.y;
          context.beginPath();
          context.arc(px, py, 2.6, 0, Math.PI * 2);
          context.fillStyle = COLORS.blue;
          context.fill();
        }
      }

      for (const id of state.order) {
        const node = state.nodes[id];
        const isSubstep = node.kind === "substep";
        const color = isSubstep ? COLORS.blue : colorFor(node);
        const age = now - node.litAt;
        const pulsing = (node.state === "active" || isSubstep) && running;

        // The wire from a substep to its parent, drawn here rather than in
        // the edge pass above so it always sits beneath the small node.
        if (isSubstep && node.parent) {
          const parent = state.nodes[node.parent];
          if (parent) {
            context.beginPath();
            context.moveTo(parent.x, parent.y);
            context.lineTo(node.x, node.y);
            context.strokeStyle = COLORS.blue;
            context.lineWidth = 1;
            context.globalAlpha = 0.5;
            context.stroke();
            context.globalAlpha = 1;
          }
        }

        // A halo while the node is working.
        if (pulsing && !reduced) {
          const wave = (Math.sin(Date.now() / 260) + 1) / 2;
          context.beginPath();
          context.arc(node.x, node.y, node.radius + 5 + wave * 6, 0, Math.PI * 2);
          context.fillStyle = color;
          context.globalAlpha = 0.1 + wave * 0.1;
          context.fill();
          context.globalAlpha = 1;
        } else if (node.litAt > 0 && age < 0.8) {
          context.beginPath();
          context.arc(node.x, node.y, node.radius + 8 * (1 - age / 0.8), 0, Math.PI * 2);
          context.fillStyle = color;
          context.globalAlpha = 0.18 * (1 - age / 0.8);
          context.fill();
          context.globalAlpha = 1;
        }

        context.beginPath();
        context.arc(node.x, node.y, node.radius, 0, Math.PI * 2);
        context.fillStyle = node.state === "idle" ? "#141822" : "#1a1f2b";
        context.fill();
        context.lineWidth = hoverRef.current === node.id ? 2.2 : 1.4;
        context.strokeStyle = color;
        context.stroke();

        // Findings counter inside the node, which is the number that matters.
        if (node.hits > 0 && !isSubstep) {
          context.fillStyle = COLORS.textBright;
          context.font = "600 12px ui-monospace, SFMono-Regular, Menlo, monospace";
          context.textAlign = "center";
          context.textBaseline = "middle";
          context.fillText(String(node.hits), node.x, node.y);
        }

        // Substep labels sit beside the small node, not below it, so a
        // cluster of them around a department stays legible rather than
        // stacking a wall of text under it.
        if (isSubstep) {
          context.fillStyle = COLORS.textBright;
          context.font = "500 10px 'Inter Tight', system-ui, sans-serif";
          context.textAlign = node.x >= (node.parent ? state.nodes[node.parent]?.x ?? 0 : 0) ? "left" : "right";
          context.textBaseline = "middle";
          const offset = context.textAlign === "left" ? node.radius + 6 : -(node.radius + 6);
          context.fillText(node.step ?? node.label, node.x + offset, node.y);
          continue;
        }

        const label =
          node.kind === "department" ? departmentName(node.label) : node.label;
        context.fillStyle = node.state === "idle" ? COLORS.text : COLORS.textBright;
        context.font = "500 11.5px 'Inter Tight', system-ui, sans-serif";
        context.textAlign = "center";
        context.textBaseline = "top";
        context.fillText(label, node.x, node.y + node.radius + 7);

        if (node.detail) {
          context.fillStyle = COLORS.text;
          context.font = "400 10px ui-monospace, SFMono-Regular, Menlo, monospace";
          context.fillText(node.detail, node.x, node.y + node.radius + 21);
        }
      }

      raf = requestAnimationFrame(draw);
    };

    raf = requestAnimationFrame(draw);

    const onMove = (e: MouseEvent) => {
      const rect = canvas.getBoundingClientRect();
      const x = e.clientX - rect.left;
      const y = e.clientY - rect.top;
      const state = stateRef.current;
      let found: GraphNode | null = null;
      for (const id of state.order) {
        const node = state.nodes[id];
        if (Math.hypot(node.x - x, node.y - y) < node.radius + 4) {
          found = node;
          break;
        }
      }
      hoverRef.current = found?.id ?? "";
      setHover(found);
      canvas.style.cursor = found ? "pointer" : "default";
    };

    // Clicking a node opens its detail. The hit test is the hover one, so
    // what looks clickable is exactly what is.
    const onClick = (e: MouseEvent) => {
      const rect = canvas.getBoundingClientRect();
      const x = e.clientX - rect.left;
      const y = e.clientY - rect.top;
      const state = stateRef.current;
      for (const id of state.order) {
        const node = state.nodes[id];
        if (Math.hypot(node.x - x, node.y - y) < node.radius + 4) {
          selectRef.current?.(node);
          return;
        }
      }
    };

    canvas.addEventListener("mousemove", onMove);
    canvas.addEventListener("click", onClick);

    return () => {
      cancelAnimationFrame(raf);
      observer.disconnect();
      canvas.removeEventListener("mousemove", onMove);
      canvas.removeEventListener("click", onClick);
    };
  }, []);

  return (
    <div className="graph-wrap" ref={wrapRef}>
      <canvas ref={canvasRef} className="graph-canvas" />

      <div className="graph-key">
        <span>
          <i style={{ background: COLORS.idle }} /> waiting
        </span>
        <span>
          <i style={{ background: COLORS.blue }} /> working
        </span>
        <span>
          <i style={{ background: COLORS.green }} /> done
        </span>
        <span>
          <i style={{ background: COLORS.red }} /> urgent
        </span>
      </div>

      {hover && (
        <div className="graph-tip">
          <strong>
            {hover.kind === "department" ? departmentName(hover.label) : hover.label}
          </strong>
          {hover.detail && <span className="mono">{hover.detail}</span>}
        </div>
      )}
    </div>
  );
}
