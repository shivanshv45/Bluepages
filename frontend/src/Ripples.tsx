/* Ripple panels.
 *
 * A change in one scene is not always local. The letter opener named in one
 * scene might also sit on a desk in another; a character renamed here might
 * be booked in a dozen other scenes. Real judgment means noticing that and
 * checking, not just reasoning about the scene that changed and stopping.
 *
 * Ripples are folded live off the run's own event stream (ripple.opened,
 * ripple.step, ripple.resolved), the same way every other panel on this
 * screen is. Each ripple either resolves quiet (nothing to see) or lands a
 * finding, which joins the main decision stream the same way any other
 * finding does.
 */

import { useEffect, useState } from "react";
import type { RunEvent } from "./api";

interface LiveRipple {
  id: string;
  scene: string;
  heading: string;
  hypothesis: string;
  state: "running" | "done";
  step: string;
  outcome: "quiet" | "finding" | "";
  result: string;
}

function str(value: unknown): string {
  return typeof value === "string" ? value : "";
}

/* Fold the run's own ripple events into panels. Runs on the feed the console
   already has, so a viewer joining mid-run still sees ripples already open. */
export function useLiveRipples(feed: RunEvent[]): LiveRipple[] {
  const [ripples, setRipples] = useState<Record<string, LiveRipple>>({});

  useEffect(() => {
    // The feed arrives newest first; replay oldest first so state builds up
    // the way the run actually happened.
    const ordered = [...feed].reverse();
    const next: Record<string, LiveRipple> = {};
    for (const event of ordered) {
      const data = event.data ?? {};
      const id = str(data.id) || str(data.scene_number);
      if (!id) continue;

      if (event.kind === "ripple.opened") {
        next[id] = {
          id,
          scene: str(data.scene_number),
          heading: str(data.heading),
          hypothesis: str(data.hypothesis) || event.message,
          state: "running",
          step: "",
          outcome: "",
          result: "",
        };
      } else if (event.kind === "ripple.step" && next[id]) {
        next[id] = { ...next[id], step: str(data.step) || event.message };
      } else if (event.kind === "ripple.resolved" && next[id]) {
        const outcome = str(data.outcome) === "finding" ? "finding" : "quiet";
        next[id] = {
          ...next[id],
          state: "done",
          outcome,
          result: str(data.result) || event.message,
        };
      }
    }
    setRipples(next);
  }, [feed]);

  return Object.values(ripples);
}

export function RipplePanels({
  ripples,
  onOpenScene,
  docked,
}: {
  ripples: LiveRipple[];
  onOpenScene?: (scene: string) => void;
  /* Docked lives inline in the rail column instead of floating over the
     screen, so it can sit in the space Budget and Account used to hold. */
  docked?: boolean;
}) {
  if (!ripples.length) {
    return docked ? <p className="rail-ripples-empty hint">Nothing else to check yet.</p> : null;
  }

  return (
    <div className={docked ? "ripple-stack docked" : "ripple-stack"} aria-live="polite">
      {ripples.map((ripple) => (
        <article
          key={ripple.id}
          className={`ripple-card ${ripple.state} ${ripple.state === "done" ? ripple.outcome : ""}`}
        >
          <header>
            <span className="ripple-scene mono">Sc. {ripple.scene}</span>
            <span className="ripple-heading">{ripple.heading}</span>
            {ripple.state === "running" && <span className="spin" aria-hidden="true" />}
            {ripple.state === "done" && (
              <span className={`ripple-outcome ${ripple.outcome}`}>
                {ripple.outcome === "finding" ? "finding" : "clear"}
              </span>
            )}
          </header>

          {ripple.state === "running" && <p className="ripple-reason">{ripple.hypothesis}</p>}

          {ripple.state === "running" && ripple.step && (
            <p className="ripple-step mono">{ripple.step}</p>
          )}

          {ripple.state === "done" && (
            <>
              <p className="ripple-hypothesis-done mono">{ripple.hypothesis}</p>
              <p className={`ripple-result ${ripple.outcome}`}>{ripple.result}</p>
            </>
          )}

          {ripple.state === "done" && onOpenScene && (
            <button className="linkish ripple-open" onClick={() => onOpenScene(ripple.scene)}>
              Open scene {ripple.scene} &rarr;
            </button>
          )}
        </article>
      ))}
    </div>
  );
}
