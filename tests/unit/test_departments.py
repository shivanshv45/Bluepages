"""Department agents (Layer 5.1).

The model's phrasing is not testable here. What is testable, and what decides
whether the fan-out is correct, is which findings reach which agent: a
department that receives another department's changes will faithfully write them
up, and the report will be wrong in a way that reads perfectly well.
"""

from __future__ import annotations

import pytest

from bluepages.agents import SPECS, fan_out, write_report
from bluepages.agents.departments import Report
from bluepages.agents.sourcing import Sourcer
from bluepages.events import CollectingStream, EventKind
from bluepages.semantic.reasoning import Finding, SemanticResult
from bluepages.testdata import ChangeKind, Department
from tests.unit.fakes import ScriptedClient


def _report(summary: str = "ok", *notes: dict) -> dict:
    return {"summary": summary, "notes": list(notes)}


EMPTY_REPORT = _report("nothing to do")


@pytest.fixture
def revision():
    """The small pair's eight findings, routed as the answer key says."""
    return SemanticResult(
        findings=[
            Finding(
                kind=ChangeKind.TIME_OF_DAY_CHANGED,
                summary="Scene 2 flips DAY to NIGHT.",
                scene="2",
                departments=[Department.SCHEDULE, Department.LOCATIONS],
                confidence=0.97,
            ),
            Finding(
                kind=ChangeKind.ELEMENT_RELOCATED,
                summary="The brass letter opener moves from scene 3 to scene 7.",
                scene="7",
                from_scene="3",
                element="brass letter opener",
                departments=[Department.PROPS],
                confidence=0.92,
            ),
            Finding(
                kind=ChangeKind.CHARACTER_RENAMED,
                summary="JANITOR is renamed CUSTODIAN.",
                scene="4",
                departments=[Department.CAST],
                confidence=0.95,
            ),
            Finding(
                kind=ChangeKind.CLEARANCE_RISK,
                summary="A Ford Bronco is named in the new scene 5A.",
                scene="5A",
                element="Ford Bronco",
                risk="medium",
                departments=[Department.CLEARANCE, Department.TRANSPORT],
                confidence=0.93,
            ),
        ]
    )


def test_every_routed_department_has_a_spec():
    """A department with no spec silently never receives a report. Finance is
    the one deliberate exception: it is a consumer, never a routing target,
    per DECISIONS.md, so fan_out skips it by design."""
    for department in Department:
        if department is Department.FINANCE:
            assert department not in SPECS
            continue
        assert department in SPECS, f"{department.value} has no spec"


def test_each_spec_says_what_it_ignores():
    """Telling an agent what is not its business is what keeps a report short."""
    for spec in SPECS.values():
        assert spec.concerns.strip()
        assert spec.ignores.strip()


def test_an_agent_receives_only_its_own_findings(revision):
    """The decision that makes the fan-out correct.

    Props must not be shown the DAY to NIGHT flip. An agent handed another
    department's change will write it up convincingly, and the report is then
    wrong in a way that reads perfectly well.
    """
    client = ScriptedClient(default=EMPTY_REPORT)
    fan_out(revision, client)

    props_prompt = next(p for p in client.prompts if "routed to Props" in p)
    assert "letter opener" in props_prompt
    assert "DAY to NIGHT" not in props_prompt
    assert "CUSTODIAN" not in props_prompt


def test_a_finding_routed_to_two_departments_reaches_both(revision):
    """The Bronco is a clearance question and a transport question at once."""
    client = ScriptedClient(default=EMPTY_REPORT)
    fan_out(revision, client)

    clearance = next(p for p in client.prompts if "routed to Clearance" in p)
    transport = next(p for p in client.prompts if "routed to Transport" in p)
    assert "Ford Bronco" in clearance
    assert "Ford Bronco" in transport


def test_a_department_with_nothing_is_not_asked(revision):
    """Silence is correct. A nightly "no changes for you" email gets muted."""
    client = ScriptedClient(default=EMPTY_REPORT)
    result = fan_out(revision, client)

    assert not any("routed to Wardrobe" in p for p in client.prompts)
    assert result.by_department(Department.WARDROBE) is None
    assert result.departments_silent > 0


