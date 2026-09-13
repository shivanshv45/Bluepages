/* The execution trace.
 *
 * The run already emits everything a trace needs: model.call.started carries
 * the label, model and attempt, model.call.finished carries latency, tokens
 * and whether it was a cache hit. This module folds those into spans with
 * parents, so the run reads as a tree of work rather than a list of lines.
 *
 * The nesting is derived from the label the pipeline already passes:
 * "scene 3 elements" is extraction, "scene 3" is reasoning, and a department
 * title is a department agent. No new events were added to the pipeline.
 *
 * The frontend never names a model. `data.model` is the tier the backend's
 * ModelClient already reports (judgment, bulk, groq, gemini), never a real
 * model id, so it is the only model field this module reads.
 */

import type { RunEvent } from "./api";

export type SpanKind = "stage" | "model" | "agent" | "scene";
export type SpanStatus = "running" | "ok" | "cached" | "failed";

export interface Span {
  id: string;
  parent: string | null;
  kind: SpanKind;
  name: string;
  detail: string;
  status: SpanStatus;
  model: string;
  provider: string;
  startedAt: number;
  endedAt: number;
  inputTokens: number;
  outputTokens: number;
  viaFallback: boolean;
  attempt: number;
  depth: number;
}

/* Per-million-token rates by tier, matching what ModelClient reports as
   role.name. Bedrock pricing for judgment and bulk; published rates for the
   fallback providers. */
const RATES: Record<string, { input: number; output: number }> = {
  judgment: { input: 3, output: 15 },
  bulk: { input: 0.8, output: 4 },
  groq: { input: 0.59, output: 0.79 },
  gemini: { input: 0.3, output: 2.5 },
};

/* A cached call is free, whatever tier it would have run on. */
export function isFree(span: Span): boolean {
  return span.status === "cached";
}

export function costOf(span: Span): number {
  if (span.status === "cached") return 0;
  const rate = RATES[span.model.toLowerCase()] ?? { input: 1, output: 5 };
  return (
    (span.inputTokens / 1_000_000) * rate.input +
    (span.outputTokens / 1_000_000) * rate.output
  );
}

/* The five stages a run moves through, in order. Model calls attach to
   whichever stage was open when they started. */
const STAGES: { id: string; name: string; detail: string }[] = [
  { id: "stage:parse", name: "Parse", detail: "reading both drafts" },
  { id: "stage:align", name: "Align", detail: "matching scenes across drafts" },
  { id: "stage:extract", name: "Extract", detail: "elements per scene" },
  { id: "stage:reason", name: "Reason", detail: "what the changes mean" },
  { id: "stage:fanout", name: "Fan out", detail: "department agents" },
];

export interface TraceState {
  spans: Span[];
  index: Record<string, number>;
  openStage: string | null;
  /* Model calls are matched to their finish event by label, because the
     stream carries no span id of its own. */
  openCalls: Record<string, string>;
  seq: number;
}

export const emptyTrace = (): TraceState => ({
  spans: [],
  index: {},
  openStage: null,
  openCalls: {},
  seq: 0,
});

function push(state: TraceState, span: Span): void {
  state.index[span.id] = state.spans.length;
  state.spans.push(span);
}

function get(state: TraceState, id: string): Span | null {
  const at = state.index[id];
  return at === undefined ? null : state.spans[at];
}

function openStage(state: TraceState, id: string, ts: number): void {
  if (state.openStage === id) return;

  // Close the previous stage so its duration is real rather than open-ended.
  if (state.openStage) {
    const previous = get(state, state.openStage);
    if (previous && !previous.endedAt) {
      previous.endedAt = ts;
      previous.status = "ok";
    }
  }

  if (!get(state, id)) {
    const spec = STAGES.find((s) => s.id === id);
    push(state, {
      id,
      parent: null,
      kind: "stage",
      name: spec?.name ?? id,
      detail: spec?.detail ?? "",
      status: "running",
      model: "",
      provider: "",
      startedAt: ts,
      endedAt: 0,
      inputTokens: 0,
      outputTokens: 0,
      viaFallback: false,
      attempt: 1,
      depth: 0,
    });
  }
  state.openStage = id;
}

/* A label tells us which stage a call belongs to and what to call it.
   "scene 3 elements" is extraction; "scene 3" is reasoning; anything else is
   a department agent writing its report. */
function classify(label: string): { stage: string; name: string; kind: SpanKind } {
  if (/elements$/i.test(label)) {
    return { stage: "stage:extract", name: label.replace(/\s*elements$/i, ""), kind: "scene" };
  }
  if (/^scene\b/i.test(label)) {
    return { stage: "stage:reason", name: label, kind: "scene" };
  }
  return { stage: "stage:fanout", name: label, kind: "agent" };
}

