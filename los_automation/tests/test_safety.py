"""
Safety tests for the automation portal.

These guard the property that makes the read-only checks safe to point at any
environment: they cannot type into a form, cannot click a destructive control,
and refuse to write anywhere except the approved dev host.

Phase 2 adds data entry, which changes the claim being defended. It is no longer
"nothing in this package writes" — it is:

    exactly ONE module writes, and it refuses to do so off the allowlist.

So section 1 now checks two things instead of one: that the read-only modules
are still write-free, and that widgets.py — the module that does write — cannot
be talked into it on the wrong host, and cannot commit a control outside its own
allowlist no matter what a flow asks for.

Run:  python -m los_automation.tests.test_safety
"""
from __future__ import annotations

import inspect
import os
import re
import sys
from datetime import date

import los_automation  # noqa: F401  (sets sys.path to the project root)

import crawler as cr
from los_automation import settings
from los_automation.runner import (case_flows as CF, checks, cli, driver,
                                   exports as X, flows, results as R,
                                   run as run_mod, targets, widgets as W)

FAILURES: list[str] = []


def check(label: str, condition: bool, note: str = "") -> None:
    status = "ok  " if condition else "FAIL"
    print(f"  [{status}] {label}" + (f" — {note}" if note else ""))
    if not condition:
        FAILURES.append(label)


# --------------------------------------------------------------------------
print("\n1. The runner cannot type into the application")
# --------------------------------------------------------------------------
PHASE1_MODULES = [driver, checks, run_mod, targets, R]
BANNED = (".fill(", ".type(", ".press_sequentially(", ".set_input_files(",
          ".check(", ".uncheck(", ".select_option(", ".drag_to(")

for mod in PHASE1_MODULES:
    src = inspect.getsource(mod)
    hits = [b for b in BANNED if b in src]
    check(f"{mod.__name__.split('.')[-1]}.py has no input-writing calls",
          not hits, f"found {hits}" if hits else "")

crawler_src = inspect.getsource(cr)
login_src = inspect.getsource(cr.login)
check("crawler.py still only calls .fill() inside login()",
      len(re.findall(r"\.fill\(", crawler_src)) == len(re.findall(r"\.fill\(", login_src)),
      f"{len(re.findall(r'.fill.', crawler_src))} total")

# Writing lives in widgets.py and nowhere else. flows.py authors the sequence
# but must go through widgets to touch anything.
#
# A narrower list here than for the read-only modules, on purpose: `.check(` is
# banned above because it is Playwright's checkbox setter, but Filler exposes a
# method of the same name, so `f.check(...)` in a flow is a call INTO widgets
# rather than a direct write. These four have no such collision.
DIRECT_WRITES = (".fill(", ".type(", ".press_sequentially(",
                 ".set_input_files(", ".select_option(", ".drag_to(")
flows_src = inspect.getsource(flows)
flow_hits = [b for b in DIRECT_WRITES if b in flows_src]
check("flows.py writes only through widgets.py", not flow_hits,
      f"found {flow_hits}" if flow_hits else "")

case_src = inspect.getsource(CF)
case_hits = [b for b in DIRECT_WRITES if b in case_src]
check("case_flows.py writes only through widgets.py", not case_hits,
      f"found {case_hits}" if case_hits else "")

widgets_src = inspect.getsource(W)
check("widgets.py is the module that writes",
      any(b in widgets_src for b in BANNED))


# --------------------------------------------------------------------------
print("\n1a. Nothing is compared against the specification")
# --------------------------------------------------------------------------
# The only expectation this suite compares a value against is the one it
# created itself: what a flow just typed in, re-read through a fresh load of
# the record. There is no second source of truth to drift out of date, and no
# check can fail because a document and an application word a field
# differently.
import importlib  # noqa: E402

check("runner.baseline is gone",
      importlib.util.find_spec("los_automation.runner.baseline") is None)

ALL_MODULES = [checks, driver, run_mod, targets, R, flows, CF, W]
for mod in ALL_MODULES:
    src = inspect.getsource(mod)
    hits = [n for n in ("import baseline", "baseline as bl", "knowledge_graph",
                        "fsd_fields", "mandatory_fsd_fields", "kb_status",
                        "fields_against_spec", "crawled_state")
            if n in src]
    check(f"{mod.__name__.split('.')[-1]}.py reads no specification",
          not hits, f"found {hits}" if hits else "")

for gone in ("fsd_sections", "tab_sections", "spec_across_tabs", "note"):
    check(f"SubScreen no longer carries {gone!r}",
          gone not in targets.SubScreen.__dataclass_fields__)
for gone in ("fsd_sections", "compare_to_crawl", "baseline_path"):
    check(f"Target no longer carries {gone!r}",
          gone not in targets.Target.__dataclass_fields__)
for gone in ("fields_against_spec", "live_labels", "_fields_from_spec",
             "_fields_match_last_crawl"):
    check(f"checks.{gone} is gone", not hasattr(checks, gone))
check("run._section_for_tab is gone", not hasattr(run_mod, "_section_for_tab"))

# What the read-only checks DO assert. Removing the specification comparison
# must not quietly remove these with it.
_screen_src = inspect.getsource(checks.run_screen)
for kept in ("_screen_reached", "_no_failed_api_calls", "_no_error_message",
             "_record_is_complete", "_no_javascript_errors"):
    check(f"a screen is still checked by {kept}", kept in _screen_src)


# --------------------------------------------------------------------------
print("\n1b. Phase 2 writes are gated")
# --------------------------------------------------------------------------
# Gate 1: the host. Every public entry point calls assert_writable, and
# assert_writable fails closed when the allowlist does not contain the host.
_saved = settings.ALLOWED_WRITE_HOSTS
try:
    settings.ALLOWED_WRITE_HOSTS = set()          # simulate an unset allowlist
    refused = False
    try:
        W.assert_writable()
    except W.WriteRefused:
        refused = True
    check("an empty write allowlist refuses all data entry", refused)

    settings.ALLOWED_WRITE_HOSTS = {"somewhere-else.example.com"}
    refused = False
    try:
        W.assert_writable()
    except W.WriteRefused:
        refused = True
    check("a host outside the allowlist refuses data entry", refused)
finally:
    settings.ALLOWED_WRITE_HOSTS = _saved

check("the configured dev host is allowed", bool(W.assert_writable()))

# Gate 2: only authored commit controls, and never a workflow transition.
COMMIT_OK = ["Save", "save", "Proceed", "Add", "OK", "Submit", "Select"]
COMMIT_NO = ["Approve", "Reject", "Delete", "Remove", "Authorize", "Forward",
             "Sign out", "Withdraw", "Release", "Bulk Action", "Return",
             "Print", "Export", "", "Save and approve"]
allowed_bad = [x for x in COMMIT_NO if W._committable(x)]
check("no destructive control is committable", not allowed_bad,
      f"allowed {allowed_bad}" if allowed_bad else "")
refused_good = [x for x in COMMIT_OK if not W._committable(x)]
check("the authored commit controls are permitted", not refused_good,
      f"refused {refused_good}" if refused_good else "")

# The flow names the request type rather than taking whatever is first: picking
# the wrong one starts the wrong workflow.
check("the create flow names its request type",
      flows.REQUEST_TYPE == "Borrower Credit Application", flows.REQUEST_TYPE)

# ---- the grid's search box ------------------------------------------------
# Phase 2 types a case id into My Bucket's search box, because paging is
# capped at twelve pages and a case on page thirteen was reported as missing.
# That is a WRITE by this suite's definition, so it lives in widgets.py and is
# gated exactly like every other one — and it must not leak into Phase 1.
check("only widgets.py can drive the grid's search box",
      hasattr(W.Filler, "search_grid"))
_search_src = inspect.getsource(W.Filler.search_grid)
check("the search box is gated on the host allowlist",
      "assert_writable()" in _search_src)
# A search term is navigation, not data. Recording it as an Entry would put it
# into the round-trip comparison, which compares what was ENTERED against what
# the record came back holding.
check("a search term is never recorded as an entered value",
      "_record(" not in _search_src)
check("a screen with no search box is reported, not failed",
      "return False" in _search_src)

# Phase 1 must still find its records by paging: driver.py is on the list of
# modules that cannot type at all, and _step_row_by_id is what it uses.
check("the read-only runner still finds records without typing",
      not [b for b in BANNED if b in inspect.getsource(driver)]
      and hasattr(driver.Session, "_step_row_by_id"))
check("the read-only row search does not use the search box",
      "search_grid" not in inspect.getsource(driver.Session._step_row_by_id))

# Search first, paging second — and paging has to stay, or a build without a
# search box, or a host where typing is refused, could not find a case at all.
_find_src = inspect.getsource(flows.find_case)
check("find_case asks the search box before paging",
      "_search_bucket" in _find_src
      and _find_src.index("_search_bucket") < _find_src.index("_next_grid_page"))
check("paging remains the fallback", "_next_grid_page" in _find_src
      and "max_pages" in _find_src)
check("the filter is cleared before paging, so paging sees the whole grid",
      '_search_bucket(s, "", say)' in _find_src)
check("a refused or missing search box falls back rather than failing",
      "WriteRefused" in inspect.getsource(flows._search_bucket)
      and "return False" in inspect.getsource(flows._search_bucket))
# Both paths open a record through the same helper, so a case opened by search
# is opened exactly as a case found by paging is.
check("search and paging open a row the same way",
      _find_src.count("_open_row_matching") == 2
      and "_click_stamped" in inspect.getsource(flows._open_row_matching))
# The thirteen fields the live form marks with a red asterisk. The flow fills
# every enterable field on the form, but these are the ones that must stay
# non-optional: an optional field that will not set is a note, and a Basic
# Information that saves with a mandatory field missing is a partial record.
BASIC_MANDATORY = [
    "Existing Customer?", "Obligor Name", "Business Segment",
    "Relationship Branch", "Dealing Branch", "Obligor Type",
    "Legal Constitution", "Regulatory Segment", "Regulatory Sector",
    "Regulatory Industry", "Country of Incorporation", "Obligor Currency",
    "Date of Incorporation"]
_basic = {f.label: f for f in flows.BASIC_INFORMATION}
_missing = [n for n in BASIC_MANDATORY if n not in _basic]
check("every mandatory Basic Information field is authored", not _missing,
      f"missing {_missing}" if _missing else
      f"{len(BASIC_MANDATORY)} mandatory of {len(flows.BASIC_INFORMATION)} "
      f"authored")
_soft = [n for n in BASIC_MANDATORY if n in _basic and _basic[n].optional]
check("no mandatory Basic Information field is treated as optional", not _soft,
      f"optional: {_soft}" if _soft else "")
# Everything beyond the mandatory thirteen must be optional, or one field the
# environment happens not to offer aborts the whole create.
_hard = [f.label for f in flows.BASIC_INFORMATION
         if f.label not in BASIC_MANDATORY and not f.optional]
check("every non-mandatory Basic Information field is optional", not _hard,
      f"would abort the run: {_hard}" if _hard else "")

# Regulatory Industry is filtered by Regulatory Sector, so the order matters.
# So do the two "life time expiry" switches: each DISABLES the expiry date
# beside it, so it has to be answered before that date is reached.
_labels = [f.label for f in flows.BASIC_INFORMATION]
check("Regulatory Sector is set before Regulatory Industry",
      _labels.index("Regulatory Sector") < _labels.index("Regulatory Industry"))
check("Obligor Type is set before Obligor Id Type, which it populates",
      _labels.index("Obligor Type") < _labels.index("Obligor Id Type"))
for _switch, _dt in [("Has LifeTime Expiry", "ID Expiry Date"),
                     ("Father/Guardian ID Has life time expiry",
                      "Father/Guardian ID Expiry Date")]:
    check(f"{_switch!r} is answered before {_dt!r}",
          _labels.index(_switch) < _labels.index(_dt))
for _switch in ["Has LifeTime Expiry",
                "Father/Guardian ID Has life time expiry"]:
    check(f"{_switch!r} is set to No so the date beside it stays enabled",
          str(_basic[_switch].value).lower() == "no",
          repr(_basic[_switch].value))

# Named on request, not taken first. Both drive downstream behaviour — the
# obligor type decides which identity fields the form offers, and the
# regulatory segment feeds the SBP threshold matrix — so "whatever the app
# lists first" would quietly change the shape of every test record when the
# environment's reference data is re-seeded.
for _label, _want in [("Obligor Type", "JOINT"),
                      ("Regulatory Segment", flows.REGULATORY_SEGMENT)]:
    check(f"{_label} is named rather than taken first",
          _basic[_label].value == _want, repr(_basic[_label].value))
check("the regulatory segment is the one that was asked for",
      flows.REGULATORY_SEGMENT == "Medium Enterprise",
      flows.REGULATORY_SEGMENT)

# Net Sales and Regulatory Segment are coupled by a SERVER rule — "Regulatory
# Segment Range Check", HTTP 417, saveAllowed=false — that shows no message and
# marks no field. Filling Net Sales with a number outside the segment's band
# stops every obligor from being created, silently. The band was measured
# against this environment: refused at 400,000,000 and below, accepted at
# 750,000,000 and above. This pins both halves together so neither can be
# changed on its own.
check("Net Sales is the value the form uses",
      _basic["Net Sales"].value == flows.NET_SALES,
      repr(_basic["Net Sales"].value))
check("Net Sales sits inside the measured Medium Enterprise band",
      flows.NET_SALES.isdigit() and int(flows.NET_SALES) >= 750_000_000,
      f"{flows.NET_SALES} — refused at 400,000,000, accepted at 750,000,000")


# --------------------------------------------------------------------------
print("\n1c. The obligor tabs are authored completely")
# --------------------------------------------------------------------------
TAB_NAMES = ["Sector And Industry", "Management & Shareholders",
             "Additional Information", "BBFS Details", "Contact and Address",
             "Limits", "Corporate Governance", "Attachments", "History"]
spec_names = [t.label for t in flows.TAB_SPECS]
check("all nine tabs that unlock after saving are covered",
      set(spec_names) == set(TAB_NAMES),
      str([x for x in TAB_NAMES if x not in spec_names]))

# A tab is more than one pass when it has more than one grid, so the labels
# repeat. What must NOT repeat is the pass name — it is what every check on
# that pass is called, and two passes sharing one name makes the report
# unreadable and the failures unattributable.
_titles = [t.title for t in flows.TAB_SPECS]
_dupes = sorted({t for t in _titles if _titles.count(t) > 1})
check("every pass over a tab has its own name", not _dupes,
      f"repeated: {_dupes}" if _dupes else f"{len(_titles)} passes")

# Grids are addressed by their caption, never by position: the '+' icons only
# exist while their section is expanded, so an index silently moves.
_positional = [t.title for t in flows.TAB_SPECS
               if t.kind == flows.LINKAGE and not t.grid_heading]
check("every linkage pass names the grid it adds a row to", not _positional,
      f"addressed by position: {_positional}" if _positional else "")

# The grids observed on the live application. A missing one is a table that
# silently stops being filled.
GRIDS = {
    "Sector And Industry": ["FINANCING SECTOR AND INDUSTRY"],
    "Management & Shareholders": ["SHAREHOLDERS AND DIRECTORS DETAILS",
                                  "MANAGEMENT", "RELATED PARTY TRANSACTION"],
    "Contact and Address": ["OBLIGOR ADDRESSES"],
    "Additional Information": [
        "OBLIGORS DEPOSIT ACCOUNTS", "REFERENCES", "BANK CHECK",
        "CUSTOMERS EXTERNAL RATINGS", "COMPANY MARKET CHECK",
        "MANUFACTURING FACILITIES / OPERATING FAC", "MAJOR PRODUCTS",
        "MAJOR SUPPLIER(S)", "MAJOR BUYER(S)", "MAJOR COMPETITOR(S)",
        "MAJOR BRAND(S)", "IDENTITY DETAILS", "EMPLOYEE TYPE DETAILS"],
}
for tab_name, headings in GRIDS.items():
    have = [t.grid_heading for t in flows.TAB_SPECS if t.label == tab_name]
    missing = [h for h in headings if h not in have]
    check(f"{tab_name}: every grid is filled", not missing,
          f"missing {missing}" if missing else f"{len(headings)} grid(s)")

# Every mandatory field observed on the live form, per pass. Anything dropped
# from a spec means that form silently stops saving.
MANDATORY = {
    "Management & Shareholders — Shareholders and Directors": [
        "Shareholder Name", "Shareholding Percentage", "Gender", "PEP",
        "On NAB / FIA List", "Related Party?"],
    "Management & Shareholders — Management": ["Full Name"],
    "Management & Shareholders — Related Party Transactions": [
        "Related Party Name", "Brief Description Of Transaction",
        "Amount (in Actual)"],
    "Additional Information — Obligor's Deposit Accounts": [
        "Nature of Account", "Account Title", "Branch Name"],
    "Additional Information — References": ["Name", "Phone"],
    "Additional Information — Bank Check": ["Bank"],
    "Additional Information — Customer's External Ratings": ["Agency"],
    "Additional Information — Company Market Check": [
        "Audience Type", "Remarks", "Name & Designation", "Date",
        "Business Relationship"],
    "Additional Information — Major Products": ["Status"],
    "Additional Information — Major Suppliers": ["Supplier", "Status"],
    "Additional Information — Major Buyers": ["Buyer Name", "Status"],
    "Additional Information — Major Competitors": ["Competitors", "Status"],
    "Additional Information — Major Brands": ["Brand Name(s)"],
    "Additional Information — Identity Details": ["ID Type"],
    "Contact and Address": ["Address Type", "Primary/Secondary?", "Address",
                            "City", "Country"],
    "Corporate Governance": [
        "No. of Non-Executive independent Director(s) on the Board",
        "External Auditor Has Satisfactory QCR Rating",
        "Does company provide Quarterly / Half Year Financial Statement",
        "Obligor timely submitted audited accounts?",
        "Latest Audited Financial Published?", "Date of Last Audit",
        "Is The Auditor QCR Rated?", "Audit Type (Qualified)"],
}
for pass_name, required in MANDATORY.items():
    spec = next(t for t in flows.TAB_SPECS if t.title == pass_name)
    have = [f.label for f in spec.fields]
    missing = [r for r in required if r not in have]
    check(f"{pass_name}: every mandatory field is authored", not missing,
          f"missing {missing}" if missing else f"{len(required)} mandatory")

