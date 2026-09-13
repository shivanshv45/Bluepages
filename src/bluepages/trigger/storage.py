"""Script storage: S3, and a local directory that behaves the same way.

The autonomous trigger needs one thing the CLI never did. A draft arrives on its
own, and nothing tells the agent which draft it supersedes. `previous_draft` is
that judgment, and it is the reason this module exists rather than the Lambda
calling boto3 inline.

The layout is `productions/<production>/<filename>`, and the production is the
folder. Ordering within a folder is by the time S3 recorded the object, not by
the filename, because a production that names drafts "blue", "pink", "yellow"
by the industry colour convention does not sort in revision order and never
will.

`LocalStore` is the same interface over a directory. It exists so the trigger
path is exercised by the test suite rather than only when AWS is configured,
which is the same reason the element database runs on SQLite locally.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

# What a script can be. Anything else in the bucket is ignored rather than
# failing the trigger: productions keep call sheets and PDFs of notes in the
# same folders, and waking the agent on a call sheet costs money for nothing.
SCRIPT_SUFFIXES = frozenset({".fdx", ".pdf"})


class NotAScriptError(ValueError):
    """The uploaded object is not a script. Not an error worth retrying."""


class NoPreviousDraftError(LookupError):
    """The first draft of a production. There is nothing to diff it against."""


@dataclass(frozen=True)
class StoredDraft:
    """One draft in storage."""

    key: str
    production: str
    filename: str
    modified: datetime
    size: int = 0

    @property
    def suffix(self) -> str:
        return Path(self.filename).suffix.lower()


def production_of(key: str) -> str:
    """The production a key belongs to.

    `productions/The Farm/draft-2.fdx` is The Farm. A key with no folder is
    treated as its own production named by the file, which keeps a bucket
    someone dropped a single file into working rather than erroring.
    """
    parts = [p for p in key.replace("\\", "/").split("/") if p]
    if len(parts) >= 2:
        # The folder immediately containing the file, so both
        # `productions/The Farm/x.fdx` and `The Farm/x.fdx` work.
        return parts[-2]
    return Path(parts[-1]).stem if parts else ""


def is_script(key: str) -> bool:
    return Path(key).suffix.lower() in SCRIPT_SUFFIXES


class ScriptStore(Protocol):
    """Where drafts live."""

    def list_drafts(self, production: str) -> list[StoredDraft]: ...

    def download(self, key: str, to: Path) -> Path: ...


class LocalStore:
    """A directory of productions. The same interface as S3, on disk."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def list_drafts(self, production: str) -> list[StoredDraft]:
        folder = self.root / production
        if not folder.is_dir():
            return []
        drafts = [
            StoredDraft(
                key=str(path.relative_to(self.root)).replace("\\", "/"),
                production=production,
                filename=path.name,
                modified=datetime.fromtimestamp(path.stat().st_mtime, tz=UTC),
                size=path.stat().st_size,
            )
            for path in folder.iterdir()
            if path.is_file() and is_script(path.name)
        ]
        return sorted(drafts, key=_draft_order)

    def download(self, key: str, to: Path) -> Path:
        """Copy the draft to a working path.

        A copy rather than a reference so the caller can treat local and S3
        identically, including deleting the working file afterwards.
        """
        import shutil

        source = self.root / key
        to.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, to)
        return to


class S3Store:
    """Drafts in an S3 bucket."""

    def __init__(self, bucket: str, client: Any = None, prefix: str = "") -> None:
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self._client = client

    @property
    def client(self) -> Any:
        if self._client is None:
            import boto3

            self._client = boto3.client("s3")
        return self._client

    def _prefix_for(self, production: str) -> str:
        parts = [p for p in (self.prefix, production) if p]
        return "/".join(parts) + "/"

    def list_drafts(self, production: str) -> list[StoredDraft]:
        paginator = self.client.get_paginator("list_objects_v2")
        drafts: list[StoredDraft] = []
        for page in paginator.paginate(
            Bucket=self.bucket, Prefix=self._prefix_for(production)
        ):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if not is_script(key):
                    continue
                drafts.append(
                    StoredDraft(
                        key=key,
                        production=production,
                        filename=Path(key).name,
                        modified=obj["LastModified"],
                        size=obj.get("Size", 0),
                    )
                )
        return sorted(drafts, key=_draft_order)

    def download(self, key: str, to: Path) -> Path:
        to.parent.mkdir(parents=True, exist_ok=True)
        self.client.download_file(self.bucket, key, str(to))
        return to


def _draft_order(draft: StoredDraft) -> tuple[datetime, str]:
    """Oldest first, filename breaking a tie.

    By upload time rather than by name. Productions name drafts by the revision
    colour convention (white, blue, pink, yellow, green), which is meaningful to
    a 1st AD and meaningless to `sorted`.
    """
    return (draft.modified, draft.filename)


def previous_draft(
    store: ScriptStore, production: str, key: str
) -> StoredDraft | None:
    """The draft the newly arrived one supersedes.

    The most recent draft older than this one. Returns None for a production's
    first draft, which is not an error: there is simply nothing to diff against
    yet, and the right response is to record the draft and wait for the next.

    An object that arrives out of order, an older draft uploaded late, still
    diffs against what preceded *it* rather than against the newest file in the
    folder. Diffing an old draft against a newer one would report every real
    change backwards.
    """
    drafts = store.list_drafts(production)
    earlier = [d for d in drafts if _draft_order(d) < _key_order(drafts, key)]
    return earlier[-1] if earlier else None


def _key_order(drafts: list[StoredDraft], key: str) -> tuple[datetime, str]:
    for draft in drafts:
        if draft.key == key:
            return _draft_order(draft)
    # Not listed yet: S3 list-after-write is read-after-write consistent, but a
    # local store or a race can still miss it. Treat it as newest.
    return (datetime.max.replace(tzinfo=UTC), "")


__all__ = [
    "SCRIPT_SUFFIXES",
    "LocalStore",
    "NoPreviousDraftError",
    "NotAScriptError",
    "S3Store",
    "ScriptStore",
    "StoredDraft",
    "is_script",
    "previous_draft",
    "production_of",
]
