"""The internal script model.

Design notes that matter downstream:

- `SceneNumber` is a parsed type, not a string. Productions insert scenes as
  `34A`, `34B` between 34 and 35, and the diff has to order them correctly to
  align drafts. Sorting `"34A"` as text puts it after `"340"`.
- Scenes are never deleted, only marked `OMITTED`. That convention is why scene
  numbers are stable across drafts, and it is what makes alignment possible at
  all. `Scene.omitted` records it.
- `Element` is deliberately empty at parse time. Tier 1 gives us typed
  paragraphs, not props. Layer 3.3 fills elements in with a model pass.
"""

from __future__ import annotations

import re
from enum import Enum
from functools import total_ordering
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, computed_field


class SourceTier(str, Enum):
    """Which parser produced this. Confidence descends with the tier."""

    FDX = "fdx"          # Tier 1: Final Draft XML. Typed, exact.
    PDF = "pdf"          # Tier 2: coordinate heuristics.
    OCR = "ocr"          # Tier 3: scanned, lossy.
    FOUNTAIN = "fountain"


class ElementType(str, Enum):
    """Paragraph types.

    The first seven map 1:1 onto Final Draft's `<Paragraph Type="...">` values,
    so tier 1 classification is a lookup rather than a heuristic.
    """

    SCENE_HEADING = "scene_heading"
    ACTION = "action"
    CHARACTER = "character"
    DIALOGUE = "dialogue"
    PARENTHETICAL = "parenthetical"
    TRANSITION = "transition"
    SHOT = "shot"
    CAST_LIST = "cast_list"
    GENERAL = "general"
    UNKNOWN = "unknown"


class InteriorExterior(str, Enum):
    INT = "INT"
    EXT = "EXT"
    INT_EXT = "INT/EXT"
    UNKNOWN = "UNKNOWN"


class TimeOfDay(str, Enum):
    """Time of day from the slugline.

    DAY->NIGHT flips are a scheduling consequence and possibly a location
    re-quote, so this is a typed field rather than free text.
    """

    DAY = "DAY"
    NIGHT = "NIGHT"
    DAWN = "DAWN"
    DUSK = "DUSK"
    MORNING = "MORNING"
    AFTERNOON = "AFTERNOON"
    EVENING = "EVENING"
    CONTINUOUS = "CONTINUOUS"
    LATER = "LATER"
    MOMENTS_LATER = "MOMENTS LATER"
    SAME = "SAME"
    UNKNOWN = "UNKNOWN"


# Ordered longest-first so "MOMENTS LATER" is not matched as "LATER".
_TIME_PATTERNS: list[tuple[str, TimeOfDay]] = [
    ("MOMENTS LATER", TimeOfDay.MOMENTS_LATER),
    ("CONTINUOUS", TimeOfDay.CONTINUOUS),
    ("AFTERNOON", TimeOfDay.AFTERNOON),
    ("MORNING", TimeOfDay.MORNING),
    ("EVENING", TimeOfDay.EVENING),
    ("MAGIC HOUR", TimeOfDay.DUSK),
    ("SUNRISE", TimeOfDay.DAWN),
    ("SUNSET", TimeOfDay.DUSK),
    ("NIGHT", TimeOfDay.NIGHT),
    ("LATER", TimeOfDay.LATER),
    ("DAWN", TimeOfDay.DAWN),
    ("DUSK", TimeOfDay.DUSK),
    ("SAME", TimeOfDay.SAME),
    ("DAY", TimeOfDay.DAY),
]

# "34", "34A", "A34", "34-A", "101B". Suffix letters are inserted scenes;
# prefix letters appear in some productions for the same purpose.
_SCENE_NUM_RE = re.compile(
    r"^\s*(?P<prefix>[A-Za-z]*)(?P<number>\d+)[-\s]?(?P<suffix>[A-Za-z]*)\s*$"
)


