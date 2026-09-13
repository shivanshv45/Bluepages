"""The HTTP surface (Layer 7).

Two kinds of endpoint, and the split matters.

**History** is what the element database already knows: past runs, findings by
department, the inventory, an element's trail across drafts. It is served from
Postgres or SQLite and it is why the app never shows an empty screen. Idle is
"watching, here is what happened last run", never a blank page with an upload
button.

**Live** is the Layer 3.6 event stream, forwarded over SSE. Those events were
built into the pipeline from the start precisely so this layer would be a
forwarder rather than a retrofit: the CLI prints them, the browser renders them,
and no pipeline stage knows the difference.

The run itself happens on a worker thread. A pipeline run takes minutes and
holding an HTTP request open for it would tie the response lifetime to the work,
which is wrong for something the AD is meant to be able to close and come back
to.
"""

from __future__ import annotations

import asyncio
import queue
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import Cookie, FastAPI, File, HTTPException, Query, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from bluepages.config import get_settings
from bluepages.events import Event, EventKind

# How long a finished run's events stay replayable. A browser that reconnects
# after a drop, or opens the page mid-run, gets the whole run rather than
# whatever happens next.
RUN_HISTORY = 400


@dataclass
class LiveRun:
    """One pipeline run, watchable while it happens.

    Events are kept as well as broadcast. A viewer arriving halfway through a
    two-minute run should see the scenes already processed, not an empty grid
    that fills from wherever they happened to join.
    """

    id: str
    production: str
    status: str = "running"
    events: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""
    subscribers: list[queue.Queue] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def emit(self, kind: EventKind, message: str, /, **data: Any) -> None:
        """The `EventStream` protocol. The pipeline calls this."""
        payload = Event(kind=kind, message=message, data=data).to_dict()
        with self.lock:
            self.events.append(payload)
            if len(self.events) > RUN_HISTORY:
                # Keep the run's start, which carries what is being processed,
                # and drop from the middle where the noise is.
                del self.events[1 : len(self.events) - RUN_HISTORY + 1]
            listeners = list(self.subscribers)
        for listener in listeners:
            listener.put(payload)

    def finish(self, status: str, error: str = "") -> None:
        self.status = status
        self.error = error
        with self.lock:
            listeners = list(self.subscribers)
        for listener in listeners:
            listener.put(None)  # sentinel: the stream is over

    def subscribe(self) -> queue.Queue:
        listener: queue.Queue = queue.Queue()
        with self.lock:
            backlog = list(self.events)
            self.subscribers.append(listener)
        for payload in backlog:
            listener.put(payload)
        if self.status != "running":
            listener.put(None)
        return listener

    def unsubscribe(self, listener: queue.Queue) -> None:
        with self.lock:
            if listener in self.subscribers:
                self.subscribers.remove(listener)

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "production": self.production,
            "status": self.status,
            "events": len(self.events),
            "error": self.error,
        }


class Runs:
    """Every run this process knows about. In memory, deliberately.

    The durable record of a run is in the element database; this is the live
    view, and losing it on restart loses nothing that matters.
    """

    def __init__(self) -> None:
        self._runs: dict[str, LiveRun] = {}
        self._lock = threading.Lock()

    def create(self, production: str) -> LiveRun:
        run = LiveRun(id=uuid.uuid4().hex[:12], production=production)
        with self._lock:
            self._runs[run.id] = run
        return run

    def get(self, run_id: str) -> LiveRun | None:
        return self._runs.get(run_id)

    def active(self) -> list[LiveRun]:
        return [r for r in self._runs.values() if r.status == "running"]

    def all(self) -> list[LiveRun]:
        return list(self._runs.values())


def create_app(db_path: Path | None = None) -> FastAPI:
    """Build the API. `db_path` overrides the configured database, for tests."""
    app = FastAPI(
        title="Bluepages",
        description="Script revisions, routed per department.",
        version="0.1.0",
    )
    app.state.runs = Runs()
    app.state.db_path = db_path
    # Where uploaded drafts are kept. The folder watcher reads the same shape.
    app.state.workspace = Path("workspace/drafts")

    # The frontend is served from a different origin in development and from
    # static hosting in production, so it is always cross-origin.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

    _register(app)
    return app


