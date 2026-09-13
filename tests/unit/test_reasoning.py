"""Semantic reasoning (Layer 3.4).

The model's judgment is not testable here: it needs the real model, and that is
what the `live` tests are for. What is testable, and what breaks more often, is
everything around it. Chiefly:

- does the prompt actually carry the evidence the judgment needs
- does a finding about a scene that did not change get rejected
- does a relocation, which legitimately names two scenes, survive that rejection
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bluepages.diff import align, diff_drafts
from bluepages.events import CollectingStream, EventKind
from bluepages.parse import parse_fdx
from bluepages.semantic.reasoning import Finding, SemanticResult, reason_about_diff
from bluepages.testdata import ChangeKind, Department
from tests.unit.fakes import EMPTY_FINDINGS, ScriptedClient, findings_answer

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


@pytest.fixture
def mechanical():
    before = parse_fdx(FIXTURES / "small-draft-1.fdx")
    after = parse_fdx(FIXTURES / "small-draft-2.fdx")
    return diff_drafts(align(before, after))


def _prompt_for(client: ScriptedClient, scene: str) -> str:
    """The prompt that asked about one scene."""
    matches = [p for p in client.prompts if p.startswith(f"SCENE {scene}\n") or f"SCENE {scene} " in p.split("\n")[0]]
    assert matches, f"no prompt for scene {scene}"
    return matches[0]


def test_every_changed_scene_is_reasoned_about(mechanical):
    """Changed, inserted and omitted scenes each need a judgment."""
    client = ScriptedClient(default=EMPTY_FINDINGS)
    result = reason_about_diff(mechanical, client)

    # 2, 3, 4, 7 changed; 5 omitted; 5A inserted.
    assert result.scenes_reasoned == 6
    assert len(client.prompts) == 6


def test_reasoning_uses_the_judgment_model(mechanical):
    """Layer 3.4 is the product. It starts at Sonnet, not Haiku."""
    client = ScriptedClient(default=EMPTY_FINDINGS)
    reason_about_diff(mechanical, client)
    assert client.judgment_calls == len(client.prompts)


def test_prompt_carries_the_relocation_question(mechanical):
    """The letter opener question has to reach the model to be answered.

    The mechanical layer refuses to claim the object moved; the prompt must
    therefore put the choice in front of the model explicitly.
    """
    client = ScriptedClient(default=EMPTY_FINDINGS)
    reason_about_diff(mechanical, client)

    prompt = _prompt_for(client, "7")
    assert "brass letter opener" in prompt
    assert "scene 3" in prompt
    assert "same object moved" in prompt


def test_prompt_carries_the_heading_flip(mechanical):
    """A DAY to NIGHT flip produces no spans, so it can only arrive as a heading."""
    client = ScriptedClient(default=EMPTY_FINDINGS)
    reason_about_diff(mechanical, client)

    prompt = _prompt_for(client, "2")
    assert "HEADING CHANGED" in prompt
    assert "FARMHOUSE KITCHEN - DAY" in prompt
    assert "FARMHOUSE KITCHEN - NIGHT" in prompt


def test_prompt_carries_the_rename_evidence(mechanical):
    """Rename vs new role needs both cues and the unchanged dialogue around them."""
    client = ScriptedClient(default=EMPTY_FINDINGS)
    reason_about_diff(mechanical, client)

    prompt = _prompt_for(client, "4")
    assert "JANITOR" in prompt
    assert "CUSTODIAN" in prompt


def test_omitted_scene_prompt_says_omitted(mechanical):
    client = ScriptedClient(default=EMPTY_FINDINGS)
    reason_about_diff(mechanical, client)

    prompt = next(p for p in client.prompts if "MARKED OMITTED" in p)
    assert "SCENE 5" in prompt
    assert "COUNTY SHERIFF" in prompt


def test_inserted_scene_prompt_says_new(mechanical):
    client = ScriptedClient(default=EMPTY_FINDINGS)
    reason_about_diff(mechanical, client)

    prompt = next(p for p in client.prompts if "IS NEW IN THIS DRAFT" in p)
    assert "SCENE 5A" in prompt
    assert "Ford Bronco" in prompt


def test_finding_about_an_unchanged_scene_is_rejected(mechanical):
    """The failure mode that matters most.

    A model given one scene and asked what it means will occasionally answer
    about the draft in general. Scene 6 is identical in both drafts, and a
    confident finding about it is indistinguishable from a real one once it
    reaches a department.
    """
    client = ScriptedClient(
        default=findings_answer(
            {
                "kind": "element_added",
                "summary": "A new radio appears in the truck.",
                "scene": "6",
                "departments": ["props"],
                "confidence": 0.95,
            }
        )
    )
    stream = CollectingStream()
    result = reason_about_diff(mechanical, client, stream=stream)

    assert result.findings == []
    assert len(result.rejected) == len(client.prompts)
    assert any("scene" in w.message for w in stream.of_kind(EventKind.PARSE_WARNING))


def test_relocation_naming_two_changed_scenes_survives(mechanical):
    """A relocation legitimately names both scenes, and both did change.

    The unchanged-scene guard must not throw away the single most important
    finding the product makes.
    """
    client = ScriptedClient(
        rules=[
            (
                "SCENE 7",
                findings_answer(
                    {
                        "kind": "element_relocated",
                        "summary": "The brass letter opener moves from scene 3 to scene 7.",
                        "scene": "7",
                        "from_scene": "3",
                        "element": "brass letter opener",
                        "departments": ["props"],
                        "confidence": 0.92,
                    }
                ),
            )
        ],
        default=EMPTY_FINDINGS,
    )
    result = reason_about_diff(mechanical, client)

    assert len(result.findings) == 1
    finding = result.findings[0]
    assert finding.kind is ChangeKind.ELEMENT_RELOCATED
    assert finding.from_scene == "3"
    assert result.rejected == []


def test_malformed_answer_skips_one_scene_only(mechanical):
    """One unusable response must not lose the other five scenes' judgments."""
    client = ScriptedClient(
        rules=[("SCENE 4", "sorry, I cannot do that")],
        default=findings_answer(
            {
                "kind": "action_rewritten",
                "summary": "something changed",
                "scene": "7",
                "departments": ["ad"],
                "confidence": 0.8,
            }
        ),
    )
    stream = CollectingStream()
    result = reason_about_diff(mechanical, client, stream=stream)

    # Five scenes answered; scene 4's malformed response was dropped with a warning.
    assert len(result.findings) == 5
    assert stream.of_kind(EventKind.PARSE_WARNING)