def test_only_the_departments_with_findings_are_notified(revision):
    client = ScriptedClient(default=EMPTY_REPORT)
    result = fan_out(revision, client)

    notified = {r.department for r in result.reports}
    assert notified == {
        Department.SCHEDULE,
        Department.LOCATIONS,
        Department.PROPS,
        Department.CAST,
        Department.CLEARANCE,
        Department.TRANSPORT,
    }


def test_the_prompt_carries_the_reasoning_not_just_the_summary(revision):
    """A department head deciding whether to act needs the why."""
    revision.findings[1].reasoning = "Scene 7 says it sits where he left it."
    client = ScriptedClient(default=EMPTY_REPORT)
    fan_out(revision, client)

    props = next(p for p in client.prompts if "routed to Props" in p)
    assert "sits where he left it" in props


def test_an_uncertain_finding_is_flagged_to_the_department(revision):
    """Hiding low confidence from the person acting on it is the wrong trade."""
    revision.findings[1].confidence = 0.4
    client = ScriptedClient(default=EMPTY_REPORT)
    fan_out(revision, client)

    props = next(p for p in client.prompts if "routed to Props" in p)
    assert "not confident" in props


def test_fan_out_uses_the_bulk_model(revision):
    """Routing is already decided. Phrasing does not need the judgment model,
    and seven Sonnet calls for wording would be the wrong place to spend."""
    client = ScriptedClient(default=EMPTY_REPORT)
    fan_out(revision, client)
    assert client.judgment_calls == 0


def test_reports_carry_their_notes(revision):
    client = ScriptedClient(
        rules=[
            (
                "routed to Props",
                _report(
                    "One prop moves scenes.",
                    {
                        "scene": "7",
                        "note": "The brass letter opener is now in the kitchen.",
                        "action": "Move it on the continuity sheet. No new buy.",
                        "urgent": False,
                    },
                ),
            )
        ],
        default=EMPTY_REPORT,
    )
    result = fan_out(revision, client)

    props = result.by_department(Department.PROPS)
    assert props is not None
    assert props.summary == "One prop moves scenes."
    assert len(props.notes) == 1
    assert "No new buy" in props.notes[0].action


def test_urgent_notes_are_separable(revision):
    client = ScriptedClient(
        rules=[
            (
                "routed to Locations",
                _report(
                    "A night shoot needs a re-quote.",
                    {"scene": "2", "note": "Now a night shoot.", "urgent": True},
                    {"scene": "5", "note": "Location released.", "urgent": False},
                ),
            )
        ],
        default=EMPTY_REPORT,
    )
    result = fan_out(revision, client)

    locations = result.by_department(Department.LOCATIONS)
    assert locations is not None
    assert len(locations.notes) == 2
    assert len(locations.urgent_notes) == 1


def test_one_failed_department_does_not_lose_the_others(revision):
    """A broken report for props must not cost cast and transport theirs."""
    client = ScriptedClient(
        rules=[("routed to Props", "this is not JSON")],
        default=EMPTY_REPORT,
    )
    stream = CollectingStream()
    result = fan_out(revision, client, stream=stream)

    assert result.by_department(Department.PROPS) is None
    assert result.by_department(Department.CAST) is not None
    assert stream.of_kind(EventKind.PARSE_WARNING)


def test_agent_events_are_emitted(revision):
    """Layer 7's agent lanes consume these, so they are a contract."""
    client = ScriptedClient(default=EMPTY_REPORT)
    stream = CollectingStream()
    fan_out(revision, client, stream=stream)

    started = stream.of_kind(EventKind.AGENT_STARTED)
    finished = stream.of_kind(EventKind.AGENT_FINISHED)
    assert len(started) == len(finished) == 6
    assert all("department" in e.data for e in started)


def test_departments_can_be_limited(revision):
    """Useful for re-running one department without paying for all of them."""
    client = ScriptedClient(default=EMPTY_REPORT)
    result = fan_out(revision, client, departments=[Department.PROPS])

    assert len(result.reports) == 1
    assert result.reports[0].department is Department.PROPS