# BBFS Details is nineteen narrative editors, and every one of them is filled
# with text that names its own box — otherwise the round-trip check matches any
# box against any other and proves nothing.
_bbfs = next(t for t in flows.TAB_SPECS if t.label == "BBFS Details")
check("all nineteen BBFS Details editors are filled",
      len(_bbfs.fields) == 19, f"{len(_bbfs.fields)} authored")
_bbfs_values = [f.value for f in _bbfs.fields]
check("each BBFS Details editor gets its own text",
      len(set(_bbfs_values)) == len(_bbfs_values),
      f"{len(set(_bbfs_values))} distinct of {len(_bbfs_values)}")

# Length, not just digits: an 11-digit number is accepted into the box and then
# fails an inline rule, so Save does nothing and reports nothing.
check("phone numbers are 14 digits",
      flows.PHONE.isdigit() and len(flows.PHONE) == 14,
      f"{flows.PHONE!r} is {len(flows.PHONE)} char(s)")
check("fax numbers are 14 digits",
      flows.FAX.isdigit() and len(flows.FAX) == 14,
      f"{flows.FAX!r} is {len(flows.FAX)} char(s)")
_phones = [(t.title, f.label, f.value)
           for t in flows.TAB_SPECS for f in t.fields
           if f.value and any(w in f.label.lower()
                              for w in ("phone", "fax", "contact no",
                                        "contact number"))]
_bad_phones = [p for p in _phones
               if not (str(p[2]).isdigit() and len(str(p[2])) == 14)]
check("every phone/fax field uses a 14-digit value", not _bad_phones,
      str(_bad_phones) if _bad_phones else f"{len(_phones)} field(s)")

# Compliance flags are Yes/No controls, so "first valid option" would turn them
# ON. Marking a synthetic obligor — or one of its people — as politically
# exposed, NAB/FIA listed or a related party is not acceptable in a shared
# environment: somebody eventually has to deal with it.
COMPLIANCE = ("politically exposed", "nab / fia", "nab/fia", "pep",
              "related party?", "is obligor a related party")
_flags = [(t.title, f.label, f.value) for t in flows.TAB_SPECS for f in t.fields
          if any(w in f.label.lower() for w in COMPLIANCE)]
_on = [x for x in _flags if str(x[2]).lower() != "no"]
check("every compliance flag is explicitly set to No", not _on,
      str(_on) if _on else f"{len(_flags)} flag(s)")

# These two hold nothing to type, and saying so is better than a failed fill.
for nm in ["Attachments", "History"]:
    spec = next(t for t in flows.TAB_SPECS if t.label == nm)
    check(f"{nm} is skipped with a reason",
          spec.skip and bool(spec.note))


# --------------------------------------------------------------------------
print("\n1d. The case screens are authored against the specification")
# --------------------------------------------------------------------------
EXPECTED_SCREENS = [
    "Request Details", "Facilities", "Observations", "Litigation",
    # 'RMG Memo' in the application until it was renamed; the key is still
    # crmd_note.
    "Shariah Comments", "RMG Memo", "Credit Memorandum", "Group Review",
    # ONE entry, covering both of its sub-menus. They stay separate in the
    # result — each carries its own screen on every entry and check — but
    # "check Business Performance" is one thing to ask for, not two.
    "Business Performance",
    "Relationship with Other Banks / FIs",
    # ONE entry, covering BOTH routes to a record on it: the '+ Add' dialog,
    # then '+ Upload' with a PDF of an SBP CIB report. Same arrangement as
    # Business Performance above — one thing to ask for, two things reported.
    "eCIB Details",
    "PR Checklist",
    "Financials",
    "History",
]
check("the case screens asked for are the ones offered",
      set(CF.SCREEN_LABEL.values()) == set(EXPECTED_SCREENS),
      str(sorted(CF.SCREEN_LABEL.values())))
# A fixed order, with the three original screens first — the order the case
# menu lists THOSE three in. Where the case menu puts the ones added later
# has not been read off the sidebar, so they are appended rather than claimed
# to be in menu order; nothing depends on it, since each screen is filled and
# saved independently of the others.
check("the screens are filled in a fixed order, the original three first",
      [CF.SCREEN_LABEL[k] for k in CF.ORDER] == EXPECTED_SCREENS,
      str([CF.SCREEN_LABEL[k] for k in CF.ORDER]))
# Every screen must be reachable from the CLI and be dispatched to a flow of
# its own. A screen in SCREEN_LABEL that no branch handles would silently be
# filled as an observation — the wrong form, on the wrong screen.
_cli_src = inspect.getsource(cli.main)
check("every case screen is a --fill-case choice",
      all(k in _cli_src for k in CF.SCREEN_LABEL),
      str([k for k in CF.SCREEN_LABEL if k not in _cli_src]))
check("'all' is read off ORDER rather than listed again",
      "list(cf.ORDER)" in inspect.getsource(cli._fill_case))

# ---- the portal offers what the runner can do -------------------------
# app.py keeps its OWN list of case screens, and it has now been forgotten
# twice: once for the eCIB upload route and once for Financials, both of
# which were wired into ORDER, the CLI and these tests but had no button. The
# symptom is the worst kind — everything passes, and the screen simply is not
# there. app.py cannot be imported here (it calls st.set_page_config at
# module scope), so its list is read out of the source.
#
# Commented-out entries start with '#' and are skipped by the pattern, which
# is what makes the deliberate omissions below distinguishable from mistakes.
_app_path = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "app.py")
_app_src = open(_app_path, encoding="utf-8").read()
_portal_keys = set(re.findall(r'^\s{4}\("([a-z_]+)",', _app_src, re.M))
check("every portal button names a screen the runner knows",
      _portal_keys <= set(CF.SCREEN_LABEL),
      str(sorted(_portal_keys - set(CF.SCREEN_LABEL))))
# Deliberately commented out of the portal on request. Anything ELSE missing
# is an oversight, and this is the check that says so.
DELIBERATELY_HIDDEN = {"request_details", "facilities", "observations"}
_no_button = set(CF.ORDER) - _portal_keys - DELIBERATELY_HIDDEN
check("every runner screen has a portal button, bar the hidden ones",
      not _no_button,
      f"no button for: {sorted(_no_button)}" if _no_button else "")
_dispatch = inspect.getsource(CF.fill_case_screens)
for _key, _fn in [("REQUEST_DETAILS", "_do_request_details"),
                  ("FACILITIES", "_do_facilities"),
                  ("LITIGATION", "_do_litigation"),
                  ("SHARIAH_COMMENTS", "_do_shariah_comments"),
                  ("CRMD_NOTE", "_do_crmd_note"),
                  ("CREDIT_MEMORANDUM", "_do_credit_memorandum"),
                  ("GROUP_REVIEW", "_do_group_review"),
                  ("BUSINESS_PERFORMANCE", "_do_business_performance"),
                  ("BANK_RELATIONSHIPS", "_do_bank_relationships"),
                  ("ECIB_DETAILS", "_do_ecib_details"),
                  ("PR_CHECKLIST", "_do_pr_checklist"),
                  ("FINANCIALS", "_do_financials"),
                  ("HISTORY", "_do_history")]:
    check(f"{_key} is dispatched to {_fn}",
          f"key == {_key}" in _dispatch and f"{_fn}(s, res" in _dispatch)

# ---- Financials -------------------------------------------------------
# Four sub-tabs behind one entry, each guarded so one cannot hide the others.
_fin_do = inspect.getsource(CF._do_financials)
check("each Financials sub-tab is guarded on its own",
      _fin_do.count("except Exception") >= 2
      and all(n in _fin_do for n in ("_fin_add_statement", "_fin_variance",
                                     "_fin_peer", "_fin_analysis")))
check("Financials fills one statement per configured year",
      "for year in FIN_YEARS" in _fin_do and len(CF.FIN_YEARS) >= 2,
      str(CF.FIN_YEARS))
_fin_add = inspect.getsource(CF._fin_add_statement)
# The tick ENABLES the two auditor dropdowns, so it has to come first.
check("'Audited financials?' is ticked before the auditor fields are set",
      _fin_add.index('"Audited financials?"')
      < _fin_add.index('"Type of Auditor"'))
# The app fills the dates itself and refuses a typed End Date.
check("the statement dates are asserted, never typed",
      "_fin_check_dates(" in _fin_add
      and ".date(" not in _fin_add)
check("a year the case already holds is a failure with a way out",
      "LOS_FIN_YEARS" in _fin_add and "R.failed(" in _fin_add)
# The chart of accounts is configuration, not a field list.
_fin_fill = inspect.getsource(CF._fin_fill_statement)
check("the chart of accounts is discovered, not authored",
      "labelled_rows()" in _fin_fill)
check("computed rows are skipped rather than written to",
      "readonly" in _fin_fill and "set_row_cell(" in _fin_fill)
# Each statement carries its OWN Save, side by side. commit() takes the first
# enabled match in document order, which is a statement somebody else filled —
# so this must go through commit_block with the column it just filled.
check("a statement is saved with its own Save, not the first on the page",
      "commit_block(col_index)" in _fin_fill
      and 'f.commit("Save")' not in _fin_fill)
check("the statement's column is found by which cell is editable",
      "col_index = next(" in _fin_fill and "readonly" in _fin_fill)
# Variance and Peer are read-only grids with one writable column.
_fin_cmt = inspect.getsource(CF._fin_comment_grid)
check("the analysis grids write only where the app allows it",
      "not c.get(\"readonly\")" in _fin_cmt)
check("a grid with nothing writable is blocked, not failed",
      "not one writable box" in _fin_cmt)

# ---- History ----------------------------------------------------------
# This screen is read-only, and that has to stay true: it is the one case
# screen a reader might assume fills something.
_hist_src = "\n".join(inspect.getsource(f) for f in
                      (CF._do_history, CF._check_view_changes,
                       CF._check_requests_history, CF._history_activate))
# Call syntax, not prose: _history_activate's docstring NAMES press_action in
# order to explain why it deliberately does not use it, and a bare substring
# search cannot tell a call from an explanation.
_hist_writes = [w for w in ("_fill_pass(", ".commit(", ".set_value(",
                            ".press_action(", ".text(", ".choose(",
                            ".set_factor(", "res.entries")
                if w in _hist_src]
check("the History screen writes nothing", not _hist_writes,
      f"found {_hist_writes}" if _hist_writes else "read-only")
# Its row click navigates to ANOTHER transaction. Running it before the other
# screens would leave them on the wrong case, and not returning would hand the
# round trip the wrong one.
check("History is the last screen in the order",
      CF.ORDER[-1] == CF.HISTORY, str(CF.ORDER[-1]))
check("History returns to the case it started on",
      "_open_case(" in inspect.getsource(CF._do_history))
# The brief is explicit that a click which merely did not throw is not a pass.
check("a View Changes click is asserted, not assumed",
      "_history_response(" in inspect.getsource(CF._check_view_changes)
      and "R.failed(" in inspect.getsource(CF._check_view_changes))
# The row click must be judged on NAVIGATION, not on the generic "something
# happened" — an expanding panel would not satisfy what was asked.
check("the row click is judged on navigation specifically",
      "navigated = " in inspect.getsource(CF._check_requests_history))
check("the 'i' icon is left alone, as the brief asks",
      "tds[0].dispatchEvent" in inspect.getsource(CF._check_requests_history))
# Each sub-check is wrapped, so one failing cannot hide the other's result.
check("each History sub-check is guarded on its own",
      "except Exception" in inspect.getsource(CF._do_history)
      and "_check_view_changes" in inspect.getsource(CF._do_history)
      and "_check_requests_history" in inspect.getsource(CF._do_history))

# eCIB Details covers two routes behind one entry, so the entry point has to
# call BOTH of them — and has to survive the first one failing. Without the
# per-route guard, an Add dialog that would not open would take both upload
# cases with it and report nothing about either.
_ecib_entry = inspect.getsource(CF._do_ecib_details)
check("the eCIB check runs the Add route and then the Upload route",
      "_do_ecib_add" in _ecib_entry and "_do_ecib_upload" in _ecib_entry
      and _ecib_entry.index("_do_ecib_add") < _ecib_entry.index("_do_ecib_upload"))
check("each eCIB route is guarded so one cannot take the other down",
      "except Exception" in _ecib_entry and "R.BLOCKED" in _ecib_entry)
# The upload route reaches the SAME sidebar entry as the Add route, so it must
# not try to click a menu item called 'eCIB Details (Upload)' — the case menu
# has no such thing. That string is a REPORTING label only.
check("the upload route opens the real sidebar entry, not its report name",
      "_open_screen(s, SCREEN_LABEL[ECIB_DETAILS], step)"
      in inspect.getsource(CF._do_ecib_upload_one),
      CF.ECIB_UPLOAD_LABEL)
check("the two eCIB routes never share a reporting line",
      CF.ECIB_UPLOAD_LABEL != CF.SCREEN_LABEL[CF.ECIB_DETAILS]
      and len({CF._upload_screen(c) for c in CF.UPLOAD_CASES}) == 2)
check("the upload route never re-uses the Add route's dialog path",
      "_add_ecib_record" not in inspect.getsource(CF._do_ecib_upload)
      and "_add_ecib_record" not in inspect.getsource(CF._do_ecib_upload_one))

# ---- PR Checklist -----------------------------------------------------
# press_action is a SECOND door to clicking things, so it has to carry the
# same guarantee commit() does. If it ever stopped honouring NEVER, a flow
# could reach Approve or Delete by asking for them as "actions".
_pa = inspect.getsource(W.Filler.press_action)
check("press_action still refuses the NEVER list",
      "NEVER" in _pa and "WriteRefused" in _pa)
# Behaviour, not wording: press_action must not consult the commit allowlist
# (that is commit()'s job) and must not have grown the list either. Checked
# this way because the docstring names COMMITTABLE in order to explain that it
# deliberately does NOT touch it, and a grep cannot tell those apart.
check("press_action does not go through the commit allowlist",
      "_committable(" not in _pa)
# The button must be pressed ONCE. The dispatch fallback exists for a click
# the overlay swallowed, and it may only fire when the click provably never
# reached the element — measured on the element, never inferred from the page,
# because on a control that toggles two presses are the same as none.
check("press_action presses once — the fallback is gated on a measured count",
      "__pressCount" in _pa and "if landed:" in _pa
      and _pa.index("__pressCount") < _pa.index("dispatchEvent"))
check("press_action assumes a click landed when it cannot tell",
      "landed = 1" in _pa)
check("the commit allowlist still holds only the ten save-ish labels",
      len(W.COMMITTABLE) == 10
      and not any(re.search(p, "generate") for p in W.COMMITTABLE)
      and not any(re.search(p, "perform pr") for p in W.COMMITTABLE),
      f"{len(W.COMMITTABLE)}: {W.COMMITTABLE}")
for _never in ("Approve", "Reject", "Delete", "Forward"):
    try:
        W.Filler(session=None, screen="x").press_action(_never)
        _refused = False
    except W.WriteRefused:
        _refused = True
    except Exception:            # noqa: BLE001 - any other failure is not a pass
        _refused = False
    check(f"press_action refuses {_never!r}", _refused)

# Generate BEFORE Save is the whole point of the ordering, and reversing it
# would store a record whose computed columns were never filled in.
_pr = inspect.getsource(CF._do_pr_checklist)
check("the PR flow generates before it saves",
      "_pr_generate" in _pr and "_pr_save" in _pr
      and _pr.index("_pr_generate") < _pr.index("_pr_save"))
check("the PR flow snapshots what Generate produced",
      "snapshot" in _pr and "_verify_pr_factors" in _pr)
check("Edit stays out of the PR happy path",
      "_pr_edit" not in _pr and hasattr(CF, "_pr_edit"))

# The three computed columns are the app's own output. Typing into them would
# be testing the test rather than the screen.
_fill_pr = inspect.getsource(CF._fill_pr_factors)
check("the PR flow never writes to the computed columns",
      not any(f"set_factor({c!r}" in _fill_pr for c in CF.PR_SYSTEM_COLUMNS)
      and "PR_SYSTEM_COLUMNS" not in _fill_pr,
      str(CF.PR_SYSTEM_COLUMNS))
check("the computed columns are named once, for reading only",
      len(CF.PR_SYSTEM_COLUMNS) == 2
      and "PR_SYSTEM_COLUMNS" in inspect.getsource(CF._pr_snapshot),
      str(CF.PR_SYSTEM_COLUMNS))
# 'Actual Value' must NOT be treated as computed: two factors carry their only
# writable input in that column, and freezing it out would silently skip them.
check("'Actual Value' is not classed as a computed column",
      not any("actual value" in c.lower() for c in CF.PR_SYSTEM_COLUMNS))

# The sections are configuration. Authoring them would under-test the screen
# silently the first time one was added — so the fill loop must iterate what
# it DISCOVERS. Pinned values are allowed, but only as values for factors
# found on the page, never as the list of factors to look for.
check("PR factors are discovered from the page, not authored",
      "factor_rows()" in _fill_pr
      and "for r in rows" in _fill_pr
      and "PR_FACTOR_VALUES" not in _fill_pr.split("for r in rows")[0]
          .split("rows = f.factor_rows()")[-1],
      f"{len(CF.PR_FACTOR_VALUES)} pinned value(s)")
