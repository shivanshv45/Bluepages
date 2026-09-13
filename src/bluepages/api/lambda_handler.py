"""The API on Lambda, behind a Function URL (Layer 7, the other deployment).

Same reasoning as `trigger/agentcore.py`: the API is one more entry point over
`create_app()`, not a second implementation. Mangum translates the Function
URL's event shape into an ASGI call and back, so nothing here knows it is not
`uvicorn`.

Lambda's filesystem is read-only outside `/tmp`, so both the SQLite fallback
and the upload workspace are pointed there. A warm container's `/tmp` persists
between invocations on the *same* container, which is a cache, not durable
storage: set `SUPABASE_DB_URL` for anything that has to survive a cold start.

**SSE does not stream here.** A standard Function URL buffers the whole
response before it reaches the client, and Mangum does not speak Lambda's
response-streaming invoke mode. `/api/runs/{run_id}/stream` still works
functionally (events are held and delivered as one burst when the run ends or
the connection times out) but the live, event-by-event view degrades to
"watch it appear once it's done". `bluepages watch` and the CLI's own
`--verbose` progress are unaffected: they read the same events locally,
off-Lambda. Two ways to get real streaming back, neither done here: run the
API on a container target that speaks HTTP natively (App Runner, ECS, EC2), or
put it on Lambda with response streaming invoked directly through a Function
URL (no Mangum, an ASGI adapter that supports it, e.g. a `run_in_lambda`-style
raw handler).
"""

from __future__ import annotations

from pathlib import Path

from mangum import Mangum

from bluepages.api.app import create_app

_app = create_app(
    db_path=Path("/tmp/bluepages.db"),
    workspace=Path("/tmp/workspace/drafts"),
)

# api_gateway_base_path is irrelevant for a Function URL (no stage prefix);
# left at the default so the same handler still works behind API Gateway.
handler = Mangum(_app)
