"""Delivery (Layer 8).

The approved fan-out, in each department head's inbox. Only approved reports are
sent, each goes out once, and every attempt is recorded: a bounce and a revision
that did not affect you look identical from the outside, and they must not.
"""

from bluepages.deliver.send import (
    ConsoleTransport,
    ResendTransport,
    SendError,
    SendResult,
    Sent,
    Transport,
    build_transport,
    department_title,
    send_reports,
)
from bluepages.deliver.template import (
    Email,
    Note,
    notes_from_rows,
    render,
    subject_line,
)

__all__ = [
    "ConsoleTransport",
    "Email",
    "Note",
    "ResendTransport",
    "SendError",
    "SendResult",
    "Sent",
    "Transport",
    "build_transport",
    "department_title",
    "notes_from_rows",
    "render",
    "send_reports",
    "subject_line",
]