# A free-text factor must be pinned, because set_factor refuses to invent a
# number for one — see the note above PR_FACTOR_VALUES.
check("the free-text Linkage factors carry a pinned value",
      any("linkage" in k for k in CF.PR_FACTOR_VALUES),
      str(list(CF.PR_FACTOR_VALUES)))
check("set_factor refuses to invent a value for a free-text factor",
      "is not guessed at" in inspect.getsource(W.Filler.set_factor))
check("a frozen Factor Value is skipped, not failed",
      "FrozenField" in _fill_pr and "frozen field" in _fill_pr)
check("FrozenField is not an ordinary FillError to callers",
      issubclass(W.FrozenField, W.FillError)
      and W.FrozenField is not W.FillError)
# A form that never opens must say so ONCE rather than as a cascade of
# failures about fields that were never on screen.
check("a PR form that will not open reports once and stops",
      "_open_pr_form" in _pr and "return" in _pr
      and "R.blocked(" in _pr)

# Every pass has to have its own name: it is what each check on that pass is
# called, and two passes sharing one makes a failure unattributable.
_all_passes = (CF.REQUEST_DETAILS_PASSES + CF.FACILITY_PASSES
               + CF.OBSERVATION_PASSES + CF.LITIGATION_PASSES
               + CF.SHARIAH_PASSES + CF.CRMD_NOTE_PASSES
               + CF.CREDIT_MEMORANDUM_PASSES + CF.GROUP_REVIEW_PASSES
               + [CF.BANK_LIMIT_PASS, CF.BANK_DETAIL_PASS, CF.ECIB_PASS])
_names = [p.name for p in _all_passes]
_dupes = sorted({n for n in _names if _names.count(n) > 1})
check("every case-screen pass has its own name", not _dupes,
      f"repeated: {_dupes}" if _dupes else f"{len(_names)} passes")
check("no field is authored without a label",
      all(f.labels and all(f.labels) for p in _all_passes for f in p.fields))

# The fields the FSD marks mandatory for these screens. Dropping one means the
# screen silently stops saving, or saves a record missing what the
# specification requires.
#
# Matched across a field's ALTERNATIVE names, not just its first. The primary
# name is the label the application actually renders — "Facility Purpose",
# "Proposed Expiry" — and the specification's wording is kept behind it, since
# the two disagree on most of these fields and only one of them is what the DOM
# will answer to.
FSD_MANDATORY = {
    "Request Details": ["Proposed Expiry Date"],
    "Request Details — Purpose of Request table": ["Request Type",
                                                   "Purpose/details of request"],
    "Facilities — Facility Request Details": ["Facility Expiry Date",
                                              "Purpose of Facility"],
    "Facilities — Limits and Exposures": ["Limit In Base CCY"],
    "Facilities — Payment": ["Timely"],
    "Facilities — Participant Banks Details": [
        "Participant Bank", "Participant Bank Syndicated Limit"],
}
for pass_name, required in FSD_MANDATORY.items():
    spec = next((p for p in _all_passes if p.name == pass_name), None)
    if spec is None:
        check(f"{pass_name} is authored", False, "no such pass")
        continue
    have = {name for f in spec.fields for name in f.labels}
    missing = [r for r in required if r not in have]
    check(f"{pass_name}: every mandatory field is authored", not missing,
          f"missing {missing}" if missing else f"{len(required)} mandatory")
    soft = [f.label for f in spec.fields
            if set(f.labels) & set(required) and f.optional and not f.marked]
    check(f"{pass_name}: no mandatory field is treated as optional", not soft,
          f"optional: {soft}" if soft else "")

# Labels observed on the live application, per pass. Each of these was filled
# by discovery on a run where the authored name found nothing, which is how the
# disagreement with the specification came to light — so the app's name is
# pinned FIRST and the FSD's is kept behind it.
OBSERVED_FIRST = {
    "Request Details": [
        ("Proposed Expiry", "Proposed Expiry Date"),
        ("Initiation Date", "Initiation date"),
    ],
    "Facilities — Facility Request Details": [
        ("Facility Purpose", "Purpose of Facility"),
        ("Facility Request Type", "Request Type (Facility)"),
        ("Facility Description", "Facility Details"),
    ],
}
for pass_name, pairs in OBSERVED_FIRST.items():
    spec = next(p for p in _all_passes if p.name == pass_name)
    for observed, from_fsd in pairs:
        fld = next((f for f in spec.fields if observed in f.labels), None)
        check(f"{pass_name}: {observed!r} is tried before {from_fsd!r}",
              fld is not None and fld.labels.index(observed)
              < (fld.labels.index(from_fsd) if from_fsd in fld.labels else 99),
              str(fld.labels) if fld else "field not authored")

# A field that was authored and then not found has to be REPORTED. Eleven of
# the twelve observation fields were skipped in silence and the run still said
# "7 passed / 0 failed / 0 blocked" — silence reading as success is the one
# failure mode this suite exists to prevent.
_fill_src = inspect.getsource(CF._fill_pass)
check("an authored field that is not on the screen is reported, not skipped",
      "every authored field is on the screen" in _fill_src
      and "missing.append" in _fill_src)

# The facility opens showing one tab and grows the rest once it is saved, so
# the strip has to be re-read rather than walked once.
_fac_src = inspect.getsource(CF._do_facilities)
check("the facility tab strip is re-read after each save",
      _fac_src.count("s.tab_strip(") >= 1 and "processed" in _fac_src
      and "for sweep in range" in _fac_src)

# The facility is not a form, it is a product request: nothing on it is
# enterable until the requested facility has been submitted with Proceed. Every
# control this flow will press to do that still has to clear widgets' own
# allowlist — a flow cannot talk it into anything else.
check("the facility request is submitted only with permitted controls",
      all(W._committable(x) for x in ("Proceed", "Add", "Save", "OK", "Select")))
OPENERS = ["Add", "Add Facility", "Request Facility", "New Facility",
           "Add Observation"]
check("the buttons that open these forms all clear the crawler's denylist",
      all(cr.is_safe_to_click(x) for x in OPENERS),
      str([x for x in OPENERS if not cr.is_safe_to_click(x)]))

# The observed control on the Requested Facility dialog is an inline treeview —
# a "Select option" toggle over a searchable tree — not an ng-select and not a
# magnifier lookup. It is tried FIRST because it is what the dialog uses.
check("widgets can drive an inline treeview dropdown",
      hasattr(W.Filler, "choose_in_tree"))
check("the tree picker skips group nodes rather than clicking them",
      "ngx-treeview-item" in inspect.getsource(W.Filler._first_leaf))
check("the requested facility is tried as a tree before anything else",
      inspect.getsource(CF._choose_requested_facility).index("choose_in_tree")
      < inspect.getsource(CF._choose_requested_facility).index(
          "choose_in_dialog"))
# The product decides which tabs the facility then has, so it is named rather
# than taken first: this environment's tree starts at a treasury instrument
# that carries neither Overdues nor a profit structure.
check("a funded facility is asked for by name",
      CF.FACILITY_PREFERENCE and CF.FACILITY_PREFERENCE[0] == "Running Finance",
      str(CF.FACILITY_PREFERENCE[:3]))

# The five tabs asked for by name must each be filled from their own section.
FIVE = ["Facility Request Details", "Limit and Exposure", "Facility Details",
        "Profit / Rental / Service Charges Structure", "Overdues"]
_unpaired = [t for t in FIVE if CF._pass_for_tab(t) is None]
check("all five named facility tabs pair with a section", not _unpaired,
      str(_unpaired) if _unpaired else f"{len(FIVE)} tabs")

# Observations is a side PANEL behind an "Add Observation" button, not a
# linkage table: add_row's '+' finds nothing there, and treating it as a table
# reports a missing add-row form on a screen that never had one.
_obs = CF.OBSERVATION_PASSES[0]
check("Observations is filled as a form, not as an add-row table",
      _obs.kind == CF.FORM, _obs.kind)
check("Observations has its own opener", bool(CF._ADD_OBSERVATION))
check("the observation's marker goes in Title, which the list shows",
      [f.label for f in _obs.fields if f.marked] == ["Title"],
      str([f.label for f in _obs.fields if f.marked]))

# Every field on the Add Observation panel, in the order it renders them. This
# is the whole panel, not a sample: a field dropped from here is a field that
# silently stops being filled and stops being verified.
OBSERVATION_PANEL = [
    "Title", "Date of audit visit", "Region Response", "Date of audit",
    "Observation", "Complied", "Comments", "Report Date", "Cutoff Date",
    "Overall risk", "Type of Audit", "Recommendation by BRR",
]
_authored = [f.label for f in _obs.fields]
check("every field on the observation panel is authored, in panel order",
      _authored == OBSERVATION_PANEL,
      f"authored {_authored}")

# Anchoring is label[title="..."], and a CSS attribute match is case-sensitive,
# so a tidied-up spelling finds nothing. Both are offered for the two the panel
# writes in sentence case.
for label, other in [("Overall risk", "Overall Risk"),
                     ("Type of Audit", "Type of audit")]:
    spec = next(f for f in _obs.fields if f.label == label)
    check(f"{label!r} also tries {other!r}", other in spec.labels,
          str(spec.labels))

# Each free-text box gets its OWN sentence. Identical text in three boxes makes
# the round trip vacuous — any box would match any other.
_texts = [f.value for f in _obs.fields if f.value and not f.marked]
check("each observation text box gets its own text",
      len(set(_texts)) == len(_texts),
      f"{len(set(_texts))} distinct of {len(_texts)}")


# --------------------------------------------------------------------------
# Litigation
#
# Same guarantees as the observation panel, for the same reasons. The form is
# reached from the screen's '+' rather than from a named button, and that is
# the one thing about it that is discovered rather than declared.
# --------------------------------------------------------------------------
_lit = CF.LITIGATION_PASSES[0]
check("Litigation is filled as a form, not as an add-row table",
      _lit.kind == CF.FORM, _lit.kind)
check("Litigation has its own opener", bool(CF._ADD_LITIGATION))
check("the '+' is a fallback for the named button, not the only route",
      "collect_action_buttons" in inspect.getsource(CF._open_add_litigation)
      and "add_row" in inspect.getsource(CF._open_add_litigation))
# add_row reports False unless a '.modal.show' arrives, which is right for a
# linkage table and wrong for a form that opens as a panel — the click still
# worked. So the form is confirmed by its FIELDS, exactly as the observation
# panel is.
check("the litigation form is confirmed by its fields, not by a modal",
      "_form_is_open" in inspect.getsource(CF._open_add_litigation)
      and "field_labels" in inspect.getsource(CF._form_is_open))

# Every field on the litigation entry form, in the order it renders them. This
# is the whole form, not a sample: a field dropped from here is a field that
# silently stops being filled and stops being verified.
# Spelled as the FORM spells them, which is not always how the requirement
# spells them: the form says "Law Firm Name", capital F. Both readings are
# authored and the app's goes first, but this list is the form's.
LITIGATION_FORM = [
    "Type of Suit", "Date Of Suit Filing", "Relevant Court",
    "Bank's Lawyer Name", "Law Firm Name", "Last Date of Hearing",
    "Next Date of Hearing", "Suit Amount", "Proceeding Details",
    "Date Of Decree",
]
_lit_authored = [f.label for f in _lit.fields]
check("every field on the litigation form is authored, in form order",
      _lit_authored == LITIGATION_FORM, f"authored {_lit_authored}")

# The two fields that make a litigation record a record. Everything else is a
# note if this deployment does not have it; without these there is nothing to
# store, and the form is expected to refuse the save and say so.
for _label in ["Type of Suit", "Date Of Suit Filing"]:
    _f = next(f for f in _lit.fields if f.label == _label)
    check(f"{_label!r} is treated as mandatory", not _f.optional)

# Anchoring is label[title="..."], and a CSS attribute match is case-sensitive,
# so a tidied-up spelling finds nothing. Both readings are offered for the
# labels whose casing the form is inconsistent about.
for _label, _other in [("Date Of Suit Filing", "Date of Suit Filing"),
                       ("Last Date of Hearing", "Last Date Of Hearing"),
                       ("Next Date of Hearing", "Next Date Of Hearing"),
                       ("Date Of Decree", "Date of Decree"),
                       ("Bank's Lawyer Name", "Banks Lawyer Name"),
                       ("Law Firm Name", "Law firm Name")]:
    _f = next(f for f in _lit.fields if f.label == _label)
    check(f"{_label!r} also tries {_other!r}", _other in _f.labels,
          str(_f.labels))

# Read off the live form, so the app's spelling is pinned ahead of the
# requirement's — the same rule as OBSERVED_FIRST above, for the same reason.
_f = next(f for f in _lit.fields if "Law Firm Name" in f.labels)
check("'Law Firm Name' is tried before 'Law firm Name'",
      _f.labels.index("Law Firm Name") < _f.labels.index("Law firm Name"),
      str(_f.labels))
# The '+' above the LITIGATION DETAILS grid is labelled "Add Details" on this
# build. It was found by the catch-all "add" before that was known, which is
# luck rather than intent; the bare "add" must stay last, since it matches
# every other name in the list.
check("the observed opener label is tried first",
      CF._ADD_LITIGATION[0] == "add details", str(CF._ADD_LITIGATION[:2]))
check("the catch-all 'add' is tried last",
      CF._ADD_LITIGATION[-1] == "add", str(CF._ADD_LITIGATION[-2:]))

# The marker goes in the rich-text box: it is the only field here with no
# length limit to truncate it, and proving THAT value came back is the only
# proof the editor's content survives a save.
check("the litigation marker goes in Proceeding Details",
      [f.label for f in _lit.fields if f.marked] == ["Proceeding Details"],
      str([f.label for f in _lit.fields if f.marked]))

# A numeric box will not take prose, and set_value refuses it outright rather
# than letting the app reject it later with no message.
_amount = next(f for f in _lit.fields if f.label == "Suit Amount")
check("the suit amount is a number",
      re.match(r"^\d+$", str(_amount.value or "")), repr(_amount.value))

_lit_texts = [f.value for f in _lit.fields if f.value and not f.marked
              and not str(f.value).isdigit()]
check("each litigation text box gets its own text",
      len(set(_lit_texts)) == len(_lit_texts),
      f"{len(set(_lit_texts))} distinct of {len(_lit_texts)}")

# Filed, then heard, then heard again, then decreed. A screen that validates
# one hearing against another would refuse any other order, and that refusal
# would read as "the automation could not set the field".
_lit_dates = {f.label: f.when for f in _lit.fields if f.when}
_chrono = ["Date Of Suit Filing", "Last Date of Hearing",
           "Next Date of Hearing", "Date Of Decree"]
check("the litigation dates run filed -> heard -> next -> decreed",
      all(_lit_dates[a] < _lit_dates[b]
          for a, b in zip(_chrono, _chrono[1:])),
      str({k: str(_lit_dates[k]) for k in _chrono}))
check("the suit was filed in the past and its next hearing is not",
      _lit_dates["Date Of Suit Filing"].year <= 2026
      and _lit_dates["Next Date of Hearing"].year >= 2027,
      str({k: str(v) for k, v in _lit_dates.items()}))
# Discovery must agree with the authored dates about which way each one points,
# or a field found rather than authored gets a date from the wrong end of the
# case.
check("a discovered 'Next Date of Hearing' is set in the future",
      CF._auto_date("Next Date of Hearing").year >= 2027)
check("a discovered 'Last Date of Hearing' is not",
      CF._auto_date("Last Date of Hearing").year <= 2026,
      str(CF._auto_date("Last Date of Hearing")))

# Litigation needs its OWN comparison, and a live run is why. Its grid shows
# five columns for a record of ten fields, and the row's detail opens as a
# MODAL — appended outside [data-crawl-root], which is the scope
# flows._screen_text reads. So the generic row-detail pass opened the record
# and then read the grid sitting behind it, reporting four values as dropped
# by the application when nothing had looked at them.
_verify_src = inspect.getsource(CF.verify_case_entries)
check("Litigation is verified by its own comparison, not the generic one",
      "SCREEN_LABEL[LITIGATION]" in _verify_src
      and "_verify_litigation(s, res, items, say)" in _verify_src)
_vlit = inspect.getsource(CF._verify_litigation)
check("the litigation record is re-opened before its fields are compared",
      "_open_our_litigation" in _vlit
      and _vlit.index("_open_our_litigation") < _vlit.index("_compare("),
      "opens the record, then compares through the open dialog")
# The marker is read with value_of, not out of any text scrape. innerText
# contains neither an <input>'s value nor anything inside a TinyMCE <iframe>,
# so a dialog full of correctly restored values reads back as nothing but its
# labels — which is how the second live run still failed to find its own
# record after the first fix.
_our = inspect.getsource(CF._open_our_litigation)
check("the run's own record is identified with value_of",
      "value_of" in _our
      and "_dialog_text" not in _our and "_screen_text" not in _our)
check("which field carries the marker is taken from the pass, not re-named",
      "fl.marked" in _our and "LITIGATION_PASSES" in _our)
check("_dialog_text reads the modal, which _screen_text cannot see",
      "modal" in inspect.getsource(CF._dialog_text))
# Falling back to the first row would compare this run's values against
# somebody else's suit and call them lost.
check("rows are opened until the run's own record is found",
      "row_opener_count" in inspect.getsource(CF._open_our_litigation)
      and "leave_row_detail" in inspect.getsource(CF._open_our_litigation))
