"""The HTTP surface (Layer 7).

What matters here is the split the app rests on. History comes from the element
database and is why no screen is ever empty. Live runs come from the Layer 3.6
event stream, forwarded rather than reimplemented, and a viewer joining a run
halfway through gets the backlog rather than an empty grid.

The approval gate is asserted here too. It has to hold at every entrance, and
this is a third one after the CLI and the trigger.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bluepages.api import create_app
from bluepages.api.app import LiveRun
from bluepages.events import EventKind
from tests.unit.fakes import ScriptedClient, elements_answer
from tests.unit.test_cli_fanout import DEPARTMENT_RULES
from tests.unit.test_cli_reason import CORRECT_RULES
from tests.unit.test_cli_store import ELEMENT_RULES

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
DRAFT1 = FIXTURES / "small-draft-1.fdx"
DRAFT2 = FIXTURES / "small-draft-2.fdx"
ALL_RULES = ELEMENT_RULES + CORRECT_RULES + DEPARTMENT_RULES


@pytest.fixture(autouse=True)
def no_real_resend(monkeypatch) -> None:
    """A real RESEND_API_KEY in .env must never make this suite's send tests
    depend on the network or an account's Cloudflare posture. pydantic-settings
    reads .env directly, so the settings singleton itself has to say there is
    no key rather than relying on the environment variable being absent."""
    from bluepages.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "resend_api_key", None)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "e.db"


@pytest.fixture
def client(db_path: Path) -> TestClient:
    return TestClient(create_app(db_path=db_path))


@pytest.fixture
def seeded(db_path: Path, monkeypatch) -> Path:
    """A production with one persisted, unapproved fan-out."""
    scripted = ScriptedClient(rules=ALL_RULES, default=elements_answer())
    monkeypatch.setattr("bluepages.llm.ModelClient", lambda *a, **k: scripted)

    from bluepages.pipeline import run_pipeline
    from bluepages.pipeline.run import persist

    result = run_pipeline(DRAFT1, DRAFT2, client=scripted, production="The Farm")
    persist(result, db_path=db_path)
    return db_path


# --- health ---------------------------------------------------------------


def test_health_reports_the_configuration(client: TestClient) -> None:
    body = client.get("/api/health").json()

    assert body["ok"] is True
    assert body["runtime"] in {"strands", "boto3"}
    assert "judgment" in body["models"]


# --- history --------------------------------------------------------------


def test_an_empty_database_lists_no_productions(client: TestClient) -> None:
    """Empty is a valid answer here. What must never be empty is the screen,
    and that is the frontend's job with this data."""
    assert client.get("/api/productions").json() == []


def test_an_unowned_production_is_not_listed(seeded: Path, client: TestClient) -> None:
    """The list is account-scoped. A production created outside the API (no
    signup, no session) has no owner and is invisible to it, the same way it
    always was to any signed-out visitor."""
    assert client.get("/api/productions").json() == []


def test_a_production_is_listed_for_the_account_that_owns_it(
    seeded: Path, client: TestClient
) -> None:
    client.post(
        "/api/auth/register",
        json={"email": "ad@thefarm.film", "password": "call-sheet-1"},
    )
    from bluepages.store import Repository, open_database

    with open_database(path=seeded) as db:
        production_id = Repository(db).ensure_production("The Farm")
        account_id = db.one("SELECT id FROM account WHERE email = ?", ("ad@thefarm.film",))[
            "id"
        ]
        from bluepages.api import auth

        auth.claim_production(db, production_id, account_id)

    # Signup already gave this account its own fresh "The Farm"; claiming the
    # seeded one adds a second, real revision alongside it.
    rows = client.get("/api/productions").json()
    with_drafts = next(r for r in rows if r["drafts"] == 2)
    assert with_drafts["title"] == "The Farm"
    assert with_drafts["latest_draft"]["revision"] == 2


