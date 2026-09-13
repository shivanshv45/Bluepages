/* The API client.
 *
 * Plain fetch and a small EventSource wrapper. Talks to the real backend at
 * /api, proxied to the FastAPI server by Vite in development.
 */

export type Kind = string;

export interface RunEvent {
  kind: Kind;
  message: string;
  data: Record<string, unknown>;
  ts: number;
}

export interface DraftRow {
  id: string;
  revision: number;
  colour: string | null;
  scene_count: number;
  ingested_at: string;
}

export interface RunRow {
  id: string;
  status: string;
  scenes_parsed: number;
  findings: number;
  model_calls: number;
  input_tokens: number;
  output_tokens: number;
  fallbacks: number;
  started_at: string;
  finished_at: string | null;
}

export interface NoteRow {
  scene_number: string | null;
  note: string;
  action: string;
  urgent: number;
}

export interface DeliveryRow {
  email: string;
  status: string;
  provider_id: string | null;
  error: string | null;
  attempted_at: string;
}

export interface ReportRow {
  id: string;
  department: string;
  summary: string;
  model_name: string | null;
  via_fallback: number;
  approved_at: string | null;
  sent_at: string | null;
  notes: NoteRow[];
  deliveries?: DeliveryRow[];
}

export interface RecipientRow {
  id: string;
  department: string;
  name: string;
  email: string;
}

export interface ClearanceRow {
  name: string;
  category: string;
  scene: string | null;
  quote: string | null;
}

export interface InventoryRow {
  name: string;
  category: string;
  department: string;
  appearances: number;
  branded?: number;
}

export interface TrailRow {
  revision: number;
  scene: string;
  department: string;
}

export interface SceneRow {
  number: string;
  state: "seen" | "changed" | "inserted" | "omitted";
  heading: string;
}

export interface ChangeRow {
  id: string;
  kind: string;
  summary: string;
  reasoning: string;
  scene_number: string | null;
  from_scene: string | null;
  confidence: number;
  risk: string | null;
  departments: string[];
}

export interface Production {
  id: string;
  title: string;
  latest_draft: DraftRow | null;
  reports: ReportRow[];
  runs: RunRow[];
  recipients: RecipientRow[];
  clearance: ClearanceRow[];
  departments: Record<string, number>;
  scenes: SceneRow[];
  changes: ChangeRow[];
}

export interface ProductionRow {
  id: string;
  title: string;
  created_at: string;
  drafts: number;
  latest_draft: DraftRow | null;
  runs: RunRow[];
}

export interface DecisionRow {
  id: string;
  run_id: string | null;
  kind: string;
  department: string | null;
  scene_number: string | null;
  summary: string;
  rationale: string;
  action: string;
  simulated: number;
  status: string;
  payload: string | null;
  created_at: string;
  // Set on a "procure" decision that cleared automatically under a
  // department's auto-approve limit, so it is never mistaken for a human
  // approval. Absent or 0 otherwise.
  auto_approved?: number;
}

export interface BudgetRow {
  department: string;
  allocated: number;
  committed: number;
  pending: number;
  remaining: number;
  over: boolean;
  auto_approve: number;
}

export interface Account {
  signed_in: boolean;
  id?: string;
  email?: string;
  name?: string;
}

export interface Health {
  ok: boolean;
  region: string;
  runtime: string;
  models: { judgment: string; bulk: string };
  email: boolean;
  active_runs: number;
}

export interface IdentifyResult {
  production: string | null;
  production_confidence: number;
  scene: string | null;
  scene_confidence: number;
  filename: string;
  // True when the runner-up production scored too close to call: the match
  // is reported, but not with full confidence.
  ambiguous?: boolean;
}

async function get<T>(path: string): Promise<T> {
  const response = await fetch(path, {
    headers: { Accept: "application/json" },
    credentials: "include",
  });
  if (!response.ok) {
    throw new Error(`${response.status} ${await response.text()}`);
  }
  return response.json() as Promise<T>;
}

async function post<T>(path: string, body?: unknown): Promise<T> {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "include",
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) {
    throw new Error(`${response.status} ${await response.text()}`);
  }
  return response.json() as Promise<T>;
}

