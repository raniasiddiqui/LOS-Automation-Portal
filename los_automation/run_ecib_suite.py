"""
The eCIB Details suite: both ways a record can be created there, one report.

    python -m los_automation.run_ecib_suite
    python -m los_automation.run_ecib_suite --dry-run
    python -m los_automation.run_ecib_suite --headed --show

Three tests, in one browser session on one case, from ONE screen check:

    1. Manual Add flow    '+ Add' -> the three-field dialog -> Proceed ->
                          fifty-odd fields -> Save.        UNCHANGED.
    2. Upload - Entity    '+ Upload' -> AKHUWAT (Entity).pdf
    3. Upload - Individual '+ Upload' -> ABDUR RAUF KHAN (Individual).pdf

All three come from the single 'ecib_details' screen key: that check runs the
Add route and then the Upload route, the same way 'business_performance'
covers both of its sub-menus. Which means the portal's one eCIB button and
the CLI's one --fill-case ecib_details do exactly what this script does; the
only thing here that is not there is the REPORT.

One session rather than three so the run logs in once, which matters more
than it should on this environment - see the login note below.

WHY THIS SCRIPT EXISTS AT ALL, given cli.py can already run the screens:
cli.py prints per-screen checks, and what was asked for here is a
consolidated PASS/FAIL per FLOW with the upload cases' per-field detail
underneath. That is a reporting concern, so it lives in a reporting script
rather than being bolted onto the runner.

THE LOGIN RETRY, and why it is here and not in crawler.py
---------------------------------------------------------
This app renders its login form roughly 20-30 seconds BEFORE its
ngx-ui-loader overlay clears. That overlay is opacity:0 - invisible to a
person - but it is full-screen at z-index 99998 with pointer-events:auto, so
it silently swallows the click on Login. crawler.login() clicks as soon as
the form appears, so whether a run starts at all depends on how loaded the
server happens to be; measured on this box it lost the race about half the
time, and a lost race surfaces as a 20s Playwright timeout that looks like
bad credentials.

The right fix is one line in crawler.login - wait for the loader before
clicking - but that is shared by every flow in this project and changing it
was outside what this work was asked to touch. So this script retries the
whole run instead, and reports the flake rather than hiding it. See
LOGIN_HINT below for the suggested fix.
"""
from __future__ import annotations

import argparse
import sys

import config as crawler_config

from . import settings
from .runner import case_flows as cf
from .runner import results as R
from .runner import widgets as W

_MARK = {R.PASS: "PASS", R.FAIL: "FAIL", R.BLOCKED: "BLOCK"}

LOGIN_HINT = (
    "Suggested one-line fix in crawler.login(), before the submit click:\n"
    "    page.wait_for_function(\n"
    "        \"() => ![...document.querySelectorAll('.ngx-overlay')].some(o => {\"\n"
    "        \"  const s = getComputedStyle(o);\"\n"
    "        \"  return s.display !== 'none' && s.pointerEvents !== 'none'\"\n"
    "        \"         && o.getBoundingClientRect().width > 0; })\",\n"
    "        timeout=90000)")

# Which screen key belongs to which line of the summary, and what to call it.
FLOWS = [
    ("Manual Add Flow", [cf.SCREEN_LABEL[cf.ECIB_DETAILS]]),
    ("Upload - Entity (AKHUWAT)", [cf._upload_screen(cf.UPLOAD_ENTITY)]),
    ("Upload - Individual (ABDUR RAUF KHAN)",
     [cf._upload_screen(cf.UPLOAD_INDIVIDUAL)]),
]


def _looks_like_login_flake(result) -> bool:
    """
    A run that never got in, as opposed to a run that found something.

    Deliberately generous about what counts. Two different failures on this
    environment both mean "we never reached the screen under test", and
    neither is a finding about the eCIB screens:

      * the click on Login lost the race with the bootstrap overlay, which
        surfaces as a Playwright timeout on button[type=submit];
      * page.goto waits for 'load' and some sub-resource of /login never
        finishes, which surfaces as a 60s navigation timeout even though the
        page's own HTML comes back in about a second.

    The guard is that NOTHING passed. Once any check has passed the run got
    in, so a later timeout is real and must not be retried away.
    """
    if any(c.status == R.PASS for c in result.checks):
        return False
    blob = " ".join([result.blocked_reason or ""]
                    + [c.detail or "" for c in result.checks]).lower()
    if not blob:
        return False
    return ("could not sign in" in blob
            or "ngx-overlay" in blob
            or ("timeout" in blob
                and ("submit" in blob or "/login" in blob
                     or "page.goto" in blob)))


def _verdict(checks: list) -> str:
    """PASS only if something was checked and nothing failed."""
    if any(c.status == R.FAIL for c in checks):
        return R.FAIL
    if not any(c.status == R.PASS for c in checks):
        return R.BLOCKED
    return R.PASS


def _for_flow(result, screens: list[str]) -> list:
    return [c for c in result.checks if c.screen in screens]