def test_one_account_never_sees_another_accounts_production(
    seeded: Path, client: TestClient
) -> None:
    """Two signups, each with their own default production of the same
    title, must not leak into each other's list."""
    client.post(
        "/api/auth/register", json={"email": "a@thefarm.film", "password": "call-sheet-1"}
    )
    client.cookies.clear()
    client.post(
        "/api/auth/register", json={"email": "b@thefarm.film", "password": "call-sheet-2"}
    )

    rows = client.get("/api/productions").json()
    assert len(rows) == 1, "each account should only see its own default production"


# --- signup's default production -------------------------------------------


def test_signup_gets_a_raw_default_production(client: TestClient) -> None:
    """Something to open immediately, in a raw state: budget rows present,
    nothing else. Not a shared fixture; this account's own row."""
    client.post(
        "/api/auth/register", json={"email": "new@thefarm.film", "password": "call-sheet-1"}
    )

    [row] = client.get("/api/productions").json()
    assert row["title"] == "The Farm"
    assert row["drafts"] == 0
    assert row["latest_draft"] is None

    budget = client.get("/api/productions/The Farm/budget").json()
    assert budget, "a fresh production must not have an empty budget screen"
    assert all(b["allocated"] > 0 for b in budget)


def test_two_signups_each_get_their_own_farm_not_a_shared_one(
    db_path: Path, client: TestClient
) -> None:
    """Every account's default production is titled 'The Farm', but each is
    its own database row. A bare title match must never hand two accounts
    the same production."""
    client.post(
        "/api/auth/register", json={"email": "a@thefarm.film", "password": "call-sheet-1"}
    )
    client.cookies.clear()
    client.post(
        "/api/auth/register", json={"email": "b@thefarm.film", "password": "call-sheet-2"}
    )

    from bluepages.store import open_database

    with open_database(path=db_path) as db:
        rows = db.query("SELECT id FROM production WHERE title = ?", ("The Farm",))

    assert len({r["id"] for r in rows}) == 2, "each signup must create its own row"


def test_a_production_lookup_resolves_to_the_requesting_accounts_own_row(
    client: TestClient,
) -> None:
    """The real risk this whole scoping exists for: two accounts sharing a
    title must never see or modify each other's budget through it."""
    client.post(
        "/api/auth/register", json={"email": "a@thefarm.film", "password": "call-sheet-1"}
    )
    client.post(
        "/api/productions/The Farm/budget",
        json={"department": "props", "allocated": 1234, "auto_approve": 0},
    )
    client.cookies.clear()

    client.post(
        "/api/auth/register", json={"email": "b@thefarm.film", "password": "call-sheet-2"}
    )
    b_budget = client.get("/api/productions/The Farm/budget").json()
    b_props = next(r for r in b_budget if r["department"] == "props")

    assert b_props["allocated"] != 1234, "account B must not see account A's budget edit"


def test_one_call_returns_everything_a_screen_needs(
    seeded: Path, client: TestClient
) -> None:
    """Six round trips is a visibly slower page on a phone, for no benefit."""
    body = client.get("/api/productions/The Farm").json()

    assert body["title"] == "The Farm"
    assert body["reports"], "no department reports"
    assert body["runs"], "no run history"
    assert "clearance" in body
    assert "departments" in body


def test_an_unknown_production_is_a_404(client: TestClient) -> None:
    assert client.get("/api/productions/Nope").status_code == 404


def test_the_inventory_is_served(seeded: Path, client: TestClient) -> None:
    rows = client.get("/api/productions/The Farm/inventory").json()

    assert rows
    assert "name" in rows[0]


def test_an_element_trail_is_served(seeded: Path, client: TestClient) -> None:
    """One row with a history, not two unrelated rows. The point of identity."""
    rows = client.get(
        "/api/productions/The Farm/inventory",
        params={"element": "brass letter opener"},
    ).json()

    assert rows, "the letter opener has no trail"
    assert "scene" in rows[0]