export function reduceTrace(previous: TraceState, event: RunEvent): TraceState {
  const state: TraceState = {
    spans: previous.spans.map((s) => ({ ...s })),
    index: { ...previous.index },
    openStage: previous.openStage,
    openCalls: { ...previous.openCalls },
    seq: previous.seq,
  };
  const data = event.data ?? {};
  const ts = event.ts;
  const label = typeof data.label === "string" ? data.label : "";

  switch (event.kind) {
    case "run.started":
      openStage(state, "stage:parse", ts);
      break;

    case "parse.finished":
      openStage(state, "stage:align", ts);
      break;

    case "model.call.started": {
      const { stage, name, kind } = classify(label);
      openStage(state, stage, ts);
      const id = `call:${state.seq++}`;
      state.openCalls[label] = id;
      push(state, {
        id,
        parent: stage,
        kind,
        name: name || String(data.model ?? "model call"),
        // No model id here: that is the real Bedrock identifier and it never
        // reaches the browser. The tier alone (judgment/bulk) is shown via
        // span.model on the row itself.
        detail: "",
        status: "running",
        model: String(data.model ?? ""),
        provider: String(data.provider ?? ""),
        startedAt: ts,
        endedAt: 0,
        inputTokens: 0,
        outputTokens: 0,
        viaFallback: Number(data.attempt ?? 1) > 1,
        attempt: Number(data.attempt ?? 1),
        depth: 1,
      });
      break;
    }

    case "model.call.finished": {
      // A cache hit never emits a start, so it arrives as a finished span
      // with no open call to close. That is worth showing: it is the reason
      // a second run costs nothing.
      const openId = state.openCalls[label];
      if (!openId) {
        const { stage, name, kind } = classify(label);
        openStage(state, stage, ts);
        push(state, {
          id: `call:${state.seq++}`,
          parent: stage,
          kind,
          name: name || "cached",
          detail: "",
          status: "cached",
          model: String(data.model ?? ""),
          provider: "",
          startedAt: ts,
          endedAt: ts,
          inputTokens: 0,
          outputTokens: 0,
          viaFallback: false,
          attempt: 1,
          depth: 1,
        });
        break;
      }
      const span = get(state, openId);
      if (span) {
        span.endedAt = ts;
        span.status = data.cached ? "cached" : "ok";
        span.inputTokens = Number(data.input_tokens ?? 0);
        span.outputTokens = Number(data.output_tokens ?? 0);
        span.viaFallback = Boolean(data.via_fallback) || span.viaFallback;
        span.model = String(data.model ?? span.model);
      }
      delete state.openCalls[label];
      break;
    }

    case "model.fallback": {
      // Mark the call that just failed, so the trace shows the drop rather
      // than silently renaming the model.
      for (let i = state.spans.length - 1; i >= 0; i--) {
        const span = state.spans[i];
        if (span.status === "running" && span.model === String(data.from_model ?? "")) {
          span.status = "failed";
          span.endedAt = ts;
          span.detail = "retried on a fallback model";
          break;
        }
      }
      break;
    }

    case "agent.started":
      openStage(state, "stage:fanout", ts);
      break;

    case "run.failed": {
      for (const span of state.spans) {
        if (!span.endedAt) {
          span.endedAt = ts;
          span.status = "failed";
        }
      }
      state.openStage = null;
      break;
    }
  }

  return state;
}

/* Close every open span. Called when the stream closes, so a finished run
   shows real durations rather than spans that look stuck. */
export function sealTrace(state: TraceState, ts: number): TraceState {
  const spans = state.spans.map((span) =>
    span.endedAt
      ? span
      : { ...span, endedAt: ts, status: (span.status === "running" ? "ok" : span.status) as SpanStatus },
  );
  return { ...state, spans, openStage: null };
}

export interface TraceTotals {
  calls: number;
  cached: number;
  tokens: number;
  cost: number;
  slowest: Span | null;
}

export function totals(state: TraceState): TraceTotals {
  let calls = 0;
  let cached = 0;
  let tokens = 0;
  let cost = 0;
  let slowest: Span | null = null;

  for (const span of state.spans) {
    if (span.kind === "stage") continue;
    if (span.status === "cached") cached += 1;
    else calls += 1;
    tokens += span.inputTokens + span.outputTokens;
    cost += costOf(span);
    const duration = span.endedAt - span.startedAt;
    if (span.endedAt && (!slowest || duration > slowest.endedAt - slowest.startedAt)) {
      slowest = span;
    }
  }

  return { calls, cached, tokens, cost, slowest };
}

/* Spans in tree order: each stage followed by its own calls. */
export function ordered(state: TraceState): Span[] {
  const out: Span[] = [];
  for (const stage of state.spans.filter((s) => s.kind === "stage")) {
    out.push(stage);
    for (const child of state.spans) {
      if (child.parent === stage.id) out.push(child);
    }
  }
  return out;
}

export function durationOf(span: Span, now: number): number {
  return (span.endedAt || now) - span.startedAt;
}

export function fmtDuration(seconds: number): string {
  if (seconds < 0.001) return "0ms";
  if (seconds < 1) return `${Math.round(seconds * 1000)}ms`;
  return `${seconds.toFixed(2)}s`;
}

export function fmtCost(cost: number): string {
  if (cost === 0) return "$0.00";
  if (cost < 0.01) return `$${cost.toFixed(4)}`;
  return `$${cost.toFixed(2)}`;
}
