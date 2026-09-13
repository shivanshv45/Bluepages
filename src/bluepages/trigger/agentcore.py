"""The AgentCore Runtime entry point (Layer 6, the other deployment).

Bluepages deploys as a Lambda on an S3 trigger, because the agent is dormant by
default and a revision arrives a few times a day: paying for an always-on
runtime is paying for idle. This module is the same pipeline behind an
AgentCore-shaped invocation, so moving is a configuration change rather than a
rewrite, and both paths stay tested.

The difference between them is only how the work arrives. Lambda is handed an
S3 event and infers the production from the key. AgentCore is handed a JSON
payload and told directly. Everything after that is `handle_upload`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def invoke(payload: dict[str, Any], context: Any = None) -> dict[str, Any]:
    """Run one revision from an AgentCore invocation payload.

    The payload names the bucket and the key of the draft that arrived:

        {"bucket": "bluepages-scripts",
         "key": "productions/The Farm/draft-2.fdx"}

    A local folder works too, which is what the tests and the demo use:

        {"root": "./scripts", "key": "The Farm/draft-2.fdx"}
    """
    from bluepages.config import get_settings
    from bluepages.events import JsonLinesStream
    from bluepages.llm import ModelClient, RunBudget
    from bluepages.trigger.handler import handle_upload
    from bluepages.trigger.storage import LocalStore, S3Store

    key = payload.get("key")
    if not key:
        # A payload with no key is a caller bug, not a transient failure. Saying
        # so beats running with a default and reporting on the wrong draft.
        return {"error": "payload must name the uploaded draft as 'key'"}

    bucket, root = payload.get("bucket"), payload.get("root")
    if bucket:
        store: Any = S3Store(bucket)
    elif root:
        store = LocalStore(root)
    else:
        return {"error": "payload must name either a 'bucket' or a local 'root'"}

    settings = get_settings()
    stream = JsonLinesStream()
    budget = RunBudget(
        max_calls=int(
            payload.get("max_calls") or settings.bluepages_max_llm_calls_per_run
        )
    )
    client = ModelClient(settings=settings, stream=stream, budget=budget)

    result = handle_upload(
        store=store,
        key=str(key),
        client=client,
        stream=stream,
        workdir=Path(payload["workdir"]) if payload.get("workdir") else None,
        # Absent, persistence goes wherever the environment points, which is
        # Supabase when it is configured. A caller has to be able to say.
        db_path=Path(payload["db_path"]) if payload.get("db_path") else None,
    )
    return result.to_dict()


def build_app() -> Any:
    """The AgentCore app, when the SDK is installed.

    Imported lazily and behind a function so the package still imports, and the
    Lambda still deploys, on a machine that has never heard of AgentCore.
    """
    from bedrock_agentcore.runtime import BedrockAgentCoreApp  # type: ignore[import-not-found]

    app = BedrockAgentCoreApp()

    @app.entrypoint
    def handler(payload: dict[str, Any]) -> dict[str, Any]:
        return invoke(payload)

    return app


__all__ = ["build_app", "invoke"]