def _previous_draft_path(app: FastAPI, title: str) -> Path | None:
    """The stored source file for a production's most recent draft.

    Returns None when the production is new, or when the recorded path no
    longer exists on disk (a database restored onto another machine).
    """
    try:
        with _open(app) as db:
            db.create_schema()
            row = db.one(
                "SELECT d.source_path FROM draft d JOIN production p "
                "ON p.id = d.production_id WHERE p.title = ? "
                "ORDER BY d.revision DESC LIMIT 1",
                (title,),
            )
    except Exception:
        return None
    if not row or not row.get("source_path"):
        return None
    path = Path(row["source_path"])
    return path if path.is_file() else None


def _claim(app: FastAPI, title: str, token: str | None) -> None:
    """Record who owns a production, once it exists. Best effort.

    The production row is written by the pipeline thread, so ownership is
    claimed after the run rather than before it. A run started signed out is
    simply unowned, reachable by title exactly as before accounts existed.
    """
    if not token:
        return
    from bluepages.api import auth

    try:
        with _open(app) as db:
            db.create_schema()
            account = auth.account_for(db, token)
            if account is None:
                return
            row = db.one("SELECT id FROM production WHERE title = ?", (title,))
            if row is not None:
                auth.claim_production(db, row["id"], account.id)
    except Exception:
        # Ownership is metadata. A failure here must never stop a run.
        pass


def _set_session(response: Response, token: str) -> None:
    """Session cookie.

    Not `secure` yet: local development runs over plain http, and a `secure`
    cookie is silently dropped there. A deployed instance behind HTTPS should
    set `secure=True` here.
    """
    response.set_cookie(
        "bp_session",
        token,
        httponly=True,
        samesite="lax",
        path="/",
        max_age=60 * 60 * 24 * 30,
    )


def _open(app: FastAPI):
    from bluepages.store import open_database

    return open_database(path=app.state.db_path)