def test_unknown_department_is_dropped_not_fatal(mechanical):
    """A bad route is a bad route, not a reason to lose the judgment."""
    client = ScriptedClient(
        rules=[
            (
                "SCENE 2",
                findings_answer(
                    {
                        "kind": "time_of_day_changed",
                        "summary": "Scene 2 flips DAY to NIGHT.",
                        "scene": "2",
                        "departments": ["schedule", "catering", "locations"],
                        "confidence": 0.97,
                    }
                ),
            )
        ],
        default=EMPTY_FINDINGS,
    )
    result = reason_about_diff(mechanical, client)

    assert len(result.findings) == 1
    assert result.findings[0].departments == [Department.SCHEDULE, Department.LOCATIONS]


def test_low_confidence_is_flagged_uncertain():
    unsure = Finding(kind=ChangeKind.ELEMENT_ADDED, summary="x", scene="7", confidence=0.4)
    sure = Finding(kind=ChangeKind.ELEMENT_ADDED, summary="x", scene="7", confidence=0.9)
    assert unsure.uncertain
    assert not sure.uncertain


def test_findings_are_ordered_by_scene(mechanical):
    """5A must read between 5 and 6, not after 59."""
    result = SemanticResult(
        findings=[
            Finding(kind=ChangeKind.ELEMENT_ADDED, summary="a", scene="7"),
            Finding(kind=ChangeKind.SCENE_INSERTED, summary="b", scene="5A"),
            Finding(kind=ChangeKind.SCENE_OMITTED, summary="c", scene="5"),
        ]
    )
    result.findings.sort(key=lambda f: (int("".join(c for c in f.scene if c.isdigit())), f.scene))
    assert [f.scene for f in result.findings] == ["5", "5A", "7"]


def test_department_counts(mechanical):
    result = SemanticResult(
        findings=[
            Finding(
                kind=ChangeKind.ELEMENT_RELOCATED,
                summary="a",
                scene="7",
                departments=[Department.PROPS],
            ),
            Finding(
                kind=ChangeKind.TIME_OF_DAY_CHANGED,
                summary="b",
                scene="2",
                departments=[Department.SCHEDULE, Department.LOCATIONS],
            ),
        ]
    )
    assert result.department_counts == {"locations": 1, "props": 1, "schedule": 1}
    assert len(result.by_department(Department.PROPS)) == 1


def test_extracted_elements_reach_the_prompt(mechanical):
    """Layer 3.3's output is evidence for Layer 3.4, not a separate report."""
    from bluepages.semantic.elements import (
        DraftElements,
        ElementCategory,
        ExtractedElement,
        SceneExtraction,
    )

    elements = DraftElements(
        scenes=[
            SceneExtraction(
                scene_number="5A",
                elements=[
                    ExtractedElement(
                        name="Ford Bronco", category=ElementCategory.VEHICLE, branded=True
                    )
                ],
            )
        ]
    )
    client = ScriptedClient(default=EMPTY_FINDINGS)
    reason_about_diff(mechanical, client, elements=elements)

    prompt = next(p for p in client.prompts if "SCENE 5A" in p)
    assert "vehicle: Ford Bronco [branded]" in prompt