def _print_flow_detail(checks: list, show_passes: bool) -> None:
    shown = [c for c in checks if show_passes or c.status != R.PASS]
    if not shown:
        print("      (all checks passed)")
        return
    for c in sorted(shown, key=lambda c: (R.ORDER.get(c.status, 9), c.name)):
        print(f"      {_MARK.get(c.status, '?'):<5} {c.name}")
        if c.expected:
            print(f"            expected: {c.expected[:150]}")
        if c.actual:
            print(f"            actual  : {c.actual[:150]}")
        if c.detail and c.status != R.PASS:
            print(f"            note    : {c.detail[:220]}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="run-ecib-suite",
        description="Run the manual Add flow and both Upload cases on the "
                    "eCIB Details screen, and print one combined report.")
    ap.add_argument("--case-id", default=settings.CASE_ID,
                    help=f"The case to work in (default {settings.CASE_ID}).")
    ap.add_argument("--dry-run", action="store_true",
                    help="Fill everything but never commit. Note that Upload's "
                         "Proceed CREATES the record, so a dry run can only "
                         "check the dialog, not the extraction.")
    ap.add_argument("--headed", action="store_true",
                    help="Show the browser.")
    ap.add_argument("--show", action="store_true",
                    help="Include passing checks in the per-field detail.")
    ap.add_argument("--attempts", type=int, default=3,
                    help="How many times to retry a run that cannot get past "
                         "the login overlay (default 3).")
    ap.add_argument("--no-verify", action="store_true",
                    help="Skip the manual flow's round trip.")
    args = ap.parse_args(argv)

    try:
        W.assert_writable()
    except W.WriteRefused as e:
        print(f"REFUSED: {e}")
        return 2

    # ONE screen key, which now covers both routes — the Add flow first, then
    # both upload cases. The three lines of the summary below come off the
    # checks' own screen labels, not off this list.
    screens = [cf.ECIB_DETAILS]
    mode = "DRY RUN" if args.dry_run else "LIVE — data will be written"
    print(f"Target : {crawler_config.BASE_URL}")
    print(f"Case   : {args.case_id}")
    print(f"Mode   : {mode}")
    print(f"PDFs   : {cf.ECIB_PDF_DIR}")
    print(f"Screens: {', '.join(cf.SCREEN_LABEL[s] for s in screens)}\n")

    result = None
    for attempt in range(1, max(1, args.attempts) + 1):
        result = cf.fill_case_screens(
            screens=screens,
            case_id=args.case_id,
            headless=False if args.headed else None,
            dry_run=args.dry_run,
            verify=not args.no_verify,
            progress=lambda m: print(f"  {m}"))
        if not _looks_like_login_flake(result):
            break
        print(f"\n  !! attempt {attempt} never got past the login overlay — "
              f"retrying\n")

    if result is None:
        print("Nothing ran.")
        return 2

    # ---- the consolidated report -------------------------------------
    print(f"\n{'=' * 78}")
    print("eCIB DETAILS — COMBINED REPORT")
    print(f"{'=' * 78}")
    print(f"run       : {result.run_id}")
    print(f"case      : {result.case_id}")
    print(f"mode      : {'DRY RUN' if result.dry_run else 'LIVE'}")
    print(f"artifacts : {result.artifacts_dir}")

    print(f"\n{'-' * 78}")
    print("SUMMARY")
    print(f"{'-' * 78}")
    verdicts = []
    for title, screens_for in FLOWS:
        checks = _for_flow(result, screens_for)
        v = _verdict(checks)
        verdicts.append(v)
        counts = {k: sum(1 for c in checks if c.status == k)
                  for k in (R.PASS, R.FAIL, R.BLOCKED)}
        print(f"  {title:<42} {_MARK[v]:<6} "
              f"({counts[R.PASS]} passed / {counts[R.FAIL]} failed / "
              f"{counts[R.BLOCKED]} blocked)")

    other = [c for c in result.checks
             if c.screen not in {s for _, ss in FLOWS for s in ss}]
    if other:
        v = _verdict(other)
        print(f"  {'Case / round trip':<42} {_MARK[v]:<6} "
              f"({len(other)} check(s))")
        verdicts.append(v)

    # Any failure fails the lot. With no failures it is a PASS only if
    # something actually passed — "nothing was checked" is BLOCKED, never a
    # green light.
    overall = R.FAIL if R.FAIL in verdicts else (
        R.PASS if R.PASS in verdicts else R.BLOCKED)

    print(f"\n{'-' * 78}")
    print("PER-FIELD DETAIL — the two upload cases")
    print(f"{'-' * 78}")
    for title, screens_for in FLOWS[1:]:
        checks = _for_flow(result, screens_for)
        print(f"\n  {title}  [{_MARK[_verdict(checks)]}]")
        if not checks:
            print("      (nothing was checked — the case did not run)")
            continue
        _print_flow_detail(checks, args.show)

    manual = _for_flow(result, FLOWS[0][1])
    failed_manual = [c for c in manual if c.status != R.PASS]
    if failed_manual:
        print(f"\n{'-' * 78}")
        print("MANUAL ADD FLOW — anything not passing")
        print(f"{'-' * 78}")
        _print_flow_detail(failed_manual, False)

    if other:
        print(f"\n{'-' * 78}")
        print("CASE-LEVEL / ROUND TRIP")
        print(f"{'-' * 78}")
        _print_flow_detail(other, args.show)

    print(f"\n{'=' * 78}")
    print(f"ALL TESTS: {_MARK[overall]}")
    print(f"{'=' * 78}")
    if result.blocked_reason:
        print(f"\nBlocked: {result.blocked_reason}")
        if _looks_like_login_flake(result):
            print(f"\n{LOGIN_HINT}")
    if not args.show:
        print("\n  (passing checks hidden in places — re-run with --show)")

    return {R.PASS: 0, R.FAIL: 1, R.BLOCKED: 2}[overall]


if __name__ == "__main__":
    sys.exit(main())
