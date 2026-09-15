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

import los_automation  # noqa: F401  (sets sys.path to the project root)

import crawler as cr
from los_automation import settings
from los_automation.runner import (case_flows as CF, checks, driver, flows,
                                   results as R, run as run_mod, targets,
                                   widgets as W)

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
                        "fsd_fields", "fsd_field_specs", "mandatory_fsd_fields",
                        "kb_status", "fsd_grounding", "fsd_ingest",
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

# The FSD is gone from this package entirely — not merely unread at run time,
# but unmentioned. It lived at the project root and the runner never loaded it;
# what was left in here was wording that attributed authored field names to a
# specification that no longer has any bearing on what this suite does.
import los_automation as _pkg  # noqa: E402
import pathlib  # noqa: E402

_pkg_root = pathlib.Path(_pkg.__file__).parent
_fsd_files = []
for _py in sorted(_pkg_root.rglob("*.py")):
    if _py.name == "test_safety.py":
        continue
    if "fsd" in _py.read_text(encoding="utf-8").lower():
        _fsd_files.append(_py.relative_to(_pkg_root).as_posix())
check("no module in this package mentions the FSD at all",
      not _fsd_files, f"still mentioned in {_fsd_files}" if _fsd_files else "")

# And "could not check" is gone as a concept, not just as a word. A third
# check status is what let a report be entirely amber and still mean nothing
# was wrong; observations replaced it, and they are never counted.
_blocked_users = []
for _py in sorted(_pkg_root.rglob("*.py")):
    if _py.name == "test_safety.py":
        continue
    _text = _py.read_text(encoding="utf-8")
    if "R.BLOCKED" in _text or "R.blocked(" in _text or "blocked_reason" in _text:
        _blocked_users.append(_py.relative_to(_pkg_root).as_posix())
check("no module records a 'could not check' result",
      not _blocked_users,
      f"still used in {_blocked_users}" if _blocked_users else "")
check("a run carries observations separately from its checks",
      "notes" in R.RunResult.__dataclass_fields__
      and "notes" in flows.FlowResult.__dataclass_fields__
      and "notes" in CF.CaseFlowResult.__dataclass_fields__)
check("an observation is not a check and cannot be counted as one",
      not hasattr(R.Note, "status"),
      "a note with a status would be the third state coming back")

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
# The fields the live form refuses to save without. The flow fills every
# enterable field on the form, but these are the ones that must stay
# non-optional: an optional field that will not set is a note, and a Basic
# Information that saves with a mandatory field missing is a partial record.
#
# Obligor Id Type is here because the app said so — "Obligor Id Type is
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
CASE_SCREEN_ORDER = ["Request Details", "Facilities", "Observations",
                     "Collaterals", "Facility Coverage", "Risk Rating",
                     "Policies & Exceptions", "Conditions", "Documents"]
check("the case screens asked for are the ones offered",
      set(CF.SCREEN_LABEL.values()) == set(CASE_SCREEN_ORDER),
      str(sorted(CF.SCREEN_LABEL.values())))
# Collaterals sits below Observations in the case sidebar and Facility
# Coverage below that, then — further down — Policies & Exceptions,
# Conditions and Documents, so that is the order a run fills them in.
# Facility Coverage has a second reason to be where it is: it associates a
# facility with a collateral, so it needs both to exist already.
check("they are filled in the order the case menu lists them",
      [CF.SCREEN_LABEL[k] for k in CF.ORDER] == CASE_SCREEN_ORDER,
      str([CF.SCREEN_LABEL[k] for k in CF.ORDER]))
# Every screen this fills has to be reachable by the label the case sidebar
# renders. Matched with '&' normalised to 'and', so the ampersand in
# "Policies & Exceptions" is what the screen shows rather than something the
# navigation depends on.
_sidebar = [s.label for s in targets.CASE_SUB_SCREENS]
_unreachable = [x for x in CASE_SCREEN_ORDER if x not in _sidebar]
check("every case screen is reached by its own sidebar label",
      not _unreachable, str(_unreachable))
