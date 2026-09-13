"""The department email (Layer 8).

Who reads this: a head of department, on a phone, at the end of a fourteen-hour
shooting day, deciding whether anything has to happen before call time tomorrow.
That reader shapes every choice here.

- The urgent count is in the subject line, because a subject line is often the
  entire message. "Props: 2 changes, 1 needs a decision" is actionable before
  the mail is even opened.
- Urgent notes sort first and are marked. Everything else is reference.
- The scene number is the first thing on every line: it is how a 1st AD and a
  department head address anything on a production.
- No images, no web fonts, no external anything. Mail clients block remote
  content by default and a layout that depends on it arrives broken.

Written as inline-styled tables because that is what mail clients actually
render. Gmail strips `<style>` blocks, Outlook renders through Word, and flexbox
is not available in either. This is not the way one writes a web page and it is
the way one writes an email.

The PRD names React Email for templates. Four transactional emails did not
justify a Node toolchain in a Python project, and the tradeoff is recorded in
DECISIONS.md. Should the templates grow, the swap touches this file only.
"""

from __future__ import annotations

import html
from dataclasses import dataclass
from typing import Any

# Blue, because the product is named for the first coloured revision after
# white. Dark enough to pass contrast on white in every client.
ACCENT = "#2f5fd0"   # revision blue, the product's one accent
URGENT = "#b0342a"
INK = "#16150f"      # warm near-black, never pure
MUTED = "#5b5851"
FAINT = "#8a877e"
RULE = "#ddd9d1"     # the hairline. the most-used non-text colour
WASH = "#f2f0ec"
PAPER = "#faf9f7"    # printed stock, not #fff


@dataclass(frozen=True)
class Note:
    """One line item, flattened out of the database row."""

    scene: str
    note: str
    action: str = ""
    urgent: bool = False


@dataclass(frozen=True)
class Email:
    """A rendered message, ready to hand to a provider."""

    subject: str
    html: str
    text: str


def subject_line(department: str, production: str, notes: list[Note]) -> str:
    """What the reader sees before deciding whether to open it.

    Says the department, the show, and whether anything needs a decision. A
    subject that says only "Script revision" makes the reader open eight mails
    to find the one that mattered.
    """
    urgent = sum(1 for n in notes if n.urgent)
    count = len(notes)
    what = "1 change" if count == 1 else f"{count} changes"
    if urgent:
        needs = "1 needs a decision" if urgent == 1 else f"{urgent} need a decision"
        return f"{department}: {what}, {needs} ({production})"
    return f"{department}: {what} ({production})"


def render(
    department: str,
    production: str,
    summary: str,
    notes: list[Note],
    draft_label: str = "",
    review_url: str = "",
) -> Email:
    """One department's email.

    `notes` arrives in report order and is re-sorted so urgent items lead. The
    order a model happened to write them in is not a priority.
    """
    ordered = sorted(notes, key=lambda n: (not n.urgent, _scene_key(n.scene)))
    return Email(
        subject=subject_line(department, production, ordered),
        html=_html(department, production, summary, ordered, draft_label, review_url),
        text=_text(department, production, summary, ordered, draft_label, review_url),
    )


def _scene_key(scene: str) -> tuple[int, str]:
    """Order scenes the way a script supervisor reads them: 3, 7, 34, 34A."""
    digits = "".join(c for c in scene if c.isdigit())
    return (int(digits) if digits else 10**9, scene)