# Save is judged by the grid gaining a row, not by the absence of a toast: this
# form closes on save whether or not it stored anything.
_lit_src = inspect.getsource(CF._do_litigation)
check("a saved litigation record is confirmed by the grid growing",
      "_grid_rows" in _lit_src and "appears in the list" in _lit_src)
check("a modal left open by the form is closed before the grid is counted",
      _lit_src.index("_close_modal") < _lit_src.index("after = "))


# --------------------------------------------------------------------------
# Shariah Comments
#
# One rich-text editor, SCD Remarks, and a Save. The smallest screen here,
# and the one with the least to go on: no list and no grid gains an entry, so
# the ROUND TRIP is the only evidence that Save stored anything.
# --------------------------------------------------------------------------
check("Shariah Comments is one pass", len(CF.SHARIAH_PASSES) == 1,
      str([p.name for p in CF.SHARIAH_PASSES]))
_shar = CF.SHARIAH_PASSES[0]
check("Shariah Comments is filled as a form, not as an add-row table",
      _shar.kind == CF.FORM, _shar.kind)
check("Shariah Comments has exactly one field",
      len(_shar.fields) == 1, str([f.label for f in _shar.fields]))

_scd = _shar.fields[0]
check("that field is SCD Remarks", _scd.label == "SCD Remarks", _scd.label)
# The only field on the screen. A run that cannot set it has exercised
# nothing, so "could not check" there would be silence reading as success.
check("SCD Remarks is treated as mandatory", not _scd.optional)
# Text that is the same on every run would match a previous run's copy and
# prove nothing. With one field, that field has to be the marked one.
check("SCD Remarks carries the run marker", _scd.marked)
# Anchored on label[title="..."], and a CSS attribute match is case-sensitive.
for _other in ["SCD remarks", "Scd Remarks"]:
    check(f"'SCD Remarks' also tries {_other!r}", _other in _scd.labels,
          str(_scd.labels))

# Nothing to open, nothing to count — and no second path invented for a
# one-field screen. Everything that makes the result trustworthy is in
# _fill_pass, which this calls like every other screen does.
_shar_src = inspect.getsource(CF._do_shariah_comments)
check("Shariah Comments needs no opener and counts no rows",
      "_fill_pass" in _shar_src
      and not any(n in _shar_src for n in ("_grid_rows", "add_row",
                                           "collect_action_buttons",
                                           "_open_add")),
      "opens the screen and runs the pass")
check("Shariah Comments is verified through the generic screen comparison",
      "SCREEN_LABEL[SHARIAH_COMMENTS]" not in _verify_src)


# --------------------------------------------------------------------------
# RMG Memo
#
# Eight narrative editors and a Save. The rule that matters on a screen of
# many identical-looking boxes is the one BBFS Details taught: every box gets
# its OWN text, and that text NAMES ITS OWN BOX. The same sentence in eight
# editors makes the round trip vacuous — any box would match any other.
# --------------------------------------------------------------------------
check("RMG Memo is one pass", len(CF.CRMD_NOTE_PASSES) == 1,
      str([p.name for p in CF.CRMD_NOTE_PASSES]))
_crmd = CF.CRMD_NOTE_PASSES[0]
check("RMG Memo is filled as a form, not as an add-row table",
      _crmd.kind == CF.FORM, _crmd.kind)

CRMD_NOTE_FORM = [
    "Recommendation",
    "Industry Strategy",
    "Internal Indicators (including business reciprocity / account turnover / "
    "overdue history, etc)",
    "Internal & External Audit Observations",
    "Return on Capital / Hurdle Rate",
    "Key Credit Concerns / Additional Information",
    "Risk Observations / Covenant Compliance Status",
    "Risk Exposure Strategy",
]
_crmd_authored = [f.label for f in _crmd.fields]
check("all eight RMG Memo editors are authored, in screen order",
      _crmd_authored == CRMD_NOTE_FORM, f"authored {_crmd_authored}")

# Each box's own text, and each naming its own box. Both halves matter: eight
# distinct sentences that did not say which section they belonged to would
# still leave the round trip unable to attribute what came back.
_crmd_texts = [f.value for f in _crmd.fields if f.value and not f.marked]
check("each RMG Memo editor gets its own text",
      len(set(_crmd_texts)) == len(_crmd_texts),
      f"{len(set(_crmd_texts))} distinct of {len(_crmd_texts)}")
_unnamed = [f.label for f in _crmd.fields
            if f.value and not f.marked
            and CF._norm(f.label.split("(")[0])[:18] not in CF._norm(f.value)]
check("each RMG Memo sentence names its own section", not _unnamed,
      f"do not: {_unnamed}" if _unnamed else f"{len(_crmd_texts)} sections")
# Every value has to be searchable in a screen's text, or the round trip's
# verdict on it can only be "cannot tell".
_short = [f.label for f in _crmd.fields
          if f.value and not flows._distinctive(f.value)]
check("every RMG Memo value is distinctive enough to verify", not _short,
      str(_short))

check("the RMG Memo marker goes in Recommendation",
      [f.label for f in _crmd.fields if f.marked] == ["Recommendation"],
      str([f.label for f in _crmd.fields if f.marked]))

# The long one is 94 characters, which is OVER the 90-character cap in
# widgets._visible_labels — so discovery would never offer it. It is fillable
# only because it is authored by name, and a shorter reading is kept behind it
# for a build that truncates the label. This is pinned because a future tidy-up
# of that field list would silently stop it being filled AND stop it being
# discovered, which is a field that vanishes without a trace.
_long = next(f for f in _crmd.fields
             if f.label.startswith("Internal Indicators"))
check("the long label is beyond what discovery would offer",
      len(_long.label) > 90, f"{len(_long.label)} characters")
check("so a shorter reading of it is authored too",
      "Internal Indicators" in _long.labels[1:], str(_long.labels))
# '&' normalises to ' and ', but the title-attribute anchor is literal, so
# both readings are offered.
_amp = next(f for f in _crmd.fields if "&" in f.label)
check("the ampersand label is also tried spelled out",
      any(" and " in x for x in _amp.labels[1:]), str(_amp.labels))

# Same as Shariah Comments: nothing to open, nothing to count, no second path.
_crmd_src = inspect.getsource(CF._do_crmd_note)
check("RMG Memo needs no opener and counts no rows",
      "_fill_pass" in _crmd_src
      and not any(n in _crmd_src for n in ("_grid_rows", "add_row",
                                           "collect_action_buttons",
                                           "_open_add")),
      "opens the screen and runs the pass")
check("RMG Memo is verified through the generic screen comparison",
      "SCREEN_LABEL[CRMD_NOTE]" not in _verify_src)


# --------------------------------------------------------------------------
# Credit Memorandum
#
# Thirty-four narrative editors — the largest screen in the case. At this
# size the text per box is BUILT by _memo_note rather than written out, so
# that "every box gets its own text, naming its own box" holds by
# construction instead of by proof-reading thirty-four sentences.
# --------------------------------------------------------------------------
check("Credit Memorandum is one pass", len(CF.CREDIT_MEMORANDUM_PASSES) == 1,
      str([p.name for p in CF.CREDIT_MEMORANDUM_PASSES]))
_memo = CF.CREDIT_MEMORANDUM_PASSES[0]
check("Credit Memorandum is filled as a form, not as an add-row table",
      _memo.kind == CF.FORM, _memo.kind)
# Thirty-three, not thirty-four — settled by a live run. Read as prose the
# section list looks like thirty-four, but "Major Obligor, Industry &
# Transaction Risks" is ONE combined heading on the screen: authored as two,
# neither was found and discovery filled the real editor.
check("all thirty-three Credit Memorandum editors are authored",
      len(_memo.fields) == 33, f"{len(_memo.fields)} authored")

# The screen's sections, in the order it lays them out. A name dropped from
# here is an editor that silently stops being filled and stops being
# verified — and on a screen of thirty-four, nobody would notice.
CREDIT_MEMORANDUM_FORM = [
    "Company / Shareholder’s Background",
    "Nature / Rationale of Request",
    "Financial Observations",
    "Financial Projections - Base Case (Long Term Only)",
    "Financial Projections - Scenario Analysis (Long Term Only)",
    "Variance Analysis (Long Term Only)",
    "Industry Strategy",
    "Internal Indicators (including business reciprocity / account turnover / "
    "overdue history, etc)",
    "Internal & External Audit Observations",
    "Return on Capital / Hurdle Rate",
    "Environmental and Social Risk Assessment",
    "Key Credit Concerns / Additional Information",
    "Risk Observations / Covenant Compliance Status",
    "Risk Advice",
    "Industry Key Success Factors & Updates",
    "Ways Out Analysis",
    # One heading, not two sections. The live application settled it.
    "Major Obligor, Industry & Transaction Risks",
    "Analysis of Obligor’s Related Parties Transactions",
    "Repayment Record",
    "Company Brief / History",
    "RELATIONSHIP - (Credit History and Account Conduct to be enclosed as "
    "per Appendix B)",
    "Ancillary Business (Company & Group)",
    "Key Drivers of Borrower’s Business and Key Risks Associated",
    "BUSINESS AND THEIR RISK MITIGANTS",
    "Rationale (Justification)",
    "Credit History & Account Conduct",
    "Other Requests",
    "DETAILS OF PAYMENT SCHEDULE IF TERM LOAN SOUGHT",
    "LATEST INCOME TAX / WEALTH TAX FORM TO BE SUBMITTED BY THE BORROWER",
    "Collateral Evaluation / Justification",
    "Collateral Justification",
    "Governance",
    "Discretionary Approval (Proposed)",
]
_memo_authored = [f.label for f in _memo.fields]
check("every Credit Memorandum section is authored, in screen order",
      _memo_authored == CREDIT_MEMORANDUM_FORM,
      "differs at: " + str([(a, b) for a, b in
                            zip(_memo_authored, CREDIT_MEMORANDUM_FORM)
                            if a != b][:3])
      or f"{len(_memo_authored)} sections")

_memo_texts = [f.value for f in _memo.fields if f.value and not f.marked]
check("each Credit Memorandum editor gets its own text",
      len(set(_memo_texts)) == len(_memo_texts),
      f"{len(set(_memo_texts))} distinct of {len(_memo_texts)}")
_unnamed = [f.label for f in _memo.fields
            if f.value and not f.marked and "Section:" not in f.value]
check("each Credit Memorandum sentence names its own section", not _unnamed,
      f"do not: {_unnamed}" if _unnamed else f"{len(_memo_texts)} sections")
_short = [f.label for f in _memo.fields
          if f.value and not flows._distinctive(f.value)]
check("every Credit Memorandum value is distinctive enough to verify",
      not _short, str(_short))
# The text lands on a real obligor's memorandum in a shared environment.
# Whoever reads it next must not have to guess whether it is genuine.
_undisclaimed = [f.label for f in _memo.fields
                 if f.value and not f.marked
                 and "not a real credit assessment" not in f.value]
check("every Credit Memorandum note says it is not a real assessment",
      not _undisclaimed, str(_undisclaimed[:3]))

check("the Credit Memorandum marker goes in Nature / Rationale of Request",
      [f.label for f in _memo.fields if f.marked] ==
      ["Nature / Rationale of Request"],
      str([f.label for f in _memo.fields if f.marked]))

# Six labels appear on BOTH RMG Memo and Credit Memorandum. Their text must
# NOT match across the two screens: if the application stored one screen's
# Industry Strategy into the other's box, identical text would let both round
# trips pass, and the defect would be invisible.
_shared = ({f.label for f in _memo.fields}
           & {f.label for f in _crmd.fields})
check("the two narrative screens really do share labels",
      len(_shared) >= 6, f"{len(_shared)} shared: {sorted(_shared)[:3]}")
_collisions = [lbl for lbl in _shared
               if next(f.value for f in _memo.fields if f.label == lbl)
               and next(f.value for f in _memo.fields if f.label == lbl)
               == next(f.value for f in _crmd.fields if f.label == lbl)]
check("a section shared with RMG Memo gets different text on each screen",
      not _collisions, f"identical on: {_collisions}" if _collisions else
      f"{len(_shared)} shared label(s), all worded differently")

# Three labels carry a right single quotation mark rather than an ASCII
# apostrophe. label[title="..."] is a literal match, so ’ and ' are different
# characters there; both readings are authored for each.
_curly = [f for f in _memo.fields if "’" in f.label]
check("the curly-apostrophe labels are authored both ways",
      len(_curly) == 3
      and all(any("'" in x for x in f.labels[1:]) for f in _curly),
      str([f.label for f in _curly]))

# Same trap as RMG Memo's: 94 characters is past what discovery would offer.
_long = next(f for f in _memo.fields
             if f.label.startswith("Internal Indicators"))
check("the long label is beyond what discovery would offer",
      len(_long.label) > 90, f"{len(_long.label)} characters")
check("so a shorter reading of it is authored too",
      "Internal Indicators" in _long.labels[1:], str(_long.labels))

# Two sections whose names nearly contain one another. The text anchor
# matches a label EXACTLY once normalised, so neither can be reached by the
# other's name — but if that ever changed, one of them would be filled twice
# and the other never.
for _a, _b in [("Collateral Justification",
                "Collateral Evaluation / Justification"),
               ("Industry Strategy", "Industry Key Success Factors & Updates")]:
    check(f"{_a!r} and {_b!r} are separate sections",
          _a in _memo_authored and _b in _memo_authored
          and CF._norm(_a) != CF._norm(_b))

_memo_src = inspect.getsource(CF._do_credit_memorandum)
check("Credit Memorandum needs no opener and counts no rows",
      "_fill_pass" in _memo_src
      and not any(n in _memo_src for n in ("_grid_rows", "add_row",
                                           "collect_action_buttons",
                                           "_open_add")),
      "opens the screen and runs the pass")
check("Credit Memorandum is verified through the generic screen comparison",
      "SCREEN_LABEL[CREDIT_MEMORANDUM]" not in _verify_src)


# --------------------------------------------------------------------------
# Group Review
#
# Seven narrative editors and a Save. The screen is NOT on every case — it is
# in the sidebar of an Annual Renewal and absent from a Borrower Credit
# Application — so a run that cannot find it must say so rather than fail.
# --------------------------------------------------------------------------
check("Group Review is one pass", len(CF.GROUP_REVIEW_PASSES) == 1,
      str([p.name for p in CF.GROUP_REVIEW_PASSES]))
_grp = CF.GROUP_REVIEW_PASSES[0]
check("Group Review is filled as a form, not as an add-row table",
      _grp.kind == CF.FORM, _grp.kind)

GROUP_REVIEW_FORM = [
    "Group Background",
    "Group Companies",
    "Industry Details / Peer Analysis / Financial Analysis",
    "Trade Business through NBP",
    "Group Relationship Yield",
    "Group Relationship Strategy / Recommendation",
    "Risk Advice",
]
_grp_authored = [f.label for f in _grp.fields]
check("all seven Group Review editors are authored, in screen order",
      _grp_authored == GROUP_REVIEW_FORM, f"authored {_grp_authored}")

_grp_texts = [f.value for f in _grp.fields if f.value and not f.marked]
check("each Group Review editor gets its own text",
      len(set(_grp_texts)) == len(_grp_texts),
      f"{len(set(_grp_texts))} distinct of {len(_grp_texts)}")
_unnamed = [f.label for f in _grp.fields
            if f.value and not f.marked and "Section:" not in f.value]
check("each Group Review sentence names its own section", not _unnamed,
      str(_unnamed))
_short = [f.label for f in _grp.fields
          if f.value and not flows._distinctive(f.value)]
check("every Group Review value is distinctive enough to verify", not _short,
      str(_short))
check("the Group Review marker goes in the relationship recommendation",
      [f.label for f in _grp.fields if f.marked] ==
      ["Group Relationship Strategy / Recommendation"],
      str([f.label for f in _grp.fields if f.marked]))

# 'Risk Advice' is on Credit Memorandum too. Identical text on both screens
# would let a value stored against the wrong one pass BOTH round trips, so
# _group_note and _memo_note deliberately word it differently.
_shared_grp = ({f.label for f in _grp.fields}
               & {f.label for f in _memo.fields})
check("Group Review really does share a section with Credit Memorandum",
      "Risk Advice" in _shared_grp, str(sorted(_shared_grp)))
_collisions = [lbl for lbl in _shared_grp
               if next(f.value for f in _grp.fields if f.label == lbl)
               and next(f.value for f in _grp.fields if f.label == lbl)
               == next(f.value for f in _memo.fields if f.label == lbl)]
check("a section shared with Credit Memorandum is worded differently here",
      not _collisions, f"identical on: {_collisions}" if _collisions else
      f"{len(_shared_grp)} shared label(s)")
check("the two note builders do not produce the same sentence",
      CF._group_note("Risk Advice") != CF._memo_note("Risk Advice"))

# Same as the other narrative screens: nothing to open, nothing to count.
_grp_src = inspect.getsource(CF._do_group_review)
check("Group Review needs no opener and counts no rows",
      "_fill_pass" in _grp_src
      and not any(n in _grp_src for n in ("_grid_rows", "add_row",
                                          "collect_action_buttons",
                                          "_open_add")),
      "opens the screen and runs the pass")
check("Group Review is verified through the generic screen comparison",
      "SCREEN_LABEL[GROUP_REVIEW]" not in _verify_src)

# ---- a container must never span two fields -------------------------------
# The bug Group Review exposed, and the worst kind this suite can have: not a
# false failure but a WRONG WRITE. Its seven labels and seven TinyMCE editors
# are all direct children of ONE <fieldset class="form-group">, so
# `fieldset:has(> label[title="Group Background"])` matched that fieldset —
# and so did the same selector for the other six. Every read and every write
# went to the FIRST editor: six sections' text landed on top of each other in
# one box, and the run's own read-back caught it.
_block_src = inspect.getsource(W.Filler._block)
check("a candidate that spans several fields is refused",
      "_ONE_FIELD_JS" in _block_src,
      "the title-attribute anchor is guarded, not just the text one")
