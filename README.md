# Revision Ripple

An agent that diffs film script drafts and tells each production department what
changed for them.

A new draft lands. The agent wakes on its own, parses both drafts, works out what
actually changed, reasons about what those changes *mean*, and routes the
consequences to each department in that department's own vocabulary. The 1st AD
reviews one screen and approves. Notifications go out.

Built with the [Strands Agents SDK](https://strandsagents.com) on Amazon Bedrock.

See [PRD.md](PRD.md) for the product, [PLAN.md](PLAN.md) for build order, and
[DECISIONS.md](DECISIONS.md) for technical choices and why.

---

## Status

| Layer | State |
|---|---|
| 0 Ground | Built. Bedrock call pending AWS setup |
| 1.1 `.fdx` parser | Done, validated on a 133-scene fixture |
| 1.2 Script model | Done |
| 3.6 Progress events | Done, built in alongside |
| 1.3 PDF parser | Done, margins calibrated per document |
| 2 Test data | Next |

---

## Requirements

- Python 3.12 or 3.13
- An AWS account with Bedrock model access enabled (see [Setup](#setup))

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
ripple doctor
```

`ripple doctor` checks your Python version, config, AWS credentials, Bedrock
model access, and the fallback providers — and tells you exactly what to fix.

## Usage

```bash
ripple doctor                        # check the environment
ripple models                        # list Bedrock models in your region
ripple parse script.fdx              # parse a draft to structured JSON
ripple parse script.pdf              # tier 2: coordinate-driven
ripple parse script.fdx -o out.json  # write it to a file
ripple parse script.fdx --stats      # summary instead of full JSON
ripple parse script.fdx -v           # show per-scene progress events
```

## Layout

```
src/ripple/
  config.py      Settings, loaded from env
  model/         The internal script model (Layer 1.2) — the shared contract
  parse/         Parser tiers: .fdx (1), PDF (2), OCR (3)
  diff/          Scene alignment and diffing (Layer 3)
  llm/           Bedrock client and the model fallback chain
  events/        Structured progress events (Layer 3.6)
  agents/        Strands department agents (Layer 5)
  api/           FastAPI REST + SSE (Layer 7)
```

## Cost safety

AWS has no hard spending cap and billing lags hours. This project keeps the
guards architectural: no LLM call in an unbounded loop, `max_tokens` always set,
a per-run call ceiling (`RIPPLE_MAX_LLM_CALLS_PER_RUN`), and response caching
during iteration (`RIPPLE_CACHE_LLM`). Develop against the small test pair, not a
feature-length script.

## License

MIT — see [LICENSE](LICENSE).
