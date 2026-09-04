"""Element extraction (Layer 3.3).

Claude Haiku over each scene: what does this scene physically require. Props,
cast, vehicles, wardrobe, set dressing, SFX, stunts, animals, extras, and the
branded or titled things that carry a rights liability.

This is the layer that populates the element database everything else reads,
including the video features. It is high volume and cheap by design: one call
per scene, on the bulk model, cached on disk, and bounded by the run budget.

Two things it deliberately does not do:

- It does not compare drafts. Extraction describes one scene in one draft. What
  changed between them is Layer 3.4's question, and answering it here would mean
  the extractor guessing at cross-draft identity without seeing both sides.
- It does not extract from dialogue. A prop named in dialogue is a mention, not
  a thing on the truck. Characters who speak are read from the cues directly,
  which is exact and free, rather than asked of a model.
"""

from __future__ import annotations

import concurrent.futures
from enum import Enum

from pydantic import BaseModel, Field

from ripple.events import EventKind, EventStream, NullStream
from ripple.llm import ModelClient
from ripple.llm.structured import SchemaError, parse_as
from ripple.model import Scene, Screenplay


class ElementCategory(str, Enum):
    """What kind of thing an element is, in production terms.

    These are the categories a breakdown actually uses, so each maps onto a
    department without a second translation step.
    """

    PROP = "prop"                  # handled by an actor
    SET_DRESSING = "set_dressing"  # in shot, not handled
    WARDROBE = "wardrobe"
    VEHICLE = "vehicle"
    CAST = "cast"
    EXTRAS = "extras"
    ANIMAL = "animal"
    SFX = "sfx"
    STUNT = "stunt"
    SOUND = "sound"
    SET = "set"
    OTHER = "other"


# Which department owns each category. Props and set dressing split because a
# handled object and a dressed one are different purchase orders.
CATEGORY_DEPARTMENT: dict[ElementCategory, str] = {
    ElementCategory.PROP: "props",
    ElementCategory.SET_DRESSING: "art",
    ElementCategory.WARDROBE: "wardrobe",
    ElementCategory.VEHICLE: "transport",
    ElementCategory.CAST: "cast",
    ElementCategory.EXTRAS: "cast",
    ElementCategory.ANIMAL: "art",
    ElementCategory.SFX: "sfx",
    ElementCategory.STUNT: "stunts",
    ElementCategory.SOUND: "sfx",
    ElementCategory.SET: "locations",
    ElementCategory.OTHER: "props",
}


class ExtractedElement(BaseModel):
    """One physical thing a scene requires."""

    name: str = Field(description="The element as the script names it, singular")
    category: ElementCategory
    # The action line it came from. Provenance: an element the AD cannot trace
    # back to a line of script is one they cannot check.
    quote: str = Field(default="", description="The phrase in the scene that establishes it")
    # Branded, titled, or otherwise someone else's intellectual property.
    branded: bool = Field(
        default=False,
        description="True for a real brand, song, book, artwork or recognisable building",
    )
    notes: str = Field(default="", description="Only if a department would need it")

    @property
    def department(self) -> str:
        return CATEGORY_DEPARTMENT[self.category]

    @property
    def key(self) -> str:
        """Identity for matching the same element across scenes and drafts."""
        return f"{self.category.value}:{self.name.strip().lower()}"


class SceneElements(BaseModel):
    """Everything one scene requires. The model returns exactly this."""

    elements: list[ExtractedElement] = Field(default_factory=list)


class SceneExtraction(BaseModel):
    """A scene's extracted elements, plus which model answered."""

    scene_number: str
    elements: list[ExtractedElement] = Field(default_factory=list)
    model_name: str = ""
    via_fallback: bool = False
    cached: bool = False

    def of_category(self, category: ElementCategory) -> list[ExtractedElement]:
        return [e for e in self.elements if e.category is category]


class DraftElements(BaseModel):
    """Every scene's elements for one draft. The element database, in memory."""

    scenes: list[SceneExtraction] = Field(default_factory=list)

    def by_scene(self) -> dict[str, SceneExtraction]:
        return {s.scene_number: s for s in self.scenes}

    def index(self) -> dict[str, list[str]]:
        """Element key -> the scenes it appears in.

        This is what makes "the same object, moved" answerable: an element in
        two scenes across two drafts is the question Layer 3.4 judges.
        """
        found: dict[str, list[str]] = {}
        for scene in self.scenes:
            for element in scene.elements:
                found.setdefault(element.key, []).append(scene.scene_number)
        return found

    @property
    def all_elements(self) -> list[ExtractedElement]:
        return [e for s in self.scenes for e in s.elements]

    @property
    def branded(self) -> list[tuple[str, ExtractedElement]]:
        """Branded elements with the scene they are in. Clearance reads this."""
        return [(s.scene_number, e) for s in self.scenes for e in s.elements if e.branded]

    def summary(self) -> dict[str, object]:
        by_category: dict[str, int] = {}
        for element in self.all_elements:
            by_category[element.category.value] = by_category.get(element.category.value, 0) + 1
        return {
            "scenes": len(self.scenes),
            "elements": len(self.all_elements),
            "distinct": len(self.index()),
            "branded": len(self.branded),
            "by_category": dict(sorted(by_category.items())),
        }