check("the guard ignores labels that are part of a control",
      "form-check-label" in W.Filler._ONE_FIELD_JS)
# Three anchors, and the flat-form one is LAST: it must never take precedence
# over a label that resolves to its own container.
check("the flat-form anchor is the last resort",
      _block_src.index("_STAMP_FIELD_JS")
      < _block_src.index("_STAMP_AFTER_LABEL_JS"))
_after = W.Filler._STAMP_AFTER_LABEL_JS
check("the flat-form anchor reads forward from the label, not by index",
      "DOCUMENT_POSITION_FOLLOWING" in _after and "wanted" in _after)
check("it stops at the next field's label",
      "labels[i + 1]" in _after and "p.contains(N)" in _after)
check("it widens only through wrappers that name no field",
      "own(l)" in _after and "break" in _after)
# A screen the case menu does not offer is BLOCKED with the sidebar's actual
# contents, not reported as a failure. That is _open_screen's NavigationError,
# caught per screen in fill_case_screens.
check("a screen missing from the case menu is blocked, not failed",
      "NavigationError" in _dispatch and "can be opened" in _dispatch)


# --------------------------------------------------------------------------
# Business Performance — two sub-menus, and not a form at all
#
# The first screen pair in the suite that is a GRID rather than fields, and
# the first reached as sub-menus of another sidebar entry. Both facts were
# read off the live screen, not assumed: the ten metric names are one
# <table>, the twenty numeric boxes are another, the boxes carry no label, no
# title, no formcontrolname and a duplicate id of "false" on every one of
# them, and the two sub-menus are TABS of Business Performance.
# --------------------------------------------------------------------------
BP_ROWS_EXPECTED = [
    "Mark-up Earned / Accrued",
    "Commission / Exchange Earned",
    "Other Fee / Income",
    "Total Income of Business Group",
    "Treasury Income",
    "Total Earnings of the Bank",
    "Average Funded Outstanding",
    "Borrowing Rate (%)",
    "Annualized Yield (%)",
    "Net Yield (%)",
]
check("all ten Business Performance rows are authored, in screen order",
      [r.label for r in CF.BP_ROWS] == BP_ROWS_EXPECTED,
      str([r.label for r in CF.BP_ROWS]))
# ONE menu entry, BOTH sub-menus. The entry must not quietly cover only one.
check("Business Performance is a single case screen",
      CF.SCREEN_LABEL[CF.BUSINESS_PERFORMANCE] == "Business Performance"
      and CF.BUSINESS_PERFORMANCE in CF.ORDER)
check("the one check covers both sub-menus",
      [s.name for s in CF.BP_SUB_MENUS]
      == ["Customer Business Performance", "Group Business Reciprocity"],
      str([s.name for s in CF.BP_SUB_MENUS]))
_bp_do = inspect.getsource(CF._do_business_performance)
check("both sub-menus are filled by the one flow",
      "for i, spec in enumerate(BP_SUB_MENUS)" in _bp_do
      and "_fill_bp_grid(" in _bp_do)
# One sub-menu failing must not take the other with it: they save separately.
# The loop has to be OUTSIDE the try, or the first failure ends both.
check("a sub-menu that cannot be opened does not abandon the other",
      "NavigationError" in _bp_do and "can be opened" in _bp_do
      and _bp_do.index("for i, spec") < _bp_do.index("try:")
      < _bp_do.index("except NavigationError"))
# Each keeps its OWN screen on every entry and check, which is what lets the
# round trip re-open the right sub-menu and the report say which one failed.
check("each sub-menu is reported under its own name",
      "screen = spec.name" in inspect.getsource(CF._fill_bp_grid))
check("both sub-menus live under Business Performance",
      [CF.SUB_MENU_PARENT.get(n) for n in (CF.BP_CUSTOMER_LABEL,
                                           CF.BP_GROUP_LABEL)]
      == [CF.BP_PARENT, CF.BP_PARENT], str(CF.SUB_MENU_PARENT))
check("the round trip knows how to re-open a nested screen",
      CF.SUB_MENU_PARENT.get("Customer Business Performance")
      == "Business Performance"
      and "_open_reported_screen" in inspect.getsource(CF.verify_case_entries))
# A sub-menu label must never collide with a registered screen label, or the
# round trip would try to open it as a top-level sidebar entry.
check("a sub-menu is not also registered as a top-level screen",
      not ({CF.BP_CUSTOMER_LABEL, CF.BP_GROUP_LABEL}
           & set(CF.SCREEN_LABEL.values())),
      str(sorted(CF.SCREEN_LABEL.values())))

# The three the application computes. Typing into one would fight the app and
# make its arithmetic uncheckable.
_computed = [r.label for r in CF.BP_ROWS if r.computed]
check("the three computed rows are marked as computed",
      _computed == ["Total Income of Business Group", "Annualized Yield (%)",
                    "Net Yield (%)"], str(_computed))
check("every computed row records the arithmetic it should follow",
      all(r.computed.strip() for r in CF.BP_ROWS if r.computed))
check("the arithmetic tolerance allows for the screen's own rounding",
      0 < CF._BP_TOLERANCE <= 0.05, str(CF._BP_TOLERANCE))

# ---- every figure unique to row, column, sub-menu and run ----------------
# The trap this pair sets: the SAME ten row names on both sub-menus. With the
# same figures on both, a value stored against the wrong sub-menu would
# satisfy both round trips and the run would report a mix-up as twenty passes.
_typed = {}
for _spec in CF.BP_SUB_MENUS:
    for _i, _r in enumerate(CF.BP_ROWS):
        if _r.computed:
            continue
        for _c in range(2):
            _typed[(_spec.name, _i, _c)] = _spec.value(_i, _c)
_vals = list(_typed.values())
check("no two Business Performance figures are the same",
      len(set(_vals)) == len(_vals),
      f"{len(set(_vals))} distinct of {len(_vals)}")
_cust, _grp_bp = CF.BP_SUB_MENUS
_same_across = [CF.BP_ROWS[i].label for i in range(len(CF.BP_ROWS))
                if not CF.BP_ROWS[i].computed
                and _cust.value(i, 0) == _grp_bp.value(i, 0)]
check("the two sub-menus never share a figure for the same row",
      not _same_across, str(_same_across))
_amounts = [v for v in _vals if "." not in v]
check("every Business Performance amount is distinctive enough to verify",
      all(flows._distinctive(v) for v in _amounts),
      str([v for v in _amounts if not flows._distinctive(v)]))
_rates = [v for v in _vals if "." in v]
check("every Business Performance rate is still a plausible percentage",
      _rates and all(0 < float(v) < 100 for v in _rates), str(_rates))
check("every figure is numeric — these boxes take nothing else",
      all(v.replace(".", "", 1).isdigit() for v in _vals))
check("no figure exceeds the 15 characters the boxes accept",
      all(len(v) <= 15 for v in _vals), str([v for v in _vals if len(v) > 15]))

# The run's own digits, and the bug that hid in them: padding on the RIGHT and
# then taking the last n returns the padding, so every run typed the same
# numbers and the round trip could be satisfied by the previous run's figures.
check("the run digits are padded on the left, so the clock survives",
      CF._bp_run_digits(4) == ("0000" + CF._BP_RUN)[-4:]
      and set(CF._bp_run_digits(4)) != {"0"},
      f"digits {CF._bp_run_digits(4)!r} from stamp {CF._BP_RUN!r}")
_digits_src = inspect.getsource(CF._bp_run_digits)
check("the run digits are read once, not per figure",
      "_stamp()" not in _digits_src and "_BP_RUN" in _digits_src,
      "reading the clock per figure splits one run across two numbers")
# These two screens carry no marker PHRASE — there is nowhere to type one —
# so nothing above should claim they do.
check("neither sub-menu pretends to carry a run-marked field",
      not any(getattr(r, "marked", False) for r in CF.BP_ROWS))

# ---- the grid is joined by NAME, never by a caller's index ---------------
# widgets is the only module allowed to type, and the only place positional
# targeting is permitted at all. What keeps it safe is that the position is
# never the caller's: it names a metric, and the index is derived and then
# checked.
_grid_js = W.Filler._GRID_JS
check("the grid is located by row name",
      "no row is named" in _grid_js and "norm(want)" in _grid_js)
check("two rows with one name are refused, not guessed between",
      "rows are named" in _grid_js)
check("the label and value tables must have the same number of rows",
      "valueRows.length === L.rows.length" in _grid_js
      and "as many rows as" in _grid_js)
check("two possible pairings are refused",
      "pairings are possible" in _grid_js)
check("a column beyond the row's width is refused",
      "there is no column" in _grid_js)
_cell_src = inspect.getsource(W.Filler.grid_cell)
check("writing a grid cell is gated by the host allowlist",
      "assert_writable()" in _cell_src)
check("a read-only cell is refused rather than typed into",
      "read-only" in _cell_src and "computes it" in _cell_src)
check("the cell is reached by a stamp, not by an nth-child index",
      "data-crawl-cell" in _cell_src and ":nth" not in _cell_src)
check("a column name that cannot be trusted becomes a plain number",
      "column " in inspect.getsource(CF._bp_columns)
      and "/\\d{4}/" in _grid_js)

# ---- the sub-menu opener must not match its own parent -------------------
# 'Business Performance' is contained in 'Customer Business Performance', and
# the driver matches menu labels by containment. Asking the sidebar for the
# child therefore SUCCEEDS by opening the parent, and the run then reports a
# screen it never reached. It worked by luck once — the child wanted was the
# parent's default tab.
_sub_src = inspect.getsource(CF._open_sub_screen)
check("the parent is opened first, before the child is looked for",
      _sub_src.index("label=parent") < _sub_src.index("_step_tab"))
check("the child is looked for as a tab of the parent",
      "_step_tab" in _sub_src and "satisfied_if_active=True" in _sub_src)
check("a containment match on the parent is not accepted as the child",
      "_norm(label) not in _norm(note)" in _sub_src
      and "again" in _sub_src)
check("a sub-menu that cannot be opened says what was tried",
      "could not be opened under" in _sub_src)

# ---- filled and verified through the grid, not through labels ------------
_bp_fill = inspect.getsource(CF._fill_bp_grid)
# The CALL, not the word: both of these functions explain in prose why they
# do not use the label-anchored path, and matching the prose passed a check
# that was meant to be about the code.
check("Business Performance is filled through the grid, not _fill_pass",
      "set_block_cell(" in _bp_fill and "_fill_pass(" not in _bp_fill)
check("it reports the rows it expected against the rows on the screen",
      "every row in the specification is on the screen" in _bp_fill)
check("it reads every figure back before saving",
      "holds every value that was typed into it" in _bp_fill
      and "block_cell_value" in _bp_fill)

# ---- the '+ Add' period, and not judging the app's arithmetic ---------
# Nothing is enterable until a period exists, so the fill has to make one.
check("Business Performance adds a period before filling anything",
      "_bp_add_period(" in _bp_fill
      and _bp_fill.index("_bp_add_period(") < _bp_fill.index("set_block_cell("))
_bp_add = inspect.getsource(CF._bp_add_period)
check("the period dialog's four fields are all set",
      all(x in _bp_add for x in ("Actual/Projected", "Year", "From", "To")))
check("the period is made with Proceed, and nothing is saved by it",
      'commit("Proceed")' in _bp_add
      and 'commit("Save")' not in _bp_add
      and "commit_block(" not in _bp_add)
check("a period that did not appear is a failure, not a silent pass",
      "the grid still shows" in _bp_add and "R.failed(" in _bp_add)
# Each period carries its own Save. commit() takes the leftmost, which is a
# period somebody else filled.
check("the period this run added is the one that gets saved",
      "commit_block(block)" in _bp_fill and 'f.commit("Save")' not in _bp_fill)
check("only the period this run added is written to",
      "block_cell(row.label, col, block)" in _bp_fill)
# The application's own totals are reported, never asserted.
_bp_verify_src = inspect.getsource(CF._verify_bp)
check("the round trip does not judge the application's arithmetic",
      "_verify_bp_arithmetic(" not in _bp_verify_src)
check("no caller judges the arithmetic any more",
      "_verify_bp_arithmetic(" not in inspect.getsource(CF).replace(
          "def _verify_bp_arithmetic(", ""))
check("a dry run on Business Performance saves nothing",
      "dry_run" in _bp_fill and "not saving" in _bp_fill)
check("both sub-menus are verified through the grid comparison",
      "BP_CUSTOMER_LABEL" in _verify_src and "BP_GROUP_LABEL" in _verify_src
      and "_verify_bp(" in _verify_src)
_bp_verify = inspect.getsource(CF._verify_bp)
check("the round trip reads the grid by row name, not by value_of",
      "block_cell_value(" in _bp_verify and "value_of(" not in _bp_verify)
check("a figure whose row or column has moved is blocked, not failed",
      "cannot be told from this" in _bp_verify)
_arith = inspect.getsource(CF._verify_bp_arithmetic)
check("a computed row the app has made writable is skipped, not asserted",
      "not read-only in this column" in _arith)
check("Net Yield is checked against the yield the screen shows",
      "rounded figure" in _arith)

# --------------------------------------------------------------------------
# Relationship with Other Banks / FIs
#
# The only screen here that CREATES the record it then fills, and the only one
# carrying a negative check — that the Add dialog refuses an empty Bank Name.
# --------------------------------------------------------------------------
_lim, _det = CF.BANK_LIMIT_PASS, CF.BANK_DETAIL_PASS
check("the Limits dialog is filled as an add-row table",
      _lim.kind == CF.LINKAGE and _lim.grid_heading == "LIMITS",
      f"{_lim.kind} / {_lim.grid_heading!r}")
check("the record's own page is filled as a form",
      _det.kind == CF.FORM, _det.kind)

LIMIT_FIELDS = ["Limit Type", "Total Limit", "Total Outstanding",
                "Outstanding Date", "Total OverDue", "Sub Limit", "FE Limits"]
check("all seven Limits fields are authored, in dialog order",
      [f.label for f in _lim.fields] == LIMIT_FIELDS,
      str([f.label for f in _lim.fields]))
DETAIL_FIELDS = ["Select Bank",
                 "Facility Details", "Security", "Pricing / Commission",
                 "Relationship Remarks",
                 "Facilities, Security, Markup / Commission, "
                 "Justification/Remarks",
                 "Expiry Date", "Classification Status"]
check("every detail field is authored, in screen order",
      [f.label for f in _det.fields] == DETAIL_FIELDS,
      str([f.label for f in _det.fields]))
# Select Bank must be AUTHORED, not discovered. Discovery fills an unauthored
# dropdown with its first option, which on another case is a different bank —
# so leaving it to discovery could change the bank of the record just created.
check("Select Bank is set to the bank that was chosen, not to a default",
      next(f.value for f in _det.fields if f.label == "Select Bank")
      == CF.BANK_NAME)
# 30/06/2027 is refused by this field — the same wrong date, from the same
# request, as Next Date of Hearing on Litigation. A date the app accepts is
# what lets the round trip say whether an expiry persists at all.
_expiry = next(f.when for f in _det.fields if f.label == "Expiry Date")
check("the expiry date avoids the date this app refuses",
      _expiry != CF.REVIEW and _expiry.year >= 2027, str(_expiry))
check("the expiry date is in the future, as an expiry must be",
      _expiry > CF.RECENT, str(_expiry))

# The marker has to be in Security: it is the one authored field that the LIST
# page renders as a column, which is what lets the round trip find the record
# this run created rather than opening the first row.
check("the marker is in Security, the field the list page shows",
      [f.label for f in _det.fields if f.marked] == ["Security"],
      str([f.label for f in _det.fields if f.marked]))
_open_ours = inspect.getsource(CF._open_our_bank_relationship)
check("the round trip opens THIS run's record, not the first row",
      "res.marker" in _open_ours and "leave_row_detail" in _open_ours)
# The list pages at five rows and the record this run creates lands on page
# two, so scanning what is on screen found two pre-existing relationships and
# reported fifteen values as unverifiable on a record one page away. Search
# first, page only as a fallback — the same lesson as My Bucket.
check("the list is searched for the marker before rows are scanned",
      "search_grid(" in _open_ours
      and _open_ours.index("search_grid(") < _open_ours.index("open_row_detail"))
check("a search box that cannot be used falls back to paging",
      "WriteRefused" in _open_ours and "searched = False" in _open_ours)
check("an empty search result clears the filter before paging",
      'search_grid("")' in _open_ours)
check("the report says whether the list was searched or only paged",
      "not just paged" in _open_ours)
check("which field carries the marker is read off the pass, not named again",
      "BANK_DETAIL_PASS.fields" in _open_ours and ".marked" in _open_ours)
check("the marker is read with value_of, not out of the page text",
      "value_of(" in _open_ours)

# The five aggregates are the application's, so nothing may be typed into
# them — and four of the five are checkable against the single limit row.
check("no aggregate figure is authored as a field to fill",
      not ({f.label for f in _det.fields} & set(CF.BANK_SUMMARY_FIELDS)),
      str({f.label for f in _det.fields} & set(CF.BANK_SUMMARY_FIELDS)))
check("four aggregates are checked against a source column",
      set(CF.BANK_SUMMARY_FROM) == {"Total Limits", "Total O/s.",
                                    "Total Overdue", "Total FE Limits"},
      str(sorted(CF.BANK_SUMMARY_FROM)))
