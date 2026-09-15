"""
Result types.

The three-state outcome is the most important design decision in the whole
portal. A non-technical operator must never be shown FAIL for something that is
not a product defect — if login times out or the environment is unreachable, the
answer is BLOCKED. Collapsing those into FAIL is how a tool like this loses
credibility in its first week.

  PASS     the app did what it should — the screen worked, or a value that was
           entered came back unchanged
  FAIL     the app did something different  -> a real finding, worth reporting
  BLOCKED  we could not tell               -> environment or missing test data
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from typing import Any, Optional

PASS = "PASS"
FAIL = "FAIL"
BLOCKED = "BLOCKED"

ORDER = {FAIL: 0, BLOCKED: 1, PASS: 2}


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


def blocked(name: str, detail: str, **kw) -> Check:
    return Check(name=name, status=BLOCKED, detail=detail, **kw)


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
    blocked_reason: str = ""
    # The record this run verified. Pinned per run so two runs are comparable.
    case_id: str = ""

    # ---- summary -------------------------------------------------------
    @property
    def counts(self) -> dict[str, int]:
        out = {PASS: 0, FAIL: 0, BLOCKED: 0}
        for c in self.checks:
            out[c.status] = out.get(c.status, 0) + 1
        return out

    @property
    def overall(self) -> str:
        """A run is only PASS if something was actually checked and nothing failed."""
        if self.blocked_reason:
            return BLOCKED
        counts = self.counts
        if counts[FAIL]:
            return FAIL
        if counts[PASS] == 0:
            return BLOCKED          # nothing verified is not a pass
        return BLOCKED if counts[BLOCKED] and counts[PASS] == 0 else PASS

    @property
    def headline(self) -> str:
        c = self.counts
        return f"{c[PASS]} passed / {c[FAIL]} failed / {c[BLOCKED]} blocked"

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
            counts = {PASS: 0, FAIL: 0, BLOCKED: 0}
            for c in checks:
                counts[c.status] = counts.get(c.status, 0) + 1
            rows.append({"screen": screen, "passed": counts[PASS],
                         "failed": counts[FAIL], "blocked": counts[BLOCKED],
                         "status": FAIL if counts[FAIL] else
                                   (PASS if counts[PASS] else BLOCKED)})
        return rows

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["overall"] = self.overall
        d["headline"] = self.headline
        return d

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)