def test_a_recipient_stores_the_email_in_the_email_column(
    seeded: Path, client: TestClient
) -> None:
    """Regression: add_recipient's args were once passed in the wrong order,
    so the name landed in the email column and delivery mailed nobody."""
    response = client.post(
        "/api/productions/The Farm/recipients",
        json={"department": "props", "email": "props@thefarm.film", "name": "Dana Ruiz"},
    )
    assert response.status_code == 200

    body = client.get("/api/productions/The Farm").json()
    row = next(r for r in body["recipients"] if r["department"] == "props")
    assert row["email"] == "props@thefarm.film"
    assert row["name"] == "Dana Ruiz"


# --- the gate -------------------------------------------------------------


def test_reports_arrive_unapproved(seeded: Path, client: TestClient) -> None:
    body = client.get("/api/productions/The Farm").json()

    assert all(r["approved_at"] is None for r in body["reports"])


def test_sending_before_approving_sends_nothing(
    seeded: Path, client: TestClient
) -> None:
    """The gate holds at the HTTP entrance too."""
    body = client.post("/api/productions/The Farm/send").json()

    assert body["delivered"] == 0
    assert body["skipped_unapproved"] > 0


def test_approving_then_sending_delivers(seeded: Path, client: TestClient) -> None:
    from bluepages.store import Repository, open_database

    with open_database(path=seeded) as db:
        repo = Repository(db)
        production_id = repo.ensure_production("The Farm")
        repo.add_recipient(production_id, "props", "props@example.com")

    approved = client.post("/api/productions/The Farm/approve").json()
    assert approved["approved"] > 0

    sent = client.post("/api/productions/The Farm/send").json()
    assert sent["delivered"] == 1


def test_a_dry_run_send_records_nothing(seeded: Path, client: TestClient) -> None:
    from bluepages.store import Repository, open_database

    with open_database(path=seeded) as db:
        repo = Repository(db)
        repo.add_recipient(
            repo.ensure_production("The Farm"), "props", "props@example.com"
        )
    client.post("/api/productions/The Farm/approve")

    client.post("/api/productions/The Farm/send", params={"dry_run": True})
    real = client.post("/api/productions/The Farm/send").json()

    assert real["delivered"] == 1, "the dry run marked it sent"


# --- live runs ------------------------------------------------------------


def test_a_run_starts_and_reports_an_id(
    client: TestClient, monkeypatch, db_path: Path
) -> None:
    scripted = ScriptedClient(rules=ALL_RULES, default=elements_answer())
    monkeypatch.setattr("bluepages.llm.ModelClient", lambda *a, **k: scripted)

    body = client.post(
        "/api/run",
        json={"before": str(DRAFT1), "after": str(DRAFT2), "production": "The Farm"},
    ).json()

    assert body["run_id"]
    assert body["production"] == "The Farm"


def test_a_run_finishes_and_its_events_are_readable(
    client: TestClient, monkeypatch, db_path: Path
) -> None:
    import time

    scripted = ScriptedClient(rules=ALL_RULES, default=elements_answer())
    monkeypatch.setattr("bluepages.llm.ModelClient", lambda *a, **k: scripted)

    run_id = client.post(
        "/api/run",
        json={"before": str(DRAFT1), "after": str(DRAFT2), "production": "The Farm"},
    ).json()["run_id"]

    deadline = time.time() + 30
    while time.time() < deadline:
        body = client.get(f"/api/runs/{run_id}").json()
        if body["status"] != "running":
            break
        time.sleep(0.1)

    assert body["status"] == "finished", body.get("error")
    kinds = {e["kind"] for e in body["events"]}
    assert "run.started" in kinds
    assert "change.detected" in kinds


def test_a_missing_file_is_rejected_before_starting(client: TestClient) -> None:
    response = client.post(
        "/api/run", json={"before": "nope.fdx", "after": str(DRAFT2)}
    )
    assert response.status_code == 400


def test_a_run_needs_both_drafts(client: TestClient) -> None:
    assert client.post("/api/run", json={"before": str(DRAFT1)}).status_code == 400