@total_ordering
class SceneNumber(BaseModel):
    """A scene number, parsed so it can be ordered and compared across drafts.

    This is the cross-draft alignment anchor (PLAN.md 3.1). It must sort the way
    a script supervisor would read it: 34, 34A, 34B, 35.
    """

    model_config = ConfigDict(frozen=True)

    raw: str
    number: int | None = None
    prefix: str = ""
    suffix: str = ""

    @classmethod
    def parse(cls, raw: str | None) -> SceneNumber | None:
        """Parse a scene number. Returns None for absent or unparseable input.

        An unnumbered scene is not an error: many drafts are unnumbered until
        they go into production. Alignment falls back to heading matching.
        """
        if raw is None:
            return None
        text = raw.strip()
        if not text:
            return None
        m = _SCENE_NUM_RE.match(text)
        if not m:
            # Keep the raw value so nothing is silently dropped.
            return cls(raw=text)
        return cls(
            raw=text,
            number=int(m.group("number")),
            prefix=m.group("prefix").upper(),
            suffix=m.group("suffix").upper(),
        )

    @property
    def is_insert(self) -> bool:
        """True for `34A`-style scenes added between two existing scenes."""
        return bool(self.suffix or self.prefix)

    @property
    def sort_key(self) -> tuple[int, str, str]:
        """Orders 34 < 34A < 34B < 35, and unparseable numbers last."""
        return (
            self.number if self.number is not None else 10**9,
            self.suffix,
            self.prefix,
        )

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, SceneNumber):
            return NotImplemented
        return self.sort_key < other.sort_key

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, SceneNumber):
            return NotImplemented
        # Compare on parsed identity, so " 34 " and "34" are the same scene.
        return self.sort_key == other.sort_key

    def __hash__(self) -> int:
        return hash(self.sort_key)

    def __str__(self) -> str:
        return self.raw


class RevisionMark(BaseModel):
    """A Final Draft revision mark on a run of text.

    `.fdx` carries `RevisionID` on individual `<Text>` runs plus a `<Revisions>`
    section naming all nine coloured sets. On a real production file this is
    ground truth our own diff can be validated against.
    """

    revision_id: int
    name: str | None = None
    colour: str | None = None


class ScriptNote(BaseModel):
    """A note attached to a paragraph. Production metadata, not screenplay text."""

    text: str
    scene_number: str | None = None


class Element(BaseModel):
    """One paragraph of the screenplay.

    At parse time this is a typed block of text. Layer 3.3 later attaches the
    extracted production elements (props, wardrobe, vehicles) via `extracted`.
    """

    type: ElementType
    text: str
    # Set only when the source marks this paragraph as revised.
    revised: bool = False
    revision_marks: list[RevisionMark] = Field(default_factory=list)
    notes: list[ScriptNote] = Field(default_factory=list)
    # Character cue this dialogue/parenthetical belongs to. Resolved at parse.
    speaker: str | None = None
    # Layer 3.3 output. Empty until the extraction pass runs.
    extracted: dict[str, Any] = Field(default_factory=dict)
    # Page number when the source provides it.
    page: str | None = None

    @property
    def is_dialogue_block(self) -> bool:
        return self.type in (ElementType.DIALOGUE, ElementType.PARENTHETICAL)


