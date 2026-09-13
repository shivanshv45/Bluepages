/* Turning the event stream into something to look at.
 *
 * The events are the pipeline's own (Layer 3.6), not a shape invented for the
 * browser. This module folds them into the state the live view renders: which
 * scenes have been touched, which department agents are working, what the run
 * has spent.
 *
 * Kept out of the components because it is the part with rules in it, and
 * because a reducer is testable in a way a component full of setState is not.
 */

import type { RunEvent } from "./api";

export type SceneState = "seen" | "changed" | "inserted" | "omitted";

export interface LaneState {
  department: string;
  status: "idle" | "working" | "done";
  findings: number;
  notes: number;
  urgent: number;
  model: string;
  viaFallback: boolean;
}

export interface RunState {
  status: "idle" | "running" | "finished" | "failed";
  stage: string;
  scenes: Record<string, SceneState>;
  order: string[];
  lanes: Record<string, LaneState>;
  feed: RunEvent[];
  findings: number;
  elements: number;
  calls: number;
  tokens: number;
  fallbacks: number;
  cached: number;
  error: string;
  startedAt: number;
}

export const emptyRun = (): RunState => ({
  status: "idle",
  stage: "",
  scenes: {},
  order: [],
  lanes: {},
  feed: [],
  findings: 0,
  elements: 0,
  calls: 0,
  tokens: 0,
  fallbacks: 0,
  cached: 0,
  error: "",
  startedAt: -1,
});

/* How many feed lines to keep.
 *
 * A feature-length run emits thousands. Keeping all of them makes the tab
 * slower the longer it is open, which is the opposite of what a run view is
 * for. Newest first, so the cap drops the oldest.
 */
const FEED_LIMIT = 120;

const num = (value: unknown): number =>
  typeof value === "number" && Number.isFinite(value) ? value : 0;

const str = (value: unknown): string => (typeof value === "string" ? value : "");

function touchScene(
  state: RunState,
  number: string,
  next: SceneState,
): void {
  if (!number) return;
  const current = state.scenes[number];
  // A scene's state only ever escalates. A "seen" arriving after "changed"
  // must not overwrite it, and the events do not arrive in a guaranteed order
  // because extraction and reasoning run concurrently.
  const rank: Record<SceneState, number> = {
    seen: 0,
    changed: 1,
    inserted: 2,
    omitted: 2,
  };
  if (current && rank[current] >= rank[next]) return;
  if (!current) state.order.push(number);
  state.scenes[number] = next;
}

function lane(state: RunState, department: string): LaneState {
  if (!state.lanes[department]) {
    state.lanes[department] = {
      department,
      status: "idle",
      findings: 0,
      notes: 0,
      urgent: 0,
      model: "",
      viaFallback: false,
    };
  }
  return state.lanes[department];
}

/* Fold one event into the run state.
 *
 * Returns a new object so React re-renders, but mutates the copy: the maps get
 * large on a feature-length run and rebuilding them per event is wasted work
 * on the one screen that is meant to feel fast.
 */
export function reduce(previous: RunState, event: RunEvent): RunState {
  const state: RunState = {
    ...previous,
    scenes: { ...previous.scenes },
    order: [...previous.order],
    lanes: { ...previous.lanes },
    feed: [event, ...previous.feed].slice(0, FEED_LIMIT),
  };
  const data = event.data ?? {};

  switch (event.kind) {
    case "run.started":
      if (state.startedAt < 0 || previous.status === "idle") {
        state.startedAt = event.ts;
        state.status = "running";
      }
      state.stage = "parsing";
      break;

    case "scene.parsed":
      touchScene(state, str(data.number) || str(data.scene_number), "seen");
      state.stage = "parsing";
      break;

    case "parse.finished":
      state.stage = "aligning";
      break;

    case "scene.aligned": {
      const kind = str(data.kind);
      const number = str(data.number) || str(data.scene_number);
      if (kind === "inserted") touchScene(state, number, "inserted");
      else if (kind === "omitted") touchScene(state, number, "omitted");
      else touchScene(state, number, "seen");
      break;
    }

    case "change.detected": {
      const number = str(data.scene_number);
      // Four things emit this kind, and only two of them mean the scene's
      // content changed.
      //
      //   semantic: a finding from the reasoner. The real thing.
      //   a mechanical span: a diff hunk inside a scene. Also real.
      //   schedule: includes "scene 6 moved", which is a scene whose text is
      //     identical and whose position shifted. Marking it changed paints
      //     the whole sweep blue and destroys the one signal it carries.
      //   clearance: names the scene a brand appears in, not a change to it.
      if (data.semantic) {
        state.findings += 1;
        touchScene(state, number, "changed");
        for (const department of asArray(data.departments)) {
          lane(state, department).findings += 1;
        }
        state.stage = "reasoning";
      } else if (data.schedule || data.clearance) {
        // Counted in their own consumers; the scene itself is untouched.
      } else if (number) {
        touchScene(state, number, "changed");
      }
      break;
    }

    case "element.found":
      state.elements += 1;
      state.stage = "extracting";
      break;

    case "model.call.finished":
      if (data.cached) state.cached += 1;
      else state.calls += 1;
      break;

    case "tokens.spent":
      state.tokens = num(data.run_total) || state.tokens + num(data.input_tokens) + num(data.output_tokens);
      break;

    case "model.fallback":
      state.fallbacks += 1;
      break;

    case "agent.started": {
      const entry = lane(state, str(data.department));
      state.lanes[entry.department] = { ...entry, status: "working" };
      state.stage = "fanning out";
      break;
    }

    case "agent.finished": {
      const entry = lane(state, str(data.department));
      state.lanes[entry.department] = {
        ...entry,
        status: "done",
        notes: num(data.notes),
        urgent: num(data.urgent),
        model: str(data.model),
        viaFallback: Boolean(data.via_fallback),
      };
      break;
    }

    case "run.finished":
      // The pipeline emits this per nested run as well as at the end, so the
      // status is settled by the stream closing rather than by this event.
      state.stage = "finishing";
      break;

    case "run.failed":
      state.status = "failed";
      state.error = event.message;
      break;
  }

  return state;
}

function asArray(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((v): v is string => typeof v === "string") : [];
}

/* Scenes in the order a script supervisor reads them: 3, 7, 34, 34A, 35. */
export function sortScenes(numbers: string[]): string[] {
  return [...numbers].sort((a, b) => {
    const na = parseInt(a.replace(/\D/g, ""), 10);
    const nb = parseInt(b.replace(/\D/g, ""), 10);
    if (Number.isNaN(na) || Number.isNaN(nb) || na === nb) return a.localeCompare(b);
    return na - nb;
  });
}

export function elapsed(state: RunState): string {
  if (state.status === "idle") return "";
  const last = state.feed[0]?.ts ?? state.startedAt;
  const seconds = Math.max(0, last - state.startedAt);
  return seconds < 60
    ? `${seconds.toFixed(1)}s`
    : `${Math.floor(seconds / 60)}m ${Math.round(seconds % 60)}s`;
}
