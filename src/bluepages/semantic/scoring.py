"""Scoring semantic output against the answer key.

CLAUDE.md: semantic output without a ground truth to check against is not
verified. This is that check.

Three things are measured, and they are not equally important:

- **Recall**: did it find each labelled change. A miss is a department not told.
- **Kind**: did it reach the right judgment. Calling a relocated prop an
  addition is the expensive error the whole project exists to avoid, so a
  finding matched on scene but wrong on kind scores as a wrong answer, not a
  partial one.
- **Forbidden phrases**: each labelled change carries a `must_not_say` list, the
  specific wrong conclusions. These are scored separately and weighted hardest.
  A run can find every change, route it correctly, and still be wrong if it told
  props to buy a letter opener that already exists.

Precision is reported but held loosely. A finding the key does not list is not
automatically wrong: the key labels the eight judgment calls it was authored to
test, not every consequence of the revision. So extra findings are surfaced for
a human to read rather than counted as errors.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field

from bluepages.semantic.reasoning import Finding, SemanticResult
from bluepages.testdata import AnswerKey, ChangeKind, Department, LabelledChange


class ChangeScore(BaseModel):
    """How one labelled change was answered."""

    change_id: str
    expected_kind: ChangeKind
    found: bool = False
    kind_correct: bool = False
    found_kind: ChangeKind | None = None
    # Departments the key wants that the finding routed to, and the ones missed.
    departments_hit: list[Department] = Field(default_factory=list)
    departments_missed: list[Department] = Field(default_factory=list)
    departments_extra: list[Department] = Field(default_factory=list)
    # Forbidden phrases that appeared. The expensive errors.
    said_forbidden: list[str] = Field(default_factory=list)
    finding_summary: str = ""
    confidence: float = 0.0

    @property
    def correct(self) -> bool:
        """Found, judged correctly, and said nothing it must not say."""
        return self.found and self.kind_correct and not self.said_forbidden

    @property
    def department_recall(self) -> float:
        wanted = len(self.departments_hit) + len(self.departments_missed)
        return len(self.departments_hit) / wanted if wanted else 1.0


class Scorecard(BaseModel):
    """The whole run, scored."""

    pair: str
    scores: list[ChangeScore] = Field(default_factory=list)
    # Findings that matched no labelled change. Reported, not penalised: the key
    # labels the judgment calls it tests, not every consequence of a revision.
    unmatched_findings: list[str] = Field(default_factory=list)
    # Findings placed in scenes the key lists as unchanged. These are real
    # errors: the key asserts those scenes are identical.
    findings_in_unchanged_scenes: list[str] = Field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.scores)

    @property
    def found(self) -> int:
        return sum(1 for s in self.scores if s.found)

    @property
    def kind_correct(self) -> int:
        return sum(1 for s in self.scores if s.kind_correct)

    @property
    def fully_correct(self) -> int:
        return sum(1 for s in self.scores if s.correct)

    @property
    def forbidden_said(self) -> int:
        return sum(len(s.said_forbidden) for s in self.scores)

    @property
    def recall(self) -> float:
        return self.found / self.total if self.total else 0.0

    @property
    def accuracy(self) -> float:
        """The headline number: found, judged right, and said nothing forbidden."""
        return self.fully_correct / self.total if self.total else 0.0

    @property
    def department_recall(self) -> float:
        """Across every labelled change, how many wanted routes were made."""
        hit = sum(len(s.departments_hit) for s in self.scores)
        wanted = hit + sum(len(s.departments_missed) for s in self.scores)
        return hit / wanted if wanted else 0.0

    @property
    def passed(self) -> bool:
        """Whether this run is good enough to call the layer working.

        Every labelled change found and judged correctly, nothing forbidden
        said, and no findings invented in scenes the key calls unchanged. The
        bar is deliberately all-or-nothing: eight changes is a small enough key
        that a partial pass is a failure with a nice number attached.
        """
        return (
            self.fully_correct == self.total
            and self.forbidden_said == 0
            and not self.findings_in_unchanged_scenes
        )

    def summary(self) -> dict[str, object]:
        return {
            "pair": self.pair,
            "changes": self.total,
            "found": self.found,
            "kind_correct": self.kind_correct,
            "fully_correct": self.fully_correct,
            "recall": round(self.recall, 3),
            "accuracy": round(self.accuracy, 3),
            "department_recall": round(self.department_recall, 3),
            "forbidden_phrases_said": self.forbidden_said,
            "unmatched_findings": len(self.unmatched_findings),
            "findings_in_unchanged_scenes": len(self.findings_in_unchanged_scenes),
            "passed": self.passed,
        }


# A finding is a candidate for a labelled change when it touches the same
# scene. Kind is scored separately rather than used for matching, so that a
# wrong judgment about the right scene reads as wrong rather than as missing.
def _scenes_of(change: LabelledChange) -> set[str]:
    return {s for s in (change.from_scene, change.to_scene) if s}


def _finding_scenes(finding: Finding) -> set[str]:
    return {s for s in (finding.scene, finding.from_scene) if s}


def _finding_text(finding: Finding) -> str:
    """Everything the finding says, for phrase checking."""
    parts = [finding.summary, finding.reasoning, finding.element or ""]
    return " ".join(parts).lower()


def _says(text: str, phrase: str) -> bool:
    """Whether a forbidden phrase appears, tolerant of ordinary word variation.

    Matched on word boundaries so "new role" does not fire on "new roles" being
    ruled out, and stemmed loosely so "cut" catches "cuts". Deliberately not a
    substring test: "prop" as a substring matches "properly", and one of the
    forbidden phrases is exactly "prop".
    """
    words = [re.escape(w) for w in phrase.lower().split()]
    pattern = r"\b" + r"\W+".join(f"{w}(?:s|es|ed|d)?" for w in words) + r"\b"
    return re.search(pattern, text) is not None


def _matches_element(change: LabelledChange, finding: Finding) -> bool:
    """Whether the finding is about the element the change names.

    Only used to disambiguate when several findings land in one scene, which is
    the normal case: scene 7 carries a relocation, an addition and an action
    rewrite all at once.
    """
    if not change.element:
        return True
    term = change.element.lower()
    text = _finding_text(finding)
    if term in text:
        return True
    # A partial name still identifies the object: "letter opener" for "brass
    # letter opener". Requires a distinctive word, not just "the".
    words = [w for w in term.split() if len(w) > 3]
    return bool(words) and sum(1 for w in words if w in text) >= max(1, len(words) - 1)


def _pick_finding(change: LabelledChange, findings: list[Finding]) -> Finding | None:
    """The finding that best answers this labelled change.

    Ranked by how specifically it matches: right kind and right element first,
    then right kind, then right element, then merely the right scene.
    """
    scenes = _scenes_of(change)
    candidates = [f for f in findings if _finding_scenes(f) & scenes]
    if not candidates:
        return None

    def rank(finding: Finding) -> tuple[int, float]:
        kind_ok = finding.kind is change.kind
        element_ok = _matches_element(change, finding)
        score = (2 if kind_ok else 0) + (1 if element_ok else 0)
        return (score, finding.confidence)

    return max(candidates, key=rank)


def score(
    key: AnswerKey,
    result: SemanticResult,
    unchanged_scene_findings: bool = True,
) -> Scorecard:
    """Score a semantic run against the labelled ground truth."""
    card = Scorecard(pair=key.pair)
    claimed: set[int] = set()

    for change in key.changes:
        finding = _pick_finding(change, result.findings)
        entry = ChangeScore(change_id=change.id, expected_kind=change.kind)

        if finding is not None:
            claimed.add(id(finding))
            wanted = set(change.departments)
            got = set(finding.departments)
            text = _finding_text(finding)
            entry = ChangeScore(
                change_id=change.id,
                expected_kind=change.kind,
                found=True,
                kind_correct=finding.kind is change.kind,
                found_kind=finding.kind,
                departments_hit=sorted(wanted & got, key=lambda d: d.value),
                departments_missed=sorted(wanted - got, key=lambda d: d.value),
                departments_extra=sorted(got - wanted, key=lambda d: d.value),
                said_forbidden=[p for p in change.must_not_say if _says(text, p)],
                finding_summary=finding.summary,
                confidence=finding.confidence,
            )
        card.scores.append(entry)

    for finding in result.findings:
        if id(finding) not in claimed:
            card.unmatched_findings.append(
                f"scene {finding.scene} {finding.kind.value}: {finding.summary}"
            )

    if unchanged_scene_findings:
        unchanged = set(key.unchanged_scenes)
        for finding in result.findings:
            if _finding_scenes(finding) & unchanged:
                card.findings_in_unchanged_scenes.append(
                    f"scene {finding.scene}: {finding.summary}"
                )

    return card


__all__ = ["ChangeScore", "Scorecard", "score"]
