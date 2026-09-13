/* The run reducer.
 *
 * This is the part of the frontend with rules in it, and the rules are about
 * meaning rather than about pixels: which events actually say a scene changed,
 * and what a scene's state may escalate to.
 *
 * Run with `npm test` (node --test, no framework).
 */

import assert from "node:assert/strict";
import { test } from "node:test";
import type { RunEvent } from "./api";
import { elapsed, emptyRun, reduce, sortScenes, type RunState } from "./runState";

const event = (
  kind: string,
  message = "",
  data: Record<string, unknown> = {},
  ts = 0,
): RunEvent => ({ kind, message, data, ts });

const fold = (events: RunEvent[]): RunState =>
  events.reduce((state, e) => reduce(state, e), emptyRun());

// --- what counts as a change ---------------------------------------------

test("a semantic finding marks its scene changed", () => {
  const state = fold([
    event("change.detected", "", { scene_number: "7", semantic: true, departments: ["props"] }),
  ]);
  assert.equal(state.scenes["7"], "changed");
  assert.equal(state.findings, 1);
});

test("a mechanical span marks its scene changed but is not a finding", () => {
  const state = fold([event("change.detected", "", { scene_number: "7" })]);
  assert.equal(state.scenes["7"], "changed");
  assert.equal(state.findings, 0, "a diff hunk is not a finding");
});

test("a moved scene is not a changed scene", () => {
  // The schedule consumer reports "scene 6 moved" for a scene whose text is
  // identical. Marking it changed paints the sweep blue and destroys the only
  // signal it carries.
  const state = fold([
    event("scene.parsed", "", { number: "6" }),
    event("change.detected", "scene 6 moved", { scene_number: "6", schedule: true }),
  ]);
  assert.equal(state.scenes["6"], "seen");
});

test("a clearance flag does not mark its scene changed", () => {
  const state = fold([
    event("scene.parsed", "", { number: "5A" }),
    event("change.detected", "", { scene_number: "5A", clearance: true }),
  ]);
  assert.equal(state.scenes["5A"], "seen");
});

test("findings are counted once, not once per department", () => {
  const state = fold([
    event("change.detected", "", {
      scene_number: "5A",
      semantic: true,
      departments: ["locations", "transport", "schedule", "cast"],
    }),
  ]);
  assert.equal(state.findings, 1);
  assert.equal(state.lanes.locations.findings, 1);
  assert.equal(state.lanes.cast.findings, 1);
});

// --- scene state escalates, never regresses -------------------------------

test("a later parse event does not undo a change", () => {
  // Extraction and reasoning run concurrently, so events do not arrive in a
  // guaranteed order.
  const state = fold([
    event("change.detected", "", { scene_number: "7", semantic: true }),
    event("scene.parsed", "", { number: "7" }),
  ]);
  assert.equal(state.scenes["7"], "changed");
});

test("omitted outranks changed", () => {
  const state = fold([
    event("change.detected", "", { scene_number: "5", semantic: true }),
    event("scene.aligned", "", { number: "5", kind: "omitted" }),
  ]);
  assert.equal(state.scenes["5"], "omitted");
});

test("inserted outranks changed", () => {
  const state = fold([
    event("change.detected", "", { scene_number: "5A", semantic: true }),
    event("scene.aligned", "", { number: "5A", kind: "inserted" }),
  ]);
  assert.equal(state.scenes["5A"], "inserted");
});

test("a scene appears in the order once", () => {
  const state = fold([
    event("scene.parsed", "", { number: "7" }),
    event("change.detected", "", { scene_number: "7", semantic: true }),
    event("scene.aligned", "", { number: "7", kind: "matched" }),
  ]);
  assert.deepEqual(state.order, ["7"]);
});

// --- counters -------------------------------------------------------------

test("a cached call is not counted as a spend", () => {
  const state = fold([
    event("model.call.finished", "", { cached: true }),
    event("model.call.finished", "", {}),
  ]);
  assert.equal(state.calls, 1);
  assert.equal(state.cached, 1);
});

test("the token total is the run total, not a sum of deltas", () => {
  // The client reports a running total, so adding them would multiply.
  const state = fold([
    event("tokens.spent", "", { run_total: 100 }),
    event("tokens.spent", "", { run_total: 250 }),
  ]);
  assert.equal(state.tokens, 250);
});

test("fallbacks are counted", () => {
  const state = fold([event("model.fallback", "", { from_model: "sonnet-5" })]);
  assert.equal(state.fallbacks, 1);
});

// --- lanes ----------------------------------------------------------------

test("an agent lane moves from working to done", () => {
  const state = fold([
    event("agent.started", "", { department: "props" }),
    event("agent.finished", "", { department: "props", notes: 3, urgent: 1 }),
  ]);
  assert.equal(state.lanes.props.status, "done");
  assert.equal(state.lanes.props.notes, 3);
  assert.equal(state.lanes.props.urgent, 1);
});

test("a run that fails records why", () => {
  const state = fold([event("run.failed", "ThrottlingException: slow down")]);
  assert.equal(state.status, "failed");
  assert.match(state.error, /Throttling/);
});

// --- ordering -------------------------------------------------------------

test("scenes sort the way a script supervisor reads them", () => {
  assert.deepEqual(sortScenes(["34A", "7", "35", "34", "3"]), [
    "3",
    "7",
    "34",
    "34A",
    "35",
  ]);
});

test("the feed is capped so a long run does not grow without bound", () => {
  const events = Array.from({ length: 400 }, (_, i) =>
    event("scene.parsed", `scene ${i}`, { number: String(i) }),
  );
  assert.ok(fold(events).feed.length <= 120);
});

test("elapsed reads from the newest event", () => {
  const state = fold([
    event("run.started", "", {}, 1000),
    event("scene.parsed", "", { number: "1" }, 1002.5),
  ]);
  assert.equal(elapsed(state), "2.5s");
});

test("elapsed is empty before a run starts", () => {
  assert.equal(elapsed(emptyRun()), "");
});