class Scene(BaseModel):
    """One scene: its slugline, its parsed heading fields, and its elements."""

    number: SceneNumber | None = None
    heading: str = ""
    int_ext: InteriorExterior = InteriorExterior.UNKNOWN
    location: str = ""
    time_of_day: TimeOfDay = TimeOfDay.UNKNOWN
    elements: list[Element] = Field(default_factory=list)

    # Marked OMITTED rather than deleted. The convention that makes scene
    # numbers stable, and therefore makes cross-draft alignment possible.
    omitted: bool = False

    # Metadata from `SceneProperties` when present.
    title: str | None = None
    page: str | None = None
    summary: str | None = None
    # Notes attached to the scene heading itself, e.g. "confirm practical lamp".
    # Production metadata the AD reads, not screenplay text.
    notes: list[ScriptNote] = Field(default_factory=list)

    # Position in the draft, 0-based. Distinct from `number`: a scene can move
    # without its number changing, and that is a schedule fact worth surfacing.
    index: int = 0
    revised: bool = False

    @computed_field  # type: ignore[prop-decorator]
    @property
    def slug(self) -> str:
        """A stable identity for this scene independent of draft position."""
        n = str(self.number) if self.number else f"idx{self.index}"
        return f"{n}:{self.int_ext.value}:{self.location}".upper()

    @property
    def action_text(self) -> str:
        """Action lines only. What physical consequences are reasoned over."""
        return "\n".join(e.text for e in self.elements if e.type is ElementType.ACTION)

    @property
    def dialogue_text(self) -> str:
        return "\n".join(e.text for e in self.elements if e.is_dialogue_block)

    @property
    def full_text(self) -> str:
        """Heading plus every element. The substrate for the mechanical diff."""
        body = "\n".join(e.text for e in self.elements)
        return f"{self.heading}\n{body}" if self.heading else body

    @property
    def characters(self) -> list[str]:
        """Speaking characters, in order of first appearance, deduplicated."""
        seen: dict[str, None] = {}
        for e in self.elements:
            if e.type is ElementType.CHARACTER and e.text.strip():
                seen.setdefault(normalise_cue(e.text), None)
        return list(seen)

    def __repr__(self) -> str:
        return f"<Scene {self.number or self.index} {self.heading[:48]!r}>"


class Screenplay(BaseModel):
    """A parsed draft: every scene, plus where it came from.

    This is what a parser returns and what the diff consumes.
    """

    title: str | None = None
    scenes: list[Scene] = Field(default_factory=list)
    source_tier: SourceTier = SourceTier.FDX
    source_path: str | None = None
    # Revision sets declared by the file, keyed by RevisionID.
    revisions: dict[int, RevisionMark] = Field(default_factory=dict)
    # Anything the parser wants to record: page counts, unparsed constructs.
    meta: dict[str, Any] = Field(default_factory=dict)

    @property
    def scene_count(self) -> int:
        return len(self.scenes)

    @property
    def numbered_scenes(self) -> list[Scene]:
        return [s for s in self.scenes if s.number is not None]

    @property
    def has_scene_numbers(self) -> bool:
        """Whether numbers can anchor alignment, or headings must instead.

        A draft is only usefully numbered if most scenes carry a number, so this
        is a majority test rather than an any() test.
        """
        if not self.scenes:
            return False
        return len(self.numbered_scenes) >= len(self.scenes) * 0.5

    @property
    def omitted_scenes(self) -> list[Scene]:
        return [s for s in self.scenes if s.omitted]

    def by_number(self) -> dict[SceneNumber, Scene]:
        """Scenes keyed by number, for alignment. Later wins on duplicates."""
        return {s.number: s for s in self.scenes if s.number is not None}

    def scene(self, number: str) -> Scene | None:
        """Look up one scene by its number as written."""
        target = SceneNumber.parse(number)
        if target is None:
            return None
        for s in self.scenes:
            if s.number is not None and s.number == target:
                return s
        return None

    @property
    def all_characters(self) -> list[str]:
        seen: dict[str, None] = {}
        for scene in self.scenes:
            for c in scene.characters:
                seen.setdefault(c, None)
        return sorted(seen)

    def stats(self) -> dict[str, Any]:
        """A summary suitable for printing after a parse."""
        by_type: dict[str, int] = {}
        for scene in self.scenes:
            for e in scene.elements:
                by_type[e.type.value] = by_type.get(e.type.value, 0) + 1
        return {
            "title": self.title,
            "source_tier": self.source_tier.value,
            "scenes": self.scene_count,
            "numbered_scenes": len(self.numbered_scenes),
            "has_scene_numbers": self.has_scene_numbers,
            "omitted_scenes": len(self.omitted_scenes),
            "characters": len(self.all_characters),
            "elements_by_type": dict(sorted(by_type.items())),
            "revised_scenes": sum(1 for s in self.scenes if s.revised),
            "revision_sets": len(self.revisions),
        }

    def __repr__(self) -> str:
        return (
            f"<Screenplay {self.title!r} scenes={self.scene_count} "
            f"tier={self.source_tier.value}>"
        )


