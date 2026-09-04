"""Generate a production-shaped `.fdx` fixture.

The public `.fdx` samples are toys: no scene numbers, no revision marks, no
omitted scenes. Those are precisely the three features Layer 1.1 exists to read,
so the parser cannot be validated against them.

This writes a file with the structure a real production file has  numbered
scenes including `34A` inserts, an `OMITTED` scene, non-zero `RevisionID` runs
against the nine declared colour sets, `SceneProperties`, `ScriptNote`, and
paragraphs outside `<Content>` that a careless parser would ingest.

Feature length is a parameter, so the same generator produces the small
iteration fixture and the scale test.
"""

from __future__ import annotations

import argparse
import random
from pathlib import Path
from xml.sax.saxutils import escape

REVISION_SETS = [
    (1, "Blue", "#00000000FFFF"),
    (2, "Pink", "#FFFF0000FFFF"),
    (3, "Yellow", "#A0A0A00000"),
    (4, "Green", "#000080800000"),
    (5, "Goldenrod", "#CCCC7F7F3232"),
    (6, "Buff", "#8E8E6B6B2323"),
    (7, "Salmon", "#A7A742424242"),
    (8, "Cherry", "#D5D523236B6B"),
    (9, "Tan", "#DBDB93937070"),
]

LOCATIONS = [
    ("INT", "FARMHOUSE KITCHEN", "DAY"),
    ("EXT", "GRAVEL DRIVEWAY", "DAY"),
    ("INT", "HARLAN'S STUDY", "NIGHT"),
    ("EXT", "WHEAT FIELD", "DUSK"),
    ("INT", "COUNTY SHERIFF'S OFFICE", "DAY"),
    ("INT", "PICKUP TRUCK - MOVING", "NIGHT"),
    ("EXT", "GRAIN SILO", "DAWN"),
    ("INT", "DINER", "MORNING"),
    ("INT", "HOSPITAL CORRIDOR", "NIGHT"),
    ("EXT", "CEMETERY", "AFTERNOON"),
]

CHARACTERS = ["HARLAN", "MAE", "DEPUTY COLE", "JANITOR", "DOC REYES"]

ACTION_LINES = [
    "The screen door bangs shut behind her.",
    "He turns the letter opener over in his hands, weighing it.",
    "Dust hangs in the light from the window.",
    "She sets the envelope on the table and slides it across.",
    "The truck idles at the end of the drive, headlights off.",
    "He pockets the brass key without looking at it.",
    "Rain starts, sudden and hard, against the tin roof.",
]

DIALOGUE_LINES = [
    "You should have called first.",
    "It was never about the money.",
    "Sign it and we're done.",
    "I know what you did out there.",
    "Nobody's coming, Harlan.",
]


def _text_run(content: str, revision_id: int = 0) -> str:
    """One `<Text>` run. Non-zero RevisionID marks it as revised."""
    return (
        f'<Text AdornmentStyle="0" Background="#FFFFFFFFFFFF" '
        f'Color="#000000000000" Font="Courier Final Draft" '
        f'RevisionID="{revision_id}" Size="12" Style="">{escape(content)}</Text>'
    )


def _scene(
    number: str,
    int_ext: str,
    location: str,
    tod: str,
    page: int,
    rng: random.Random,
    revised_set: int = 0,
    omitted: bool = False,
    note: str | None = None,
) -> str:
    """One scene: a numbered heading paragraph plus its body paragraphs."""
    parts: list[str] = []

    if omitted:
        # The convention: the scene stays, its content does not.
        parts.append(
            f'    <Paragraph Number="{number}" Type="Scene Heading">\n'
            f'      <SceneProperties Length="0/8" Page="{page}" Title=""/>\n'
            f"      {_text_run('OMITTED')}\n"
            f"    </Paragraph>"
        )
        return "\n".join(parts)

    heading = f"{int_ext}. {location} - {tod}"
    note_xml = ""
    if note:
        note_xml = f"\n      <ScriptNote><Paragraph>{_text_run(note)}</Paragraph></ScriptNote>"

    parts.append(
        f'    <Paragraph Number="{number}" Type="Scene Heading">\n'
        f'      <SceneProperties Length="{rng.randint(1, 9)}/8" Page="{page}" '
        f'Title="">\n'
        f"        <Summary>{escape(location.title())} beat.</Summary>\n"
        f"      </SceneProperties>\n"
        f"      {_text_run(heading, revised_set)}{note_xml}\n"
        f"    </Paragraph>"
    )

    # Action. Sometimes split across two runs, one revised: that is the
    # sub-paragraph revision tracking the parser has to union correctly.
    action = rng.choice(ACTION_LINES)
    if revised_set and rng.random() < 0.5:
        head, _, tail = action.partition(" ")
        parts.append(
            '    <Paragraph Type="Action">\n'
            f"      {_text_run(head + ' ')}\n"
            f"      {_text_run(tail, revised_set)}\n"
            "    </Paragraph>"
        )
    else:
        parts.append(
            '    <Paragraph Type="Action">\n'
            f"      {_text_run(action, revised_set)}\n"
            "    </Paragraph>"
        )

    speaker = rng.choice(CHARACTERS)
    cue = speaker if rng.random() < 0.8 else f"{speaker} (CONT'D)"
    parts.append(f'    <Paragraph Type="Character">\n      {_text_run(cue)}\n    </Paragraph>')
    if rng.random() < 0.3:
        parts.append(
            '    <Paragraph Type="Parenthetical">\n'
            f"      {_text_run('(quietly)')}\n"
            "    </Paragraph>"
        )
    parts.append(
        '    <Paragraph Type="Dialogue">\n'
        f"      {_text_run(rng.choice(DIALOGUE_LINES), revised_set)}\n"
        "    </Paragraph>"
    )
    return "\n".join(parts)


