"""Author the Layer 2 revision pairs and their labelled answer key.

No public corpus of the same script at draft N and N+1 exists; productions do
not release those. So we write both drafts ourselves, applying the real
conventions, and record exactly what changed and who owns it.

Authoring rather than sourcing is what makes Layer 3 testable: a leaked real
draft would give us two files and no ground truth. Here every change is
deliberate, so the answer key is exact rather than an opinion.

The script content is hand-written and coherent, not generated. That matters:
the semantic layer has to decide whether the letter opener in scene 7 is the
same object that left scene 3, and it can only do that if the prop has a real
history in the text.

Each change embeds one of the judgment calls the PRD names as the product:

  1. prop relocated       is it the same object moved, or a cut and a new buy?
  2. character renamed    is CUSTODIAN a renamed JANITOR, or a new role?
  3. action rewritten     does the rewrite have physical consequences, or is it
                          only prose? (Here: none for props, but a new camera
                          setup, so it is an AD concern.)
  4. DAY -> NIGHT         not a prop change at all: schedule, and a possible
                          location re-quote.
  5. scene omitted        marked OMITTED, never deleted.
  6. scene inserted       34A-style, between two existing scenes.
  7. prop added           a genuinely new buy for props.
  8. branded object       a rights-clearance liability introduced by the revision.

Usage:
    python scripts/make_revision_pair.py --out tests/fixtures
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from xml.sax.saxutils import escape

REVISION_SETS = [
    (1, "Blue", "#00000000FFFF"),
    (2, "Pink", "#FFFF0000FFFF"),
    (3, "Yellow", "#A0A0A0A00000"),
    (4, "Green", "#000080800000"),
    (5, "Goldenrod", "#CCCC7F7F3232"),
    (6, "Buff", "#8E8E6B6B2323"),
    (7, "Salmon", "#A7A742424242"),
    (8, "Cherry", "#D5D523236B6B"),
    (9, "Tan", "#DBDB93937070"),
]

# ---------------------------------------------------------------------------
# Draft 1: the original. Eight scenes, coherent, with props that have a history.
# ---------------------------------------------------------------------------

DRAFT_1: list[dict] = [
    {
        "number": "1",
        "heading": "EXT. FARMHOUSE - DAWN",
        "page": 1,
        "body": [
            ("Action", "A pickup truck sits in the yard, tyres bald, one headlight cracked."),
            ("Action", "HARLAN VANCE, 60s, weathered, crosses the yard with a tin mug."),
            ("Character", "HARLAN"),
            ("Dialogue", "Thirty years and it still won't start in the cold."),
        ],
    },
    {
        "number": "2",
        "heading": "INT. FARMHOUSE KITCHEN - DAY",
        "page": 1,
        "body": [
            ("Action", "MAE VANCE, 30s, sorts a stack of unopened mail at the table."),
            ("Action", "She wears her father's canvas work jacket, sleeves rolled twice."),
            ("Character", "MAE"),
            ("Dialogue", "You've been letting these pile up since March."),
            ("Character", "HARLAN"),
            ("Parenthetical", "(not looking up)"),
            ("Dialogue", "Nothing in there I want to read."),
        ],
    },
    {
        "number": "3",
        "heading": "INT. HARLAN'S STUDY - DAY",
        "page": 2,
        "body": [
            ("Action", "A brass letter opener lies on the desk beside a ledger."),
            ("Action", "Harlan picks it up, turns it over, sets it down again."),
            ("Character", "HARLAN"),
            ("Dialogue", "Your mother gave me that. Nineteen eighty-four."),
        ],
    },
    {
        "number": "4",
        "heading": "EXT. WHEAT FIELD - DAY",
        "page": 2,
        "body": [
            ("Action", "Mae walks the fence line. The wheat is thin and going to seed."),
            ("Action", "A JANITOR from the county office waits by the gate, clipboard in hand."),
            ("Character", "JANITOR"),
            ("Dialogue", "They sent me to post the notice. I'm sorry about it."),
        ],
    },
    {
        "number": "5",
        "heading": "INT. COUNTY SHERIFF'S OFFICE - DAY",
        "page": 3,
        "body": [
            ("Action", "DEPUTY COLE, 40s, slides a manila envelope across the counter."),
            ("Action", "Mae takes it without opening it."),
            ("Character", "DEPUTY COLE"),
            ("Dialogue", "Sixty days. After that it's out of my hands."),
        ],
    },
    {
        "number": "6",
        "heading": "INT. PICKUP TRUCK - MOVING - DUSK",
        "page": 3,
        "body": [
            ("Action", "Mae drives. The envelope sits unopened on the bench seat."),
            ("Character", "MAE"),
            ("Dialogue", "He's going to sign it. He just doesn't know it yet."),
        ],
    },
    {
        "number": "7",
        "heading": "INT. FARMHOUSE KITCHEN - NIGHT",
        "page": 4,
        "body": [
            ("Action", "Harlan hands her the envelope across the table."),
            ("Character", "HARLAN"),
            ("Dialogue", "Read it to me. My eyes are going."),
        ],
    },
    {
        "number": "8",
        "heading": "EXT. FARMHOUSE - NIGHT",
        "page": 4,
        "body": [
            ("Action", "The kitchen light goes out. The truck stays where it is."),
            ("Transition", "FADE OUT."),
        ],
    },
]


# ---------------------------------------------------------------------------
# Draft 2: the revision. Every difference from draft 1 is deliberate and
# recorded in the answer key below.
# ---------------------------------------------------------------------------

DRAFT_2: list[dict] = [
    {
        "number": "1",
        "heading": "EXT. FARMHOUSE - DAWN",
        "page": 1,
        "body": [
            ("Action", "A pickup truck sits in the yard, tyres bald, one headlight cracked."),
            ("Action", "HARLAN VANCE, 60s, weathered, crosses the yard with a tin mug."),
            ("Character", "HARLAN"),
            ("Dialogue", "Thirty years and it still won't start in the cold."),
        ],
    },
    {
        # CHANGE 4: DAY -> NIGHT. Not a prop change. Schedule, and possibly a
        # location re-quote for a night shoot.
        "number": "2",
        "heading": "INT. FARMHOUSE KITCHEN - NIGHT",
        "page": 1,
        "revised": 2,
        "body": [
            ("Action", "MAE VANCE, 30s, sorts a stack of unopened mail at the table."),
            ("Action", "She wears her father's canvas work jacket, sleeves rolled twice."),
            ("Character", "MAE"),
            ("Dialogue", "You've been letting these pile up since March."),
            ("Character", "HARLAN"),
            ("Parenthetical", "(not looking up)"),
            ("Dialogue", "Nothing in there I want to read."),
        ],
    },
    {
        # CHANGE 1 (origin): the letter opener leaves this scene.
        "number": "3",
        "heading": "INT. HARLAN'S STUDY - DAY",
        "page": 2,
        "revised": 2,
        "body": [
            ("Action", "A ledger lies open on the desk, columns filled in a careful hand."),
            ("Action", "Harlan closes it without reading."),
            ("Character", "HARLAN"),
            ("Dialogue", "Your mother kept these. Nineteen eighty-four onward."),
        ],
    },
    {
        # CHANGE 2: JANITOR -> CUSTODIAN. Same function, same dialogue, same
        # scene: a rename, not a new role. Casting's answer is worth money.
        "number": "4",
        "heading": "EXT. WHEAT FIELD - DAY",
        "page": 2,
        "revised": 2,
        "body": [
            ("Action", "Mae walks the fence line. The wheat is thin and going to seed."),
            ("Action", "A CUSTODIAN from the county office waits by the gate, clipboard in hand."),
            ("Character", "CUSTODIAN"),
            ("Dialogue", "They sent me to post the notice. I'm sorry about it."),
        ],
    },
    {
        # CHANGE 5: omitted, not deleted. The number survives.
        "number": "5",
        "heading": "OMITTED",
        "page": 3,
        "revised": 2,
        "omitted": True,
        "body": [],
    },
    {
        # CHANGE 6: an inserted scene, 5A, between 5 and 6.
        # CHANGE 8: a branded object, a rights-clearance liability.
        "number": "5A",
        "heading": "EXT. COUNTY OFFICE - PARKING LOT - DAY",
        "page": 3,
        "revised": 2,
        "body": [
            ("Action", "Deputy Cole waits by a county Ford Bronco, engine running."),
            ("Action", "He hands Mae the manila envelope through the window."),
            ("Character", "DEPUTY COLE"),
            ("Dialogue", "Sixty days. After that it's out of my hands."),
        ],
    },
    {
        "number": "6",
        "heading": "INT. PICKUP TRUCK - MOVING - DUSK",
        "page": 3,
        "body": [
            ("Action", "Mae drives. The envelope sits unopened on the bench seat."),
            ("Character", "MAE"),
            ("Dialogue", "He's going to sign it. He just doesn't know it yet."),
        ],
    },
    {
        # CHANGE 1 (destination): the same letter opener, relocated from sc. 3.
        # CHANGE 3: "hands her the envelope" -> "slides the envelope across the
        #           table". No prop consequence; a different camera setup.
        # CHANGE 7: reading glasses, a genuinely new prop.
        "number": "7",
        "heading": "INT. FARMHOUSE KITCHEN - NIGHT",
        "page": 4,
        "revised": 2,
        "body": [
            ("Action", "The brass letter opener sits on the table where he left it."),
            ("Action", "Harlan slides the envelope across the table to her."),
            ("Action", "He puts on a pair of wire-rimmed reading glasses."),
            ("Character", "HARLAN"),
            ("Dialogue", "Read it to me. My eyes are going."),
        ],
    },
    {
        "number": "8",
        "heading": "EXT. FARMHOUSE - NIGHT",
        "page": 4,
        "body": [
            ("Action", "The kitchen light goes out. The truck stays where it is."),
            ("Transition", "FADE OUT."),
        ],
    },
]


# ---------------------------------------------------------------------------
# The answer key. This is what makes Layer 3 measurable.
# ---------------------------------------------------------------------------

ANSWER_KEY: dict = {
    "pair": "small",
    "draft_from": "small-draft-1.fdx",
    "draft_to": "small-draft-2.fdx",
    "note": (
        "Hand-authored ground truth. Every change below is deliberate. "
        "Layer 3 is scored against this; a semantic result with no key to check "
        "against is not verified."
    ),
    "changes": [
        {
            "id": "prop-relocated",
            "kind": "element_relocated",
            "summary": "The brass letter opener moves from scene 3 to scene 7.",
            "from_scene": "3",
            "to_scene": "7",
            "element": "brass letter opener",
            "departments": ["props"],
            "judgment": (
                "Same object relocated, not a cut and a new buy. Props needs the "
                "difference: a relocation is a continuity and set-dressing note, "
                "a new buy is a purchase order. The text supports identity: it is "
                "the same brass letter opener, and scene 7 says it sits 'where he "
                "left it'."
            ),
            "must_not_say": [
                "letter opener cut",
                "new letter opener required",
                "purchase a letter opener",
            ],
        },
        {
            "id": "character-renamed",
            "kind": "character_renamed",
            "summary": "JANITOR is renamed CUSTODIAN.",
            "from_scene": "4",
            "to_scene": "4",
            "from_name": "JANITOR",
            "to_name": "CUSTODIAN",
            "departments": ["cast"],
            "judgment": (
                "A rename, not a new role. Same scene, same function, identical "
                "dialogue. Casting must not be told to fill a new part; the "
                "existing booking carries over under a new name."
            ),
            "must_not_say": ["new role", "cast a custodian", "additional character"],
        },
        {
            "id": "action-rewritten-no-prop-change",
            "kind": "action_rewritten",
            "summary": (
                "'hands her the envelope' becomes 'slides the envelope across the "
                "table'."
            ),
            "from_scene": "7",
            "to_scene": "7",
            "departments": ["ad"],
            "judgment": (
                "No props consequence: the envelope is unchanged and still one "
                "envelope. It is a different physical action and therefore a "
                "different camera setup, which is the AD's concern. This is the "
                "category a structural diff cannot resolve: the text changed, the "
                "kit did not."
            ),
            "must_not_say": ["new prop", "additional envelope", "props affected"],
        },
        {
            "id": "day-to-night",
            "kind": "time_of_day_changed",
            "summary": "Scene 2 flips DAY to NIGHT.",
            "from_scene": "2",
            "to_scene": "2",
            "from_value": "DAY",
            "to_value": "NIGHT",
            "departments": ["schedule", "locations"],
            "judgment": (
                "Not a prop change at all. It is a scheduling change, and a "
                "possible location re-quote because a night shoot at a practical "
                "location is priced differently. Reporting this to props would be "
                "noise."
            ),
            "must_not_say": ["prop", "wardrobe change"],
        },
        {
            "id": "scene-omitted",
            "kind": "scene_omitted",
            "summary": "Scene 5 is marked OMITTED.",
            "from_scene": "5",
            "to_scene": "5",
            "departments": ["schedule", "locations", "cast"],
            "judgment": (
                "The scene number survives, which is why alignment still works. "
                "The county sheriff's office interior is no longer needed, and its "
                "content is largely absorbed by the new scene 5A."
            ),
            "must_not_say": ["scene deleted", "scene 5 missing"],
        },
        {
            "id": "scene-inserted",
            "kind": "scene_inserted",
            "summary": "Scene 5A is inserted between 5 and 6.",
            "from_scene": None,
            "to_scene": "5A",
            "departments": ["locations", "transport", "schedule", "cast"],
            "judgment": (
                "A new exterior location (county office parking lot) and a picture "
                "vehicle. Transport is involved because the Bronco is a working "
                "vehicle in shot, not set dressing."
            ),
            "must_not_say": ["scene 5A unchanged"],
        },
        {
            "id": "prop-added",
            "kind": "element_added",
            "summary": "Wire-rimmed reading glasses are added in scene 7.",
            "from_scene": None,
            "to_scene": "7",
            "element": "wire-rimmed reading glasses",
            "departments": ["props"],
            "judgment": (
                "A genuinely new item: it appears nowhere in draft 1. This is the "
                "contrast case for the letter opener, which looks similar to a "
                "diff but is not a new buy."
            ),
            "must_not_say": ["glasses relocated", "glasses moved"],
        },
        {
            "id": "clearance-branded-vehicle",
            "kind": "clearance_risk",
            "summary": "A Ford Bronco is named in the new scene 5A.",
            "from_scene": None,
            "to_scene": "5A",
            "element": "Ford Bronco",
            "risk": "medium",
            "departments": ["clearance", "transport"],
            "judgment": (
                "A named brand introduced by the revision. Caught at script stage "
                "it is a phone call; caught after the shoot it is a reshoot. "
                "Transport also needs it because it is a picture vehicle."
            ),
            "must_not_say": ["no clearance risk", "generic vehicle"],
        },
    ],
    "unchanged_scenes": ["1", "6", "8"],
    # Counted from the changes above, not asserted by hand: `validate` checks
    # these against the list and caught a miscount here when they disagreed.
    "expected_department_counts": {
        "props": 2,
        "cast": 3,
        "locations": 3,
        "transport": 2,
        "schedule": 3,
        "clearance": 1,
        "ad": 1,
    },
}


# ---------------------------------------------------------------------------
# The feature-length pair.
#
# PLAN.md asks for two pairs: a small one for fast iteration, and one at feature
# scale. Running a 120-scene pair on every prompt tweak wastes both time and
# credit, so the small pair above is the working fixture and this one is the
# scale test.
#
# The eight hand-authored scenes are the spine, spread through the film with
# connective scenes between them. The same eight labelled changes apply, at
# whatever numbers the spine lands on. The padding is deliberately identical in
# both drafts, so anything the pipeline reports outside the labelled set is a
# false positive, and measuring that is the point of the scale test.
# ---------------------------------------------------------------------------

FILLER_LOCATIONS = [
    ("INT", "GRAIN ELEVATOR", "DAY"),
    ("EXT", "COUNTY ROAD", "DAY"),
    ("INT", "FEED STORE", "DAY"),
    ("EXT", "IRRIGATION DITCH", "DUSK"),
    ("INT", "BANK LOBBY", "DAY"),
    ("EXT", "CHURCH STEPS", "MORNING"),
    ("INT", "DINER BOOTH", "NIGHT"),
    ("EXT", "TRACTOR SHED", "DAY"),
    ("INT", "HOSPITAL WAITING ROOM", "NIGHT"),
    ("EXT", "CEMETERY GATE", "AFTERNOON"),
]

FILLER_BODY = [
    ("Action", "Mae signs where the clerk points, and does not read the page."),
    ("Character", "MAE"),
    ("Dialogue", "How long does this usually take?"),
]


def _filler(number: int, page: int) -> dict:
    """A connective scene, identical in both drafts.

    These exist to give the pipeline a large volume of genuinely unchanged
    material. A finding reported against one of these is a false positive.
    """
    int_ext, location, tod = FILLER_LOCATIONS[number % len(FILLER_LOCATIONS)]
    return {
        "number": str(number),
        "heading": f"{int_ext}. {location} - {tod}",
        "page": page,
        "summary": f"{location.title()} beat.",
        "body": FILLER_BODY,
    }


def build_feature(spine: list[dict], total_scenes: int = 120) -> list[dict]:
    """Spread the spine through a feature-length script, padding between beats.

    Numbering comes from draft 1 and draft 2 inherits it. That is not a
    convenience: scene numbers are stable across drafts by industry convention,
    which is the whole reason alignment works and the reason scenes are marked
    OMITTED rather than deleted. Numbering each draft independently would shift
    every scene after the insert and quietly break the premise the project rests
    on. An earlier version of this function did exactly that, and `validate`
    caught it: unchanged spine scenes landed on 76 in one draft and 78 in the
    other.

    So draft 1 is laid out first, and draft 2 reuses those numbers by matching
    on the position in the spine. The inserted scene takes a letter suffix
    against its predecessor, e.g. `5A` becomes `61A`.
    """
    layout = _layout(total_scenes)
    # Keyed by the small-pair number, so both drafts resolve the same scene to
    # the same slot regardless of how many scenes each of them has.
    slots: dict[str, str] = layout["slots"]

    out: list[dict] = []
    for scene in spine:
        copied = dict(scene)
        copied["number"] = slots[scene["number"]]
        out.append(copied)

    # Interleave the filler at its own numbers, then sort into scene order.
    for number, page in layout["filler"]:
        out.append(_filler(number, page))

    out.sort(key=_scene_sort_key)
    for position, scene in enumerate(out):
        scene["page"] = position // 2 + 1
    return out


def _scene_sort_key(scene: dict) -> tuple[int, str]:
    """Order scenes the way a script supervisor reads them: 60, 61, 61A, 62."""
    raw = scene["number"]
    digits = "".join(c for c in raw if c.isdigit())
    suffix = "".join(c for c in raw if c.isalpha())
    return (int(digits) if digits else 0, suffix)


def _layout(total_scenes: int) -> dict:
    """Decide which numbers the spine occupies and which the filler fills.

    Computed once and shared by both drafts, so the numbering is identical.
    `DRAFT_1` sets the sequence; the extra scene in `DRAFT_2` is the insert and
    takes a letter suffix rather than a number of its own.
    """
    spine_count = len(DRAFT_1)
    gap = max(1, (total_scenes - spine_count) // spine_count)

    spine_numbers: list[str] = []
    filler: list[tuple[int, int]] = []
    number = 1

    for index in range(spine_count):
        if index > 0:
            for _ in range(gap):
                filler.append((number, 0))
                number += 1
        spine_numbers.append(str(number))
        number += 1

    while len(spine_numbers) + len(filler) < total_scenes:
        filler.append((number, 0))
        number += 1

    # Map every small-pair scene number onto its feature-scale slot. DRAFT_1
    # defines the sequence; DRAFT_2's extra scene is the insert and takes a
    # letter suffix against the scene it follows, so it needs no slot of its own.
    slots = {
        scene["number"]: spine_numbers[index]
        for index, scene in enumerate(DRAFT_1)
    }
    for index, scene in enumerate(DRAFT_2):
        if scene["number"].endswith("A"):
            predecessor = DRAFT_2[index - 1]["number"]
            slots[scene["number"]] = f"{slots[predecessor]}A"

    return {"slots": slots, "filler": filler}


def feature_answer_key(feature_1: list[dict], feature_2: list[dict]) -> dict:
    """The small key, renumbered to wherever the spine landed at feature scale.

    Scenes are matched by heading and body rather than by position, because
    filler is interleaved and the two drafts have different scene counts.
    """
    import copy

    def locate(spine: list[dict], built: list[dict]) -> dict[str, str]:
        mapping: dict[str, str] = {}
        for original in spine:
            for candidate in built:
                if candidate.get("heading") == original["heading"] and candidate.get(
                    "body"
                ) == original.get("body"):
                    mapping[original["number"]] = candidate["number"]
                    break
        return mapping

    before = locate(DRAFT_1, feature_1)
    after = locate(DRAFT_2, feature_2)

    key = copy.deepcopy(ANSWER_KEY)
    key["pair"] = "feature"
    key["draft_from"] = "feature-pair-1.fdx"
    key["draft_to"] = "feature-pair-2.fdx"
    key["note"] = (
        "The same eight labelled changes at feature scale. Every other scene is "
        "identical between the drafts, so anything reported outside this set is "
        "a false positive."
    )
    for change in key["changes"]:
        if change.get("from_scene") in before:
            change["from_scene"] = before[change["from_scene"]]
        if change.get("to_scene") in after:
            change["to_scene"] = after[change["to_scene"]]

    key["unchanged_scenes"] = sorted(
        {after[n] for n in ANSWER_KEY["unchanged_scenes"] if n in after}
    )
    return key


# ---------------------------------------------------------------------------
# .fdx emission
# ---------------------------------------------------------------------------


def _text(content: str, revision_id: int = 0) -> str:
    return (
        f'<Text AdornmentStyle="0" Background="#FFFFFFFFFFFF" Color="#000000000000" '
        f'Font="Courier Final Draft" RevisionID="{revision_id}" Size="12" '
        f'Style="">{escape(content)}</Text>'
    )


def _scene_xml(scene: dict) -> str:
    revised = scene.get("revised", 0)
    # A Summary is optional in real .fdx files, but including it here keeps the
    # fixture exercising the SceneProperties path the parser reads.
    summary = escape(scene.get("summary", scene["heading"].title()))
    lines = [
        f'    <Paragraph Number="{escape(scene["number"])}" Type="Scene Heading">',
        f'      <SceneProperties Length="6/8" Page="{scene["page"]}" Title="">',
        f"        <Summary>{summary}</Summary>",
        "      </SceneProperties>",
        f'      {_text(scene["heading"], revised)}',
        "    </Paragraph>",
    ]
    for para_type, content in scene["body"]:
        lines.append(f'    <Paragraph Type="{para_type}">')
        lines.append(f"      {_text(content, revised)}")
        lines.append("    </Paragraph>")
    return "\n".join(lines)


def build_fdx(scenes: list[dict], title: str, active_set: int) -> str:
    body = "\n".join(_scene_xml(s) for s in scenes)
    revisions = "\n".join(
        f'    <Revision Color="{colour}" FullRevision="No" ID="{rid}" Mark="*" '
        f'Name="{name}" PageColor="#FFFFFFFFFFFF" Style=""/>'
        for rid, name, colour in REVISION_SETS
    )
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="no" ?>
<FinalDraft DocumentType="Script" Template="No" Version="5">

  <Content>
    <Paragraph Type="Action">
      {_text("FADE IN:")}
    </Paragraph>
{body}
  </Content>

  <TitlePage>
    <Content>
      <Paragraph Alignment="Center" Type="General">
        {_text(title)}
      </Paragraph>
    </Content>
  </TitlePage>

  <Revisions ActiveSet="{active_set}" RevisionMode="Yes" RevisionsShown="All">
{revisions}
  </Revisions>

  <SceneNumberOptions NumberScheme="1A" ShowNumbersOnLeft="Yes" ShowNumbersOnRight="Yes"/>
</FinalDraft>
"""


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, default=Path("tests/fixtures"))
    ap.add_argument("--scenes", type=int, default=120, help="feature-pair scene count")
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    one = args.out / "small-draft-1.fdx"
    two = args.out / "small-draft-2.fdx"
    key = args.out / "small-answer-key.json"

    one.write_text(build_fdx(DRAFT_1, "THE LONG ACRE", 1), encoding="utf-8")
    two.write_text(build_fdx(DRAFT_2, "THE LONG ACRE", 2), encoding="utf-8")
    key.write_text(json.dumps(ANSWER_KEY, indent=2) + "\n", encoding="utf-8")

    print(f"wrote {one}  ({len(DRAFT_1)} scenes)")
    print(f"wrote {two}  ({len(DRAFT_2)} scenes)")
    print(f"wrote {key}  ({len(ANSWER_KEY['changes'])} labelled changes)")

    # The feature-scale pair: the same eight changes, buried in unchanged
    # material. PLAN.md asks for both, because running a 120-scene pair on every
    # prompt tweak wastes time and credit.
    feature_1 = build_feature(DRAFT_1, args.scenes)
    feature_2 = build_feature(DRAFT_2, args.scenes)
    f_one = args.out / "feature-pair-1.fdx"
    f_two = args.out / "feature-pair-2.fdx"
    f_key = args.out / "feature-answer-key.json"

    f_one.write_text(build_fdx(feature_1, "THE LONG ACRE", 1), encoding="utf-8")
    f_two.write_text(build_fdx(feature_2, "THE LONG ACRE", 2), encoding="utf-8")
    f_key.write_text(
        json.dumps(feature_answer_key(feature_1, feature_2), indent=2) + "\n", encoding="utf-8"
    )

    print(f"wrote {f_one}  ({len(feature_1)} scenes)")
    print(f"wrote {f_two}  ({len(feature_2)} scenes)")
    print(f"wrote {f_key}")


if __name__ == "__main__":
    main()