SYSTEM = """You are a script supervisor doing a production breakdown.

You read one scene and list what it physically requires on the day: the things a department has to buy, dress, wear, drive, wrangle, rig or book.

Rules:
- Only what the scene's action establishes. Never invent what a location would plausibly contain.
- Something named only in dialogue is a mention, not a requirement. Skip it.
- A prop is handled by an actor. Set dressing is in shot but untouched. Wardrobe is worn. That distinction decides who pays for it, so get it right.
- Mark branded=true for any real brand, song title, book title, artwork or recognisable real building. These are rights liabilities and are the reason this pass exists at all.
- Name things as the script names them. Do not generalise a "brass letter opener" into a "letter opener": the adjective is what makes it the same object in the next draft.
- Return an empty list when a scene requires nothing physical. That is a real answer, not a failure.

Return only JSON matching the schema. No prose, no explanation."""


def _scene_prompt(scene: Scene) -> str:
    """What the model sees. Action and heading only, and never the whole draft.

    Dialogue is excluded on purpose: it is most of a scene's tokens and it names
    things that do not exist on the day.
    """
    lines = [f"SCENE {scene.number or '(unnumbered)'}: {scene.heading}", ""]
    action = scene.action_text.strip()
    lines.append(action if action else "(no action lines)")
    speaking = scene.characters
    if speaking:
        lines.append("")
        lines.append(f"Speaking in this scene: {', '.join(speaking)}")
    lines.append("")
    lines.append(
        'Return JSON: {"elements": [{"name": ..., "category": ..., "quote": ..., '
        '"branded": true|false, "notes": ...}]}'
    )
    lines.append("category is one of: " + ", ".join(c.value for c in ElementCategory))
    return "\n".join(lines)


def extract_scene(
    scene: Scene,
    client: ModelClient,
    stream: EventStream | None = None,
) -> SceneExtraction:
    """Extract one scene's elements. One model call, on the bulk model."""
    stream = stream or NullStream()
    number = str(scene.number) if scene.number else f"idx{scene.index}"

    # An omitted scene has no content and nothing to require. Asking costs a
    # call and returns an empty list every time.
    if scene.omitted or not scene.elements:
        return SceneExtraction(scene_number=number, model_name="skipped")

    completion = client.complete(
        prompt=_scene_prompt(scene),
        system=SYSTEM,
        judgment=False,
        max_tokens=1500,
        cache_key_extra="extract-v1",
    )
    parsed = parse_as(completion.text, SceneElements)

    # Speaking characters come from the cues, which are exact. The model is not
    # asked for them, and anything it volunteers is replaced rather than merged.
    elements = [e for e in parsed.elements if e.category is not ElementCategory.CAST]
    elements.extend(
        ExtractedElement(name=cue, category=ElementCategory.CAST, quote=cue)
        for cue in scene.characters
    )

    result = SceneExtraction(
        scene_number=number,
        elements=elements,
        model_name=completion.model_name,
        via_fallback=completion.via_fallback,
        cached=completion.cached,
    )
    for element in result.elements:
        stream.emit(
            EventKind.ELEMENT_FOUND,
            f"scene {number}: {element.category.value} {element.name}"
            + ("  [branded]" if element.branded else ""),
            scene_number=number,
            element=element.name,
            category=element.category.value,
            department=element.department,
            branded=element.branded,
        )
    return result


def extract_draft(
    screenplay: Screenplay,
    client: ModelClient,
    stream: EventStream | None = None,
    scenes: list[str] | None = None,
    max_workers: int = 4,
) -> DraftElements:
    """Extract every scene in a draft, or only the ones named in `scenes`.

    Scenes run concurrently because a 120-scene draft is 120 independent calls
    and doing them in series is minutes of waiting. The concurrency is modest on
    purpose: Bedrock throttles under parallel load, and while the fallback chain
    handles that, degrading to Haiku because we asked too fast is a self-inflicted
    downgrade.
    """
    stream = stream or NullStream()
    wanted = [
        s
        for s in screenplay.scenes
        if scenes is None or (s.number is not None and str(s.number) in scenes)
    ]
    stream.emit(
        EventKind.INFO,
        f"extracting elements from {len(wanted)} scenes",
        scenes=len(wanted),
    )

    results: list[SceneExtraction] = []
    failures: list[tuple[str, Exception]] = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(extract_scene, s, client, stream): s for s in wanted}
        for future in concurrent.futures.as_completed(futures):
            scene = futures[future]
            number = str(scene.number) if scene.number else f"idx{scene.index}"
            try:
                results.append(future.result())
            except SchemaError as exc:
                # A malformed answer on one scene should not lose the other 119.
                # It is still reported: silently dropping a scene means an
                # element list that is quietly incomplete.
                failures.append((number, exc))
                stream.emit(
                    EventKind.PARSE_WARNING,
                    f"scene {number}: extraction returned unusable JSON, skipped",
                    scene_number=number,
                    error=str(exc)[:200],
                )

    results.sort(key=lambda r: _scene_sort_key(r.scene_number))
    draft = DraftElements(scenes=results)
    summary = draft.summary()
    stream.emit(
        EventKind.INFO,
        f"extracted {summary['elements']} elements "
        f"({summary['distinct']} distinct) from {summary['scenes']} scenes"
        + (f", {len(failures)} scenes failed" if failures else ""),
        elements=summary,
        failed_scenes=[n for n, _ in failures],
    )
    return draft


def _scene_sort_key(number: str) -> tuple[int, str]:
    """Order scene numbers the way a script supervisor reads them: 5, 5A, 6."""
    digits = "".join(c for c in number if c.isdigit())
    return (int(digits) if digits else 10**9, number)


__all__ = [
    "CATEGORY_DEPARTMENT",
    "DraftElements",
    "ElementCategory",
    "ExtractedElement",
    "SceneElements",
    "SceneExtraction",
    "extract_draft",
    "extract_scene",
]