def build(scene_count: int, seed: int = 7, revised_ratio: float = 0.15) -> str:
    """Build the whole document, including the non-Content paragraph traps."""
    rng = random.Random(seed)

    revisions = "\n".join(
        f'    <Revision Color="{colour}" FullRevision="No" ID="{rid}" Mark="*" '
        f'Name="{name}" PageColor="#FFFFFFFFFFFF" Style=""/>'
        for rid, name, colour in REVISION_SETS
    )

    scenes: list[str] = []
    n = 1
    page = 1
    for i in range(scene_count):
        int_ext, location, tod = LOCATIONS[i % len(LOCATIONS)]

        # An inserted scene every ninth: 34A between 34 and 35.
        if i > 0 and i % 9 == 0:
            scenes.append(
                _scene(
                    f"{n - 1}A", int_ext, location, tod, page, rng,
                    revised_set=2,  # inserts are by definition new: Pink
                )
            )

        omitted = i > 0 and i % 13 == 0
        revised = 0
        if not omitted and rng.random() < revised_ratio:
            revised = rng.choice([1, 2, 3])

        note = "Confirm practical lamp is available." if i == 4 else None
        scenes.append(
            _scene(str(n), int_ext, location, tod, page, rng, revised, omitted, note)
        )
        n += 1
        if i % 2:
            page += 1

    body = "\n".join(scenes)

    # Paragraphs outside <Content>: page furniture a `.//Paragraph` XPath eats.
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="no" ?>
<FinalDraft DocumentType="Script" Template="No" Version="5">

  <Content>
    <Paragraph Type="Action">
      {_text_run("FADE IN:")}
    </Paragraph>
{body}
    <Paragraph Type="Transition">
      {_text_run("FADE OUT.")}
    </Paragraph>
  </Content>

  <TitlePage>
    <Content>
      <Paragraph Alignment="Center" Type="General">
        {_text_run("THE LONG ACRE")}
      </Paragraph>
      <Paragraph Alignment="Center" Type="General">
        {_text_run("Written by A. Writer")}
      </Paragraph>
    </Content>
  </TitlePage>

  <HeaderAndFooter FooterVisible="No" HeaderVisible="Yes">
    <Header>
      <Paragraph Alignment="Right">
        <DynamicLabel Type="Page #"/>
        {_text_run("THIS IS A PAGE HEADER, NOT SCREENPLAY ACTION")}
      </Paragraph>
    </Header>
  </HeaderAndFooter>

  <Watermarking Opacity="70" Position="Diagonal Descending">
    <DynamicContent>
      <Paragraph Alignment="Left">
        {_text_run("CONFIDENTIAL WATERMARK, NOT SCREENPLAY ACTION")}
      </Paragraph>
    </DynamicContent>
  </Watermarking>

  <Revisions ActiveSet="2" RevisionMode="Yes" RevisionsShown="All">
{revisions}
  </Revisions>

  <SceneNumberOptions NumberScheme="1A" ShowNumbersOnLeft="Yes" ShowNumbersOnRight="Yes"/>
</FinalDraft>
"""


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--scenes", type=int, default=120)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--revised-ratio", type=float, default=0.15)
    ap.add_argument("-o", "--out", type=Path, required=True)
    args = ap.parse_args()

    xml = build(args.scenes, args.seed, args.revised_ratio)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(xml, encoding="utf-8")
    print(f"wrote {args.out} ({len(xml):,} bytes, {args.scenes} base scenes)")


if __name__ == "__main__":
    main()