export const api = {
  health: () => get<Health>("/api/health"),
  me: () => get<Account>("/api/auth/me"),
  register: (email: string, password: string, name: string) =>
    post<{ id: string; email: string; name: string }>("/api/auth/register", {
      email,
      password,
      name,
    }),
  signIn: (email: string, password: string) =>
    post<{ id: string; email: string; name: string }>("/api/auth/login", {
      email,
      password,
    }),
  signOut: () => post<{ ok: boolean }>("/api/auth/logout"),
  budget: (title: string) =>
    get<BudgetRow[]>(`/api/productions/${encodeURIComponent(title)}/budget`),
  setBudget: (title: string, department: string, allocated: number, autoApprove?: number) =>
    post<{ department: string; allocated: number }>(
      `/api/productions/${encodeURIComponent(title)}/budget`,
      { department, allocated, auto_approve: autoApprove },
    ),
  addRecipient: (title: string, department: string, email: string) =>
    post<{ department: string; email: string }>(
      `/api/productions/${encodeURIComponent(title)}/recipients`,
      { department, email },
    ),
  decisions: (title: string) =>
    get<DecisionRow[]>(`/api/productions/${encodeURIComponent(title)}/decisions`),
  decide: (id: string, status: "approved" | "rejected") =>
    post<{ id: string; status: string }>(`/api/decisions/${id}`, { status }),
  productions: () => get<ProductionRow[]>("/api/productions"),
  production: (title: string) =>
    get<Production>(`/api/productions/${encodeURIComponent(title)}`),
  inventory: (title: string) =>
    get<InventoryRow[]>(`/api/productions/${encodeURIComponent(title)}/inventory`),
  trail: (title: string, element: string) =>
    get<TrailRow[]>(
      `/api/productions/${encodeURIComponent(title)}/inventory?element=${encodeURIComponent(element)}`,
    ),
  approve: (title: string) =>
    post<{ approved: number }>(`/api/productions/${encodeURIComponent(title)}/approve`),
  send: (title: string, dryRun = false) =>
    post<Record<string, unknown>>(
      `/api/productions/${encodeURIComponent(title)}/send?dry_run=${dryRun}`,
    ),
  uploadDrafts: async (before: File | null, after: File, production: string) => {
    const form = new FormData();
    if (before) form.append("before", before);
    form.append("after", after);
    const response = await fetch(
      `/api/drafts?production=${encodeURIComponent(production)}`,
      { method: "POST", body: form, credentials: "include" },
    );
    if (!response.ok) {
      throw new Error(`${response.status} ${await response.text()}`);
    }
    return response.json() as Promise<{ run_id: string; production: string }>;
  },
  startRun: (before: string, after: string, production: string) =>
    post<{ run_id: string; production: string }>("/api/run", {
      before,
      after,
      production,
    }),
  identify: async (file: File) => {
    const form = new FormData();
    form.append("file", file);
    const response = await fetch("/api/identify", {
      method: "POST",
      body: form,
      credentials: "include",
    });
    if (!response.ok) {
      throw new Error(`${response.status} ${await response.text()}`);
    }
    return response.json() as Promise<IdentifyResult>;
  },
};

/* Watch a run's Layer 3.6 events.
 *
 * The server replays the backlog on connect, so a viewer opening the page
 * mid-run sees the scenes already processed rather than an empty grid filling
 * from wherever they joined. Returns a cleanup function.
 */
export function watchRun(
  runId: string,
  onEvent: (event: RunEvent) => void,
  onClose: (status: string, error: string) => void,
): () => void {
  const source = new EventSource(`/api/runs/${runId}/stream`, {
    withCredentials: true,
  });

  const handle = (raw: MessageEvent) => {
    try {
      onEvent(JSON.parse(raw.data) as RunEvent);
    } catch {
      /* A malformed frame is not worth tearing the stream down for. */
    }
  };

  const kinds = [
    "run.started", "run.finished", "run.failed",
    "parse.started", "scene.parsed", "parse.finished", "parse.warning",
    "align.started", "scene.aligned", "change.detected",
    "element.found",
    "model.call.started", "model.call.finished", "model.fallback", "tokens.spent",
    "agent.started", "agent.step", "agent.finished",
    "ripple.opened", "ripple.step", "ripple.resolved",
    "info",
  ];
  for (const kind of kinds) source.addEventListener(kind, handle as EventListener);

  source.addEventListener("run.closed", (raw) => {
    try {
      const body = JSON.parse((raw as MessageEvent).data) as {
        status: string;
        error: string;
      };
      onClose(body.status, body.error);
    } catch {
      onClose("finished", "");
    }
    source.close();
  });

  source.onerror = () => {
    if (source.readyState === EventSource.CLOSED) onClose("failed", "connection lost");
  };

  return () => source.close();
}
