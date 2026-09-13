"""Persisting a run, and resolving element identity across drafts.

Most of this module is unremarkable row writing. The part that matters is
`_resolve_identity`, which decides whether the brass letter opener in draft 2
scene 7 is the same object as the one in draft 1 scene 3.

The design decision: **identity is read from the semantic findings, not
re-derived here.** Layer 3.4 already made that judgment with the full text in
front of it, and a second, dumber matcher at the storage layer would sometimes
disagree with the finding it is storing. When the semantic layer says
`element_relocated`, that is an identity claim and this module records it.
Name matching is only the fallback for elements no finding mentions, which is
most of them: an unchanged prop in an unchanged scene needs no judgment.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from bluepages.diff import DraftDiff
from bluepages.model import Screenplay
from bluepages.semantic.elements import DraftElements, ExtractedElement
from bluepages.semantic.reasoning import Finding, SemanticResult
from bluepages.store.db import Database, new_id, now
from bluepages.testdata import ChangeKind

# Change kinds that assert two scenes hold the same object. These are the
# findings that carry an identity claim.
_RELOCATION_KINDS = frozenset({ChangeKind.ELEMENT_RELOCATED, ChangeKind.SCENE_MOVED})


@dataclass
class PersistedRun:
    """What a persisted run produced, for the caller to report or query."""

    run_id: str
    production_id: str
    from_draft_id: str | None
    to_draft_id: str
    scenes: int = 0
    elements: int = 0
    changes: int = 0
    identities_created: int = 0
    identities_matched: int = 0

    def summary(self) -> dict[str, Any]:
        return {
            "run": self.run_id,
            "scenes": self.scenes,
            "elements": self.elements,
            "changes": self.changes,
            "identities_created": self.identities_created,
            "identities_matched": self.identities_matched,
        }


@dataclass
class _IdentityCache:
    """Identities for one production, keyed for the two ways we look them up."""

    by_key: dict[str, str] = field(default_factory=dict)      # category:name -> id
    by_name: dict[str, list[str]] = field(default_factory=dict)  # name -> ids
    created: int = 0
    matched: int = 0


class Repository:
    """Reads and writes the element database."""

    def __init__(self, db: Database) -> None:
        self.db = db

    # --- productions and drafts -------------------------------------------

    def ensure_production(self, title: str, production_id: str | None = None) -> str:
        """Find a production by title, or create it."""
        if production_id:
            row = self.db.one("SELECT id FROM production WHERE id = ?", (production_id,))
            if row:
                return str(row["id"])

        row = self.db.one("SELECT id FROM production WHERE title = ?", (title,))
        if row:
            return str(row["id"])

        pid = production_id or new_id()
        self.db.execute(
            "INSERT INTO production (id, title, created_at) VALUES (?, ?, ?)",
            (pid, title, now()),
        )
        return pid

    def save_draft(
        self,
        production_id: str,
        screenplay: Screenplay,
        revision: int | None = None,
        colour: str | None = None,
    ) -> str:
        """Persist a draft and every scene in it.

        `revision` defaults to the one this source file already occupies, and
        otherwise to one past the highest stored. Keying on the source path
        matters: re-running the pipeline on the same pair has to land on the
        same two revisions, or every run appends two more drafts and the
        production accumulates duplicates of the same script.
        """
        if revision is None and screenplay.source_path:
            same_file = self.db.one(
                "SELECT revision FROM draft WHERE production_id = ? AND source_path = ?",
                (production_id, screenplay.source_path),
            )
            if same_file is not None:
                revision = int(same_file["revision"])

        if revision is None:
            row = self.db.one(
                "SELECT MAX(revision) AS top FROM draft WHERE production_id = ?",
                (production_id,),
            )
            top = row["top"] if row and row["top"] is not None else 0
            revision = int(top) + 1

        existing = self.db.one(
            "SELECT id FROM draft WHERE production_id = ? AND revision = ?",
            (production_id, revision),
        )
        if existing:
            # Re-ingesting the same revision replaces it rather than duplicating.
            # Scenes cascade, so the old ones go with it.
            self.db.execute("DELETE FROM draft WHERE id = ?", (existing["id"],))

        draft_id = new_id()
        self.db.execute(
            "INSERT INTO draft (id, production_id, revision, colour, source_path, "
            "source_tier, scene_count, ingested_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                draft_id,
                production_id,
                revision,
                colour,
                screenplay.source_path,
                screenplay.source_tier.value,
                screenplay.scene_count,
                now(),
            ),
        )

        self.db.executemany(
            "INSERT INTO scene (id, draft_id, number, heading, int_ext, location, "
            "time_of_day, omitted, idx) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    new_id(),
                    draft_id,
                    str(scene.number) if scene.number else None,
                    scene.heading,
                    scene.int_ext.value,
                    scene.location,
                    scene.time_of_day.value,
                    1 if scene.omitted else 0,
                    scene.index,
                )
                for scene in screenplay.scenes
            ],
        )
        return draft_id

    def scene_ids(self, draft_id: str) -> dict[str, str]:
        """Scene number -> row id, for attaching elements."""
        rows = self.db.query(
            "SELECT id, number FROM scene WHERE draft_id = ? AND number IS NOT NULL",
            (draft_id,),
        )
        return {str(r["number"]): str(r["id"]) for r in rows}

    # --- elements and identity --------------------------------------------

    def _load_identities(self, production_id: str) -> _IdentityCache:
        """Every identity already known for this production."""
        cache = _IdentityCache()
        rows = self.db.query(
            "SELECT id, name, category FROM element_identity WHERE production_id = ?",
            (production_id,),
        )
        for row in rows:
            ident, name, category = str(row["id"]), str(row["name"]), str(row["category"])
            cache.by_key[f"{category}:{name.strip().lower()}"] = ident
            cache.by_name.setdefault(name.strip().lower(), []).append(ident)
        return cache

    def _resolve_identity(
        self,
        element: ExtractedElement,
        production_id: str,
        draft_id: str,
        cache: _IdentityCache,
        claimed: dict[str, str],
    ) -> str:
        """The identity this element belongs to, creating one if it is new.

        Order matters. A relocation claim from the semantic layer wins over name
        matching, because the semantic layer saw the text and this does not.
        """
        key = element.key
        # A finding already said this element is one that exists. Believe it.
        if key in claimed:
            cache.matched += 1
            return claimed[key]

        if key in cache.by_key:
            cache.matched += 1
            return cache.by_key[key]

        identity_id = new_id()
        self.db.execute(
            "INSERT INTO element_identity (id, production_id, name, category, "
            "department, branded, first_draft_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                identity_id,
                production_id,
                element.name,
                element.category.value,
                element.department,
                1 if element.branded else 0,
                draft_id,
                now(),
            ),
        )
        cache.by_key[key] = identity_id
        cache.by_name.setdefault(element.name.strip().lower(), []).append(identity_id)
        cache.created += 1
        return identity_id

    def save_elements(
        self,
        production_id: str,
        draft_id: str,
        elements: DraftElements,
        findings: list[Finding] | None = None,
    ) -> tuple[int, int, int]:
        """Persist a draft's elements, resolving each to an identity.

        Returns (rows written, identities created, identities matched).
        """
        cache = self._load_identities(production_id)
        scene_rows = self.scene_ids(draft_id)
        claimed = self._identity_claims(findings or [], cache)

        written = 0
        for extraction in elements.scenes:
            scene_id = scene_rows.get(extraction.scene_number)
            if scene_id is None:
                # An element for a scene that is not in this draft is a bug
                # upstream, not something to invent a row for.
                continue
            for element in extraction.elements:
                identity_id = self._resolve_identity(
                    element, production_id, draft_id, cache, claimed
                )
                self.db.execute(
                    "INSERT INTO element (id, scene_id, identity_id, name, category, "
                    "department, quote, branded, notes) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        new_id(),
                        scene_id,
                        identity_id,
                        element.name,
                        element.category.value,
                        element.department,
                        element.quote,
                        1 if element.branded else 0,
                        element.notes,
                    ),
                )
                written += 1

        return written, cache.created, cache.matched

    def _identity_claims(
        self, findings: list[Finding], cache: _IdentityCache
    ) -> dict[str, str]:
        """Element keys the semantic layer says are an existing object.

        A relocation finding names an element that already exists somewhere in
        the production. Mapping it onto the known identity is what stops the
        letter opener becoming a second row in the inventory when it moves.
        """
        claims: dict[str, str] = {}
        for finding in findings:
            if finding.kind not in _RELOCATION_KINDS or not finding.element:
                continue
            name = finding.element.strip().lower()
            candidates = cache.by_name.get(name)
            if not candidates:
                # Try the other direction: the finding may name the object more
                # fully or less fully than the extractor did.
                candidates = [
                    ident
                    for known, idents in cache.by_name.items()
                    if (known in name or name in known) and len(known) > 3
                    for ident in idents
                ]
            if len(candidates) == 1:
                # Only when unambiguous. Two candidates means the claim does not
                # identify one object, and guessing would merge two props.
                for key, ident in cache.by_key.items():
                    if ident == candidates[0]:
                        claims[key] = ident
        return claims

    # --- changes -----------------------------------------------------------

    def save_changes(
        self,
        production_id: str,
        from_draft_id: str | None,
        to_draft_id: str,
        result: SemanticResult,
    ) -> int:
        """Persist the semantic findings and their department routing."""
        cache = self._load_identities(production_id)
        written = 0

        for finding in result.findings:
            change_id = new_id()
            identity_id = None
            if finding.element:
                name = finding.element.strip().lower()
                candidates = cache.by_name.get(name, [])
                if len(candidates) == 1:
                    identity_id = candidates[0]

            self.db.execute(
                "INSERT INTO change (id, production_id, from_draft_id, to_draft_id, "
                "kind, summary, reasoning, scene_number, from_scene, identity_id, "
                "confidence, risk, model_name, via_fallback, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    change_id,
                    production_id,
                    from_draft_id,
                    to_draft_id,
                    finding.kind.value,
                    finding.summary,
                    finding.reasoning,
                    finding.scene,
                    finding.from_scene,
                    identity_id,
                    finding.confidence,
                    finding.risk,
                    next(iter(result.models_used), None),
                    1 if result.fallbacks else 0,
                    now(),
                ),
            )
            self.db.executemany(
                "INSERT INTO change_department (change_id, department) VALUES (?, ?)",
                [(change_id, d.value) for d in finding.departments],
            )
            written += 1
        return written

    # --- department reports (Layer 5) --------------------------------------

    def save_reports(self, to_draft_id: str, fan: Any) -> int:
        """Persist the department fan-out for one draft.

        Replaces any existing report for the same department and draft, so
        re-running the fan-out after a prompt change does not leave two reports
        a department could be sent.
        """
        written = 0
        for report in fan.reports:
            self.db.execute(
                "DELETE FROM report WHERE to_draft_id = ? AND department = ?",
                (to_draft_id, report.department.value),
            )
            report_id = new_id()
            self.db.execute(
                "INSERT INTO report (id, to_draft_id, department, summary, "
                "model_name, via_fallback, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    report_id,
                    to_draft_id,
                    report.department.value,
                    report.summary,
                    report.model_name,
                    1 if report.via_fallback else 0,
                    now(),
                ),
            )
            self.db.executemany(
                "INSERT INTO report_note (id, report_id, scene_number, note, "
                "action, urgent, idx) VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        new_id(),
                        report_id,
                        note.scene,
                        note.note,
                        note.action,
                        1 if note.urgent else 0,
                        index,
                    )
                    for index, note in enumerate(report.notes)
                ],
            )
            written += 1
        return written

    def reports_for_draft(self, to_draft_id: str) -> list[dict[str, Any]]:
        """Every department report on this draft, with its notes."""
        reports = self.db.query(
            "SELECT id, department, summary, model_name, via_fallback, "
            "approved_at, sent_at FROM report WHERE to_draft_id = ? "
            "ORDER BY department",
            (to_draft_id,),
        )
        for report in reports:
            report["notes"] = self.db.query(
                "SELECT scene_number, note, action, urgent FROM report_note "
                "WHERE report_id = ? ORDER BY idx",
                (str(report["id"]),),
            )
        return reports

    def approve_reports(self, to_draft_id: str) -> int:
        """The AD's approval. Nothing is sent before this (PRD section 5).

        Marks every report on the draft, because the AD approves the fan-out as
        a whole rather than department by department.
        """
        cursor = self.db.execute(
            "UPDATE report SET approved_at = ? WHERE to_draft_id = ? AND approved_at IS NULL",
            (now(), to_draft_id),
        )
        return int(getattr(cursor, "rowcount", 0) or 0)

    def pending_reports(self, to_draft_id: str) -> list[dict[str, Any]]:
        """Reports approved but not yet sent. What Layer 8 picks up."""
        return self.db.query(
            "SELECT id, department, summary FROM report WHERE to_draft_id = ? "
            "AND approved_at IS NOT NULL AND sent_at IS NULL ORDER BY department",
            (to_draft_id,),
        )

    # --- runs --------------------------------------------------------------

    def start_run(
        self, production_id: str, from_draft_id: str | None, to_draft_id: str | None
    ) -> str:
        run_id = new_id()
        self.db.execute(
            "INSERT INTO run (id, production_id, from_draft_id, to_draft_id, status, "
            "started_at) VALUES (?, ?, ?, ?, ?, ?)",
            (run_id, production_id, from_draft_id, to_draft_id, "running", now()),
        )
        return run_id

    def finish_run(
        self,
        run_id: str,
        status: str = "ok",
        scenes: int = 0,
        findings: int = 0,
        budget: dict[str, Any] | None = None,
        fallbacks: int = 0,
        error: str | None = None,
    ) -> None:
        budget = budget or {}
        self.db.execute(
            "UPDATE run SET status = ?, scenes_parsed = ?, findings = ?, "
            "model_calls = ?, input_tokens = ?, output_tokens = ?, fallbacks = ?, "
            "finished_at = ?, error = ? WHERE id = ?",
            (
                status,
                scenes,
                findings,
                int(budget.get("calls", 0)),
                int(budget.get("input_tokens", 0)),
                int(budget.get("output_tokens", 0)),
                fallbacks,
                now(),
                error,
                run_id,
            ),
        )

    # --- the whole run -----------------------------------------------------

    def persist_run(
        self,
        title: str,
        before: Screenplay,
        after: Screenplay,
        diff: DraftDiff,
        result: SemanticResult | None = None,
        elements: DraftElements | None = None,
        budget: dict[str, Any] | None = None,
    ) -> PersistedRun:
        """Persist one complete pipeline run: both drafts, elements, findings.

        Ordered so identity resolution sees the findings: elements are written
        with the semantic claims already available, which is what lets a
        relocation reuse an existing identity rather than minting a new one.
        """
        production_id = self.ensure_production(title)
        from_draft_id = self.save_draft(production_id, before)
        to_draft_id = self.save_draft(production_id, after)
        run_id = self.start_run(production_id, from_draft_id, to_draft_id)

        persisted = PersistedRun(
            run_id=run_id,
            production_id=production_id,
            from_draft_id=from_draft_id,
            to_draft_id=to_draft_id,
            scenes=after.scene_count,
        )

        findings = result.findings if result else []
        if elements is not None:
            written, created, matched = self.save_elements(
                production_id, to_draft_id, elements, findings
            )
            persisted.elements = written
            persisted.identities_created = created
            persisted.identities_matched = matched

        if result is not None:
            persisted.changes = self.save_changes(
                production_id, from_draft_id, to_draft_id, result
            )

        self.finish_run(
            run_id,
            status="ok",
            scenes=after.scene_count,
            findings=persisted.changes,
            budget=budget,
            fallbacks=result.fallbacks if result else 0,
        )
        return persisted

    # --- queries -----------------------------------------------------------

    def element_history(self, production_id: str, name: str) -> list[dict[str, Any]]:
        """One element's trail across every draft.

        The query behind Layer 7.3's inventory screen: the letter opener showing
        3 -> 7 over four revisions. This is the whole reason identity exists.
        """
        return self.db.query(
            "SELECT d.revision, s.number AS scene, e.name, e.category, e.department "
            "FROM element e "
            "JOIN scene s ON s.id = e.scene_id "
            "JOIN draft d ON d.id = s.draft_id "
            "JOIN element_identity i ON i.id = e.identity_id "
            "WHERE i.production_id = ? AND LOWER(i.name) = LOWER(?) "
            "ORDER BY d.revision, s.number",
            (production_id, name),
        )

    def changes_for_department(
        self, to_draft_id: str, department: str
    ) -> list[dict[str, Any]]:
        """What one department needs to know about this draft. The fan-out query."""
        return self.db.query(
            "SELECT c.kind, c.summary, c.reasoning, c.scene_number, c.from_scene, "
            "c.confidence, c.risk FROM change c "
            "JOIN change_department cd ON cd.change_id = c.id "
            "WHERE c.to_draft_id = ? AND cd.department = ? "
            "ORDER BY c.scene_number",
            (to_draft_id, department),
        )

    def department_counts(self, to_draft_id: str) -> dict[str, int]:
        """Unread counts per department. The badges in Layer 7.1."""
        rows = self.db.query(
            "SELECT cd.department, COUNT(*) AS n FROM change c "
            "JOIN change_department cd ON cd.change_id = c.id "
            "WHERE c.to_draft_id = ? GROUP BY cd.department ORDER BY cd.department",
            (to_draft_id,),
        )
        return {str(r["department"]): int(r["n"]) for r in rows}

    def inventory(self, production_id: str) -> list[dict[str, Any]]:
        """Every distinct element the production knows about, and where it appears."""
        return self.db.query(
            "SELECT i.name, i.category, i.department, i.branded, "
            "COUNT(DISTINCT e.id) AS appearances "
            "FROM element_identity i "
            "LEFT JOIN element e ON e.identity_id = i.id "
            "WHERE i.production_id = ? "
            "GROUP BY i.id, i.name, i.category, i.department, i.branded "
            "ORDER BY i.department, i.name",
            (production_id,),
        )

    def clearance_flags(self, to_draft_id: str) -> list[dict[str, Any]]:
        """Branded elements in this draft. What the clearance consumer reads."""
        return self.db.query(
            "SELECT DISTINCT e.name, e.category, s.number AS scene, e.quote "
            "FROM element e JOIN scene s ON s.id = e.scene_id "
            "WHERE s.draft_id = ? AND e.branded = 1 "
            "ORDER BY s.number, e.name",
            (to_draft_id,),
        )

    def runs(self, production_id: str, limit: int = 20) -> list[dict[str, Any]]:
        """Recent runs. Layer 7.3's timeline, and what makes the app non-empty."""
        return self.db.query(
            "SELECT id, status, scenes_parsed, findings, model_calls, "
            "input_tokens, output_tokens, fallbacks, started_at, finished_at "
            "FROM run WHERE production_id = ? ORDER BY started_at DESC LIMIT ?",
            (production_id, limit),
        )

    def latest_draft(self, production_id: str) -> dict[str, Any] | None:
        return self.db.one(
            "SELECT id, revision, colour, scene_count, ingested_at FROM draft "
            "WHERE production_id = ? ORDER BY revision DESC LIMIT 1",
            (production_id,),
        )

    def scene_map(
        self, to_draft_id: str, from_draft_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Every scene in a draft with what the last run made of it.

        The live view is rebuilt from this when nothing is running, which is
        what stops the run screen being a blank page between revisions. The
        states match the ones the event stream emits, so the same component
        renders a finished run and a live one without knowing which it has.
        """
        scenes = self.db.query(
            "SELECT number, heading, omitted, idx FROM scene "
            "WHERE draft_id = ? ORDER BY idx",
            (to_draft_id,),
        )
        changed = {
            str(row["scene_number"])
            for row in self.db.query(
                "SELECT DISTINCT scene_number FROM change WHERE to_draft_id = ?",
                (to_draft_id,),
            )
            if row["scene_number"]
        }
        # A scene present in the new draft and absent from the old one was
        # inserted. Without the previous draft it is simply "changed", which is
        # true but less useful.
        before: set[str] = set()
        if from_draft_id:
            before = {
                str(row["number"])
                for row in self.db.query(
                    "SELECT number FROM scene WHERE draft_id = ?", (from_draft_id,)
                )
                if row["number"]
            }

        out: list[dict[str, Any]] = []
        for row in scenes:
            number = str(row["number"] or "")
            if not number:
                continue
            if row["omitted"]:
                state = "omitted"
            elif from_draft_id and number not in before:
                state = "inserted"
            elif number in changed:
                state = "changed"
            else:
                state = "seen"
            out.append({"number": number, "state": state, "heading": row["heading"]})
        return out

    def changes_for_draft(self, to_draft_id: str) -> list[dict[str, Any]]:
        """Every finding on a draft, with the departments it was routed to.

        This is the semantic layer's output as stored: what the diff meant, not
        what textually differed.
        """
        rows = self.db.query(
            "SELECT id, kind, summary, reasoning, scene_number, from_scene, "
            "confidence, risk FROM change WHERE to_draft_id = ? "
            "ORDER BY scene_number",
            (to_draft_id,),
        )
        for row in rows:
            row["departments"] = [
                str(d["department"])
                for d in self.db.query(
                    "SELECT department FROM change_department WHERE change_id = ?",
                    (str(row["id"]),),
                )
            ]
        return rows

    def draft_before(self, production_id: str, revision: int) -> dict[str, Any] | None:
        """The draft this one supersedes."""
        return self.db.one(
            "SELECT id, revision FROM draft WHERE production_id = ? AND revision < ? "
            "ORDER BY revision DESC LIMIT 1",
            (production_id, revision),
        )

    # --- recipients and delivery (Layer 8) ---------------------------------

    def add_recipient(
        self, production_id: str, department: str, email: str, name: str = ""
    ) -> str:
        """Add a department head to a production's distribution list.

        Re-adding the same address updates the name rather than creating a
        second row, so a corrected spelling does not double the send.
        """
        existing = self.db.one(
            "SELECT id FROM recipient WHERE production_id = ? AND department = ? "
            "AND email = ?",
            (production_id, department, email),
        )
        if existing is not None:
            self.db.execute(
                "UPDATE recipient SET name = ? WHERE id = ?",
                (name, str(existing["id"])),
            )
            return str(existing["id"])

        recipient_id = new_id()
        self.db.execute(
            "INSERT INTO recipient (id, production_id, department, name, email, "
            "added_at) VALUES (?, ?, ?, ?, ?, ?)",
            (recipient_id, production_id, department, name, email, now()),
        )
        return recipient_id

    def remove_recipient(self, production_id: str, email: str) -> int:
        cursor = self.db.execute(
            "DELETE FROM recipient WHERE production_id = ? AND email = ?",
            (production_id, email),
        )
        return int(getattr(cursor, "rowcount", 0) or 0)

    def recipients(
        self, production_id: str, department: str | None = None
    ) -> list[dict[str, Any]]:
        if department is None:
            return self.db.query(
                "SELECT id, department, name, email FROM recipient "
                "WHERE production_id = ? ORDER BY department, email",
                (production_id,),
            )
        return self.db.query(
            "SELECT id, department, name, email FROM recipient "
            "WHERE production_id = ? AND department = ? ORDER BY email",
            (production_id, department),
        )

    def record_delivery(
        self,
        report_id: str,
        email: str,
        status: str,
        provider_id: str | None = None,
        error: str | None = None,
    ) -> str:
        """One attempt to deliver one report to one address.

        Recorded whether it succeeded or not. A failed send that leaves no trace
        looks exactly like a revision that did not touch the department, and
        those two must never be confusable.
        """
        delivery_id = new_id()
        self.db.execute(
            "INSERT INTO delivery (id, report_id, email, status, provider_id, "
            "error, attempted_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (delivery_id, report_id, email, status, provider_id, error, now()),
        )
        return delivery_id

    def mark_sent(self, report_id: str) -> None:
        """Stamp a report as sent.

        Only after at least one delivery succeeded. A report marked sent when
        every address bounced would never be retried.
        """
        self.db.execute(
            "UPDATE report SET sent_at = ? WHERE id = ? AND sent_at IS NULL",
            (now(), report_id),
        )

    def deliveries(self, report_id: str) -> list[dict[str, Any]]:
        return self.db.query(
            "SELECT email, status, provider_id, error, attempted_at FROM delivery "
            "WHERE report_id = ? ORDER BY attempted_at",
            (report_id,),
        )

    def report_notes(self, report_id: str) -> list[dict[str, Any]]:
        return self.db.query(
            "SELECT scene_number, note, action, urgent FROM report_note "
            "WHERE report_id = ? ORDER BY idx",
            (report_id,),
        )


__all__ = ["PersistedRun", "Repository"]