def test_an_unknown_run_is_a_404(client: TestClient) -> None:
    assert client.get("/api/runs/nope").status_code == 404
    assert client.get("/api/runs/nope/stream").status_code == 404


# --- the live run object --------------------------------------------------


def test_a_late_subscriber_receives_the_backlog() -> None:
    """A viewer joining a two-minute run halfway through must see the scenes
    already processed, not an empty grid filling from wherever they joined."""
    run = LiveRun(id="r1", production="The Farm")
    run.emit(EventKind.RUN_STARTED, "started")
    run.emit(EventKind.SCENE_PARSED, "scene 1")

    listener = run.subscribe()

    assert listener.get_nowait()["kind"] == "run.started"
    assert listener.get_nowait()["kind"] == "scene.parsed"


def test_a_subscriber_receives_live_events() -> None:
    run = LiveRun(id="r1", production="The Farm")
    listener = run.subscribe()

    run.emit(EventKind.CHANGE_DETECTED, "scene 7 changed")

    assert listener.get_nowait()["message"] == "scene 7 changed"


def test_finishing_closes_every_stream() -> None:
    """A browser waiting on a finished run must not hang."""
    run = LiveRun(id="r1", production="The Farm")
    listener = run.subscribe()

    run.finish("finished")

    assert listener.get_nowait() is None


def test_subscribing_to_a_finished_run_ends_immediately() -> None:
    run = LiveRun(id="r1", production="The Farm")
    run.emit(EventKind.RUN_STARTED, "started")
    run.finish("finished")

    listener = run.subscribe()

    assert listener.get_nowait()["kind"] == "run.started"
    assert listener.get_nowait() is None


def test_the_backlog_is_capped_but_keeps_the_start() -> None:
    """A feature-length run emits thousands of events. The first one says what
    is being processed, so it is the one worth keeping."""
    from bluepages.api.app import RUN_HISTORY

    run = LiveRun(id="r1", production="The Farm")
    run.emit(EventKind.RUN_STARTED, "the run began")
    for i in range(RUN_HISTORY + 50):
        run.emit(EventKind.SCENE_PARSED, f"scene {i}")

    assert len(run.events) <= RUN_HISTORY
    assert run.events[0]["message"] == "the run began"


def test_unsubscribing_stops_delivery() -> None:
    run = LiveRun(id="r1", production="The Farm")
    listener = run.subscribe()
    run.unsubscribe(listener)

    run.emit(EventKind.INFO, "after")

    assert listener.empty()


# --- identify (Layer 7 drop capture) ---------------------------------------


def test_identifying_the_drafts_own_second_draft_matches_its_production(
    seeded: Path, client: TestClient
) -> None:
    """DRAFT2 is already on file as The Farm's latest revision. Dropping the
    same file again should match it by scene number, no model call."""
    with DRAFT2.open("rb") as handle:
        response = client.post(
            "/api/identify", files={"file": ("small-draft-2.fdx", handle, "application/xml")}
        )

    body = response.json()
    assert body["production"] == "The Farm"
    assert body["production_confidence"] > 0
    assert body["filename"] == "small-draft-2.fdx"


def test_an_unrecognised_file_reports_no_match(client: TestClient) -> None:
    """An empty database has nothing to match against."""
    with DRAFT1.open("rb") as handle:
        response = client.post(
            "/api/identify", files={"file": ("small-draft-1.fdx", handle, "application/xml")}
        )

    body = response.json()
    assert body["production"] is None
    assert body["production_confidence"] == 0.0


def test_a_bare_scene_number_with_no_corroboration_is_not_enough(client: TestClient) -> None:
    """The advertised use case is dropping one page. If it carries only a
    scene number like '7', which exists in nearly every script, that alone
    must not be enough to confidently name a production: it is coincidence,
    not evidence."""
    from bluepages.api.app import _pick_identify_result, _score_identify_candidate

    candidate = _score_identify_candidate(
        title="Some Other Production",
        dropped_numbers={"7"},
        dropped_by_number={},  # no heading captured for the dropped page
        dropped_headings=set(),
        stored_by_number={"7": "int. kitchen - day"},
        stored_headings={"int. kitchen - day"},
    )
    result = _pick_identify_result([candidate])

    assert result["production"] is None
    assert result["production_confidence"] == 0.0