check("every aggregate's source is a field on the Limits dialog",
      all(any(src in f.labels for f in _lim.fields)
          for src in CF.BANK_SUMMARY_FROM.values()),
      str([src for src in CF.BANK_SUMMARY_FROM.values()
           if not any(src in f.labels for f in _lim.fields)]))
# Wallet Share (%) compares this bank against the others, so it cannot be
# derived from one record. It is reported for confirmation, never asserted —
# asserting a rule nobody has confirmed is how a suite starts crying wolf.
check("Wallet Share is reported for confirmation, not asserted",
      "Wallet Share (%)" in CF.BANK_SUMMARY_FIELDS
      and "Wallet Share (%)" not in CF.BANK_SUMMARY_FROM)
_summary_src = inspect.getsource(CF._check_bank_summary)
check("the aggregate check says what it cannot derive",
      "needs confirming" in _summary_src)
check("the aggregates are checked on a record this run created",
      "exactly one limit row" in _summary_src)
# The limit amounts must differ from each other, or an aggregate reading the
# wrong column would still pass.
_amts = [f.value for f in _lim.fields if f.value and str(f.value).isdigit()]
check("the limit amounts are all different from each other",
      len(set(_amts)) == len(_amts), str(_amts))

# The negative check: an empty Bank Name must be refused.
_req_src = inspect.getsource(CF._check_bank_name_required)
check("the Add dialog is tested with an empty Bank Name",
      "Proceed" in _req_src and "bank name" in _req_src.lower())
check("a Proceed accepted on the empty form is a FAILURE",
      "R.failed(" in _req_src and "Proceed was accepted" in _req_src)
check("a refusal with no readable message is blocked, not passed",
      "R.BLOCKED" in _req_src and "said nothing this run could" in _req_src)
check("the empty-Proceed check proves nothing was created",
      "_bank_rows(" in inspect.getsource(CF._add_bank_relationship))

# Creating the record is the step that makes the rest possible, so a failure
# there must stop the screen rather than fill somebody else's record.
_add_src = inspect.getsource(CF._add_bank_relationship)
check("a relationship that cannot be created stops the screen",
      "return None" in _add_src)
_do_rel = inspect.getsource(CF._do_bank_relationships)
check("nothing is filled unless the relationship was created",
      "if not chosen:" in _do_rel and "return" in _do_rel)
check("the bank chosen in the dialog is checked on the record's page",
      "_check_selected_bank(" in _do_rel)
# Escape dismisses the Limits dialog through the shared add-row path, and
# Escape has closed more than a dialog on this application before.
check("the record's page is confirmed to survive the Limits dialog",
      "survives the Limits dialog" in _do_rel)
check("the limit row is added before the aggregates are read",
      _do_rel.index("BANK_LIMIT_PASS") < _do_rel.index("_check_bank_summary"))
check("the page is saved once, after everything is entered",
      _do_rel.index("_check_bank_summary")
      < _do_rel.index("BANK_DETAIL_PASS"))
check("the screen has its own round trip",
      "_verify_bank_relationship(" in _verify_src)

# ---- the limit row is compared column by column ----------------------
# A limit's values are table CELLS, not labelled fields, so _compare falls
# through to searching the page's text — and that search normalises
# punctuation away, so '10000000' never matches the rendered '10,000,000'.
# Four values were reported as dropped while the aggregates directly above
# them proved they were stored.
_vlim = inspect.getsource(CF._verify_bank_limits)
_vrel = inspect.getsource(CF._verify_bank_relationship)
check("the limit row is compared against the LIMITS table's own columns",
      "_verify_bank_limits(" in _vrel and "head" in _vlim)
check("the limit entries are told apart from the record's own fields",
      "BANK_LIMIT_PASS.name" in _vrel)
check("each cell is matched to its column by name, not by position",
      "_norm(h) == _norm(label)" in _vlim)
check("the comparison is the presentation-insensitive one",
      "_same_value(" in _vlim)
# The tempting fix was to make the text search digits-only. That would have
# been worse than the bug: '1000000' is a substring of '10000000', so Total
# Outstanding would match the Total Limit cell and a lost value could pass.
check("the text search was NOT loosened to compare digits only",
      "substring" in _vlim)
# Two of the seven fields are not columns of the table at all.
check("a field the table does not show is blocked, not failed",
      "has no" in _vlim and "R.BLOCKED" in _vlim)
check("FE Limits is pointed at the aggregate that does cover it",
      "Total FE Limits" in _vlim)
check("an empty LIMITS table after a saved row is a failure",
      "R.FAIL if table else R.BLOCKED" in _vlim)

# The bank is a parameter, as asked for, and overridable without a code edit.
check("the bank name is a parameter",
      CF.BANK_NAME and "LOS_BANK_NAME" in inspect.getsource(CF))

# ---- the Cancel helper -----------------------------------------------
# Kept for a cancel-flow test rather than used by the happy path. It clicks
# without the write gate, so nothing that could commit may be reachable.
check("a reusable Cancel helper exists", hasattr(W.Filler, "cancel_modal"))
check("its dismissal controls are a closed list",
      isinstance(W.Filler._DISMISS, tuple) and W.Filler._DISMISS)
check("nothing on that list can commit",
      not [w for w in W.Filler._DISMISS if W._committable(w)],
      str([w for w in W.Filler._DISMISS if W._committable(w)]))
_cancel_src = inspect.getsource(W.Filler.cancel_modal)
check("a committable label passed to Cancel is refused",
      "WriteRefused" in _cancel_src and "_committable(" in _cancel_src)
check("Cancel falls back to the generic dismissal rather than failing",
      "_close_modal()" in _cancel_src)

# ---- dropdown matching is whitespace-tolerant ------------------------
# The bank list offers 'Islamic  Bank' with two spaces, so asking for the name
# as anyone would write it matched nothing. Collapsing runs of whitespace on
# both sides is strictly more forgiving: anything that matched before still
# matches, and still matches first.
_choose_src = inspect.getsource(W.Filler.choose)
check("dropdown options are matched with whitespace collapsed",
      "_flat(" in _choose_src and "\\s+" in _choose_src)
check("an exact match still wins over a containment match",
      _choose_src.index("t == want") < _choose_src.index("want in t"))
check("'Islamic Bank' resolves to the app's 'Islamic  Bank'",
      re.sub(r"\s+", " ", "Islamic Bank".lower())
      in re.sub(r"\s+", " ", "Islamic  Bank".lower()))


# --------------------------------------------------------------------------
# eCIB Details
#
# The largest screen here: a three-field dialog, then fifty-three fields over
# six sections. Two facts about it were found by probing and contradict the
# specification, and both would have produced a wrong report if taken on
# trust — see the comments on each check.
# --------------------------------------------------------------------------
_ecib = CF.ECIB_PASS
check("eCIB Details is filled as one form", _ecib.kind == CF.FORM,
      _ecib.kind)
check("the eCIB record's page is saved with Save",
      _ecib.save_label == "Save", _ecib.save_label)

_elabels = [f.label for f in _ecib.fields]
# ---- the gates come BEFORE the fields they open ------------------------
# Three sections are gated by a Yes/No dropdown, and on a fresh record the
# twenty-three fields beneath them are READ-ONLY. Authored the other way
# round, twenty-three fields would report as unsettable on a screen that was
# working perfectly. The specification calls them all "numeric input".
ECIB_GATED = {
    "Rescheduled / Restructured?": [
        "Number of Times Restructured/Rescheduled (in 5 yrs) - for entities "
        "only",
        "Amount Under Litigation (Loans)",
        "Amount Under Litigation (Investments)"],
    "Write-Off?": ["Write-Offs (During last 10 Yrs)",
                   "Waived-Offs (During last 10 Yrs)",
                   "Recovery (During last 10 Yrs)",
                   "Settled Write-Offs (During last 5 Yrs)",
                   "Settled Waived-Offs (During last 5 Yrs)",
                   "Settled Recovery (During last 5 Yrs)"],
    "Overdue?": [x for x in _elabels
                 if "DPD" in x or "went into overdue" in x],
}
check("the three gate dropdowns are authored",
      all(g in _elabels for g in ECIB_GATED),
      str([g for g in ECIB_GATED if g not in _elabels]))
check("ECIB_GATES names exactly those three",
      set(CF.ECIB_GATES) == set(ECIB_GATED), str(CF.ECIB_GATES))
for _gate, _deps in ECIB_GATED.items():
    _gi = _elabels.index(_gate)
    _early = [d for d in _deps
              if d in _elabels and _elabels.index(d) < _gi]
    check(f"{_gate} is answered before the {len(_deps)} field(s) it opens",
          not _early, f"authored too early: {_early}")
check("every gate is answered Yes, which is what opens its fields",
      all(next(f.value for f in _ecib.fields if f.label == g) == "Yes"
          for g in CF.ECIB_GATES))

# ---- the computed fields ----------------------------------------------
# Only SEVEN fields are genuinely computed, and the specification's list of
# greyed-out fields is not that seven: 'Non-Fund Based (Loans)' and
# '(Investments)' are ordinary inputs, and 'Number of Times Restructured' is
# gated rather than computed.
check("no computed field is authored as a field to fill",
      not (set(CF.ECIB_COMPUTED) & set(_elabels)),
      str(sorted(set(CF.ECIB_COMPUTED) & set(_elabels))))
check("Non-Fund Based IS filled, despite the spec calling it greyed out",
      "Non-Fund Based (Loans)" in _elabels
      and "Non-Fund Based (Investments)" in _elabels)
check("Number of Times Restructured is filled, not treated as computed",
      any("Number of Times Restructured" in x for x in _elabels)
      and not any("Number of Times Restructured" in c
                  for c in CF.ECIB_COMPUTED))
check("every computed field's sources are fields this run enters",
      all(src in _elabels
          for srcs in CF.ECIB_COMPUTED.values() for src in srcs),
      str([src for srcs in CF.ECIB_COMPUTED.values() for src in srcs
           if src not in _elabels]))
# A Total expressed as 'Fund Based + Non-Fund Based' would let a wrong Fund
# Based quietly excuse a wrong Total. Each is written out as its inputs.
check("no computed field is derived from another computed field",
      not any(set(srcs) & set(CF.ECIB_COMPUTED)
              for srcs in CF.ECIB_COMPUTED.values()),
      str({c: sorted(set(s) & set(CF.ECIB_COMPUTED))
           for c, s in CF.ECIB_COMPUTED.items()
           if set(s) & set(CF.ECIB_COMPUTED)}))
check("Fund Based sums three fields, not the two the spec guessed at",
      len(CF.ECIB_COMPUTED["Fund Based (Loans)"]) == 3
      and "Other Outstanding (Loans)"
      in CF.ECIB_COMPUTED["Fund Based (Loans)"])
_sums = inspect.getsource(CF._check_ecib_sums)
check("a computed figure that is not a number is a failure",
      "showing a number" in _sums and "R.failed(" in _sums)
check("the computed figures are checked on the re-opened record too",
      "_check_ecib_sums" in inspect.getsource(CF._verify_ecib_details)
      and "after re-opening" in inspect.getsource(CF._verify_ecib_details))
check("the two runs of the sums check are labelled apart",
      "before reloading" in inspect.getsource(CF._do_ecib_add))

# ---- values -----------------------------------------------------------
# Forty-odd numeric fields on one screen makes "two fields share a value" a
# real risk rather than a theoretical one.
_egates = set(CF.ECIB_GATES)
_evals = [f.value for f in _ecib.fields
          if f.value and not f.marked and f.label not in _egates]
_edupes = sorted({v for v in _evals if _evals.count(v) > 1})
check("no two eCIB fields share a value", not _edupes, str(_edupes))
check("the eCIB amounts carry this run's digits",
      CF._ECIB_RUN and any(CF._ECIB_RUN[-4:] in v for v in _evals),
      f"run digits {CF._ECIB_RUN[-4:]!r}")
_EXPECTED_WORDS = {"Yes", CF.ECIB_TYPE}
check("every eCIB value is a number, a gate's Yes, or the eCIB type",
      all(f.value.isdigit() or f.value in _EXPECTED_WORDS
          for f in _ecib.fields if f.value and not f.marked),
      str([f.value for f in _ecib.fields
           if f.value and not f.marked
           and not (f.value.isdigit() or f.value in _EXPECTED_WORDS)]))
# The marker must be the field the LIST renders, or the round trip cannot
# find the record this run created.
check("the eCIB marker is in Company/Individual Name, a list column",
      [f.label for f in _ecib.fields if f.marked]
      == ["Company/Individual Name"],
      str([f.label for f in _ecib.fields if f.marked]))
check("Exposure As On precedes the report date given to the dialog",
      next(f.when for f in _ecib.fields if f.label == "Exposure As On")
      <= CF.ECIB_REPORT_DATE)
# The app blocks the save when the report is older than two months, which it
# was right to do — so these dates are relative to the run, not constants. A
# fixed date here would work today and silently stop working in two months.
import datetime as _dt  # noqa: E402
_age = (_dt.date.today() - CF.ECIB_REPORT_DATE).days
check("the eCIB report date is inside the app's two-month window",
      0 <= _age <= 55, f"{CF.ECIB_REPORT_DATE} is {_age} day(s) old")
check("the exposure date is inside that window too",
      0 <= (_dt.date.today() - CF.ECIB_EXPOSURE_DATE).days <= 55,
      str(CF.ECIB_EXPOSURE_DATE))
check("both eCIB dates are relative to the run, not fixed constants",
      CF.ECIB_REPORT_DATE == CF._ECIB_TODAY - _dt.timedelta(days=7)
      and CF.ECIB_EXPOSURE_DATE == CF._ECIB_TODAY - _dt.timedelta(days=14))

# ---- the two negative checks, and the generalised helper --------------
check("both sets of required-field rules are recorded",
      len(CF.ECIB_DIALOG_RULES) == 3 and len(CF.ECIB_PAGE_RULES) == 2,
      f"{CF.ECIB_DIALOG_RULES} / {CF.ECIB_PAGE_RULES}")
_req = inspect.getsource(CF._check_required_refused)
check("the empty-commit check is shared, not copied per screen",
      "_check_required_refused(" in inspect.getsource(CF._do_ecib_add)
      and "_check_required_refused(" in inspect.getsource(
          CF._add_ecib_record))
check("the dialog and the page are BOTH tested with nothing filled",
      inspect.getsource(CF._do_ecib_add).count(
          "_check_required_refused(") >= 1
      and "Proceed" in inspect.getsource(CF._add_ecib_record))
check("a commit accepted on an empty form is a FAILURE",
      "R.failed(" in _req and "was accepted with" in _req)
check("expected messages are matched loosely, not by exact string",
      "_norm(w) in _norm(m)" in _req)
check("a refusal that quotes only some rules is blocked, not passed",
      "were not" in _req and "R.BLOCKED" in _req)
# A single read at a fixed delay made this flaky: it passed on one run and
# reported "said nothing this run could read" on the next, same screen, same
# behaviour. Angular renders some messages late and clears others early.
check("validation messages are polled for, not read once",
      "for attempt in range(" in _req and "if m not in said" in _req)
# These messages arrive late, so the poll waits before its first read and
# then keeps going — reading immediately found nothing where a single read
# 1.8 seconds later had quoted both rules.
check("the poll waits before its first read, then keeps trying",
      _req.index("wait_for_timeout(1500)")
      < _req.index("for attempt in range(")
      < _req.index("wait_for_timeout(300)"))
# A dialog that has gone was accepted; a page stays put either way, so its
# acceptance has to be read from a success message instead.
check("a page that accepts an empty commit is caught by its success message",
      "not oks" in _req and "was accepted with nothing filled" in _req)

# ---- Proceed does not persist here ------------------------------------
# Unlike the bank dialog, Proceed only opens the page: nothing exists until
# Save. That is why a dry run can press Proceed and leave the case untouched —
# except it does not even do that, it cancels.
_eadd = inspect.getsource(CF._add_ecib_record)
check("a dry run cancels the eCIB dialog rather than proceeding",
      "cancel_modal()" in _eadd and "dry run" in _eadd)
check("the run stops if the record's page never opens",
      "_looks_like_detail(s)" in _eadd)

# ---- what the dialog carries through ---------------------------------
# All THREE of the dialog's fields arrive on the record's page, and all three
# are asserted rather than re-entered. Reading the raw <ng-select> attribute
# made eCIB Type look like it had not arrived; value_of reads the rendered
# selection and says 'Entity'.
check("all three of the dialog's fields are asserted on the record's page",
      set(CF.ECIB_CARRIED) == {"eCIB Borrower Code", "eCIB Report Date",
                               "eCIB Type"},
      str(sorted(CF.ECIB_CARRIED)))
check("each carried field says how it should be compared",
      all(isinstance(v, tuple) and len(v) == 2
          for v in CF.ECIB_CARRIED.values()))
# eCIB Type must NOT be re-selected: its dropdown carries the chosen value
# and no options at all, so choosing would fail however right the value is.
check("eCIB Type is asserted, never re-selected",
      "eCIB Type" not in _elabels and "eCIB Type" in CF.ECIB_CARRIED)
_carried = inspect.getsource(CF._check_ecib_carried)
check("a carried field that arrives empty is a failure",
      "the field is empty" in _carried and "R.failed(" in _carried)

# ---- View Past Dues stays out of the happy path -----------------------
check("a View Past Dues helper exists", hasattr(CF, "_open_past_dues"))
check("it is not pressed by the happy path",
      "_open_past_dues" not in inspect.getsource(CF._do_ecib_add)
      and "_open_past_dues" not in inspect.getsource(CF._do_ecib_upload_one))
_past = inspect.getsource(CF._open_past_dues)
check("it does not go through commit(), which would refuse it",
      ".commit(" not in _past and "collect_action_buttons" in _past)

