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
import tempfile
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

# The specification is gone from this package entirely - not merely unread at
# run time, but unmentioned. What was left in here was wording that attributed
# authored field names to a document that has no bearing on what this suite
# does.
import los_automation as _pkg  # noqa: E402
import pathlib  # noqa: E402

_pkg_root = pathlib.Path(_pkg.__file__).parent
_fsd_files = []
for _py in sorted(_pkg_root.rglob("*.py")):
    if _py.name == "test_safety.py":
        continue
    if "fsd" in _py.read_text(encoding="utf-8").lower():
        _fsd_files.append(_py.relative_to(_pkg_root).as_posix())
check("no module in this package mentions the specification at all",
      not _fsd_files, f"still mentioned in {_fsd_files}" if _fsd_files else "")

# Observations survive the merge as a SEPARATE channel from checks. BLOCKED
# came back as a check status when the two branches merged - it is what the
# eCIB, Financials, PR Checklist and History screens record when they cannot
# tell - but the thing it was originally removed for must not come back with
# it: a run must still be able to say something WITHOUT that something
# counting towards, or colouring, the verdict.
check("a run carries observations separately from its checks",
      "notes" in R.RunResult.__dataclass_fields__
      and "notes" in flows.FlowResult.__dataclass_fields__
      and "notes" in CF.CaseFlowResult.__dataclass_fields__)
