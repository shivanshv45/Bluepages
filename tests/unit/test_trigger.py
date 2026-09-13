"""The autonomous trigger (Layer 6).

Layer 6 is what turns a script someone runs into an agent. The things worth
testing are the judgments it makes with no human present: which production a
file belongs to, which draft it supersedes, what to do when there is no earlier
draft, and what it must not do on its own.

The last one is the important one. An agent that wakes on an upload and mails
eight department heads unasked is the thing a 1st AD turns off on day two, so
the approval gate is asserted here and not only in the CLI tests.
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from bluepages.events import CollectingStream, EventKind
from bluepages.trigger import (
    LocalStore,
    S3Store,
    handle_upload,
    is_script,
    previous_draft,
    production_of,
    s3_records,
)
from tests.unit.fakes import ScriptedClient, elements_answer
from tests.unit.test_cli_fanout import DEPARTMENT_RULES
from tests.unit.test_cli_reason import CORRECT_RULES
from tests.unit.test_cli_store import ELEMENT_RULES

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
ALL_RULES = ELEMENT_RULES + CORRECT_RULES + DEPARTMENT_RULES


@pytest.fixture
def client() -> ScriptedClient:
    return ScriptedClient(rules=ALL_RULES, default=elements_answer())


@pytest.fixture
def store(tmp_path: Path) -> LocalStore:
    """A production folder with two drafts, the second uploaded later."""
    folder = tmp_path / "The Farm"
    folder.mkdir(parents=True)
    first = folder / "draft-1.fdx"
    second = folder / "draft-2.fdx"
    shutil.copyfile(FIXTURES / "small-draft-1.fdx", first)
    shutil.copyfile(FIXTURES / "small-draft-2.fdx", second)

    # Explicit mtimes: the ordering rule is upload time, and a copy can land
    # both files in the same clock tick.
    now = datetime.now(tz=UTC)
    _touch(first, now - timedelta(hours=2))
    _touch(second, now)
    return LocalStore(tmp_path)


def _touch(path: Path, when: datetime) -> None:
    stamp = when.timestamp()
    import os

    os.utime(path, (stamp, stamp))


# --- reading the key ------------------------------------------------------


def test_the_production_is_the_folder() -> None:
    assert production_of("productions/The Farm/draft-2.fdx") == "The Farm"


def test_a_key_with_one_folder_still_names_the_production() -> None:
    assert production_of("The Farm/draft-2.fdx") == "The Farm"


def test_a_bare_file_becomes_its_own_production() -> None:
    """A bucket someone dropped a single file into must still work."""
    assert production_of("draft-2.fdx") == "draft-2"


def test_backslash_keys_are_handled() -> None:
    """Windows-style separators reach the store when a local folder is synced."""
    assert production_of("productions\\The Farm\\draft-2.fdx") == "The Farm"


@pytest.mark.parametrize("name", ["a.fdx", "a.FDX", "a.pdf", "b/c/d.Pdf"])
def test_scripts_are_recognised(name: str) -> None:
    assert is_script(name)


@pytest.mark.parametrize("name", ["callsheet.xlsx", "notes.txt", "a.fdx.bak", "folder/"])
def test_everything_else_is_not_a_script(name: str) -> None:
    assert not is_script(name)


# --- the S3 event ---------------------------------------------------------


def test_s3_records_are_read() -> None:
    event = {
        "Records": [
            {"s3": {"bucket": {"name": "b"}, "object": {"key": "k/one.fdx"}}},
            {"s3": {"bucket": {"name": "b"}, "object": {"key": "k/two.fdx"}}},
        ]
    }
    assert s3_records(event) == [("b", "k/one.fdx"), ("b", "k/two.fdx")]


def test_keys_are_url_decoded() -> None:
    """S3 sends "The+Farm". Left encoded it becomes a second production."""
    event = {
        "Records": [
            {"s3": {"bucket": {"name": "b"}, "object": {"key": "productions/The+Farm/d.fdx"}}}
        ]
    }
    [(_, key)] = s3_records(event)
    assert key == "productions/The Farm/d.fdx"
    assert production_of(key) == "The Farm"


def test_an_event_with_no_records_is_not_an_error() -> None:
    """S3 sends test events on notification setup."""
    assert s3_records({}) == []


def test_a_malformed_record_is_skipped() -> None:
    assert s3_records({"Records": [{"s3": {}}, {}]}) == []


# --- finding the previous draft -------------------------------------------


def test_the_previous_draft_is_the_one_before_it(store: LocalStore) -> None:
    earlier = previous_draft(store, "The Farm", "The Farm/draft-2.fdx")
    assert earlier is not None
    assert earlier.filename == "draft-1.fdx"


def test_the_first_draft_has_no_previous(store: LocalStore) -> None:
    """Not an error. There is simply nothing to diff against yet."""
    assert previous_draft(store, "The Farm", "The Farm/draft-1.fdx") is None


def test_ordering_is_by_upload_time_not_filename(tmp_path: Path) -> None:
    """Productions name drafts by the revision colour convention.

    White, blue, pink, yellow, green is meaningful to a 1st AD and meaningless
    to `sorted`, so sorting by name would diff the wrong pair.
    """
    folder = tmp_path / "The Farm"
    folder.mkdir(parents=True)
    now = datetime.now(tz=UTC)
    for name, age in [("white.fdx", 3), ("blue.fdx", 2), ("pink.fdx", 1)]:
        path = folder / name
        shutil.copyfile(FIXTURES / "small-draft-1.fdx", path)
        _touch(path, now - timedelta(hours=age))

    earlier = previous_draft(LocalStore(tmp_path), "The Farm", "The Farm/pink.fdx")
    assert earlier is not None
    assert earlier.filename == "blue.fdx"


def test_a_late_upload_diffs_against_what_preceded_it(tmp_path: Path) -> None:
    """An old draft uploaded late must not diff against a newer one.

    Reversed, every real change is reported backwards: an added prop reads as a
    cut and a purchase order becomes a strike.
    """
    folder = tmp_path / "The Farm"
    folder.mkdir(parents=True)
    now = datetime.now(tz=UTC)
    for name, age in [("first.fdx", 5), ("second.fdx", 4), ("newest.fdx", 1)]:
        path = folder / name
        shutil.copyfile(FIXTURES / "small-draft-1.fdx", path)
        _touch(path, now - timedelta(hours=age))

    earlier = previous_draft(LocalStore(tmp_path), "The Farm", "The Farm/second.fdx")
    assert earlier is not None
    assert earlier.filename == "first.fdx"


def test_non_scripts_in_the_folder_are_not_candidates(store: LocalStore) -> None:
    """A call sheet next to the drafts must never be diffed as one."""
    (store.root / "The Farm" / "callsheet.xlsx").write_text("x", encoding="utf-8")

    drafts = store.list_drafts("The Farm")
    assert all(d.suffix in {".fdx", ".pdf"} for d in drafts)


def test_an_unknown_production_lists_nothing(store: LocalStore) -> None:
    assert store.list_drafts("No Such Production") == []


# --- the run --------------------------------------------------------------


def test_an_upload_runs_the_pipeline(
    store: LocalStore, client: ScriptedClient, tmp_path: Path
) -> None:
    result = handle_upload(
        store,
        "The Farm/draft-2.fdx",
        client=client,
        db_path=tmp_path / "elements.db",
    )

    assert result.ran
    assert result.production == "The Farm"
    assert result.previous_key == "The Farm/draft-1.fdx"
    assert result.summary["fan_out"]["reports"] > 0


def test_a_first_draft_does_nothing_and_says_why(
    store: LocalStore, client: ScriptedClient, tmp_path: Path
) -> None:
    """Not a failure. A caller treating it as one would retry forever."""
    result = handle_upload(
        store,
        "The Farm/draft-1.fdx",
        client=client,
        db_path=tmp_path / "elements.db",
    )

    assert not result.ran
    assert "first draft" in result.reason
    assert client.prompts == [], "a first draft must not cost a model call"


def test_a_non_script_upload_is_ignored(
    store: LocalStore, client: ScriptedClient, tmp_path: Path
) -> None:
    """Productions keep call sheets in the same folders.

    Waking the agent on every object is how a trigger becomes expensive.
    """
    result = handle_upload(
        store, "The Farm/callsheet.xlsx", client=client, db_path=tmp_path / "e.db"
    )

    assert not result.ran
    assert "not a script" in result.reason
    assert client.prompts == []


def test_the_run_emits_events(
    store: LocalStore, client: ScriptedClient, tmp_path: Path
) -> None:
    """Layer 7 watches an autonomous run through these."""
    stream = CollectingStream()
    handle_upload(
        store,
        "The Farm/draft-2.fdx",
        client=client,
        stream=stream,
        db_path=tmp_path / "elements.db",
    )

    started = stream.of_kind(EventKind.RUN_STARTED)
    assert any(e.data.get("trigger") == "upload" for e in started)
    assert stream.count(EventKind.RUN_FINISHED) >= 1


def test_an_autonomous_run_sends_nothing(
    store: LocalStore, client: ScriptedClient, tmp_path: Path
) -> None:
    """The property Layer 6 must not break.

    The agent wakes on its own, does the work, and stops at the AD's desk. Every
    report it wrote is unapproved, and Layer 8's send queue is empty.
    """
    from bluepages.store import Repository, open_database

    db_path = tmp_path / "elements.db"
    handle_upload(
        store, "The Farm/draft-2.fdx", client=client, db_path=db_path
    )

    with open_database(path=db_path) as db:
        repo = Repository(db)
        draft = repo.latest_draft(repo.ensure_production("The Farm"))
        reports = repo.reports_for_draft(str(draft["id"]))
        queued = repo.pending_reports(str(draft["id"]))

    assert reports, "the run persisted no reports"
    assert all(r["approved_at"] is None for r in reports)
    assert queued == [], "an autonomous run queued something to send"


def test_the_working_files_are_cleaned_up(
    store: LocalStore, client: ScriptedClient, tmp_path: Path
) -> None:
    """Lambda reuses /tmp across warm invocations.

    A handler that never cleans up eventually fails on a full disk, for reasons
    that look nothing like the cause.
    """
    import tempfile

    tmp = Path(tempfile.gettempdir())
    before = set(tmp.glob("bluepages-*"))
    handle_upload(
        store, "The Farm/draft-2.fdx", client=client, db_path=tmp_path / "e.db"
    )

    assert set(tmp.glob("bluepages-*")) <= before


def test_persistence_can_be_skipped(
    store: LocalStore, client: ScriptedClient
) -> None:
    """A dry run must not write to the inventory."""
    result = handle_upload(
        store, "The Farm/draft-2.fdx", client=client, persist_run=False
    )
    assert result.ran


# --- the S3 store ---------------------------------------------------------


class _FakeS3:
    """Just enough of the S3 client to exercise `S3Store`."""

    def __init__(self, objects: list[dict]) -> None:
        self.objects = objects
        self.downloaded: list[tuple[str, str]] = []

    def get_paginator(self, _name: str):
        class _P:
            def paginate(_self, **kwargs):
                prefix = kwargs.get("Prefix", "")
                yield {
                    "Contents": [
                        o for o in self.objects if o["Key"].startswith(prefix)
                    ]
                }

        return _P()

    def download_file(self, bucket: str, key: str, path: str) -> None:
        self.downloaded.append((bucket, key))
        Path(path).write_text("x", encoding="utf-8")


def test_s3_store_lists_only_scripts_under_the_production() -> None:
    now = datetime.now(tz=UTC)
    fake = _FakeS3([
        {"Key": "productions/The Farm/a.fdx", "LastModified": now, "Size": 1},
        {"Key": "productions/The Farm/callsheet.xlsx", "LastModified": now, "Size": 1},
        {"Key": "productions/Other/b.fdx", "LastModified": now, "Size": 1},
    ])
    store = S3Store("bucket", client=fake, prefix="productions")

    drafts = store.list_drafts("The Farm")
    assert [d.filename for d in drafts] == ["a.fdx"]


def test_s3_store_orders_oldest_first() -> None:
    now = datetime.now(tz=UTC)
    fake = _FakeS3([
        {"Key": "p/The Farm/new.fdx", "LastModified": now, "Size": 1},
        {"Key": "p/The Farm/old.fdx", "LastModified": now - timedelta(days=1), "Size": 1},
    ])
    store = S3Store("bucket", client=fake, prefix="p")

    assert [d.filename for d in store.list_drafts("The Farm")] == ["old.fdx", "new.fdx"]


def test_s3_store_downloads_to_the_given_path(tmp_path: Path) -> None:
    fake = _FakeS3([])
    store = S3Store("bucket", client=fake)
    target = tmp_path / "nested" / "after.fdx"

    store.download("k/after.fdx", target)

    assert target.exists()
    assert fake.downloaded == [("bucket", "k/after.fdx")]


# --- the AgentCore entry point --------------------------------------------


def test_agentcore_runs_a_local_payload(
    store: LocalStore, client: ScriptedClient, monkeypatch, tmp_path: Path
) -> None:
    """The same pipeline, a different invocation shape."""
    from bluepages.trigger import agentcore

    monkeypatch.setattr("bluepages.llm.ModelClient", lambda *a, **k: client)
    # Explicit, so the test does not persist into whatever the developer's .env
    # happens to configure.
    out = agentcore.invoke(
        {
            "root": str(store.root),
            "key": "The Farm/draft-2.fdx",
            "db_path": str(tmp_path / "elements.db"),
        }
    )

    assert out["ran"]
    assert out["production"] == "The Farm"


def test_agentcore_rejects_a_payload_with_no_key() -> None:
    """A caller bug, not a transient failure. Running a default would report
    on the wrong draft."""
    from bluepages.trigger import agentcore

    assert "error" in agentcore.invoke({"bucket": "b"})


def test_agentcore_rejects_a_payload_with_no_source() -> None:
    from bluepages.trigger import agentcore

    assert "error" in agentcore.invoke({"key": "The Farm/draft-2.fdx"})
