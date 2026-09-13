"""The element database (Layer 4).

`production -> draft -> scene -> element`, with element identity tracked across
drafts. Identity is what makes "the same object, moved" a fact the system can
query rather than a sentence in a report.

SQLite locally, Postgres on Supabase, one schema for both.
"""

from bluepages.store.db import Database, new_id, now, open_database
from bluepages.store.repository import PersistedRun, Repository

__all__ = [
    "Database",
    "PersistedRun",
    "Repository",
    "new_id",
    "now",
    "open_database",
]