# `MARY (CONT'D)`, `MARY (V.O.)`, `MARY (O.S.)` and `MARY` are one character.
_CUE_SUFFIX_RE = re.compile(
    # Both apostrophes are deliberate: Final Draft writes a typographic
    # right-single-quote in CONT'D, while hand-edited files use the ASCII one.
    r"\((?:CONT[’']?D|CONTD|CONT|V\.?\s*O\.?|O\.?\s*S\.?|O\.?\s*C\.?|SUBTITLED?|"  # noqa: RUF001
    r"FILTERED|PRELAP|INTO PHONE|ON PHONE)\)",
    re.IGNORECASE,
)


def normalise_cue(cue: str) -> str:
    """Strip a character cue down to the character's name.

    Getting this wrong makes a rename look like a new role, which is exactly the
    judgment call the product is supposed to get right.
    """
    text = cue.strip().upper()
    text = _CUE_SUFFIX_RE.sub("", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip(" .*")


def parse_heading(heading: str) -> tuple[InteriorExterior, str, TimeOfDay]:
    """Split a slugline into INT/EXT, location, and time of day.

    `INT. FARMHOUSE KITCHEN - NIGHT` -> (INT, "FARMHOUSE KITCHEN", NIGHT)

    Sluglines are a strong convention but not a grammar, so each part degrades
    to UNKNOWN independently rather than failing the whole parse.
    """
    text = heading.strip().upper()
    text = re.sub(r"^\s*\d+[A-Z]?[.\s]+", "", text)  # leading scene number

    int_ext = InteriorExterior.UNKNOWN
    # INT/EXT first: it contains both other tokens as substrings.
    if re.match(r"^\s*(INT\.?\s*/\s*EXT|EXT\.?\s*/\s*INT|I\s*/\s*E)\.?\b", text):
        int_ext = InteriorExterior.INT_EXT
        text = re.sub(r"^\s*(INT\.?\s*/\s*EXT|EXT\.?\s*/\s*INT|I\s*/\s*E)\.?\s*", "", text)
    elif re.match(r"^\s*INT\.?\b", text):
        int_ext = InteriorExterior.INT
        text = re.sub(r"^\s*INT\.?\s*", "", text)
    elif re.match(r"^\s*EXT\.?\b", text):
        int_ext = InteriorExterior.EXT
        text = re.sub(r"^\s*EXT\.?\s*", "", text)

    # Time of day is the last dash-separated segment, when it names a time.
    time_of_day = TimeOfDay.UNKNOWN
    location = text.strip()
    if "-" in text or "—" in text:
        parts = re.split(r"\s+[-—]+\s+|\s*[-—]\s*$", text)
        parts = [p.strip() for p in parts if p.strip()]
        if len(parts) > 1:
            tail = parts[-1]
            for pattern, tod in _TIME_PATTERNS:
                if pattern in tail:
                    time_of_day = tod
                    location = " - ".join(parts[:-1]).strip()
                    break
            else:
                location = " - ".join(parts).strip()
    if time_of_day is TimeOfDay.UNKNOWN:
        # Some sluglines omit the dash: "INT. KITCHEN NIGHT".
        for pattern, tod in _TIME_PATTERNS:
            if re.search(rf"\b{re.escape(pattern)}\s*$", location):
                time_of_day = tod
                location = re.sub(rf"\s*\b{re.escape(pattern)}\s*$", "", location).strip()
                break

    return int_ext, location.strip(" .-—"), time_of_day
