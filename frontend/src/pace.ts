/* Pacing the event stream.
 *
 * Two things arrive faster than a person can read. The server replays the
 * backlog the instant a viewer connects, and a cached run (which is most runs
 * during development, per the cost rule in CLAUDE.md) emits its whole pipeline
 * in well under a second. Both land as one burst, and the graph jumps straight
 * to its finished state with nothing to watch.
 *
 * So the events are queued on arrival and drained on a timer. The run's own
 * timestamps are preserved on every event, because the trace measures real
 * model latency and must not be told a comfortable lie. Only the delivery to
 * the screen is paced.
 */

import type { RunEvent } from "./api";

/* How fast to drain.
 *
 * Fast enough that a real 60-second run is never held back (the queue simply
 * stays empty and events pass straight through), slow enough that a cached
 * run still reads as a sequence of steps.
 */
const TICK_MS = 110;

/* Some events deserve to be dwelt on. A finding routing to departments is the
   moment the product exists for, so it holds the screen a beat longer. */
const DWELL: Record<string, number> = {
  "change.detected": 3,
  "agent.finished": 2,
  "model.fallback": 4,
  "parse.finished": 2,
};

/* When the queue grows past this, drain several per tick rather than falling
   further behind. A long feature run must not finish minutes after the
   pipeline did. */
const CATCH_UP = 40;

export interface Pacer {
  push: (event: RunEvent) => void;
  /* Called when the stream closes. The queue keeps draining, and `onDrained`
     fires once the last event has actually been shown. */
  close: (onDrained: () => void) => void;
  stop: () => void;
}

export function createPacer(deliver: (event: RunEvent) => void): Pacer {
  const queue: RunEvent[] = [];
  let debt = 0;
  let closed = false;
  let drainedCallback: (() => void) | null = null;

  const timer = setInterval(() => {
    if (debt > 0) {
      debt -= 1;
      return;
    }

    if (!queue.length) {
      if (closed && drainedCallback) {
        const done = drainedCallback;
        drainedCallback = null;
        done();
      }
      return;
    }

    // Behind: drain a batch so the screen catches up with the pipeline.
    const batch = queue.length > CATCH_UP ? Math.ceil(queue.length / CATCH_UP) : 1;
    for (let i = 0; i < batch && queue.length; i++) {
      const event = queue.shift()!;
      deliver(event);
      if (i === batch - 1) debt = DWELL[event.kind] ?? 0;
    }
  }, TICK_MS);

  return {
    push(event) {
      queue.push(event);
    },
    close(onDrained) {
      closed = true;
      drainedCallback = onDrained;
    },
    stop() {
      clearInterval(timer);
      queue.length = 0;
      drainedCallback = null;
    },
  };
}
