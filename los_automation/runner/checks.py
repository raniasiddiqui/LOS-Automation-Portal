"""
Read-only assertions, phrased for an operator who has never seen the code.

These check that a screen WORKS: that it opens and renders something, that
nothing it requested from the server failed, that it shows no error, that the
record it holds is complete, and that the browser logged no unhandled error.

They deliberately do not check WHICH FIELDS a screen ought to have. The only
expectation this suite compares values against is the one it created itself —
what a Phase 2 flow just typed in, re-read through a fresh load of the record.
That comparison lives in flows.py and case_flows.py, and it is trustworthy
because both halves of it come from the same run.

Every check returns a Check rather than raising, so one broken expectation never
hides the rest of the report. Names read as sentences about the app, because they
are what a non-technical person sees in the results table.
"""
from __future__ import annotations

from . import results as R
from .driver import Session
from .targets import SubScreen, Target


def run_screen(session: Session, target: Target, sub: SubScreen) -> list[R.Check]:
    """The checks for ONE sub-screen of an opened record."""
    checks: list[R.Check] = []
    shot = session.screenshot(f"{target.key}-{_slug(sub.name)}")

    checks.append(_screen_reached(session, sub, shot))
    checks.append(_no_failed_api_calls(session, shot))
    checks.append(_no_error_message(session, shot))
    checks.append(_record_is_complete(session, shot))
    checks.append(_no_javascript_errors(session))

    for c in checks:
        c.screen = sub.name
    return checks


def _slug(text: str) -> str:
    return "".join(ch if ch.isalnum() else "-" for ch in text).strip("-")[:40]


# --------------------------------------------------------------------------

def _screen_reached(session: Session, sub: SubScreen, shot: str) -> R.Check:
    name = f"{sub.name} opens and shows its content"
    fields = session.read_fields()
    grids = session.read_grids()

    if fields:
        extra = f" and {len(grids)} table(s)" if grids else ""
        return R.passed(name, detail=f"{len(fields)} fields visible{extra}.",
                        evidence=[shot] if shot else [])
    if grids:
        # Not a form — a linkage table. Perfectly valid content.
        cols = sum(len(g["headers"]) for g in grids)
        rows = sum(g["rows"] for g in grids)
        return R.passed(
            name,
            detail=f"This screen presents data as {len(grids)} table(s): "
                   f"{cols} column(s), {rows} row(s). Its fields become editable "
                   f"only when a row is added.",
            evidence=[shot] if shot else [])
    return R.failed(
        name,
        expected="the screen shows fields or a table of data",
        actual="nothing was found on the screen",
        detail=("The screen opened but looks empty. It may still be loading, or "
                "its content may have failed to render."),
        evidence=[shot] if shot else [])


def _no_failed_api_calls(session: Session, shot: str) -> R.Check:
    name = "No server errors while loading this screen"
    bad = session.failed_api_calls()
    if bad:
        worst = "; ".join(f"{b['status']} {b['method']} "
                          f"{(b['url'] or '').split('/api/')[-1][:70]}" for b in bad[:4])
        return R.failed(
            name,
            expected="every request returns a success status",
            actual=f"{len(bad)} request(s) failed: {worst}",
            detail="A failing API call almost always means data is missing or "
                   "broken on this screen.",
            evidence=[shot] if shot else [])
    return R.passed(name, detail="All API calls returned a success status.")


def _no_error_message(session: Session, shot: str) -> R.Check:
    name = "No error message shown on this screen"
    banners = [b for b in session.error_banners() if b.strip()]
    if banners:
        return R.failed(
            name,
            expected="no error text visible",
            actual=" | ".join(banners[:3]),
            evidence=[shot] if shot else [])
    return R.passed(name)


def _record_is_complete(session: Session, shot: str) -> R.Check:
    """
    Inline validation on a saved record means mandatory data is missing from it.
    Reported separately from errors: nothing has malfunctioned, but the record
    could not be submitted as it stands, which is worth an operator knowing.
    """
    name = "This screen has no missing mandatory data"
    msgs = session.validation_messages()
    if msgs:
        return R.failed(
            name,
            expected="no mandatory field flagged as missing",
            actual=f"{len(msgs)} field(s) flagged: " + " | ".join(msgs[:4]),
            detail="The screen works — this is about the record itself, which is "
                   "incomplete. Expected for a draft; a concern for a completed one.",
            evidence=[shot] if shot else [])
    return R.passed(name)


def _no_javascript_errors(session: Session) -> R.Check:
    name = "Screen loads without application errors"
    if session.console_errors:
        return R.failed(
            name,
            expected="no unhandled errors in the browser",
            actual=f"{len(session.console_errors)} error(s): "
                   f"{session.console_errors[0][:160]}",
            detail="Unhandled front-end errors often leave part of the screen "
                   "unusable.")
    return R.passed(name)
