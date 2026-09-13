"""The autonomous trigger (Layer 6).

A draft lands in storage and the agent wakes. No one opens an app, and nothing
tells the agent which draft the new one supersedes: working that out is its own
first judgment.
"""

from bluepages.trigger.handler import (
    TriggerResult,
    handle_upload,
    lambda_handler,
    s3_records,
)
from bluepages.trigger.storage import (
    LocalStore,
    S3Store,
    ScriptStore,
    StoredDraft,
    is_script,
    previous_draft,
    production_of,
)

__all__ = [
    "LocalStore",
    "S3Store",
    "ScriptStore",
    "StoredDraft",
    "TriggerResult",
    "handle_upload",
    "is_script",
    "lambda_handler",
    "previous_draft",
    "production_of",
    "s3_records",
]