def _html(
    department: str,
    production: str,
    summary: str,
    notes: list[Note],
    draft_label: str,
    review_url: str,
) -> str:
    e = html.escape
    urgent_count = sum(1 for n in notes if n.urgent)

    rows = "".join(_note_row(n) for n in notes) or _empty_row()
    banner = (
        f"""
        <tr><td style="padding:0 32px 20px 32px;">
          <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%"
                 style="background:#faf0ef;border:1px solid #e8c9c5;border-radius:2px;">
            <tr><td style="padding:12px 16px;font:600 14px/1.45 -apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;color:{URGENT};">
              {urgent_count} item{"" if urgent_count == 1 else "s"} need{"s" if urgent_count == 1 else ""} a decision before this shoots.
            </td></tr>
          </table>
        </td></tr>"""
        if urgent_count
        else ""
    )

    review = (
        f"""
        <tr><td style="padding:4px 32px 32px 32px;">
          <a href="{e(review_url)}"
             style="display:inline-block;padding:11px 20px;background:{ACCENT};color:#ffffff;
                    text-decoration:none;border-radius:3px;
                    font:600 14px/1 -apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;">
            Open the full revision
          </a>
        </td></tr>"""
        if review_url
        else ""
    )

    draft = f" &middot; {e(draft_label)}" if draft_label else ""

    # Table layout and inline styles throughout: Gmail strips <style> blocks and
    # Outlook renders through Word, which has no flexbox and no grid.
    return f"""<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{e(department)}: script revision</title></head>
<body style="margin:0;padding:0;background:{WASH};">
<div style="display:none;max-height:0;overflow:hidden;opacity:0;">
  {e(summary[:140])}
</div>
<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%"
       style="background:{WASH};padding:24px 12px;">
<tr><td align="center">
  <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="600"
         style="max-width:600px;width:100%;background:{PAPER};border:1px solid {RULE};border-radius:3px;">

    <tr><td style="padding:26px 32px 2px 32px;">
      <div style="font:600 20px/1.3 -apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;color:{INK};">
        {e(department)}
      </div>
      <div style="padding-top:5px;font:400 13px/1.45 -apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;color:{MUTED};">
        {e(production)}{draft}
      </div>
    </td></tr>

    <tr><td style="padding:18px 32px 20px 32px;
                   font:400 15px/1.6 -apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;color:{INK};">
      {e(summary)}
    </td></tr>
{banner}
    <tr><td style="padding:0 32px;">
      <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%">
        {rows}
      </table>
    </td></tr>

    <tr><td style="padding:24px 32px 0 32px;"></td></tr>
{review}
    <tr><td style="padding:18px 32px 26px 32px;border-top:1px solid {RULE};
                   font:400 12px/1.55 -apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;color:{MUTED};">
      Sent by Bluepages after the 1st AD approved this revision.
      Only changes that affect {e(department)} are listed.
    </td></tr>

  </table>
</td></tr>
</table>
</body></html>"""


def _note_row(note: Note) -> str:
    e = html.escape
    mark = (
        f'<span style="display:inline-block;margin-left:8px;padding:1px 7px;'
        f'background:#fef2f2;color:{URGENT};border-radius:10px;'
        f"font:600 11px/1.7 -apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;"
        f'vertical-align:2px;">before it shoots</span>'
        if note.urgent
        else ""
    )
    action = (
        f"""
        <div style="padding-top:7px;font:400 14px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;color:{ACCENT};">
          &rarr; {e(note.action)}
        </div>"""
        if note.action
        else ""
    )
    scene = e(note.scene) if note.scene else "--"
    return f"""
    <tr><td style="padding:0 0 4px 0;">
      <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%"
             style="border-top:1px solid {RULE};">
        <tr>
          <td width="62" valign="top"
              style="padding:15px 12px 15px 0;
                     font:500 13px/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;color:{FAINT};white-space:nowrap;">
            Sc. {scene}
          </td>
          <td valign="top" style="padding:15px 0;">
            <div style="font:400 15px/1.55 -apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;color:{INK};">
              {e(note.note)}{mark}
            </div>{action}
          </td>
        </tr>
      </table>
    </td></tr>"""


def _empty_row() -> str:
    """Should not be reachable: a department with nothing is not sent to.

    Present so a malformed report degrades to a readable message rather than an
    empty white box that looks like a broken send.
    """
    return f"""
    <tr><td style="padding:16px 0;border-top:1px solid {RULE};
                   font:400 15px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;color:{MUTED};">
      No line items on this revision.
    </td></tr>"""


def _text(
    department: str,
    production: str,
    summary: str,
    notes: list[Note],
    draft_label: str,
    review_url: str,
) -> str:
    """The plain-text part.

    Not a fallback nobody sees: it is what a spam filter scores, what a screen
    reader may reach for, and what a phone on a bad signal renders.
    """
    lines = [
        f"{department.upper()} - {production}",
        f"Script revision{f' ({draft_label})' if draft_label else ''}",
        "",
        summary,
        "",
    ]
    for note in notes:
        flag = "  [BEFORE IT SHOOTS]" if note.urgent else ""
        lines.append(f"Sc. {note.scene or '--'}  {note.note}{flag}")
        if note.action:
            lines.append(f"    -> {note.action}")
        lines.append("")

    if review_url:
        lines += [f"Open the full revision: {review_url}", ""]
    lines.append(
        f"Sent by Bluepages after the 1st AD approved this revision. "
        f"Only changes that affect {department} are listed."
    )
    return "\n".join(lines)


def notes_from_rows(rows: list[dict[str, Any]]) -> list[Note]:
    """Database rows to notes. SQLite has no booleans, so urgent arrives as 0/1."""
    return [
        Note(
            scene=str(row.get("scene_number") or ""),
            note=str(row.get("note") or ""),
            action=str(row.get("action") or ""),
            urgent=bool(row.get("urgent")),
        )
        for row in rows
    ]


__all__ = ["Email", "Note", "notes_from_rows", "render", "subject_line"]
