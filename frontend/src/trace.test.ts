/* The trace reducer, against the event shapes the pipeline actually emits.
 *
 * The events here are copied from what client.py and departments.py send, not
 * invented for the test, so a change to the emitter that breaks the trace
 * shows up as a failure here rather than as an empty screen. `model` is
 * always a tier (judgment, bulk), matching role.name, never a real model id:
 * the frontend must never learn or show which model actually answered.
 */

import { strict as assert } from "node:assert";
import { test } from "node:test";
import {
  emptyTrace,
  fmtCost,
  fmtDuration,
  ordered,
  reduceTrace,
  sealTrace,
  totals,
} from "./trace";
import type { RunEvent } from "./api";

const event = (
  kind: string,
  ts: number,
  data: Record<string, unknown> = {},
): RunEvent => ({ kind, message: "", data, ts });

function replay(events: RunEvent[]) {
  return events.reduce(reduceTrace, emptyTrace());
}

test("model calls nest under the stage that was open", () => {
  const state = replay([
    event("run.started", 0),
    event("parse.finished", 1),
    event("model.call.started", 2, {
      label: "scene 3 elements",
      model: "bulk",
      provider: "bedrock",
      attempt: 1,
    }),
    event("model.call.finished", 3, {
      label: "scene 3 elements",
      model: "bulk",
      input_tokens: 900,
      output_tokens: 120,
    }),
  ]);

  const spans = ordered(state);
  const call = spans.find((s) => s.name === "scene 3");
  assert.ok(call, "the element call is present");
  assert.equal(call.parent, "stage:extract");
  assert.equal(call.status, "ok");
  assert.equal(call.inputTokens, 900);
});

test("a reasoning call and an element call land in different stages", () => {
  const state = replay([
    event("run.started", 0),
    event("model.call.started", 1, { label: "scene 7 elements", model: "bulk" }),
    event("model.call.finished", 2, { label: "scene 7 elements", model: "bulk" }),
    event("model.call.started", 3, { label: "scene 7", model: "judgment" }),
    event("model.call.finished", 4, { label: "scene 7", model: "judgment" }),
  ]);

  const spans = ordered(state);
  const extract = spans.filter((s) => s.parent === "stage:extract");
  const reason = spans.filter((s) => s.parent === "stage:reason");
  assert.equal(extract.length, 1);
  assert.equal(reason.length, 1);
  assert.equal(reason[0].model, "judgment");
});

test("a department title becomes an agent span in the fan-out", () => {
  const state = replay([
    event("run.started", 0),
    event("agent.started", 1, { department: "props", findings: 2 }),
    event("model.call.started", 1, { label: "Props", model: "bulk" }),
    event("model.call.finished", 3, {
      label: "Props",
      model: "bulk",
      input_tokens: 400,
      output_tokens: 200,
    }),
  ]);

  const span = ordered(state).find((s) => s.kind === "agent");
  assert.ok(span);
  assert.equal(span.parent, "stage:fanout");
  assert.equal(span.name, "Props");
});

test("a cache hit arrives with no start and still shows", () => {
  const state = replay([
    event("run.started", 0),
    event("model.call.finished", 1, {
      label: "scene 3",
      model: "judgment",
      cached: true,
    }),
  ]);

  const span = ordered(state).find((s) => s.kind !== "stage");
  assert.ok(span);
  assert.equal(span.status, "cached");
  assert.equal(totals(state).cached, 1);
  assert.equal(totals(state).calls, 0);
});

test("a fallback marks the failed call rather than renaming it, and never names a model", () => {
  const state = replay([
    event("run.started", 0),
    event("model.call.started", 1, { label: "scene 4", model: "judgment", attempt: 1 }),
    event("model.fallback", 2, {
      from_model: "judgment",
      to_model: "bulk",
      error: "ThrottlingException",
    }),
    event("model.call.started", 2, { label: "scene 4", model: "bulk", attempt: 2 }),
    event("model.call.finished", 4, {
      label: "scene 4",
      model: "bulk",
      via_fallback: true,
      input_tokens: 500,
      output_tokens: 300,
    }),
  ]);

  const spans = ordered(state).filter((s) => s.kind !== "stage");
  const failed = spans.find((s) => s.status === "failed");
  assert.ok(failed, "the throttled judgment call is kept and marked failed");
  assert.equal(failed.model, "judgment");
  assert.equal(failed.detail, "retried on a fallback model");

  const succeeded = spans.find((s) => s.status === "ok");
  assert.ok(succeeded);
  assert.equal(succeeded.viaFallback, true);
  assert.equal(succeeded.attempt, 2);
});

test("a started call never carries the real model id into its detail", () => {
  const state = replay([
    event("run.started", 0),
    event("model.call.started", 1, {
      label: "scene 3",
      model: "judgment",
      model_id: "us.anthropic.claude-sonnet-4-6",
    }),
  ]);

  const span = ordered(state).find((s) => s.kind !== "stage");
  assert.ok(span);
  assert.equal(span.detail, "");
});

test("sealing closes spans left open when the stream ends", () => {
  const open = replay([
    event("run.started", 0),
    event("model.call.started", 1, { label: "scene 9", model: "judgment" }),
  ]);
  assert.ok(ordered(open).some((s) => s.status === "running"));

  const sealed = sealTrace(open, 6);
  assert.ok(!ordered(sealed).some((s) => s.status === "running"));
  assert.ok(ordered(sealed).every((s) => s.endedAt > 0));
});

test("cost uses the tier the call actually ran on", () => {
  const state = replay([
    event("run.started", 0),
    event("model.call.started", 1, { label: "scene 3", model: "judgment" }),
    event("model.call.finished", 2, {
      label: "scene 3",
      model: "judgment",
      input_tokens: 1_000_000,
      output_tokens: 0,
    }),
  ]);

  // Judgment tier input is $3/M, so exactly one million input tokens is $3.
  assert.equal(fmtCost(totals(state).cost), "$3.00");
});

test("durations read as a person would write them", () => {
  assert.equal(fmtDuration(0.42), "420ms");
  assert.equal(fmtDuration(3.5), "3.50s");
});
