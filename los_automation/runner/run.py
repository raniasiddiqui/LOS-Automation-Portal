"""
Run orchestration: one function the CLI and the Streamlit UI both call.

Keeping this separate from the CLI is what lets Phase 3 drive runs from a
background thread without duplicating any logic.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Callable, Optional

import config as crawler_config

from .. import settings
from . import checks as checks_mod
from . import results as R
from .driver import NavigationError, Session, new_run_id
from .targets import SubScreen, Target, get as get_target

Progress = Optional[Callable[[str], None]]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# Raw browser errors are meaningless to the people this portal is for.
# "net::ERR_NAME_NOT_RESOLVED" has to become something an operator can act on.
_ERROR_HINTS = [
    ("ERR_NAME_NOT_RESOLVED",
     "The application's address could not be found. Check you are on the office "
     "network or VPN, and that the environment address is correct."),
    ("ERR_CONNECTION_REFUSED",
     "The server refused the connection. The environment is probably down or "
     "restarting."),
    ("ERR_CONNECTION_TIMED_OUT",
     "The server did not respond in time. It may be down, or unreachable from "
     "this machine."),
    ("ERR_INTERNET_DISCONNECTED",
     "This machine has no network connection."),
    ("ERR_CERT_", "The site's security certificate was rejected by the browser."),
    ("ERR_PROXY", "The network proxy rejected the request."),
    ("Timeout", "The application took too long to respond. It may be very slow or "
                "partly down."),
]


def humanise(error: str) -> str:
    """Plain-language explanation, with the technical detail kept for whoever
    needs it."""
    text = str(error)
    for needle, hint in _ERROR_HINTS:
        if needle.lower() in text.lower():
            return f"{hint}\n\nTechnical detail: {text.splitlines()[0][:200]}"
    return text[:400]


def verify(target_key: str, headless: Optional[bool] = None,
           progress: Progress = None, emit=None,
           run_id: Optional[str] = None,
           case_id: Optional[str] = None) -> R.RunResult:
    """Read-only verification of one target. Never writes to the application."""
    from .targets import resolve as resolve_target

    case_id = case_id or settings.CASE_ID
    target = resolve_target(get_target(target_key), case_id)
    run_id = run_id or new_run_id("verify")
    _emit = emit or (lambda event: None)

    def say(msg: str) -> None:
        if progress:
            progress(msg)
        _emit({"kind": "log", "text": msg})

    result = R.RunResult(
        run_id=run_id, target_key=target.key, target_title=target.title,
        mode=settings.VERIFY, base_url=crawler_config.BASE_URL, started_at=_now(),
        artifacts_dir=os.path.join(settings.ARTIFACTS_DIR, run_id))
    result.case_id = case_id
    _emit({"kind": "start", "run_id": run_id, "target": target.key,
           "title": target.title, "path": target.path_description(),
           "case_id": case_id, "screens": target.screen_names(),
           "artifacts_dir": result.artifacts_dir})

    say(f"Signing in to {crawler_config.BASE_URL} ...")
    try:
        with Session(run_id, mode=settings.VERIFY, headless=headless,
                     emit=_emit) as s:
            try:
                s.login()
            except NavigationError as e:
                # Nothing about the application is known if we never got in.
                result.error_reason = str(e)
                result.steps = s.steps
                return _finish(result, _emit, say)

            say(f"Navigating: {target.path_description()}")
            try:
                s.navigate(target)
            except NavigationError as e:
                shot = s.screenshot(f"{target.key}-nav-failed")
                where = s.steps[-1] if s.steps else None
                name = f"Reach {target.title}"
                detail = f"Stopped at step {where.index} ({where.kind})." if where else ""
                if e.environmental:
                    result.error_reason = f"{e} {detail}".strip()
                else:
                    result.checks.append(R.failed(
                        name, expected="the screen can be reached by its menu path",
                        actual=str(e), detail=detail, evidence=[shot] if shot else []))
                result.steps = s.steps
                return _finish(result, _emit, say)

            # Walk every screen of the opened record, checking each against its
            # own section of the specification.
            _walk_screens(s, target, result, _emit, say)
            result.steps = s.steps

    except Exception as e:  # noqa: BLE001 - browser/environment failure, not a defect
        result.error_reason = humanise(e)

    return _finish(result, _emit, say)


# --------------------------------------------------------------------------
# Walking the screens of an opened record
# --------------------------------------------------------------------------

def _worst(checks: list[R.Check]) -> str:
    """The status a screen should be shown as: any failure dominates."""
    return R.FAIL if any(c.status == R.FAIL for c in checks) else R.PASS


def _walk_screens(s: Session, target: Target, result: R.RunResult,
                  emit, say) -> None:
    """
    Visit every screen the target declares, and the screens those reveal.

    Three shapes of screen, and they need different handling:

      a sidebar entry with tabs   Obligor Details (BIR) — a doorway; its tabs
                                  are the screens, so it is not checked itself.
      a form                      Request Details, Credit Memorandum — checked
                                  directly.
      a summary grid              Facilities, Collaterals, Documents — a record's
                                  data is in its DETAIL view, so the first row is
                                  opened and checked, and any tab strip it
                                  exposes is walked too.

    Progress numbering counts planned screens only; row-detail and tab passes
    are announced as extra screens, which is why the UI clamps its bar.
    """
    planned = target.screen_names()
    total = max(len(planned), 1)
    seq = {"n": 0}

    def announce_start(label: str) -> None:
        seq["n"] += 1
        emit({"kind": "screen_start", "index": seq["n"], "total": total,
              "screen": label})
        say(f"[{seq['n']}/{total}] {label} ...")

    def announce_done(label: str, checks: list[R.Check], note: str) -> None:
        emit({"kind": "screen_done", "index": seq["n"], "total": total,
              "screen": label, "status": _worst(checks), "note": note,
              "shot": next((e for c in checks for e in c.evidence if e), "")})

    def check_now(sub: SubScreen, note: str) -> list[R.Check]:
        # Cleared per screen so API failures and JS errors are attributed to
        # the screen that actually produced them.
        s.recorder.clear()
        s.console_errors.clear()
        checks = checks_mod.run_screen(s, target, sub)
        result.checks.extend(checks)
        announce_done(sub.name, checks, note)
        return checks

    def unreachable(sub: SubScreen, e: NavigationError) -> None:
        shot = s.screenshot(f"{target.key}-{sub.name[:24]}-missing")
        # An environmental stop — no test data in the grid, say — says nothing
        # about whether the screen exists, so it is written down rather than
        # asserted on. A screen the app simply does not offer is a finding.
        if e.environmental:
            result.notes.append(R.observation(
                f"{sub.name} could not be reached", str(e),
                evidence=[shot] if shot else [], screen=sub.name))
            status = R.ERROR
        else:
            chk = R.failed(f"{sub.name} can be opened",
                           expected=f"a '{sub.name}' screen exists",
                           actual=str(e), evidence=[shot] if shot else [])
            chk.screen = sub.name
            result.checks.append(chk)
            status = chk.status
        emit({"kind": "screen_done", "index": seq["n"], "total": total,
              "screen": sub.name, "status": status,
              "note": str(e)[:160], "shot": shot})

    def drill_into_record(sub: SubScreen) -> None:
        """Open the first row of this screen's grid and check what it reveals."""
        opened = s.open_row_detail()
        if not opened:
            say(f"    {sub.name}: no record to open.")
            return

        strip = s.tab_strip() if sub.walk_inner_tabs else []
        if not strip:
            detail = SubScreen(label=f"{sub.name} → record detail")
            announce_start(detail.name)
            check_now(detail, opened)
            s.leave_row_detail()
            return

        for tab in strip:
            label = f"{sub.name} → {tab['label']}"
            inner = SubScreen(label=label)
            announce_start(label)
            how = opened              # the first tab is the one the row opened on
            if not tab["active"]:
                try:
                    how = s.open_tab(tab["label"])
                except NavigationError as e:
                    unreachable(inner, e)
                    continue
            check_now(inner, how)

        s.leave_row_detail()

    def visit(sub: SubScreen) -> None:
        if sub.check_self:
            announce_start(sub.name)
        try:
            note = s.open_sub_screen(sub)
        except NavigationError as e:
            # A doorway that cannot be opened is still worth a line in the
            # report — otherwise its children vanish without explanation.
            if not sub.check_self:
                announce_start(sub.name)
            unreachable(sub, e)
            return

        if sub.check_self:
            check_now(sub, note)
        if sub.open_row_detail:
            drill_into_record(sub)
        for child in sub.children:
            visit(child)

    for sub in target.sub_screens:
        visit(sub)


def _finish(result: R.RunResult, emit, say) -> R.RunResult:
    """Single exit point, so every path persists the report and announces the
    outcome — a UI waiting on a 'done' event must never be left hanging."""
    result.finished_at = _now()
    path = _persist(result)
    say(f"Done — {result.headline}")
    emit({"kind": "done", "overall": result.overall, "headline": result.headline,
          "result_path": path})
    return result


def _persist(result: R.RunResult) -> str:
    """Write the report next to its screenshots so a run is self-contained."""
    os.makedirs(result.artifacts_dir, exist_ok=True)
    path = os.path.join(result.artifacts_dir, "result.json")
    with open(path, "w", encoding="utf-8") as f:
        f.write(result.to_json())
    return path
