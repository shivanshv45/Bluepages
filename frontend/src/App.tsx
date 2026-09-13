/* Bluepages.
 *
 * The rule that shapes this file, from PLAN.md: the app never shows an empty
 * screen. Idle is "watching, here is what happened last run", never a blank
 * page with an upload button. That is why the console falls back to the last
 * run reconstructed from the database rather than to a placeholder.
 *
 * One console rather than four tabs. The graph, the trace, the revision and
 * the inventory are all views of the same run, so they belong on one screen.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { Link, Navigate, useSearchParams } from "react-router-dom";
import {
  api,
  watchRun,
  type Account,
  type Production,
  type ProductionRow,
  type RunEvent,
} from "./api";
import { ago } from "./components";
import { Console } from "./Console";
import { elapsed, emptyRun, reduce, type RunState } from "./runState";
import { emptyTrace, reduceTrace, sealTrace, type TraceState } from "./trace";
import {
  emptyGraph,
  graphFromProduction,
  reduceGraph,
  seedGraph,
  type GraphState,
} from "./graph";
import { createPacer } from "./pace";
import { DRAFT_DROPPED_EVENT, type DraftDroppedDetail } from "./DropCapture";

export default function App() {
  const [searchParams, setSearchParams] = useSearchParams();
  const urlTitle = searchParams.get("title") ?? "";
  const urlScene = searchParams.get("scene") ?? "";
  const shouldAutoRun = searchParams.get("run") === "1";
  const [productions, setProductions] = useState<ProductionRow[] | null>(null);
  const [title, setTitle] = useState<string>(urlTitle);
  const [production, setProduction] = useState<Production | null>(null);
  const [run, setRun] = useState<RunState>(emptyRun);
  const [trace, setTrace] = useState<TraceState>(emptyTrace);
  const [graph, setGraph] = useState<GraphState>(emptyGraph);
  const [now, setNow] = useState<number>(0);
  const [runId, setRunId] = useState<string>("");
  const [error, setError] = useState<string>("");
  const [account, setAccount] = useState<Account | null>(null);
  // Bumped when a run ends, so the decision log refetches without polling.
  const [decisionKey, setDecisionKey] = useState(0);

  // The graph lays out in pixels, so its reducer needs the canvas size.
  const sizeRef = useRef({ width: 900, height: 400 });
  const nowRef = useRef(0);

  useEffect(() => {
    api.me().then(setAccount).catch(() => setAccount({ signed_in: false }));
    api
      .productions()
      .then((rows) => {
        setProductions(rows);
        if (rows.length) setTitle((current) => current || rows[0].title);
      })
      .catch((e: Error) => setError(e.message));
  }, []);

  const load = useCallback((name: string) => {
    if (!name) return;
    api.production(name).then(setProduction).catch((e: Error) => setError(e.message));
  }, []);

  useEffect(() => {
    load(title);
  }, [title, load]);

  /* Rebuild the graph from the database when nothing is running.
   *
   * Without this a reload shows an empty graph beside a full set of results,
   * which is the blank panel PLAN.md rules out. A live run overwrites this the
   * moment its first event lands.
   */
  useEffect(() => {
    if (!production || run.status === "running") return;
    setGraph(
      graphFromProduction(
        production.changes,
        production.reports,
        production.scenes.length,
        sizeRef.current.width,
        sizeRef.current.height,
      ),
    );
  }, [production, run.status]);

  /* Watch a run.
   *
   * Events go through a pacer rather than straight to state. The server
   * replays its backlog the moment we connect, and a cached run emits its
   * whole pipeline in well under a second, so without pacing the screen jumps
   * to the finished state with nothing to watch. The events keep their real
   * timestamps; only the delivery to the screen is slowed.
   */
  useEffect(() => {
    if (!runId) return;

    const pacer = createPacer((event: RunEvent) => {
      setRun((state) => reduce(state, event));
      setTrace((state) => reduceTrace(state, event));
      setGraph((state) =>
        reduceGraph(state, event, sizeRef.current.width, sizeRef.current.height),
      );
      nowRef.current = event.ts;
      setNow(event.ts);
    });

    const stop = watchRun(
      runId,
      (event) => pacer.push(event),
      (status) => {
        // The queue may still be draining, so the run is only marked finished
        // once the last event has actually reached the screen.
        pacer.close(() => {
          setRun((state) => ({
            ...state,
            status: status === "failed" ? "failed" : "finished",
          }));
          setTrace((state) => sealTrace(state, nowRef.current));
          setDecisionKey((n) => n + 1);
          load(title);
          api.productions().then(setProductions).catch(() => undefined);
        });
      },
    );

    return () => {
      stop();
      pacer.stop();
    };
  }, [runId, title, load]);

  const begin = useCallback(() => {
    setError("");
    setRun({ ...emptyRun(), status: "running" });
    setTrace(emptyTrace());
    setGraph(seedGraph(sizeRef.current.width, sizeRef.current.height));
  }, []);

  const startSample = useCallback(async () => {
    begin();
    try {
      const started = await api.startRun(
        "tests/fixtures/small-draft-1.fdx",
        "tests/fixtures/small-draft-2.fdx",
        title || "The Farm",
      );
      setRunId(started.run_id);
      if (!title) setTitle(started.production);
    } catch (e) {
      setError((e as Error).message);
      setRun((state) => ({ ...state, status: "failed" }));
    }
  }, [title, begin]);

  const startUpload = useCallback(
    async (before: File | null, after: File, name: string) => {
      begin();
      try {
        const started = await api.uploadDrafts(before, after, name);
        setRunId(started.run_id);
        setTitle(started.production);
      } catch (e) {
        setError((e as Error).message);
        setRun((state) => ({ ...state, status: "failed" }));
      }
    },
    [begin],
  );

  /* A draft dropped anywhere in the app (DropCapture) fires this once it has
     worked out which production the file belongs to. If that is the console
     already open, the run starts right here rather than making the drop
     useless because nobody was looking at a new tab. */
  useEffect(() => {
    const onDropped = (e: Event) => {
      const detail = (e as CustomEvent<DraftDroppedDetail>).detail;
      if (!detail || detail.production !== title) return;
      if (run.status === "running") return;
      startSample();
    };
    window.addEventListener(DRAFT_DROPPED_EVENT, onDropped);
    return () => window.removeEventListener(DRAFT_DROPPED_EVENT, onDropped);
  }, [title, run.status, startSample]);

  /* A drop that landed on a console that was not mounted yet cannot reach it
     with an event, since the page it fired on is gone by the time this one
     loads. ?run=1 in the URL carries that intent across the navigation
     instead. Consumed once: the flag is stripped from the URL immediately
     so refreshing the page does not restart the run underneath the viewer. */
  useEffect(() => {
    if (!shouldAutoRun || !urlTitle) return;
    setSearchParams(
      (params) => {
        params.delete("run");
        return params;
      },
      { replace: true },
    );
    startSample();
    // startSample is intentionally left out: it closes over title, which is
    // still catching up to urlTitle on the very first render, and re-firing
    // this effect every time startSample's identity changes would run it more
    // than once. shouldAutoRun/urlTitle only ever matter on that first load.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [shouldAutoRun, urlTitle]);

  const recipients: Record<string, string> = {};
  for (const row of production?.recipients ?? []) {
    recipients[row.department] = row.email;
  }

  const lastRun = production?.runs?.[0] ?? productions?.[0]?.runs?.[0] ?? null;
  const statusText =
    run.status === "running"
      ? `${run.stage || "working"} · ${elapsed(run)}`
      : run.status === "failed"
        ? "last run failed"
        : lastRun
          ? `watching. last run ${ago(lastRun.finished_at ?? lastRun.started_at)}.`
          : "watching. no runs yet.";

  const pip =
    run.status === "running" ? "live" : run.status === "failed" ? "failed" : "done";

  if (account === null) {
    return <div className="shell" />;
  }
  if (!account.signed_in) {
    return <Navigate to="/login" replace />;
  }

  return (
    <div className="shell">
      <header className="statusbar">
        <Link className="wordmark" to="/">
          blue<span>pages</span>
        </Link>
        <Link className="linkish statusbar-projects" to="/projects">
          Productions
        </Link>
        <div className="state">
          <span className={`pip ${pip}`} aria-hidden="true" />
          <span>{statusText}</span>
        </div>
        <div className="spacer" />
      </header>

      {error && <div className="err banner">{error}</div>}

      <Console
        run={run}
        trace={trace}
        graph={graph}
        now={now}
        production={production}
        productions={productions}
        title={title}
        scene={urlScene}
        onPick={setTitle}
        onSample={startSample}
        onUpload={startUpload}
        onApprove={async () => {
          await api.approve(title);
          load(title);
        }}
        onSend={async () => {
          await api.send(title);
          load(title);
        }}
        onGraphResize={(width, height) => {
          sizeRef.current = { width, height };
        }}
        decisionKey={decisionKey}
        recipients={recipients}
        onDecided={() => undefined}
        onAddRecipient={async (department, email) => {
          await api.addRecipient(title, department, email);
          load(title);
        }}
      />
    </div>
  );
}
