"""Ripples (Layer 6): checking a model's own unfounded hunch against real
scene text, capped and cheap, the way agents/sourcing.py caps its searches."""

from __future__ import annotations

from bluepages.events import CollectingStream, EventKind
from bluepages.model.script import Element, ElementType, Scene, SceneNumber, Screenplay
from bluepages.semantic.reasoning import Finding, RippleQuestion
from bluepages.semantic.ripples import check_ripples, confirmed_findings
from bluepages.testdata import ChangeKind
from tests.unit.fakes import ScriptedClient


def _screenplay(number: str, text: str) -> Screenplay:
    scene = Scene(
        number=SceneNumber.parse(number),
        heading=f"INT. SCENE {number}",
        elements=[Element(type=ElementType.ACTION, text=text)],
    )
    return Screenplay(scenes=[scene])


def _question(scene: str, summary: str = "A prop appears here too.") -> RippleQuestion:
    finding = Finding(
        kind=ChangeKind.ELEMENT_ADDED,
        summary=summary,
        scene=scene,
        reasoning="noticed while reasoning about a neighbouring scene",
    )
    return RippleQuestion(asked_about="4", finding=finding, note="test")


def test_a_confirmed_ripple_returns_its_finding():
    after = _screenplay("9", "A brass compass sits on the desk.")
    client = ScriptedClient(
        default={"confirmed": True, "quote": "A brass compass sits on the desk.", "note": "present"}
    )
    ripples = check_ripples([_question("9")], after, client)

    assert len(ripples) == 1
    assert ripples[0].confirmed is True
    findings = confirmed_findings(ripples)
    assert len(findings) == 1
    assert findings[0].scene == "9"


def test_an_unconfirmed_ripple_is_dropped():
    after = _screenplay("9", "Nothing relevant happens here.")
    client = ScriptedClient(default={"confirmed": False, "quote": "", "note": "not supported"})
    ripples = check_ripples([_question("9")], after, client)

    assert ripples[0].confirmed is False
    assert confirmed_findings(ripples) == []


def test_a_confirmed_flag_with_no_quote_does_not_count():
    """A model saying yes without pointing at the line is not evidence."""
    after = _screenplay("9", "A brass compass sits on the desk.")
    client = ScriptedClient(default={"confirmed": True, "quote": "", "note": ""})
    ripples = check_ripples([_question("9")], after, client)

    assert ripples[0].confirmed is False


def test_a_scene_number_that_does_not_exist_is_dropped_without_a_call():
    after = _screenplay("9", "Something.")
    client = ScriptedClient(default={"confirmed": True, "quote": "x", "note": ""})
    ripples = check_ripples([_question("999")], after, client)

    assert ripples[0].confirmed is False
    assert client.prompts == []


def test_ripple_events_are_emitted():
    after = _screenplay("9", "A brass compass sits on the desk.")
    client = ScriptedClient(
        default={"confirmed": True, "quote": "A brass compass sits on the desk.", "note": ""}
    )
    stream = CollectingStream()
    check_ripples([_question("9")], after, client, stream=stream)

    assert stream.of_kind(EventKind.RIPPLE_OPENED)
    assert stream.of_kind(EventKind.RIPPLE_STEP)
    assert stream.of_kind(EventKind.RIPPLE_RESOLVED)


def test_ripple_check_uses_the_bulk_model():
    """A narrow yes/no check does not need judgment-tier reasoning."""
    after = _screenplay("9", "Something.")
    client = ScriptedClient(default={"confirmed": False, "quote": "", "note": ""})
    check_ripples([_question("9")], after, client)
    assert client.judgment_calls == 0
