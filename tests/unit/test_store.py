"""The element database (Layer 4).

Most of this is row writing and is tested for the obvious things. The tests
that earn their place are the identity ones: whether the brass letter opener in
draft 2 scene 7 is recorded as the same object that was in draft 1 scene 3, or
as a second object. That distinction is what separates an inventory from a pile
of diffs, and it is the thing PLAN.md calls the subtle part.

Every test runs against in-memory SQLite, so persistence is exercised on every
test run rather than only when a cloud database happens to be reachable.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bluepages.diff import align, diff_drafts
from bluepages.parse import parse_fdx
from bluepages.semantic.elements import (
    DraftElements,
    ElementCategory,
    ExtractedElement,
    SceneExtraction,
)
from bluepages.semantic.reasoning import Finding, SemanticResult
from bluepages.store import Database, Repository
from bluepages.testdata import ChangeKind, Department

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


@pytest.fixture
def db():
    with Database() as connection:
        connection.create_schema()
        yield connection


@pytest.fixture
def repo(db):
    return Repository(db)


@pytest.fixture
def drafts():
    return (
        parse_fdx(FIXTURES / "small-draft-1.fdx"),
        parse_fdx(FIXTURES / "small-draft-2.fdx"),
    )


def _elements(scene: str, *names: str, category=ElementCategory.PROP) -> DraftElements:
    return DraftElements(
        scenes=[
            SceneExtraction(
                scene_number=scene,
                elements=[ExtractedElement(name=n, category=category) for n in names],
            )
        ]
    )


# --- schema ---------------------------------------------------------------


def test_schema_creates_every_table(db):
    assert db.tables() == {
        "production",
        "draft",
        "scene",
        "element",
        "element_identity",
        "change",
        "change_department",
        "report",
        "report_note",
        "recipient",
        "delivery",
        "run",
        # Layer 10: accounts and the decision log.
        "account",
        "session",
        "production_owner",
        "decision",
        "budget",
    }


def test_schema_is_idempotent(db):
    """It runs on every connect, so a second application must not fail."""
    db.create_schema()
    assert "element_identity" in db.tables()


def test_foreign_keys_cascade(db, repo, drafts):
    """SQLite ignores ON DELETE CASCADE unless the pragma is on.

    Without it a deleted draft leaves orphan scenes behind, and the scene count
    for a production drifts upward every time a draft is re-ingested.
    """
    before, _ = drafts
    pid = repo.ensure_production("Cascade Test")
    draft_id = repo.save_draft(pid, before)
    assert db.query("SELECT id FROM scene WHERE draft_id = ?", (draft_id,))

    db.execute("DELETE FROM draft WHERE id = ?", (draft_id,))
    assert db.query("SELECT id FROM scene WHERE draft_id = ?", (draft_id,)) == []


# --- productions and drafts -----------------------------------------------


def test_ensure_production_is_stable(repo):
    """Ingesting a second draft must not create a second production."""
    first = repo.ensure_production("The Farm")
    second = repo.ensure_production("The Farm")
    assert first == second


def test_revisions_auto_increment(repo, drafts):
    before, after = drafts
    pid = repo.ensure_production("The Farm")
    repo.save_draft(pid, before)
    repo.save_draft(pid, after)

    latest = repo.latest_draft(pid)
    assert latest is not None
    assert latest["revision"] == 2


def test_scenes_persist_with_their_heading_fields(repo, drafts, db):
    """Locations and scheduling read these, so they are columns, not blobs."""
    _, after = drafts
    pid = repo.ensure_production("The Farm")
    draft_id = repo.save_draft(pid, after)

    row = db.one(
        "SELECT * FROM scene WHERE draft_id = ? AND number = ?", (draft_id, "2")
    )
    assert row is not None
    assert row["time_of_day"] == "NIGHT"
    assert row["int_ext"] == "INT"
    assert "FARMHOUSE KITCHEN" in row["location"]


def test_omitted_scene_is_recorded_as_omitted(repo, drafts, db):
    """The scene number survives, which is the premise alignment rests on."""
    _, after = drafts
    pid = repo.ensure_production("The Farm")
    draft_id = repo.save_draft(pid, after)

    row = db.one(
        "SELECT omitted FROM scene WHERE draft_id = ? AND number = ?", (draft_id, "5")
    )
    assert row is not None
    assert row["omitted"] == 1


def test_reingesting_a_revision_replaces_it(repo, drafts, db):
    """Re-running a draft must not double its scenes."""
    _, after = drafts
    pid = repo.ensure_production("The Farm")
    repo.save_draft(pid, after, revision=1)
    repo.save_draft(pid, after, revision=1)

    rows = db.query("SELECT id FROM draft WHERE production_id = ?", (pid,))
    assert len(rows) == 1


# --- element identity, the subtle part ------------------------------------


def test_the_same_element_in_two_drafts_is_one_identity(repo, drafts):
    """The letter opener moving 3 -> 7 is one object, not two.

    This is the whole reason the identity table exists. Two rows in `element`,
    one row in `element_identity`.
    """
    before, after = drafts
    pid = repo.ensure_production("The Farm")

    d1 = repo.save_draft(pid, before)
    repo.save_elements(pid, d1, _elements("3", "brass letter opener"))

    d2 = repo.save_draft(pid, after)
    relocation = [
        Finding(
            kind=ChangeKind.ELEMENT_RELOCATED,
            summary="The brass letter opener moves from scene 3 to scene 7.",
            scene="7",
            from_scene="3",
            element="brass letter opener",
            departments=[Department.PROPS],
        )
    ]
    written, created, matched = repo.save_elements(
        pid, d2, _elements("7", "brass letter opener"), relocation
    )

    assert written == 1
    assert created == 0, "a relocated prop must not mint a second identity"
    assert matched == 1

    inventory = repo.inventory(pid)
    assert len(inventory) == 1
    assert inventory[0]["appearances"] == 2


def test_element_history_is_the_trail_across_drafts(repo, drafts):
    """The query behind the inventory screen: 3 -> 7 over two revisions."""
    before, after = drafts
    pid = repo.ensure_production("The Farm")

    d1 = repo.save_draft(pid, before)
    repo.save_elements(pid, d1, _elements("3", "brass letter opener"))
    d2 = repo.save_draft(pid, after)
    repo.save_elements(pid, d2, _elements("7", "brass letter opener"))

    history = repo.element_history(pid, "brass letter opener")
    assert [(h["revision"], h["scene"]) for h in history] == [(1, "3"), (2, "7")]


def test_a_genuinely_new_element_gets_its_own_identity(repo, drafts):
    """The contrast case. Glasses appear nowhere in draft 1, so they are new.

    If this and the relocation test both pass, identity is discriminating rather
    than merging everything or splitting everything.
    """
    before, after = drafts
    pid = repo.ensure_production("The Farm")

    d1 = repo.save_draft(pid, before)
    repo.save_elements(pid, d1, _elements("3", "brass letter opener"))

    d2 = repo.save_draft(pid, after)
    _, created, _ = repo.save_elements(
        pid, d2, _elements("7", "brass letter opener", "wire-rimmed reading glasses")
    )

    assert created == 1, "the glasses are new and need their own identity"
    assert len(repo.inventory(pid)) == 2


def test_identity_matching_is_case_insensitive(repo, drafts):
    """Extractors are not consistent about capitalisation across runs."""
    before, after = drafts
    pid = repo.ensure_production("The Farm")

    d1 = repo.save_draft(pid, before)
    repo.save_elements(pid, d1, _elements("3", "Brass Letter Opener"))
    d2 = repo.save_draft(pid, after)
    _, created, matched = repo.save_elements(pid, d2, _elements("7", "brass letter opener"))

    assert created == 0
    assert matched == 1


def test_same_name_different_category_is_a_different_thing(repo, drafts):
    """A prop knife and an SFX knife are not one object.

    Identity is keyed on category as well as name, because the department that
    owns it is part of what the object *is*.
    """
    before, after = drafts
    pid = repo.ensure_production("The Farm")

    d1 = repo.save_draft(pid, before)
    repo.save_elements(pid, d1, _elements("3", "knife", category=ElementCategory.PROP))
    d2 = repo.save_draft(pid, after)
    _, created, _ = repo.save_elements(
        pid, d2, _elements("7", "knife", category=ElementCategory.SFX)
    )

    assert created == 1
    assert len(repo.inventory(pid)) == 2


def test_a_relocation_claim_does_not_cross_categories(repo, drafts):
    """One name, two categories: the claim must land on the matching one.

    A prop "letter" and a dressed "letter" are different objects. Resolving the
    claim onto the wrong one, or onto both, loses a real element from the
    inventory and the AD has no way to notice.
    """
    before, after = drafts
    pid = repo.ensure_production("The Farm")

    d1 = repo.save_draft(pid, before)
    repo.save_elements(
        pid,
        d1,
        DraftElements(
            scenes=[
                SceneExtraction(
                    scene_number="3",
                    elements=[
                        ExtractedElement(name="letter", category=ElementCategory.PROP),
                        ExtractedElement(
                            name="letter", category=ElementCategory.SET_DRESSING
                        ),
                    ],
                )
            ]
        ),
    )

    d2 = repo.save_draft(pid, after)
    claim = [
        Finding(
            kind=ChangeKind.ELEMENT_RELOCATED,
            summary="the letter moves",
            scene="7",
            element="letter",
            departments=[Department.PROPS],
        )
    ]
    repo.save_elements(pid, d2, _elements("7", "letter"), claim)

    # Still two identities, and only the prop gained an appearance.
    inventory = {(i["category"], i["appearances"]) for i in repo.inventory(pid)}
    assert inventory == {("prop", 2), ("set_dressing", 1)}


def test_a_relocation_claim_naming_an_unknown_element_creates_one(repo, drafts):
    """A claim about something never seen before cannot match anything.

    Inventing a link would be worse than creating a new identity, so the
    fallback has to be the safe direction.
    """
    before, after = drafts
    pid = repo.ensure_production("The Farm")

    d1 = repo.save_draft(pid, before)
    repo.save_elements(pid, d1, _elements("3", "brass letter opener"))

    d2 = repo.save_draft(pid, after)
    bogus = [
        Finding(
            kind=ChangeKind.ELEMENT_RELOCATED,
            summary="the shotgun moves",
            scene="7",
            element="shotgun",
            departments=[Department.PROPS],
        )
    ]
    _, created, _ = repo.save_elements(pid, d2, _elements("7", "shotgun"), bogus)

    assert created == 1
    assert len(repo.inventory(pid)) == 2


# --- changes and routing --------------------------------------------------


def test_changes_persist_with_their_departments(repo, drafts):
    before, after = drafts
    pid = repo.ensure_production("The Farm")
    d1 = repo.save_draft(pid, before)
    d2 = repo.save_draft(pid, after)

    result = SemanticResult(
        findings=[
            Finding(
                kind=ChangeKind.TIME_OF_DAY_CHANGED,
                summary="Scene 2 flips DAY to NIGHT.",
                scene="2",
                departments=[Department.SCHEDULE, Department.LOCATIONS],
                confidence=0.97,
            )
        ]
    )
    assert repo.save_changes(pid, d1, d2, result) == 1

    schedule = repo.changes_for_department(d2, "schedule")
    assert len(schedule) == 1
    assert "DAY to NIGHT" in schedule[0]["summary"]
    # And props is not told about a scheduling change.
    assert repo.changes_for_department(d2, "props") == []


def test_department_counts_are_the_fan_out_badges(repo, drafts):
    before, after = drafts
    pid = repo.ensure_production("The Farm")
    d1 = repo.save_draft(pid, before)
    d2 = repo.save_draft(pid, after)

    result = SemanticResult(
        findings=[
            Finding(
                kind=ChangeKind.ELEMENT_RELOCATED,
                summary="opener moves",
                scene="7",
                departments=[Department.PROPS],
            ),
            Finding(
                kind=ChangeKind.SCENE_OMITTED,
                summary="scene 5 omitted",
                scene="5",
                departments=[Department.PROPS, Department.SCHEDULE],
            ),
        ]
    )
    repo.save_changes(pid, d1, d2, result)

    assert repo.department_counts(d2) == {"props": 2, "schedule": 1}


def test_clearance_flags_read_branded_elements(repo, drafts):
    """Another consumer of the same data, per the PRD."""
    _, after = drafts
    pid = repo.ensure_production("The Farm")
    d2 = repo.save_draft(pid, after)
    repo.save_elements(
        pid,
        d2,
        DraftElements(
            scenes=[
                SceneExtraction(
                    scene_number="5A",
                    elements=[
                        ExtractedElement(
                            name="Ford Bronco",
                            category=ElementCategory.VEHICLE,
                            branded=True,
                        ),
                        ExtractedElement(
                            name="manila envelope", category=ElementCategory.PROP
                        ),
                    ],
                )
            ]
        ),
    )

    flags = repo.clearance_flags(d2)
    assert [f["name"] for f in flags] == ["Ford Bronco"]


# --- runs ------------------------------------------------------------------


def test_a_run_records_what_it_cost(repo, drafts):
    """Cost has to be visible after the fact, not only while it happens."""
    before, after = drafts
    pid = repo.ensure_production("The Farm")
    d1 = repo.save_draft(pid, before)
    d2 = repo.save_draft(pid, after)

    run_id = repo.start_run(pid, d1, d2)
    repo.finish_run(
        run_id,
        status="ok",
        scenes=9,
        findings=8,
        budget={"calls": 11, "input_tokens": 4000, "output_tokens": 900},
        fallbacks=1,
    )

    runs = repo.runs(pid)
    assert len(runs) == 1
    assert runs[0]["status"] == "ok"
    assert runs[0]["model_calls"] == 11
    assert runs[0]["fallbacks"] == 1
    assert runs[0]["finished_at"] is not None


def test_persist_run_writes_the_whole_pipeline(repo, drafts):
    """The end to end path: both drafts, elements, findings, and the run row."""
    before, after = drafts
    diff = diff_drafts(align(before, after))
    result = SemanticResult(
        findings=[
            Finding(
                kind=ChangeKind.ELEMENT_RELOCATED,
                summary="The brass letter opener moves from scene 3 to scene 7.",
                scene="7",
                from_scene="3",
                element="brass letter opener",
                departments=[Department.PROPS],
            )
        ]
    )

    persisted = repo.persist_run(
        title="The Farm",
        before=before,
        after=after,
        diff=diff,
        result=result,
        elements=_elements("7", "brass letter opener"),
        budget={"calls": 11, "input_tokens": 4000, "output_tokens": 900},
    )

    assert persisted.scenes == 9
    assert persisted.changes == 1
    assert persisted.elements == 1
    assert repo.runs(persisted.production_id)[0]["findings"] == 1


def test_a_failed_run_rolls_back(drafts, tmp_path):
    """A diff that dies at scene 80 must not leave a partial record.

    79 scenes that look like a complete draft is worse than no draft at all,
    because nothing downstream can tell it is incomplete. Uses a file rather
    than :memory: so the rollback is observable after the connection closes.
    """
    before, _ = drafts
    db_path = tmp_path / "elements.db"

    with Database(path=db_path) as db:
        db.create_schema()
        Repository(db).ensure_production("The Farm")

    # A second connection that dies partway through.
    with pytest.raises(RuntimeError, match="died mid-run"), Database(path=db_path) as db:
        repo = Repository(db)
        pid = repo.ensure_production("The Farm")
        repo.save_draft(pid, before)
        raise RuntimeError("pipeline died mid-run")

    # Nothing from the failed transaction survived.
    with Database(path=db_path) as db:
        assert db.query("SELECT id FROM draft") == []
        assert db.query("SELECT id FROM scene") == []


def test_a_successful_run_commits(drafts, tmp_path):
    """The other half of the rollback test: a clean exit must persist."""
    before, _ = drafts
    db_path = tmp_path / "elements.db"

    with Database(path=db_path) as db:
        db.create_schema()
        repo = Repository(db)
        repo.save_draft(repo.ensure_production("The Farm"), before)

    with Database(path=db_path) as db:
        assert len(db.query("SELECT id FROM draft")) == 1
        assert db.query("SELECT id FROM scene")


def test_query_returns_plain_dicts(repo, drafts, db):
    """Both backends must return the same shape, or callers branch on backend."""
    before, _ = drafts
    pid = repo.ensure_production("The Farm")
    repo.save_draft(pid, before)

    rows = db.query("SELECT title FROM production WHERE id = ?", (pid,))
    assert isinstance(rows[0], dict)
    assert rows[0]["title"] == "The Farm"


def test_postgres_placeholder_translation():
    """The one real difference between the backends, checked without a server."""
    sqlite_db = Database()
    postgres_db = Database(dsn="postgresql://localhost/x")

    sql = "SELECT * FROM element WHERE id = ? AND name = ?"
    assert sqlite_db._translate(sql) == sql
    assert postgres_db._translate(sql).count("%s") == 2


def test_cannot_configure_both_backends():
    with pytest.raises(ValueError):
        Database(dsn="postgresql://localhost/x", path="/tmp/x.db")


def test_using_a_closed_database_is_an_error():
    """Better than a confusing NoneType attribute error deep in a query."""
    db = Database()
    with pytest.raises(RuntimeError, match="not connected"):
        db.query("SELECT 1")


def test_the_same_file_lands_on_the_same_revision(repo, drafts):
    """Re-running the pipeline must not append two more drafts every time.

    Without keying on the source path, a second run of the same pair becomes
    revisions 3 and 4, and the production accumulates duplicates of one script.
    Found by a fan-out test that expected 7 reports and got 14.
    """
    before, after = drafts
    for _ in range(3):
        repo.save_draft(repo.ensure_production("The Farm"), before)
        repo.save_draft(repo.ensure_production("The Farm"), after)

    pid = repo.ensure_production("The Farm")
    rows = repo.db.query(
        "SELECT revision FROM draft WHERE production_id = ? ORDER BY revision", (pid,)
    )
    assert [r["revision"] for r in rows] == [1, 2]


def test_a_genuinely_new_draft_still_increments(repo, drafts, tmp_path):
    """The other half: a different file is a new revision, not a replacement."""
    before, after = drafts
    pid = repo.ensure_production("The Farm")
    repo.save_draft(pid, before)
    repo.save_draft(pid, after)

    # A third draft, from a different path.
    third = after.model_copy(update={"source_path": str(tmp_path / "draft-3.fdx")})
    repo.save_draft(pid, third)

    latest = repo.latest_draft(pid)
    assert latest is not None
    assert latest["revision"] == 3