# ---- the round trip ---------------------------------------------------
check("eCIB Details has its own round trip",
      "_verify_ecib_details(" in _verify_src)
_eopen = inspect.getsource(CF._open_our_ecib_record)
check("the eCIB list is searched for the marker before rows are scanned",
      "search_grid(" in _eopen
      and _eopen.index("search_grid(") < _eopen.index("open_row_detail"))
check("which field carries the eCIB marker is read off the pass",
      "ECIB_PASS.fields" in _eopen and ".marked" in _eopen)
check("an eCIB record that cannot be found is blocked, not failed",
      "R.BLOCKED" in inspect.getsource(CF._verify_ecib_details))

# ---- discovery must not overwrite the dialog's own data ---------------
# Discovery is indiscriminate by design, and on this screen that turned into
# a hazard: it found the report date the dialog had set writable on the
# record's page and typed a date of its own over it.
_fp = inspect.getsource(CF._fill_pass)
check("_fill_pass can be told which fields discovery must leave alone",
      "leave_alone" in _fp and "done: set[str] = {" in _fp)
check("the eCIB pass leaves the dialog's three fields alone",
      "leave_alone=list(ECIB_CARRIED)" in inspect.getsource(
          CF._do_ecib_add))
# The dialog calls it 'Borrower Code'; the record's page calls it 'eCIB
# Borrower Code'. Recorded under the page's name, or the round trip looks up
# a label that is not there.
check("the borrower code is recorded under the name the page uses",
      'e.label = "eCIB Borrower Code"' in inspect.getsource(
          CF._add_ecib_record))

# ---- Exposure As On has its own check, with the diagnosis -------------
# The round trip alone says "the value did not survive", which is true and
# unhelpful. This field forces the day to the 1st, and the format that looks
# like it works is the box keeping text nothing has parsed.
_exp = inspect.getsource(CF._check_ecib_exposure)
check("Exposure As On is checked for the DAY it was given",
      "takes the day it was given" in _exp)
check("a box holding exactly what was typed is called unparsed",
      "nothing" in _exp and "flows._norm_value(shown)" in _exp)
check("the diagnosis names the field that DOES store its day",
      "eCIB Report Date" in _exp)
check("it is a failure, not a blocked",
      _exp.count("R.failed(") >= 2)


# The save path is a second copy of _fill_pass's tail, deliberately. The check
# NAMES must stay identical, or the same outcome reads differently depending
# on which screen produced it.
_commit_src = inspect.getsource(CF._commit_and_report)
_fill_src = inspect.getsource(CF._fill_pass)
for _phrase in ['{name} is saved', 'the app confirms it was saved',
                "Save stores the values and clears the form's messages"]:
    _in_fill = _phrase.replace("{name}", "{spec.name}")
    check(f"the grid save reports {_phrase[:34]!r} as every other screen does",
          _phrase in _commit_src and _in_fill in _fill_src)

# The audit's cut-off must not fall after the date it was reported.
_dates = {f.label: f.when for f in _obs.fields if f.when}
check("the audit cut-off precedes the report date",
      _dates["Cutoff Date"] <= _dates["Report Date"],
      f"cutoff {_dates['Cutoff Date']}, report {_dates['Report Date']}")
check("every observation date is in the past, as an audit's must be",
      all(d.year <= 2026 for d in _dates.values()),
      str({k: str(v) for k, v in _dates.items()}))

# Exactly one field per screen carries the run marker. Without it the round
# trip matches a previous run's identical text and proves nothing, and the
# facility this run created cannot be told from the ones already on the case.
for group, passes in [("Request Details", CF.REQUEST_DETAILS_PASSES),
                      ("Facilities", CF.FACILITY_PASSES),
                      ("Observations", CF.OBSERVATION_PASSES),
                      ("Litigation", CF.LITIGATION_PASSES),
                      ("Shariah Comments", CF.SHARIAH_PASSES),
                      ("RMG Memo", CF.CRMD_NOTE_PASSES),
                      ("Credit Memorandum", CF.CREDIT_MEMORANDUM_PASSES),
                      ("Group Review", CF.GROUP_REVIEW_PASSES),
                      ("Relationship with Other Banks / FIs",
                       [CF.BANK_LIMIT_PASS, CF.BANK_DETAIL_PASS]),
                      ("eCIB Details", [CF.ECIB_PASS])]:
    marked = [f.label for p in passes for f in p.fields if f.marked]
    check(f"{group} carries exactly one run-marked field", len(marked) == 1,
          str(marked))
check("the run marker is distinctive enough to search a screen for",
      flows._distinctive(CF.run_marker("0101-000000")),
      CF.run_marker("0101-000000"))

# Compliance and structural switches are pinned to No, for the same reason
# they are on the obligor form: "first valid option" turns them ON, and a
# switch that makes a whole table mandatory — or flags a synthetic record as
# politically exposed — creates work for a human who did not ask for it.
#
# Only the choice-shaped fields are in scope. _PIN_NO is deliberately broad —
# it also catches 'Total Syndication Amount' and 'SBP PRs Exceptions', which are
# an amount and a text area, not switches. Pinning those to "No" would be
# nonsense, and at run time _auto_value never does: it applies the pin only to
# dropdowns, lookups and switches.
# ONE documented exception, and it is deliberate rather than an oversight.
# eCIB Details gates three sections behind a Yes/No dropdown: answer 'No' and
# the twenty-three fields beneath stay READ-ONLY, so 'No' would mean checking
# nothing on half that screen. These are not compliance flags about a person —
# they are eCIB attributes of a synthetic record whose name says so — and the
# rule still applies to every other switch in every pass, including the two
# amount fields on the same screen that _PIN_NO also matches.
_CHOICEY = ("Yes", "No", None)
_GATE_EXCEPTIONS = set(CF.ECIB_GATES)
_switches = [(p.name, f.label, f.value) for p in _all_passes for f in p.fields
             if CF._PIN_NO.search(f.label) and f.value in _CHOICEY
             and f.when is None and not f.marked]
_on = [x for x in _switches
       if str(x[2]).lower() != "no" and x[1] not in _GATE_EXCEPTIONS]
check("every pinned switch is authored as No, bar the documented gates",
      not _on, str(_on) if _on else f"{len(_switches)} switch(es)")
# The exception must not be a blanket one: only the gates may say Yes, and
# only because saying No leaves their fields unfillable.
_yes = [x[1] for x in _switches if str(x[2]).lower() == "yes"]
check("the only switches answered Yes are the eCIB section gates",
      set(_yes) <= _GATE_EXCEPTIONS, str(sorted(set(_yes))))
check("a gate answered No would leave its fields read-only",
      "read-only" in inspect.getsource(CF._report_ecib_gates))
for label in ["Politically Exposed?", "Is Syndicated Limit?",
              "Is Obligor a Related Party?", "Life Time Expiry?"]:
    check(f"a discovered {label!r} defaults to No",
          CF._auto_value(label, "switch", "M") == "No"
          and CF._auto_value(label, "checkbox", "M") == "No")

# A value discovered on the screen still has to suit the box it goes into: a
# numeric field will not take prose, and phone boxes validate on LENGTH.
check("a discovered numeric field gets a number",
      re.match(r"^-?\d+(\.\d+)?$",
               CF._auto_value("Limit In Base CCY", "number", "M") or ""))
check("a discovered percentage gets a plausible rate",
      CF._auto_value("Pricing (%)", "number", "M") == "5")
check("a discovered phone field gets 14 digits",
      CF._auto_value("Contact Number", "text", "M") == flows.PHONE)
check("a discovered date that is an expiry is set in the future",
      CF._auto_date("Facility Expiry Date").year >= 2027)
check("a discovered date that is historical is not",
      CF._auto_date("Overdue since").year <= 2026,
      str(CF._auto_date("Overdue since")))

# Pairing a facility tab with the pass that belongs to it. The app and the FSD
# word these differently, and filling a tab from the wrong pass is worse than
# filling it by discovery — it types one tab's values into another.
TAB_PAIRS = {
    "Facility Request Details": "Facilities — Facility Request Details",
    "Facility Details": "Facilities — Facility Details",
    "Limit and Exposure": "Facilities — Limits and Exposures",
    "Overdues": "Facilities — Overdue",
    "Profit / Rental / Service Charges Structure":
        "Facilities — Profit / Commission Structure",
    "Payment": "Facilities — Payment",
    "Utilisation & Outstanding": "Facilities — Utilisation & Outstanding",
}
for tab, want in TAB_PAIRS.items():
    got = CF._pass_for_tab(tab)
    check(f"'{tab[:36]}' is filled from its own section",
          got is not None and got.name == want,
          got.name if got else "no pass matched")
for tab in ["Facility Summary", "Facility Risk Rating Log",
            "Facility R1 Log History"]:
    check(f"'{tab}' is filled by discovery rather than mispaired",
          CF._pass_for_tab(tab) is None,
          getattr(CF._pass_for_tab(tab), "name", ""))

# The verification leg must not pair the requested-facility choice with a tab:
# it was made in a dialog before the facility had any.
check("the requested facility belongs to no tab",
      CF._tab_for_group(CF.REQUESTED_FACILITY,
                        ["Facility Details", "Payment"]) == "")


# --------------------------------------------------------------------------
print("\n1d-i. Escape never closes the form being filled")
# --------------------------------------------------------------------------
# A real failure, found on the Litigation form. Escape is what closes this
# app's date picker and its dropdown panels — and inside a Bootstrap modal it
# closes the MODAL as well. So after the first date was picked the whole entry
# form went away, the field already answered read back empty, and the seven
# fields after it reported "not on this screen". The run blamed the
# application for losing a value the automation had just thrown away itself.
#
# Both closers now press nothing unless something is actually open, and never
# use Escape while a modal is up. This matters well beyond Litigation: every
# add-row dialog in the suite is a modal with dates and dropdowns in it.
for _name in ("_close_calendar", "_close_dropdown", "_calendar_open"):
    check(f"widgets has {_name}", hasattr(W.Filler, _name))

_cal = inspect.getsource(W.Filler._calendar_pick)
check("the calendar is closed through _close_calendar, not by Escape",
      "_close_calendar" in _cal and 'press("Escape")' not in _cal)
_closer = inspect.getsource(W.Filler._close_calendar)
check("nothing is pressed unless a calendar is open",
      "_calendar_open" in _closer
      and _closer.index("_calendar_open") < _closer.index('press("Escape")'))
check("Escape is not used on the calendar while a modal is up",
      "_scope() != \"body\"" in _closer
      and _closer.index("_scope()") < _closer.index('press("Escape")'))

_dd = inspect.getsource(W.Filler.choose)
check("a dropdown panel is closed through _close_dropdown, not by Escape",
      "_close_dropdown" in _dd and 'press("Escape")' not in _dd)
_ddc = inspect.getsource(W.Filler._close_dropdown)
check("Escape is not used on a dropdown while a modal is up",
      "_scope() != \"body\"" in _ddc
      and _ddc.index("_scope()") < _ddc.index('press("Escape")'))

# The date path still proves the app took the date it was given. Making the
# closer safe must not weaken the check that made it worth having.
_date_src = inspect.getsource(W.Filler.date)
check("a date is still read back and confirmed",
      "_date_taken" in _date_src and "input_value" in _date_src)
check("a date silently changed to another date is still a finding",
      not W.Filler._date_taken(date(2026, 1, 31), "January 20, 2026")
      and W.Filler._date_taken(date(2026, 1, 31), "January 31st, 2026"))


# --------------------------------------------------------------------------
print("\n1e. The round-trip comparison does not invent findings")
# --------------------------------------------------------------------------
# Each of these was a real false failure before it was fixed. A verification
# that cries wolf is worse than none, so they are pinned here.
SAME = [
    # the app displays what it stores, formatted
    ("5000000", "5,000,000", "text", "thousands separators"),
    ("1000000", "1,000,000", "text", "thousands separators"),
    ("25", "25", "text", "identical"),
    # a lookup renders only its description, not its code
    ("00000000000 - FOREIGN CONSTITUENTS", "FOREIGN CONSTITUENTS", "lookup",
     "code dropped"),
    ("UP3708 - 301 - Main Abottabad", "301 - Main Abottabad", "lookup",
     "prefix dropped"),
    # double space in the app's own label
    ("AIBG - Aitemaad Islamic Banking Group",
     "AIBG  - Aitemaad Islamic Banking Group", "lookup", "whitespace"),
    # a date entered numerically, displayed long-form
    ("01/01/2020", "January 1st, 2020", "date", "date formatting"),
]
for typed, shown, kind, why in SAME:
    check(f"{why}: {typed[:22]!r} == {shown[:22]!r}",
          flows._same_value(typed, shown, kind))

DIFFERENT = [
    ("01/01/2020", "January 1st, 2001", "date", "wrong year is a real finding"),
    ("5000000", "5,000,001", "text", "different number"),
    ("AUTOMATION TEST A", "AUTOMATION TEST B", "text", "different text"),
]
for typed, shown, kind, why in DIFFERENT:
    check(f"{why}", not flows._same_value(typed, shown, kind))

# Short values cannot be searched for in a screen's text, so the verdict must be
# "cannot tell" rather than a failure. Reporting FAIL on these produced dozens
# of findings out of nothing.
for short in ["25", "No", "10", "7", "Yes"]:
    check(f"{short!r} is treated as too short to search for",
          not flows._distinctive(short))
for long in ["AUTOMATION SHAREHOLDER", "automation.test@example.com",
             "03001234567890"]:
    check(f"{long[:24]!r} is distinctive enough to search for",
          flows._distinctive(long))


# --------------------------------------------------------------------------
print("\n1f. The downloads say exactly what the run said")
# --------------------------------------------------------------------------
# A report that disagrees with the page it came from is worse than no report.
# So the CSVs and the PDF are all built from exports.py's row builders, and
# these pin what those rows contain — including the two things that are easy
# to lose on the way out: a rich-text value's structure, and the fields a run
# entered as opposed to the checks it made.

check("exports.py cannot type into the application",
      not [b for b in BANNED if b in inspect.getsource(X)])
check("exports.py never touches a browser",
      not [n for n in ("playwright", "import widgets", "Session(", "self.page",
                       "s.page", "cr.") if n in inspect.getsource(X)])

# One run, made up here rather than recorded, so this exercises the whole
# export path without a browser. Its ten fields are the litigation form.
_RUN = {
    "run_id": "case-litigation-20260101-000000",
    "target_key": "case.screens",
    "target_title": "Fill and verify Litigation",
    "base_url": "http://example.invalid/riskNucleus",
    "started_at": "2026-01-01T00:00:00+00:00",
    "finished_at": "2026-01-01T00:04:00+00:00",
    "artifacts_dir": "artifacts/case-litigation-20260101-000000",
    "case_id": "52224-2026",
    "screens": ["Litigation"],
    "marker": "AUTOTEST-0101-000000",
    "dry_run": False,
    "overall": R.FAIL,
    "headline": "2 passed / 1 failed / 1 blocked",
    "blocked_reason": "",
    "steps": [{"index": 1, "kind": "step", "label": "sign in",
               "status": R.PASS, "note": ""}],
    "entries": [
        {"label": f.label, "value": (f.value if f.value else
                                     (f.when.strftime("%d/%m/%Y") if f.when
                                      else "first option")),
         "kind": ("date" if f.when else
                  ("rich-text" if f.marked else "text")),
         "screen": "Litigation", "group": "Litigation"}
        for f in CF.LITIGATION_PASSES[0].fields],
    "checks": [
        {"name": "Litigation is saved", "status": R.PASS, "expected": "",
         "actual": "", "detail": "10 field(s) entered and saved.",
         "evidence": [], "screen": "Litigation"},
        {"name": "Litigation: Suit Amount carried through", "status": R.FAIL,
         "expected": "10000000", "actual": "1,000,000", "detail": "",
         "evidence": [], "screen": "Litigation"},
        {"name": "Litigation: Date Of Decree carried through",
         "status": R.BLOCKED, "expected": "31/12/2027", "actual": "",
         "detail": "Nothing could be read back.", "evidence": [],
         "screen": "Litigation"},
        {"name": "A litigation record can be added", "status": R.PASS,
         "expected": "", "actual": "", "detail": "", "evidence": [],
         "screen": "Litigation"},
    ],
}
# The marker field is authored with value "" and filled at run time, so give
# it the rich text a real run would have put there — several paragraphs, which
# is the case the export has to get right.
_PROCEEDINGS = (
    "<p>Entered by automated test. AUTOTEST-0101-000000.</p>"
    "<p>Written statement filed;<br/>next hearing for framing of issues.</p>")
next(e for e in _RUN["entries"]
     if e["label"] == "Proceeding Details")["value"] = _PROCEEDINGS

# The checks CSV keeps the columns it has always had. Renaming one breaks
# every spreadsheet anybody has built on top of it.
check("the checks CSV keeps its columns, in order",
      list(X.check_rows(_RUN)[0]) ==
      ["Screen", "Check", "Result", "Should be", "Actually", "Notes"],
      str(list(X.check_rows(_RUN)[0])))
check("the checks CSV puts failures first",
      X.check_rows(_RUN)[0]["Result"] == R.FAIL,
      X.check_rows(_RUN)[0]["Result"])

# Every field the run entered reaches the export, once each, in the order the
# form asked for them. A field that silently stops being exported is a field
# nobody can check the run against.
_rows = X.entry_rows(_RUN)
check("every entered field reaches the export",
      len(_rows) == len(_RUN["entries"]),
      f"{len(_rows)} of {len(_RUN['entries'])}")
check("the entered fields keep the order the form asks for them in",
      [r["Field"] for r in _rows] == LITIGATION_FORM,
      str([r["Field"] for r in _rows]))