class TestNumericSceneNumbers:
    """A model that writes `"scene": 7` must not lose the whole finding.

    Scene numbers are strings because of inserts like 34A, but a model looking
    at scene 7 writes the JSON number 7. Rejecting that discarded correct
    judgments over typing: one unquoted field lost every finding in the scene.
    """

    def test_an_integer_scene_is_accepted(self):
        from bluepages.llm.structured import parse_as
        from bluepages.semantic.reasoning import SceneFindings

        parsed = parse_as(
            '{"findings":[{"kind":"element_relocated","summary":"s",'
            '"scene":7,"from_scene":3,"departments":["props"],'
            '"reasoning":"r","element":"brass letter opener","confidence":0.95}]}',
            SceneFindings,
        )
        finding = parsed.findings[0]
        assert finding.scene == "7"
        assert finding.from_scene == "3"

    def test_a_quoted_scene_is_unchanged(self):
        """The common case must not regress: 34A has to survive verbatim."""
        from bluepages.llm.structured import parse_as
        from bluepages.semantic.reasoning import SceneFindings

        parsed = parse_as(
            '{"findings":[{"kind":"scene_inserted","summary":"s",'
            '"scene":"34A","departments":["schedule"],"reasoning":"r"}]}',
            SceneFindings,
        )
        assert parsed.findings[0].scene == "34A"

    def test_a_float_scene_does_not_become_seven_point_zero(self):
        from bluepages.llm.structured import parse_as
        from bluepages.semantic.reasoning import SceneFindings

        parsed = parse_as(
            '{"findings":[{"kind":"scene_omitted","summary":"s",'
            '"scene":7.0,"departments":["schedule"],"reasoning":"r"}]}',
            SceneFindings,
        )
        assert parsed.findings[0].scene == "7"


class TestSceneLabelNormalisation:
    """A model that labels its scene must not lose a correct judgment.

    "SCENE 4: Character renamed" and "2/INT. FARMHOUSE KITCHEN" both name a
    scene the diff flagged, but the guard compares against bare numbers and
    dropped them. The judgment was right; only the formatting was loose.
    """

    def test_a_labelled_scene_reduces_to_its_number(self):
        from bluepages.llm.structured import parse_as
        from bluepages.semantic.reasoning import SceneFindings

        parsed = parse_as(
            '{"findings":[{"kind":"character_renamed","summary":"s",'
            '"scene":"SCENE 4: Character renamed","departments":["cast"],'
            '"reasoning":"r"}]}',
            SceneFindings,
        )
        assert parsed.findings[0].scene == "4"

    def test_a_heading_suffix_is_stripped(self):
        from bluepages.llm.structured import parse_as
        from bluepages.semantic.reasoning import SceneFindings

        parsed = parse_as(
            '{"findings":[{"kind":"time_of_day_changed","summary":"s",'
            '"scene":"2/INT. FARMHOUSE KITCHEN - NIGHT","departments":["schedule"],'
            '"reasoning":"r"}]}',
            SceneFindings,
        )
        assert parsed.findings[0].scene == "2"

    def test_an_insert_number_survives(self):
        """34A must not become 34: they are different scenes."""
        from bluepages.llm.structured import parse_as
        from bluepages.semantic.reasoning import SceneFindings

        parsed = parse_as(
            '{"findings":[{"kind":"scene_inserted","summary":"s",'
            '"scene":"Scene 34A - EXT. LOT","departments":["schedule"],'
            '"reasoning":"r"}]}',
            SceneFindings,
        )
        assert parsed.findings[0].scene == "34A"

    def test_a_value_with_no_number_is_left_for_the_guard(self):
        """Unparseable stays unparseable, so the guard can still reject it."""
        from bluepages.llm.structured import parse_as
        from bluepages.semantic.reasoning import SceneFindings

        parsed = parse_as(
            '{"findings":[{"kind":"action_rewritten","summary":"s",'
            '"scene":"the kitchen scene","departments":["ad"],"reasoning":"r"}]}',
            SceneFindings,
        )
        assert parsed.findings[0].scene == "the kitchen scene"

    def test_the_guard_still_rejects_a_scene_the_diff_did_not_flag(self):
        """Normalising must not weaken the hallucination guard."""
        from bluepages.semantic.reasoning import Finding, _reject_unfounded
        from bluepages.testdata import ChangeKind

        finding = Finding(kind=ChangeKind.ACTION_REWRITTEN, summary="s", scene="SCENE 99")
        kept, rejected, ripples = _reject_unfounded([finding], asked_about="4", flagged={"4"})
        assert kept == []
        assert len(rejected) == 1
        assert len(ripples) == 1
