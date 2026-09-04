"""Tier 2: the PDF parser (Layer 1.3).

Screenplay structure in a PDF is positional, not semantic. There is no tag
saying "this is dialogue"; there is only a line of text sitting 2.5 inches from
the left edge. `pdfplumber` exposes those coordinates and this module turns them
back into typed elements.

The central design decision, and it is evidence-driven rather than assumed:

    **Margins are measured per document, not hardcoded.**

The conventional figures are action at 1.5", dialogue at 2.5", character cues at
3.7". Measured against two real production screenplays:

    element         Social Network   Code 8    convention
    action              1.32"         1.50"      1.5"
    dialogue            2.32"         2.50"      2.5"
    parenthetical       2.72"         2.90"      -
    character cue       3.32"         3.50"      3.7"

The relative structure is identical, but the absolute offsets differ by 0.18"
between two files, and neither matches the textbook cue position. Hardcoding any
one of those columns misclassifies every cue in the other document. So the
parser builds a histogram of left-edge positions, finds the document's own
columns, and assigns roles by their order and spacing.

Confidence is reported rather than assumed: tier 2 output is a reconstruction,
and Layer 3 should know how much to trust it.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bluepages.events import EventKind, EventStream, NullStream
from bluepages.model.script import (
    Element,
    ElementType,
    Scene,
    SceneNumber,
    Screenplay,
    SourceTier,
    normalise_cue,
    parse_heading,
)

# A slugline: INT./EXT., optionally preceded by a scene number.
_SLUG_RE = re.compile(
    r"^\s*(?:[A-Z]?\d+[A-Z]?[.\s]+)?(INT|EXT|I/E|INT\.?/EXT|EXT\.?/INT)[.\s]",
    re.IGNORECASE,
)

# A trailing or leading scene number on a slugline line, e.g. "34" or "34A".
_EDGE_NUM_RE = re.compile(r"^\s*([A-Z]?\d+[A-Z]?)\s+|\s+([A-Z]?\d+[A-Z]?)\s*$")

# A cut scene, recorded rather than deleted. Carries a number but no slugline.
_OMITTED_RE = re.compile(r"^\s*(?:[A-Z]?\d+[A-Z]?\s+)?OMITTED(?:\s+[A-Z]?\d+[A-Z]?)?\s*$", re.IGNORECASE)

# Transitions: right-aligned in the page, and always these few phrases.
_TRANSITION_RE = re.compile(
    r"^\s*(FADE (IN|OUT|TO)|CUT TO|SMASH CUT|MATCH CUT|DISSOLVE TO|"
    r"WIPE TO|INTERCUT|BACK TO|END OF|THE END|FADE TO BLACK)",
    re.IGNORECASE,
)

# Page furniture: a bare page number, or "(MORE)" / "(CONT'D)" continuations.
_PAGE_NUM_RE = re.compile(r"^\s*\d+[.\s]*$")
_CONTINUATION_RE = re.compile(r"^\s*\(\s*(MORE|CONT[’']?D|CONTINUED)\s*\)?\s*$", re.IGNORECASE)
_CONTINUED_RE = re.compile(r"^\s*\(?\s*CONTINUED\s*\)?\s*[:.]?\s*$", re.IGNORECASE)


class PdfParseError(Exception):
    """The file is not a readable text PDF."""


@dataclass
class Margins:
    """The left-edge columns this particular document uses, in inches.

    Discovered by histogram rather than assumed, because real screenplays vary
    by nearly a fifth of an inch and the cue column in particular is nowhere
    near its textbook position.
    """

    action: float
    dialogue: float
    character: float
    parenthetical: float
    # How confident the calibration is: the share of body lines that landed on
    # one of the detected columns.
    confidence: float = 0.0
    # Every column found, for diagnostics.
    columns: list[tuple[float, int]] = field(default_factory=list)

    def classify(self, x: float, tolerance: float = 0.18) -> ElementType:
        """Assign an element type to a line by its left edge.

        Nearest column wins, within tolerance. Beyond that the line is action:
        action is the widest and most forgiving element, and misfiling a stray
        line as action is far less damaging than inventing a character.
        """
        candidates = (
            (abs(x - self.character), ElementType.CHARACTER),
            (abs(x - self.parenthetical), ElementType.PARENTHETICAL),
            (abs(x - self.dialogue), ElementType.DIALOGUE),
            (abs(x - self.action), ElementType.ACTION),
        )
        distance, kind = min(candidates)
        return kind if distance <= tolerance else ElementType.ACTION

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": round(self.action, 2),
            "dialogue": round(self.dialogue, 2),
            "parenthetical": round(self.parenthetical, 2),
            "character": round(self.character, 2),
            "confidence": round(self.confidence, 3),
        }


@dataclass
class _Line:
    """One visual line of the page: its text and where it sits."""

    text: str
    x0: float      # left edge, inches
    top: float     # distance from page top, points
    page: int


def parse_pdf(
    path: str | Path,
    stream: EventStream | None = None,
    max_pages: int | None = None,
) -> Screenplay:
    """Parse a text-based screenplay PDF into the internal script model.

    Raises `PdfParseError` when the PDF has no extractable text, which means it
    is scanned and belongs to tier 3 (OCR), not here.
    """
    import pdfplumber

    stream = stream or NullStream()
    path = Path(path)
    if not path.exists():
        raise PdfParseError(f"{path}: file not found")

    stream.emit(EventKind.PARSE_STARTED, f"parsing {path.name}", path=str(path), tier="pdf")

    lines: list[_Line] = []
    try:
        with pdfplumber.open(str(path)) as pdf:
            pages = pdf.pages if max_pages is None else pdf.pages[:max_pages]
            page_count = len(pdf.pages)
            for page_no, page in enumerate(pages):
                lines.extend(_page_lines(page, page_no))
    except PdfParseError:
        raise
    except Exception as exc:  # pragma: no cover - corrupt input
        raise PdfParseError(f"{path.name}: could not read PDF: {exc}") from exc

    if not lines:
        raise PdfParseError(
            f"{path.name}: no extractable text. This is a scanned PDF and needs "
            "tier 3 (OCR), which is Layer 1.4."
        )

    margins = calibrate(lines)
    stream.emit(
        EventKind.INFO,
        "margins calibrated: "
        f"action {margins.action:.2f}\" dialogue {margins.dialogue:.2f}\" "
        f"cue {margins.character:.2f}\" (confidence {margins.confidence:.0%})",
        **margins.to_dict(),
    )
    if margins.confidence < 0.5:
        stream.emit(
            EventKind.PARSE_WARNING,
            f"only {margins.confidence:.0%} of lines sit on a detected column; "
            "this PDF may not be a standard screenplay layout",
            confidence=margins.confidence,
        )

    screenplay = _assemble(lines, margins, stream)
    screenplay.source_path = str(path)
    screenplay.meta.update(
        {
            "pages": page_count,
            "margins": margins.to_dict(),
            "columns": [(round(x, 2), n) for x, n in margins.columns[:8]],
        }
    )

    if screenplay.scenes and not screenplay.has_scene_numbers:
        stream.emit(
            EventKind.PARSE_WARNING,
            f"{len(screenplay.scenes)} scenes but no scene numbers; "
            "cross-draft alignment will fall back to heading matching",
            total=len(screenplay.scenes),
        )

    stream.emit(
        EventKind.PARSE_FINISHED,
        f"{path.name}: {screenplay.scene_count} scenes from {page_count} pages",
        scenes=screenplay.scene_count,
        pages=page_count,
        tier="pdf",
    )
    return screenplay


# ---------------------------------------------------------------------------
# Line extraction
# ---------------------------------------------------------------------------


def _page_lines(page: Any, page_no: int) -> list[_Line]:
    """Group a page's words into visual lines, keeping each line's left edge."""
    try:
        words = page.extract_words()
    except Exception:  # pragma: no cover - a single unreadable page
        return []

    buckets: dict[float, list[dict[str, Any]]] = defaultdict(list)
    for w in words:
        # Round to a tenth of a point: same visual line, minor float drift.
        buckets[round(w["top"], 1)].append(w)

    out: list[_Line] = []
    for top in sorted(buckets):
        row = sorted(buckets[top], key=lambda w: w["x0"])
        text = _clean(" ".join(w["text"] for w in row))
        if not text:
            continue
        out.append(_Line(text=text, x0=row[0]["x0"] / 72.0, top=top, page=page_no))
    return out


# PDF text extraction mangles typographic punctuation when the font's encoding
# is not embedded cleanly. Seen in real files as U+FFFD where an apostrophe
# should be. Restoring these matters: "Mark's" vs "Mark?s" changes what the
# element extractor in Layer 3.3 reads.
# ruff: noqa: RUF001 - the ambiguous characters are the whole point of this map
_MOJIBAKE = {
    "�": "'",
    "’": "'",
    "‘": "'",
    "“": '"',
    "”": '"',
    "–": "-",
    "—": "-",
    "…": "...",
}


def _clean(text: str) -> str:
    for bad, good in _MOJIBAKE.items():
        text = text.replace(bad, good)
    return re.sub(r"\s+", " ", text).strip()


# ---------------------------------------------------------------------------
# Calibration
# ---------------------------------------------------------------------------


def calibrate(lines: list[_Line], bin_size: float = 0.05) -> Margins:
    """Find this document's own left-edge columns.

    Screenplays are typographically rigid: nearly every line starts at one of
    four x positions. Those show up as sharp spikes in a histogram of left
    edges. The columns are then assigned to roles by their order, because the
    *relative* layout is universal even though the absolute offsets are not:

        action < dialogue < parenthetical < character cue

    Falls back to the conventional margins only when a document is too short or
    too irregular to calibrate from.
    """
    # Calibrate on body text only. Three kinds of line are excluded:
    #
    # - far-right furniture (page numbers, CONTINUED)
    # - sluglines and OMITTED markers: in a numbered script these begin with the
    #   scene number printed at the 1.0" number column, which otherwise reads as
    #   a spurious leftmost column and drags every role one position left.
    #   Measured on a numbered fixture: 133 lines at 1.00" outvoted the 125 real
    #   action lines at 1.50" and mis-assigned all four margins.
    # - transitions, which are right-aligned and belong to no body column
    #
    # None of these need coordinates to be recognised, so removing them costs
    # nothing and makes the histogram show only the four columns that matter.
    body = [
        ln
        for ln in lines
        if ln.x0 < 6.0
        and not _is_furniture(ln.text)
        and not _SLUG_RE.match(ln.text)
        and not _OMITTED_RE.match(ln.text)
        and not _TRANSITION_RE.match(ln.text)
    ]
    if len(body) < 20:
        return Margins(action=1.5, dialogue=2.5, character=3.7, parenthetical=2.9, confidence=0.0)

    hist: Counter[float] = Counter()
    for ln in body:
        hist[round(ln.x0 / bin_size) * bin_size] += 1

    # A column is a position used by at least 2% of body lines. That threshold
    # keeps the four real columns and discards one-off indents.
    threshold = max(3, len(body) * 0.02)
    columns = sorted(
        ((x, n) for x, n in hist.items() if n >= threshold),
        key=lambda item: item[0],
    )
    if len(columns) < 2:
        return Margins(action=1.5, dialogue=2.5, character=3.7, parenthetical=2.9, confidence=0.0)

    positions = [x for x, _ in columns]

    # Action is the leftmost real column, and it is always the widest-used of
    # the left-hand ones. Dialogue is the next column right of it.
    action = positions[0]
    dialogue = next((x for x in positions if x > action + 0.4), action + 1.0)
    # The character cue is the rightmost column that is still in the body area;
    # parentheticals sit between dialogue and the cue.
    character = next(
        (x for x in reversed(positions) if x > dialogue + 0.5 and x < 5.0),
        dialogue + 1.0,
    )
    parenthetical = next(
        (x for x in positions if dialogue + 0.2 < x < character - 0.2),
        (dialogue + character) / 2,
    )

    detected = Margins(
        action=action,
        dialogue=dialogue,
        character=character,
        parenthetical=parenthetical,
        columns=sorted(((x, n) for x, n in columns), key=lambda i: -i[1]),
    )

    # Confidence: what share of body lines land on one of the four columns.
    on_column = sum(
        1
        for ln in body
        if min(
            abs(ln.x0 - action),
            abs(ln.x0 - dialogue),
            abs(ln.x0 - parenthetical),
            abs(ln.x0 - character),
        )
        <= 0.15
    )
    detected.confidence = on_column / len(body)
    return detected


def _is_furniture(text: str) -> bool:
    """Page numbers, (MORE), (CONTINUED): not screenplay content."""
    return bool(
        _PAGE_NUM_RE.match(text)
        or _CONTINUATION_RE.match(text)
        or _CONTINUED_RE.match(text)
    )


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


def _assemble(lines: list[_Line], margins: Margins, stream: EventStream) -> Screenplay:
    """Turn classified lines into scenes.

    Lines are merged into elements: consecutive lines of the same type inside
    one block are one paragraph, because a PDF line break is a typesetting
    artefact, not an authorial one.
    """
    scenes: list[Scene] = []
    current: Scene | None = None
    preamble: list[Element] = []

    pending_type: ElementType | None = None
    pending_lines: list[str] = []
    last_speaker: str | None = None

    def flush() -> None:
        nonlocal pending_type, pending_lines, last_speaker
        if pending_type is None or not pending_lines:
            pending_type, pending_lines = None, []
            return

        text = " ".join(pending_lines).strip()
        if not text:
            pending_type, pending_lines = None, []
            return

        kind = pending_type
        speaker: str | None = None
        if kind is ElementType.CHARACTER:
            last_speaker = normalise_cue(text) or None
        elif kind in (ElementType.DIALOGUE, ElementType.PARENTHETICAL):
            speaker = last_speaker
        elif kind is ElementType.ACTION:
            last_speaker = None

        element = Element(type=kind, text=text, speaker=speaker)
        if current is None:
            preamble.append(element)
        else:
            current.elements.append(element)
        pending_type, pending_lines = None, []

    for line in lines:
        text = line.text

        if _is_furniture(text):
            continue

        # A slugline starts a scene, wherever it sits horizontally: INT./EXT. is
        # an unambiguous semantic signal and outranks the coordinate guess.
        # An OMITTED line is also a scene: it is how a cut scene is recorded,
        # and dropping it would lose the very fact the diff needs.
        if _SLUG_RE.match(text) or _OMITTED_RE.match(text):
            flush()
            if current is not None:
                scenes.append(current)
            current = _new_scene(text, len(scenes))
            if _OMITTED_RE.match(current.heading):
                current.omitted = True
            if preamble and not scenes:
                current.elements.extend(preamble)
                preamble = []
            last_speaker = None
            stream.emit(
                EventKind.SCENE_PARSED,
                f"scene {current.number or current.index}: {current.heading[:60]}",
                scene_number=str(current.number) if current.number else None,
                heading=current.heading,
                index=current.index,
                page=line.page,
            )
            continue

        kind = margins.classify(line.x0)

        # Transitions are phrase-identifiable. Position does not decide them:
        # "CUT TO:" is right-aligned but "FADE IN:" sits at the action column,
        # so requiring a right-hand position would miss every FADE IN. The
        # phrase list is narrow enough to be safe on its own, and the trailing
        # colon or short length keeps it from eating a line of action prose.
        if _TRANSITION_RE.match(text) and _looks_like_transition(text):
            flush()
            pending_type, pending_lines = ElementType.TRANSITION, [text]
            flush()
            continue

        # A character cue is a short, upper-case line at the cue column. The
        # case test matters: a long line at the cue column is wrapped dialogue
        # in a document whose columns are close together.
        if kind is ElementType.CHARACTER and not _looks_like_cue(text):
            kind = ElementType.DIALOGUE

        if kind is not pending_type:
            flush()
            pending_type = kind
        pending_lines.append(text)

    flush()
    if current is not None:
        scenes.append(current)

    return Screenplay(scenes=scenes, source_tier=SourceTier.PDF)


# A cue is a name, so it holds letters. These are lines that sit at or near the
# cue column but are something else: a clock time, a date, a bare number.
_NOT_A_CUE_RE = re.compile(
    r"^\s*(?:\d{1,2}:\d{2}\s*(?:[AP]\.?M\.?)?|\d+|[IVXLC]+\.?)\s*$",
    re.IGNORECASE,
)


def _looks_like_transition(text: str) -> bool:
    """Whether a transition-phrase line is really a transition.

    Guards against an action line that merely opens with one of the phrases,
    e.g. "BACK TO the window, where the light has changed." A real transition
    is short and typically ends in a colon or a full stop.
    """
    return len(text) <= 32


def _looks_like_cue(text: str) -> bool:
    """Whether a line at the cue column is really a character cue.

    Cues are short and upper-case. `MARK` and `ERICA (V.O.)` are cues; a full
    sentence at the same x is wrapped dialogue.
    """
    if len(text) > 40:
        return False
    if _NOT_A_CUE_RE.match(text):
        return False
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return False
    upper_share = sum(1 for c in letters if c.isupper()) / len(letters)
    return upper_share > 0.85


def _new_scene(text: str, index: int) -> Scene:
    """Build a Scene from a slugline line, pulling any scene number off it."""
    number: SceneNumber | None = None

    # A numbered script prints the number at both edges of the slugline.
    match = _EDGE_NUM_RE.search(text)
    heading = text
    if match:
        raw = match.group(1) or match.group(2)
        candidate = SceneNumber.parse(raw)
        # Only accept it if it parsed to an actual number, so a slugline
        # beginning with a word is not mistaken for a numbered scene.
        if candidate is not None and candidate.number is not None:
            number = candidate
            heading = _EDGE_NUM_RE.sub(" ", text).strip()

    heading = re.sub(r"\s+", " ", heading).strip()
    int_ext, location, time_of_day = parse_heading(heading)
    return Scene(
        number=number,
        heading=heading,
        int_ext=int_ext,
        location=location,
        time_of_day=time_of_day,
        index=index,
    )


def page_count(path: str | Path) -> int:
    """Pages in a PDF, without parsing it. Used to size a run before starting."""
    import pdfplumber

    with pdfplumber.open(str(path)) as pdf:
        return len(pdf.pages)


def median_confidence(screenplay: Screenplay) -> float:
    """The calibration confidence recorded on a parsed screenplay."""
    margins = screenplay.meta.get("margins") or {}
    value = margins.get("confidence", 0.0)
    return float(value) if isinstance(value, int | float) else 0.0


__all__ = ["Margins", "PdfParseError", "calibrate", "page_count", "parse_pdf"]
