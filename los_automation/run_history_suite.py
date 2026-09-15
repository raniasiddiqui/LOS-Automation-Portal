"""
The History suite: both sub-tabs, one report.

    python -m los_automation.run_history_suite
    python -m los_automation.run_history_suite --headed --show

Two checks, in one browser session on one case:

    1. Workflow Log      click 'View Changes' on the first row and assert the
                         screen ACTUALLY responded — a click that raised no
                         error is not a pass.
    2. Requests History  click a grid ROW (not the 'i' icon) and assert it
                         navigates to that transaction, logging whatever
                         screen it lands on.

READ-ONLY. Nothing on this screen is typed into, so --dry-run would do
exactly the same thing and is not offered.

WHAT THE ROW CLICK ACTUALLY DOES, measured rather than assumed: it leaves
History and lands on the clicked transaction's Credit Approval Memo
(/master/ca-package/customeroverview), switching transaction in the process —
on the pinned case it went from 52224-2026 to 52223-2026. The flow returns to
the original case afterwards, and History runs last in the screen order for
the same reason.

The login retry is shared with run_ecib_suite — see the long note there about
the ngx-ui-loader overlay.
"""
from __future__ import annotations

import argparse
import sys

import config as crawler_config

from . import settings
from .run_ecib_suite import LOGIN_HINT, _looks_like_login_flake
from .runner import case_flows as cf
from .runner import results as R

_MARK = {R.PASS: "PASS", R.FAIL: "FAIL", R.BLOCKED: "BLOCK"}

HISTORY_SCREEN = cf.SCREEN_LABEL[cf.HISTORY]

# The two lines the brief asks for, and which checks belong to each. Matched
# on a fragment of the check name, so a reworded check does not silently fall
# out of the summary and into "other".
FLOWS = [
    ("Workflow Log - View Changes",
     ("workflow log", "view changes")),
    ("Requests History - Row Click Redirect",
     ("requests history", "row")),
]


def _verdict(checks: list) -> str:
    if any(c.status == R.FAIL for c in checks):
        return R.FAIL
    if not any(c.status == R.PASS for c in checks):
        return R.BLOCKED
    return R.PASS


def _belongs(check, fragments) -> bool:
    name = (check.name or "").lower()
    return any(f in name for f in fragments)


def _print_detail(checks: list, show_passes: bool) -> None:
    shown = [c for c in checks if show_passes or c.status != R.PASS]
    if not shown:
        print("      (every check passed)")
        return
    for c in sorted(shown, key=lambda c: (R.ORDER.get(c.status, 9), c.name)):
        print(f"      {_MARK.get(c.status, '?'):<6} {c.name}")
        for field, tag in (("expected", "expected"), ("actual", "actual  ")):
            val = getattr(c, field, "")
            if val:
                print(f"               {tag}: {val[:150]}")
        if c.detail:
            print(f"               note    : {c.detail[:200]}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="run-history-suite",
        description="Check the History screen's Workflow Log and Requests "
                    "History tabs, and print one combined report.")
    ap.add_argument("--case-id", default=settings.CASE_ID,
                    help=f"The case to work in (default {settings.CASE_ID}).")
    ap.add_argument("--headed", action="store_true", help="Show the browser.")
    ap.add_argument("--show", action="store_true",
                    help="Include passing checks in the detail.")
    ap.add_argument("--attempts", type=int, default=3,
                    help="Retries for a run that cannot get past the login "
                         "overlay (default 3).")
    args = ap.parse_args(argv)

    print(f"Target : {crawler_config.BASE_URL}")
    print(f"Case   : {args.case_id}")
    print(f"Screen : {HISTORY_SCREEN}  (read-only — nothing is written)\n")

    result = None
    for attempt in range(1, max(1, args.attempts) + 1):
        result = cf.fill_case_screens(
            screens=[cf.HISTORY],
            case_id=args.case_id,
            headless=False if args.headed else None,
            # Read-only either way; False so the flow takes its ordinary path
            # rather than the dry-run branches the other screens use.
            dry_run=False,
            verify=False,
            progress=lambda m: print(f"  {m}"))
        if not _looks_like_login_flake(result):
            break
        print(f"\n  !! attempt {attempt} never got past the login overlay — "
              f"retrying\n")

    if result is None:
        print("Nothing ran.")
        return 2

    mine = [c for c in result.checks if c.screen == HISTORY_SCREEN]
    other = [c for c in result.checks if c.screen != HISTORY_SCREEN]

    print(f"\n{'=' * 78}")
    print("HISTORY — COMBINED REPORT")
    print(f"{'=' * 78}")
    print(f"run       : {result.run_id}")
    print(f"case      : {result.case_id}")
    print(f"artifacts : {result.artifacts_dir}")

    print(f"\n{'-' * 78}")
    print("SUMMARY")
    print(f"{'-' * 78}")
    verdicts, claimed = [], []
    for title, fragments in FLOWS:
        checks = [c for c in mine if _belongs(c, fragments)]
        claimed.extend(checks)
        v = _verdict(checks)
        verdicts.append(v)
        counts = {k: sum(1 for c in checks if c.status == k)
                  for k in (R.PASS, R.FAIL, R.BLOCKED)}
        print(f"  {title:<40} {_MARK[v]:<6} "
              f"({counts[R.PASS]} passed / {counts[R.FAIL]} failed / "
              f"{counts[R.BLOCKED]} blocked)")

    # Anything on this screen that neither line claimed. Printed rather than
    # dropped: a check nobody counted is how a failure goes missing.
    spare = [c for c in mine if c not in claimed]
    if spare:
        v = _verdict(spare)
        verdicts.append(v)
        print(f"  {'History - other checks':<40} {_MARK[v]:<6} "
              f"({len(spare)} check(s))")
    if other:
        verdicts.append(_verdict(other))
        print(f"  {'Case-level':<40} {_MARK[_verdict(other)]:<6} "
              f"({len(other)} check(s))")

    print(f"\n{'-' * 78}")
    print("DETAIL")
    print(f"{'-' * 78}")
    for title, fragments in FLOWS:
        checks = [c for c in mine if _belongs(c, fragments)]
        print(f"\n  {title}  [{_MARK[_verdict(checks)]}]")
        if not checks:
            print("      (nothing was checked — the tab did not run)")
            continue
        _print_detail(checks, args.show)
    if spare:
        print(f"\n  History - other checks  [{_MARK[_verdict(spare)]}]")
        _print_detail(spare, args.show)
    if other:
        print(f"\n  Case-level  [{_MARK[_verdict(other)]}]")
        _print_detail(other, args.show)

    overall = R.FAIL if R.FAIL in verdicts else (
        R.PASS if R.PASS in verdicts else R.BLOCKED)
    print(f"\n{'=' * 78}")
    print(f"ALL CHECKS: {_MARK[overall]}")
    print(f"{'=' * 78}")
    if result.blocked_reason:
        print(f"\nBlocked: {result.blocked_reason}")
        if _looks_like_login_flake(result):
            print(f"\n{LOGIN_HINT}")
    if not args.show:
        print("\n  (passing checks hidden — re-run with --show)")

    return {R.PASS: 0, R.FAIL: 1, R.BLOCKED: 2}[overall]


if __name__ == "__main__":
    sys.exit(main())
