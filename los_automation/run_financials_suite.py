"""
The Financials suite: four sub-tabs, one report.

    python -m los_automation.run_financials_suite
    python -m los_automation.run_financials_suite --show
    LOS_FIN_YEARS=2010,2011 python -m los_automation.run_financials_suite

Five things, in one browser session on one case:

    1. Financials Input, statement 1   New Statement -> the eleven-field
                                       dialog -> Proceed -> fill the chart of
                                       accounts -> Save
    2. Financials Input, statement 2   the same, for a second year
    3. Financial Variance Analysis     Perform -> the VARIANCE COMMENTS column
    4. Financial Peer Analysis         Perform -> Add Peer -> Calculate ->
                                       comments -> Save
    5. Financial Analysis              every rich text editor -> Save

THE YEARS MATTER. A financial year the case already holds cannot be added
again, so LOS_FIN_YEARS must name years it does not have. A run that reuses
one reports the dialog refusing it rather than carrying on.

The login retry is shared with run_ecib_suite — see the note there about the
ngx-ui-loader overlay.
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


def _flows() -> list[tuple[str, str]]:
    """The report's lines, and the screen each one collects."""
    rows = [(f"Financials Input (Statement {i + 1}, FY{y})",
             f"{cf.FIN_INPUT_LABEL} (FY{y})")
            for i, y in enumerate(cf.FIN_YEARS)]
    rows += [
        ("Financial Variance Analysis", cf.FIN_VARIANCE_LABEL),
        ("Financial Peer Analysis", cf.FIN_PEER_LABEL),
        ("Financial Analysis (RTEs)", cf.FIN_ANALYSIS_LABEL),
    ]
    return rows


def _verdict(checks: list) -> str:
    if any(c.status == R.FAIL for c in checks):
        return R.FAIL
    if not any(c.status == R.PASS for c in checks):
        return R.BLOCKED
    return R.PASS


def _detail(checks: list, show_passes: bool) -> None:
    shown = [c for c in checks if show_passes or c.status != R.PASS]
    if not shown:
        print("      (every check passed)")
        return
    for c in sorted(shown, key=lambda c: (R.ORDER.get(c.status, 9), c.name)):
        print(f"      {_MARK.get(c.status, '?'):<6} {c.name}")
        for field, tag in (("expected", "expected"), ("actual", "actual  ")):
            v = getattr(c, field, "")
            if v:
                print(f"               {tag}: {v[:170]}")
        if c.detail:
            print(f"               note    : {c.detail[:230]}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="run-financials-suite",
        description="Fill and check the four Financials sub-tabs, and print "
                    "one combined report.")
    ap.add_argument("--case-id", default=settings.CASE_ID)
    ap.add_argument("--headed", action="store_true", help="Show the browser.")
    ap.add_argument("--show", action="store_true",
                    help="Include passing checks in the detail.")
    ap.add_argument("--attempts", type=int, default=3,
                    help="Retries for a run that cannot get past the login "
                         "overlay (default 3).")
    ap.add_argument("--no-verify", action="store_true",
                    help="Skip the round trip.")
    args = ap.parse_args(argv)

    print(f"Target : {crawler_config.BASE_URL}")
    print(f"Case   : {args.case_id}")
    print(f"Years  : {', '.join(cf.FIN_YEARS)}  (LOS_FIN_YEARS)")
    print(f"Caps   : {cf.FIN_MAX_CELLS} chart cell(s), "
          f"{cf.FIN_MAX_COMMENTS} comment(s) per grid\n")

    result = None
    for attempt in range(1, max(1, args.attempts) + 1):
        result = cf.fill_case_screens(
            screens=[cf.FINANCIALS], case_id=args.case_id,
            headless=False if args.headed else None,
            dry_run=False, verify=not args.no_verify,
            progress=lambda m: print(f"  {m}"))
        if not _looks_like_login_flake(result):
            break
        print(f"\n  !! attempt {attempt} never got past the login overlay — "
              f"retrying\n")

    if result is None:
        print("Nothing ran.")
        return 2

    flows = _flows()
    claimed = {screen for _t, screen in flows}

    print(f"\n{'=' * 78}")
    print("FINANCIALS — COMBINED REPORT")
    print(f"{'=' * 78}")
    print(f"run       : {result.run_id}")
    print(f"case      : {result.case_id}")
    print(f"artifacts : {result.artifacts_dir}")

    print(f"\n{'-' * 78}")
    print("SUMMARY")
    print(f"{'-' * 78}")
    verdicts = []
    for title, screen in flows:
        checks = [c for c in result.checks if c.screen == screen]
        v = _verdict(checks)
        verdicts.append(v)
        counts = {k: sum(1 for c in checks if c.status == k)
                  for k in (R.PASS, R.FAIL, R.BLOCKED)}
        print(f"  {title:<44} {_MARK[v]:<6} "
              f"({counts[R.PASS]} passed / {counts[R.FAIL]} failed / "
              f"{counts[R.BLOCKED]} blocked)")

    other = [c for c in result.checks if c.screen not in claimed]
    if other:
        v = _verdict(other)
        verdicts.append(v)
        print(f"  {'Case-level / round trip':<44} {_MARK[v]:<6} "
              f"({len(other)} check(s))")

    print(f"\n{'-' * 78}")
    print("DETAIL")
    print(f"{'-' * 78}")
    for title, screen in flows:
        checks = [c for c in result.checks if c.screen == screen]
        print(f"\n  {title}  [{_MARK[_verdict(checks)]}]")
        if not checks:
            print("      (nothing was checked — the sub-tab did not run)")
            continue
        _detail(checks, args.show)
    if other:
        print(f"\n  Case-level / round trip  [{_MARK[_verdict(other)]}]")
        _detail(other, args.show)

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
