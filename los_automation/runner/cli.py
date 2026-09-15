"""
Command line for the runner — the development and debugging entry point.

    python -m los_automation.runner.cli --list
    python -m los_automation.runner.cli --check-env
    python -m los_automation.runner.cli --verify obligor.view
    python -m los_automation.runner.cli --verify obligor.view --show
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import config as crawler_config

from .. import settings
from . import results as R
from . import run as run_mod
from . import targets as tg

_MARK = {R.PASS: "PASS", R.FAIL: "FAIL", R.ERROR: "ERROR"}


def _print_env() -> None:
    allowed, reason = settings.write_allowed(crawler_config.BASE_URL)
    print("Environment")
    print(f"  Target URL     : {crawler_config.BASE_URL}")
    print(f"  Credentials    : {'set' if crawler_config.USERNAME else 'MISSING'}")
    print(f"  Headless       : {settings.HEADLESS}")
    print(f"  Data entry     : {'ALLOWED' if allowed else 'BLOCKED'} — {reason}")


def _print_sub_screens(subs, indent: int = 0) -> None:
    """Parents before children, so a doorway like Obligor Details (BIR) reads
    as the group of tabs it actually is."""
    for sub in subs:
        pad = " " * indent
        extra = []
        if sub.kind == tg.CONTEXT_MENU:
            extra.append("case menu")
        if sub.open_row_detail:
            extra.append("opens a record")
        if sub.walk_inner_tabs:
            extra.append("walks its tabs")
        tail = f"   [{', '.join(extra)}]" if extra else ""
        if sub.check_self:
            print(f"  {'':<22}   {pad}- {sub.name:<28}{tail}")
        else:
            print(f"  {'':<22}   {pad}- {sub.name:<28} (checked via its tabs){tail}")
        _print_sub_screens(sub.children, indent + 2)


def _print_targets() -> None:
    print(f"Record under test: {settings.CASE_ID}  (override with --case-id)")
    for area, items in sorted(tg.by_area().items()):
        print(f"\n{area}")
        for t in items:
            flag = "  [writes]" if t.writable else ""
            print(f"  {t.key:<22} {t.title}{flag}")
            print(f"  {'':<22} path: {t.path_description()}")
            _print_sub_screens(t.sub_screens)


def _print_result(result: R.RunResult, show_passes: bool) -> None:
    print(f"\n{'=' * 78}")
    print(f"{result.target_title}")
    if result.case_id:
        print(f"record {result.case_id}")
    print(f"run {result.run_id}   {result.overall}   {result.headline}")
    print(f"{'=' * 78}")

    if result.steps:
        print("\nNavigation")
        for s in result.steps:
            print(f"  {_MARK.get(s.status, '?'):<5} {s.index}. {s.kind:<13} "
                  f"{(s.label or '')[:34]:<34} {s.note[:70]}")

    summary = result.screen_summary()
    if len(summary) > 1:
        print("\nPer screen")
        for row in summary:
            print(f"  {_MARK.get(row['status'], '?'):<5} {row['screen'][:32]:<34} "
                  f"{row['passed']} passed / {row['failed']} failed")

    for screen, checks in result.by_screen().items():
        shown = [c for c in checks if show_passes or c.status != R.PASS]
        if not shown:
            continue
        print(f"\n{screen}")
        for c in shown:
            print(f"  {_MARK.get(c.status, '?'):<5} {c.name}")
            if c.expected:
                print(f"        expected: {c.expected}")
            if c.actual:
                print(f"        actual  : {c.actual}")
            if c.detail:
                print(f"        note    : {c.detail}")
            for e in c.evidence:
                if e:
                    print(f"        evidence: {e}")

    if not show_passes and result.counts[R.PASS]:
        print(f"\n  ({result.counts[R.PASS]} passing checks hidden — use --show)")
    _print_notes(result)
    if result.error_reason:
        print(f"\nCould not run: {result.error_reason}")
    print(f"\nArtifacts: {result.artifacts_dir}")


def _print_notes(result) -> None:
    """
    The run's observations: not verdicts, so they are printed apart from the
    checks and counted nowhere.
    """
    notes = getattr(result, "notes", None) or []
    if not notes:
        return
    print(f"\nObservations ({len(notes)}) — not passes or failures")
    for n in notes:
        where = f"[{n.screen}] " if getattr(n, "screen", "") else ""
        print(f"  - {where}{n.text}")


def _emitter(path: str):
    """One JSON object per line, flushed immediately: the reader polls this
    file while the run is still in progress."""
    if not path:
        return None
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)

    def emit(event: dict) -> None:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(event) + "\n")
            fh.flush()
    return emit


def _write_pdf(artifacts_dir: str, events_path: str = "",
               quiet: bool = True) -> str:
    """
    Build the downloadable report, here in the runner's own process.

    It has to happen here and not in the web page: rendering the PDF drives the
    same headless Chromium the runs use, and Playwright's sync API will not
    start inside Streamlit's asyncio loop. Doing it at the end of every run
    also means the download is ready the moment the run is, rather than after
    a second wait nobody expected.

    Never fatal. A run that produced a perfectly good result.json must not be
    reported as failed because a report could not be drawn from it.
    """
    from . import report_pdf

    try:
        path = report_pdf.build(artifacts_dir)
    except Exception as exc:  # noqa: BLE001
        if not quiet:
            print(f"\n(The PDF report could not be built: {str(exc)[:200]})")
        return ""
    if events_path:
        try:
            with open(events_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"kind": "report", "pdf": path,
                                     "html": os.path.join(
                                         artifacts_dir,
                                         report_pdf.HTML_NAME)}) + "\n")
        except OSError:
            pass
    if not quiet and path:
        print(f"PDF report: {path}")
    return path


def _create_obligor(args) -> int:
    """Phase 2 entry point. Kept separate so the read-only path above cannot
    accidentally reach it."""
    from . import flows
    from . import widgets as W

    try:
        W.assert_writable()
    except W.WriteRefused as e:
        print(f"REFUSED: {e}")
        return 2

    if not args.dry_run and not args.quiet:
        print("This WILL create a real obligor and a real credit case on "
              f"{crawler_config.BASE_URL}.\n")

    result = flows.create_obligor(
        headless=False if args.headed else None,
        dry_run=args.dry_run,
        obligor_name=args.obligor_name,
        stop_after_save=args.stop_after_save,
        fill_tabs=args.fill_tabs,
        progress=None if args.quiet else (lambda m: print(f"  {m}")),
        run_id=args.run_id,
        emit=_emitter(args.events))

    if not args.quiet:
        print(f"\n{'=' * 78}")
        print(f"{'DRY RUN' if result.dry_run else 'LIVE'}   {result.overall}   "
              f"{result.headline}")
        print(f"obligor : {result.obligor_name}")
        print(f"customer: {result.customer_id or '(not created)'}")
        print(f"case    : {result.request_id or '(not created)'}")
        print(f"{'=' * 78}")
        for st in result.steps:
            print(f"  {_MARK.get(st['status'], '?'):<5} {st['index']:>2}. "
                  f"{st['text'][:38]:<38} {st.get('note', '')[:60]}")
        shown = [c for c in result.checks
                 if args.show or c.status != R.PASS]
        for c in shown:
            print(f"\n  {_MARK.get(c.status, '?'):<5} {c.name}")
            for k in ("expected", "actual", "detail"):
                if getattr(c, k, ""):
                    print(f"        {k}: {getattr(c, k)[:200]}")
        _print_notes(result)
        if result.error_reason:
            print(f"\nCould not run: {result.error_reason}")
        print(f"\nArtifacts: {result.artifacts_dir}")

    _write_pdf(result.artifacts_dir, args.events, args.quiet)
    return {R.PASS: 0, R.FAIL: 1, R.ERROR: 2}.get(result.overall, 1)


def _fill_obligor_details(args) -> int:
    """
    Phase 2c entry point: take a case that already exists and fill the rest of
    its Obligor Details (BIR) — every tab below Basic Information, and every
    one of their tables — then verify by round trip.

    The other half of --create-obligor, which now stops at Basic Information.
    Kept as its own entry point so neither can be reached by the other's flags.
    """
    from . import flows
    from . import widgets as W

    try:
        W.assert_writable()
    except W.WriteRefused as e:
        print(f"REFUSED: {e}")
        return 2

    if not args.dry_run and not args.quiet:
        print(f"This WILL write to case {args.case_id} on "
              f"{crawler_config.BASE_URL}.\n")

    result = flows.fill_case_obligor_details(
        case_id=args.case_id,
        headless=False if args.headed else None,
        dry_run=args.dry_run,
        verify=not args.no_verify,
        progress=None if args.quiet else (lambda m: print(f"  {m}")),
        run_id=args.run_id,
        emit=_emitter(args.events))

    if not args.quiet:
        print(f"\n{'=' * 78}")
        print(f"{'DRY RUN' if result.dry_run else 'LIVE'}   {result.overall}   "
              f"{result.headline}")
        print(f"case    : {result.request_id}")
        print(f"{'=' * 78}")
        for st in result.steps:
            print(f"  {_MARK.get(st['status'], '?'):<5} {st['index']:>2}. "
                  f"{st['text'][:44]:<44} {str(st.get('note', ''))[:56]}")
        by_screen: dict[str, list] = {}
        for c in result.checks:
            by_screen.setdefault(c.screen or "(case)", []).append(c)
        for screen, items in by_screen.items():
            shown = [c for c in items if args.show or c.status != R.PASS]
            if not shown:
                continue
            print(f"\n{screen}")
            for c in shown:
                print(f"  {_MARK.get(c.status, '?'):<5} {c.name}")
                for k in ("expected", "actual", "detail"):
                    if getattr(c, k, ""):
                        print(f"        {k}: {getattr(c, k)[:220]}")
        _print_notes(result)
        if result.error_reason:
            print(f"\nCould not run: {result.error_reason}")
        print(f"\nArtifacts: {result.artifacts_dir}")

    _write_pdf(result.artifacts_dir, args.events, args.quiet)
    return {R.PASS: 0, R.FAIL: 1, R.ERROR: 2}.get(result.overall, 1)


def _fill_case(args) -> int:
    """
    Phase 2b entry point: fill Request Details / Facilities / Observations /
    Conditions / Documents on a case that already exists, then verify by round
    trip.

    Kept separate from both the read-only path and the create-obligor path, so
    neither can be reached by accident from the other's flags.
    """
    from . import case_flows as cf
    from . import widgets as W

    try:
        W.assert_writable()
    except W.WriteRefused as e:
        print(f"REFUSED: {e}")
        return 2

    wanted = list(cf.ORDER) if args.fill_case == "all" else [args.fill_case]

    if not args.dry_run and not args.quiet:
        print(f"This WILL write to case {args.case_id} on "
              f"{crawler_config.BASE_URL}.\n")

    result = cf.fill_case_screens(
        screens=wanted,
        case_id=args.case_id,
        headless=False if args.headed else None,
        dry_run=args.dry_run,
        verify=not args.no_verify,
        progress=None if args.quiet else (lambda m: print(f"  {m}")),
        run_id=args.run_id,
        emit=_emitter(args.events))

    if not args.quiet:
        print(f"\n{'=' * 78}")
        print(f"{'DRY RUN' if result.dry_run else 'LIVE'}   {result.overall}   "
              f"{result.headline}")
        print(f"case    : {result.case_id}")
        print(f"screens : {', '.join(cf.SCREEN_LABEL[s] for s in result.screens)}")
        print(f"marker  : {result.marker}")
        if result.facility_ref:
            print(f"facility: {result.facility_ref}")
        print(f"{'=' * 78}")
        for st in result.steps:
            print(f"  {_MARK.get(st['status'], '?'):<5} {st['index']:>2}. "
                  f"{st['text'][:44]:<44} {str(st.get('note', ''))[:56]}")

        by_screen: dict[str, list] = {}
        for c in result.checks:
            by_screen.setdefault(c.screen or "(case)", []).append(c)
        for screen, items in by_screen.items():
            shown = [c for c in items if args.show or c.status != R.PASS]
            if not shown:
                continue
            print(f"\n{screen}")
            for c in shown:
                print(f"  {_MARK.get(c.status, '?'):<5} {c.name}")
                for k in ("expected", "actual", "detail"):
                    if getattr(c, k, ""):
                        print(f"        {k}: {getattr(c, k)[:220]}")
        if not args.show and result.counts[R.PASS]:
            print(f"\n  ({result.counts[R.PASS]} passing checks hidden — "
                  f"use --show)")
        _print_notes(result)
        if result.error_reason:
            print(f"\nCould not run: {result.error_reason}")
        print(f"\nArtifacts: {result.artifacts_dir}")

    _write_pdf(result.artifacts_dir, args.events, args.quiet)
    return {R.PASS: 0, R.FAIL: 1, R.ERROR: 2}.get(result.overall, 1)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="los-runner",
        description="Verify and (later) exercise Loan Origination System screens.")
    ap.add_argument("--list", action="store_true", help="List available targets.")
    ap.add_argument("--check-env", action="store_true",
                    help="Show environment readiness, run nothing.")
    ap.add_argument("--verify", metavar="TARGET",
                    help="Run the read-only checks for a target.")
    ap.add_argument("--show", action="store_true",
                    help="Include passing checks in the output.")
    ap.add_argument("--headed", action="store_true",
                    help="Show the browser (useful while debugging a path).")
    ap.add_argument("--case-id", default=settings.CASE_ID,
                    help=f"The record to verify (default {settings.CASE_ID}). "
                         f"Pinning it keeps runs comparable.")
    ap.add_argument("--run-id", help="Use this run id instead of generating one.")
    ap.add_argument("--events", metavar="PATH",
                    help="Append progress events as JSON lines. The web UI reads "
                         "this to stream steps and screenshots while running.")
    ap.add_argument("--quiet", action="store_true",
                    help="Suppress the human-readable report (for UI use).")

    ap.add_argument("--make-pdf", metavar="DIR",
                    help="Build report.pdf and report.html from a finished "
                         "run's artifacts folder, and run nothing else. Every "
                         "run does this for itself; this is for the ones that "
                         "predate it.")

    # ---- Phase 2: data entry ------------------------------------------
    ap.add_argument("--create-obligor", action="store_true",
                    help="PHASE 2, WRITES DATA. Create an obligor, fill Basic "
                         "Information ONLY — leaving Client Number/CIF empty — "
                         "raise a Borrower Credit Application, then find the "
                         "new case in My Bucket and verify every entered value "
                         "against it. The remaining tabs are a separate run: "
                         "see --fill-obligor-details. Refused unless the host "
                         "is in LOS_ALLOWED_WRITE_HOSTS.")
    ap.add_argument("--fill-tabs", action="store_true",
                    help="With --create-obligor: also fill the nine tabs that "
                         "unlock after Save, in the same run. Off by default — "
                         "creating the record and finishing it are two jobs.")
    ap.add_argument("--dry-run", action="store_true",
                    help="With --create-obligor or --fill-case: fill every "
                         "field and report what the form holds, but do NOT "
                         "click Save. Nothing is written.")
    ap.add_argument("--obligor-name", default="",
                    help="With --create-obligor: the name to use. Defaults to "
                         "'AUTOMATION TEST <timestamp>'.")
    ap.add_argument("--stop-after-save", action="store_true",
                    help="With --create-obligor: create the obligor but do not "
                         "raise a transaction.")

    # ---- Phase 2b: the case's own screens ------------------------------
    ap.add_argument("--fill-case", metavar="SCREEN",
                    choices=["request_details", "facilities", "observations",
                             "collaterals", "coverage", "risk_rating",
                             "policies", "conditions", "documents", "all"],
                    help="PHASE 2b, WRITES DATA. Open the case named by "
                         "--case-id from My Bucket and fill one of its screens "
                         "— request_details, facilities, observations, "
                         "collaterals, coverage, risk_rating, policies, "
                         "conditions or documents — or 'all' for the nine in "
                         "order, then re-open the case "
                         "and check every entered value came back. "
                         "'collaterals' asks for a collateral by "
                         "classification and name and fills every tab it "
                         "opens with; 'risk_rating' scores the case's rating "
                         "model through Perform Risk Rating and checks the "
                         "rating reaches the Rating Summary and Rating "
                         "History; 'policies' raises a policy exception "
                         "through Add Exception; 'conditions' raises a "
                         "condition and attaches a file to it; 'documents' "
                         "also downloads whatever the case already has "
                         "attached. Refused unless the host is in "
                         "LOS_ALLOWED_WRITE_HOSTS.")
    # ---- Phase 2c: the rest of the obligor, on an existing case ---------
    ap.add_argument("--fill-obligor-details", action="store_true",
                    help="PHASE 2c, WRITES DATA. Open the case named by "
                         "--case-id from My Bucket, go to Obligor Details "
                         "(BIR), and fill every tab below Basic Information "
                         "and every one of their eighteen tables. Then come "
                         "back in through My Bucket and check every entered "
                         "value came back. This is the other half of "
                         "--create-obligor. Refused unless the host is in "
                         "LOS_ALLOWED_WRITE_HOSTS.")

    ap.add_argument("--no-verify", action="store_true",
                    help="With --fill-case or --fill-obligor-details: fill and "
                         "save, but skip the round trip. Only useful while "
                         "working on the filling.")
    args = ap.parse_args(argv)

    if args.list:
        _print_targets()
        return 0
    if args.check_env:
        _print_env()
        return 0
    if args.make_pdf:
        path = _write_pdf(args.make_pdf, "", quiet=False)
        return 0 if path else 1

    if args.create_obligor:
        return _create_obligor(args)

    if args.fill_obligor_details:
        return _fill_obligor_details(args)

    if args.fill_case:
        return _fill_case(args)

    if not args.verify:
        ap.print_help()
        return 2

    try:
        tg.get(args.verify)
    except KeyError as e:
        print(e)
        return 2

    emit = None
    if args.events:
        os.makedirs(os.path.dirname(os.path.abspath(args.events)) or ".", exist_ok=True)

        def emit(event: dict) -> None:  # noqa: F811 - deliberate conditional def
            # One JSON object per line, flushed immediately: the reader is
            # polling this file while the run is still in progress.
            with open(args.events, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(event) + "\n")
                fh.flush()

    result = run_mod.verify(
        args.verify,
        headless=False if args.headed else None,
        progress=None if args.quiet else (lambda m: print(f"  {m}")),
        emit=emit,
        run_id=args.run_id,
        case_id=args.case_id)
    if not args.quiet:
        _print_result(result, show_passes=args.show)
    _write_pdf(result.artifacts_dir, args.events, args.quiet)
    # FAIL is a finding (exit 1); ERROR is an environment problem (exit 2), so
    # CI can tell "the app is broken" from "we could not test it".
    return {R.PASS: 0, R.FAIL: 1, R.ERROR: 2}.get(result.overall, 1)


if __name__ == "__main__":
    sys.exit(main())
