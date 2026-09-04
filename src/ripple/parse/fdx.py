"""Tier 1: the Final Draft `.fdx` parser (Layer 1.1).

`.fdx` is the primary path, not a shortcut. Final Draft is the industry standard
and the XML hands us directly what every other parser tier has to guess at:

- `<Paragraph Type="...">`  typed elements, so classification is a lookup
- `@Number` on scene headings  the stable cross-draft alignment anchor
- `RevisionID` on `<Text>` runs  sub-paragraph revision tracking, validated
  against the `<Revisions>` section that names all nine coloured sets
- `SceneProperties` (`@Title`, `@Page`, `Length`) and `ScriptNote`  metadata

Two structural facts drive this implementation:

1. `<Paragraph>` elements also live outside `<Content>`  in `Watermarking`,
   `HeaderAndFooter` and `TitlePage`. A `.//Paragraph` XPath ingests page
   furniture as screenplay action. Everything here is scoped to
   `./Content/Paragraph`. Verified against real files: one sample has 13
   paragraphs anywhere and 8 in Content.
2. A paragraph's text is split across several `<Text>` runs, each of which may
   carry its own `RevisionID`. The text is the concatenation of the runs; the
   revision state is the union of them.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from lxml import etree

from ripple.events import EventKind, EventStream, NullStream
from ripple.model.script import (
    Element,
    ElementType,
    InteriorExterior,
    RevisionMark,
    Scene,
    SceneNumber,
    Screenplay,
    ScriptNote,
    SourceTier,
    normalise_cue,
    parse_heading,
)

# Final Draft's `Type` attribute -> our ElementType. Exact strings from the
# format; the fallback keeps unknown future types visible rather than dropped.
_TYPE_MAP: dict[str, ElementType] = {
    "Scene Heading": ElementType.SCENE_HEADING,
    "Action": ElementType.ACTION,
    "Character": ElementType.CHARACTER,
    "Dialogue": ElementType.DIALOGUE,
    "Parenthetical": ElementType.PARENTHETICAL,
    "Transition": ElementType.TRANSITION,
    "Shot": ElementType.SHOT,
    "Cast List": ElementType.CAST_LIST,
    "General": ElementType.GENERAL,
    "New Act": ElementType.GENERAL,
    "End of Act": ElementType.GENERAL,
    "Act Break": ElementType.GENERAL,
}

# A scene is omitted, not deleted. Final Draft users write this several ways.
_OMITTED_TOKENS = ("OMITTED", "OMIT", "DELETED", "SCENE OMITTED")


class FdxParseError(Exception):
    """The file is not usable Final Draft XML."""


def parse_fdx(
    path: str | Path,
    stream: EventStream | None = None,
) -> Screenplay:
    """Parse a `.fdx` file into the internal script model.

    Raises `FdxParseError` when the file is not Final Draft XML at all. Anything
    recoverable  a missing scene number, an unknown paragraph type, a heading
    that will not parse  is emitted as a `PARSE_WARNING` event and the parse
    continues. A feature script with three odd sluglines should still parse.
    """
    stream = stream or NullStream()
    path = Path(path)
    stream.emit(EventKind.PARSE_STARTED, f"parsing {path.name}", path=str(path), tier="fdx")

    root = _load(path)
    revisions = _parse_revision_sets(root)
    title = _parse_title(root)

    content = root.find("Content")
    if content is None:
        raise FdxParseError(f"{path.name}: no <Content> element; not a Final Draft script")

    scenes: list[Scene] = []
    current: Scene | None = None
    # Front matter before the first slugline (FADE IN:, act headers) belongs to
    # no scene. Held here so it is preserved rather than silently dropped.
    preamble: list[Element] = []
    last_speaker: str | None = None
    unnumbered = 0

    for para in content.findall("Paragraph"):
        raw_type = para.get("Type") or "General"
        el_type = _TYPE_MAP.get(raw_type, ElementType.UNKNOWN)
        if el_type is ElementType.UNKNOWN:
            stream.emit(
                EventKind.PARSE_WARNING,
                f"unknown paragraph type {raw_type!r}",
                paragraph_type=raw_type,
            )

        text, marks = _paragraph_text_and_revisions(para, revisions)
        notes = _parse_script_notes(para)

        if el_type is ElementType.SCENE_HEADING:
            if current is not None:
                scenes.append(current)
            current = _start_scene(
                para=para,
                text=text,
                marks=marks,
                notes=notes,
                index=len(scenes),
                stream=stream,
            )
            if current.number is None:
                unnumbered += 1
            # Front matter attaches to the first scene so nothing is lost.
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
            )
            continue

        # Track the speaking character so dialogue and parentheticals carry it.
        if el_type is ElementType.CHARACTER:
            last_speaker = normalise_cue(text) or None
        speaker = last_speaker if el_type in (
            ElementType.DIALOGUE,
            ElementType.PARENTHETICAL,
        ) else None
        if el_type in (ElementType.ACTION, ElementType.TRANSITION, ElementType.SHOT):
            last_speaker = None

        # A blank General paragraph is spacing, not content.
        if not text.strip() and el_type in (ElementType.GENERAL, ElementType.UNKNOWN):
            continue

        element = Element(
            type=el_type,
            text=text,
            revised=bool(marks),
            revision_marks=marks,
            notes=notes,
            speaker=speaker,
        )
        if current is None:
            preamble.append(element)
        else:
            current.elements.append(element)
            if marks:
                current.revised = True

    if current is not None:
        scenes.append(current)

    screenplay = Screenplay(
        title=title,
        scenes=scenes,
        source_tier=SourceTier.FDX,
        source_path=str(path),
        revisions=revisions,
        meta={
            "unnumbered_scenes": unnumbered,
            "preamble_elements": len(preamble),
            "fdx_version": root.get("Version"),
            "document_type": root.get("DocumentType"),
        },
    )

    # Alignment (Layer 3.1) anchors on scene numbers. If they are absent this is
    # a pre-production draft and alignment must fall back to headings; that is a
    # different quality of result, so it is surfaced loudly rather than assumed.
    if scenes and not screenplay.has_scene_numbers:
        stream.emit(
            EventKind.PARSE_WARNING,
            f"{unnumbered}/{len(scenes)} scenes have no @Number; "
            "cross-draft alignment will fall back to heading matching",
            unnumbered=unnumbered,
            total=len(scenes),
        )

    stream.emit(
        EventKind.PARSE_FINISHED,
        f"{path.name}: {len(scenes)} scenes",
        scenes=len(scenes),
        numbered=len(screenplay.numbered_scenes),
        omitted=len(screenplay.omitted_scenes),
        tier="fdx",
    )
    return screenplay


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _load(path: Path) -> etree._Element:
    """Parse the XML defensively.

    `resolve_entities=False` matters: these files arrive from outside the
    production and an XML parser that resolves external entities is an SSRF and
    file-disclosure hole. `recover=True` because real Final Draft output
    occasionally carries stray control characters that are not worth failing on.
    """
    if not path.exists():
        raise FdxParseError(f"{path}: file not found")

    parser = etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        huge_tree=False,
        recover=True,
    )
    try:
        tree = etree.parse(str(path), parser)
    except etree.XMLSyntaxError as exc:  # pragma: no cover - malformed input
        raise FdxParseError(f"{path.name}: malformed XML: {exc}") from exc

    root = tree.getroot()
    if root is None:
        raise FdxParseError(f"{path.name}: empty document")
    if root.tag != "FinalDraft":
        raise FdxParseError(
            f"{path.name}: root element is <{root.tag}>, expected <FinalDraft>"
        )
    return root


def _inner_text(element: etree._Element) -> str:
    """All descendant text of an element, as a string.

    `itertext()` is typed as yielding `str | bytes` because comment and
    processing-instruction nodes can carry bytes. Screenplay text is never
    bytes, but a stray comment inside a paragraph would otherwise crash the
    join, so non-string nodes are skipped rather than coerced.
    """
    return "".join(t for t in element.itertext() if isinstance(t, str))


def _parse_revision_sets(root: etree._Element) -> dict[int, RevisionMark]:
    """Read the nine coloured revision sets the document declares.

    Blue, Pink, Yellow, Green, Goldenrod, Buff, Salmon, Cherry, Tan  the paper
    protocol this project automates. Mapping IDs to names is what lets a report
    say "changed in the Pink revision" rather than "RevisionID 2".
    """
    out: dict[int, RevisionMark] = {}
    section = root.find("Revisions")
    if section is None:
        return out
    for rev in section.findall("Revision"):
        raw_id = rev.get("ID")
        if raw_id is None:
            continue
        try:
            rid = int(raw_id)
        except ValueError:
            continue
        out[rid] = RevisionMark(
            revision_id=rid,
            name=rev.get("Name"),
            colour=rev.get("Color"),
        )
    return out


def _parse_title(root: etree._Element) -> str | None:
    """Pull the script title from the title page, when there is one.

    The title page is free-form: it holds whatever the writer typed. The first
    non-empty line is the title by overwhelming convention.
    """
    tp = root.find("TitlePage")
    if tp is None:
        return None
    for para in tp.findall(".//Paragraph"):
        text = _inner_text(para).strip()
        # Skip page-number furniture and separator punctuation.
        if text and text not in {".", "-"} and not text.isdigit():
            return text
    return None


def _paragraph_text_and_revisions(
    para: etree._Element,
    revisions: dict[int, RevisionMark],
) -> tuple[str, list[RevisionMark]]:
    """Join a paragraph's `<Text>` runs and collect their revision marks.

    A single paragraph is often several runs, because Final Draft splits on any
    formatting or revision boundary. The paragraph's text is the concatenation;
    its revision state is the union of the runs' `RevisionID`s.

    `RevisionID="0"` means unrevised and is the default on every run, so only
    non-zero IDs count.
    """
    parts: list[str] = []
    marks: dict[int, RevisionMark] = {}

    for text_el in para.findall("Text"):
        parts.append(text_el.text or "")
        raw = text_el.get("RevisionID")
        if raw in (None, "0", ""):
            continue
        try:
            rid = int(raw)
        except ValueError:
            continue
        if rid == 0:
            continue
        marks[rid] = revisions.get(rid) or RevisionMark(revision_id=rid)

    # Final Draft breaks a line inside a paragraph with <SceneProperties> or
    # other siblings; joining the runs directly preserves the author's spacing.
    text = "".join(parts)
    # Collapse the hard line breaks Final Draft encodes, keep intentional spaces.
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    return text, list(marks.values())


def _parse_script_notes(para: etree._Element) -> list[ScriptNote]:
    """Production notes attached to a paragraph. Metadata, not screenplay text."""
    notes: list[ScriptNote] = []
    for note in para.findall("ScriptNote"):
        text = _inner_text(note).strip()
        if text:
            notes.append(ScriptNote(text=text))
    return notes


def _start_scene(
    para: etree._Element,
    text: str,
    marks: list[RevisionMark],
    notes: list[ScriptNote],
    index: int,
    stream: EventStream,
) -> Scene:
    """Build a Scene from a Scene Heading paragraph.

    The scene number is read from `@Number` on the paragraph, which is where
    Final Draft puts it. Some files instead carry it inside `SceneProperties`,
    and a few write it into the slugline text itself; all three are tried,
    because a missing number degrades alignment for the whole draft.
    """
    sp = para.find("SceneProperties")

    raw_number = para.get("Number")
    if not raw_number and sp is not None:
        raw_number = sp.get("Number")
    number = SceneNumber.parse(raw_number)

    int_ext, location, time_of_day = parse_heading(text)

    heading_upper = text.upper()
    omitted = any(tok in heading_upper for tok in _OMITTED_TOKENS)

    # An omitted scene is a bare "OMITTED" with no slugline. That is correct,
    # not a defect, so it must not raise a malformed-heading warning.
    if int_ext is InteriorExterior.UNKNOWN and text.strip() and not omitted:
        stream.emit(
            EventKind.PARSE_WARNING,
            f"slugline has no INT/EXT: {text[:60]!r}",
            heading=text,
            index=index,
        )

    scene = Scene(
        number=number,
        heading=text,
        int_ext=int_ext,
        location=location,
        time_of_day=time_of_day,
        omitted=omitted,
        index=index,
        revised=bool(marks),
        notes=notes,
    )
    if sp is not None:
        scene.title = sp.get("Title") or None
        scene.page = sp.get("Page") or None
        summary = sp.find("Summary")
        if summary is not None:
            scene.summary = _inner_text(summary).strip() or None
    return scene


def parse_fdx_string(xml: str, stream: EventStream | None = None) -> Screenplay:
    """Parse `.fdx` content already in memory. Used by tests and by the S3 path."""
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".fdx", encoding="utf-8", delete=False) as fh:
        fh.write(xml)
        tmp = Path(fh.name)
    try:
        return parse_fdx(tmp, stream=stream)
    finally:
        tmp.unlink(missing_ok=True)


def summarise(screenplay: Screenplay) -> dict[str, Any]:
    """Convenience wrapper so the CLI and tests report the same shape."""
    return screenplay.stats()
