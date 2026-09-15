"""
The PR Checklist suite: Perform PR, fill, Generate, Save, verify — one report.

    python -m los_automation.run_pr_suite
    python -m los_automation.run_pr_suite --dry-run
    python -m los_automation.run_pr_suite --headed --show

One test, five stages, on one case:

    1. Perform PR         open the checklist form from the log screen
    2. Fill               every 'Factor Value' the form offers; frozen ones
                          asserted frozen, never skipped quietly
    3. Generate -> Save   IN THAT ORDER, snapshotting the computed columns in
                          between
    4. Verify             a new PR RISK RATING LOG row, then re-open it
                          through the eye icon and compare every factor
                          against the snapshot; then Generate Pdf
    5. This report

WHY THIS SCRIPT EXISTS, given cli.py can already run the screen: cli.py prints
per-screen checks. What the brief asks for is a consolidated PASS/FAIL for the
flow with a per-factor table underneath. That is a reporting concern, so it
lives in a reporting script rather than being bolted onto the runner.

The login retry is shared with run_ecib_suite rather than copied — see the
long note there about the ngx-ui-loader overlay, which is the same overlay
that makes 'Perform PR' need a dispatch fallback in widgets.press_action.
"""
from __future__ import annotations

import argparse
import sys

import config as crawler_config

from . import settings
from .run_ecib_suite import LOGIN_HINT, _looks_like_login_flake
from .runner import case_flows as cf
from .runner import results as R
from .runner import widgets as W

_MARK = {R.PASS: "PASS", R.FAIL: "FAIL", R.BLOCKED: "BLOCK"}

PR_SCREEN = cf.SCREEN_LABEL[cf.PR_CHECKLIST]


def _verdict(checks: list) -> str:
    """PASS only if something was checked and nothing failed."""
    if any(c.status == R.FAIL for c in checks):
        return R.FAIL
    if not any(c.status == R.PASS for c in checks):
        return R.BLOCKED
    return R.PASS


def _wrap(text: str, width: int, indent: int) -> list[str]:
    """Fold a long regulatory sentence so the table stays readable."""
    words, lines, cur = (text or "").split(), [], ""
    for w in words:
        if len(cur) + len(w) + 1 > width:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    return lines or [""]


def _print_factor_table(checks: list, show_passes: bool) -> None:
    """
    The per-factor detail the brief asks for: factor, expected, actual, status.

    One row per check rather than one per factor, because a factor carries
    several checks — its value, and each of the three computed columns — and
    collapsing them would lose which of them disagreed.
    """
    shown = [c for c in checks if show_passes or c.status != R.PASS]
    if not shown:
        print("      (every check passed)")
        return

    print(f"      {'STATUS':<7} {'CHECK':<58} DETAIL")
    print(f"      {'-' * 7} {'-' * 58} {'-' * 40}")
    for c in sorted(shown, key=lambda c: (R.ORDER.get(c.status, 9), c.name)):
        name_lines = _wrap(c.name, 58, 0)
        print(f"      {_MARK.get(c.status, '?'):<7} {name_lines[0]:<58}")
        for extra in name_lines[1:]:
            print(f"      {'':<7} {extra:<58}")
        for field, tag in (("expected", "expected"), ("actual", "actual  ")):
            val = getattr(c, field, "")
            if not val:
                continue
            for i, line in enumerate(_wrap(val, 60, 0)):
                lead = f"{tag}: " if i == 0 else "          "
                print(f"      {'':<7}   {lead}{line}")
        if c.detail and c.status != R.PASS:
            for i, line in enumerate(_wrap(c.detail, 60, 0)[:6]):
                lead = "note    : " if i == 0 else "          "
                print(f"      {'':<7}   {lead}{line}")
        print()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="run-pr-suite",
        description="Perform a PR checklist end to end and print one "
                    "combined report.")
    ap.add_argument("--case-id", default=settings.CASE_ID,
                    help=f"The case to work in (default {settings.CASE_ID}).")
    ap.add_argument("--dry-run", action="store_true",
                    help="Open the form and read every factor, but press "
                         "neither Generate nor Save.")
    ap.add_argument("--headed", action="store_true", help="Show the browser.")
    ap.add_argument("--show", action="store_true",
                    help="Include passing checks in the per-factor table.")
    ap.add_argument("--attempts", type=int, default=3,
                    help="Retries for a run that cannot get past the login "
                         "overlay (default 3).")
    args = ap.parse_args(argv)

    try:
        W.assert_writable()
    except W.WriteRefused as e:
        print(f"REFUSED: {e}")
        return 2

    mode = "DRY RUN" if args.dry_run else "LIVE — data will be written"
    print(f"Target : {crawler_config.BASE_URL}")
    print(f"Case   : {args.case_id}")
    print(f"Mode   : {mode}")
    print(f"Screen : {PR_SCREEN}\n")

    result = None
    for attempt in range(1, max(1, args.attempts) + 1):
        result = cf.fill_case_screens(
            screens=[cf.PR_CHECKLIST],
            case_id=args.case_id,
            headless=False if args.headed else None,
            dry_run=args.dry_run,
            verify=False,          # this screen verifies itself, in-flow
            progress=lambda m: print(f"  {m}"))
        if not _looks_like_login_flake(result):
            break
        print(f"\n  !! attempt {attempt} never got past the login overlay — "
              f"retrying\n")

    if result is None:
        print("Nothing ran.")
        return 2

    mine = [c for c in result.checks if c.screen == PR_SCREEN]
    other = [c for c in result.checks if c.screen != PR_SCREEN]

    print(f"\n{'=' * 78}")
    print("PR CHECKLIST — COMBINED REPORT")
    print(f"{'=' * 78}")
    print(f"run       : {result.run_id}")
    print(f"case      : {result.case_id}")
    print(f"mode      : {'DRY RUN' if result.dry_run else 'LIVE'}")
    print(f"artifacts : {result.artifacts_dir}")

    print(f"\n{'-' * 78}")
    print("SUMMARY")
    print(f"{'-' * 78}")
    v = _verdict(mine)
    counts = {k: sum(1 for c in mine if c.status == k)
              for k in (R.PASS, R.FAIL, R.BLOCKED)}
    print(f"  {'PR Checklist - Perform & Save':<42} {_MARK[v]:<6} "
          f"({counts[R.PASS]} passed / {counts[R.FAIL]} failed / "
          f"{counts[R.BLOCKED]} blocked)")
    verdicts = [v]
    if other:
        ov = _verdict(other)
        print(f"  {'Case-level':<42} {_MARK[ov]:<6} ({len(other)} check(s))")
        verdicts.append(ov)

    print(f"\n{'-' * 78}")
    print("PER-FACTOR DETAIL")
    print(f"{'-' * 78}")
    if not mine:
        print("      (nothing was checked — the screen did not run)")
    else:
        _print_factor_table(mine, args.show)

    if other:
        print(f"{'-' * 78}")
        print("CASE-LEVEL")
        print(f"{'-' * 78}")
        _print_factor_table(other, args.show)

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
