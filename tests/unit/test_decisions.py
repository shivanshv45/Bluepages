"""Decision derivation and persistence (Layer 10).

Sourcing moved into the fan-out pool (agents/departments.py::write_report), so
from_findings should read a report's `sourced` dict rather than search again.
persist() is scoped to one run's decisions, not a production's whole history.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bluepages.agents import decisions as decision_log
from bluepages.agents.sourcing import Sourced
from bluepages.store import Repository, open_database
from bluepages.testdata import ChangeKind, Department


class _Finding:
    def __init__(self, **kwargs):
        self.departments = kwargs.get("departments", [])
        self.kind = kwargs.get("kind", "")
        self.scene = kwargs.get("scene")
        self.from_scene = kwargs.get("from_scene")
        self.summary = kwargs.get("summary", "")
        self.reasoning = kwargs.get("reasoning", "")
        self.confidence = kwargs.get("confidence", 0.9)
        self.element = kwargs.get("element")
        self.risk = kwargs.get("risk")
        self.uncertain = kwargs.get("uncertain", False)


class _Report:
    def __init__(self, department, sourced=None, notes=None):
        self.department = department
        self.notes = notes or []
        self.sourced = sourced or {}
        self.model_name = "bulk"
        self.via_fallback = False
        self.summary = "ok"


def test_procurement_reads_the_reports_sourced_dict_not_the_network():
    """The report already carries a Sourced result from write_report; a
    procurement decision should use it rather than calling the sourcer again."""
    finding = _Finding(
        kind=ChangeKind.ELEMENT_ADDED.value,
        summary="A new picture vehicle appears.",
        scene="9",
        element="1970 Dodge Charger",
        departments=[Department.TRANSPORT],
    )
    sourced = Sourced(
        product="1970 Dodge Charger replica",
        supplier="Picture Car Warehouse",
        price=1200.0,
        unit="week rental",
        lead_time="2 weeks",
        grounded=True,
    )
    report = _Report(Department.TRANSPORT, sourced={"1970 Dodge Charger": sourced})

    items = decision_log.from_findings([finding], [report])

    procure = next(d for d in items if d.kind == "procure")
    assert procure.payload["supplier"] == "Picture Car Warehouse"
    assert procure.payload["total"] == 1200.0
    assert procure.payload["grounded"] is True


def test_procurement_falls_back_to_searching_when_no_report_has_it():
    """A caller with no reports (a headless run, or a test) still gets a
    procurement decision, just via the old direct-search path."""
    finding = _Finding(
        kind=ChangeKind.ELEMENT_ADDED.value,
        summary="A new prop appears.",
        scene="3",
        element="unsourced-fallback-test-element",
        departments=[Department.PROPS],
    )

    class _StubSourcer:
        def find(self, element, department, context=""):
            return Sourced(
                product=f"stub for {element}",
                supplier="Stub Supplier",
                price=10.0,
                unit="each",
                lead_time="now",
                grounded=True,
            )

    items = decision_log.from_findings([finding], reports=None, sourcer=_StubSourcer())

    procure = next(d for d in items if d.kind == "procure")
    assert procure.payload["supplier"] == "Stub Supplier"


def test_a_grounded_procurement_under_the_limit_auto_approves():
    finding = _Finding(
        kind=ChangeKind.ELEMENT_ADDED.value,
        summary="A new prop appears.",
        scene="9",
        element="brass compass",
        departments=[Department.PROPS],
    )
    sourced = Sourced(
        product="brass compass", supplier="Prop House", price=150.0,
        unit="each", lead_time="3 days", grounded=True,
    )
    report = _Report(Department.PROPS, sourced={"brass compass": sourced})
    budget = {"props": {"allocated": 12000, "committed": 0, "auto_approve": 500}}

    items = decision_log.from_findings([finding], [report], budget=budget)

    procure = next(d for d in items if d.kind == "procure")
    assert procure.status == "approved"
    assert procure.payload["auto_approved"] is True
    assert "auto-approve limit" in procure.rationale


def test_a_procurement_over_the_limit_stays_pending():
    finding = _Finding(
        kind=ChangeKind.ELEMENT_ADDED.value,
        summary="A new vehicle appears.",
        scene="9",
        element="vintage motorcycle",
        departments=[Department.TRANSPORT],
    )
    sourced = Sourced(
        product="vintage motorcycle", supplier="Picture Vehicles Inc", price=5000.0,
        unit="week rental", lead_time="1 week", grounded=True,
    )
    report = _Report(Department.TRANSPORT, sourced={"vintage motorcycle": sourced})
    budget = {"transport": {"allocated": 25000, "committed": 0, "auto_approve": 500}}

    items = decision_log.from_findings([finding], [report], budget=budget)

    procure = next(d for d in items if d.kind == "procure")
    assert procure.status == "proposed"
    assert procure.payload["auto_approved"] is False


def test_an_ungrounded_procurement_never_auto_approves_even_under_a_high_limit():
    finding = _Finding(
        kind=ChangeKind.ELEMENT_ADDED.value,
        summary="A new prop appears.",
        scene="9",
        element="mystery item",
        departments=[Department.PROPS],
    )
    sourced = Sourced(
        product="mystery item (not sourced)", supplier="To be sourced", price=0.0,
        unit="unknown", lead_time="unknown", grounded=False,
    )
    report = _Report(Department.PROPS, sourced={"mystery item": sourced})
    budget = {"props": {"allocated": 12000, "committed": 0, "auto_approve": 100000}}

    items = decision_log.from_findings([finding], [report], budget=budget)

    procure = next(d for d in items if d.kind == "procure")
    assert procure.status == "proposed"


def test_two_procurements_in_one_run_share_the_same_remaining_budget():
    """A $2,000 limit should not clear two $1,500 purchases in the same run:
    the second sees the first's total already spent against remaining."""
    findings = [
        _Finding(
            kind=ChangeKind.ELEMENT_ADDED.value, summary="a", scene="1",
            element="item-a", departments=[Department.PROPS],
        ),
        _Finding(
            kind=ChangeKind.ELEMENT_ADDED.value, summary="b", scene="2",
            element="item-b", departments=[Department.PROPS],
        ),
    ]
    report = _Report(
        Department.PROPS,
        sourced={
            "item-a": Sourced(product="a", supplier="s", price=1500.0, unit="each",
                               lead_time="now", grounded=True),
            "item-b": Sourced(product="b", supplier="s", price=1500.0, unit="each",
                               lead_time="now", grounded=True),
        },
    )
    budget = {"props": {"allocated": 12000, "committed": 10000, "auto_approve": 2000}}

    items = decision_log.from_findings(findings, [report], budget=budget)

    procures = [d for d in items if d.kind == "procure"]
    approved = [d for d in procures if d.status == "approved"]
    assert len(approved) == 1


