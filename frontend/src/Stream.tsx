/* The decision stream.
 *
 * The right rail used to be a list: forty cards, all present at once, scrolled.
 * That reads as a report the agent finished before you arrived. This renders
 * the same decisions as a stream that writes itself out the way a model does,
 * one line at a time, and then **stops** at anything needing a human: a
 * purchase to approve, a brief to send.
 *
 * The pause is the point. An agent that halts and asks is doing something an
 * ordinary dashboard cannot; an agent that renders forty finished cards is
 * indistinguishable from a database query. Nothing below fakes the decisions
 * themselves, which are real and already made. Only their arrival is paced.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { api, type DecisionRow, type RunEvent } from "./api";
import { departmentName } from "./components";

/* Types text out a character at a time, the way a model's own answer
   arrives. Only the newest line does this; anything already read sits
   finished, because replaying every card's typing on every render would be
   noise, not signal. */
function TypedText({ text, active, speed = 14 }: { text: string; active: boolean; speed?: number }) {
  const [shown, setShown] = useState(active ? 0 : text.length);

  useEffect(() => {
    if (!active) {
      setShown(text.length);
      return;
    }
    setShown(0);
    let i = 0;
    const id = setInterval(() => {
      i += 1;
      setShown(i);
      if (i >= text.length) clearInterval(id);
    }, speed);
    return () => clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [text, active]);

  return <>{text.slice(0, shown)}</>;
}

const KIND_LABEL: Record<string, string> = {
  route: "Routed",
  urgency: "Urgency",
  draft_email: "Drafted",
  await_approval: "Held",
  send: "Sent",
  fallback: "Fallback",
  procure: "Purchase",
  social_post: "Social",
};

/* A decision that needs a person stops the stream. Everything else flows. */
function blocks(row: DecisionRow): boolean {
  return row.status === "pending" || row.status === "proposed";
}

/* How far the run has got.
 *
 * A decision is a department's conclusion, so it cannot be true before that
 * department's agent has actually finished: showing "Ford Bronco flagged
 * before it shoots" while Clearance is still shown checking the brand
 * database is the run contradicting itself. So a row is "reached" once its
 * own department has finished, not when it started, and it sorts on that
 * same moment, which keeps it under the activity lines that led to it
 * instead of jumping ahead of steps still to come. The two rows with no
 * department hang off the events that produced them: the fallback line off
 * `model.fallback`, the drafted post off Social's `agent.finished`.
 *
 * Nothing here invents a decision or reorders one arbitrarily. It only
 * withholds rows the run has not concluded yet, so the pane fills in beside
 * the graph instead of showing the whole finished report the moment the
 * console opens.
 */
interface Reach {
  /* Department to the wall-clock second its agent finished. */
  finishedAt: Map<string, number>;
  fallbackAt: number;
}

function reachedBy(feed: RunEvent[]): Reach {
  const finishedAt = new Map<string, number>();
  let fallbackAt = -1;
  // The feed arrives newest first, so a later pass overwrites an earlier one
  // with the true first timestamp.
  for (const event of feed) {
    const department = String(event.data?.department ?? "");
    if (event.kind === "agent.finished" && department) finishedAt.set(department, event.ts);
    if (event.kind === "model.fallback") fallbackAt = event.ts;
  }
  return { finishedAt, fallbackAt };
}

/* When the run arrived at a row, or -1 if it has not yet. */
function reachedAt(row: DecisionRow, reach: Reach): number {
  if (row.kind === "fallback") return reach.fallbackAt;
  if (row.kind === "social_post") return reach.finishedAt.get("social") ?? -1;
  if (!row.department) return -1;
  return reach.finishedAt.get(row.department) ?? -1;
}

/* Live activity.
 *
 * The stored decisions only cover the department agents, but the pipeline
 * stages ahead of them (parsing, aligning, reasoning, every model call and
 * every step inside a department) are real work with nothing in the log to
 * show for it. That left the pane near-empty while the graph was visibly
 * busy. These entries come straight off the run's own events, so the pane is
 * active from the first second and every node that lights up says what it is
 * doing. They carry no id and no gate: they are activity, not decisions.
 */
export interface Activity {
  ts: number;
  kind: string;
  label: string;
  department: string;
  scene: string;
  text: string;
}

const ACTIVITY_LABEL: Record<string, string> = {
  "run.started": "Received",
  "scene.parsed": "Parsed",
  "parse.finished": "Parsed",
  "scene.aligned": "Aligned",
  "model.call.started": "Reasoning",
  "model.call.finished": "Reasoned",
  "element.found": "Extracted",
  "change.detected": "Found",
  "agent.started": "Started",
  "agent.step": "Working",
  "agent.finished": "Finished",
  "tokens.spent": "Spend",
  info: "Checking",
};

function activityFrom(feed: RunEvent[]): Activity[] {
  const out: Activity[] = [];
  for (const event of feed) {
    const label = ACTIVITY_LABEL[event.kind];
    if (!label) continue;
    const data = event.data ?? {};
    out.push({
      ts: event.ts,
      kind: event.kind,
      label,
      department: String(data.department ?? ""),
      scene: String(data.scene_number ?? data.number ?? ""),
      text: event.message,
    });
  }
  // The feed arrives newest first.
  return out.reverse();
}

export function Stream({
  title,
  refreshKey,
  recipients,
  onChanged,
  running,
  feed,
  onAwaiting,
  onlyAwaiting,
}: {
  title: string;
  refreshKey: number;
  recipients: Record<string, string>;
  onChanged: () => void;
  running: boolean;
  feed: RunEvent[];
  onAwaiting?: (count: number) => void;
  /* The Approvals tab: the same stream, narrowed to the rows that stopped
     and asked for a person. Everything the agent handled on its own stays
     in Decisions, which is where the work is legible as work. */
  onlyAwaiting?: boolean;
}) {
  const [rows, setRows] = useState<DecisionRow[] | null>(null);
  const [error, setError] = useState("");
  const endRef = useRef<HTMLDivElement>(null);
  /* Whether a run has happened while this console has been open. Until one
     has, the pane stays empty: the last run's decisions belong to a draft
     the viewer has not opened, and showing them makes the console look like
     it already did the work the run is about to do. */
  const [ranOnce, setRanOnce] = useState(false);

  useEffect(() => {
    if (running) setRanOnce(true);
  }, [running]);

  const load = useCallback(() => {
    if (!title) return;
    api
      .decisions(title)
      // Oldest first: a stream reads forwards, unlike a log.
      .then((data) => setRows([...data].reverse()))
      .catch((e: Error) => setError(e.message));
  }, [title]);

  useEffect(() => {
    load();
  }, [load, refreshKey]);

  useEffect(() => {
    // The Approvals tab is a list, not a stream, so it must not yank the
    // reader to the bottom every time the run emits an event.
    if (onlyAwaiting) return;
    endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [feed.length, onlyAwaiting]);

  /* What the run has written out so far: while it is live, the rows whose
     department has actually started; once it has ended, everything.
     The Approvals tab is a standing list rather than a replay of the run, so
     it does not wait on ranOnce: a tab opened after the run still has to show
     what is sitting there waiting on a person. */
  const reached = (() => {
    if (!rows) return [];
    if (!ranOnce && !onlyAwaiting) return [];
    if (!running) return rows;
    const reach = reachedBy(feed);
    return rows
      .map((row) => ({ row, at: reachedAt(row, reach) }))
      .filter((entry) => entry.at >= 0)
      .sort((a, b) => a.at - b.at)
      .map((entry) => entry.row);
  })();

  const awaiting = reached.filter(blocks);

  useEffect(() => {
    onAwaiting?.(awaiting.length);
  }, [awaiting.length, onAwaiting]);

  /* The two sources interleaved on one clock: the run's own activity, and the
     decisions it produced. A decision lands at the moment its department
     started, so it reads as the outcome of the lines above it. */
  const timeline: ({ kind: "act"; act: Activity } | { kind: "row"; row: DecisionRow })[] =
    (() => {
      if (onlyAwaiting || !ranOnce) return [];
      const reach = reachedBy(feed);
      const acts = activityFrom(feed).map(
        (act) => ({ at: act.ts, entry: { kind: "act" as const, act } }),
      );
      const decisions = reached.map((row) => ({
        // A finished run has no live feed to sit against, so decisions fall
        // back to their own recorded order.
        at: running ? reachedAt(row, reach) : Number.MAX_SAFE_INTEGER,
        entry: { kind: "row" as const, row },
      }));
      return [...acts, ...decisions]
        .sort((a, b) => a.at - b.at)
        .map((item) => item.entry);
    })();

  const resolve = async (id: string, status: "approved" | "rejected") => {
    setRows((current) =>
      (current ?? []).map((row) => (row.id === id ? { ...row, status } : row)),
    );
    try {
      await api.decide(id, status);
      // Tells the budget rail to refetch. The stream keeps its position.
      onChanged();
    } catch {
      load();
    }
  };

  if (error) return <div className="pane-empty">{error}</div>;
  if (!rows) return <div className="pane-empty">Reading the decision log…</div>;

  /* Approvals: only what stopped and asked, no stream furniture. */
  if (onlyAwaiting) {
    if (!awaiting.length) {
      return (
        <div className="pane-empty">
          <p>
            Nothing waiting on you. Anything the agent cannot decide by itself
            collects here, with what it wants to do and why.
          </p>
        </div>
      );
    }
    return (
      <div className="stream">
        {awaiting.map((row) => (
          <Line
            key={row.id}
            row={row}
            latest={false}
            blocking
            recipient={recipients[row.department ?? ""] ?? ""}
            onResolve={resolve}
          />
        ))}
      </div>
    );
  }

  if (!timeline.length) {
    return (
      <div className="pane-empty">
        <p>
          Nothing decided yet. Drop a revision and the agent writes out what it
          does here, stopping whenever it needs you.
        </p>
      </div>
    );
  }

  return (
    <div className="stream">
      {timeline.map((entry, i) =>
        entry.kind === "act" ? (
          <ActivityLine
            key={`act-${entry.act.ts}-${i}`}
            act={entry.act}
            latest={running && i === timeline.length - 1}
          />
        ) : (
          <Line
            key={entry.row.id}
            row={entry.row}
            latest={false}
            blocking={blocks(entry.row)}
            recipient={recipients[entry.row.department ?? ""] ?? ""}
            onResolve={resolve}
          />
        ),
      )}

      {!running && (
        <div className="stream-done">
          <span className="mono">{reached.length} decisions</span>
        </div>
      )}

      <div ref={endRef} />
    </div>
  );
}

/* One line of the run working. Lighter than a decision card on purpose: this
   is the agent narrating itself, and the cards are what it concluded. */
function ActivityLine({ act, latest }: { act: Activity; latest: boolean }) {
  return (
    <div className={`act ${latest ? "latest" : ""}`}>
      <span className={`act-kind ${act.kind.replace(".", "-")}`}>{act.label}</span>
      {act.department && <span className="act-dept">{departmentName(act.department)}</span>}
      {act.scene && <span className="mono act-scene">Sc. {act.scene}</span>}
      <span className="act-text">{act.text}</span>
    </div>
  );
}

function Line({
  row,
  latest,
  blocking,
  recipient,
  onResolve,
}: {
  row: DecisionRow;
  latest: boolean;
  blocking: boolean;
  recipient: string;
  onResolve: (id: string, status: "approved" | "rejected") => void;
}) {
  const payload = parse(row.payload);

  return (
    <article
      className={`line ${row.kind} ${row.status} ${latest ? "latest" : ""} ${
        blocking ? "blocking" : ""
      }`}
    >
      <header>
        <span className={`line-kind ${row.kind}`}>
          {KIND_LABEL[row.kind] ?? row.kind}
        </span>
        {row.department && (
          <span className="line-dept">{departmentName(row.department)}</span>
        )}
        {row.scene_number && (
          <span className="mono line-scene">Sc. {row.scene_number}</span>
        )}
        {row.status === "approved" && row.auto_approved ? (
          <span className="line-ok">auto-approved under the limit</span>
        ) : row.status === "approved" ? (
          <span className="line-ok">approved</span>
        ) : null}
        {row.status === "rejected" && <span className="line-no">rejected</span>}
      </header>

      <p className="line-summary">
        <TypedText text={row.summary} active={latest} />
      </p>
      {row.rationale && (
        <p className="line-why">
          <TypedText text={row.rationale} active={latest} speed={10} />
        </p>
      )}

      {/* A purchase renders as an order line, because that is what it is.
          A payload from an older run may predate sourcing, so the order line
          only renders when there is actually a product in it. */}
      {row.kind === "procure" && payload && payload.product ? (
        <Order payload={payload} />
      ) : null}

      {row.kind === "social_post" && payload ? <SocialPost payload={payload} /> : null}

      {row.kind === "draft_email" && (
        <p className="line-to">
          {recipient ? (
            <>
              To <span className="mono">{recipient}</span>
            </>
          ) : (
            <span className="line-norecipient">
              No recipient set for this department. Add one to send.
            </span>
          )}
        </p>
      )}

      {row.action && row.kind !== "procure" && row.kind !== "social_post" && (
        <p className="line-action">
          <span aria-hidden="true">&rarr;</span> {row.action}
        </p>
      )}

      {blocking && (
        <div className="line-gate">
          <span className="line-ask">
            {row.kind === "procure"
              ? "Bluepages does not place orders. Approving sends this to Finance to complete."
              : row.kind === "social_post"
                ? "Approve to post, or reject to skip it."
                : "Approve to release this brief."}
          </span>
          <div className="line-buttons">
            <button className="btn primary" onClick={() => onResolve(row.id, "approved")}>
              {row.kind === "procure" ? "Trigger payment" : row.kind === "social_post" ? "Post" : "Approve"}
            </button>
            <button className="btn" onClick={() => onResolve(row.id, "rejected")}>
              Reject
            </button>
          </div>
        </div>
      )}
    </article>
  );
}

/* The order line. A real product, a real supplier, a priced total. */
function Order({ payload }: { payload: Record<string, unknown> }) {
  const money = (value: unknown) =>
    typeof value === "number"
      ? value.toLocaleString(undefined, { style: "currency", currency: "USD" })
      : "--";

  const image = typeof payload.image === "string" ? payload.image : "";
  const url = typeof payload.url === "string" ? payload.url : "";

  return (
    <div className="order">
      {/* The picture comes off the retailer's own page, so a listing that
          blocks hotlinking simply has none rather than a broken frame. */}
      {image && (
        <a
          className="order-shot"
          href={url || undefined}
          target="_blank"
          rel="noreferrer noopener"
        >
          <img
            src={image}
            alt={String(payload.product ?? "product")}
            loading="lazy"
            onError={(e) => {
              (e.currentTarget.closest(".order-shot") as HTMLElement)?.remove();
            }}
          />
        </a>
      )}

      <div className="order-head">
        <strong>{String(payload.product ?? "")}</strong>
        <span className="mono order-total">{money(payload.total)}</span>
      </div>
      <dl className="order-grid">
        <div>
          <dt>Supplier</dt>
          <dd>{String(payload.supplier ?? "")}</dd>
        </div>
        <div>
          <dt>Unit</dt>
          <dd>
            {money(payload.unit_price)} / {String(payload.unit ?? "")}
          </dd>
        </div>
        <div>
          <dt>Lead time</dt>
          <dd>{String(payload.lead_time ?? "")}</dd>
        </div>
      </dl>
      {payload.note ? <p className="order-note">{String(payload.note)}</p> : null}
      {/* The citations. A price without a source is an assertion; with one it
          is checkable, which is the whole difference. */}
      {Array.isArray(payload.sources) && payload.sources.length > 0 && (
        <p className="order-sources">
          Found on{" "}
          {(payload.sources as string[]).map((source, i) => (
            <span key={i}>
              {i > 0 && ", "}
              {i === 0 && url ? (
                <a
                  className="mono"
                  href={url}
                  target="_blank"
                  rel="noreferrer noopener"
                >
                  {source}
                </a>
              ) : (
                <span className="mono">{source}</span>
              )}
            </span>
          ))}
        </p>
      )}

      <p className="order-sim">
        {payload.grounded === false
          ? "Not sourced: the web search failed, so this line needs pricing by hand."
          : "Live listing. Bluepages never pays a supplier itself: approving sends this to Finance, and it moves to spent once they complete it."}
      </p>
    </div>
  );
}

/* A drafted social post. The agent decides a change is worth telling people
   about and writes the copy, but nothing posts until a person says so. */
function SocialPost({ payload }: { payload: Record<string, unknown> }) {
  const platform = String(payload.platform ?? "");
  const image = typeof payload.image === "string" ? payload.image : "";
  const copy = String(payload.copy ?? "");

  return (
    <div className="social">
      {image && (
        <div className="social-shot">
          <img src={image} alt="" loading="lazy" />
        </div>
      )}
      <div className="social-body">
        {platform && <span className="social-platform mono">{platform}</span>}
        <p className="social-copy">{copy}</p>
      </div>
    </div>
  );
}

function parse(payload: string | null): Record<string, unknown> | null {
  if (!payload) return null;
  try {
    return JSON.parse(payload) as Record<string, unknown>;
  } catch {
    return null;
  }
}