# The order authored here must be the order the sidebar itself lists them in,
# or "the order the case menu lists them" above is only a claim.
_positions = [_sidebar.index(x) for x in CASE_SCREEN_ORDER if x in _sidebar]
check("that order is the sidebar's own order",
      _positions == sorted(_positions),
      str(list(zip(CASE_SCREEN_ORDER, _positions))))

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
_find_src = inspect.getsource(flows.find_case)
check("no rows read is reported as nothing searched, not as a missing case",
      "if scanned == 0:" in _find_src
      and "says nothing about whether the case exists" in _find_src,
      "'the case is not there' about a blank page sends somebody hunting a "
      "transaction that completed")
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
check("a run with nothing checked is ERROR, not PASS", r.overall == R.ERROR,
      r.overall)
r.checks.append(R.passed("a"))
check("a run with only passes is PASS", r.overall == R.PASS, r.overall)
r.notes.append(R.note("something worth knowing"))
check("an observation does not change the outcome", r.overall == R.PASS,
      r.overall)
check("observations are counted apart from checks",
      r.counts == {R.PASS: 1, R.FAIL: 0} and len(r.notes) == 1,
      str(r.counts))
r.checks.append(R.failed("c", "x", "y"))
check("any failure makes the run FAIL", r.overall == R.FAIL, r.overall)
check("failures sort to the top", r.sorted_checks()[0].status == R.FAIL)

# There is no third check status, and nothing may create one.
check("results offers no way to record a third check status",
      not hasattr(R, "blocked") and not hasattr(R, "BLOCKED"),
      "'could not check' has to be impossible, not merely unused")
check("every check a run can hold is a pass or a failure",
      all(c.status in (R.PASS, R.FAIL) for c in r.checks),
      str(sorted({c.status for c in r.checks})))
check("ERROR is a run state, never a check status",
      R.ERROR not in R.ORDER)

r2 = R.RunResult(run_id="t", target_key="k", target_title="T", mode=settings.VERIFY,
                 base_url="u", started_at="now", error_reason="host unreachable")
r2.checks.append(R.passed("a"))
check("an explicit error_reason overrides passes", r2.overall == R.ERROR)
check("an environment problem is never reported as a failure",
      r2.overall != R.FAIL,
      "a broken VPN must not read as a broken application")


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
    check("a record that is not in the grid is an ERROR, not a FAIL",
          t.steps[1].missing_is_environmental)
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
a = targets.get("case.all_screens")
check("whole-case path is menu -> specific record",
      [s.kind for s in a.steps] == [targets.MENU, targets.ROW_BY_ID],
      str([s.kind for s in a.steps]))

labels = [s.label for s in a.sub_screens]
SIDEBAR = ["Credit Approval Memo", "Obligor Details (BIR)", "Queries",
           "Request Details", "Facilities", "Observations", "Collaterals",
           "Facility Coverage", "Risk Rating", "Financials",
           "Credit Memorandum", "eCIB Details", "Policies & Exceptions",
           "Conditions", "Documents", "CRMD Note", "History",
           "Relationship with Other Banks / FIs", "Business Performance"]
check("every sidebar entry in the screenshot is covered, in order",
      labels == SIDEBAR, str([x for x in SIDEBAR if x not in labels]))
check("case screens are reached by the sidebar, not by tabs",
      all(s.kind == targets.CONTEXT_MENU for s in a.sub_screens),
      str([s.label for s in a.sub_screens if s.kind != targets.CONTEXT_MENU]))
# Obligor Details (BIR) is a sidebar entry whose content is a tab strip. It must
# not be checked as itself as well, or Basic Information is reported twice.
bir = next(s for s in a.sub_screens if s.label == "Obligor Details (BIR)")
check("Obligor Details (BIR) is a doorway to its tabs",
      not bir.check_self and len(bir.children) == len(targets.OBLIGOR_SUB_SCREENS))
check("its children are tabs",
      all(c.kind == targets.TAB for c in bir.children))
check("the planned screen list flattens parents into children",
      "Obligor Details (BIR)" not in a.screen_names()
      and "Basic Information" in a.screen_names())

# The grid screens are the reason row detail exists: a summary row shows a few
# of a record's values and its detail view holds the rest.
for nm in ["Facilities", "Collaterals", "Documents", "Conditions"]:
    s = next(x for x in a.sub_screens if x.label == nm)
    check(f"{nm} opens a row to reach the record's own data", s.open_row_detail)

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