def test_a_social_report_produces_social_post_decisions_not_draft_email():
    """Social gets its own decision kind: a post to publish, not a brief to
    send to a department head."""
    note = type("Note", (), {"scene": "5A", "note": "You won't believe what just drove into frame.", "urgent": False})()
    report = _Report(Department.SOCIAL, notes=[note])

    items = decision_log.from_findings([], [report])

    assert any(d.kind == "social_post" for d in items)
    assert not any(d.kind == "draft_email" and d.department == "social" for d in items)
    post = next(d for d in items if d.kind == "social_post")
    assert post.payload["copy"] == "You won't believe what just drove into frame."
    assert post.status == "pending"


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "e.db"


def test_persist_is_scoped_to_one_run_not_the_whole_production(db_path: Path) -> None:
    """A re-run of a different revision must not erase an earlier run's log."""
    with open_database(path=db_path) as db:
        db.create_schema()
        repo = Repository(db)
        production_id = repo.ensure_production("The Farm")
        run_1 = repo.start_run(production_id, None, None)
        run_2 = repo.start_run(production_id, None, None)

        first = [decision_log.Decision(kind="route", summary="first run's decision")]
        decision_log.persist(db, production_id, run_1, first)

        second = [decision_log.Decision(kind="route", summary="second run's decision")]
        decision_log.persist(db, production_id, run_2, second)

        rows = decision_log.for_production(db, production_id)

    summaries = {r["summary"] for r in rows}
    assert "first run's decision" in summaries
    assert "second run's decision" in summaries


def test_re_persisting_the_same_run_replaces_only_its_own_rows(db_path: Path) -> None:
    with open_database(path=db_path) as db:
        db.create_schema()
        repo = Repository(db)
        production_id = repo.ensure_production("The Farm")
        run_id = repo.start_run(production_id, None, None)

        decision_log.persist(
            db, production_id, run_id,
            [decision_log.Decision(kind="route", summary="stale")],
        )
        decision_log.persist(
            db, production_id, run_id,
            [decision_log.Decision(kind="route", summary="fresh")],
        )

        rows = decision_log.for_production(db, production_id)

    summaries = [r["summary"] for r in rows]
    assert "fresh" in summaries
    assert "stale" not in summaries