def test_a_scene_number_with_a_matching_heading_is_decisive(client: TestClient) -> None:
    """Number and heading agreeing on the same scene is the strong signal: a
    single dropped page can still be identified when its heading corroborates
    the number."""
    from bluepages.api.app import _pick_identify_result, _score_identify_candidate

    candidate = _score_identify_candidate(
        title="The Farm",
        dropped_numbers={"7"},
        dropped_by_number={"7": "int. sheriff's office - day"},
        dropped_headings={"int. sheriff's office - day"},
        stored_by_number={"7": "int. sheriff's office - day"},
        stored_headings={"int. sheriff's office - day"},
    )
    result = _pick_identify_result([candidate])

    assert result["production"] == "The Farm"
    assert result["scene"] == "7"
    assert result["production_confidence"] > 0.5


def test_two_productions_sharing_a_scene_number_but_not_a_heading_are_told_apart(
    client: TestClient,
) -> None:
    """Scene 7 exists in both productions, but only one's heading matches the
    dropped page. The bare number overlap must not win on its own."""
    from bluepages.api.app import _pick_identify_result, _score_identify_candidate

    dropped_numbers = {"7"}
    dropped_by_number = {"7": "int. sheriff's office - day"}
    dropped_headings = {"int. sheriff's office - day"}

    wrong = _score_identify_candidate(
        title="Wrong Production",
        dropped_numbers=dropped_numbers,
        dropped_by_number=dropped_by_number,
        dropped_headings=dropped_headings,
        stored_by_number={"7": "ext. parking lot - night"},
        stored_headings={"ext. parking lot - night"},
    )
    right = _score_identify_candidate(
        title="The Farm",
        dropped_numbers=dropped_numbers,
        dropped_by_number=dropped_by_number,
        dropped_headings=dropped_headings,
        stored_by_number={"7": "int. sheriff's office - day"},
        stored_headings={"int. sheriff's office - day"},
    )
    result = _pick_identify_result([wrong, right])

    assert result["production"] == "The Farm"
    assert result.get("ambiguous", False) is False


def test_two_equally_strong_candidates_are_reported_as_ambiguous(client: TestClient) -> None:
    """When nothing tells two productions apart, confidence must reflect that
    rather than picking one arbitrarily and calling it certain."""
    from bluepages.api.app import _pick_identify_result, _score_identify_candidate

    dropped_numbers = {"7"}
    dropped_by_number = {"7": "int. sheriff's office - day"}
    dropped_headings = {"int. sheriff's office - day"}

    a = _score_identify_candidate(
        title="Production A",
        dropped_numbers=dropped_numbers,
        dropped_by_number=dropped_by_number,
        dropped_headings=dropped_headings,
        stored_by_number={"7": "int. sheriff's office - day"},
        stored_headings={"int. sheriff's office - day"},
    )
    b = _score_identify_candidate(
        title="Production B",
        dropped_numbers=dropped_numbers,
        dropped_by_number=dropped_by_number,
        dropped_headings=dropped_headings,
        stored_by_number={"7": "int. sheriff's office - day"},
        stored_headings={"int. sheriff's office - day"},
    )
    result = _pick_identify_result([a, b])

    assert result["ambiguous"] is True
    assert result["production_confidence"] <= 0.5


def test_an_unsupported_file_type_is_rejected_without_parsing(client: TestClient) -> None:
    import io

    response = client.post(
        "/api/identify",
        files={"file": ("notes.txt", io.BytesIO(b"hello"), "text/plain")},
    )

    assert response.status_code == 200
    assert response.json()["production"] is None