check("every litigation field named in the requirement is exported",
      not [n for n in LITIGATION_FORM
           if n not in [r["Field"] for r in _rows]])
check("no exported value is empty",
      not [r["Field"] for r in _rows if not str(r["Value"]).strip()],
      str([r["Field"] for r in _rows if not str(r["Value"]).strip()]))

# Rich text is unwound, not deleted. The markup goes, the paragraphs stay, and
# nothing that would split a CSV row survives.
_rich = next(r for r in _rows if r["Field"] == "Proceeding Details")
check("rich-text markup is not exported as markup",
      "<p>" not in _rich["Value"] and "<br" not in _rich["Value"],
      _rich["Value"][:60])
check("rich-text content is exported, not dropped",
      "AUTOTEST-0101-000000" in _rich["Value"]
      and "framing of issues" in _rich["Value"])
check("a rich-text cell holds no raw newline to split the CSV row",
      "\n" not in _rich["Value"] and "\r" not in _rich["Value"],
      repr(_rich["Value"][:60]))
check("the paragraph break is still visible in one CSV cell",
      "¶" in _rich["Value"], _rich["Value"][:80])
check("the PDF keeps the paragraphs as paragraphs",
      len(X.rich_paragraphs(_PROCEEDINGS)) == 2,
      str(X.rich_paragraphs(_PROCEEDINGS)))
check("a line break inside a paragraph is kept as one",
      "\n" in X.rich_paragraphs(_PROCEEDINGS)[1])
for _raw, _want in [("&amp; &lt;b&gt;", "& <b>"), ("plain text", "plain text")]:
    check(f"{_raw!r} exports as {_want!r}", X.plain_text(_raw) == _want,
          X.plain_text(_raw))

_csv = X.entries_csv(_RUN)
check("the entered-values CSV has a header and one row per field",
      len(_csv.strip().splitlines()) >= len(_rows) + 1,
      f"{len(_csv.strip().splitlines())} line(s)")
for _name in ["Type of Suit", "Suit Amount", "Date Of Decree"]:
    check(f"{_name!r} is in the entered-values CSV", _name in _csv)

# The PDF is a real PDF, and it is built from the same rows. Its text cannot
# be read back without a parser, so what is pinned here is that it builds for
# a run with a failure, a blocker, several date formats and multi-paragraph
# rich text in it — which is where a table of raw strings would have thrown.
check("reportlab is available for the PDF download", X.pdf_available(),
      X.PDF_HINT if not X.pdf_available() else "")
if X.pdf_available():
    _pdf = X.build_pdf(_RUN)
    check("the PDF download is a PDF", _pdf[:5] == b"%PDF-", str(_pdf[:8]))
    check("the PDF has content in it", len(_pdf) > 3000, f"{len(_pdf)} bytes")
    # A dry run and a run with nothing entered are both real states of this
    # page, and neither may take the report down with it.
    # A Shariah Comments run is one rich-text field and nothing else — the
    # narrowest thing either export has to cope with, and the one where a
    # dropped cell would leave the report empty rather than merely short.
    _SHARIAH_RUN = {
        **_RUN,
        "target_title": "Fill and verify Shariah Comments",
        "screens": ["Shariah Comments"],
        "entries": [{"label": "SCD Remarks", "value": _PROCEEDINGS,
                     "kind": "rich-text", "screen": "Shariah Comments",
                     "group": "Shariah Comments"}],
    }
    _srows = X.entry_rows(_SHARIAH_RUN)
    check("a one-field Shariah run exports its single editor",
          len(_srows) == 1 and _srows[0]["Field"] == "SCD Remarks"
          and "framing of issues" in _srows[0]["Value"],
          str(_srows))
    check("its CSV is a header and one row",
          len(X.entries_csv(_SHARIAH_RUN).strip().splitlines()) == 2,
          X.entries_csv(_SHARIAH_RUN).strip())

    # A RMG Memo run is eight narrative editors, and its third label is 94
    # characters. A reportlab table of raw strings does not wrap — it
    # overflows the column and the text vanishes off the page — so this is
    # the case that proves every cell really is a Paragraph.
    _CRMD_RUN = {
        **_RUN,
        "target_title": "Fill and verify RMG Memo",
        "screens": ["RMG Memo"],
        "entries": [
            {"label": f.label,
             "value": (_PROCEEDINGS if f.marked else f.value),
             "kind": "rich-text", "screen": "RMG Memo",
             "group": "RMG Memo"}
            for f in CF.CRMD_NOTE_PASSES[0].fields],
    }
    _crows = X.entry_rows(_CRMD_RUN)
    check("all eight RMG Memo editors reach the export",
          len(_crows) == 8, f"{len(_crows)} row(s)")
    check("the 94-character label survives the export intact",
          any(len(r["Field"]) > 90 for r in _crows),
          str([len(r["Field"]) for r in _crows]))
    check("each exported CRMD value is its own text",
          len({r["Value"] for r in _crows}) == 8,
          f"{len({r['Value'] for r in _crows})} distinct of 8")

    # Thirty-four narrative editors is the biggest report either export has
    # to produce, and the one where a table that does not split across pages
    # would throw rather than paginate.
    _MEMO_RUN = {
        **_RUN,
        "target_title": "Fill and verify Credit Memorandum",
        "screens": ["Credit Memorandum"],
        "entries": [
            {"label": f.label,
             "value": (_PROCEEDINGS if f.marked else f.value),
             "kind": "rich-text", "screen": "Credit Memorandum",
             "group": "Credit Memorandum"}
            for f in CF.CREDIT_MEMORANDUM_PASSES[0].fields],
    }
    _mrows = X.entry_rows(_MEMO_RUN)
    check("all thirty-three Credit Memorandum editors reach the export",
          len(_mrows) == 33, f"{len(_mrows)} row(s)")
    check("each exported Credit Memorandum value is its own text",
          len({r["Value"] for r in _mrows}) == 33,
          f"{len({r['Value'] for r in _mrows})} distinct of 33")
    check("the curly apostrophes survive the export",
          any("’" in r["Field"] for r in _mrows),
          str([r["Field"] for r in _mrows if "’" in r["Field"]])[:80])
    _mcsv = X.entries_csv(_MEMO_RUN)
    check("the Credit Memorandum CSV has a row per editor",
          len(_mcsv.strip().splitlines()) >= 34,
          f"{len(_mcsv.strip().splitlines())} line(s)")

    for _label, _variant in [("a dry run", {**_RUN, "dry_run": True}),
                             ("a Shariah Comments run", _SHARIAH_RUN),
                             ("a RMG Memo run", _CRMD_RUN),
                             ("a Credit Memorandum run", _MEMO_RUN),
                             ("a run that entered nothing",
                              {**_RUN, "entries": []}),
                             ("a run that checked nothing",
                              {**_RUN, "checks": []}),
                             ("a blocked run",
                              {**_RUN, "blocked_reason": "host unreachable"})]:
        try:
            ok_pdf = X.build_pdf(_variant)[:5] == b"%PDF-"
        except Exception as exc:  # noqa: BLE001
            ok_pdf, _label = False, f"{_label}: {exc}"
        check(f"the PDF builds for {_label}", ok_pdf)


# --------------------------------------------------------------------------
print("\n2. Destructive controls remain unreachable")
# --------------------------------------------------------------------------
DESTRUCTIVE = ["Save", "Submit", "Approve", "Reject", "Delete", "Remove", "Post",
               "Confirm", "Proceed", "Authorize", "Bulk Action", "Sign out",
               "Edit", "Update", "Export", "Download", "Upload", "Print"]
refused = [d for d in DESTRUCTIVE if not cr.is_safe_to_click(d) and not cr.is_safe_tab(d)]
check("every destructive label is refused by both gates",
      len(refused) == len(DESTRUCTIVE),
      f"allowed: {[d for d in DESTRUCTIVE if d not in refused]}")

# Navigation labels must still be permitted, or the target is unreachable.
NAV = ["Basic Information", "Sector & Industry", "Obligor Details (BIR)",
       "Queries", "Facilities", "Collaterals", "Additional Information"]
allowed_nav = [n for n in NAV if cr.is_safe_tab(n)]
check("real navigation labels are still allowed",
      len(allowed_nav) == len(NAV),
      f"refused: {[n for n in NAV if n not in allowed_nav]}")


# --------------------------------------------------------------------------
print("\n3. Write allowlist fails closed")
# --------------------------------------------------------------------------
ok, reason = settings.write_allowed(
    "http://nationalbankinternal-dev.risknucleus.com:341/riskNucleus")
check("approved dev host is allowed", ok, reason)

for bad in ["http://faysalbank-prod.risknucleus.com/riskNucleus",
            "http://nationalbankinternal-uat.risknucleus.com:341/riskNucleus",
            "https://nationalbankinternal-dev.risknucleus.com/riskNucleus",  # no port
            "not-a-url", ""]:
    ok_bad, why = settings.write_allowed(bad)
    check(f"refused: {bad or '(empty)'}", not ok_bad, why[:70])

_saved = settings.ALLOWED_WRITE_HOSTS
try:
    settings.ALLOWED_WRITE_HOSTS = set()
    ok_empty, why_empty = settings.write_allowed(
        "http://nationalbankinternal-dev.risknucleus.com:341/riskNucleus")
    check("an empty allowlist blocks everything (fails closed)", not ok_empty,
          why_empty[:70])
finally:
    settings.ALLOWED_WRITE_HOSTS = _saved


# --------------------------------------------------------------------------
print("\n4. Results model separates defects from environment problems")
# --------------------------------------------------------------------------
r = R.RunResult(run_id="t", target_key="k", target_title="T", mode=settings.VERIFY,
                base_url="u", started_at="now")
check("a run with nothing checked is BLOCKED, not PASS", r.overall == R.BLOCKED,
      r.overall)
r.checks.append(R.passed("a"))
check("a run with only passes is PASS", r.overall == R.PASS, r.overall)
r.checks.append(R.blocked("b", "no data"))
check("passes plus blocked is still PASS", r.overall == R.PASS, r.overall)
r.checks.append(R.failed("c", "x", "y"))
check("any failure makes the run FAIL", r.overall == R.FAIL, r.overall)
check("failures sort to the top", r.sorted_checks()[0].status == R.FAIL)

r2 = R.RunResult(run_id="t", target_key="k", target_title="T", mode=settings.VERIFY,
                 base_url="u", started_at="now", blocked_reason="host unreachable")
r2.checks.append(R.passed("a"))
check("an explicit blocked_reason overrides passes", r2.overall == R.BLOCKED)


# --------------------------------------------------------------------------
print("\n5. Target definition matches the observed application")
# --------------------------------------------------------------------------
# The primary route is via My Bucket, because that is the grid holding the
# NNNNN-YYYY case ids people actually test with.
#
# Which case target is enabled is a choice made in targets.py — sections get
# commented out there on request — so these assertions run against whichever
# one is live rather than naming it. A disabled target must not fail the suite;
# a BROKEN one must.
CASE_KEY = next((k for k in ("case.obligor", "case.all_screens")
                 if k in targets.TARGETS), "")
check("a credit-case route is enabled", bool(CASE_KEY),
      "none of the case targets are uncommented in targets.py")

if CASE_KEY:
    t = targets.get(CASE_KEY)
    print(f"        (checking against {CASE_KEY})")
    kinds = [s.kind for s in t.steps]
    check("case path starts from My Bucket and opens a named record",
          kinds[:2] == [targets.MENU, targets.ROW_BY_ID], str(kinds))
    check("case path starts from My Bucket", "bucket" in t.steps[0].path,
          t.steps[0].path)
    check("a record that is not in the grid is BLOCKED, not FAIL",
          t.steps[1].missing_is_blocked)
    check("the record id is a placeholder resolved at run time",
          t.steps[1].value == "{case_id}", t.steps[1].value)

    resolved = targets.resolve(t, "52224-2026")
    check("the placeholder is substituted",
          resolved.steps[1].value == "52224-2026", resolved.steps[1].value)
    check("resolving does not mutate the template",
          t.steps[1].value == "{case_id}")
    check("resolving keeps the id-format metadata",
          resolved.id_pattern == t.id_pattern and bool(resolved.id_description))

# The obligor tab strip is walked in full wherever it appears — as a target's
# own screen list, or as the children of the Obligor Details (BIR) sidebar entry.
OBLIGOR_TABS = ["Basic Information", "Sector And Industry",
                "Management & Shareholders", "Additional Information"]
check("all obligor sub-screens are walked",
      [s.label for s in targets.OBLIGOR_SUB_SCREENS][:4] == OBLIGOR_TABS,
      str([s.label for s in targets.OBLIGOR_SUB_SCREENS][:4]))
check("Basic Information may already be the active tab",
      targets.OBLIGOR_SUB_SCREENS[0].satisfied_if_active)

# The All Obligors route and the Basic-Information-only route are commented out
# in targets.py on request. Asserting they are ABSENT rather than deleting these
# lines keeps the fact visible: if someone uncomments either block, this says so.
DISABLED = ["obligor.record", "case.obligor_basic"]
check("the two disabled routes are not offered",
      not any(k in targets.TARGETS for k in DISABLED),
      str([k for k in DISABLED if k in targets.TARGETS]))
check("only the credit-case area remains in the menu",
      list(targets.by_area()) == ["Credit case (case IDs like 52224-2026)"],
      str(list(targets.by_area())))

# Routing advice: the failure this prevents was a usability bug, not a
# technical one — the operator had no way to know the id was in the other grid.
check("a case id is routed to the enabled My Bucket target",
      targets.suggested_target("52224-2026") == CASE_KEY,
      f"suggested {targets.suggested_target('52224-2026')!r}, "
      f"enabled route is {CASE_KEY!r}")
check("no advice when the id already fits the route",
      targets.route_advice("52224-2026", CASE_KEY) == "",
      targets.route_advice("52224-2026", CASE_KEY))
check("an unrecognised id suggests nothing",
      targets.suggested_target("not-an-id") == "")


# --------------------------------------------------------------------------
print("\n5b. The whole-case target covers the case's own sidebar")
# --------------------------------------------------------------------------
# Guarded the way section 5 above already guards itself. Every Phase 1 route in
# targets.py is currently commented out, so TARGETS is empty and this section
# has nothing to check against — and asking for the target regardless raised a
# KeyError that took the whole suite down with it, at the last section, after
# every check had already passed. The absence is reported (see "a credit-case
# route is enabled") rather than crashing the run that reports it.
if "case.all_screens" not in targets.TARGETS:
    check("the whole-case target is defined", False,
          "case.all_screens is commented out in targets.py, so the case "
          "sidebar coverage below could not be checked")
else:
    a = targets.get("case.all_screens")
    check("whole-case path is menu -> specific record",
          [s.kind for s in a.steps] == [targets.MENU, targets.ROW_BY_ID],
          str([s.kind for s in a.steps]))

    labels = [s.label for s in a.sub_screens]
    SIDEBAR = ["Credit Approval Memo", "Obligor Details (BIR)", "Queries",
               "Request Details", "Facilities", "Observations", "Collaterals",
               "Facility Coverage", "Risk Rating", "Financials",
               "Credit Memorandum", "eCIB Details", "Policies & Exceptions",
               "Conditions", "Documents", "RMG Memo", "History",
               "Relationship with Other Banks / FIs", "Business Performance"]
    check("every sidebar entry in the screenshot is covered, in order",
          labels == SIDEBAR, str([x for x in SIDEBAR if x not in labels]))
    check("case screens are reached by the sidebar, not by tabs",
          all(s.kind == targets.CONTEXT_MENU for s in a.sub_screens),
          str([s.label for s in a.sub_screens
               if s.kind != targets.CONTEXT_MENU]))
    # Obligor Details (BIR) is a sidebar entry whose content is a tab strip. It
    # must not be checked as itself as well, or Basic Information is reported
    # twice.
    bir = next(s for s in a.sub_screens if s.label == "Obligor Details (BIR)")
    check("Obligor Details (BIR) is a doorway to its tabs",
          not bir.check_self
          and len(bir.children) == len(targets.OBLIGOR_SUB_SCREENS))
    check("its children are tabs",
          all(c.kind == targets.TAB for c in bir.children))
    check("the planned screen list flattens parents into children",
          "Obligor Details (BIR)" not in a.screen_names()
          and "Basic Information" in a.screen_names())

    # The grid screens are the reason row detail exists: a summary row shows a
    # few of a record's values and its detail view holds the rest.
    for nm in ["Facilities", "Collaterals", "Documents", "Conditions"]:
        s = next(x for x in a.sub_screens if x.label == nm)
        check(f"{nm} opens a row to reach the record's own data",
              s.open_row_detail)

    # A record whose data is spread over a tab strip has the whole strip walked.
    for nm in ["Facilities", "Collaterals", "eCIB Details", "Financials"]:
        s = next(x for x in a.sub_screens if x.label == nm)
        check(f"{nm} walks the tabs of the record it opens",
              s.walk_inner_tabs and s.open_row_detail)

check("no Phase 1 target is writable",
      not any(x.writable for x in targets.TARGETS.values()))

# The top-level menu must be captured once at login, not re-read later: after a
# case opens, the sidebar becomes the case menu, and reading it then would mark
# the case entries "already known" and exclude them.
src = inspect.getsource(driver.Session.login)
check("top-level menu paths are captured during login",
      "top_level_paths" in src and "collect_menu_targets" in src)
check("no live re-read of the top-level menu remains",
      not hasattr(driver.Session, "_top_level_paths"))


print("\n" + "=" * 70)
if FAILURES:
    print(f"{len(FAILURES)} SAFETY CHECK(S) FAILED:")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
print("ALL SAFETY CHECKS PASSED")
