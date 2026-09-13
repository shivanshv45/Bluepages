# Architecture

End to end: a draft lands, the agent wakes, department heads get told what
changed for them.

![Architecture](docs/diagrams/architecture.svg)

## The path through the system

**Trigger.** An `ObjectCreated` event on the watched S3 prefix
(`productions/<name>/`) invokes a Lambda. The handler finds the previous draft
for that production itself rather than being told about it, so the upload is the
entire interface. A first upload has nothing to compare against and just records
a baseline. The same `run_pipeline()` is what the CLI and the API call, so an
S3 upload and a human typing `bluepages reason` take an identical path.

**Parse.** `.fdx` through `lxml` is tier 1 and the primary path, not a shortcut:
it is what productions actually use, and it gives typed elements, stable scene
numbers and native revision marks. PDF through `pdfplumber` is tier 2, driven by
position rather than semantics, because screenplay PDFs encode structure as
indentation: scene headings at 1.5", dialogue at 2.5", character cues at 3.7".

**Align.** Scenes are matched on `@Number` into matched, omitted, inserted and
removed sets.

**Diff.** `difflib` within each matched pair produces change spans and
relocation candidates.

**Extract.** A bulk-tier model reads each changed scene for props, cast,
vehicles, wardrobe and clearance risks.

**Reason.** A judgment-tier model takes each change, with its scene text, and
decides what it means and who needs to know.

**Route.** Findings fan out to 12 department agents in parallel, each rewriting
the same finding into its own vocabulary. Clearance and schedule impact consume
the same diff without model calls. Ripple checks verify the model's cross-scene
hunches; sourcing prices new elements against the live web; the budget layer
does the arithmetic; the decision log records what the agent chose and why.

**Persist, then stop.** Everything is written unapproved. The AD approves, and
only then does `send` have anything to act on.

![The approval gate](docs/diagrams/approval-gate.svg)

## Why the shape is this shape

**Scene numbers are the alignment anchor**, not line-by-line text matching. They
are stable across drafts by industry convention (a cut scene is marked
`OMITTED`, never deleted, which is exactly why the number stays stable). Get
alignment wrong and every layer downstream reasons about the wrong pair of
scenes.

**The mechanical diff never claims an element moved.** It emits relocation
*candidates*: a prop's name appearing in a scene it did not appear in on the
other draft. Whether that is the same object relocated or a new purchase order
is a judgment made with the full scene text in front of it, not a rule baked
into the substrate. Asserting it lower down would bake the wrong answer in
beneath the layer that can reason about it.

![The semantic layer](docs/diagrams/semantic-layer.svg)

**Two model tiers, not one.** The bulk tier runs once per changed scene doing
cheap, high-volume extraction. The judgment tier runs once per changed scene
doing the reasoning. Both scope to the scenes the revision actually touched, so
cost tracks the size of the revision rather than the length of the script. A
model handed a whole draft and asked what changed will summarize and invent; a
model shown one scene's diff will not.

**Strands `Agent` drives every model call, with the cost guards inside it.**
`max_tokens` is `BedrockModel` config, so no call can omit it. The per-run call
ceiling is a `BeforeInvocationEvent` hook, chosen because that event fires on
`__call__`, `stream_async` *and* `structured_output`, while `BeforeModelCallEvent`
skips structured output entirely. Getting that backwards would leave structured
calls unbounded, which is the precise failure the ceiling exists to prevent. The
disk cache and the fallback chain sit above the agent: a cache hit never
constructs one, and a throttled rung is classified by the same whitelist.

**Fan-out is a thread pool, not a Strands multi-agent primitive.** `Swarm` and
`GraphBuilder` are both for dependent hand-off work; department reports are N
independent, stateless rewrites of an already-routed finding. Strands still
drives each individual call.

**Every stage emits progress events from the start.** The CLI prints them, the
API forwards the identical stream over SSE. The live run view is a consumer of
the pipeline's own instrumentation, not a parallel tracking system that can
drift from what actually happened.

**The AD approval gate is the same code path whether a human or an S3 upload
started the run.** An autonomous trigger that mails a dozen department heads
unasked is the thing that gets switched off on day two. Reports persist
unapproved either way, and `send` only ever sees what `approve` touched.

**Purchases are proposals, and say so.** There is no vendor account and no
payment rail, so the agent places no orders. The sourced product, its price and
its citations are real and checkable; the budget arithmetic is real; the
transaction is not, and it is stored and rendered as a proposal rather than a
record of something that happened.

**Lambda over an always-on runtime.** The agent is dormant between revisions, so
a function that bills for the seconds it runs beats a runtime that bills for
idling. `trigger/agentcore.py` is a tested AgentCore Runtime entry point if that
tradeoff changes.

## Storage

`production → draft → scene → element`, with element identity tracked across
drafts so the same object can be followed from the draft it entered on to the
one it left. Supabase Postgres in deployment, local SQLite otherwise, behind one
repository interface so neither the pipeline nor the API knows which is running.

## Surfaces

| | |
|---|---|
| **CLI** | The complete system: `parse`, `diff`, `reason`, `fan-out`, `approve`, `send`, `report`, `inventory`, `history`, `recipients`, `watch`, `doctor`, `models`, `key`. Everything works headless. |
| **API** | FastAPI. REST for productions, drafts, inventory, approval, decisions, budget and recipients; SSE for the live run stream; session auth. |
| **Web** | Vite + React + TypeScript over that API. Upload, live run console, scenes and inventory, the approval gate, per-department inboxes, decisions and budget. |
| **Email** | One message per department, the urgent count in the subject line, every attempt recorded. Only ever after approval. |
