"""
Result types.

A check has two outcomes and only two:

  PASS  the app did what it should — the screen worked, or a value that was
        entered came back unchanged
  FAIL  the app did something different  -> a real finding, worth reporting

There is deliberately no third check status. "Could not check" used to be one,
and it grew to carry everything from a dry run to a field left blank on purpose
until a report could be a third unreadable amber and still mean nothing was
wrong. Anything that is not an assertion about the application is not a check:
it is either an OBSERVATION (see RunResult.notes) or, when the whole run could
not proceed, an ERROR.

ERROR is a RUN-level state, never a check status. Login timing out, an
unreachable environment or a host that is not approved for data entry are not
product defects, and showing them as FAIL is how a tool like this loses
credibility in its first week. A run that errors reports no verdict on the
application at all.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from typing import Any, Optional

PASS = "PASS"
FAIL = "FAIL"
# Run-level only. Never assign this to Check.status.
ERROR = "ERROR"

ORDER = {FAIL: 0, PASS: 1}


@dataclass
class Check:
    """One assertion, phrased for a human who has never seen the codebase."""
    name: str
    status: str
    expected: str = ""
    actual: str = ""
    detail: str = ""
    evidence: list[str] = field(default_factory=list)   # artifact file paths
    # Which sub-screen produced this, e.g. "Sector And Industry". A run now
    # walks several screens of one record, so results have to say where each
    # finding came from.
    screen: str = ""

    @property
    def ok(self) -> bool:
        return self.status == PASS

    def as_row(self) -> dict:
        return {
            "Screen": self.screen,
            "Check": self.name,
            "Result": self.status,
            "Expected": self.expected,
            "Actual": self.actual,
            "Notes": self.detail,
        }


def passed(name: str, detail: str = "", **kw) -> Check:
    return Check(name=name, status=PASS, detail=detail, **kw)


def failed(name: str, expected: str = "", actual: str = "", detail: str = "", **kw) -> Check:
    return Check(name=name, status=FAIL, expected=expected, actual=actual,
                 detail=detail, **kw)


@dataclass
class Note:
    """
    Something worth telling the operator that is NOT a verdict on the app.

    A field the screen does not have under the name this suite knows it by is
    the reason this exists: it may have been renamed, or this deployment may
    simply not configure it, and neither is a defect. The run carries on and
    says so afterwards rather than stopping — see flows.create_obligor.

    Notes are never counted, never sorted with checks and never change a run's
    outcome.
    """
    text: str
    screen: str = ""
    evidence: list[str] = field(default_factory=list)   # artifact file paths

    def as_row(self) -> dict:
        return {"Screen": self.screen, "Observation": self.text}


def note(text: str, screen: str = "", evidence: Optional[list] = None) -> Note:
    return Note(text=text, screen=screen, evidence=list(evidence or []))


def observation(subject: str, detail: str, evidence: Optional[list] = None,
                screen: str = "") -> Note:
    """
    An observation that reads like the check it replaces.

    Anything this suite cannot make an assertion about is not a check. A dry
    run, a tab deliberately left alone, a screen whose dialog offered nothing
    to associate — each of those used to be recorded as a third "could not
    check" status, and a report could be all amber while saying nothing was
    wrong. They are observations, and `subject` keeps the thing being observed
    at the front of the sentence so the note still reads as a statement about
    a named screen.
    """
    text = f"{subject}: {detail}" if detail else subject
    return Note(text=text, screen=screen, evidence=list(evidence or []))


@dataclass
class StepLog:
    """One navigation hop, so a failure says WHERE it stopped."""
    index: int
    kind: str
    label: str
    status: str
    note: str = ""


@dataclass
class RunResult:
    run_id: str
    target_key: str
    target_title: str
    mode: str
    base_url: str
    started_at: str
    finished_at: str = ""
    checks: list[Check] = field(default_factory=list)
    steps: list[StepLog] = field(default_factory=list)
    artifacts_dir: str = ""
    created_records: list[str] = field(default_factory=list)
    # Why the run could not proceed at all — an environment problem, not a
    # finding about the application. Set this and the run reports ERROR and no
    # verdict.
    error_reason: str = ""
    # Observations: worth reading, never a verdict. See Note.
    notes: list[Note] = field(default_factory=list)
    # The record this run verified. Pinned per run so two runs are comparable.
    case_id: str = ""

    # ---- summary -------------------------------------------------------
    @property
    def counts(self) -> dict[str, int]:
        out = {PASS: 0, FAIL: 0}
        for c in self.checks:
            out[c.status] = out.get(c.status, 0) + 1
        return out

    @property
    def overall(self) -> str:
        """
        PASS only if something was actually checked and nothing failed.

        A run that verified nothing is an ERROR, not a pass: there is no
        evidence either way, and the one thing this suite must never do is let
        silence read as success.
        """
        if self.error_reason:
            return ERROR
        counts = self.counts
        if counts[FAIL]:
            return FAIL
        if counts[PASS] == 0:
            return ERROR            # nothing verified is not a pass
        return PASS

    @property
    def headline(self) -> str:
        c = self.counts
        out = f"{c[PASS]} passed / {c[FAIL]} failed"
        if self.notes:
            out += f" / {len(self.notes)} observation(s)"
        return out

    def sorted_checks(self) -> list[Check]:
        """Failures first — an operator should not have to scroll to find them."""
        return sorted(self.checks, key=lambda c: (ORDER.get(c.status, 9), c.name))

    def by_screen(self) -> dict[str, list[Check]]:
        """Grouped in the order the screens were visited."""
        out: dict[str, list[Check]] = {}
        for c in self.checks:
            out.setdefault(c.screen or "(record)", []).append(c)
        for v in out.values():
            v.sort(key=lambda c: (ORDER.get(c.status, 9), c.name))
        return out

    def screen_summary(self) -> list[dict]:
        rows = []
        for screen, checks in self.by_screen().items():
            counts = {PASS: 0, FAIL: 0}
            for c in checks:
                counts[c.status] = counts.get(c.status, 0) + 1
            rows.append({"screen": screen, "passed": counts[PASS],
                         "failed": counts[FAIL],
                         "status": FAIL if counts[FAIL] else PASS})
        return rows

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["overall"] = self.overall
        d["headline"] = self.headline
        return d

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)