def _register(app: FastAPI) -> None:
    from bluepages.store import Repository

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        settings = get_settings()
        return {
            "ok": True,
            "region": settings.aws_region,
            "runtime": settings.bluepages_agent_runtime,
            "models": {
                "judgment": settings.bedrock_model_judgment,
                "bulk": settings.bedrock_model_bulk,
            },
            "email": bool(settings.resend_api_key and settings.resend_from),
            "active_runs": len(app.state.runs.active()),
        }

    @app.get("/api/productions")
    def productions(bp_session: str | None = Cookie(default=None)) -> list[dict[str, Any]]:
        """The signed-in account's own productions, enough to render a list
        without a second call.

        Scoped to ownership rather than every production in the database: two
        accounts can both have a production titled "The Farm", and neither
        should see the other's. Signed out returns nothing rather than
        erroring, since Projects is only ever reached signed in.
        """
        from bluepages.api import auth

        with _open(app) as db:
            db.create_schema()
            account = auth.account_for(db, bp_session)
            if account is None:
                return []
            owned = set(auth.owned_production_ids(db, account.id))
            if not owned:
                return []
            placeholders = ",".join("?" for _ in owned)
            rows = db.query(
                "SELECT p.id, p.title, p.created_at, "
                "(SELECT COUNT(*) FROM draft d WHERE d.production_id = p.id) AS drafts "
                f"FROM production p WHERE p.id IN ({placeholders}) "
                "ORDER BY p.created_at DESC",
                tuple(owned),
            )
            repo = Repository(db)
            for row in rows:
                latest = repo.latest_draft(str(row["id"]))
                row["latest_draft"] = latest
                row["runs"] = repo.runs(str(row["id"]), limit=1)
        return rows

    @app.get("/api/productions/{title}")
    def production(
        title: str, bp_session: str | None = Cookie(default=None)
    ) -> dict[str, Any]:
        """Everything one production's screen needs, in one request.

        Deliberately one call rather than six. The AD opens this on a phone at
        the end of a shooting day, and six round trips is a visibly slower page
        for no benefit.
        """
        with _open(app) as db:
            db.create_schema()
            repo = Repository(db)
            account = _account(bp_session)
            production_id = _production_id(db, title, account.id if account else None)
            latest = repo.latest_draft(production_id)
            draft_id = str(latest["id"]) if latest else None

            reports = repo.reports_for_draft(draft_id) if draft_id else []
            for report in reports:
                report["deliveries"] = repo.deliveries(str(report["id"]))

            # The scene map is what the run view falls back to when nothing is
            # running. Without it that screen is a blank page between
            # revisions, which is the one thing the design forbids.
            scenes: list[dict[str, Any]] = []
            changes: list[dict[str, Any]] = []
            if draft_id and latest is not None:
                previous = repo.draft_before(production_id, int(latest["revision"]))
                scenes = repo.scene_map(
                    draft_id, str(previous["id"]) if previous else None
                )
                changes = repo.changes_for_draft(draft_id)

            return {
                "id": production_id,
                "title": title,
                "latest_draft": latest,
                "reports": reports,
                "runs": repo.runs(production_id, limit=20),
                "recipients": repo.recipients(production_id),
                "clearance": repo.clearance_flags(draft_id) if draft_id else [],
                "departments": repo.department_counts(draft_id) if draft_id else {},
                "scenes": scenes,
                "changes": changes,
            }

    @app.get("/api/productions/{title}/inventory")
    def inventory(
        title: str,
        element: str | None = Query(default=None),
        bp_session: str | None = Cookie(default=None),
    ) -> list[dict[str, Any]]:
        """The element database. With `element`, that object's trail instead.

        The trail is the point of tracking identity: a prop that moved is one
        row with a history, not two unrelated rows.
        """
        with _open(app) as db:
            db.create_schema()
            repo = Repository(db)
            account = _account(bp_session)
            production_id = _production_id(db, title, account.id if account else None)
            if element:
                return repo.element_history(production_id, element)
            return repo.inventory(production_id)

    @app.post("/api/productions/{title}/approve")
    def approve(title: str, bp_session: str | None = Cookie(default=None)) -> dict[str, Any]:
        """The AD's decision point. The one interruption the product allows."""
        with _open(app) as db:
            db.create_schema()
            repo = Repository(db)
            account = _account(bp_session)
            production_id = _production_id(db, title, account.id if account else None)
            latest = repo.latest_draft(production_id)
            if latest is None:
                raise HTTPException(404, f"{title} has no drafts")
            approved = repo.approve_reports(str(latest["id"]))
        return {"approved": approved}

    @app.post("/api/productions/{title}/send")
    def send(
        title: str,
        dry_run: bool = Query(default=False),
        bp_session: str | None = Cookie(default=None),
    ) -> dict[str, Any]:
        """Send the approved fan-out. Only approved reports go."""
        from bluepages.deliver import ConsoleTransport, build_transport, send_reports

        with _open(app) as db:
            db.create_schema()
            repo = Repository(db)
            account = _account(bp_session)
            production_id = _production_id(db, title, account.id if account else None)
            latest = repo.latest_draft(production_id)
            if latest is None:
                raise HTTPException(404, f"{title} has no drafts")

            transport = (
                ConsoleTransport() if dry_run else build_transport(get_settings())
            )
            result = send_reports(
                repo=repo,
                production_id=production_id,
                to_draft_id=str(latest["id"]),
                transport=transport,
                production=title,
                draft_label=f"draft {latest['revision']}",
                record=not dry_run,
            )
        return result.summary()

    # --- runs -------------------------------------------------------------

    @app.get("/api/runs")
    def runs() -> list[dict[str, Any]]:
        return [r.summary() for r in app.state.runs.all()]

    @app.get("/api/runs/{run_id}")
    def run_detail(run_id: str) -> dict[str, Any]:
        run = app.state.runs.get(run_id)
        if run is None:
            raise HTTPException(404, "no such run")
        return {**run.summary(), "events": run.events}

    @app.get("/api/runs/{run_id}/stream")
    async def stream(run_id: str):
        """The Layer 3.6 events, over SSE.

        A viewer joining mid-run receives the backlog first, so the scene grid
        shows what has already been processed rather than filling from wherever
        they happened to connect.
        """
        from sse_starlette.sse import EventSourceResponse

        run = app.state.runs.get(run_id)
        if run is None:
            raise HTTPException(404, "no such run")

        async def generator():
            listener = run.subscribe()
            loop = asyncio.get_running_loop()
            try:
                while True:
                    payload = await loop.run_in_executor(None, listener.get)
                    if payload is None:
                        yield {
                            "event": "run.closed",
                            "data": _json({"status": run.status, "error": run.error}),
                        }
                        return
                    yield {"event": payload["kind"], "data": _json(payload)}
            finally:
                run.unsubscribe(listener)

        return EventSourceResponse(generator())

    @app.post("/api/run")
    def start_run(
        payload: dict[str, Any], bp_session: str | None = Cookie(default=None)
    ) -> dict[str, Any]:
        """Start a pipeline run on a worker thread.

        Returns immediately with a run id to watch. A run takes minutes, and
        holding the request open for it would tie the response to work the AD
        is meant to be able to walk away from.
        """
        before, after = payload.get("before"), payload.get("after")
        if not before or not after:
            raise HTTPException(400, "before and after are required")

        before_path, after_path = Path(before), Path(after)
        for path in (before_path, after_path):
            if not path.is_file():
                raise HTTPException(400, f"no such file: {path}")

        production = str(payload.get("production") or before_path.stem)
        _claim(app, production, bp_session)
        run = app.state.runs.create(production)
        threading.Thread(
            target=_execute,
            args=(app, run, before_path, after_path, production),
            daemon=True,
        ).start()
        return {"run_id": run.id, "production": production}

    @app.post("/api/drafts")
    async def upload_drafts(
        after: UploadFile = File(...),
        before: UploadFile | None = File(default=None),
        production: str = Query(default=""),
        bp_session: str | None = Cookie(default=None),
    ) -> dict[str, Any]:
        """Accept a revision pair and start a run on it.

        The files are written into the workspace rather than parsed in memory,
        because the pipeline takes paths and because a production wants the
        drafts it processed kept, not thrown away after the run.
        """
        import shutil

        workspace = Path(app.state.workspace)
        workspace.mkdir(parents=True, exist_ok=True)

        def _save(upload: UploadFile) -> Path:
            name = Path(upload.filename or "draft.fdx").name
            if not name.lower().endswith((".fdx", ".pdf")):
                raise HTTPException(400, f"unsupported file type: {name}")
            target = workspace / f"{uuid.uuid4().hex[:8]}-{name}"
            with target.open("wb") as handle:
                shutil.copyfileobj(upload.file, handle)
            return target

        after_path = _save(after)
        title = production or after_path.stem

        # The usual case: a production already has drafts, so the new one is
        # diffed against the stored previous draft. Asking for both every time
        # would make the user re-upload a file the database already has.
        if before is None:
            previous = _previous_draft_path(app, title)
            if previous is None:
                raise HTTPException(
                    400,
                    "This production has no previous draft on file. "
                    "Upload the earlier draft as well for the first revision.",
                )
            before_path = previous
        else:
            before_path = _save(before)

        saved = [before_path, after_path]
        _claim(app, title, bp_session)
        run = app.state.runs.create(title)
        threading.Thread(
            target=_execute,
            args=(app, run, saved[0], saved[1], title),
            daemon=True,
        ).start()
        return {"run_id": run.id, "production": title}

    @app.post("/api/identify")
    async def identify(file: UploadFile = File(...)) -> dict[str, Any]:
        """Guess which production and scene a dropped file belongs to.

        Parsing, not a model call: scene numbers are stable across drafts by
        industry convention, so matching parsed scene numbers and headings
        against what each production already has on file is decisive and
        free. A file that matches nothing says so rather than guessing.
        """
        import shutil
        import tempfile

        from bluepages.parse import parse_script
        from bluepages.store import Repository

        filename = Path(file.filename or "draft").name
        if not filename.lower().endswith((".fdx", ".pdf")):
            return {
                "production": None,
                "production_confidence": 0.0,
                "scene": None,
                "scene_confidence": 0.0,
                "filename": filename,
            }

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / filename
            with path.open("wb") as handle:
                shutil.copyfileobj(file.file, handle)
            try:
                dropped = parse_script(path)
            except Exception:
                return {
                    "production": None,
                    "production_confidence": 0.0,
                    "scene": None,
                    "scene_confidence": 0.0,
                    "filename": filename,
                }

        dropped_numbers = {str(s.number) for s in dropped.numbered_scenes}
        dropped_by_number = {
            str(s.number): s.heading.strip().lower()
            for s in dropped.numbered_scenes
            if s.heading
        }
        dropped_headings = {s.heading.strip().lower() for s in dropped.scenes if s.heading}

        candidates: list[_IdentifyCandidate] = []
        with _open(app) as db:
            db.create_schema()
            repo = Repository(db)
            for row in db.query("SELECT id, title FROM production"):
                latest = repo.latest_draft(str(row["id"]))
                if latest is None:
                    continue
                stored = db.query(
                    "SELECT number, heading FROM scene WHERE draft_id = ?",
                    (latest["id"],),
                )
                stored_by_number = {
                    str(s["number"]): str(s["heading"] or "").strip().lower()
                    for s in stored
                    if s["number"]
                }
                stored_headings = {
                    str(s["heading"]).strip().lower() for s in stored if s["heading"]
                }
                if not stored_by_number and not stored_headings:
                    continue
                candidates.append(
                    _score_identify_candidate(
                        title=str(row["title"]),
                        dropped_numbers=dropped_numbers,
                        dropped_by_number=dropped_by_number,
                        dropped_headings=dropped_headings,
                        stored_by_number=stored_by_number,
                        stored_headings=stored_headings,
                    )
                )

        result = _pick_identify_result(candidates)
        result["filename"] = filename
        return result

    # --- accounts (Layer 10) ----------------------------------------------

    def _account(token: str | None):
        """The signed-in account, or None. Never raises."""
        from bluepages.api import auth

        with _open(app) as db:
            db.create_schema()
            return auth.account_for(db, token)

    def _require_account(token: str | None):
        account = _account(token)
        if account is None:
            raise HTTPException(401, "Sign in to continue.")
        return account

    @app.post("/api/auth/register")
    def register(payload: dict[str, Any], response: Response) -> dict[str, Any]:
        from bluepages.api import auth
        from bluepages.agents import budget as budget_log
        from bluepages.store.db import new_id, now

        with _open(app) as db:
            db.create_schema()
            try:
                account = auth.register(
                    db,
                    str(payload.get("email", "")),
                    str(payload.get("password", "")),
                    str(payload.get("name", "")),
                )
            except auth.AuthError as exc:
                raise HTTPException(400, str(exc)) from exc
            token = auth.start_session(db, account.id)

            # Something to open immediately after signing up, in a raw state:
            # no drafts, no runs, budget rows only. Inserted directly rather
            # than through Repository.ensure_production, which matches on
            # title first and would hand every new account the same row the
            # first "The Farm" signup created. Every account gets its own.
            production_id = new_id()
            db.execute(
                "INSERT INTO production (id, title, created_at) VALUES (?, ?, ?)",
                (production_id, "The Farm", now()),
            )
            auth.claim_production(db, production_id, account.id)
            budget_log.ensure_defaults(db, production_id)

        _set_session(response, token)
        return {"id": account.id, "email": account.email, "name": account.name}

    @app.post("/api/auth/login")
    def login(payload: dict[str, Any], response: Response) -> dict[str, Any]:
        from bluepages.api import auth

        with _open(app) as db:
            db.create_schema()
            try:
                account = auth.login(
                    db, str(payload.get("email", "")), str(payload.get("password", ""))
                )
            except auth.AuthError as exc:
                raise HTTPException(401, str(exc)) from exc
            token = auth.start_session(db, account.id)

        _set_session(response, token)
        return {"id": account.id, "email": account.email, "name": account.name}

    @app.post("/api/auth/logout")
    def logout(response: Response, bp_session: str | None = Cookie(default=None)) -> dict[str, Any]:
        from bluepages.api import auth

        if bp_session:
            with _open(app) as db:
                db.create_schema()
                auth.end_session(db, bp_session)
        response.delete_cookie("bp_session", path="/")
        return {"ok": True}

    @app.get("/api/auth/me")
    def me(bp_session: str | None = Cookie(default=None)) -> dict[str, Any]:
        account = _account(bp_session)
        if account is None:
            return {"signed_in": False}
        return {
            "signed_in": True,
            "id": account.id,
            "email": account.email,
            "name": account.name,
        }

    # --- decisions (Layer 10) ---------------------------------------------

    @app.get("/api/productions/{title}/decisions")
    def decisions(
        title: str, bp_session: str | None = Cookie(default=None)
    ) -> list[dict[str, Any]]:
        """What the agent decided on this production's last runs.

        `auto_approved` is lifted out of the JSON payload onto the row itself,
        so the frontend can tell an auto-clearance from a human approval
        without parsing the payload string first.
        """
        import json

        from bluepages.agents import decisions as decision_log

        with _open(app) as db:
            db.create_schema()
            account = _account(bp_session)
            production_id = _production_id(db, title, account.id if account else None)
            rows = decision_log.for_production(db, production_id)

        for row in rows:
            payload = row.get("payload")
            if not payload:
                continue
            try:
                data = json.loads(payload)
            except ValueError:
                continue
            if data.get("auto_approved"):
                row["auto_approved"] = 1
        return rows

    @app.post("/api/decisions/{decision_id}")
    def decide(decision_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Approve or reject one decision the agent is holding."""
        from bluepages.agents import decisions as decision_log

        status = str(payload.get("status", ""))
        if status not in {"approved", "rejected"}:
            raise HTTPException(400, "status must be approved or rejected")

        with _open(app) as db:
            db.create_schema()
            if not decision_log.set_status(db, decision_id, status):
                raise HTTPException(404, "no such decision")
        return {"id": decision_id, "status": status}

    @app.get("/api/productions/{title}/budget")
    def budget(
        title: str, bp_session: str | None = Cookie(default=None)
    ) -> list[dict[str, Any]]:
        """Allocation, committed and at-risk per department."""
        from bluepages.agents import budget as budget_log

        with _open(app) as db:
            db.create_schema()
            account = _account(bp_session)
            production_id = _production_id(db, title, account.id if account else None)
            return budget_log.summary(db, production_id)

    @app.post("/api/productions/{title}/budget")
    def set_budget(
        title: str,
        payload: dict[str, Any],
        bp_session: str | None = Cookie(default=None),
    ) -> dict[str, Any]:
        from bluepages.agents import budget as budget_log

        department = str(payload.get("department", ""))
        try:
            amount = float(payload.get("allocated", 0))
        except (TypeError, ValueError):
            raise HTTPException(400, "allocated must be a number") from None
        if not department or amount < 0:
            raise HTTPException(400, "department and a non-negative amount are required")

        auto_approve = payload.get("auto_approve")
        if auto_approve is not None:
            try:
                auto_approve = float(auto_approve)
            except (TypeError, ValueError):
                raise HTTPException(400, "auto_approve must be a number") from None
            if auto_approve < 0:
                raise HTTPException(400, "auto_approve must not be negative")

        with _open(app) as db:
            db.create_schema()
            account = _account(bp_session)
            production_id = _production_id(db, title, account.id if account else None)
            budget_log.set_allocation(db, production_id, department, amount, auto_approve)
        return {"department": department, "allocated": amount, "auto_approve": auto_approve}

    @app.post("/api/productions/{title}/recipients")
    def add_recipient(
        title: str,
        payload: dict[str, Any],
        bp_session: str | None = Cookie(default=None),
    ) -> dict[str, Any]:
        """Who a department's brief is emailed to."""
        from bluepages.store import Repository

        department = str(payload.get("department", ""))
        email = str(payload.get("email", "")).strip()
        if not department or "@" not in email:
            raise HTTPException(400, "department and a valid email are required")

        with _open(app) as db:
            db.create_schema()
            repo = Repository(db)
            account = _account(bp_session)
            production_id = _production_id(db, title, account.id if account else None)
            repo.add_recipient(
                production_id,
                department,
                email,
                str(payload.get("name", "")) or email.split("@")[0],
            )
        return {"department": department, "email": email}

    @app.delete("/api/productions/{title}/recipients")
    def drop_recipient(
        title: str,
        email: str = Query(...),
        bp_session: str | None = Cookie(default=None),
    ) -> dict[str, Any]:
        from bluepages.store import Repository

        with _open(app) as db:
            db.create_schema()
            account = _account(bp_session)
            production_id = _production_id(db, title, account.id if account else None)
            removed = Repository(db).remove_recipient(production_id, email)
        return {"removed": removed}

    @app.exception_handler(LookupError)
    def _not_found(_request, exc: LookupError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=404)


def _execute(
    app: FastAPI, run: LiveRun, before: Path, after: Path, production: str
) -> None:
    """One pipeline run, on a worker thread, reporting through the event stream."""
    from bluepages.llm import ModelClient, RunBudget
    from bluepages.pipeline import run_pipeline
    from bluepages.pipeline.run import persist

    try:
        settings = get_settings()
        budget = RunBudget(max_calls=settings.bluepages_max_llm_calls_per_run)
        client = ModelClient(settings=settings, stream=run, budget=budget)
        result = run_pipeline(
            before, after, client=client, stream=run, production=production
        )
        persist(result, db_path=app.state.db_path)
        run.finish("finished")
    except Exception as exc:
        # Reported through the stream as well as recorded: a browser watching a
        # run that dies should see why, not simply stop receiving events.
        run.emit(
            EventKind.RUN_FAILED,
            f"{type(exc).__name__}: {exc}",
            error=type(exc).__name__,
        )
        run.finish("failed", f"{type(exc).__name__}: {exc}")


@dataclass
class _IdentifyCandidate:
    """How well one production's latest draft matches a dropped file.

    Three signals, not one: scene-number overlap, heading-text overlap, and
    agreement (a scene where the dropped file's number AND heading both match
    the same stored scene). Agreement is weighted heaviest because it is the
    only signal that cannot be coincidence: scene numbers alone repeat across
    unrelated scripts (nearly every screenplay has a "scene 7"), so number
    overlap alone must never be enough to name a production with confidence.
    """

    title: str
    number_score: float
    heading_score: float
    agreement: set[str] = field(default_factory=set)
    agreed_scene: str | None = None
    number_overlap: set[str] = field(default_factory=set)
    heading_overlap: set[str] = field(default_factory=set)

    @property
    def score(self) -> float:
        agreement_score = 1.0 if self.agreement else 0.0
        return 0.25 * self.number_score + 0.35 * self.heading_score + 0.40 * agreement_score

    @property
    def best_scene(self) -> str | None:
        if self.agreed_scene:
            return self.agreed_scene
        if self.number_overlap:
            return sorted(self.number_overlap)[0]
        if self.heading_overlap:
            return sorted(self.heading_overlap)[0]
        return None


def _score_identify_candidate(
    title: str,
    dropped_numbers: set[str],
    dropped_by_number: dict[str, str],
    dropped_headings: set[str],
    stored_by_number: dict[str, str],
    stored_headings: set[str],
) -> _IdentifyCandidate:
    number_overlap = dropped_numbers & set(stored_by_number)
    heading_overlap = dropped_headings & stored_headings
    agreement = {
        number
        for number, heading in dropped_by_number.items()
        if heading and stored_by_number.get(number) == heading
    }
    return _IdentifyCandidate(
        title=title,
        number_score=len(number_overlap) / max(len(dropped_numbers), 1),
        heading_score=len(heading_overlap) / max(len(dropped_headings), 1),
        agreement=agreement,
        agreed_scene=sorted(agreement)[0] if agreement else None,
        number_overlap=number_overlap,
        heading_overlap=heading_overlap,
    )


# Below this many scenes, number overlap with no heading corroboration is not
# distinctive: a single "scene 7" matches most scripts on file. Declining to
# guess here is what fixes the drop-one-page case, which is the advertised use
# of this endpoint.
_FEW_SCENES_NEEDING_CORROBORATION = 2

# When the runner-up's score is this close to the best, the two productions
# are not distinguishable enough to report a confident single answer.
_AMBIGUITY_MARGIN = 0.10


def _pick_identify_result(candidates: list[_IdentifyCandidate]) -> dict[str, Any]:
    """The best-matching production and scene, or an honest decline.

    Never a model call: scene numbers and headings are stable across drafts by
    industry convention, so this is a lookup, not a judgment. A drop that
    cannot be told apart from another production says so rather than guessing.
    """
    no_match = {
        "production": None,
        "production_confidence": 0.0,
        "scene": None,
        "scene_confidence": 0.0,
    }
    scored = sorted((c for c in candidates if c.score > 0), key=lambda c: c.score, reverse=True)
    if not scored:
        return no_match

    best = scored[0]
    few_scenes = len(best.number_overlap) + len(best.heading_overlap) <= (
        _FEW_SCENES_NEEDING_CORROBORATION
    )
    if few_scenes and not best.agreement:
        # A handful of bare scene numbers with nothing else to corroborate
        # them is not distinctive enough to name a production.
        return no_match

    confidence = min(best.score, 1.0)
    ambiguous = len(scored) > 1 and (best.score - scored[1].score) < _AMBIGUITY_MARGIN
    if ambiguous:
        confidence = min(confidence, 0.5)

    return {
        "production": best.title,
        "production_confidence": round(confidence, 2),
        "scene": best.best_scene,
        "scene_confidence": round(confidence, 2) if best.best_scene else 0.0,
        "ambiguous": ambiguous,
    }


def _production_id(db: Any, title: str, account_id: str | None = None) -> str:
    """Resolve a production by title, preferring the requesting account's own.

    Two accounts can each have a production titled "The Farm" since every new
    signup gets its own by that name. A bare title match with no account
    filter would be ambiguous between them and could leak one account's
    budget, decisions or recipients onto another's screen. When an account is
    given and it owns a matching title, that row wins; otherwise this falls
    back to the old bare match, which is what keeps productions created
    before accounts existed (or by a signed-out run) reachable exactly as
    before.
    """
    if account_id is not None:
        row = db.one(
            "SELECT p.id FROM production p "
            "JOIN production_owner o ON o.production_id = p.id "
            "WHERE p.title = ? AND o.account_id = ?",
            (title, account_id),
        )
        if row is not None:
            return str(row["id"])
    row = db.one("SELECT id FROM production WHERE title = ?", (title,))
    if row is None:
        raise HTTPException(404, f"no production named {title!r}")
    return str(row["id"])


def _json(payload: Any) -> str:
    import json

    return json.dumps(payload)


app = create_app()

__all__ = ["LiveRun", "Runs", "app", "create_app"]
