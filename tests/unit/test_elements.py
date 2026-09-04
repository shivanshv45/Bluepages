"""Element extraction (Layer 3.3).

What is tested here is everything around the model call: what the prompt
contains, what happens to a malformed answer, and whether cast comes from the
cues rather than from the model's guess.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ripple.events import CollectingStream, EventKind
from ripple.model import Scene, SceneNumber
from ripple.parse import parse_fdx
from ripple.semantic.elements import (
    CATEGORY_DEPARTMENT,
    DraftElements,
    ElementCategory,
    ExtractedElement,
    SceneExtraction,
    extract_draft,
    extract_scene,
)
from tests.unit.fakes import EMPTY_ELEMENTS, ScriptedClient, elements_answer

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


@pytest.fixture
def draft2():
    return parse_fdx(FIXTURES / "small-draft-2.fdx")


def test_every_category_routes_to_a_department():
    """A category with no department is an element nobody is told about."""
    for category in ElementCategory:
        assert category in CATEGORY_DEPARTMENT


def test_prompt_carries_action_but_not_dialogue(draft2):
    """Dialogue is most of a scene's tokens and names things that do not exist.

    Scene 7's dialogue is "Read it to me. My eyes are going." The reading
    glasses in the action are real; nothing in the dialogue is.
    """
    scene = draft2.scene("7")
    client = ScriptedClient(default=EMPTY_ELEMENTS)
    extract_scene(scene, client)

    prompt = client.prompts[0]
    assert "wire-rimmed reading glasses" in prompt
    assert "Read it to me" not in prompt


def test_prompt_names_the_scene_and_heading(draft2):
    scene = draft2.scene("5A")
    client = ScriptedClient(default=EMPTY_ELEMENTS)
    extract_scene(scene, client)

    prompt = client.prompts[0]
    assert "SCENE 5A" in prompt
    assert "EXT. COUNTY OFFICE - PARKING LOT - DAY" in prompt


def test_extraction_uses_the_bulk_model(draft2):
    """Layer 3.3 is high volume and cheap. It must not reach for Sonnet."""
    scene = draft2.scene("7")
    client = ScriptedClient(default=EMPTY_ELEMENTS)
    extract_scene(scene, client)
    assert client.judgment_calls == 0


def test_max_tokens_is_always_set(draft2):
    """The ScriptedClient asserts this. The test exists to make the rule visible."""
    scene = draft2.scene("7")
    client = ScriptedClient(default=EMPTY_ELEMENTS)
    extract_scene(scene, client)


def test_cast_comes_from_the_cues_not_the_model(draft2):
    """Character cues are exact and free. A model guess would only add error.

    Here the model claims a character who does not speak in the scene; the cue
    list must win.
    """
    scene = draft2.scene("5A")
    client = ScriptedClient(
        default=elements_answer(
            {"name": "SHERIFF BRANNIGAN", "category": "cast"},
            {"name": "manila envelope", "category": "prop"},
        )
    )
    result = extract_scene(scene, client)

    cast = {e.name for e in result.of_category(ElementCategory.CAST)}
    assert cast == {"DEPUTY COLE"}
    assert "SHERIFF BRANNIGAN" not in cast
    # Non-cast elements the model returned survive untouched.
    assert any(e.name == "manila envelope" for e in result.elements)


def test_omitted_scene_costs_no_call():
    """An OMITTED scene has no content. Asking is one call to learn nothing."""
    scene = Scene(number=SceneNumber.parse("5"), heading="OMITTED", omitted=True)
    client = ScriptedClient(default=EMPTY_ELEMENTS)
    result = extract_scene(scene, client)

    assert client.prompts == []
    assert result.elements == []
    assert result.model_name == "skipped"


def test_branded_elements_are_collected(draft2):
    scene = draft2.scene("5A")
    client = ScriptedClient(
        default=elements_answer(
            {"name": "Ford Bronco", "category": "vehicle", "branded": True},
            {"name": "manila envelope", "category": "prop"},
        )
    )
    draft = DraftElements(scenes=[extract_scene(scene, client)])

    branded = draft.branded
    assert len(branded) == 1
    assert branded[0][0] == "5A"
    assert branded[0][1].name == "Ford Bronco"


def test_element_department_routing():
    """A handled prop and a dressed object are different purchase orders."""
    prop = ExtractedElement(name="letter opener", category=ElementCategory.PROP)
    dressing = ExtractedElement(name="ledger", category=ElementCategory.SET_DRESSING)
    vehicle = ExtractedElement(name="Bronco", category=ElementCategory.VEHICLE)

    assert prop.department == "props"
    assert dressing.department == "art"
    assert vehicle.department == "transport"


def test_index_maps_elements_to_scenes():
    """The index is what makes "the same object in two scenes" answerable."""
    draft = DraftElements(
        scenes=[
            SceneExtraction(
                scene_number="3",
                elements=[ExtractedElement(name="brass letter opener", category=ElementCategory.PROP)],
            ),
            SceneExtraction(
                scene_number="7",
                elements=[ExtractedElement(name="Brass Letter Opener", category=ElementCategory.PROP)],
            ),
        ]
    )
    index = draft.index()
    assert index["prop:brass letter opener"] == ["3", "7"]


def test_one_bad_scene_does_not_lose_the_others(draft2):
    """A malformed answer on one scene must not drop the rest of the draft."""
    client = ScriptedClient(
        rules=[("SCENE 4", "this is not JSON at all")],
        default=elements_answer({"name": "envelope", "category": "prop"}),
    )
    stream = CollectingStream()
    draft = extract_draft(draft2, client, stream=stream, scenes=["3", "4", "7"])

    numbers = {s.scene_number for s in draft.scenes}
    assert numbers == {"3", "7"}
    warnings = stream.of_kind(EventKind.PARSE_WARNING)
    assert any("4" in w.data.get("scene_number", "") for w in warnings)


def test_scenes_filter_limits_the_calls(draft2):
    """Extraction is scoped to changed scenes. 120 calls to learn nothing is waste."""
    client = ScriptedClient(default=EMPTY_ELEMENTS)
    extract_draft(draft2, client, scenes=["7"])
    assert len(client.prompts) == 1
    assert "SCENE 7" in client.prompts[0]


def test_results_are_ordered_by_scene_number(draft2):
    """Concurrency returns in completion order; output must read 5, 5A, 6."""
    client = ScriptedClient(default=EMPTY_ELEMENTS)
    draft = extract_draft(draft2, client, scenes=["7", "3", "5A", "4"])
    assert [s.scene_number for s in draft.scenes] == ["3", "4", "5A", "7"]


def test_element_events_are_emitted(draft2):
    """Layer 7's live surface consumes these, so they are a contract."""
    scene = draft2.scene("5A")
    client = ScriptedClient(
        default=elements_answer({"name": "Ford Bronco", "category": "vehicle", "branded": True})
    )
    stream = CollectingStream()
    extract_scene(scene, client, stream=stream)

    found = stream.of_kind(EventKind.ELEMENT_FOUND)
    assert any(e.data["element"] == "Ford Bronco" for e in found)
    bronco = next(e for e in found if e.data["element"] == "Ford Bronco")
    assert bronco.data["branded"] is True
    assert bronco.data["department"] == "transport"
