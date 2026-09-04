# Bluepages

An agent that diffs film script drafts and tells each production department what
changed for them.

A new draft lands. The agent wakes on its own, parses both drafts, works out what
actually changed, reasons about what those changes *mean*, and routes the
consequences to each department in that department's own vocabulary. The 1st AD
reviews one screen and approves. Notifications go out.

Built with the [Strands Agents SDK](https://strandsagents.com) on Amazon Bedrock.

## Why

A film script is rewritten constantly during production, sometimes daily. Every
revision silently invalidates the element lists eight or more departments work
from. Today the 1st Assistant Director reads both drafts side by side and works
out the difference by hand. It takes hours, it happens at night because
revisions arrive after the shooting day and are needed before call time, and
things get missed. A missed element costs a shoot day.

The mechanical diff is the easy part. The product is the judgment on top of it:

- The letter opener moved from sc. 3 to sc. 7. Same object relocated, or a cut
  and a new buy? Props needs the difference, because one is a continuity note
  and the other is a purchase order.
- `JANITOR` became `CUSTODIAN`. Renamed character, or a genuinely new role?
  Casting's answer is worth thousands of dollars.
- "hands her the envelope" became "slides the envelope across the table". Props
  are unaffected, but that is a different camera setup and the AD needs to know.
- Scene 2 flipped DAY to NIGHT. Not a prop change at all. A scheduling change,
  and possibly a location re-quote.

Structural diffing cannot tell you that a dialogue rewrite has no physical
consequence while one word in an action line just cost the production a day.

## Requirements

- Python 3.12 or 3.13
- An AWS account with Bedrock model access enabled, for the semantic layer.
  Parsing and the mechanical diff run without it.

## Setup

```bash
# 1. Create the virtualenv
py -3.12 -m venv .venv          # Windows
python3.12 -m venv .venv        # macOS / Linux

# 2. Activate it
.venv\Scripts\activate          # Windows
source .venv/bin/activate       # macOS / Linux

# 3. Install
pip install -e ".[dev]"

# 4. Configure
cp .env.example .env            # then fill in .env

# 5. Verify everything is wired up
bluepages doctor
```

`bluepages doctor` checks your Python version, config, AWS credentials, Bedrock
model access and the fallback providers, and tells you exactly what to fix. It
ends by actually calling Bedrock, because "credentials resolve" and "the model
answers" are different things.

### Bedrock access

Model access is enabled per region and per model in the AWS console: **Bedrock →
Model access → Modify model access**, then enable Claude Sonnet 5 and Claude
Haiku 4.5 in the region you intend to use. Run `bluepages models` to list the
model IDs that region actually offers, and put them in `.env`. Model IDs are
config, never hardcoded, so pointing at a different model or an
inference-profile ARN is an `.env` change.

## Usage

Parse a draft into the internal script model:

```bash
bluepages parse script.fdx                # .fdx, tier 1, the primary path
bluepages parse script.pdf                # PDF, tier 2, coordinate-driven
bluepages parse script.fdx --stats        # summary instead of full JSON
bluepages parse script.fdx -o out.json    # write it to a file
bluepages parse script.fdx -v             # show per-scene progress events
```

Diff two drafts. This is mechanical only and costs nothing:

```bash
bluepages diff old.fdx new.fdx
bluepages diff old.fdx new.fdx --key answer-key.json     # and score the result
```

Run the full pipeline, including the model passes. This costs money:

```bash
bluepages reason old.fdx new.fdx
bluepages reason old.fdx new.fdx --key answer-key.json   # score against ground truth
bluepages reason old.fdx new.fdx --no-elements           # skip extraction, fewer calls
bluepages reason old.fdx new.fdx --max-calls 20          # tighter ceiling for this run
bluepages reason old.fdx new.fdx --json findings.json    # write the findings out
```

Environment and test data:

```bash
bluepages doctor                          # check the environment
bluepages models                          # list Bedrock models in your region
bluepages key tests/fixtures/small-answer-key.json   # show and validate an answer key
```

### Try it

The repo ships a small hand-authored revision pair and a labelled answer key:

```bash
bluepages diff tests/fixtures/small-draft-1.fdx tests/fixtures/small-draft-2.fdx \
  --key tests/fixtures/small-answer-key.json
```

## How it works

```
  .fdx / PDF                parse        Screenplay (scenes, typed elements)
       │
       ▼
  align on @Number          3.1          matched / omitted / inserted / removed
       │
       ▼
  difflib within pairs      3.2          change spans, relocation candidates
       │
       ▼
  Haiku over each scene     3.3          props, cast, vehicles, wardrobe, clearance
       │
       ▼
  Sonnet over each change   3.4          what it means, and who needs to know
       │
       ▼
  findings, routed by department
```

Scene numbers are the anchor. They are stable across drafts by industry
convention, which is exactly why scenes are marked `OMITTED` rather than
deleted: deleting scene 34 would renumber everything after it. Alignment rests
on that premise.

The mechanical layer never claims an element moved. It emits relocation
*candidates*, and whether the letter opener in sc. 7 is the one that left sc. 3
is the judgment the semantic layer exists to make. Asserting it in the substrate
would bake the wrong answer in below the layer that can reason about it.

## Correctness

No public corpus of the same script at draft N and N+1 exists, so the revision
pairs are authored here, by hand, along with a labelled answer key. The key
records the exact list of what changed, which department each change belongs to,
and a `must_not_say` list per change: the specific wrong conclusions that would
be expensive in production.

That key is the correctness measure. Semantic output with no ground truth to
check it against is not verified, so `--key` scores three things separately:

- **recall**, whether each labelled change was found at all
- **judgment**, whether it reached the right conclusion about the change
- **forbidden phrases**, whether it said something the key rules out

```bash
pytest                    # the full suite; the paid tests are deselected
pytest -m live            # the semantic layer against real Bedrock. Costs money.
ruff check src tests scripts
mypy
```

The `live` suite is the only one that measures model judgment. It skips when AWS
is not configured.

## Layout

```
src/bluepages/
  config.py      Settings, loaded from env
  model/         The internal script model, the shared contract
  parse/         Parser tiers: .fdx (1), PDF (2), OCR (3)
  diff/          Scene alignment and the mechanical diff
  semantic/      Element extraction, reasoning, and scoring against the key
  llm/           Bedrock client, structured output, model fallback chain
  events/        Structured progress events, consumed by the CLI and later SSE
  testdata.py    Answer key loading and validation
  agents/        Strands department agents
  api/           FastAPI REST + SSE
```

## Cost safety

AWS has no hard spending cap and billing lags hours, so the guards are
architectural rather than reactive:

- No LLM call sits in an unbounded loop
- `max_tokens` is always set; there is no code path that omits it
- Every run has a call ceiling (`BLUEPAGES_MAX_LLM_CALLS_PER_RUN`) that raises
  rather than continuing
- Responses are cached on disk during iteration (`BLUEPAGES_CACHE_LLM`), so
  re-running on an unchanged pair is free
- Develop against the small test pair, not a feature-length script

The model fallback chain (Sonnet → Haiku → Groq/Gemini) is for resilience, not
economy: Bedrock throttles under parallel load. It triggers on throttling,
timeouts and 5xx only, never on schema or prompt errors, which fail identically
on every provider. Which model answered is always logged, because fallback
output is weaker and that should be visible rather than silent.

## License

MIT — see [LICENSE](LICENSE).
