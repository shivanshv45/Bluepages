"""Sending the fan-out (Layer 8).

The rule this module exists to enforce: **only approved reports are sent, and
each one goes out once**. Everything else here is bookkeeping in service of it.

An unapproved report is skipped rather than refused, because a mixed batch is
normal: the AD approves a revision, a later run adds a department, and the send
should deliver what is approved without failing over what is not.

Delivery is recorded per address. A report that reached four of five heads is a
different state from one that reached nobody, and "we tried and it bounced" must
never look like "this revision did not affect you", which is what a department
sees when nothing arrives.

Resend over SES, per DECISIONS.md: the SES sandbox needs every recipient
verified individually, which is the kind of friction that gets a feature cut.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from bluepages.deliver.template import Note, notes_from_rows, render
from bluepages.events import EventKind, EventStream, NullStream


class SendError(RuntimeError):
    """The provider rejected a message."""


@dataclass
class Sent:
    """One delivery attempt."""

    department: str
    email: str
    ok: bool
    provider_id: str = ""
    error: str = ""


@dataclass
class SendResult:
    """What one send run did."""

    sent: list[Sent] = field(default_factory=list)
    reports_sent: int = 0
    skipped_unapproved: int = 0
    skipped_already_sent: int = 0
    skipped_no_recipient: list[str] = field(default_factory=list)

    @property
    def failures(self) -> list[Sent]:
        return [s for s in self.sent if not s.ok]

    def summary(self) -> dict[str, Any]:
        return {
            "delivered": sum(1 for s in self.sent if s.ok),
            "failed": len(self.failures),
            "reports_sent": self.reports_sent,
            "skipped_unapproved": self.skipped_unapproved,
            "skipped_already_sent": self.skipped_already_sent,
            "skipped_no_recipient": list(self.skipped_no_recipient),
        }


class Transport(Protocol):
    """Anything that can put a message on the wire."""

    def send(
        self, to: str, subject: str, html: str, text: str
    ) -> str: ...


class ConsoleTransport:
    """Prints instead of sending. The default when Resend is not configured.

    Not a mock for tests: it is how the whole Layer 8 path is demonstrated and
    debugged before an API key exists, and it keeps a misconfigured deploy from
    silently doing nothing.
    """

    def __init__(self, console: Any = None) -> None:
        if console is None:
            from rich.console import Console

            console = Console()
        self.console = console
        self.messages: list[tuple[str, str]] = []

    def send(self, to: str, subject: str, html: str, text: str) -> str:
        self.messages.append((to, subject))
        self.console.print(f"[dim]would send to[/dim] {to}")
        self.console.print(f"  [bold]{subject}[/bold]")
        return f"console-{len(self.messages)}"


class ResendTransport:
    """Resend's REST API, over stdlib http.

    Deliberately not the `resend` package: one POST does not justify a
    dependency, and the failure modes of a hand-rolled call here are ones we
    can read.
    """

    ENDPOINT = "https://api.resend.com/emails"

    def __init__(self, api_key: str, sender: str, timeout: float = 20.0) -> None:
        if not api_key:
            raise SendError("no Resend API key")
        if not sender:
            raise SendError("no sender address: set RESEND_FROM")
        self.api_key = api_key
        self.sender = sender
        self.timeout = timeout

    def send(self, to: str, subject: str, html: str, text: str) -> str:
        import json
        import urllib.error
        import urllib.request

        body = json.dumps({
            "from": self.sender,
            "to": [to],
            "subject": subject,
            "html": html,
            "text": text,
        }).encode("utf-8")

        request = urllib.request.Request(
            self.ENDPOINT,
            data=body,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                # Without a User-Agent, Cloudflare returns a bare 403 (error
                # 1010) before Resend ever sees the request.
                "User-Agent": "bluepages/1.0",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise SendError(f"Resend returned {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise SendError(f"could not reach Resend: {exc.reason}") from exc

        return str(payload.get("id", ""))


def build_transport(settings: Any, stream: EventStream | None = None) -> Transport:
    """Resend when configured, otherwise the console.

    Falling back to printing rather than raising is deliberate: `approve` should
    work end to end on a machine with no email set up, and a run that says
    exactly what it would have sent is more useful than one that refuses.
    """
    stream = stream or NullStream()
    if settings.resend_api_key and settings.resend_from:
        return ResendTransport(settings.resend_api_key, settings.resend_from)
    stream.emit(
        EventKind.INFO,
        "no Resend key configured: printing the fan-out instead of sending",
        transport="console",
    )
    return ConsoleTransport()


def send_reports(
    repo: Any,
    production_id: str,
    to_draft_id: str,
    transport: Transport,
    production: str = "",
    draft_label: str = "",
    review_url: str = "",
    stream: EventStream | None = None,
    resend_sent: bool = False,
    record: bool = True,
) -> SendResult:
    """Send every approved, unsent report on a draft.

    `resend_sent` re-sends reports already marked sent, which is for a genuine
    resend after a provider outage. It is off by default because the normal
    accident is running the command twice.

    `record=False` is for a dry run: it reports exactly what would go out and
    writes nothing. Recording a dry run would be worse than useless, because the
    real send afterwards would skip every report as already sent.
    """
    stream = stream or NullStream()
    result = SendResult()

    for report in repo.reports_for_draft(to_draft_id):
        department = str(report["department"])

        if report.get("approved_at") is None:
            # The gate. An unapproved report is skipped, never sent.
            result.skipped_unapproved += 1
            continue
        if report.get("sent_at") is not None and not resend_sent:
            result.skipped_already_sent += 1
            continue

        recipients = repo.recipients(production_id, department)
        if not recipients:
            result.skipped_no_recipient.append(department)
            stream.emit(
                EventKind.INFO,
                f"{department}: approved but nobody to send it to",
                department=department,
            )
            continue

        notes = notes_from_rows(repo.report_notes(str(report["id"])))
        email = render(
            department=department_title(department),
            production=production,
            summary=str(report.get("summary") or ""),
            notes=notes,
            draft_label=draft_label,
            review_url=review_url,
        )

        delivered_any = False
        for person in recipients:
            address = str(person["email"])
            try:
                provider_id = transport.send(
                    address, email.subject, email.html, email.text
                )
            except SendError as exc:
                # One bad address must not lose the other three.
                result.sent.append(
                    Sent(department, address, ok=False, error=str(exc))
                )
                if record:
                    repo.record_delivery(
                        str(report["id"]), address, "failed", error=str(exc)[:500]
                    )
                stream.emit(
                    EventKind.PARSE_WARNING,
                    f"{department}: could not send to {address}: {exc}",
                    department=department,
                    email=address,
                )
                continue

            delivered_any = True
            result.sent.append(
                Sent(department, address, ok=True, provider_id=provider_id)
            )
            if record:
                repo.record_delivery(
                    str(report["id"]), address, "sent", provider_id=provider_id
                )
            stream.emit(
                EventKind.AGENT_FINISHED,
                f"{department}: sent to {address}",
                department=department,
                email=address,
            )

        # Only when something actually landed. A report marked sent after every
        # address bounced would never be retried.
        if delivered_any:
            if record:
                repo.mark_sent(str(report["id"]))
            result.reports_sent += 1

    stream.emit(
        EventKind.INFO,
        f"{result.reports_sent} report(s) sent to "
        f"{sum(1 for s in result.sent if s.ok)} address(es)",
        **result.summary(),
    )
    return result


def department_title(department: str) -> str:
    """The department's display name, from the fan-out's own spec table."""
    from bluepages.agents import SPECS
    from bluepages.testdata import Department

    try:
        return SPECS[Department(department)].title
    except (KeyError, ValueError):
        return department.replace("_", " ").title()


__all__ = [
    "ConsoleTransport",
    "Note",
    "ResendTransport",
    "SendError",
    "SendResult",
    "Sent",
    "Transport",
    "build_transport",
    "department_title",
    "send_reports",
]
