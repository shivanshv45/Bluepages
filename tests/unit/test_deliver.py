"""Delivery (Layer 8).

Two things matter here and the rest is detail.

Nothing unapproved is ever sent. That is the promise the whole product rests on,
and it has to hold when the run was triggered by an upload nobody watched.

A failed send is recorded as a failure. A department that receives nothing
cannot tell "this revision does not affect you" from "it bounced", so the
system has to be able to.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bluepages.deliver import (
    ConsoleTransport,
    Note,
    ResendTransport,
    SendError,
    build_transport,
    render,
    send_reports,
    subject_line,
)
from bluepages.store import Repository, open_database


@pytest.fixture
def repo(tmp_path: Path):
    with open_database(path=tmp_path / "e.db") as db:
        db.create_schema()
        yield Repository(db)


@pytest.fixture
def draft(repo):
    """A production with one draft and one approved props report."""
    production_id = repo.ensure_production("The Farm")
    draft_id = _make_draft(repo, production_id)
    return production_id, draft_id


def _make_draft(repo, production_id: str) -> str:
    from bluepages.store.db import new_id, now

    draft_id = new_id()
    repo.db.execute(
        "INSERT INTO draft (id, production_id, revision, source_tier, "
        "scene_count, ingested_at) VALUES (?, ?, ?, ?, ?, ?)",
        (draft_id, production_id, 2, "fdx", 8, now()),
    )
    return draft_id


def _add_report(
    repo, draft_id: str, department: str = "props", approved: bool = False
) -> str:
    from bluepages.store.db import new_id, now

    report_id = new_id()
    repo.db.execute(
        "INSERT INTO report (id, to_draft_id, department, summary, model_name, "
        "via_fallback, approved_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            report_id,
            draft_id,
            department,
            "One prop moved scenes.",
            "haiku-4.5",
            0,
            now() if approved else None,
            now(),
        ),
    )
    repo.db.execute(
        "INSERT INTO report_note (id, report_id, scene_number, note, action, "
        "urgent, idx) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (new_id(), report_id, "7", "The letter opener is now in the kitchen.",
         "Re-dress, do not buy.", 0, 0),
    )
    return report_id


class _Transport:
    """Records what it was asked to send. Optionally fails on an address."""

    def __init__(self, fail_on: str | None = None) -> None:
        self.sent: list[tuple[str, str]] = []
        self.fail_on = fail_on

    def send(self, to: str, subject: str, html: str, text: str) -> str:
        if to == self.fail_on:
            raise SendError("mailbox does not exist")
        self.sent.append((to, subject))
        return f"id-{len(self.sent)}"


# --- the gate -------------------------------------------------------------


def test_an_unapproved_report_is_never_sent(repo, draft) -> None:
    """The promise the product rests on."""
    production_id, draft_id = draft
    _add_report(repo, draft_id, approved=False)
    repo.add_recipient(production_id, "props", "props@example.com")
    transport = _Transport()

    result = send_reports(repo, production_id, draft_id, transport)

    assert transport.sent == []
    assert result.skipped_unapproved == 1
    assert result.reports_sent == 0


def test_an_approved_report_is_sent(repo, draft) -> None:
    production_id, draft_id = draft
    _add_report(repo, draft_id, approved=True)
    repo.add_recipient(production_id, "props", "props@example.com")
    transport = _Transport()

    result = send_reports(repo, production_id, draft_id, transport, production="The Farm")

    assert [to for to, _ in transport.sent] == ["props@example.com"]
    assert result.reports_sent == 1


def test_a_mixed_batch_sends_only_what_is_approved(repo, draft) -> None:
    """Normal: the AD approves, a later run adds a department."""
    production_id, draft_id = draft
    _add_report(repo, draft_id, "props", approved=True)
    _add_report(repo, draft_id, "cast", approved=False)
    repo.add_recipient(production_id, "props", "props@example.com")
    repo.add_recipient(production_id, "cast", "cast@example.com")
    transport = _Transport()

    result = send_reports(repo, production_id, draft_id, transport)

    assert [to for to, _ in transport.sent] == ["props@example.com"]
    assert result.skipped_unapproved == 1


def test_a_report_is_not_sent_twice(repo, draft) -> None:
    """The normal accident is running the command again."""
    production_id, draft_id = draft
    _add_report(repo, draft_id, approved=True)
    repo.add_recipient(production_id, "props", "props@example.com")

    first = _Transport()
    send_reports(repo, production_id, draft_id, first)
    second = _Transport()
    result = send_reports(repo, production_id, draft_id, second)

    assert second.sent == []
    assert result.skipped_already_sent == 1


def test_again_re_sends(repo, draft) -> None:
    """For a genuine resend after a provider outage."""
    production_id, draft_id = draft
    _add_report(repo, draft_id, approved=True)
    repo.add_recipient(production_id, "props", "props@example.com")

    send_reports(repo, production_id, draft_id, _Transport())
    again = _Transport()
    send_reports(repo, production_id, draft_id, again, resend_sent=True)

    assert len(again.sent) == 1


# --- failures -------------------------------------------------------------


def test_a_bad_address_does_not_lose_the_others(repo, draft) -> None:
    production_id, draft_id = draft
    _add_report(repo, draft_id, approved=True)
    repo.add_recipient(production_id, "props", "good@example.com")
    repo.add_recipient(production_id, "props", "bad@example.com")
    transport = _Transport(fail_on="bad@example.com")

    result = send_reports(repo, production_id, draft_id, transport)

    assert [to for to, _ in transport.sent] == ["good@example.com"]
    assert len(result.failures) == 1
    assert result.reports_sent == 1


def test_a_failure_is_recorded(repo, draft) -> None:
    """A bounce and "this revision does not affect you" look identical to a
    department. The system has to be able to tell them apart."""
    production_id, draft_id = draft
    report_id = _add_report(repo, draft_id, approved=True)
    repo.add_recipient(production_id, "props", "bad@example.com")

    send_reports(repo, production_id, draft_id, _Transport(fail_on="bad@example.com"))

    [delivery] = repo.deliveries(report_id)
    assert delivery["status"] == "failed"
    assert "mailbox" in delivery["error"]


def test_a_report_where_every_address_failed_is_not_marked_sent(repo, draft) -> None:
    """Marked sent, it would never be retried."""
    production_id, draft_id = draft
    _add_report(repo, draft_id, approved=True)
    repo.add_recipient(production_id, "props", "bad@example.com")

    result = send_reports(
        repo, production_id, draft_id, _Transport(fail_on="bad@example.com")
    )

    assert result.reports_sent == 0
    [report] = repo.reports_for_draft(draft_id)
    assert report["sent_at"] is None


def test_a_department_with_no_recipient_is_reported(repo, draft) -> None:
    """Silently skipping it would look like a successful send."""
    production_id, draft_id = draft
    _add_report(repo, draft_id, approved=True)

    result = send_reports(repo, production_id, draft_id, _Transport())

    assert result.skipped_no_recipient == ["props"]


def test_a_successful_delivery_is_recorded(repo, draft) -> None:
    production_id, draft_id = draft
    report_id = _add_report(repo, draft_id, approved=True)
    repo.add_recipient(production_id, "props", "props@example.com")

    send_reports(repo, production_id, draft_id, _Transport())

    [delivery] = repo.deliveries(report_id)
    assert delivery["status"] == "sent"
    assert delivery["provider_id"] == "id-1"


# --- recipients -----------------------------------------------------------


def test_recipients_are_per_production(repo) -> None:
    """The property master on one show is not the one on the next."""
    farm = repo.ensure_production("The Farm")
    other = repo.ensure_production("Other Show")
    repo.add_recipient(farm, "props", "a@example.com")
    repo.add_recipient(other, "props", "b@example.com")

    assert [r["email"] for r in repo.recipients(farm)] == ["a@example.com"]


def test_re_adding_an_address_updates_rather_than_duplicates(repo) -> None:
    """A corrected spelling must not double the send."""
    production_id = repo.ensure_production("The Farm")
    repo.add_recipient(production_id, "props", "a@example.com", "Jo")
    repo.add_recipient(production_id, "props", "a@example.com", "Jo Smith")

    rows = repo.recipients(production_id)
    assert len(rows) == 1
    assert rows[0]["name"] == "Jo Smith"


def test_a_recipient_can_be_removed(repo) -> None:
    production_id = repo.ensure_production("The Farm")
    repo.add_recipient(production_id, "props", "a@example.com")

    assert repo.remove_recipient(production_id, "a@example.com") == 1
    assert repo.recipients(production_id) == []


# --- the message ----------------------------------------------------------


def test_the_subject_says_whether_a_decision_is_needed() -> None:
    """A subject line is often the whole message."""
    notes = [Note("7", "a", urgent=True), Note("9", "b")]
    subject = subject_line("Props", "The Farm", notes)

    assert "Props" in subject
    assert "2 changes" in subject
    assert "1 needs a decision" in subject
    assert "The Farm" in subject


def test_the_subject_is_quieter_when_nothing_is_urgent() -> None:
    subject = subject_line("Props", "The Farm", [Note("7", "a")])

    assert "1 change" in subject
    assert "decision" not in subject


def test_urgent_notes_come_first() -> None:
    """Whatever order the model wrote them in."""
    email = render(
        "Props",
        "The Farm",
        "summary",
        [Note("3", "routine item"), Note("9", "urgent item", urgent=True)],
    )

    assert email.text.index("urgent item") < email.text.index("routine item")


def test_the_rest_sort_by_scene_the_way_a_supervisor_reads_them() -> None:
    email = render(
        "Props",
        "The Farm",
        "s",
        [Note("34", "thirty four"), Note("7", "seven"), Note("34A", "insert")],
    )
    body = email.text

    assert body.index("seven") < body.index("thirty four") < body.index("insert")


def test_the_html_escapes_the_content() -> None:
    """Scene text is not ours and lands inside markup."""
    email = render(
        "Props", "The Farm", "s", [Note("7", "a <script>alert(1)</script> prop")]
    )

    assert "<script>alert" not in email.html
    assert "&lt;script&gt;" in email.html


def test_the_html_loads_nothing_remote() -> None:
    """Mail clients block remote content by default.

    A link the reader chooses to follow is fine. What breaks a layout is content
    the client must fetch to render: images, stylesheets, web fonts. There is
    none, even when a review link is present.
    """
    email = render(
        "Props", "The Farm", "s", [Note("7", "a note")], review_url="https://x.test/1"
    )

    assert "<img" not in email.html
    assert "<link" not in email.html
    assert "@import" not in email.html
    assert "url(" not in email.html
    assert "background-image" not in email.html


def test_the_html_carries_no_style_block() -> None:
    """Gmail strips them, so anything that mattered would be lost.

    Every rule is inline for that reason, and a <style> block appearing here
    would mean a rule silently stopped applying in the client most people use.
    """
    email = render("Props", "The Farm", "s", [Note("7", "a note")])

    assert "<style" not in email.html


def test_a_review_link_is_included_when_given() -> None:
    email = render(
        "Props", "The Farm", "s", [Note("7", "a")], review_url="https://x.test/r/1"
    )

    assert "https://x.test/r/1" in email.html
    assert "https://x.test/r/1" in email.text


def test_the_action_appears_in_both_parts() -> None:
    """Plain text is what a spam filter scores and a screen reader may reach."""
    email = render(
        "Props", "The Farm", "s", [Note("7", "moved", action="Re-dress, do not buy")]
    )

    assert "Re-dress, do not buy" in email.html
    assert "Re-dress, do not buy" in email.text


def test_an_empty_report_still_renders() -> None:
    """Should be unreachable, but a broken send must not be a blank white box."""
    email = render("Props", "The Farm", "nothing this time", [])

    assert "No line items" in email.html


# --- transports -----------------------------------------------------------


def test_the_console_transport_is_the_default_without_a_key() -> None:
    """`approve` and `send` must work end to end with no email configured."""

    class S:
        resend_api_key = None
        resend_from = None

    assert isinstance(build_transport(S()), ConsoleTransport)


def test_resend_is_used_when_configured() -> None:
    class S:
        resend_api_key = "re_test"
        resend_from = "a@b.test"

    assert isinstance(build_transport(S()), ResendTransport)


def test_resend_refuses_to_construct_without_a_sender() -> None:
    with pytest.raises(SendError, match="sender"):
        ResendTransport("re_test", "")


def test_a_dry_run_records_nothing(repo, draft) -> None:
    """Recorded, it would mark the reports sent and the real send would skip
    every one of them. Worse than useless."""
    production_id, draft_id = draft
    report_id = _add_report(repo, draft_id, approved=True)
    repo.add_recipient(production_id, "props", "props@example.com")
    transport = _Transport()

    result = send_reports(repo, production_id, draft_id, transport, record=False)

    assert result.reports_sent == 1, "it should still report what would go out"
    assert transport.sent, "the transport is still exercised"
    assert repo.deliveries(report_id) == []
    [report] = repo.reports_for_draft(draft_id)
    assert report["sent_at"] is None