def test_reports_are_ordered_by_title(revision):
    client = ScriptedClient(default=EMPTY_REPORT)
    result = fan_out(revision, client)

    titles = [r.title for r in result.reports]
    assert titles == sorted(titles)


def test_write_report_records_which_model_answered(revision):
    """Fallback output is weaker and that has to stay visible."""
    client = ScriptedClient(default=EMPTY_REPORT, model_name="groq")
    report = write_report(
        SPECS[Department.PROPS], revision.by_department(Department.PROPS), client
    )

    assert report.model_name == "groq"
    assert report.finding_count == 1


def test_an_empty_report_is_marked_empty():
    report = Report(
        department=Department.PROPS, title="Props", summary="nothing", notes=[]
    )
    assert report.is_empty


def test_a_new_element_is_sourced_inside_the_department_slice():
    """Sourcing moved into write_report, in the fan-out pool: a department
    report for a change that adds an element carries what was found for it."""
    # A name unlikely to collide with agents/sourcing.py's on-disk cache from
    # a real run, so this test sees a fresh, deterministic unsourced result.
    element = "test-fixture-widget-zz9"
    revision = SemanticResult(
        findings=[
            Finding(
                kind=ChangeKind.ELEMENT_ADDED,
                summary="A new picture vehicle is introduced.",
                scene="9",
                element=element,
                departments=[Department.TRANSPORT],
                confidence=0.9,
            )
        ]
    )
    client = ScriptedClient(default=EMPTY_REPORT)
    # Search budget already spent: Sourcer degrades to an unsourced line
    # rather than calling the network, which is exactly what a test needs.
    sourcer = Sourcer(max_searches=0)
    result = fan_out(revision, client, sourcer=sourcer)

    transport = result.by_department(Department.TRANSPORT)
    assert transport is not None
    assert element in transport.sourced
    assert transport.sourced[element].grounded is False


def test_agent_step_events_bracket_sourcing_and_drafting():
    revision = SemanticResult(
        findings=[
            Finding(
                kind=ChangeKind.ELEMENT_ADDED,
                summary="A new prop is introduced.",
                scene="9",
                element="brass compass",
                departments=[Department.PROPS],
                confidence=0.9,
            )
        ]
    )
    client = ScriptedClient(default=EMPTY_REPORT)
    stream = CollectingStream()
    fan_out(revision, client, stream=stream, sourcer=Sourcer(max_searches=0))

    steps = stream.of_kind(EventKind.AGENT_STEP)
    step_names = {e.data.get("step") for e in steps}
    assert "sourcing" in step_names
    assert "drafting" in step_names


def test_social_has_a_spec_and_can_be_routed_to():
    """Social is a routed department: unlike clearance/schedule it needs real
    judgment, so it goes through fan_out with its own DepartmentSpec."""
    revision = SemanticResult(
        findings=[
            Finding(
                kind=ChangeKind.ELEMENT_ADDED,
                summary="A striking new 1970s picture vehicle appears.",
                scene="5A",
                element="Ford Bronco",
                departments=[Department.SOCIAL, Department.TRANSPORT],
                confidence=0.9,
            )
        ]
    )
    client = ScriptedClient(default=EMPTY_REPORT)
    result = fan_out(revision, client, sourcer=Sourcer(max_searches=0))

    social = result.by_department(Department.SOCIAL)
    assert social is not None


def test_finance_is_never_a_fan_out_target():
    """Finance has no DepartmentSpec, so fan_out silently skips it even if a
    finding claimed to route there: it only ever reads decisions, never a
    finding directly."""
    assert Department.FINANCE not in SPECS


def test_a_finding_with_no_new_element_emits_no_sourcing_step(revision):
    """The letter opener relocates; nothing is bought. No sourcing step."""
    client = ScriptedClient(default=EMPTY_REPORT)
    stream = CollectingStream()
    fan_out(revision, client, stream=stream, sourcer=Sourcer(max_searches=0))

    props_steps = [
        e for e in stream.of_kind(EventKind.AGENT_STEP)
        if e.data.get("department") == "props"
    ]
    assert all(e.data.get("step") != "sourcing" for e in props_steps)