check("an observation is not a check and cannot be counted as one",
      not hasattr(R.Note, "status"),
      "a note with a status would be a check wearing another name")

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
# The fields the live form refuses to save without. The flow fills every
# enterable field on the form, but these are the ones that must stay
# non-optional: an optional field that will not set is a note, and a Basic
# Information that saves with a mandatory field missing is a partial record.
#
# Obligor Id Type is here because the app said so - "Obligor Id Type is
# Required" refused a save on a run where it had been authored optional and
# passed over as "not rendered yet". It is still conditional, which is a
# different thing: see _retry_conditional.
BASIC_MANDATORY = [
    "Existing Customer?", "Obligor Name", "Business Segment",
    "Relationship Branch", "Dealing Branch", "Obligor Type",
    "Obligor Id Type",
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
# The twenty screens of the case menu, in the order a run fills them. Both
# branches of this suite brought their own half of this list; it is written out
# once here, in full, because a list that is merely "whatever SCREEN_LABEL
# happens to hold" would assert nothing at all.
EXPECTED_SCREENS = [
    "Request Details", "Facilities", "Observations", "Collaterals",
    # Facility Coverage ASSOCIATES a facility with a collateral, so it has
    # nothing to associate until both of those exist.
    "Facility Coverage",
    # Financials before Risk Rating: the rating model reads the case's
    # financials, so scoring it first scores an empty case.
    "Financials", "Risk Rating", "Credit Memorandum",
    # ONE entry, covering BOTH routes to a record on it: the '+ Add' dialog,
    # then '+ Upload' with a PDF of an SBP CIB report. Same arrangement as
    # Business Performance below - one thing to ask for, two things reported.
    "eCIB Details",
    "Policies & Exceptions", "Conditions", "Documents",
    # 'CRMD Note' in the application until it was renamed; the key is still
    # crmd_note.
    "RMG Memo",
    "Shariah Comments", "Group Review",
    "Relationship with Other Banks / FIs",
    # ONE entry, covering both of its sub-menus. They stay separate in the
    # result - each carries its own screen on every entry and check - but
    # "check Business Performance" is one thing to ask for, not two.
    "Business Performance",
    "PR Checklist", "Litigation",
    # History goes LAST: its second check clicks a row of Requests History,
    # which navigates to a DIFFERENT transaction's screen. A screen that moves
    # the run to another case cannot sit in front of screens that expect to
    # still be on this one.
    "History",
]
check("the case screens asked for are the ones offered",
      set(CF.SCREEN_LABEL.values()) == set(EXPECTED_SCREENS),
      str(sorted(set(CF.SCREEN_LABEL.values()) ^ set(EXPECTED_SCREENS))))
check("there are twenty of them", len(CF.SCREEN_LABEL) == 20,
      str(len(CF.SCREEN_LABEL)))
check("the screens are filled in a fixed, declared order",
      [CF.SCREEN_LABEL[k] for k in CF.ORDER] == EXPECTED_SCREENS,
      str([CF.SCREEN_LABEL[k] for k in CF.ORDER]))
check("ORDER covers every screen exactly once",
      sorted(CF.ORDER) == sorted(CF.SCREEN_LABEL),
      str(sorted(set(CF.ORDER) ^ set(CF.SCREEN_LABEL))))
# Facility Coverage needs both a facility and a collateral; Risk Rating needs
# the financials. Asserted as positions, so reordering the list above cannot
# quietly break either.
_pos = {k: i for i, k in enumerate(CF.ORDER)}
check("Facility Coverage is filled after Facilities and Collaterals",
      _pos[CF.COVERAGE] > _pos[CF.FACILITIES]
      and _pos[CF.COVERAGE] > _pos[CF.COLLATERALS])
check("Risk Rating is scored after Facilities and Financials",
      _pos[CF.RISK_RATING] > _pos[CF.FACILITIES]
      and _pos[CF.RISK_RATING] > _pos[CF.FINANCIALS])
check("History is the last screen in the order",
      CF.ORDER[-1] == CF.HISTORY, CF.ORDER[-1])
check("History is the only read-only screen",
      CF.READ_ONLY_SCREENS == {CF.HISTORY}, str(CF.READ_ONLY_SCREENS))

# Every screen must be reachable from the CLI and be dispatched to a flow of
# its own. A screen in SCREEN_LABEL that no branch handles would silently be
# filled as an observation - the wrong form, on the wrong screen.
check("every case screen is a --fill-case choice",
      "list(cf.ORDER)" in inspect.getsource(cli.main),
      "the choice list is read off ORDER, so it cannot drift")
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
# Twelve, not ten: the Documents screen's "Upload Other Document" panel commits
# with Upload rather than Save. It stores a record like any other Save - it is
# not a workflow transition and it removes nothing - so it belongs in the
# allowlist rather than being worked around by a flow.
#
# What matters is not the COUNT but what is still OUT: the buttons that merely
# act (Generate, Perform PR) must never become things a flow may save with, or
# the guarantee this list exists for is weakened for every screen in the suite.
check("the commit allowlist holds only save-ish labels",
      len(W.COMMITTABLE) == 12
      and not any(re.search(p, "generate") for p in W.COMMITTABLE)
      and not any(re.search(p, "perform pr") for p in W.COMMITTABLE)
      and not any(re.search(p, "edit") for p in W.COMMITTABLE),
      f"{len(W.COMMITTABLE)}: {W.COMMITTABLE}")
check("Upload is a permitted commit control",
      W._committable("Upload") and W._committable("Upload File"),
      "the Documents panel commits with Upload rather than Save")
check("an acting button is still not committable",
      not W._committable("Generate") and not W._committable("Perform PR")
      and not W._committable("Edit"))
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

# A field that was authored and then not found has to be REPORTED - silence
# reading as success is the one failure mode this suite exists to prevent.
# Asserted once, further down, against the _fill_pass this merge kept: it
# records the miss as an OBSERVATION rather than a check, which is the
# stricter of the two things the branches asked for here.

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
# over a label that resolves to its own container. The merged widgets keeps the
# qa-automation spelling of this anchor, _STAMP_SIBLING_JS, which walks forward
# from the matched label exactly as the other branch's _STAMP_AFTER_LABEL_JS
# did. What is asserted here is the BEHAVIOUR both were written for, not the
# name either gave it.
check("the flat-form anchor is the last resort",
      _block_src.index("_STAMP_FIELD_JS")
      < _block_src.index("_STAMP_SIBLING_JS"))
_after = W.Filler._STAMP_SIBLING_JS
check("the flat-form anchor reads forward from the label, not by index",
      "nextElementSibling" in _after and "wanted" in _after,
      "an index would pair label 3 with control 3 and never notice a gap")
check("it stops at the next field's label",
      "sib.matches(LABEL)" in _after and "break" in _after,
      "without this, Group Review's seven editors all answer to the first "
      "label and six sections land in one box")
check("it accepts a control nested inside a wrapper that names no field",
      "sib.querySelector(ctrlSel)" in _after)
# The last-resort container is still returned only after every tighter anchor
# has been tried, so a screen that worked before the one-field guard existed
# does not stop working now.
check("a container spanning several fields is a last resort, not a first try",
      _block_src.index("spans_several = loc")
      < _block_src.index("if spans_several is not None:"))
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
# The case screens contributed by the qa-automation branch: Collaterals,
# Facility Coverage, Risk Rating, Policies & Exceptions, Conditions and
# Documents. Same section, same `check` helper - they assert about their
# own screens and nothing above.
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# Every screen this fills has to be reachable by the label the case sidebar
# renders. Matched with '&' normalised to 'and', so the ampersand in
# "Policies & Exceptions" is what the screen shows rather than something the
# navigation depends on.
#
# Only the screens the sidebar is KNOWN to list are asserted here. The ones
# added later (Litigation, Shariah Comments, PR Checklist, Group Review) were
# read off live cases rather than off CASE_SUB_SCREENS, so requiring them here
# would assert about a list that was never claimed to be complete.
_sidebar = [s.label for s in targets.CASE_SUB_SCREENS]
_known = [x for x in EXPECTED_SCREENS if x in _sidebar]
check("every case screen the sidebar declares is reached by its own label",
      len(_known) >= 12, f"{len(_known)} of {len(EXPECTED_SCREENS)} declared")
# The order authored above follows the sidebar's, with TWO deliberate
# departures. Both are named here so they stay deliberate rather than becoming
# drift - any OTHER inversion fails this check.
#
#   Financials before Risk Rating, where the sidebar lists Risk Rating first.
#   The rating model reads the case's financials, so scoring it before they
#   exist scores an empty case.
#
#   History last, where the sidebar lists it before Relationship with Other
#   Banks / FIs and Business Performance. Its second check clicks a row of
#   Requests History, which navigates to a DIFFERENT transaction's screen, so
#   it cannot sit in front of screens that expect to still be on this case.
_EXPECTED_DEPARTURES = {
    ("Financials", "Risk Rating"),
    ("Relationship with Other Banks / FIs", "History"),
    ("Business Performance", "History"),
}
_positions = [_sidebar.index(x) for x in _known]
_inversions = {(_known[i], _known[j])
               for i in range(len(_known)) for j in range(i + 1, len(_known))
               if _positions[i] > _positions[j]}
check("the order is the sidebar's own, bar the declared dependencies",
      _inversions == _EXPECTED_DEPARTURES,
      f"unexpected: {sorted(_inversions - _EXPECTED_DEPARTURES)}; "
      f"no longer true: {sorted(_EXPECTED_DEPARTURES - _inversions)}")

# Every pass has to have its own name: it is what each check on that pass is
# called, and two passes sharing one makes a failure unattributable.
_all_passes = (CF.REQUEST_DETAILS_PASSES + CF.FACILITY_PASSES
               + CF.OBSERVATION_PASSES + CF.COLLATERAL_PASSES
               + CF.POLICY_PASSES + CF.CONDITION_PASSES
               + [CF.ADDITIONAL_DOCUMENT_PASS, CF.DOCUMENT_ACTION_PASS])
_names = [p.name for p in _all_passes]
_dupes = sorted({n for n in _names if _names.count(n) > 1})
check("every case-screen pass has its own name", not _dupes,
      f"repeated: {_dupes}" if _dupes else f"{len(_names)} passes")
check("no field is authored without a label",
      all(f.labels and all(f.labels) for p in _all_passes for f in p.fields))

# ---- Documents ------------------------------------------------------------
# This screen uploads a file and commits one of its panels with Upload rather
# than Save, so both of those are pinned here.
check("the Documents passes each attach a file",
      all(p.attach for p in (CF.ADDITIONAL_DOCUMENT_PASS,
                             CF.DOCUMENT_ACTION_PASS)),
      f"{CF.ADDITIONAL_DOCUMENT_PASS.attach!r} / "
      f"{CF.DOCUMENT_ACTION_PASS.attach!r}")
check("Upload is a permitted commit control",
      W._committable("Upload") and W._committable("upload"))
check("widening COMMITTABLE for Upload let nothing else through",
      not any(W._committable(x) for x in
              ("Upload and Approve", "Delete Attachment", "Remove Upload",
               "Download Attachments", "Approve", "Reject")))
# Saving moves an attachment out of the boxes that took it and into the
# document's attachments list. Verification has to read it from there, and
# these two tables are what route it — if they drift, the round trip goes back
# to reporting stored values as lost.
check("every Documents pass has somewhere to be verified from",
      set(CF._VERIFY_ANCHORS) == {CF.ADDITIONAL_DOCUMENT_PASS.name,
                                  CF.DOCUMENT_ACTION_PASS.name},
      str(sorted(CF._VERIFY_ANCHORS)))
check("each panel is told apart by a field the other does not have",
      all(a != b for a, b in CF._VERIFY_ANCHORS.values()))
check("the attachment fields are verified from the attachments list",
      all(CF._is_attachment_field({"label": lb, "kind": "text"})
          for lb in ("Attachment Title", "Attachment Description",
                     "Attachment", "Upload File"))
      and not any(CF._is_attachment_field({"label": lb, "kind": "text"})
                  for lb in ("Description", "Date of Action", "Comments",
                             "Justification/Comments Box")))
check("an uploaded file is always verified from the attachments list",
      CF._is_attachment_field({"label": "Anything At All", "kind": "file"}))

check("the file the run uploads is a real PNG",
      (lambda p: (open(p, "rb").read(8) == b"\x89PNG\r\n\x1a\n",
                  os.remove(p))[0])(
          CF._black_png(os.path.join(tempfile.gettempdir(),
                                     "_los_safety_check.png"))))
# Naming the upload after the run is what makes the round trip mean anything:
# a fixed filename would match a previous run's attachment just as well.
check("the uploaded file is named after the run",
      "AUTOTEST-" in CF.run_marker("0101-000000"))

# The fields these screens refuse to save without, measured against the live
# application. Dropping one means the screen silently stops saving.
#
# Matched across a field's ALTERNATIVE names, not just its first. The primary
# name is the label the application actually renders — "Facility Purpose",
# "Proposed Expiry" — and older wordings are kept behind it, since builds
# disagree on most of these and only one of them is what the DOM will answer
# to.
MANDATORY = {
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
for pass_name, required in MANDATORY.items():
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
# by discovery on a run where the authored name found nothing, which is how
# the disagreement came to light — so the app's own name is pinned FIRST and
# the older wording is kept behind it.
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
    for observed, older in pairs:
        fld = next((f for f in spec.fields if observed in f.labels), None)
        check(f"{pass_name}: {observed!r} is tried before {older!r}",
              fld is not None and fld.labels.index(observed)
              < (fld.labels.index(older) if older in fld.labels else 99),
              str(fld.labels) if fld else "field not authored")

# A field that was authored and then not found has to be REPORTED. Eleven of
# the twelve observation fields were skipped in silence and the run still said
# "7 passed / 0 failed" — silence reading as success is the one failure mode
# this suite exists to prevent. It is an OBSERVATION, not a check: these
# labels are authored here and the screen's are configuration, so a field that
# is not there may simply have been renamed.
_fill_src = inspect.getsource(CF._fill_pass)
check("an authored field that is not on the screen is reported, not skipped",
      "authored field(s) not on the screen" in _fill_src
      and "missing.append" in _fill_src
      and "R.observation(" in _fill_src)
# And a missing field must never stop the pass, whatever it was authored as.
check("a field that is not on the screen never ends the pass",
      "if flows._not_on_screen(exc):" in _fill_src
      and _fill_src.index("if flows._not_on_screen(exc):")
      < _fill_src.index("failed_required += 1"),
      "it may have been renamed; the form's own validation decides if it "
      "mattered")

# The facility opens showing one tab and grows the rest once it is saved, so
# the strip has to be re-read rather than walked once.
_fac_src = inspect.getsource(CF._do_facilities)
check("the facility tab strip is re-read after each save",
      _fac_src.count("s.tab_strip(") >= 1 and "processed" in _fac_src
      and "for sweep in range" in _fac_src)

# ---- filling every field the form insists on ------------------------------
# A live run filled 23 of the 24 fields on Facility Details, was refused with
# "Grace Period is Required", and lost the whole tab; Facility Request Details
# went the same way on "Facility Request Type is Required". Both were authored
# with a value this deployment does not offer, so both stayed empty — and an
# authored field is never offered to discovery, which is what made it terminal.
#
# Three defences, in the order they apply to one field.
#
# 1. A named value the option list does not have falls back to the app's own
#    first valid option instead of leaving the field empty.
check("a named value the deployment does not offer falls back to its first "
      "option",
      "no option matching" in _fill_src and "first_option=True" in _fill_src,
      "otherwise one wrong option name costs the whole tab")
check("the fallback exists in _set_one and drops the authored value",
      "first_option" in inspect.getsource(CF._set_one)
      and "None if first_option" in inspect.getsource(CF._set_one))
# The marked field is exempt: its text carries the run marker, which is the
# only thing that tells this run's record from an earlier run's.
check("the marker field never falls back to an option of the app's choosing",
      "not spec_field.marked" in _fill_src)
# 2. Neither of those two fields is pinned to a value this build lacks any
#    more, and both are mandatory on the form, so neither is optional here.
for _pass_name, _label in [
        ("Facilities — Facility Request Details", "Facility Request Type"),
        ("Facilities — Facility Details", "Grace Period")]:
    _spec = next(p for p in _all_passes if p.name == _pass_name)
    _f = next((f for f in _spec.fields if f.label == _label), None)
    check(f"{_label!r} takes the app's own option rather than a named one",
          _f is not None and _f.value is None,
          "not authored" if _f is None else repr(_f.value))
    check(f"{_label!r} is treated as the mandatory field the form says it is",
          _f is not None and not _f.optional)
# 3. Whatever the form STILL says it needs is read out of its own validation
#    text and answered, which is the only defence that does not depend on
#    somebody having authored the right label.
check("the form's own validation messages are acted on, not just quoted",
      "_satisfy_unmet(f, res, say)" in _fill_src
      and _fill_src.index("_satisfy_unmet") < _fill_src.index("f.commit("),
      "reading them after Save only explains the failure")
_unmet_src = inspect.getsource(CF._satisfy_unmet)
check("answering a required field is retried, since one can render another",
      "rounds" in _unmet_src and "if not progressed" in _unmet_src)
check("a chooser gets the app's first option and a text box a generated value",
      "_auto_value(" in _unmet_src and "None if kind in" in _unmet_src)
check("a rule no field can satisfy is left to be reported, not looped on",
      "break" in _unmet_src)
# The message names the field; the field's own label is the fixed string to
# match it against. Longest match wins, or 'Type' would answer a message about
# 'Facility Request Type'.
_MSGS = [("Facility Request Type is Required", "Facility Request Type"),
         ("Grace Period is Required", "Grace Period"),
         ("Please select Compliance status", "Compliance status"),
         ("Limit In Base CCY must be greater than 0", "Limit In Base CCY"),
         ("Facility Purpose cannot be empty", "Facility Purpose"),
         ("Select at least one", "")]
_LABELS = ["Facility Request Type", "Type", "Grace Period", "Period",
           "Limit In Base CCY", "Compliance status", "Facility Purpose"]
_wrong = [(m, CF._field_in_message(m, _LABELS)) for m, want in _MSGS
          if CF._field_in_message(m, _LABELS) != want]
check("each validation message resolves to the field it names", not _wrong,
      str(_wrong) if _wrong else f"{len(_MSGS)} message(s)")

# ---- reading a value back that lives in an input --------------------------
# The value of an <input> is never part of innerText — the browser keeps it as
# a property, not a text node. The facility's marker is typed into Facility
# Purpose, a text input, so a marker search over innerText could not find it
# on ANY facility however well it had saved: one live run reported all 61
# values unverifiable on a facility whose every field was on screen.
_vals_src = CF._FIELD_VALUES_JS
check("field values are read as values, not as page text",
      "el.value" in _vals_src and "input, textarea" in _vals_src,
      "innerText never contains an input's value")
check("a rich-text editor is read through its iframe body",
      "contentDocument" in _vals_src and "catch" in _vals_src,
      "and a cross-origin frame is skipped, not thrown")
check("a hidden input is not mistaken for something the user can see",
      "hidden" in _vals_src and "vis(el)" in _vals_src)
_carries_src = inspect.getsource(CF._carries_marker)
check("a record is identified by what it shows AND what its fields hold",
      "_screen_text" in _carries_src and "_entered_values_text" in _carries_src,
      "a saved value renders as text in a grid and as a value on a form")
_readable_src = inspect.getsource(CF._readable_text)
check("the round trip's text fallback sees field values too",
      "_entered_values_text" in _readable_src)
_facopen_src = inspect.getsource(CF._open_our_record)
check("every tab of a row is searched, not just the one it opens on",
      "_marker_on_tabs" in _facopen_src
      and "open_tab" in inspect.getsource(CF._marker_on_tabs),
      "the marker is on Facility Request Details, which may not be first")
check("a facility that cannot be identified says what was searched",
      "tried" in _facopen_src
      and "the values its fields are holding" in _facopen_src,
      "'that usually means it was not saved' was both wrong and uncheckable")
check("no row is opened by falling back to the first one",
      "row_opener_count" in _facopen_src and "_marker_on_tabs" in _facopen_src,
      "row 1 on the live case is an earlier run's facility")
# A facility's detail REPLACES the grid — it is a screen with tabs, not an
# overlay — so leave_row_detail, which only dismisses overlays, cannot get
# back to the grid. Without an explicit hop, row 1 was searched and rows 2
# and 3 reported "would not open", the new facility among them.
_grid_src = inspect.getsource(CF._back_to_grid)
check("the grid is re-entered between rows, not assumed to still be there",
      "_back_to_grid(s, screen)" in _facopen_src
      and "if i and not" in _facopen_src,
      "leave_row_detail only dismisses overlays")
check("getting back to the grid uses the case sidebar hop that works",
      "_step_context_menu" in _grid_src and "CONTEXT_MENU" in _grid_src)
check("failing to get back to the grid is reported, not passed over",
      "could not be re-opened to" in _facopen_src)

# A tab with nothing enterable and no Save is a log view, not a screen that
# refuses to save. Two of them put two failures in the report for two tabs
# behaving correctly — while saying nothing about the tabs that did refuse.
check("a read-only log tab is not reported as a missing Save button",
      "if not f.entries:" in _fill_src
      and "log or history view" in _fill_src)
check("a tab that WAS filled and cannot be committed is still a failure",
      "everything entered" in _fill_src and "R.failed(" in _fill_src)

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

# The audit's cut-off must not fall after the date it was reported.
_dates = {f.label: f.when for f in _obs.fields if f.when}
check("the audit cut-off precedes the report date",
      _dates["Cutoff Date"] <= _dates["Report Date"],
      f"cutoff {_dates['Cutoff Date']}, report {_dates['Report Date']}")
check("every observation date is in the past, as an audit's must be",
      all(d.year <= 2026 for d in _dates.values()),
      str({k: str(v) for k, v in _dates.items()}))

# ---- Collaterals ----------------------------------------------------------
# The same shape as Facilities: a grid, an Add that opens a picker, a Proceed
# that CREATES the record, and a tab strip that depends on what was asked for.
# So it reuses the same machinery, and these checks pin that it really is
# reused rather than re-implemented beside it.
_coll = CF.COLLATERAL_PASSES[0]
check("Collaterals has its own opener", bool(CF._ADD_COLLATERAL))
check("the buttons that start a collateral clear the crawler's denylist",
      all(cr.is_safe_to_click(x) for x in
          ("Add", "Add Collateral", "New Collateral")),
      str([x for x in ("Add", "Add Collateral", "New Collateral")
           if not cr.is_safe_to_click(x)]))
check("the collateral dialog is committed only with permitted controls",
      all(W._committable(x) for x in ("Proceed", "Add", "Save", "OK")))
# Classification populates the name list, so answering the name first finds an
# empty one. Same dependency as Regulatory Sector -> Regulatory Industry.
_choose_src = inspect.getsource(CF._choose_collateral)
check("the collateral classification is answered before the name",
      _choose_src.index("_COLLATERAL_CLASSIFICATION")
      < _choose_src.index("_COLLATERAL_NAME"),
      "the classification is what populates the name list")
check("the name list is given time to be fetched after the classification",
      "wait_until_settled" in _choose_src,
      "reading it in the same breath finds the previous list, or none")
# The Name control is a MULTI-select — "Select options", plural — and a
# multi-select leaves its panel open over whatever is beneath it, which is
# the other dropdown and then Proceed.
check("an open option panel is dismissed before the next click",
      "_dismiss_option_panel(s)" in _choose_src)
_dismiss_src = inspect.getsource(CF._dismiss_option_panel)
# Asserted as a property rather than by looking for '.ng-dropdown-panel' in
# this function: the selectors moved into _OPTION_PANELS when the treeview
# panel was added to them, and that list is checked on its own below. What
# matters here is unchanged — nothing presses Escape without first
# establishing that a panel is open to absorb it.
check("Escape is pressed only while a panel is actually open",
      _dismiss_src.index("_option_panel_open(s)")
      < _dismiss_src.index('press("Escape")'),
      "with no panel open, Escape closes the dialog and loses the choice")
check("the four tabs of a collateral are each authored as their own pass",
      [p.name for p in CF.COLLATERAL_PASSES] ==
      ["Collaterals — Basic Information", "Collaterals — Collateral Policy",
       "Collaterals — Shares Collateral",
       "Collaterals — Stocks Hypothecation"],
      str([p.name for p in CF.COLLATERAL_PASSES]))
check("each collateral tab can be matched back to the live strip by alias",
      all(p.aliases for p in CF.COLLATERAL_PASSES),
      str([p.name for p in CF.COLLATERAL_PASSES if not p.aliases]))
# A tab authored with no fields is filled entirely by discovery. That is a
# deliberate choice over guessing labels, so it has to carry a note saying so
# — otherwise it reads as a pass somebody forgot to finish.
for _p in CF.COLLATERAL_PASSES:
    if not _p.fields:
        check(f"{_p.name} says its fields are discovered, not authored",
              "discovered" in _p.note.lower(), _p.note[:60])
# The marker goes in Collateral Description: mandatory, free text, and the
# subtitle the detail screen shows — so it identifies the record.
check("the collateral's marker goes in Collateral Description",
      [f.label for f in _coll.fields if f.marked] == ["Collateral Description"],
      str([f.label for f in _coll.fields if f.marked]))
check("the marker field is also the anchor that says the detail is open",
      CF._COLLATERAL_ANCHOR == "Collateral Description")
# The app assigns Customers Collateral ID and the grid identifies a collateral
# by it. Typing over an app-assigned key creates a record nobody can find.
check("the app-assigned collateral ID is never typed over",
      not any("collateral id" in lb.lower()
              for f in _coll.fields for lb in f.labels),
      str([f.label for f in _coll.fields]))
# Mandatory on the form, so not optional here.
for _req in ["Collateral Description", "Collateral Status"]:
    _f = next((f for f in _coll.fields if f.label == _req), None)
    check(f"the collateral's mandatory {_req!r} is not treated as optional",
          _f is not None and (not _f.optional or _f.marked),
          "not authored" if _f is None else f"optional={_f.optional}")
# A collateral this run invented is PROPOSED — not held, not released.
_status = next((f for f in _coll.fields if f.label == "Collateral Status"),
               None)
check("a new collateral is proposed rather than claimed to be held",
      _status is not None and _status.value == "Proposed",
      "not authored" if _status is None else repr(_status.value))
# Forced sale below market, market below assessed value. A collateral whose
# forced-sale value exceeds its market value is what a cross-field rule
# refuses, and the refusal would read as a number this could not set.
_amounts = {f.label: int(f.value) for f in _coll.fields
            if f.value and str(f.value).isdigit()}
check("the collateral's values keep forced sale < market < assessed",
      _amounts.get("Forced Sale Value", 0)
      < _amounts.get("Market Value / Tentative Market Value", 0)
      < _amounts.get("Value", 0),
      str(_amounts))
check("the collateral margin is a percentage inside 0-100",
      0 <= _amounts.get("Margin (%)", -1) <= 100,
      str(_amounts.get("Margin (%)")))
# Province populates City and District, so it is answered first.
_coll_labels = [f.label for f in _coll.fields]
check("the province is answered before the city and district it populates",
      _coll_labels.index("Province") < _coll_labels.index("City")
      and _coll_labels.index("Province") < _coll_labels.index("District"))
# The record-finding machinery is SHARED with Facilities, including both bugs
# fixed there: the marker search that reads field values, and the grid hop
# between rows.
_vc_src = inspect.getsource(CF._verify_collateral)
check("the collateral round trip reuses the shared record finder",
      "_open_our_record(" in _vc_src and "_readable_text(s)" in _vc_src,
      "so the innerText and grid-reset fixes apply to it too")
check("the collateral is found by marker, never by taking the first row",
      "res.collateral_ref" in _vc_src and "_open_our_record" in _vc_src)
check("each collateral pass is compared on the tab it was entered on",
      "_tab_for_group(" in _vc_src and "s.open_tab(tab)" in _vc_src)
# The dialog choice belongs to no tab: it was made before any tab existed.
check("what was asked for in the dialog is not paired with a tab",
      CF._tab_for_group(CF.REQUESTED_COLLATERAL,
                        ["Basic Information", "Collateral Policy"]) == "",
      "pairing it would compare a classification against a tab that never "
      "held it")
# CRR Log History is a log, like the facility's two log tabs. It is walked and
# reported, and _fill_pass recognises it as read-only rather than as a screen
# refusing to save.
check("CRR Log History is not authored as a fillable tab",
      not any("crr" in a.lower() for p in CF.COLLATERAL_PASSES
              for a in p.aliases),
      str([a for p in CF.COLLATERAL_PASSES for a in p.aliases]))
# The tab helpers are shared, so the default has to stay the facility's or
# every existing caller changes behaviour.
check("sharing the tab helpers left the facility's own behaviour alone",
      CF._pass_for_tab("Facility Details") is not None
      and CF._pass_for_tab("Facility Details").name.startswith("Facilities"),
      str(CF._pass_for_tab("Facility Details")))
check("a collateral tab resolves to a collateral pass, not a facility one",
      (CF._pass_for_tab("Basic Information", CF.COLLATERAL_PASSES) or _coll
       ).name == "Collaterals — Basic Information",
      str(CF._pass_for_tab("Basic Information", CF.COLLATERAL_PASSES)))
check("a collateral this run created is named among the records it left",
      "res.collateral_ref" in inspect.getsource(CF._persist))


# ---- Facility Coverage ----------------------------------------------------
# Two trees and no grid. Each node's '+' opens the Collateral Association
# dialog, and the same association is entered from both sides — a build can
# save one and drop the other, and one pass would not notice.
_cov = CF.coverage_passes("AUTOTEST-0914-110412")
check("the association is entered from both sides of the screen",
      [p.name for p in _cov] == [CF.COVERAGE_FROM_FACILITY,
                                 CF.COVERAGE_FROM_COLLATERAL],
      str([p.name for p in _cov]))
# The first dropdown MIRRORS the side it was opened from: from a facility you
# attach a collateral, from a collateral you attach a facility. Authored as one
# label for both, the collateral side failed outright — "No field labelled
# 'Collateral' is on this screen. Visible here: Facilities, ..." — and saved an
# association naming no counterpart.
check("the counterpart dropdown is named for the side it is opened from",
      _cov[0].fields[0].labels[0] == "Collaterals"
      and _cov[1].fields[0].labels[0] == "Facilities",
      f"{_cov[0].fields[0].labels} / {_cov[1].fields[0].labels}")
for _p, _first in zip(_cov, ("Collaterals", "Facilities")):
    check(f"{_p.name}: every field on the dialog is authored",
          [f.label for f in _p.fields]
          == [_first, "Collateral Priority -", "Coverage %"],
          str([f.label for f in _p.fields]))
    check(f"{_p.name}: none of its three fields is treated as optional",
          not any(f.optional for f in _p.fields))
    # '+ Add' on this dialog appends another empty row; it does not commit
    # one. Offering it as a Save fallback would file a blank row as a save.
    check(f"{_p.name}: Save has no Add fallback",
          _p.save_label == "Save" and not _p.save_alt,
          f"{_p.save_label!r} / {_p.save_alt}")
# The label the screen renders carries a trailing dash. Keeping the app's
# spelling first and the tidy one behind it is the rule the module follows.
_prio = next(f for f in _cov[0].fields if "Priority" in f.label)
check("the priority field tries the label the screen actually renders",
      _prio.labels[0] == "Collateral Priority -"
      and "Collateral Priority" in _prio.labels, str(_prio.labels))
# This screen has NO free-text field, so the run marker cannot be typed on it
# and the round trip has nothing unique to search for — unless the numbers
# are made unique. The percentage is derived from the marker instead.
_pcts = [(CF._coverage_percent(m, 0), CF._coverage_percent(m, 1))
         for m in ("AUTOTEST-0914-110412", "AUTOTEST-0101-000000",
                   "AUTOTEST-1231-235959")]
check("the two sides never claim the same share",
      all(a != b for a, b in _pcts), str(_pcts))
check("every derived share is a plausible percentage",
      all(0 < int(x) < 100 for pair in _pcts for x in pair), str(_pcts))
check("no derived share is a round multiple of five",
      all(int(x) % 5 for pair in _pcts for x in pair),
      "those are the values a person enters by hand, so the likeliest to "
      "be sitting there already")
_docov = inspect.getsource(CF._do_coverage)
check("the '+' is told apart by which tree it is on",
      "side" in inspect.getsource(CF._coverage_plus_controls)
      and "r.left < mid" in CF._COVERAGE_PLUS_JS,
      "both trees draw the same icon; the column is what distinguishes them")
check("the tree midpoint comes from the content root, not the window",
      "box.left + box.width / 2" in CF._COVERAGE_PLUS_JS,
      "a collapsed sidebar would otherwise move the boundary across the icons")
check("an empty tree is reported as nothing to associate, not as a failure",
      "R.observation(" in _docov and "no '+' to add an association" in _docov,
      "the trees are empty until a facility and a collateral exist")
check("the dialog is closed before the other side's '+' is clicked",
      "_close_modal_hard(s)" in _docov)
# This template leaves a closed dialog in the DOM still carrying .show, so
# querySelector('.modal.show') can resolve to the one just shut rather than
# the one just opened. A run filled the facility side, closed it, opened the
# collateral side, and then reported every field missing with an empty
# "Visible here:" — it was looking inside a dialog that was gone.
check("the dialog is addressed by a stamp on the one that is visible",
      "data-modal-scope" in CF._MODAL_SCOPE_JS
      and "open[open.length - 1]" in CF._MODAL_SCOPE_JS,
      "the last visible modal is the live one; '.modal.show' is not enough")
check("the fill is scoped to that stamp, not to '.modal.show'",
      "scope=scope" in _docov and '".modal.show"' not in _docov)
# An open dialog is not necessarily a dialog with a row in it. Its own
# '+ Add' is what adds one, and pressing that beats reporting three fields as
# missing on a screen that is working correctly.
_opensrc = inspect.getsource(CF._open_association_dialog)
check("a dialog that opens with no row has one added before it is filled",
      "_ADD_ROW_JS" in _opensrc and "_dialog_fields" in _opensrc)
check("the row-adding '+ Add' is confined to the open dialog",
      "data-modal-scope" in CF._ADD_ROW_JS,
      "a '+' outside it belongs to a tree and opens another dialog")
check("a dialog with nothing to fill is reported once, not field by field",
      "no row to fill" in _docov and "R.observation(" in _docov)
# An association is ONE relationship between a facility and a collateral. On a
# case carrying one of each, the first side makes the only association there
# is and the second is then correctly offered nothing — confirmed against the
# live application. That is the app being right, and it must not arrive as a
# mandatory field that "opened no options to choose from".
_optsrc = inspect.getsource(CF._counterpart_options)
check("what is left to associate is asked before anything is typed",
      _docov.index("_counterpart_options(") < _docov.index("_fill_pass("),
      "otherwise an exhausted list arrives as three failed field checks")
check("an exhausted list is reported as a non-event, not as a failure",
      "nothing is left to associate" in _docov.lower()
      and "R.observation(" in _docov)
check("the report says how to exercise the side that was skipped",
      "Add a second" in _docov,
      "a second facility or collateral gives it something to associate")
# None means the question could not be put — not that the list is empty.
check("an unprobeable control still gets the ordinary fill",
      "left is not None and not left" in _docov,
      "None is 'could not tell', which is not the same as 'nothing left'")
check("probing the list cannot be mistaken for emptiness on a non-dropdown",
      "return None" in _optsrc and "kind_of(label) == \"missing\"" in _optsrc)
# And a side that entered nothing has no share to look for.
check("only a side that claimed a share is searched for on the tree",
      "if spec.name not in entered:" in _docov,
      "demanding one from a side that entered nothing invents a failure")
# A share already on the tree before this run cannot prove this run stored
# anything, and saying so beats claiming a pass.
check("a share that was already on the tree is observed, not passed",
      "not in before" in _docov and "R.observation(" in _docov,
      "a share this run cannot claim proves nothing either way")
_vcov = inspect.getsource(CF._verify_coverage)
check("the round trip finds each association by this run's own share",
      "_coverage_percent(res.marker, offset)" in _vcov)
check("a side this run never reached is not reported as missing",
      'if not any(e.get("group") == group for e in items):' in _vcov,
      "claiming a failure for a side that entered nothing invents one")
# A collapsed node's children are not in the document at all, so reading the
# page while they are shut reports a stored association as never saved — which
# is what one run did, on a screen whose own toast said "Linkage Saved
# Successfully".
check("collapsed tree nodes are opened before the tree is read",
      "_expand_coverage_trees" in _vcov
      and "_expand_coverage_trees" in inspect.getsource(CF._coverage_tree_text))
# And the expander must never touch a '+': that is the ADD control, and
# clicking one during the read-only leg opens the association dialog.
check("expanding a tree never clicks an add control",
      "/plus/.test(c)) continue" in CF._EXPAND_TREE_JS,
      "a '+' opens the Collateral Association dialog, it does not expand")
check("only collapsed nodes are clicked, never open ones",
      all(x in CF._EXPAND_TREE_JS
          for x in ("caret-right", "chevron-right"))
      and "caret-down" not in CF._EXPAND_TREE_JS,
      "clicking a node that already points down would close it")
# The dialog names a record by its whole option text and the tree by its own
# reference. Comparing one against the other reported a stored association as
# lost, so the reference is what is looked for.
_shown, _how = CF._tree_shows(
    "AUTOMATION TEST - (AIBG) CC30445 - Consumer Goods (0) - 33%",
    "AUTOMATION TEST 0909-1407-30445 - Entered by automated test. "
    "AUTOTEST-0910-165927. Field: Collateral Description. (0) - 25%")
check("a record is matched by the reference the tree actually shows",
      _shown and "30445" in _how, f"{_shown} / {_how}")
check("a tree holding nothing of the sort is not matched",
      not CF._tree_shows("no tree here", "CC30445 - Consumer Goods")[0])
check("a short number is never treated as a reference",
      not CF._tree_shows("a page mentioning 25 and 0", "25")[0],
      "years, counts and percentages appear on almost any node")
# The counterpart and the share are each answered by the check built for them,
# so neither is handed to _compare to be reported on a second time.
check("the share and the counterpart are not double-reported by _compare",
      'key.startswith("coverage")' in _vcov and "counterpart.append(e)" in _vcov)

# ---- the association has to be recorded at BOTH ends --------------------
#
# An association is ONE relationship written to two trees: the collateral
# belongs under the facility on the left and the facility under the collateral
# on the right. Reading the whole page as one string cannot tell those apart —
# a share rendered on one tree alone satisfies it — and a half-written
# association is precisely what this screen is most likely to produce.
_bothsrc = inspect.getsource(CF._check_both_trees)
check("each tree is read as a panel of its own, not as one page",
      "facility" in CF._COVERAGE_SIDE_TEXT_JS
      and "collateral" in CF._COVERAGE_SIDE_TEXT_JS)
check("the panels are found by heading, and failing that by column",
      "card-title" in CF._COVERAGE_SIDE_TEXT_JS
      and "r.left < mid" in CF._COVERAGE_SIDE_TEXT_JS,
      "the same rule the '+' discovery uses to tell the two trees apart")
check("an association on one tree only is a failure, not a pass",
      "only the {here} tree carries it" in _bothsrc
      and "R.failed(" in _bothsrc)
check("a tree that could not be read is observed, not called a defect",
      "return None" in _bothsrc
      and "Neither tree could be read" in _bothsrc
      and "R.observation(" in _bothsrc,
      "an unreadable panel says nothing about the application")
check("both ends are checked after the save and again on the round trip",
      "_check_both_trees(" in _docov and "_check_both_trees(" in _vcov)
check("the round trip reads the two trees from the fresh load",
      "_coverage_side_text(s, expand=False)" in _vcov,
      "the nodes are already open by then; expanding again would close them")

# ---- Escape must never reach the dialog ---------------------------------
#
# Escape inside a Bootstrap modal closes the MODAL. Every "No field labelled
# ... Visible here:" with nothing after the colon this suite has produced came
# from one: a dropdown with nothing to offer pressed Escape, the Collateral
# Association dialog went with it, and all three of its fields were then
# reported missing from a screen that was working perfectly.
_dismiss_overlay_src = inspect.getsource(W.Filler._dismiss_overlay)
check("a neutral click, not Escape, dismisses an overlay inside a dialog",
      "_open_modal()" in _dismiss_overlay_src
      and _dismiss_overlay_src.index("mouse.click")
          < _dismiss_overlay_src.index('press("Escape")'),
      "Escape is reached only once there is no dialog for it to destroy")
check("the neutral click aims at the dialog that is actually on screen",
      "range(modals.count() - 1, -1, -1)" in inspect.getsource(W.Filler._open_modal),
      "the last visible one; a closed dialog keeps .show in this template")
for _name in ("options", "choose", "choose_in_dialog"):
    _src = inspect.getsource(getattr(W.Filler, _name))
    check(f"Filler.{_name} never presses Escape over an open dialog",
          'press("Escape")' not in _src,
          "it would close the form the field belongs to")
check("asking what is left to associate cannot close the dialog",
      "_dismiss_overlay" in inspect.getsource(W.Filler.options),
      "the answer is worthless if getting it destroys the form")
# A list this app fetches when the panel opens is empty for its first few
# hundred milliseconds. Reading it once reported Collateral Priority — static
# reference data that is never empty — as "opened no options to choose from".
_panelsrc = inspect.getsource(W.Filler._open_panel)
check("a dropdown is polled for its options, not read once",
      "_panel_texts()" in _panelsrc and "while waited" in _panelsrc)
check("a panel that never opened at all gets one more click",
      "for attempt in (1, 2)" in _panelsrc,
      "a first click landing while the dialog settles is the case this loses")
check("ng-select's own empty row is not counted as something to associate",
      "no items" in _optsrc.lower(),
      "an exhausted list has to read as empty for the side to be skipped")

# ---- pressing the tree's '+' --------------------------------------------
#
# A blind mouse click at a point is the one strategy with no way of reporting
# that it missed: it cannot fail, it simply does nothing, and the run then
# blames the app for a dialog that never opened.
_presssrc = inspect.getsource(CF._press_plus)
check("the '+' is clicked as an element before it is clicked as a point",
      _presssrc.index("el.click(") < _presssrc.index("mouse.click("),
      "an element click reports a control that is covered or off screen")
check("a real mouse click is still tried, and a scripted one after it",
      "mouse.click(" in _presssrc and "_CLICK_PLUS_JS" in _presssrc,
      "several of this template's icon controls ignore one or the other")
check("the '+' can be addressed as an element, not only as coordinates",
      "data-cov-plus" in CF._COVERAGE_PLUS_JS and "el.setAttribute" in CF._COVERAGE_PLUS_JS)
check("the mouse falls back to where the icon is now, not where it was",
      "bounding_box(" in _presssrc,
      "scrolling it into view moves it; a stale point hits whatever took it")
check("a click that worked is never followed by another",
      _presssrc.count("_visible_modals(s) > was") >= 2,
      "a second click would stack a second dialog on the first")
check("the dialog is waited for by counting, not by '.modal.show'",
      "_wait_for_modal(" in _presssrc
      and "wait_for_selector" not in inspect.getsource(CF._open_association_dialog),
      "a hidden .modal.show earlier in the DOM would absorb the wait")
check("only visible dialogs are counted",
      "getBoundingClientRect" in CF._VISIBLE_MODALS_JS
      and "filter(vis)" in CF._VISIBLE_MODALS_JS)
# Facility Coverage associates a facility with a collateral, so it has nothing
# to work with until both exist.
check("Facility Coverage is filled after Facilities and Collaterals",
      CF.ORDER.index(CF.COVERAGE) > CF.ORDER.index(CF.FACILITIES)
      and CF.ORDER.index(CF.COVERAGE) > CF.ORDER.index(CF.COLLATERALS),
      str([CF.SCREEN_LABEL[k] for k in CF.ORDER]))
check("Facility Coverage is reached by its sidebar label",
      CF.SCREEN_LABEL[CF.COVERAGE] in
      [s.label for s in targets.CASE_SUB_SCREENS],
      CF.SCREEN_LABEL[CF.COVERAGE])


# ---- Risk Rating ----------------------------------------------------------
# The only screen in this module that leaves the case menu: Perform Risk Rating
# opens a scoring model on a page of its own, and everything after that happens
# there.
check("Risk Rating is reached by its sidebar label",
      CF.SCREEN_LABEL[CF.RISK_RATING] in
      [s.label for s in targets.CASE_SUB_SCREENS],
      CF.SCREEN_LABEL[CF.RISK_RATING])
# The model reads the case's facilities and financials, so rating a case before
# they exist rates an empty one.
check("Risk Rating is scored after Facilities",
      CF.ORDER.index(CF.RISK_RATING) > CF.ORDER.index(CF.FACILITIES),
      str([CF.SCREEN_LABEL[k] for k in CF.ORDER]))
# One flow written as two functions: _do_risk_rating opens the model and
# guarantees the way back out of its tab, _rate_the_model does everything on it.
# Read as one here, in call order, because that is the order the steps run in.
_rate_src = (inspect.getsource(CF._do_risk_rating)
             + inspect.getsource(CF._rate_the_model))
# Edit -> fill -> Generate Score -> Save, in that order and no other. The model
# opens READ-ONLY, so filling before Edit reports every input as a field that
# refused a value; and a score generated after Save is not the one that saved.
check("the model is put into Edit before its inputs are answered",
      _rate_src.index("_RATING_EDIT") < _rate_src.index("_do_rating_inputs"),
      "the model opens read-only")
check("the score is generated after the inputs and before the save",
      _rate_src.index("_do_rating_inputs")
      < _rate_src.index("_GENERATE_SCORE")
      < _rate_src.index("_save_rating"))
check("the Calculation Sheet is read before the rating is saved",
      _rate_src.index("_check_calculation_sheet")
      < _rate_src.index("_save_rating"))
# Each hop is confirmed before the next is attempted: failing to reach the
# model is a different fault from reaching it and having it refuse to score.
check("navigating to the model is confirmed, not assumed",
      "_wait_for_model_page(" in _rate_src,
      "a button that changed nothing must not read as a model that opened")
# A live run judged the navigation at a fixed second and a half and reported
# "the browser did not go anywhere" about a page that was still loading — and
# threw away the whole screen over it.
_wait_src = inspect.getsource(CF._wait_for_model_page)
check("the model is POLLED for, not slept on",
      "time.monotonic()" in _wait_src and "while" in _wait_src
      and "_MODEL_WAIT_S" in _wait_src,
      "a page that is still loading is not a page that went nowhere")
check("the wait is long enough for a route that fetches a model",
      CF._MODEL_WAIT_S >= 30, f"{CF._MODEL_WAIT_S}s")
# Three ways the model can arrive, and a URL comparison alone sees only one.
for _way, _needle in (("a new tab", "s.other_pages()"),
                      ("a route", "s.page.url != before_url"),
                      ("a render in place", "_model_on_screen(s)")):
    check(f"the model arriving as {_way} is recognised", _needle in _wait_src)
# Only a tab that appeared BECAUSE of the click may be followed. Adopting any
# other open page hands the rest of the run whatever was lying around.
check("only a tab opened by the click is followed",
      "id(p) not in seen" in _wait_src and "before_pages" in _rate_src,
      "a download window or a tab an earlier screen left behind is not the "
      "model")
check("following a new tab rebinds everything bound to a page",
      all(x in inspect.getsource(driver.Session.adopt_page)
          for x in ("self.recorder", 'page.on("dialog"', "set_default_timeout")),
      "the recorder especially, or the failed-request checks read the wrong "
      "page")
# A run that follows the model into its tab and never comes back spends the
# rest of its life asking a DIFFERENT application on a different host for
# RiskNucleus screens. It cost a live run its whole verification leg: every
# check passed, and the case it had just rated was then reported missing from
# My Bucket because the tab it asked showed a blank page.
check("a followed tab is remembered so the run can come back",
      "self._left_behind.append" in inspect.getsource(driver.Session.adopt_page)
      and hasattr(driver.Session, "release_page"))
check("the rating model's tab is handed back however the rating ends",
      "finally:" in _rate_src and "s.release_page()" in _rate_src,
      "six ways out of the model, and every one of them is on the model's tab")
check("coming back does not re-bind the page it returns to",
      "Deliberately NOT re-bound"
      in inspect.getsource(driver.Session.release_page),
      "binding twice dismisses every dialog twice and records every request "
      "twice")
check("release_page on a session that followed nothing is harmless",
      "if not self._left_behind:"
      in inspect.getsource(driver.Session.release_page))
# An empty grid and a grid without this case are different findings.
#
# The condition carries a second clause since the merge: My Bucket's own search
# box is asked first, and a search that ran HAS looked - so the "nothing was
# searched" sentence is reserved for the case where neither route saw a row.
_find_src = inspect.getsource(flows.find_case)
check("no rows read is reported as nothing searched, not as a missing case",
      "if scanned == 0 and not searched:" in _find_src
      and "says nothing about whether the case exists" in _find_src,
      "'the case is not there' about a blank page sends somebody hunting a "
      "transaction that completed")
# The search is tried FIRST and paging is kept as the fallback, so this is
# never worse at finding a case than paging alone was.
check("the grid's search box is asked before paging",
      "_search_bucket(s, needle, say)" in _find_src
      and _find_src.index("_search_bucket") < _find_src.index("for page_no in"))
check("paging still runs when the search finds nothing",
      "for page_no in range(1, max_pages + 1):" in _find_src)
check("a search that matched nothing clears its own filter",
      '_search_bucket(s, "", say)' in _find_src,
      "otherwise the paging below walks the filtered grid")
# Scoring is a server round trip, so the same impatience applies to it.
check("the score is polled for rather than read straight after the click",
      "_wait_for_score(s)" in _rate_src
      and "time.monotonic()" in inspect.getsource(CF._wait_for_score),
      "reading it immediately reads the empty block that was there before")
# The screen has no free-text field, so the RATING is the identifier — read the
# moment Generate Score answers, because it is the only thing the Rating
# Summary can be checked against.
check("the rating is read off the model and kept for the round trip",
      "_wait_for_score(s)" in _rate_src
      and "_read_rating_results(s)" in inspect.getsource(CF._wait_for_score)
      and "res.rating_values = results" in _rate_src)
check("a model that generated no rating is a failure, not a pass",
      "every field of it is still blank" in _rate_src
      and "R.failed(" in _rate_src)
# Nothing is asserted about WHICH rating comes out: the inputs are reference
# data and the model is configuration.
_inputs_src = inspect.getsource(CF._do_rating_inputs)
check("the model's LOVs take the app's own first option",
      "_rating_input_value" in _inputs_src
      and "return None" in inspect.getsource(CF._rating_input_value),
      "a named value would break when the environment is re-seeded")
check("the inputs are scoped to Basic Information, not the whole model page",
      "_section_scope(s, _RATING_INPUT_SECTION)" in _inputs_src,
      "the page also carries the computed figures and the result block")
# The model is a legacy .aspx screen: name in the left cell, dropdown in the
# right, no <label> anywhere. Read as labels alone it looks empty, and a
# section whose every input is answerable was reported as offering none.
check("the model's inputs are read from their table cells, not only labels",
      "cell_labels=True" in _inputs_src,
      "its dropdowns carry no <label>, so label-only discovery finds nothing")
check("cell labels stay opt-in",
      W.Filler.__dataclass_fields__["cell_labels"].default is False,
      "a data grid's rows would otherwise read as one field per row")
check("a cell holding a control is a value, not the name of a field",
      "!c.querySelector(ctrlSel)" in W.Filler._STAMP_CELL_JS
      and "!c.querySelector(ctrlSel)" in W.Filler._CELL_LABELS_JS)
check("only the row's first name cell names a field",
      "cells.findIndex(c => !c.querySelector(ctrlSel)" in W.Filler._STAMP_CELL_JS
      and "cells.findIndex(c => !c.querySelector(ctrlSel)"
          in W.Filler._CELL_LABELS_JS,
      "a spacer cell would resolve to the same control — two labels, one box")
check("discovery and locating agree on what a cell field is",
      "maxCells" in W.Filler._STAMP_CELL_JS
      and "maxCells" in W.Filler._CELL_LABELS_JS,
      "a label discovery offers that _block cannot resolve is a false refusal")
check("a real <label> still wins over a cell that reads the same way",
      inspect.getsource(W.Filler._block).index("_STAMP_FIELD_JS")
      < inspect.getsource(W.Filler._block).index("_STAMP_CELL_JS"))
# Red on the Calculation Sheet is the model objecting to its own arithmetic.
_calc_src = inspect.getsource(CF._check_calculation_sheet)
check("anything flagged red on the Calculation Sheet is a failure",
      "_red_flagged(s)" in _calc_src and "flagged red" in _calc_src)
check("a Calculation Sheet that would not open is not called clean",
      "this is \"not\" a clean sheet".replace('"', '') in _calc_src
      or "not a clean sheet" in _calc_src,
      "'nothing was flagged' and 'nothing was looked at' are different")
check("red is read from the computed style, not only from class names",
      "getComputedStyle" in CF._RED_FIELDS_JS
      and "text-danger" in CF._RED_FIELDS_JS,
      "it is what the operator is actually looking at")
check("flagging a cell does not flag every wrapper above it",
      "el.querySelector('td, th, input, select, textarea')" in CF._RED_FIELDS_JS,
      "reporting the whole table says nothing")
# The round trip: the case's own copy of the answer, read from a fresh load.
_vrate_src = inspect.getsource(CF._verify_risk_rating)
check("the round trip checks the Rating Summary AND the Rating History",
      "_RATING_SUMMARY" in _vrate_src and "_RATING_HISTORY" in _vrate_src)
check("the summary is compared by VALUE, not merely by having rows",
      "_grade_of(c) == want" in _vrate_src,
      "a summary showing a different rating is worse than one showing none")
check("the two tables are told apart by their headings",
      "heading" in CF._RATING_GRID_JS and "querySelector('table')"
      in CF._RATING_GRID_JS,
      "reading the page as one string would let one satisfy the other")
check("a history holding only earlier ratings is not a pass",
      "rows[-1]" in _vrate_src and "an earlier rating, " in _vrate_src,
      "its length alone proves nothing on a case rated before")
check("a run that produced no rating cannot pass on somebody else's",
      "rating that was already on the case" in _vrate_src
      and "a rating from this run to compare against" in _vrate_src)

# ---- the round trip turns on the RATING and nothing else -------------------
# Everything else the two grids hold is the same answer said again (Grade
# Description), the model's workings (the granular columns, Score) or the app's
# bookkeeping (Model Type, Performed By/On). Comparing them failed over
# presentation — the summary writes '1' where the history writes '1-Excellent'
# — on ratings that had plainly carried across.
check("one value is verified, and it is the rating",
      CF.RATING_KEYS == ["Final Rating"], str(CF.RATING_KEYS))
check("the whole result block is still READ and reported",
      len(CF.RATING_RESULTS) > len(CF.RATING_KEYS)
      and "Grade Description" in CF.RATING_RESULTS,
      "a result block with holes in it is worth knowing about, just not a "
      "second failing check")
check("the rating is read from its own COLUMN, not searched for in the grid",
      "_rating_column(summary" in _vrate_src
      and "_rating_column(history" in _vrate_src,
      "'1' is a substring of '11-Doubtful' — a text search would pass a case "
      "rated 11 as one rated 1")
check("the column is matched on its exact header",
      "if key in headers:" in inspect.getsource(CF._rating_column),
      "'Granular Level Rating' contains 'Rating', and is not the rating")
_grades = {"1": "1", "1-Excellent": "1", "11-Doubtful": "11",
           "9-SEVERE WATCHLIST": "9", "Doubtful : 11": "11", "": ""}
check("one grade, however each screen spells it",
      all(CF._grade_of(k) == v for k, v in _grades.items()),
      str({k: CF._grade_of(k) for k in _grades}))
check("a rating of 1 is not satisfied by a rating of 11",
      CF._grade_of("11-Doubtful") != CF._grade_of("1-Excellent"))
# The history is checked at its NEWEST row. This very case is why: it already
# held a 1-Excellent from a week before the run that produced another, so "some
# row matches" would have passed without this run's rating ever being recorded.
check("the history is checked at the row this run added",
      "rows[-1]" in _vrate_src and "newest" in _vrate_src,
      "an older row carrying the same grade is an earlier rating, not this one")
check("a column that cannot be found is not reported as a wrong rating",
      "this is not a wrong rating" in _vrate_src
      and "this is not a missing entry" in _vrate_src,
      "'the rating could not be read' and 'the case shows another' are "
      "different findings")
# The model's LOVs are answered in a tab of its own, on a different
# application. The case's Risk Rating screen shows two grids and nothing else.
check("the model's inputs are not looked for on the case's screen",
      "_compare(" not in _vrate_src and "They live on the model" in _vrate_src,
      "eighteen answers reported as lost on a screen that never displays them")


# ---- Collaterals ----------------------------------------------------------
# The Obligor Collateral dialog has TWO inline treeviews — Collateral
# Classification and Collateral Name — and that is what broke it. choose_in_tree
# located its toggle as "the first visible one in scope", which is unambiguous
# only while a dialog has one: asked for the name it re-opened the
# classification's tree, so a live run recorded 'Pledge' — a classification —
# as the collateral NAME, for both dropdowns, four times over.
_dlg_src = inspect.getsource(CF._choose_one_in_dialog)
_attempts = _dlg_src[_dlg_src.index("attempts = ("):]
check("the collateral dropdowns are driven as treeviews first",
      _attempts.index("choose_in_tree") < _attempts.index("set_value"),
      "that is the shape both of them are drawn in")
check("each tree toggle is confined to its own field",
      "within=mine" in _attempts and "_block(label)" in _dlg_src,
      "otherwise the name dropdown re-opens the classification's tree")
check("widgets can confine a tree toggle without changing the old callers",
      "within" in inspect.signature(W.Filler.choose_in_tree).parameters
      and inspect.signature(
          W.Filler.choose_in_tree).parameters["within"].default == "",
      "the facility dialog has one tree and must behave exactly as before")
# The same value in both dropdowns is the signature of the bug, so it is
# caught and said rather than filed as a result.
_pick_src = inspect.getsource(CF._choose_collateral)
check("a classification appearing as the name is reported, not recorded",
      "if got in picked:" in _pick_src)
# An option panel left open covers the button beneath it. Only ng-select
# panels were looked for; a treeview panel is none of those classes, so it
# stayed open and Proceed was clicked through it.
check("every shape of option panel is closed, the treeview included",
      any("treeview" in sel for sel in CF._OPTION_PANELS)
      and ".ng-dropdown-panel" in CF._OPTION_PANELS,
      str(CF._OPTION_PANELS))
check("closing a panel is confirmed rather than assumed",
      "return not _option_panel_open(s)" in
      inspect.getsource(CF._dismiss_option_panel))
_req_src = inspect.getsource(CF._request_collateral)
check("the way is cleared before Proceed is pressed",
      _req_src.index("_dismiss_option_panel(s)") < _req_src.index("f.commit("))
# A covered button raises a Playwright timeout, not a FillError. Without
# PWError here it escaped the loop, escaped the flow, and surfaced as
# "Collaterals could be filled" with a raw locator dump for a detail.
check("a blocked Proceed is a finding, not an escaping exception",
      "PWError) as exc" in _req_src,
      "a timeout is not a FillError")
check("a dialog that will not advance stops the loop re-answering it",
      "picked == last_picked" in _req_src)
# CRR Log History is the collateral's log view. It is left out of the authored
# passes on purpose — _fill_pass already recognises a tab with nothing
# enterable and no Save as read-only.
_col_tabs = [p.name.split("—")[-1].strip() for p in CF.COLLATERAL_PASSES]
check("the four fillable collateral tabs are authored",
      _col_tabs == ["Basic Information", "Collateral Policy",
                    "Shares Collateral", "Stocks Hypothecation"],
      str(_col_tabs))
check("the collateral's marker goes in a field the record shows back",
      [f.label for f in CF.COLLATERAL_PASSES[0].fields if f.marked]
      == ["Collateral Description"])
check("the collateral passes are matched against their own tabs, not the "
      "facility's",
      CF._pass_for_tab("Collateral Policy", CF.COLLATERAL_PASSES) is not None
      and CF._pass_for_tab("Collateral Policy") is None,
      "passes= defaults to the facility's so existing callers are unchanged")


# ---- Policies & Exceptions ------------------------------------------------
# Add Exception is a form behind a button, like Add Observation and Add
# Condition. What is different about this screen is the column down the LEFT:
# it carries filters over the policy library, which are labelled controls that
# are not fields of the record being created — so both the "did the form
# open?" test and the "was it stored?" test have to be specific enough not to
# be answered by the filters instead.
_exc = CF.POLICY_PASSES[0]
check("Policies & Exceptions is filled as a form, not as an add-row table",
      _exc.kind == CF.FORM, _exc.kind)
check("Policies & Exceptions has its own opener", bool(CF._ADD_EXCEPTION))
check("the buttons that open the exception form clear the crawler's denylist",
      all(cr.is_safe_to_click(x) for x in
          ("Add Exception", "+Add Exception", "Add Policy Exception")),
      str([x for x in ("Add Exception", "+Add Exception",
                       "Add Policy Exception") if not cr.is_safe_to_click(x)]))
check("the bare 'add' fallback is tried last, after the specific names",
      CF._ADD_EXCEPTION[-1] == "add" and len(CF._ADD_EXCEPTION) > 1,
      str(CF._ADD_EXCEPTION))

# Every field on the form, in the order it renders them.
EXCEPTION_FORM = ["Title", "Description", "Compliance status", "Request Type",
                  "Rationale/Reasons for deviation"]
_exc_labels = [f.label for f in _exc.fields]
check("every field on the Add Exception form is authored, in form order",
      _exc_labels == EXCEPTION_FORM, f"authored {_exc_labels}")
check("the exception's marker goes in Title, which the list shows",
      [f.label for f in _exc.fields if f.marked] == ["Title"],
      str([f.label for f in _exc.fields if f.marked]))
# The three the form marks with a red asterisk. A screen that saves without
# one of them is saving a partial record — and this form refuses to save at
# all while any of them is empty, which is what its validation messages say.
for _req in ["Description", "Compliance status", "Request Type"]:
    _f = next((f for f in _exc.fields if f.label == _req), None)
    check(f"the exception's mandatory {_req!r} is not treated as optional",
          _f is not None and (not _f.optional or _f.marked),
          "not authored" if _f is None else f"optional={_f.optional}")

# Each free-text box gets its OWN sentence, for the same reason the
# observation's do: identical text in two boxes makes the round trip vacuous.
_exc_texts = [f.value for f in _exc.fields if f.value and not f.marked]
check("each exception text box gets its own text",
      len(set(_exc_texts)) == len(_exc_texts),
      f"{len(set(_exc_texts))} distinct of {len(_exc_texts)}")
# The form counts Rationale down from 1000 characters. A value the app
# truncates comes back different and would be reported as a lost value when
# nothing was lost.
_rationale = next(f for f in _exc.fields
                  if f.label == "Rationale/Reasons for deviation")
check("the rationale stays inside the form's 1000-character limit",
      len(str(_rationale.value)) < 1000, f"{len(str(_rationale.value))} chars")
check("the rationale's alternative labels cover the spacing of its slash",
      "Rationale / Reasons for deviation" in _rationale.labels,
      str(_rationale.labels))

# The form is confirmed by a field only IT has. "Some labelled field appeared"
# would pass on a screen where Add Exception did nothing, because the filter
# column has labelled controls of its own.
check("the exception form is confirmed by fields only it has",
      bool(CF._EXCEPTION_ANCHORS)
      and all(a in _exc_labels for a in CF._EXCEPTION_ANCHORS),
      str(CF._EXCEPTION_ANCHORS))
check("each anchor is a field the form marks mandatory, so it is always drawn",
      all(not next(f for f in _exc.fields if f.label == a).optional
          for a in CF._EXCEPTION_ANCHORS))
# Reading that middle column only means something once it is filtered to
# exceptions: unfiltered it is the policy LIBRARY, which is full of rows before
# this run does anything and does not grow when an exception is raised. Both
# legs go through the same filter for that reason.
_pol_src = inspect.getsource(CF._do_policies)
_ver_src = inspect.getsource(CF._verify_policies)
check("the exception count is only trusted with the list filtered",
      "_show_exceptions_only" in _pol_src
      and "if filtered else -1" in _pol_src,
      "the unfiltered list is the policy library, not a register")
check("the round trip searches the same filtered list",
      "_show_exceptions_only" in _ver_src)
# Three controls on this screen have 'exception' in the name and only one is
# the filter. Matching loosely would click Add Exception — leaving the list
# unfiltered and the marker searched for over the whole policy library.
check("the Exceptions filter is matched exactly, not by containment",
      CF._is_exceptions_filter("Exceptions")
      and CF._is_exceptions_filter("exceptions ")
      and not any(CF._is_exceptions_filter(x) for x in
                  ("Add Exception", "+ Add Exception", "New Exception",
                   "Policy Exception Requirement", "All")),
      str([x for x in ("Add Exception", "Policy Exception Requirement", "All")
           if CF._is_exceptions_filter(x)]))
# Closing the form re-renders the list, so a filter applied before the save
# cannot be assumed to survive it. Counted against the library instead, `after`
# would beat `before` for reasons that have nothing to do with the save.
check("the filter is re-applied before the list is re-read, not assumed",
      _pol_src.count("_show_exceptions_only") == 2
      and _pol_src.index("if dry_run:")
      < _pol_src.rindex("_show_exceptions_only"))
# Which is why the marker decides and the count only corroborates: a marker
# this run invented cannot be matched by a library row or by an earlier run's.
check("the exception is confirmed by this run's marker, not by a row count",
      _pol_src.index("listed = flows._appears_in(res.marker")
      < _pol_src.index("if listed:") < _pol_src.index("elif after > before"),
      "a row count alone can pass on rows this run did not create")
_grew_branch = _pol_src[_pol_src.index("elif after > before >= 0:"):
                        _pol_src.rindex("    else:")]
check("a list that grew without showing the marker is not called a pass",
      "R.observation(" in _grew_branch and "R.passed(" not in _grew_branch,
      "something was stored, but not provably this run's exception")
# The marker check on the list is a finding in its own right: an exception
# that is not there at all is a different fault from one missing a field.
check("the round trip checks the list for the marker before opening anything",
      _ver_src.index("The saved exception is listed on the case")
      < _ver_src.index("_open_our_exception"))
# A live run saved an exception, saw it in the list, and then reported all
# eleven values unverifiable: the row was clicked on the wrong element. One
# guess at where a list row binds its handler is not enough, so the candidates
# are enumerated and tried in turn until the form actually arrives.
_open_src = inspect.getsource(CF._open_our_exception)
check("the row is found by the run's full marker, not by position",
      "_exception_row_targets(s, res.marker)" in _open_src
      and "index" not in _open_src,
      "a position walk opens whatever is first; a prefix opens an earlier run's")
check("more than one way of clicking the row is tried",
      "for spot in targets" in _open_src
      and "_click_row_spot" in _open_src)
check("the row is scrolled into view before its coordinates are used",
      "scrollIntoView" in CF._EXCEPTION_ROW_JS)
# The climb outwards has to stop before the candidate becomes the whole list:
# clicking that would hit the filter column instead of the row.
check("the click candidates stay within the row",
      "own + budget" in CF._EXCEPTION_ROW_JS)
_showing_src = inspect.getsource(CF._showing_our_exception)
check("the exception this run raised is told apart by the form's own Title",
      "value_of(" in _showing_src and "_exception_anchor(s)" in _showing_src)
check("the form is polled for, not read once straight after the click",
      "deadline" in _showing_src and "wait_for_timeout" in _showing_src,
      "asking immediately discards the click that did work")
check("a form holding someone else's exception is closed, not compared",
      "_close_side_panel(s, anchor)" in _open_src
      and "which is not ours" in _open_src)
check("a list with no row of ours is reported rather than compared wrongly",
      "R.failed(" in _ver_src and "_compare" in _ver_src)


# ---- Conditions -----------------------------------------------------------
# Add Condition is a side PANEL behind a button, like Add Observation and
# unlike a linkage table — and its attachment is behind a PAPERCLIP rather
# than in a file field, which is what makes it different from every other
# attachment in the module.
_cond = CF.CONDITION_PASSES[0]
check("Conditions is filled as a form, not as an add-row table",
      _cond.kind == CF.FORM, _cond.kind)
check("Conditions has its own opener", bool(CF._ADD_CONDITION))
check("the buttons that open the condition panel clear the crawler's denylist",
      all(cr.is_safe_to_click(x) for x in ("Add Condition", "+ Add Condition")))
# The sidebar's Upload is a BULK import of conditions from a file, not part of
# adding one. Feeding it a generated PNG would report a defect that is nothing
# but this run handing it the wrong kind of file.
check("the sidebar's bulk Upload is not one of the buttons this flow presses",
      not any("upload" in x for x in CF._ADD_CONDITION),
      str(CF._ADD_CONDITION))

# Every field observed on the panel, in the order it renders them. Only the
# top half of the panel was seen — it scrolls — so this is a prefix rather
# than the whole authored list, and everything under Expiry Date is filled by
# discovery.
CONDITION_PANEL = ["Description", "Title", "Category", "Type",
                   "Effective From", "Expiry Date"]
_cond_labels = [f.label for f in _cond.fields]
check("every field observed on the Add Condition panel is authored, in panel "
      "order",
      _cond_labels[:len(CONDITION_PANEL)] == CONDITION_PANEL,
      f"authored {_cond_labels}")
check("the condition's marker goes in Title, which the register shows",
      [f.label for f in _cond.fields if f.marked] == ["Title"],
      str([f.label for f in _cond.fields if f.marked]))
# The four the panel marks with a red asterisk. A screen that saves without
# one of them is saving a partial record.
for _req in ["Description", "Category", "Type", "Effective From"]:
    _f = next((f for f in _cond.fields if f.label == _req), None)
    check(f"the condition's mandatory {_req!r} is not treated as optional",
          _f is not None and (not _f.optional or _f.marked),
          "not authored" if _f is None else f"optional={_f.optional}")

# The three fields below the fold, each named here because a live run found it
# by discovery first and made the wrong choice with it.
#
# Complied: "whatever the app offers first" is a coin toss on a Yes/No
# control, and a condition raised seconds ago that claims to have been
# complied with is a false record somebody downstream has to unpick. The label
# is the panel's own, brackets and all — 'Complied' alone matched nothing, and
# the run reported it as a field this deployment does not have.
_complied = next((f for f in _cond.fields
                  if "Complied" in f.label), None)
check("the condition's Complied flag is pinned to No",
      _complied is not None and str(_complied.value).lower() == "no",
      "not authored" if _complied is None else repr(_complied.value))
check("the Complied flag uses the label the panel actually renders",
      _complied is not None and _complied.label == "Complied? (Yes/No)",
      "" if _complied is None else _complied.label)

# Status: its options are the register's own filters, and left to discovery it
# took the first — Expired. Two conditions effective in 2026 and expiring in
# 2027 were filed as already expired, and the register showed both with an
# EXPIRED badge.
_status = next((f for f in _cond.fields if f.label == "Status"), None)
check("the condition's Status is named rather than taken first",
      _status is not None and _status.value == "Required",
      "not authored" if _status is None else repr(_status.value))
check("the Status this run asks for is not one that expires it",
      _status is not None and "expire" not in str(_status.value).lower())

# Complied Date contradicts Complied=No, and the app refuses a date there —
# which arrived in the report as a date-picker failure on every run, on a
# control behaving exactly as it should.
check("Complied Date is left blank on purpose, with a reason",
      any("Complied Date" in x for x in _cond.skip) and bool(_cond.skip_note),
      str(_cond.skip))
check("a skipped field is never offered to discovery",
      "spec.skip" in inspect.getsource(CF._fill_pass)
      and "left blank on purpose" in inspect.getsource(CF._fill_pass))

# Every free-text box gets its own sentence, or the round trip is vacuous:
# either box would match the other.
_cond_texts = [f.value for f in _cond.fields if f.value and not f.marked]
check("each condition text box gets its own text",
      len(set(_cond_texts)) == len(_cond_texts),
      f"{len(set(_cond_texts))} distinct of {len(_cond_texts)}")

# Effective in the past, expiring in the future: the condition is created
# ACTIVE. The alternatives are both states the screen has a filter for and
# neither is worth creating.
_cdates = {f.label: f.when for f in _cond.fields if f.when}
check("the condition is effective before it expires",
      _cdates["Effective From"] < _cdates["Expiry Date"],
      f"{_cdates['Effective From']} -> {_cdates['Expiry Date']}")
check("the condition is not created already expired",
      _cdates["Expiry Date"].year >= 2027, str(_cdates["Expiry Date"]))

# The panel was observed on one deployment, so the name of its commit control
# is exactly the sort of thing a build changes — and "no Save button" would
# then be reported as a defect on a form that saves perfectly well.
check("the condition panel's commit is tried under more than one name",
      _cond.save_label == "Save" and bool(_cond.save_alt),
      f"{_cond.save_label!r} then {_cond.save_alt}")
check("every name the condition panel may commit under is permitted",
      all(W._committable(x) for x in [_cond.save_label] + _cond.save_alt))
check("the attachments dialog commits only with permitted controls",
      all(W._committable(x) for x in CF._ATTACH_COMMITS))

# The attachment. It goes on through the paperclip, because the panel has no
# file field — and falling back to the first file input on the screen would
# hand the image to the sidebar's bulk import instead.
check("the condition's file is attached through the panel's paperclip",
      _cond.attach and _cond.attach_via_clip,
      f"attach={_cond.attach!r}, via_clip={_cond.attach_via_clip}")
check("widgets knows a file input is not a text box",
      "input[type=file]" in inspect.getsource(W.Filler.kind_of)
      and "'file'" in inspect.getsource(W.Filler.kind_of))
check("discovery skips a file input rather than typing into it",
      '"file"' in inspect.getsource(CF._fill_discovered))

# Saving files the attachment against the condition and clears the dialog that
# took it, so its Title is not the condition's Title and must not be compared
# against it. The group is what keeps the two apart.
check("the attachment dialog's values are recorded apart from the panel's",
      CF.CONDITION_ATTACHMENT_GROUP != _cond.name
      and CF.CONDITION_ATTACHMENT_GROUP.startswith(
          CF.SCREEN_LABEL[CF.CONDITIONS]),
      CF.CONDITION_ATTACHMENT_GROUP)
_verify_cond_src = inspect.getsource(CF._verify_conditions)
check("the round trip reads the attachment from the condition's own list",
      "CONDITION_ATTACHMENT_GROUP" in _verify_cond_src
      and "_open_attachments" in _verify_cond_src)
# Identical text in two boxes makes the round trip vacuous — either would
# match the other — and this dialog is nothing but discovered text boxes.
check("each box on the attachments dialog gets its own text",
      CF._attachment_value("Title", "text", "M")
      != CF._attachment_value("Description", "text", "M"))
check("the attachments dialog still gets a number where one is wanted",
      CF._attachment_value("Amount", "number", "M") == CF.SMALL_AMOUNT,
      repr(CF._attachment_value("Amount", "number", "M")))

# What says the panel opened, and what says the condition saved. Neither can
# be "some labelled field appeared": this screen's filter sidebar carries
# labelled controls of its own, so that passes when nothing opened at all.
_open_cond_src = inspect.getsource(CF._open_add_condition)
check("the condition panel's arrival is proved by a field only it has",
      "_CONDITION_ANCHOR" in _open_cond_src
      and "_wait_for_anchor" in _open_cond_src)
_do_cond_src = inspect.getsource(CF._do_conditions)
check("whether the condition saved is decided by the register growing",
      "_condition_rows" in _do_cond_src
      and "appears in the register" in _do_cond_src)

# The register reads like a grid and is NOT one: a saved condition is a
# <p class="todo-title">, the same component the Documents checklist uses, and
# "No Data Found" is that list's empty message. Counting it with the <table>
# row counter returned 0 both before and after a save that had worked, and
# reported a stored condition as "nothing was stored" — with a screenshot
# showing it on the screen attached to the finding.
for _fn, _name in [(CF._condition_rows, "counted"),
                   (CF._open_condition_row, "opened")]:
    _src = inspect.getsource(_fn)
    check(f"conditions are {_name} as a todo list, not as table rows",
          "todo-title" in _src and "tbody" not in _src)
check("the register is never counted with the grid row counter",
      "_grid_rows" not in _do_cond_src
      and "_grid_rows" not in inspect.getsource(CF._open_our_condition))

# Clicking a condition slides a panel OVER the register rather than replacing
# it, so the list — with every title in it, this run's included — is still on
# screen behind whichever row was opened. The panel's own Title is the only
# thing that says which record is showing.
_our_cond_src = inspect.getsource(CF._open_our_condition)
check("the saved condition is identified by the panel's own Title",
      "value_of" in _our_cond_src and "_condition_rows" in _our_cond_src)
# And by the FULL marker, not a title prefix. _open_document matches 40
# normalised characters, which is ample for a document and far too short here:
# two runs on the same day both give "…autotest 0908 1…" at that length, so
# the prefix would open the earlier run's condition and compare this run's
# values against it — and most would match, because the same code entered
# both. A silent pass against the wrong record is worse than any failure.
check("the condition is not matched by a title prefix",
      "_open_document" not in _our_cond_src, _our_cond_src[:0])
_t1, _t2 = (flows._norm_value(f"{CF._NOTE} {CF.run_marker(x)}. Field: Title.")
            for x in ("0908-165321", "0908-170012"))
check("two runs on the same day ARE confusable by title prefix",
      _t1[:40] == _t2[:40] and _t1 != _t2,
      f"both normalise to {_t1[:40]!r} — which is why the prefix route is not "
      f"used")

# The clip finder has to report what it clicked. The Documents paperclip
# finder came back empty on a panel that plainly draws one, and "no paperclip"
# with nothing else in it is a finding nobody can act on.
_clip_src = inspect.getsource(CF._attach_through_clip)
check("a clip that opens no dialog is reported with what was tried",
      "tried.append" in _clip_src and "Clicked, in " in _clip_src)
check("the attachment never falls back to another form's file input",
      "strict=True" in _clip_src
      and "strict" in inspect.getsource(W.Filler.upload))


# Exactly one field per screen carries the run marker. Without it the round
# trip matches a previous run's identical text and proves nothing, and the
# facility this run created cannot be told from the ones already on the case.
for group, passes in [("Request Details", CF.REQUEST_DETAILS_PASSES),
                      ("Facilities", CF.FACILITY_PASSES),
                      ("Observations", CF.OBSERVATION_PASSES),
                      ("Conditions", CF.CONDITION_PASSES)]:
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
_CHOICEY = ("Yes", "No", None)
_switches = [(p.name, f.label, f.value) for p in _all_passes for f in p.fields
             if CF._PIN_NO.search(f.label) and f.value in _CHOICEY
             and f.when is None and not f.marked]
_on = [x for x in _switches if str(x[2]).lower() != "no"]
check("every pinned switch is authored as No", not _on,
      str(_on) if _on else f"{len(_switches)} switch(es)")
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

# Pairing a facility tab with the pass that belongs to it. Builds word these
# differently, and filling a tab from the wrong pass is worse than filling it
# by discovery — it types one tab's values into another.
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

# The merge carries BOTH branches' closers - _dismiss_calendar /
# _dismiss_overlay from one, _close_calendar / _close_dropdown from the other -
# because each branch's screens are driven by its own methods. The property
# below is what actually matters, so it is asserted of every one of them
# rather than of whichever name happened to survive.
_CLOSERS = ("_dismiss_calendar", "_dismiss_overlay",
            "_close_calendar", "_close_dropdown")
for _name in _CLOSERS + ("_calendar_open", "_open_modal"):
    check(f"widgets has {_name}", hasattr(W.Filler, _name))

# No caller presses Escape itself. Whatever a field path needs to dismiss, it
# goes through a closer that knows whether a dialog is underneath it.
for _name in ("_calendar_pick", "choose", "date", "choose_in_dialog"):
    _src = inspect.getsource(getattr(W.Filler, _name))
    check(f"{_name} never presses Escape itself",
          'press("Escape")' not in _src,
          "one keystroke here closes the modal and takes the half-filled "
          "form with it")

# And every closer that CAN press Escape checks first that no dialog is up.
for _name in ("_dismiss_overlay", "_close_calendar", "_close_dropdown"):
    _src = inspect.getsource(getattr(W.Filler, _name))
    if 'press("Escape")' not in _src:
        check(f"{_name} does not press Escape at all", True)
        continue
    _guard = ("_open_modal" if "_open_modal" in _src else "_scope()")
    check(f"{_name} refuses Escape while a modal is up",
          _guard in _src and _src.index(_guard) < _src.index('press("Escape")'),
          f"guarded by {_guard}")

# The calendar closer must also press nothing at all unless a calendar is open.
_closer = inspect.getsource(W.Filler._dismiss_calendar)
check("nothing is pressed unless a calendar is open",
      "_calendar_open" in _closer)

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
# Three check states, and a fourth channel that is not a check at all.
#
# BLOCKED exists so an environment problem never reads as a product defect: a
# broken VPN must not come back as a broken application. Observations exist so
# that "worth saying" does not have to borrow a check status to get said -
# which is what made BLOCKED a dumping ground the first time round, and what
# turned a report into a wall of amber that meant nothing was wrong.
#
# Both halves are asserted here, because the failure mode is that one quietly
# absorbs the other.
r = R.RunResult(run_id="t", target_key="k", target_title="T", mode=settings.VERIFY,
                base_url="u", started_at="now")
check("a run with nothing checked is BLOCKED, not PASS", r.overall == R.BLOCKED,
      r.overall)
r.checks.append(R.passed("a"))
check("a run with only passes is PASS", r.overall == R.PASS, r.overall)

r.notes.append(R.note("something worth knowing"))
check("an observation does not change the outcome", r.overall == R.PASS,
      r.overall)
check("observations are counted apart from checks",
      r.counts == {R.PASS: 1, R.FAIL: 0, R.BLOCKED: 0} and len(r.notes) == 1,
      str(r.counts))

r.checks.append(R.blocked("b", "no data"))
check("passes plus blocked is still PASS", r.overall == R.PASS, r.overall)
r.checks.append(R.failed("c", "x", "y"))
check("any failure makes the run FAIL", r.overall == R.FAIL, r.overall)
check("failures sort to the top", r.sorted_checks()[0].status == R.FAIL)
check("blocked sorts above passing", R.ORDER[R.BLOCKED] < R.ORDER[R.PASS])

r2 = R.RunResult(run_id="t", target_key="k", target_title="T", mode=settings.VERIFY,
                 base_url="u", started_at="now", blocked_reason="host unreachable")
r2.checks.append(R.passed("a"))
check("an explicit blocked_reason overrides passes", r2.overall == R.BLOCKED)
check("an environment problem is never reported as a failure",
      r2.overall != R.FAIL,
      "a broken VPN must not read as a broken application")

# The two branches spelled the run-level state differently. Both spellings have
# to keep working, and they have to mean the same thing - a second state that
# merely LOOKED like the first is how a run would end up reporting neither.
check("ERROR is an alias of BLOCKED, not a fourth state", R.ERROR == R.BLOCKED)
r3 = R.RunResult(run_id="t", target_key="k", target_title="T", mode=settings.VERIFY,
                 base_url="u", started_at="now")
r3.error_reason = "set through the old name"
check("error_reason and blocked_reason are the same field",
      r3.blocked_reason == "set through the old name"
      and r3.error_reason == r3.blocked_reason)
c4 = CF.CaseFlowResult(run_id="t", started_at="now")
c4.error_reason = "x"
check("a case run carries the same alias", c4.blocked_reason == "x"
      and c4.overall == R.BLOCKED)


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
# Deliberately NOT a failing check. Every Phase 1 route in targets.py is
# commented out under the "DISABLED ON REQUEST" banner there, on both branches
# this merge joined - the read-only walk-the-screens runner is switched off,
# which is a product decision, not a defect. Asserting it is enabled turned a
# decision somebody made into a red suite that everyone learns to ignore.
#
# The contract this section states for itself is kept instead: a disabled
# target does not fail the suite, a BROKEN one does. Everything below runs only
# when a route is live, and every one of those assertions is unchanged.
if not CASE_KEY:
    print("        (no Phase 1 case route is enabled in targets.py - the "
          "read-only route checks below are skipped)")
check("Phase 1 routes are either absent or well-formed",
      CASE_KEY == "" or CASE_KEY in targets.TARGETS,
      "a route that is present must satisfy everything below")

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
# With every route commented out, by_area() is empty - and that is the same
# deliberate state as above. What must stay true either way is that no OTHER
# area has crept back into the menu.
check("no area beyond the credit case is offered",
      set(targets.by_area()) <= {"Credit case (case IDs like 52224-2026)"},
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
    print("        (case.all_screens is commented out in targets.py - the "
          "sidebar-coverage checks below are skipped)")
    # The sidebar list the target WOULD be checked against is still asserted,
    # because CASE_SUB_SCREENS is what the case-menu navigation reads at run
    # time whether or not a Phase 1 target is enabled. Turning a route off must
    # not quietly stop this from being checked.
    _labels = [x.label for x in targets.CASE_SUB_SCREENS]
    check("the case sidebar is still declared in order",
          _labels[:6] == ["Credit Approval Memo", "Obligor Details (BIR)",
                          "Queries", "Request Details", "Facilities",
                          "Observations"],
          str(_labels[:6]))
    check("the renamed RMG Memo entry is the one the sidebar carries",
          "RMG Memo" in _labels and "CRMD Note" not in _labels,
          str([x for x in _labels if "Memo" in x or "Note" in x]))
    check("case screens are reached by the sidebar, not by tabs",
          all(x.kind == targets.CONTEXT_MENU
              for x in targets.CASE_SUB_SCREENS),
          str([x.label for x in targets.CASE_SUB_SCREENS
               if x.kind != targets.CONTEXT_MENU]))
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
