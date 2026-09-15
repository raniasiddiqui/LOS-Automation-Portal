"""
Phase 2b: fill the credit case's OWN screens, then verify them by round trip.

The case already exists and is sitting in My Bucket. This module opens it and
fills six of its screens:

    My Bucket -> find the case -> open it
      -> Request Details   fill the form, its Purpose of Request table, Save
      -> Facilities        select the REQUESTED FACILITY -> Proceed
                           -> fill every tab of the facility it opens, Saving each
      -> Observations      record an observation, Save
      -> Collaterals       ask for a collateral by classification and name ->
                           Proceed -> fill every tab it opens with, Saving each
      -> Facility          associate a collateral with a facility from BOTH
         Coverage          trees: '+' -> Collateral Association -> Save
      -> Policies &        raise a policy exception through Add Exception, Save
         Exceptions
      -> Conditions        raise a condition, attach a file to it through the
                           panel's paperclip, Save
      -> Documents         download what is attached, add a document, action one
      -> re-open the case FROM MY BUCKET and compare every value against
         what was typed

Each screen is its own entry point, so the portal offers a separate Check
button per route and a failure on one does not hide the others.

Why the last step matters, and why it re-enters through My Bucket rather than
just reading the screen it has just left: a value the app dropped, truncated or
re-formatted is invisible on the form that still holds it in memory. Coming back
in through the grid forces a fresh fetch from the server, so what is compared is
what was actually stored.

Two things about these screens make them different from the obligor form, and
they shape everything below:

  1. The facility is not a form you fill, it is a PRODUCT you request. Nothing
     is enterable until the requested facility has been chosen and Proceed
     pressed; the tabs that then appear depend on which product was chosen.
     So the tab strip is discovered live, never declared.

  2. Their fields are configured per deployment, so there is no field list to
     work from. The ones named below were read off the live screens and are
     authored with values chosen to satisfy each field's type and format.
     Anything else the screen turns out to be showing is then discovered by
     label and filled too, which is what "fill every field" has to mean on a
     screen whose exact field list is configuration rather than code.

Everything typed still goes through widgets.py, which refuses to write at all
off the approved host and refuses to commit anything but Save / Proceed / Add /
OK no matter what this file asks for.
"""
from __future__ import annotations

import json
import contextlib
import itertools
import json
import os
import re
import struct
import time
import zlib
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Optional  # noqa: F401 - Callable used in signatures

from playwright.sync_api import Error as PWError

import config as crawler_config
import crawler as cr

from .. import settings
from . import flows
from . import results as R
from . import widgets as W
from .driver import NavigationError, Session, new_run_id
from .targets import CONTEXT_MENU, NavStep

# The screens this module fills. The keys are what the CLI and the portal pass
# around; the values are the labels the case sidebar actually renders.
REQUEST_DETAILS = "request_details"
FACILITIES = "facilities"
OBSERVATIONS = "observations"
DOCUMENTS = "documents"
CONDITIONS = "conditions"
POLICIES = "policies"
COLLATERALS = "collaterals"
COVERAGE = "coverage"
RISK_RATING = "risk_rating"
LITIGATION = "litigation"
SHARIAH_COMMENTS = "shariah_comments"
CRMD_NOTE = "crmd_note"
CREDIT_MEMORANDUM = "credit_memorandum"
GROUP_REVIEW = "group_review"
BUSINESS_PERFORMANCE = "business_performance"
BANK_RELATIONSHIPS = "bank_relationships"
ECIB_DETAILS = "ecib_details"
PR_CHECKLIST = "pr_checklist"
HISTORY = "history"
FINANCIALS = "financials"

SCREEN_LABEL = {
    REQUEST_DETAILS: "Request Details",
    FACILITIES: "Facilities",
    OBSERVATIONS: "Observations",
    DOCUMENTS: "Documents",
    CONDITIONS: "Conditions",
    POLICIES: "Policies & Exceptions",
    COLLATERALS: "Collaterals",
    COVERAGE: "Facility Coverage",
    RISK_RATING: "Risk Rating",
    LITIGATION: "Litigation",
    SHARIAH_COMMENTS: "Shariah Comments",
    # The application renamed this screen: the case sidebar now reads 'RMG
    # Memo' (/ca-package/rmg-memo) and carries no 'CRMD Note' at all, so the
    # old label found no menu entry to click. Read off the live case menu, not
    # assumed.
    #
    # The KEY stays 'crmd_note' on purpose. It is what --fill-case takes, what
    # the portal's button and the jobs layer pass around, and what previous
    # runs' artifacts are filed under; renaming it would break all of that to
    # no benefit. Only the label the operator sees, and the menu entry the run
    # clicks, have changed. The screen's eight editors are untouched.
    CRMD_NOTE: "RMG Memo",
    CREDIT_MEMORANDUM: "Credit Memorandum",
    GROUP_REVIEW: "Group Review",
    BUSINESS_PERFORMANCE: "Business Performance",
    # The sidebar renders this with a lower-case 'banks'. The driver matches
    # menu labels case-insensitively, so the specification's capitalisation is
    # kept here and resolves to the entry either way.
    BANK_RELATIONSHIPS: "Relationship with Other Banks / FIs",
    ECIB_DETAILS: "eCIB Details",
    PR_CHECKLIST: "PR Checklist",
    HISTORY: "History",
    FINANCIALS: "Financials",
}
# The order the case menu lists them in, which is the order a run fills them:
# Observations, Collaterals, Facility Coverage, then — further down —
# Policies & Exceptions, Conditions and Documents.
#
# Facility Coverage comes after both Facilities and Collaterals on purpose and
# not only because the sidebar lists it there: it ASSOCIATES the two, so it
# has nothing to associate until a facility and a collateral exist. A run that
# fills it first finds two empty trees.
#
# Risk Rating sits where the sidebar puts it, after Facility Coverage. It is
# also the right place for it: the model it scores reads the case's financials
# and its facilities, so rating a case before those exist rates an empty one.
# Financials is ONE menu entry over several sub-tabs, the same arrangement as
# Business Performance. The four the brief asks for are reported separately so
# a failure on one cannot be read as a failure of another; the screen also
# carries GROUP FINANCIALS and GROUP FINANCIAL VARIANCE, which are not asked
# for and are not touched.
FIN_INPUT_LABEL = "Financials Input"
FIN_VARIANCE_LABEL = "Financial Variance Analysis"
FIN_PEER_LABEL = "Financial Peer Analysis"
FIN_ANALYSIS_LABEL = "Financial Analysis"

# The twenty screens of the case menu, in the order a run fills them. Two
# constraints fix this order and neither is cosmetic:
#
#   Facility Coverage ASSOCIATES a facility with a collateral, so it has
#   nothing to associate until both exist � Facilities and Collaterals come
#   first, or it finds two empty trees. Risk Rating scores a model that reads
#   the case's financials and facilities, so Financials comes before it or it
#   rates an empty case.
#
#   History goes LAST. Its second check clicks a row of Requests History,
#   which navigates to a DIFFERENT transaction's screen � measured: from case
#   52224 to case 52223's Credit Approval Memo. A screen that moves the run to
#   another case cannot sit in front of screens that expect to still be on
#   this one. _do_history puts the case back afterwards as well; the ordering
#   is the belt to that pair of braces.
ORDER = [REQUEST_DETAILS, FACILITIES, OBSERVATIONS, COLLATERALS, COVERAGE,
         FINANCIALS, RISK_RATING, CREDIT_MEMORANDUM, ECIB_DETAILS, POLICIES,
         CONDITIONS, DOCUMENTS, CRMD_NOTE, SHARIAH_COMMENTS, GROUP_REVIEW,
         BANK_RELATIONSHIPS, BUSINESS_PERFORMANCE, PR_CHECKLIST, LITIGATION,
         HISTORY]

# Screens that only LOOK. History asks two questions � does a link respond,
# does a row navigate � and answers both without typing anything, so a run
# made only of it writes nothing at all and says so.
READ_ONLY_SCREENS = {HISTORY}

# eCIB Details is ONE menu entry covering BOTH ways a record can be created on
# it: '+ Add' with a hand-typed dialog first, then '+ Upload' with a PDF of an
# SBP CIB report. The same reasoning as Business Performance below — "check
# eCIB Details" is one thing an operator wants done, not two — and the same
# resolution: the two stay separate in the RESULT. The Add flow's checks carry
# 'eCIB Details' and each upload case carries a line of its own, so a
# consolidated report can say which route stored what, and a failure in one
# can never be read as a failure of the other.
#
# Only ONE of the two writes through a Save. Upload's Proceed creates the
# record outright, and it OVERWRITES whatever is held against the same
# borrower code rather than adding a second row — so the Add flow runs first
# on purpose: reversing them would have the uploads land on a screen whose
# list the Add flow is still expected to find its own record in.
ECIB_UPLOAD_LABEL = "eCIB Details (Upload)"

# ==========================================================================
# PR Checklist
#
# A log grid — PR RISK RATING LOG — with a 'Perform PR' button that opens a
# long form of repeating sections. Each section holds a table of five
# columns: 'PR Factor' names a regulatory requirement, 'Factor Value' is the
# ONLY writable cell, and 'Regulatory Requirement', 'Actual Value' and
# 'Compliant Status' are computed by the application when Generate is pressed.
#
# NOTHING here is authored as a field list, unlike every other screen in this
# file, and that is deliberate: the sections are configuration. The brief says
# so outright — "there may be more sections above/below what's visible" — so
# the flow walks whatever factor tables are on the page and reports what it
# found. A named list would be wrong the first time a section was added, and
# would quietly under-test rather than fail.
#
# ORDER MATTERS AND IS NOT NEGOTIABLE: Generate first, THEN Save. Generate is
# what populates the three computed columns; saving first would store a record
# with them empty. The snapshot taken between the two is the expected data for
# the verification leg.
#
# 'Edit' is kept out of the happy path — see _pr_edit.
# ==========================================================================

# The columns the log grid is expected to carry, read off the live screen.
PR_LOG_COLUMNS = ["ID", "Request Type", "Date Of Rating", "Perform By",
                  "Profile Type", "Performed On", "View"]

# The columns the application computes. 'Actual Value' is NOT among them,
# despite the brief listing it there: on the live form two factors — both
# 'Linkage between borrower`s equity ...' rows — carry their only writable
# input in that column, while other rows have a disabled one in it. So the
# rule cannot be "never touch this column"; it is "fill what is enabled, leave
# what is grey", which is what the screen itself expresses and what set_factor
# follows.
PR_SYSTEM_COLUMNS = ["Regulatory Requirement", "Compliant Status"]

# Factor values to pin. Everything else discovered on the page takes the first
# real option and is REPORTED, so the choice can be reviewed and moved in here
# later. Keyed by a distinctive fragment of the factor text, matched case- and
# punctuation-insensitively, because these labels are long regulatory
# sentences — several with typographic apostrophes (borrower`s, bank's) —
# and quoting them whole is a transcription risk.
#
# The two 'Linkage ...' rows MUST be here. They are free-text boxes in the
# ACTUAL VALUE column rather than dropdowns, and set_factor refuses to invent
# a number for them: the figure feeds the application's own compliance
# arithmetic, so a made-up default would produce a Compliant Status that means
# nothing. The value below is a placeholder chosen to be obviously synthetic
# and is reported on every run — change it to whatever the checklist should
# actually be tested with.
PR_LINKAGE_VALUE = os.getenv("LOS_PR_LINKAGE_VALUE", "1")

PR_FACTOR_VALUES: dict[str, str] = {
    "linkage between borrower s equity": PR_LINKAGE_VALUE,
    # The dropdowns below are left unpinned on purpose: each offers exactly
    # ["", <compliant option>, <non-compliant option>] and the flow takes the
    # first real one, which is the compliant answer in every case —
    # 'Obtained', 'Compliant', 'Yes'. Pin one here to test the other branch.
}

# Confirmed frozen by the brief. Not a failure and not skipped silently: the
# flow asserts it really is locked, because a Factor Value that became
# writable would be a change worth reporting.
PR_FROZEN_FACTORS = [
    "banks dfis are free to determine the margin requirements on facilities",
]

# The factors the brief names. NOT a list of what to fill — the flow fills
# every factor the page offers, named here or not, because the sections are
# configuration. This list exists so the report can say whether the form
# actually showed the factors that were asked about: a screen that quietly
# stopped rendering one of them would otherwise pass, having filled everything
# it could see.
#
# Matched on a distinctive fragment, case- and punctuation-insensitively.
# These are long regulatory sentences and several carry typographic
# apostrophes (borrower`s, bank's), so quoting them whole is a transcription
# risk that would show up as a false "missing factor".
# ==========================================================================
# Financials
#
# One sidebar entry over six sub-tabs. The four the brief asks for:
#
#   FINANCIALS INPUT             'New Statement' -> an eleven-field dialog ->
#                                Proceed adds a COLUMN of ~177 editable cells
#                                to a 450-row chart of accounts -> Save
#   FINANCIAL VARIANCE ANALYSIS  'Perform Financial Analysis' -> a grid whose
#                                only writable column is VARIANCE COMMENTS
#   FINANCIAL PEER ANALYSIS      'Perform Peer Analysis' -> '+ Add Peer' ->
#                                Calculate -> comments -> Save
#   FINANCIAL ANALYSIS           several TinyMCE editors -> Save
#
# GROUP FINANCIALS and GROUP FINANCIAL VARIANCE also exist and are left alone.
#
# THREE THINGS THE DIALOG DOES THAT ARE NOT OBVIOUS, all measured:
#
#   1. 'Audited financials?' GATES two fields. Type of Auditor and Financial
#      Auditor are disabled until it is ticked, so the tick comes first or
#      they report as unsettable on a dialog that is working.
#   2. Start Date and End Date FILL THEMSELVES from the Financial Year —
#      picking 2005 makes them 'January 1st, 2005' and 'December 31st, 2005'.
#      End Date stays disabled even then. So they are ASSERTED, never typed;
#      an attempt to type one is refused by the app and rightly so.
#   3. The brief's 'Major / Minor Qualification' dropdown is NOT on this
#      dialog. It is reported as missing rather than quietly skipped.
#
# The chart of accounts is ~450 rows and is NOT authored. Which rows are
# editable is the application's decision — a saved statement comes back
# entirely read-only — so the run fills what it finds enabled and says how
# many that was.
# ==========================================================================

# Two statements, as the brief asks, with different years so the second
# cannot collide with the first. Parameterised: a year the case already holds
# cannot be added again.
FIN_YEARS = [y.strip() for y in
             os.getenv("LOS_FIN_YEARS", "2006,2007").split(",") if y.strip()]
FIN_TYPE = os.getenv("LOS_FIN_TYPE", "Actual")
FIN_TENOR = os.getenv("LOS_FIN_TENOR", "1 Year")
FIN_AUDITOR_TYPE = os.getenv("LOS_FIN_AUDITOR_TYPE", "Chartered Accountant")
FIN_AUDITOR = os.getenv("LOS_FIN_AUDITOR", "A.F. Ferguson & Co.")
FIN_UDIN = os.getenv("LOS_FIN_UDIN", "Yes")

# Print opens a 'Financial Print Model' dialog — it downloads nothing on its
# own. The dialog asks which statement to print (a list of the saved ones)
# and in which format, and only Submit produces the file.
FIN_PRINT_FORMAT = os.getenv("LOS_FIN_PRINT_FORMAT", "Excel")

# The peer to add. NAMED, not left to the picker's first option: the Customer
# lookup is a TREE, and its top level is customer TYPES — 'Other /
# Unspecified' and the like — with the actual customers nested under them.
# Taking the first thing offered picked the type, which is not a peer, and the
# grid then never grew a peer column.
FIN_PEER_CUSTOMER = os.getenv("LOS_FIN_PEER", "Rahim FI (dont change)")

# Named in the brief but absent from the live dialog. Kept so the run reports
# the discrepancy rather than silently not filling it.
FIN_EXPECTED_DIALOG_FIELDS = [
    "Actual/Projected Financials?", "Financial Period Tenor (Year / Quarter "
    "/ etc.)", "Financial Year", "Start Date", "End Date",
    "Audited financials?", "Type of Auditor", "Financial Auditor",
    "Major / Minor Qualification",
    "Qualification / Emphasis of matter (If applicable)", "UDIN Verification",
]

# How many of the chart's rows to fill. The grid is ~450 rows and every cell
# is a round trip through Angular's change detection, so filling every one of
# them takes long enough to be its own problem. A cap keeps a run to minutes
# while still covering every SECTION of the chart, because the rows are taken
# in the order the screen lists them. 0 means no cap.
FIN_MAX_CELLS = int(os.getenv("LOS_FIN_MAX_CELLS", "40"))
# Likewise for the two comment grids, which are long for the same reason.
FIN_MAX_COMMENTS = int(os.getenv("LOS_FIN_MAX_COMMENTS", "25"))

_fin_n = itertools.count()
_fin_run_digits = ""


def _fin_amount() -> str:
    """
    A distinct figure for the next cell.

    Distinct per cell AND per run, for the reason every other numeric screen
    here gives: two cells sharing a value would let a figure stored against
    the wrong row pass the round trip, and 450 rows on one screen makes that
    a real risk rather than a theoretical one. Kept inside the brief's
    0-1,000,000 range.

    The run's digits are taken on FIRST USE rather than at import, because
    _stamp is defined further down this file than these constants are.
    """
    global _fin_run_digits
    if not _fin_run_digits:
        _fin_run_digits = ("000" + re.sub(r"\D", "", _stamp()))[-3:]
    n = next(_fin_n)
    return f"{(n % 900) + 100}{_fin_run_digits}"


HISTORY_WORKFLOW_TAB = "Workflow Log"
HISTORY_REQUESTS_TAB = "Requests History"

# Read off the live grids. The brief's names differ slightly from the app's
# for the last two — it calls them "a small 'i' (info) icon", the app calls
# the columns Info and Info More — so the app's own spelling is used and the
# brief's reading is preserved in the comment.
HISTORY_WORKFLOW_COLUMNS = ["Action Taken By", "Action Description - Remarks",
                            "Initiation Date", "Action Taken On/Response Date",
                            "TAT"]
HISTORY_REQUESTS_COLUMNS = ["Transaction ID", "Package Type", "Initiated By",
                            "Initiated On", "Status", "Request Type"]

# What counts as "the screen did something" after a click. Deliberately
# generous about the FORM the response takes and strict about there being
# one: the brief asks for an explicit assertion that something observable
# happened, rather than treating a click that did not throw as a success.
#
# 'View Changes' turns out to expand the diff INLINE — no modal, no new tab,
# no URL change — so an assertion written around a modal would have failed a
# working screen. Measured on the live grid: the table went from 8 rows to
# 151 and the page text from 2,299 characters to 14,273.
HISTORY_GROWTH_ROWS = 5
HISTORY_GROWTH_CHARS = 500

PR_EXPECTED_FACTORS = [
    "linkage between borrower s equity",          # (Fund Based) and (Total)
    "bbfs",
    "all exposures shall be adequately secured",
    "at discretion",
    "the policy may be part of the bank s overall credit policy",
    "joint inspection of pledged stock",
    "financing against shares",
    "r 7 criteria 1 to 7",
]

# Business Performance is ONE menu entry covering BOTH of its sub-menus: the
# check opens each in turn, fills it and saves it. They are not separate
# entries, because "check Business Performance" is one thing an operator wants
# done, not two.
#
# They do stay separate in the RESULT, though, and that is not a contradiction.
# Every entry and every check carries the sub-menu it belongs to as its screen,
# so each keeps its own save result, its own round trip and its own line in the
# per-screen summary. A single label over both would report one save for two
# saves and could not say which of them stored what.
BP_PARENT = "Business Performance"
BP_CUSTOMER_LABEL = "Customer Business Performance"
BP_GROUP_LABEL = "Group Business Reciprocity"

# The sidebar entry a screen hides under, keyed by the label it is REPORTED
# under. Absent from this map means "the sidebar names it directly"; present
# means the round trip has to go through the parent to get back inside it.
SUB_MENU_PARENT = {
    BP_CUSTOMER_LABEL: BP_PARENT,
    BP_GROUP_LABEL: BP_PARENT,
}

FORM = "form"
LINKAGE = "linkage"

_NOTE = "Entered by automated test."


def _stamp() -> str:
    return datetime.now().strftime("%m%d-%H%M%S")


def run_marker(stamp: str = "") -> str:
    """
    A phrase unique to one run, typed into a free-text field on every screen.

    It does two jobs. It makes the round trip meaningful — a sentence that is
    the same on every run proves nothing when it comes back, because the
    previous run's copy would match just as well. And it is how the facility
    this run created is found again among the ones already on the case: the
    verification leg opens rows until it finds the one carrying this phrase,
    rather than assuming the new facility is the first in the grid.
    """
    return f"AUTOTEST-{stamp or _stamp()}"


# --------------------------------------------------------------------------
# What to type
#
# `labels` is a list because one field can read differently from one build to
# the next: what is "Request Type (Facility)" on one deployment is just
# "Request Type" on another. The first
# label that is actually on the screen wins; if none is, the field is recorded
# as not present rather than failing the pass.
#
# `value=None` means "take whatever the app offers first", which is the right
# answer for a dropdown or lookup fed by reference data that is re-seeded per
# environment. Values ARE named where the choice changes behaviour — a currency
# that makes the exchange rate meaningful, "No" on the syndication switch that
# would otherwise demand participant banks nobody asked for.
# --------------------------------------------------------------------------

@dataclass
class CField:
    labels: list[str]
    value: Optional[str] = None
    when: Optional[date] = None
    # A field that is not on this deployment's screen, or that the app fills
    # itself, is an observation rather than a failure. Only the handful the
    # screen itself refuses to save without are hard.
    optional: bool = True
    # Rendered only once something else has been answered. Its absence is the
    # form behaving correctly, so it is logged and passed over.
    conditional: bool = False
    # Gets this run's unique marker rather than a fixed sentence. Exactly one
    # field per screen is marked — see run_marker for why one is needed and why
    # more than one would be noise.
    marked: bool = False

    @property
    def label(self) -> str:
        return self.labels[0]


def _t(labels, value: str, optional: bool = True) -> CField:
    return CField(labels if isinstance(labels, list) else [labels], value=value,
                  optional=optional)


def _p(labels, value: Optional[str] = None, optional: bool = True) -> CField:
    """A dropdown, a lookup or a Yes/No switch — widgets works out which."""
    return CField(labels if isinstance(labels, list) else [labels], value=value,
                  optional=optional)


def _d(labels, when: date, optional: bool = True) -> CField:
    return CField(labels if isinstance(labels, list) else [labels], when=when,
                  optional=optional)


def _mark(labels, optional: bool = False) -> CField:
    """
    The one free-text field on a screen that carries this run's marker.

    Deliberately one per screen. The marker is what makes the round trip mean
    something — a fixed sentence coming back proves nothing, because the
    previous run's copy of it would match just as well — and it is how the
    facility this run created is told apart from the ones already on the case.
    """
    return CField(labels if isinstance(labels, list) else [labels], value="",
                  optional=optional, marked=True)


@dataclass
class Pass:
    """One fill-and-save over a screen, or over one tab or table of it."""
    name: str                       # what the checks call it
    kind: str = FORM
    aliases: list[str] = field(default_factory=list)   # tab labels this covers
    fields: list[CField] = field(default_factory=list)
    grid_heading: str = ""          # for LINKAGE: the caption above the grid
    grid: int = 0                   # fallback when the grid has no caption
    note: str = ""
    save_label: str = "Save"
    # Further names for the commit control, tried in order when `save_label`
    # is not on the form. One name is enough on the screens that were probed
    # field by field; it is not enough on a panel observed on a single
    # deployment, where the difference between Save and Add is exactly the
    # sort of thing a build changes — and "no Save button" would then be
    # reported as a defect on a form that saves perfectly well.
    save_alt: list[str] = field(default_factory=list)
    # Attach the run's test file to this pass. The value is the label of the
    # field to attach it to, or "*" for "whatever file input this form has".
    # A file input is not a control set_value can drive — filling one is a
    # different browser operation entirely — so it is named here rather than
    # written as another CField.
    attach: str = ""
    # Attach through the form's PAPERCLIP rather than through a file input of
    # its own. The Add Condition panel has no file field at all: its file box
    # lives inside the dialog the clip beside Title opens, so `upload` on the
    # panel finds nothing there — and, falling back to the first file input on
    # the screen, would put this run's image into the sidebar's bulk import.
    # See _attach_through_clip.
    attach_via_clip: bool = False
    # Fields the screen shows that discovery must NOT fill, and why.
    #
    # A field that contradicts an authored answer is worse filled than left
    # alone. Complied Date is the case that produced this: this run answers
    # Complied with No on purpose, so a complied date would be a record of
    # something that did not happen — and the app refuses it anyway, which
    # arrived in the report as "'Complied Date' would not accept 2026-01-15"
    # on every run, reading like a defect in a date picker that is behaving
    # exactly as it should.
    skip: list[str] = field(default_factory=list)
    skip_note: str = ""


# Dates. Expiries have to be in the future or the app refuses them; everything
# historical is kept plausible and inside the current review cycle.
FUTURE = date(2027, 12, 31)
REVIEW = date(2027, 6, 30)
# Mid-month on purpose. Month-end is where a date picker is most likely to
# clamp or roll over, and a date quietly changed to another one is harder to
# spot than a date refused outright.
RECENT = date(2026, 1, 15)
YEAR_END = date(2025, 12, 31)
PAST = date(2025, 6, 30)

AMOUNT = "10000000"
SMALL_AMOUNT = "1000000"


# ==========================================================================
# Request Details — its own form, its Basic Details and Purpose of Request
# ==========================================================================

REQUEST_DETAILS_PASSES: list[Pass] = [
    Pass(
        name="Request Details",
        kind=FORM,
        fields=[
            # The case's own request type. Named where the screen offers that
            # option, left to the app's first otherwise: this is set when the
            # transaction is raised and is usually already answered and locked.
            _p(["Request Type"], value="FR–Fresh Case Request"),

            # Dates. Some builds call this "Proposed Expiry" and some
            # "Proposed Expiry Date", which is why it was once authored as two
            # fields and one of them always failed. It has to be in the future;
            # the form enforces that.
            _d(["Proposed Expiry", "Proposed Expiry Date"], FUTURE,
               optional=False),
            _d(["Initiation Date", "Initiation date"], RECENT),
            _d(["Review Date"], REVIEW),
            _d(["Date of Request / BBFS / FAF", "Date of Request"], PAST),
            _d(["Decision Date"], RECENT),
            _d(["Extension Till"], FUTURE),

            # The marker goes in Purpose of Request: a rich-text box on the
            # screen itself on this build, not a row in a table.
            _mark(["Purpose of Request", "Purpose/details of request",
                   "Purpose / details of request"]),
            _t(["Justification of Request", "Justification"],
               "Raised by an automated test to exercise the Request Details "
               "screen end to end."),
            # A synthetic case has no liquid collateral, so this is answered No
            # rather than left to "first valid option", which would tick it.
            _p(["Is Finance Requested by Customer fully Secured through "
                "Liquid Collateral"], value="No"),

            _p(["Status of Account (CAM Header)", "Status of Account"],
               value="Regular"),
            _p(["Relationship Strategy"], value="Maintain"),

            _t(["Extension #", "Extension No."], "1"),
            _t(["Total Extension Days (Proposed)", "Total Extension Days"], "30"),
            _t(["Total Approved Extensions"], "1"),
            _t(["Last Approval Memo Number", "Last Approval Memo Number + Date"],
               "IDG/KHI/AUTOTEST/2026"),
            _d(["Last Approval Memo Date"], PAST),

            _p(["Cashback Customer"], value="No"),
            _t(["SBP PRs Exceptions — Mention Relevant PR",
                "SBP PRs Exceptions", "SBP PRs Exceptions - Mention Relevant PR"],
               "Relaxation re Ins. Cir. 8/2023 — recorded by an automated test."),

            # 7.2.1 Basic Details. On this build they sit on the same screen; if
            # the deployment puts them elsewhere they simply are not found, which
            # is recorded rather than failed.
            _p(["eCIB Type"], value="Borrower eCIB"),
            _t(["Borrower Code"], "AUTOECIB0001"),
            _d(["eCIB Report Date"], RECENT),
            _d(["Exposure As On"], RECENT),
            _p(["eCIB Status"]),
            _t(["Company/Individual Name", "Company / Individual Name"],
               "AUTOMATION TEST COMPANY"),
        ]),

    # Purpose of Request is a table on some builds. On this one it is a
    # rich-text box on the screen instead, filled by the pass above — so this
    # pass runs only where a table with an add-row control actually exists,
    # rather than reporting a missing add-row form on a screen that has none.
    Pass(
        name="Request Details — Purpose of Request table",
        kind=LINKAGE,
        grid_heading="PURPOSE OF REQUEST",
        fields=[
            _p(["Request Type", "Purpose Type"], value="With enhancement",
               optional=False),
            _t(["Purpose/details of request", "Purpose / details of request",
                "Details of Request"],
               "Purpose row recorded by an automated test.", optional=False),
        ],
        note="The specification models Purpose of Request as a linkage table. "
             "Where the screen renders it as a text box instead, it is filled "
             "on the screen itself and this pass has nothing to add a row to."),
]


# ==========================================================================
# Facilities — the facility detail tabs: Payment, Utilisation & Outstanding,
#              Participant Banks Details and the rest
#
# One pass per tab of the facility detail. Which tabs a facility actually has
# depends on the product that was requested, so these are matched against the
# strip the app renders rather than assumed: a pass whose tab is absent is
# skipped with that reason, and a tab that matches no pass is still opened and
# filled by discovery.
# ==========================================================================

FACILITY_PASSES: list[Pass] = [
    # Labels here are the ones the application actually renders, taken off the
    # live tab; older wordings follow as alternatives, because builds differ —
    # "Purpose of Facility" against the screen's "Facility Purpose",
    # "Request Type (Facility)" against "Facility Request Type", "Facility
    # Details" against "Facility Description" — and a run
    # authored from the specification alone filled none of them.
    Pass(
        name="Facilities — Facility Request Details",
        aliases=["Facility Request Details", "Request Details"],
        fields=[
            # Mandatory, and left to the app's own first valid option rather
            # than named. "New/Fresh" was named here and this deployment does
            # not offer it — it offers Green Light / In-Principle Approval,
            # NTB, "Additional / Fresh Credit Facitlities" (the app's spelling),
            # Fresh Facilities as Sub-limits, Annual Review of Term Loan,
            # Renewal and eight more — so the field was left empty, the form
            # answered "Facility Request Type is Required", and Save was
            # refused. Nothing on this pass reached the case because of one
            # value that does not exist here. The option list is request-type
            # reference data, re-seeded per environment, so naming any of them
            # would be the same bet made again.
            _p(["Facility Request Type", "Request Type (Facility)",
                "Request Type"], optional=False),
            _d(["Facility Start Date"], RECENT),
            _d(["Facility Expiry Date"], FUTURE, optional=False),
            _d(["Facility Maturity Date"], FUTURE),
            # The marker goes in Facility Purpose: it is the field the
            # verification leg reads to tell THIS run's facility from the
            # others already on the case. It was on "Purpose of Facility"
            # before — a label this app does not have — so no marker was
            # written at all and the round trip could not find the facility.
            _mark(["Facility Purpose", "Purpose of Facility"]),
            _t(["Facility Description", "Facility Details"],
               "Working capital facility recorded by an automated test."),
            _t(["Rational for Request", "Rationale for Request"],
               "Raised by an automated test to exercise the facility screens "
               "end to end."),
            _t(["Debit Turnover Last Year Of Loan"], SMALL_AMOUNT),
            _t(["Credit Turnover Last Year Of Loan"], SMALL_AMOUNT),
            _p(["Currency", "Facility Currency"], value="PKR"),
            _d(["Annual Review Date"], REVIEW),
            _d(["Facility Approval Date"], RECENT),
            _d(["Date of Last Approval"], PAST),
            _t(["Account Behavior", "Account Behaviour"],
               "Satisfactory. No irregularity recorded by the automated test."),
        ]),

    Pass(
        name="Facilities — Facility Details",
        aliases=["Facility Details"],
        fields=[
            # Main Facility is the product itself: a search/lookup fed by the
            # NBP product master, so the first valid entry is taken.
            _p(["Main Facility"]),
            _p(["Facility Max. Tenor", "Facility Max Tenor"]),
            _t(["Tenor Months", "Tenor (Months)"], "12"),
            _p(["Secured / Unsecured", "Secured/Unsecured"]),
            _t(["Facility Conditions"], SMALL_AMOUNT),
            # Mandatory, and a DROPDOWN on this build rather than a number of
            # months: it offers repayment structures — "Lump Sum Payment on
            # Cash Sales Realization", "As per Bank Discreation", "Principal:
            # Monthly, Profit: Monthly" and so on. Authored as the text "1" it
            # matched no option, so the field stayed empty, the form answered
            # "Grace Period is Required", and Save was refused — losing the
            # whole tab over one field. Left to the app's first valid option
            # for the same reason as Facility Request Type above.
            _p(["Grace Period"], optional=False),
            _t(["Availablity Period (Months)", "Availability Period (Months)"],
               "12"),
            _p(["Financing Country"], value="Pakistan"),
            _p(["Basel Category"]),
            _t(["Climate Smart Activities (CSA)", "Climate Smart Activities"],
               "Not applicable. Synthetic facility created by an automated test."),
            _p(["Repayment History"]),
            # 'Yes' here makes the app demand a parent facility that does not
            # exist on a case with one synthetic facility on it.
            _p(["Facility belongs to program based lending"], value="No"),
            _t(["Other Condition", "Other Conditions"],
               "No additional conditions. Automated test."),
        ]),

    Pass(
        name="Facilities — Limits and Exposures",
        aliases=["Limit and Exposure", "Limits and Exposures",
                 "Limit & Exposure", "Limits & Exposures"],
        fields=[
            _d(["Updated On"], RECENT),
            _p(["Limit Currency"], value="PKR"),
            _t(["Exchange Rate"], "1"),
            _t(["Limit In Base CCY", "Limit in Base CCY"], AMOUNT,
               optional=False),
            _t(["Proposed Limit"], AMOUNT),
            _t(["Aggregate Limit"], AMOUNT),
            _t(["Outstanding Exposure"], "5000000"),
            _t(["Exposure In Base Currency", "Exposure in Base Currency"],
               "5000000"),
            _t(["Principal Outstanding"], "4000000"),
            _t(["Mark Outstanding", "Markup Outstanding"], "1000000"),
            _t(["Risk Weighted"], "100"),
            _t(["No Cap Per Party"], "20"),
            _t(["No Cap Aggregate"], "25"),
            # Deliberately No. Turning syndication on makes Participant Banks
            # mandatory and pulls in a bank master this case has no business
            # referencing — see the note on the Participant Banks pass below.
            _p(["Is Syndicated Limit?", "Is Syndicated Limit"], value="No"),
            _p(["Forced Conversion from non-funded to funded?",
                "Forced Conversion from non funded to funded?"], value="No"),
            # Only enterable once syndication is on, which it deliberately is
            # not, so their absence is the form behaving correctly.
            CField(["Total Syndication Amount"], value=AMOUNT, conditional=True),
            CField(["Bank syndicated share (%)", "Bank Syndicated Share (%)"],
                   value="50", conditional=True),
        ]),

    Pass(
        name="Facilities — Overdue",
        aliases=["Overdues", "Overdue"],
        fields=[
            _d(["Overdue since", "Overdue Since"], RECENT),
            _d(["Position as of", "Position As Of"], RECENT),
            _t(["Total Principal Overdue Amount"], "10000"),
            _d(["Principal Overdue since", "Principal Overdue Since"], RECENT),
            _t(["Principal Overdue since (No.of Days)",
                "Principal Overdue since (No. of Days)"], "1"),
            _t(["Total Markup Overdue Amount"], "5000"),
            _d(["Markup Overdue since", "Markup Overdue Since"], RECENT),
            _t(["Overdues Amount", "Overdue Amount"], "15000"),
            _t(["Total Overdue Amount"], "15000"),
            _t(["Overdue since (No.of Days)", "Overdue since (No. of Days)"],
               "1"),
        ]),

    Pass(
        name="Facilities — Profit / Commission Structure",
        aliases=["Profit / Rental / Service Charges Structure",
                 "Profit/Commission Structure", "Profit Commission Structure",
                 "Profit / Commission Structure", "Pricing"],
        fields=[
            _p(["Benchmark Rate"]),
            _t(["Pricing (%)", "Pricing"], "5"),
            _p(["Repricing Frequency"]),
            _d(["Updated On"], RECENT),
            _p(["Repayment Frequency"]),
            _t(["Payment terms (Principal & Profit)",
                "Payment Terms (Principal & Profit)"],
               "Principal and profit payable quarterly in arrears. "
               "Automated test."),
            _t(["Profit payment terms", "Profit Payment Terms"],
               "Profit payable quarterly in arrears. Automated test."),
            _t(["Commission"], "1"),
            _t(["Commission Repayment Term", "Commission Repayment Terms"],
               "Commission recovered upfront. Automated test."),
        ]),

    Pass(
        name="Facilities — Payment",
        aliases=["Payment", "Payments", "Payment History"],
        fields=[
            _p(["Product Type"]),
            _p(["Timely"], value="Yes", optional=False),
            _t(["Early (Days)"], "2"),
            _t(["Delay (Days)"], "1"),
            _t(["No. of Delays more than 5 Days",
                "No of Delays more than 5 Days"], "1"),
            _t(["Max Delay (Days)"], "3"),
            _t(["No. of Times in PAD", "No of Times in PAD"], "1"),
            _t(["Max Days in PAD"], "2"),
            _t(["Avg. Days in PAD", "Avg Days in PAD"], "2"),
            _t(["Charity Amount (PKR)", "Charity Amount"], "1000"),
            _p(["Charity Recovered"], value="Yes"),
        ]),

    Pass(
        name="Facilities — Utilisation & Outstanding",
        aliases=["Utilisation & Outstanding", "Utilization & Outstanding",
                 "Utilisation and Outstanding", "Utilization and Outstanding"],
        fields=[
            _t(["Utilization - Min", "Utilisation - Min"], "10"),
            _t(["Utilization - Max", "Utilisation - Max"], "90"),
            _t(["Utilization - Average", "Utilisation - Average"], "50"),
            _t(["Outstanding Exposure"], "5000000"),
            _t(["Actual Outstanding"], "4500000"),
            _t(["Average Outstanding"], "4000000"),
        ]),

    # Only reachable on a syndicated facility, which this run deliberately does
    # not create. Kept authored so that if a deployment shows the table anyway
    # it gets filled, and so the report says why it was skipped when it does
    # not — silence would read as an oversight.
    Pass(
        name="Facilities — Participant Banks Details",
        kind=LINKAGE,
        aliases=["Participant Banks", "Participant Bank Details",
                 "Participant Banks Details", "Syndication"],
        grid_heading="PARTICIPANT BANKS DETAILS",
        fields=[
            _p(["Participant Bank"], optional=False),
            _t(["Participant Bank Syndicated Limit", "Syndicated Limit"],
               SMALL_AMOUNT, optional=False),
        ],
        note="Applies to syndicated facilities only. This run sets "
             "'Is Syndicated Limit?' to No, because turning it on makes the "
             "participant bank table mandatory and puts real bank names on a "
             "synthetic facility."),
]


# ==========================================================================
# Collaterals
#
# The same shape as Facilities, and handled the same way for the same reason:
# a collateral is not a form you fill, it is a RECORD YOU ASK FOR. The screen
# is a grid with an Add button, and Add opens an "Obligor Collateral" dialog
# holding two dropdowns — Collateral Classification, then Collateral Name —
# and a Proceed. Nothing is enterable until Proceed has been pressed, and
# WHICH tabs the collateral then has depends on what was asked for: a shares
# collateral carries a Shares Collateral tab, a stocks one carries Stocks
# Hypothecation. So the tab strip is discovered live, never declared.
#
# The two dialog dropdowns are answered in order and not in parallel. The
# classification is what populates the name list — picking a name before a
# classification finds an empty list — which is the same dependency the
# obligor form has between Regulatory Sector and Regulatory Industry.
#
# Only Basic Information's fields are authored below. The other three tabs
# were not observed field by field, so rather than guess at labels they are
# declared as passes with no fields and filled entirely by discovery, which
# is what the module already does for any tab the specification does not
# name. Whatever those tabs turn out to demand is then answered from their own
# validation messages — see _satisfy_unmet.
# ==========================================================================

# What the button that starts a collateral is called. "add" is matched last so
# the more specific names win first, and "add new" is included because the
# collateral detail carries an "Add New" button of its own.
_ADD_COLLATERAL = ["add collateral", "new collateral", "create collateral",
                   "add"]

# The pass that records WHAT was asked for. It belongs to no tab — the choice
# was made in a dialog before any tab existed — so the verification leg must
# not try to pair it with one. Same reasoning as REQUESTED_FACILITY.
REQUESTED_COLLATERAL = "Collaterals — collateral requested"

# The two dropdowns on the Obligor Collateral dialog, in the order they have
# to be answered.
_COLLATERAL_CLASSIFICATION = ["Collateral Classification",
                              "Collateral Classifications"]
_COLLATERAL_NAME = ["Collateral Name", "Collateral Names"]

# The field that says the collateral's own detail is open. It is mandatory on
# Basic Information and is not on the grid behind it, so it distinguishes
# "the collateral opened" from "we are still looking at the list".
_COLLATERAL_ANCHOR = "Collateral Description"


COLLATERAL_PASSES: list[Pass] = [
    Pass(
        name="Collaterals — Basic Information",
        aliases=["Basic Information", "Basic Info"],
        fields=[
            # Customers Collateral ID is deliberately NOT authored: the app
            # generates it (CC30416 on the record this was read from) and it
            # is the column the grid identifies a collateral by. Typing over
            # an app-assigned key is how a run creates a duplicate nobody can
            # find again.
            #
            # The marker goes in Collateral Description. It is mandatory, it
            # is free text, and it is what the detail screen shows as its own
            # subtitle — so it is what tells THIS run's collateral from the
            # ones already on the case.
            _mark(["Collateral Description", "Collateral Descriptions"]),

            _p(["Underlying assets", "Underlying Assets"]),

            # Named because the choice is not cosmetic: a collateral this run
            # has just invented is PROPOSED, not held or released. If this
            # deployment does not offer that word the app's own first option
            # is taken instead — see the fallback in _fill_pass.
            _p(["Collateral Status"], value="Proposed", optional=False),

            # Kept in a consistent relationship rather than picked
            # independently: forced sale below market, market below the
            # assessed value. A collateral whose forced-sale value exceeds its
            # market value is the sort of thing a cross-field rule refuses,
            # and the refusal would read as this automation being unable to
            # set a number.
            _t(["Value"], AMOUNT),
            _t(["Market Value / Tentative Market Value", "Market Value",
                "Tentative Market Value"], "9000000"),
            _t(["Forced Sale Value", "Forced Sale Value (FSV)"], "8000000"),
            # A percentage, so it has to be inside 0-100.
            _t(["Margin (%)", "Margin"], "25"),

            # Magnifier lookups fed by reference data, so the app's first
            # entry is taken. Province is answered before City and District
            # because it is what populates them.
            _p(["Province"]),
            _p(["City"]),
            _p(["District"]),
        ],
        note="Read off the Basic Information tab of a saved collateral. "
             "Anything this deployment shows beyond these is still filled, by "
             "discovery."),

    # The three tabs below were not observed field by field. Declaring them
    # with aliases and no fields is deliberate: it means each is recognised,
    # named in the report and filled from what it actually shows, rather than
    # being authored from guessed labels that would then be reported as
    # "not on this screen" on every run.
    Pass(
        name="Collaterals — Collateral Policy",
        aliases=["Collateral Policy", "Policy"],
        note="Every field on this tab was discovered from the screen: the "
             "specification gives it no field table and it was not probed "
             "field by field."),
    Pass(
        name="Collaterals — Shares Collateral",
        aliases=["Shares Collateral", "Shares"],
        note="Every field on this tab was discovered from the screen. It "
             "appears only on a collateral classified as shares, so on any "
             "other one its absence is the app behaving correctly."),
    Pass(
        name="Collaterals — Stocks Hypothecation",
        aliases=["Stocks Hypothecation", "Stock Hypothecation", "Stocks"],
        note="Every field on this tab was discovered from the screen. It "
             "appears only on a collateral that hypothecates stock, so on any "
             "other one its absence is the app behaving correctly."),
]

# CRR Log History is NOT in that list, and that is deliberate. It is a log of
# what has happened to the collateral — like the facility's Risk Rating Log
# and R1 Log History — with nothing enterable and no Save. It is still walked
# and still reported; _fill_pass recognises a tab with no enterable field and
# no commit control as read-only rather than as a screen refusing to save.


# ==========================================================================
# Facility Coverage
#
# The screen is two TREES side by side and no grid at all. The left tree lists
# the case's facilities, the right its collaterals, and each node carries a '+'
# that opens the same "Collateral Association" dialog. Associating from either
# side is the same act recorded from the other end — which collateral covers
# which facility, in what priority, for what share — so both are exercised.
#
# The dialog is a ROW EDITOR, and that shapes two decisions below:
#
#   - Its footer has '+ Add', 'Save' and 'Close'. '+ Add' appends ANOTHER
#     empty row; it does not save. So Save has no alternatives here, unlike
#     every other pass in this module: falling back to Add would add a blank
#     row and leave the dialog open, having stored nothing, and report it as
#     saved.
#   - It opens holding one empty row, which is the row this fills.
#
# There is no free-text field anywhere on it — two dropdowns and a percentage
# — so there is nowhere to type the run marker, and the round trip cannot look
# for one. The COVERAGE PERCENTAGE is used as the identifier instead: it is
# derived from the run's marker so each run picks a different one, and the
# tree renders it back ("CC30445 - Consumer Goods (0) - 25%"). See
# _coverage_percent.
# ==========================================================================

# The two sides of the screen, named for the report.
COVERAGE_FROM_FACILITY = "Facility Coverage — from the facility"
COVERAGE_FROM_COLLATERAL = "Facility Coverage — from the collateral"

# What the dialog is called, and the button that commits it. 'Add' is
# deliberately NOT a fallback — see the note above.
_COVERAGE_DIALOG = "Collateral Association"


def _coverage_percent(marker: str, offset: int = 0) -> str:
    """
    The coverage share this run claims, derived from its own marker.

    This screen has no free-text field, so the marker sentence cannot be typed
    anywhere and the round trip has nothing unique to search for — unless the
    numbers themselves are made unique. A fixed 25% would match whatever is
    already on the case and prove nothing about this run; a percentage derived
    from the marker is this run's own.

    Kept between 11 and 89 so it is always a plausible share, and never a
    round multiple of five, because those are the values a person enters by
    hand and therefore the ones most likely to be sitting there already.

    `offset` separates the two sides of the screen. They have to differ: the
    tree is the only place either can be read back from, and two associations
    sharing a percentage would be indistinguishable in it.
    """
    digits = re.sub(r"\D", "", marker or "") or "0"
    n = (int(digits[-6:]) + offset * 7) % 79 + 11
    if n % 5 == 0:
        n += 1
    return str(n)


def coverage_passes(marker: str) -> list[Pass]:
    """
    The association to enter, per side.

    Built rather than declared because the percentage depends on the run's
    marker, and the marker is not known until the run starts.
    """
    return [
        Pass(
            name=group,
            # No save_alt. '+ Add' on this dialog appends a row rather than
            # committing one, so offering it as a fallback would report a
            # blank row as a save.
            save_label="Save", save_alt=[],
            fields=[
                # The other end of the association, and its label MIRRORS the
                # side the dialog was opened from: from a facility you attach
                # a collateral, so the dropdown reads "Collaterals"; from a
                # collateral you attach a facility, so the same dropdown reads
                # "Facilities". Authored as one label for both, the collateral
                # side failed outright — "No field labelled 'Collateral' is on
                # this screen. Visible here: Facilities, Collateral Priority
                # -, Coverage %" — and saved an association with no counterpart
                # named. Reference data either way, so the app's own first
                # entry is taken: what matters is that an association exists.
                _p(other, optional=False),
                # The screen renders the label with a trailing dash,
                # "Collateral Priority -", and its placeholder is "Type".
                # Both spellings are offered because the dash is exactly the
                # sort of thing a build tidies up.
                _p(["Collateral Priority -", "Collateral Priority",
                    "Priority"], optional=False),
                # Filled per side by _do_coverage, which knows the run's
                # marker. A share, so it has to be inside 0-100.
                _t(["Coverage %", "Coverage", "Coverage Percentage"],
                   _coverage_percent(marker, offset), optional=False),
            ],
            note="Read off the Collateral Association dialog. Anything else "
                 "this deployment shows on it is still filled, by discovery.")
        for offset, (group, other) in enumerate((
            (COVERAGE_FROM_FACILITY, ["Collaterals", "Collateral"]),
            (COVERAGE_FROM_COLLATERAL, ["Facilities", "Facility"]),
        ))
    ]


# ==========================================================================
# Risk Rating
#
# The only screen in this module that leaves the case menu. "Perform Risk
# Rating" navigates to a SCORING MODEL of its own — "MODEL NAME :
# CORPORATE/COMMERCIAL" — and everything below happens there:
#
#     Risk Rating -> Perform Risk Rating -> the model page
#       -> Edit                    the page opens read-only
#       -> fill Basic Information  its LOVs are the model's inputs
#       -> Generate Score          the app computes the rating
#       -> read the result block   Final Score .. Grade Description
#       -> Calculation Sheet       a field flagged RED is the model objecting
#       -> Save                    commits the rating to the case
#       -> back to Risk Rating     Rating Summary and Rating History
#
# Two things about it shape the code below.
#
# There is NO free-text field anywhere on the model, so there is nowhere to
# type the run marker and the round trip has nothing unique to search for. The
# RATING the model produces is used as the identifier instead — the same answer
# Facility Coverage reached with its coverage percentage. That is why the
# result block is read the moment Generate Score answers and kept on the
# result: it is the only thing the Rating Summary can be checked against.
#
# And the inputs are LOVs whose options are this deployment's reference data,
# so no value is authored for any of them. Each takes the app's own first
# option, which is what `value=None` means everywhere else in this module. A
# named value would break the moment the environment is re-seeded, and the
# point of the exercise is that a rating comes out at all and comes out
# consistently — not that it comes out equal to some number written here.
# ==========================================================================

# The buttons, in the order they are pressed. Each is matched on the button's
# own text, lower-cased, and the first that is on screen wins.
_PERFORM_RATING = ["perform risk rating", "perform rating", "perform"]
_RATING_EDIT = ["edit"]
_GENERATE_SCORE = ["generate score", "generate"]

# The section of the model page whose LOVs are the inputs.
_RATING_INPUT_SECTION = ["Basic Information", "Basic Info"]

# What the model produces, in the order the page renders it. All of it is read
# and all of it is REPORTED — a result block with holes in it is worth an
# operator knowing about — but only one field of it is verified. See below.
RATING_RESULTS = [
    "Final Score", "Model Rating", "Analyst Rating", "Final Rating Grade",
    "Granular Level Rating", "Granular Override Rating", "Final Rating",
    "Risk Rating", "Grade Description",
]

# The ONE value the round trip turns on: the rating itself.
#
# Everything else the two grids show is either the same answer said again or
# not an answer at all. Grade Description is the grade in words, so it can only
# fail where the grade already has; Granular Level Rating and Granular Override
# Rating are the model's workings; Score, Model Type, Request Type, Profile
# Type, Performed By and Performed On are the app's own bookkeeping, true of
# the record whatever the model decided.
#
# Checking them cost more than it caught. Each grid writes the same grade its
# own way — the model answers '1', the Rating Summary shows '1', the Rating
# History shows '1-Excellent' — so comparing the block as text failed over
# presentation on a rating that had plainly carried across. One value, read out
# of its own column, is both the honest check and the strict one.
RATING_KEYS = ["Final Rating"]

# What res.rating_ref records this run as having left on the case. Wider than
# what is verified on purpose: it is a human's reference to the rating, not an
# assertion about it.
RATING_SHOWN = ["Final Rating", "Grade Description", "Granular Level Rating"]

# The column of each grid that holds the rating. Matched on the header text
# EXACTLY: 'Granular Level Rating' and 'Final Rating Grade' both contain the
# other headings as substrings, and picking the wrong column would compare a
# grade against a grade that is not the one the case is showing.
_SUMMARY_RATING_COL = ["Final Rating", "Final Rating Grade", "Rating"]
_HISTORY_RATING_COL = ["Rating", "Final Rating"]

# The model's two tabs.
_TAB_MODEL = ["ORR Model Summary", "Model Summary", "ORR Model"]
_TAB_CALCULATION = ["Calculation Sheet", "Calculation"]


# ==========================================================================
# Observations
#
# Every label here was read off the panel itself, and anything below the fold
# is picked up by discovery.
#
# It is NOT a linkage table. "+ Add Observation" opens a panel down the right
# of the screen rather than a modal, so add_row's '+' finds nothing and the
# dialog scope never applies: the fields are on the page. Saved observations
# then appear in the middle list as "COB<n> <Title>", which is why the run
# marker goes in Title — it is the one field the list shows, and therefore the
# one the round trip can find without opening anything.
# ==========================================================================

OBSERVATION_PASSES: list[Pass] = [
    Pass(
        name="Observations",
        kind=FORM,
        # In the order the panel renders them. Order is worth keeping even
        # where nothing depends on it: a field that turns out to gate another
        # is then already answered first, and a run that stalls says which box
        # on the panel it stalled at.
        fields=[
            _mark(["Title"]),
            _d(["Date of audit visit"], PAST),
            _p(["Region Response"]),
            _d(["Date of audit"], PAST),
            _t(["Observation", "Observation Details"],
               "Synthetic observation recorded by an automated test. No action "
               "is required and this record is not a real audit finding."),
            _p(["Complied"]),
            _t(["Comments", "Remarks"],
               "Comment recorded by an automated test against a synthetic "
               "observation."),
            # The cut-off deliberately precedes the report date: an audit
            # reported before the period it covers had closed is the kind of
            # thing a date rule on this panel would refuse, and a refusal there
            # would look like the automation could not set the field.
            _d(["Report Date", "Report date"], RECENT),
            _d(["Cutoff Date", "Cut off Date", "Cut-off Date", "Cutoff date"],
               YEAR_END),
            # Label casing is copied from the panel, not tidied up: fields are
            # anchored on label[title="..."], and a CSS attribute match is
            # case-SENSITIVE, so 'Overall Risk' would not find 'Overall risk'.
            # Both spellings are offered and the first that exists wins.
            _p(["Overall risk", "Overall Risk"]),
            _p(["Type of Audit", "Type of audit"]),
            _t(["Recommendation by BRR", "Recommendation By BRR",
                "Recommendation by BRR."],
               "No recommendation. Raised by an automated test against a "
               "synthetic observation."),
        ],
        note="The specification defines recording an observation as a process "
             "and gives it no field table, so every field here was read off "
             "the panel rather than taken from the specification."),
]

# What the button that opens the observation panel is called.
_ADD_OBSERVATION = ["add observation", "new observation", "create observation",
                    "add"]


# ==========================================================================
# Policies & Exceptions — recording a deviation from a credit policy
#
# The screen is three columns and only the right-hand one is a form:
#
#   left    Add Exception, and filters over the policy library — policy type,
#           Fresh/Existing, approver-level yes/no. Every one of those is a
#           FILTER, not a field of the record being created.
#   middle  the policy library itself, each row badged with its type and
#           whether it is fresh or existing.
#   right   the Add Exception form, which is what this fills.
#
# That shape is why the panel is confirmed by a field only IT has rather than
# by "some labelled field appeared": the filter column carries labelled
# controls of its own, so the looser test would pass on a screen where Add
# Exception did nothing at all. Same reasoning as the Conditions screen, and
# the same solution.
#
# The specification describes recording an exception as a process and gives it
# no field table, so every field below was read off the form. Anything else
# this deployment shows is still filled, by discovery.
# ==========================================================================

# What the button that opens the exception form is called. "add" is last: the
# more specific names must win before a bare "Add" elsewhere on the screen can.
_ADD_EXCEPTION = ["add exception", "new exception", "create exception",
                  "add policy exception", "add"]

# Fields that say the Add Exception form is open, tried in order. Both are
# mandatory on the form and neither is anywhere else on the screen, so either
# one appearing is proof the form arrived; two are offered because which of
# them a build renders under exactly this label is not worth depending on.
_EXCEPTION_ANCHORS = ("Compliance status", "Request Type")


POLICY_PASSES: list[Pass] = [
    Pass(
        name="Policies & Exceptions",
        save_label="Save",
        save_alt=["Add", "Submit"],
        # In the order the form renders them, which is also the order the
        # mandatory ones have to be answered in for its validation messages to
        # clear as the run goes down the form.
        fields=[
            # The marker goes in Title. It is the one field the register shows
            # back, so it is what tells THIS run's exception from the ones
            # already on the case without opening anything — and it is what
            # the round trip reads when a row is re-opened.
            _mark(["Title", "Exception Title", "Policy Title"]),

            _t(["Description", "Exception Description"],
               "Synthetic policy exception raised by an automated test. It is "
               "not a real deviation from credit policy and no approval is "
               "expected of anybody.",
               optional=False),

            # Both are reference data, re-seeded per environment, so the app's
            # first valid entry is taken rather than a name that would break
            # the next time that data changes. Nothing here depends on WHICH
            # option is chosen the way the Conditions status did — an exception
            # is a request either way, and the round trip compares whatever was
            # chosen against what came back.
            _p(["Compliance status", "Compliance Status"], optional=False),
            _p(["Request Type", "Exception Request Type"], optional=False),

            # Its own sentence, not Description's. Two boxes holding the same
            # text make the round trip vacuous — either would match the other.
            # The form counts this one down from 1000 characters, so it is kept
            # well inside that: a value the app truncates comes back different
            # and would be reported as lost when nothing was lost.
            _t(["Rationale/Reasons for deviation",
                "Rationale / Reasons for deviation",
                "Rationale for deviation", "Rationale", "Justification"],
               "Raised by an automated test to exercise the Policies & "
               "Exceptions screen end to end. There is no business reason for "
               "this deviation and no approver action is required."),
        ],
        note="The specification defines raising an exception as a process and "
             "gives it no field table, so every field here was read off the "
             "Add Exception form rather than taken from the specification."),
]


# ==========================================================================
# Documents — the case's document checklist
#
# Three separate things happen on this screen and they are three separate
# passes, because they can fail independently and one of them writes nothing:
#
#   1. Download Attachments   READ ONLY. Whatever the case already holds is
#                             pulled down and kept as evidence. A case with no
#                             attachments yet is the normal state on a fresh
#                             record, so nothing to download is reported as
#                             "could not check", never as a failure.
#   2. Additional Documents   Adds a document that is not on the standard
#                             checklist: fill its form, attach a file, Save.
#   3. Document Action        Opens one of the checklist documents, attaches a
#                             file to it, edits its fields and Saves.
#
# The screen is not a grid and not a linkage table. Documents render as a list
# of rows in the middle, and clicking one slides a "Document Action" panel in
# down the right — the same shape as Add Observation, so it is handled the same
# way: found by label, confirmed by its fields appearing, not by a `.modal.show`
# that never arrives.
#
# Documents are a checklist to be actioned rather than a field table, so every
# field below was read off the panel. Anything this deployment shows that is
# not named here is still filled, by discovery.
# ==========================================================================

# The attachment the run uploads. A real file has to exist for the browser to
# read, and the name carries the run marker on purpose: it is the one part of
# an attachment the screen displays back, so it is what the round trip looks
# for. A fixed name would match a previous run's upload just as well and prove
# nothing.
def _black_png(path: str, width: int = 320, height: int = 200) -> str:
    """
    Write a small black PNG, using nothing but the standard library.

    Something has to be uploaded to exercise the attachment fields, and it has
    to be a genuine image: the app checks the extension and the browser reads
    the bytes. Generating it beats committing a binary to the repository, and
    beats depending on Pillow for eight lines of work — a PNG is a signature,
    an IHDR, one zlib-compressed IDAT and an IEND.

    Black because the point is to be obviously a test artefact rather than to
    look like a document somebody scanned.
    """
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    # Each scanline is a filter byte followed by three zero bytes per pixel.
    scanlines = (b"\x00" + b"\x00" * (width * 3)) * height
    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height,
                                        8, 2, 0, 0, 0))   # 8-bit truecolour
           + chunk(b"IDAT", zlib.compress(scanlines, 9))
           + chunk(b"IEND", b""))
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(png)
    return path


# What the button that adds a non-checklist document is called.
_ADD_DOCUMENT = ["additional document", "additional documents", "add document",
                 "new document", "upload document", "add additional document"]

# What the button that pulls down the case's attachments is called.
_DOWNLOAD_ATTACHMENTS = ["download attachment", "download attachments",
                         "download all", "download"]

_ATTACH_NOTE = ("Attached by an automated test. This is a generated black "
                "image, not a real document.")


ADDITIONAL_DOCUMENT_PASS = Pass(
    name="Documents — Additional Documents",
    kind=FORM,
    # The panel's own paperclip. Upload File is mandatory here — this form
    # exists to put a file on the case, so there is nothing to save without one.
    attach="Upload File",
    # It commits with "Upload", not "Save". See COMMITTABLE in widgets.py.
    save_label="Upload",
    fields=[
        # Title is what the checklist renders, so it is the field a round trip
        # can find without opening anything — the same reasoning as
        # Observations.
        _mark(["Title", "Document Title", "Document Name", "Name"]),
        _t(["Comments", "Description", "Document Description"],
           "Synthetic document added by an automated test. It carries a "
           "generated black image and no real content."),
    ],
    note="Three fields — Title, Upload File and Comments — read off the "
         "'Upload Other Document' panel. Anything else this deployment shows "
         "is filled by discovery.")


DOCUMENT_ACTION_PASS = Pass(
    name="Documents — Document Action",
    kind=FORM,
    attach="Attachment",
    fields=[
        # Title is READ-ONLY here — the app fills it with the checklist
        # document's own name — so there is nothing to enter and it is not in
        # this list. Description IS editable and arrives prefilled with the
        # same text; it is authored rather than left to discovery so that
        # overwriting it is a decision on the record rather than a side effect.
        _t(["Description", "Document Description"],
           "Description replaced by an automated test while actioning this "
           "document. The document itself is unchanged."),
        _d(["Date of Action", "Date Of Action", "Action Date"], RECENT),
        # Left clear on purpose. Ticking Discrepant marks a real checklist item
        # as a problem, which is exactly the sort of thing somebody downstream
        # would then have to chase.
        CField(["Discrepant"], value="No"),
        _mark(["Justification/Comments Box", "Justification / Comments Box",
               "Justification", "Justification/Comments"]),
        _t(["Attachment Title", "Attachment Name"], "AUTOMATION TEST IMAGE"),
        _t(["Attachment Description"], _ATTACH_NOTE),
    ],
    note="Read off the Document Action panel. The document's status — the "
         "'Pending' chip at the top of the panel — carries no label of its "
         "own, so there is no way to address it that does not come down to "
         "picking a control by position. It is left alone rather than set by "
         "guesswork; everything below it is filled.")


# ==========================================================================
# Conditions — the case's condition register
#
# "+ Add Condition" opens a panel down the right of the screen: the same shape
# as Add Observation and the two Documents panels, and not a modal, so
# add_row's '+' finds nothing and the dialog scope never applies. Its fields
# are on the page.
#
# Three things about this screen shape everything below.
#
#   1. The attachment is behind a PAPERCLIP, not a file field. The panel shows
#      "Title*" followed by a clip and its attachment count, and there is no
#      "Upload File" box anywhere on it — the file box is inside the dialog
#      that clip opens. So the file goes on differently from every other
#      attachment in this module; see _attach_through_clip.
#   2. Saved conditions land in a real <table>, where Observations and
#      Documents both render lists. So the grid's row count is what says
#      whether Save stored anything, and the round trip re-opens the row
#      rather than hunting for a title among <p> elements.
#   3. The specification defines raising a condition as a process and gives it
#      no field table, so every field here was read off the panel. Only its
#      top half was observed — the panel scrolls — so anything below Expiry
#      Date is filled by discovery and reported as discovered rather than
#      specified.
#
# The sidebar's own "Upload" button is deliberately never pressed. It is a
# BULK import of conditions from a file rather than part of adding one, the
# crawler's denylist refuses to click it, and feeding a generated PNG to an
# importer that expects a workbook would report a defect that is nothing but
# this run handing it the wrong kind of file. Exercising it is a separate job.
# ==========================================================================

# What the button that opens the condition panel is called. "add" is last: the
# screen has an "Add Condition" button and the more specific names must win
# before a bare "Add" anywhere else on it can.
_ADD_CONDITION = ["add condition", "new condition", "create condition", "add"]

# The field that says the Add Condition panel is open. Every other label on
# this screen belongs to the filter sidebar down the left, which carries
# labelled controls of its own — so "some labelled field appeared" would pass
# on a screen where no panel opened at all.
_CONDITION_ANCHOR = "Effective From"

# The attachment dialog's own fields are recorded under their own group, not
# the pass's. They have to be: the dialog has a Title of its own, and the
# pass reads back everything it recorded off the PANEL — so a dialog field
# called Title would be compared against the condition's title and reported
# as lost. The round trip reads this group from the condition's attachments
# list instead.
CONDITION_ATTACHMENT_GROUP = "Conditions — attachment"

# What the attachments dialog commits with, tried in order.
_ATTACH_COMMITS = ("Add", "Upload", "Save", "OK")


CONDITION_PASSES: list[Pass] = [
    Pass(
        name="Conditions",
        kind=FORM,
        # "*" rather than a field name because there is no field on the panel
        # to name: attach_via_clip routes this through the paperclip instead.
        attach="*",
        attach_via_clip=True,
        save_label="Save",
        save_alt=["Add", "Submit"],
        # In the order the panel renders them. Order is worth keeping even
        # where nothing is known to depend on it: a field that turns out to
        # gate another is then already answered first, and a run that stalls
        # says which box on the panel it stalled at.
        fields=[
            _t(["Description", "Condition Description"],
               "Synthetic condition raised by an automated test. It is not a "
               "real approval condition and nothing is expected of anybody.",
               optional=False),

            # The marker goes in Title: it is the column the register shows,
            # so it is what tells THIS run's condition from the ones already
            # on the case without opening anything, and it is what the panel's
            # own Title field is read for when a row is re-opened.
            _mark(["Title", "Condition Title"]),

            # Category and Type are reference data, re-seeded per environment,
            # so the app's first valid entry is taken rather than a name that
            # would break the next time the data changes.
            _p(["Category", "Condition Category"], optional=False),
            _p(["Type", "Condition Type"], optional=False),

            # Effective in the recent past and expiring in the future, which
            # makes the condition ACTIVE. The alternatives are both states the
            # screen has a filter for and neither is the one worth creating: a
            # future effective date makes a condition nothing has to happen
            # about yet, and a past expiry makes one that is already expired.
            _d(["Effective From", "Effective From Date"], RECENT,
               optional=False),
            _d(["Expiry Date", "Expiry"], FUTURE),

            # Below the fold, and every one of them named here because a run
            # found it by discovery first and made the wrong choice.
            #
            # Status is mandatory and its options are the register's own
            # filters — Expired, Required / Missing, Discrepant,
            # Available/Okay. Left to "whatever the app offers first" it took
            # Expired, so two conditions effective in 2026 and expiring in
            # 2027 were filed as already expired and the register showed both
            # with an EXPIRED badge. 'Required' is matched as a substring on
            # purpose: this build writes the option "Required / Missing" and
            # the spacing around that slash is not something to depend on.
            _p(["Status", "Condition Status"], value="Required",
               optional=False),

            # Its own sentence, not Description's. Two boxes holding the same
            # text make the round trip vacuous — either would match the other.
            _t(["Comments", "Remarks"],
               "Comment recorded by an automated test against a synthetic "
               "condition. No action is required."),

            # Deliberately No. "Whatever the app offers first" is a coin toss
            # on a Yes/No control, and a condition raised seconds ago that
            # claims to have been complied with is a false record somebody
            # downstream has to unpick. The label is the panel's own, brackets
            # and all — 'Complied' alone matched nothing.
            _p(["Complied? (Yes/No)", "Complied?", "Complied"], value="No"),
        ],
        # Complied is No, so there is no date on which it was complied with.
        skip=["Complied Date", "Compliance Date"],
        skip_note="A condition this run has just raised has not been complied "
                  "with — it answers Complied with No — so a complied date "
                  "would record something that did not happen. The app "
                  "refuses one there in any case, which is why filling it by "
                  "discovery produced a date-picker failure on every run.",
        note="The specification gives conditions no field table, so every "
             "field here was read off the Add Condition panel: the six above "
             "the fold from the screen itself, and Status, Comments and "
             "Complied from what a run's own discovery reported finding below "
             "it. Anything further down is still filled by discovery."),
]


# --------------------------------------------------------------------------
# Values for fields nobody authored
#
# Discovery finds the labels; something still has to decide what to put in
# them. A numeric box will not take a sentence and a phone box validates on
# length, so the value follows what the label says the field is for. This is a
# heuristic and it is allowed to be wrong: a refused value is recorded against
# that one field and the pass carries on.
# --------------------------------------------------------------------------

# Compliance and structural switches are pinned to No. "Whatever the app offers
# first" turns a Yes/No control ON, and flagging a synthetic record as
# politically exposed — or flipping a switch that makes a whole table of fields
# mandatory — creates work for a human who did not ask for it.
_PIN_NO = re.compile(
    r"politically exposed|nab\s*/?\s*fia|\bpep\b|related party|syndicat|"
    r"sub[- ]?limit|program based|forced conversion|waiver|exception|"
    r"litigation|default|classified|restructur|resched|write[- ]?off|"
    r"life ?time expiry|cashback", re.I)

_MONEY = re.compile(
    r"amount|limit|exposure|balance|outstanding|value|worth|salary|income|"
    r"sales|turnover|price|cost|charge|fee|principal|markup|mark[- ]?up|"
    r"\bpkr\b|\bccy\b|currency amount", re.I)
_PERCENT = re.compile(r"%|percent|percentage|\brate\b|\bshare\b|ratio|margin",
                      re.I)
_COUNT = re.compile(
    r"\bno\.?\s*of\b|\bnumber of\b|\bcount\b|\bdays?\b|\bmonths?\b|\byears?\b|"
    r"tenor|period|frequency|times|grace", re.I)
_EMAILISH = re.compile(r"e-?mail", re.I)
_PHONEISH = re.compile(r"phone|mobile|cell|fax|contact (no|number)", re.I)

# Word boundaries matter here: without them "Overdue since" matches "due" and a
# date that is by definition in the past gets set to 2027.
_FUTURE_DATE = re.compile(
    r"\b(expiry|expires?|expiration|maturity|matures?|review|till|until|due|"
    r"target|next|proposed)\b", re.I)


def _auto_value(label: str, kind: str, marker: str) -> Optional[str]:
    """What to put in a field nobody wrote a value for."""
    if kind in ("dropdown", "lookup", "switch", "select"):
        return "No" if _PIN_NO.search(label) else None
    if kind == "checkbox":
        # Ticking a box that gates a whole section — syndication, sub-limits,
        # life-time expiry — turns fields mandatory or disables the date beside
        # it. Those are left clear deliberately; everything else is ticked,
        # because leaving a checkbox alone is not filling it in.
        return "No" if _PIN_NO.search(label) else "Yes"
    if kind in ("rich", "textarea"):
        return f"{_NOTE} {marker}. Section: {label[:60]}."
    if kind == "number":
        return _number_for(label)
    if kind == "text":
        if _EMAILISH.search(label):
            return flows.EMAIL
        if _PHONEISH.search(label):
            return flows.PHONE          # 14 digits: these validate on LENGTH
        num = _number_for(label, none_ok=True)
        if num is not None:
            return num
        return f"{_NOTE} {marker}."
    return None


def _number_for(label: str, none_ok: bool = False) -> Optional[str]:
    if _PERCENT.search(label):
        return "5"
    if _MONEY.search(label):
        return SMALL_AMOUNT
    if _COUNT.search(label):
        return "12"
    return None if none_ok else "1"


def _auto_date(label: str) -> date:
    return FUTURE if _FUTURE_DATE.search(label) else RECENT


# --------------------------------------------------------------------------
# Result
# --------------------------------------------------------------------------

@dataclass
class CaseFlowResult:
    run_id: str
    started_at: str
    case_id: str = ""
    screens: list[str] = field(default_factory=list)
    marker: str = ""
    facility_ref: str = ""
    # Documents. `upload_path` is the generated test image, made once per run;
    # `document_ref` is the checklist document the run opened and actioned, so
    # the round trip re-opens THAT one rather than whichever is first this
    # time; `downloaded` lists what Download Attachments actually produced.
    upload_path: str = ""
    document_ref: str = ""
    downloaded: list[str] = field(default_factory=list)
    # The title this run gave the condition it added, so the report can name
    # the record rather than describe it, and a later re-verification has
    # something to look for.
    condition_ref: str = ""
    # The same, for the policy exception it raised.
    exception_ref: str = ""
    # The coverage shares this run claimed, per side of the Facility Coverage
    # screen. That screen has no free-text field to carry the marker, so the
    # percentages are what identify its associations — see _coverage_percent.
    coverage_ref: str = ""
    # What was asked for in the Obligor Collateral dialog — the classification
    # and name — which is what the report calls the collateral it created.
    collateral_ref: str = ""
    # What the risk rating model produced, read off the model page the moment
    # Generate Score answered. This screen has no free-text field and no marker
    # to search for, so the RATING ITSELF is what the round trip looks for in
    # the Rating Summary — the same trick Facility Coverage plays with its
    # coverage percentage. Kept as a dict so the summary can be compared field
    # by field and the report can name what disagreed.
    rating_ref: str = ""
    rating_values: dict = field(default_factory=dict)
    finished_at: str = ""
    artifacts_dir: str = ""
    entries: list[dict] = field(default_factory=list)
    steps: list[dict] = field(default_factory=list)
    checks: list[R.Check] = field(default_factory=list)
    dry_run: bool = True
    # Why the run could not proceed at all — an environment problem, never a
    # finding about the application.
    blocked_reason: str = ""
    # Observations: worth reading, never a verdict. See results.Note.
    notes: list[R.Note] = field(default_factory=list)

    # Back-compat alias: the qa-automation branch called this `error_reason`.
    @property
    def error_reason(self) -> str:
        return self.blocked_reason

    @error_reason.setter
    def error_reason(self, value: str) -> None:
        self.blocked_reason = value

    @property
    def counts(self) -> dict:
        out = {R.PASS: 0, R.FAIL: 0, R.BLOCKED: 0}
        for c in self.checks:
            out[c.status] = out.get(c.status, 0) + 1
        return out

    @property
    def overall(self) -> str:
        if self.blocked_reason:
            return R.BLOCKED
        c = self.counts
        if c[R.FAIL]:
            return R.FAIL
        return R.PASS if c[R.PASS] else R.BLOCKED

    @property
    def headline(self) -> str:
        c = self.counts
        out = f"{c[R.PASS]} passed / {c[R.FAIL]} failed / {c[R.BLOCKED]} blocked"
        if self.notes:
            out += f" / {len(self.notes)} observation(s)"
        return out

    @property
    def title(self) -> str:
        names = ", ".join(SCREEN_LABEL.get(s, s) for s in self.screens)
        return (f"Fill and verify {names}"
                + (" (dry run)" if self.dry_run else ""))

    def marker_text(self, spec: "CField") -> str:
        """
        The value for a marked field, or "" to leave the field's own value
        alone. Naming the field inside the sentence matters as much as the
        marker does: several screens have more than one free-text box, and text
        that does not say which box it went into makes the round trip vacuous —
        any box would match any other.
        """
        if not spec.marked:
            return ""
        return f"{_NOTE} {self.marker}. Field: {spec.label}."


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-")[:40]


def _worst(checks: list[R.Check]) -> str:
    """The status a screen should show as: any failure dominates."""
    return R.FAIL if any(c.status == R.FAIL for c in checks) else R.PASS


# --------------------------------------------------------------------------
# The entry point
# --------------------------------------------------------------------------

def fill_case_screens(screens: Optional[list[str]] = None,
                      case_id: str = "",
                      headless: Optional[bool] = None,
                      dry_run: bool = True,
                      verify: bool = True,
                      progress: Optional[Callable] = None,
                      run_id: str = "", emit=None) -> CaseFlowResult:
    """
    Open a case from My Bucket and fill the screens asked for, then verify.

    dry_run=True fills every field and reports what each form then holds WITHOUT
    saving, which is how a change here is checked without leaving data on a
    case. dry_run=False saves, and the round trip afterwards is the point of the
    exercise.
    """
    reason = W.assert_writable()          # before a browser is even launched
    screens = [s for s in (screens or ORDER) if s in SCREEN_LABEL] or list(ORDER)
    screens.sort(key=ORDER.index)
    case_id = (case_id or settings.CASE_ID).strip()
    stamp = _stamp()
    run_id = run_id or new_run_id("case" if not dry_run else "casedry")
    _emit = emit or (lambda e: None)

    res = CaseFlowResult(
        run_id=run_id, started_at=_now(), dry_run=dry_run, case_id=case_id,
        screens=screens, marker=run_marker(stamp),
        artifacts_dir=os.path.join(settings.ARTIFACTS_DIR, run_id))

    def say(msg: str) -> None:
        if progress:
            progress(msg)
        _emit({"kind": "log", "text": msg})

    def step(text: str, status: str = R.PASS, note: str = "") -> None:
        res.steps.append({"index": len(res.steps) + 1, "text": text,
                          "status": status, "note": note})
        _emit({"kind": "step_done", "index": len(res.steps), "text": text,
               "status": status, "note": note})

    say(f"Data entry gate: {reason}")
    # Not every screen writes. Saying "data will be written" for a run that
    # only contains History would be false, and a warning that is sometimes
    # false is a warning people stop reading.
    writes = [k for k in screens if k not in READ_ONLY_SCREENS]
    if dry_run:
        say("DRY RUN — nothing will be saved")
    elif writes:
        say(f"LIVE — data will be written to case {case_id}")
    else:
        say(f"READ-ONLY — {', '.join(SCREEN_LABEL[k] for k in screens)} "
            f"writes nothing to case {case_id}")
    say(f"Case: {case_id}")
    say(f"Screens: {', '.join(SCREEN_LABEL[s] for s in screens)}")
    say(f"Run marker: {res.marker}")

    # The file the Documents and Conditions screens upload, made once and
    # named after the run so the round trip can tell this run's attachment
    # from any other. Written even on a dry run: a dry run still attaches the
    # file, it just never presses Save.
    if DOCUMENTS in screens or CONDITIONS in screens:
        res.upload_path = _black_png(
            os.path.join(res.artifacts_dir, f"{res.marker}.png"))
        say(f"Test attachment: {os.path.basename(res.upload_path)}")

    if not case_id:
        res.error_reason = ("No case id was given, so there is no case to "
                            "open. Set one in the sidebar or pass --case-id.")
        return _finish(res, say)

    try:
        with Session(run_id, mode=settings.TRANSACT, headless=headless,
                     emit=_emit) as s:
            s.login()
            step("sign in")

            if not _open_case(s, res, say, step, "01"):
                return _finish(res, say)

            for key in screens:
                label = SCREEN_LABEL[key]
                say(f"{label} …")
                _emit({"kind": "screen_start", "index": screens.index(key) + 1,
                       "total": len(screens), "screen": label})
                before = len(res.checks)
                # An error on one screen must not take the other two with it:
                # they are independent, and a run that abandons Observations
                # because a facility tab misbehaved reports nothing about a
                # screen that may have been perfectly fine.
                try:
                    if key == REQUEST_DETAILS:
                        _do_request_details(s, res, dry_run, say, step)
                    elif key == FACILITIES:
                        _do_facilities(s, res, dry_run, say, step)
                    elif key == DOCUMENTS:
                        _do_documents(s, res, dry_run, say, step)
                    elif key == CONDITIONS:
                        _do_conditions(s, res, dry_run, say, step)
                    elif key == POLICIES:
                        _do_policies(s, res, dry_run, say, step)
                    elif key == COVERAGE:
                        _do_coverage(s, res, dry_run, say, step)
                    elif key == RISK_RATING:
                        _do_risk_rating(s, res, dry_run, say, step)
                    elif key == COLLATERALS:
                        _do_collaterals(s, res, dry_run, say, step)
                    elif key == LITIGATION:
                        _do_litigation(s, res, dry_run, say, step)
                    elif key == SHARIAH_COMMENTS:
                        _do_shariah_comments(s, res, dry_run, say, step)
                    elif key == CRMD_NOTE:
                        _do_crmd_note(s, res, dry_run, say, step)
                    elif key == CREDIT_MEMORANDUM:
                        _do_credit_memorandum(s, res, dry_run, say, step)
                    elif key == GROUP_REVIEW:
                        _do_group_review(s, res, dry_run, say, step)
                    elif key == BUSINESS_PERFORMANCE:
                        _do_business_performance(s, res, dry_run, say, step)
                    elif key == BANK_RELATIONSHIPS:
                        _do_bank_relationships(s, res, dry_run, say, step)
                    elif key == ECIB_DETAILS:
                        _do_ecib_details(s, res, dry_run, say, step)
                    elif key == PR_CHECKLIST:
                        _do_pr_checklist(s, res, dry_run, say, step)
                    elif key == FINANCIALS:
                        _do_financials(s, res, dry_run, say, step)
                    elif key == HISTORY:
                        _do_history(s, res, dry_run, say, step)
                    else:
                        _do_observations(s, res, dry_run, say, step)
                except NavigationError as e:
                    # A screen the case menu will not open is a finding unless
                    # it was the environment that stopped us getting there.
                    if e.environmental:
                        res.notes.append(R.observation(
                            f"{label} could not be reached", str(e),
                            evidence=[s.screenshot(
                                f"{_safe(label)}-unreachable")]))
                        step(f"open {label}", R.ERROR, str(e)[:150])
                    else:
                        res.checks.append(R.failed(
                            f"{label} can be opened",
                            expected=f"the case menu offers {label!r}",
                            actual=str(e),
                            evidence=[s.screenshot(
                                f"{_safe(label)}-unreachable")],
                            screen=label))
                        step(f"open {label}", R.FAIL, str(e)[:150])
                except Exception as e:      # noqa: BLE001
                    res.checks.append(R.failed(
                        f"{label} could be filled",
                        expected="the screen fills and saves without the run "
                                 "falling over",
                        actual=str(e)[:300],
                        evidence=[s.screenshot(f"{_safe(label)}-error")],
                        screen=label))
                    step(f"fill {label}", R.FAIL, str(e)[:150])
                mine = res.checks[before:]
                _emit({"kind": "screen_done",
                       "index": screens.index(key) + 1, "total": len(screens),
                       "screen": label, "status": _worst(mine),
                       "note": f"{len(mine)} check(s)"})

            say(f"{len(res.entries)} field(s) entered in total")

            # ---- the round trip ------------------------------------------
            #
            # Each of these is a reason the comparison did not happen, not a
            # verdict on the application, so none of them records a check.
            if dry_run:
                res.notes.append(R.note(
                    "Dry run: nothing was saved, so the entered values were "
                    "not read back. Re-run without the dry-run option to "
                    "verify them."))
                return _finish(res, say)
            if not verify:
                res.notes.append(R.note(
                    "Verification was switched off for this run, so nothing "
                    "was read back."))
                return _finish(res, say)
            if not res.entries:
                res.notes.append(R.note(
                    "Nothing was entered, so there was nothing to read back."))
                return _finish(res, say)

            say("Re-opening the case from My Bucket to verify …")
            res.checks.extend(verify_case_entries(s, res, say, step))
            return _finish(res, say)

    except W.WriteRefused as e:
        res.error_reason = str(e)
    except NavigationError as e:
        res.error_reason = str(e)
    except Exception as e:  # noqa: BLE001 - environment failure, not a defect
        res.error_reason = str(e)[:400]

    return _finish(res, say)


def verify_case_screens(case_id: str = "", entries: Optional[list[dict]] = None,
                        marker: str = "", facility_ref: str = "",
                        document_ref: str = "", upload_path: str = "",
                        condition_ref: str = "", exception_ref: str = "",
                        collateral_ref: str = "",
                        headless: Optional[bool] = None,
                        progress: Optional[Callable] = None,
                        run_id: str = "", emit=None) -> CaseFlowResult:
    """
    Re-run ONLY the round trip, against values a previous run recorded.

    Read-only. It exists so the comparison can be corrected and re-checked
    without filling three screens again — a full fill takes minutes and leaves
    data on a real case. `entries` is what a previous run wrote to flow.json.
    """
    case_id = (case_id or settings.CASE_ID).strip()
    run_id = run_id or new_run_id("case-verify")
    _emit = emit or (lambda e: None)
    res = CaseFlowResult(
        run_id=run_id, started_at=_now(), dry_run=True, case_id=case_id,
        screens=list(ORDER), marker=marker, facility_ref=facility_ref,
        # Which document was actioned and which file was attached. Without
        # them the Documents round trip has no record to re-open and no
        # filename to look for, so it would report everything as unverifiable.
        document_ref=document_ref, upload_path=upload_path,
        # Which condition and which exception were raised. The round trip finds
        # each by the marker in its Title, so these are what the report names
        # rather than what it searches by — but without them a re-verification
        # can only describe the records it opened.
        condition_ref=condition_ref, exception_ref=exception_ref,
        collateral_ref=collateral_ref,
        entries=list(entries or []),
        artifacts_dir=os.path.join(settings.ARTIFACTS_DIR, run_id))

    def say(msg: str) -> None:
        if progress:
            progress(msg)
        _emit({"kind": "log", "text": msg})

    def step(text: str, status: str = R.PASS, note: str = "") -> None:
        res.steps.append({"index": len(res.steps) + 1, "text": text,
                          "status": status, "note": note})
        _emit({"kind": "step_done", "index": len(res.steps), "text": text,
               "status": status, "note": note})

    say(f"Verifying case {case_id} against {len(res.entries)} recorded value(s)")
    say("This leg is read-only — nothing is written.")
    if not res.entries:
        res.error_reason = ("No recorded values were supplied to compare "
                            "against, so there was nothing to verify.")
        return _finish(res, say)

    try:
        with Session(run_id, mode=settings.VERIFY, headless=headless,
                     emit=_emit) as s:
            s.login()
            step("sign in")
            res.checks.extend(verify_case_entries(s, res, say, step,
                                                  reopen=True))
            return _finish(res, say)
    except NavigationError as e:
        res.error_reason = str(e)
    except Exception as e:  # noqa: BLE001
        res.error_reason = str(e)[:400]
    return _finish(res, say)


# --------------------------------------------------------------------------
# Getting to the case
# --------------------------------------------------------------------------

def _open_case(s: Session, res: CaseFlowResult, say, step,
               shot_prefix: str, attempts: int = 2) -> bool:
    """
    Find the case in My Bucket, open it, and wait for its sidebar.

    RETRIED, and nothing is recorded until the last attempt fails. Opening
    the case a second time - which is what the round trip does - is markedly
    less reliable than the first: on a case carrying several 450-row
    financial statements the sidebar intermittently never arrives, and a
    single miss discarded an entire run's verification with "the case could
    not be re-opened". A second go costs seconds and usually succeeds.

    A case that is genuinely not in the grid is the run having nothing to work
    on, not the application misbehaving, so it ends the run BLOCKED and
    asserts nothing about the app.
    """
    last = ""
    for attempt in range(1, max(1, attempts) + 1):
        final = attempt >= attempts
        try:
            row = flows.find_case(s, res.case_id, say)
        except NavigationError as e:
            last = str(e)
            if not final:
                # Pause before going again. The failure this retry exists for
                # is 'Looked at 0 row(s) across 12 page(s)' - My Bucket's grid
                # had not rendered yet, and retrying instantly just reads the
                # same empty grid a second time.
                say(f"    the case would not open ({str(e)[:70]}) - "
                    f"reloading My Bucket, then retrying")
                _reload_bucket(s)
                continue
            res.blocked_reason = last
            res.checks.append(R.blocked(
                "The case can be opened from My Bucket", last,
                evidence=[s.screenshot(f"{shot_prefix}-case-not-found")]))
            step("open the case from My Bucket", R.BLOCKED, last[:150])
            return False

        if flows.wait_for_case_menu(s, say):
            res.checks.append(R.passed(
                "The case can be opened from My Bucket",
                detail=f"Found and opened: {row[:120]}"
                       + ("" if attempt == 1 else
                          f" (on attempt {attempt} - the sidebar did not "
                          f"arrive the first time)")))
            step("open the case from My Bucket", note=row[:120])
            s.screenshot(f"{shot_prefix}-case-open")
            return True

        last = ("The case opened but its sidebar never populated, so none of "
                "its screens could be reached.")
        if not final:
            say("    the case opened but its sidebar did not - reloading My "
                "Bucket, then retrying")
            _reload_bucket(s)
            continue
        res.blocked_reason = last
        res.checks.append(R.blocked(
            "The case menu loads", last,
            evidence=[s.screenshot(f"{shot_prefix}-no-case-menu")]))
        step("wait for the case menu", R.BLOCKED, last[:150])
    return False


def _open_screen(s: Session, label: str, step) -> str:
    """Click one entry of the case's own sidebar."""
    note = s._step_context_menu(NavStep(kind=CONTEXT_MENU, label=label))
    step(f"open {label}", note=note)
    return note


# --------------------------------------------------------------------------
# Filling one pass
# --------------------------------------------------------------------------

def _norm(text: str) -> str:
    s = (text or "").strip().lower().replace("&", " and ")
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", s)).strip()


def _set_one(f: W.Filler, spec: CField, override: str = "",
             first_option: bool = False) -> W.Entry:
    """
    Set one authored field, trying each of its candidate labels in turn.

    One field can read differently from one build to the next and neither
    wording is wrong — 'Request Type (Facility)' on one is 'Request Type' on
    another. Trying the alternatives is what stops a naming difference being
    reported as a missing field.

    `first_option` drops the authored value and takes whatever the app offers
    first, which is what widgets does for value=None. It is the retry for a
    named value this deployment does not have in its option list.
    """
    missing: Optional[Exception] = None
    for label in spec.labels:
        try:
            value = None if first_option else (override or spec.value)
            return f.set_value(label, value, when=spec.when)
        except W.FillError as exc:
            if "No field labelled" in str(exc):
                missing = exc
                continue
            raise
    raise missing or W.FillError(
        f"No field labelled {' / '.join(spec.labels)!r} is on this screen.")


def _fill_pass(s: Session, res: CaseFlowResult, screen: str, spec: Pass,
               dry_run: bool, say, step, discover: bool = True,
               scope: str = "",
               leave_alone: Optional[list[str]] = None) -> list[W.Entry]:
    """
    Fill and save ONE pass: a screen, a tab of a facility, or one add-row table.

    Authored fields first, in the order they are declared, because order matters
    on these screens as much as it does on the obligor form — a currency before
    the rate that depends on it, a switch before the field it enables. Then
    whatever else the screen turns out to be showing.

    A field that will not set does NOT abandon the pass. Each pass saves
    independently, so stopping at the first awkward dropdown would leave every
    later tab unfilled — a far worse outcome than one recorded blocker.
    """
    f = W.Filler(session=s, screen=screen, group=spec.name,
                 scope_selector=scope)
    shot = _safe(spec.name)

    if spec.kind == LINKAGE:
        where = (f"the '+' under {spec.grid_heading!r}" if spec.grid_heading
                 else "the table's '+'")
        if not _open_add_row(f, spec):
            res.checks.append(R.failed(
                f"{spec.name} offers an add-row form",
                expected=f"{where} opens a form to enter a row",
                actual="no dialog opened",
                detail="Without the dialog there is nowhere to enter a row. "
                       "Either this table is not on the screen any more, or its "
                       "section is collapsed so the '+' is not rendered."
                       + (f" {spec.note}" if spec.note else ""),
                evidence=[s.screenshot(f"{shot}-noadd")], screen=screen))
            step(f"add a row on {spec.name}", R.FAIL, "no dialog opened")
            return []
        step(f"open the add form on {spec.name}")

    failed_required = 0
    missing: list[str] = []
    # Seeded with the fields this pass has decided not to fill, so discovery
    # never reaches them. Reported below rather than passed over in silence:
    # a deliberate omission with a reason is a different thing from a field
    # that quietly stopped being tested.
    # Fields DISCOVERY must not touch: the pass's own declared `skip` list,
    # plus anything the caller asks to be left alone. `leave_alone` is the
    # per-call form of the same rule — eCIB's record page carries three values
    # the CREATE dialog already set, and discovery finding them writable typed
    # a date of its own over the key data the record was created with.
    done: set[str] = {_norm(x) for x in list(spec.skip) + list(leave_alone or [])}

    for spec_field in spec.fields:
        override = res.marker_text(spec_field)
        try:
            e = _set_one(f, spec_field, override)
            say(f"    {e.label} = {e.value[:60]!r}")
        except (W.FillError, Exception) as exc:  # noqa: BLE001
            # A named value this deployment does not offer is not a field that
            # cannot be set — it is a field whose option list differs here.
            # Falling back to the app's own first valid option is what keeps
            # one wrong name from costing the whole pass: 'Facility Request
            # Type' and 'Grace Period' were each named a value that does not
            # exist on this build, each stayed empty, and each refused the
            # save for the entire tab it was on. Discovery cannot rescue them
            # — an authored field is never offered to it — so the retry has to
            # happen here.
            #
            # The marked field is exempt: its value carries the run marker and
            # is the only thing that tells this run's record from another's.
            if "no option matching" in str(exc) and not spec_field.marked:
                try:
                    e = _set_one(f, spec_field, override, first_option=True)
                    say(f"    {e.label} = {e.value[:60]!r} (the app's first "
                        f"option; {spec_field.value!r} is not offered here)")
                    res.notes.append(R.observation(
                        f"{spec.name}: {spec_field.label} took the value it "
                        f"was given",
                        f"{spec_field.value!r} is not one of this "
                        f"deployment's options, so the app's first valid "
                        f"option ({e.value!r}) was taken instead. The field "
                        f"is answered and the save is not held up, but the "
                        f"value came from the application rather than from "
                        f"this run.", screen=screen))
                    continue
                except (W.FillError, Exception):  # noqa: BLE001
                    pass          # fall through to the ordinary reporting

            # A field the app has not rendered yet, or that this deployment
            # simply does not have, is the form behaving correctly. That holds
            # however the field was authored here: these labels are this
            # suite's, the screen's are configuration, and a field that has
            # been renamed reads exactly like one that was never there. It is
            # collected and reported afterwards as an observation.
            if flows._not_on_screen(exc):
                missing.append(spec_field.label)
                say(f"    - {spec_field.label}: not on this screen")
                continue
            if not spec_field.optional:
                failed_required += 1
            res.checks.append(R.failed(
                f"{spec.name}: {spec_field.label} can be set",
                expected=f"{spec_field.label} accepts a value",
                actual=str(exc)[:300],
                detail="The field is on the screen and would not take a value."
                       + ("" if spec_field.optional else
                          " It is marked mandatory, so the screen may refuse "
                          "to save without it."),
                screen=screen))
            say(f"    !! {spec_field.label}: {str(exc)[:110]}")
        finally:
            # Set or not, an authored field is never offered to discovery
            # again — under any of its alternative names.
            for label in spec_field.labels:
                done.add(_norm(label))

    # ---- the file, if this pass carries one ----------------------------
    #
    # After the typed fields and before discovery. After, because a form that
    # renders its attachment fields only once something else is answered needs
    # those answers first; before, because discovery must not then try to type
    # a sentence into the file input it can see.
    if spec.attach and res.upload_path:
        target = "" if spec.attach == "*" else spec.attach
        try:
            if spec.attach_via_clip:
                e = _attach_through_clip(s, res, dry_run, say)
            else:
                e = f.upload(res.upload_path, target)
            say(f"    {e.label} = {e.value!r}")
            done.add(_norm(e.label))
            where = ("the dialog behind the form's paperclip"
                     if spec.attach_via_clip
                     else target or "the form’s file input")
            res.checks.append(R.passed(
                f"{spec.name}: a file can be attached",
                detail=f"Attached {e.value} to {where}.", screen=screen))
        except (W.FillError, Exception) as exc:      # noqa: BLE001
            res.checks.append(R.failed(
                f"{spec.name}: a file can be attached",
                expected=("a way to attach a file to " + spec.name
                          if spec.attach_via_clip
                          else f"a file input on {spec.name}"),
                actual=str(exc)[:250],
                detail="Nothing was uploaded, so the attachment checks below "
                       "say nothing about this pass.",
                evidence=[s.screenshot(f"{shot}-noattach")], screen=screen))
            say(f"    !! attachment: {str(exc)[:110]}")

    authored = len(f.entries)

    # A field that was authored and then not found has to be REPORTED, not
    # merely logged. Eleven of the twelve fields on the Add Observation panel
    # were once skipped as "not on this screen", nothing was recorded, and the
    # run announced 7 passed / 0 failed on a form where one box had been
    # filled. Silence reading as success is the one thing this suite is built
    # not to do — so it is written down, as the observation it is rather than
    # as a verdict on an application that may simply not have these fields.
    if missing:
        res.notes.append(R.observation(
            f"{spec.name}: {len(missing)} authored field(s) not on the screen",
            ", ".join(missing[:12])
            + ". They were not filled. Either this deployment does not have "
              "them, or their labels read differently here — the run reports "
              "what it could not find rather than passing over it, and the "
              "form's own validation decides whether any of them mattered.",
            evidence=[s.screenshot(f"{shot}-missing")], screen=screen))

    if spec.skip:
        res.notes.append(R.observation(
            f"{spec.name}: {', '.join(spec.skip[:4])} left blank on purpose",
            spec.skip_note or "This pass deliberately does not fill these.",
            screen=screen))
        say(f"    - {', '.join(spec.skip)}: left blank on purpose")

    # ---- everything else the screen is showing ------------------------
    if discover:
        extra, refused = _fill_discovered(f, res, done, say)
        if extra:
            say(f"    {extra} further field(s) found on the screen and filled")
        if refused:
            res.checks.append(R.failed(
                f"{spec.name}: every field the screen shows accepts a value",
                expected="every field discovered on the screen takes a value",
                actual=f"{len(refused)} did not: " + " | ".join(refused[:6]),
                detail="These are fields beyond the ones authored for this "
                       "pass, found by walking the screen.",
                screen=screen))

    say(f"    {len(f.entries)} field(s) set ({authored} specified, "
        f"{len(f.entries) - authored} discovered)"
        + (f", {len(missing)} not on the screen" if missing else ""))
    s.screenshot(f"{shot}-filled")

    # ---- answer whatever the form still says it needs -------------------
    #
    # The last line of defence for "fill every field", and the one that does
    # not depend on anybody having authored the right label. Everything above
    # works from a list — the authored fields, then the labels discovery can
    # see — and a mandatory field that is on neither list stops the save for
    # the whole tab. The form itself knows which ones those are and says so in
    # its own validation text, so that text is read and acted on before Save
    # is pressed rather than quoted afterwards as the reason it failed.
    answered = _satisfy_unmet(f, res, say)
    if answered:
        res.notes.append(R.observation(
            f"{spec.name}: {len(answered)} field(s) the form demanded were "
            f"answered from its own validation messages",
            "These were not authored and discovery had not filled them; the "
            "form named them as required and they were then set so the save "
            "could proceed: " + " | ".join(answered[:8])
            + ". Worth authoring properly if they matter — the values here "
              "came from the application's own first option or from a "
              "generated one.", screen=screen))

    # ---- validate BEFORE saving ---------------------------------------
    #
    # A pass whose fields all accepted their values can still refuse to save:
    # several boxes here validate on length or on a range, and Save then does
    # nothing at all — no toast, no navigation. Reading the form's own messages
    # first is what turns "Save did nothing" into a readable result.
    outstanding = f.unsatisfied()
    if outstanding:
        res.checks.append(R.failed(
            f"{spec.name} has no unmet field rules before saving",
            expected="no validation message left on the form",
            actual=f"{len(outstanding)} outstanding: "
                   + " | ".join(outstanding[:5]),
            detail="The form will refuse to save while these stand, so the "
                   "values below were entered but not stored.",
            evidence=[s.screenshot(f"{shot}-invalid")], screen=screen))
        say(f"    !! {len(outstanding)} unmet rule(s): "
            f"{' | '.join(outstanding[:3])}")
    else:
        res.checks.append(R.passed(
            f"{spec.name} has no unmet field rules before saving",
            detail=f"{len(f.entries)} field(s) filled, the form reports no "
                   f"outstanding validation.", screen=screen))

    # ---- read every value back BEFORE saving --------------------------
    #
    # This is what makes a lost value attributable. Recommendation by BRR was
    # typed, accepted, saved without complaint and came back empty — and with
    # only the round trip to go on, "the automation never typed it" and "the
    # app dropped it on save" look identical. Reading the form back while it is
    # still on screen separates the two, so the report can say which.
    held = _values_held(f)
    if held:
        res.checks.append(R.failed(
            f"{spec.name} holds every value that was typed into it",
            expected="each field still shows what was entered, before Save",
            actual=f"{len(held)} did not: " + " | ".join(held[:6]),
            detail="The value did not stay in the box, so anything the round "
                   "trip says about these fields is about the form, not about "
                   "what the application stored.",
            evidence=[s.screenshot(f"{shot}-not-held")], screen=screen))
        say(f"    !! {len(held)} field(s) did not hold their value")
    elif f.entries:
        res.checks.append(R.passed(
            f"{spec.name} holds every value that was typed into it",
            detail=f"All {len(f.entries)} field(s) still read back what was "
                   f"entered, so anything missing after Save was dropped by "
                   f"the application rather than never typed.", screen=screen))

    if dry_run:
        say(f"  {spec.name}: dry run — not saving")
        if spec.kind == LINKAGE:
            f._close_modal()
        res.notes.append(R.observation(
            f"{spec.name} is saved",
            f"Dry run: {len(f.entries)} field(s) were filled and abandoned.",
            screen=screen))
        return f.entries

    rows_before = flows._grid_rows(s) if spec.kind == LINKAGE else -1

    # `save_alt` is tried only when the pass names alternatives, so a screen
    # whose commit control was probed field by field still gets exactly one
    # attempt and exactly the failure it got before.
    names = [spec.save_label] + [x for x in spec.save_alt
                                 if x != spec.save_label]
    committed, refusal = "", ""
    for name in names:
        try:
            f.commit(name)
            committed = name
            step(f"save {spec.name}", note=f"clicked {name}")
            break
        # PWError belongs here for the same reason it belongs in the
        # collateral dialog's loop: a commit control that is covered, disabled
        # or simply absent raises a Playwright TIMEOUT, not a FillError. Left
        # out, it escaped this loop and the whole screen with it — one run
        # lost every Facility Coverage result to a raw
        # 'Locator.click: Timeout ... body button:has-text("Save")', including
        # the side that had already saved perfectly well.
        except (W.FillError, W.WriteRefused, PWError) as exc:
            refusal = refusal or str(exc).splitlines()[0][:250]
    if not committed:
        # No commit control AND nothing that could be entered is a READ-ONLY
        # view, not a screen that refuses to save. The facility's Risk Rating
        # Log and R1 Log History are both of those: they list what has
        # happened to the facility, they have no enterable field and no Save,
        # and reporting them as "Save button missing" put two failures in the
        # report for two tabs behaving exactly as they should — while saying
        # nothing about the tabs that really did refuse.
        if not f.entries:
            res.notes.append(R.observation(
                f"{spec.name} is saved",
                f"Nothing on this tab can be entered and it has no "
                f"{' / '.join(names)} control, so there was nothing to fill "
                f"or save. It reads as a log or history view rather than a "
                f"form — {refusal}", screen=screen))
            step(f"save {spec.name}", note="read-only: nothing to save")
            say(f"  {spec.name}: nothing enterable and no Save — read-only")
            if spec.kind == LINKAGE:
                f._close_modal()
            return f.entries
        res.checks.append(R.failed(
            f"{spec.name} is saved",
            expected=f"a {' or '.join(names)} button on {spec.name}",
            actual=refusal,
            detail=f"{len(f.entries)} field(s) were filled on this tab and "
                   f"there is no way to commit them, so everything entered "
                   f"here was lost.",
            evidence=[s.screenshot(f"{shot}-nosave")], screen=screen))
        step(f"save {spec.name}", R.FAIL, refusal[:140])
        if spec.kind == LINKAGE:
            f._close_modal()
        return f.entries

    msgs = f.messages()
    still_invalid = f.unsatisfied()
    refused_rules = flows._refused_rules(s)
    saved_shot = s.screenshot(f"{shot}-saved")

    if msgs["bad"] or refused_rules:
        res.checks.append(R.failed(
            f"{spec.name} is saved",
            expected="the app confirms it was saved",
            actual=" | ".join((msgs["bad"] + refused_rules)[:3]),
            detail="The application refused the save. Its own validation rules "
                   "are quoted above — a rule marked 'blocks the save' means "
                   "the values entered have to change, not the automation."
            if refused_rules else "",
            evidence=[saved_shot], screen=screen))
        for r in refused_rules:
            say(f"    !! refused: {r}")
        f._close_modal()
        return f.entries

    if still_invalid:
        res.checks.append(R.failed(
            f"{spec.name} is saved",
            expected="Save stores the values and clears the form's messages",
            actual=f"the form is still showing {len(still_invalid)} unmet "
                   f"rule(s): " + " | ".join(still_invalid[:4]),
            detail="Save was refused, so nothing was written for this pass.",
            evidence=[saved_shot], screen=screen))
        f._close_modal()
        return f.entries

    if spec.kind == LINKAGE:
        f._close_modal()
        rows_after = flows._grid_rows(s)
        if rows_after > rows_before:
            res.checks.append(R.passed(
                f"{spec.name} is saved",
                detail=f"The screen's tables went from {rows_before} to "
                       f"{rows_after} row(s), so the row was stored.",
                screen=screen))
        else:
            res.checks.append(R.failed(
                f"{spec.name} is saved",
                expected=f"one more row than the {rows_before} before saving",
                actual=f"still {rows_after} row(s)",
                detail="Save raised no error but no table gained a row, so "
                       "nothing was stored.",
                evidence=[s.screenshot(f"{shot}-norow")], screen=screen))
        return f.entries

    if not f.entries:
        res.checks.append(R.failed(
            f"{spec.name} is saved",
            expected=f"the {len(spec.fields)} field(s) on this pass entered "
                     f"and stored",
            actual="not one field could be set, so nothing was stored",
            detail="Save reported no error because an empty form is valid. "
                   "See the field failures above for why nothing went in.",
            evidence=[saved_shot], screen=screen))
    elif failed_required:
        res.checks.append(R.failed(
            f"{spec.name} is saved",
            expected="every mandatory field entered before Save",
            actual=f"{failed_required} mandatory field(s) could not be set",
            detail="Save raised no error, but the pass was committed with "
                   "mandatory fields missing. The round trip below says what "
                   "actually reached the record.",
            evidence=[saved_shot], screen=screen))
    else:
        res.checks.append(R.passed(
            f"{spec.name} is saved",
            detail=f"{len(f.entries)} field(s) entered and saved"
                   + (f". {' | '.join(msgs['ok'][:2])}" if msgs["ok"] else "."),
            screen=screen))
    return f.entries


# Matching a validation message to the field it names lives in flows.py, because
# the obligor form needs exactly the same thing and two copies of a matcher this
# fiddly would drift apart. Re-exported here under the names this module has
# always used.
_RULE_WORDS = flows._RULE_WORDS


_field_in_message = flows._field_in_message


def _satisfy_unmet(f: W.Filler, res: CaseFlowResult, say,
                   rounds: int = 3) -> list[str]:
    """
    Answer the fields the form's own validation messages name, and report what
    was answered.

    Read-and-act rather than read-and-quote. A mandatory field that no pass
    authored and that discovery could not see — because it is disabled until
    something else is answered, or because its label is not one the pass knows
    — refuses the save for everything on that tab. Two of them cost this run
    two whole facility tabs: 'Facility Request Type is Required' and 'Grace
    Period is Required', each on a form whose other twenty-odd fields were
    filled and then thrown away.

    Three rounds, because answering one field can render the next: a repayment
    structure that only demands a grace period once it is chosen. It stops as
    soon as a round changes nothing, so a rule that cannot be satisfied by
    filling a field — a range check, a cross-field rule — costs one pass over
    the messages and is then left to be reported as the blocker it is.
    """
    answered: list[str] = []
    for _ in range(rounds):
        msgs = f.unsatisfied()
        if not msgs:
            break
        labels = f.field_labels(140)
        progressed = False
        for msg in msgs:
            label = _field_in_message(msg, labels)
            if not label:
                continue
            kind = f.kind_of(label)
            if kind in ("missing", "readonly", "unknown", "file"):
                continue
            # A chooser gets the app's own first valid option; anything typed
            # gets the same generated value discovery would have given it.
            value = (None if kind in ("dropdown", "lookup", "select", "switch")
                     else _auto_value(label, kind, res.marker))
            try:
                e = f.set_value(label, value, when=_auto_date(label))
            except (W.FillError, Exception):  # noqa: BLE001
                continue          # reported by the unmet-rules check below
            answered.append(f"{e.label} = {e.value[:40]}")
            say(f"    * {e.label} = {e.value[:50]!r} — the form said it was "
                f"required")
            progressed = True
        if not progressed:
            break
    return answered


def _values_held(f: W.Filler) -> list[str]:
    """
    Fields that no longer show what was just typed into them, read off the form
    before it is saved.

    Only the entries this pass made are checked, and each against its own
    recorded value, so this says nothing about fields the app filled itself.
    """
    out: list[str] = []
    for e in f.entries:
        # A file input is exempt. The browser deliberately does not hand a page
        # the file it was given — the value reads back as a fake path, or as
        # nothing at all once the app has moved the upload into its own list —
        # so neither answer is evidence about the attachment. Whether it stored
        # is what the round trip is for.
        if e.kind == "file":
            continue
        try:
            shown = f.value_of(e.label)
        except Exception:  # noqa: BLE001 - a control that will not read back
            continue       # is not evidence either way
        if not shown:
            out.append(f"{e.label}: now empty")
        elif not flows._same_value(e.value, shown, e.kind):
            out.append(f"{e.label}: now {shown[:40]!r}")
    return out


def _open_add_row(f: W.Filler, spec: Pass) -> bool:
    """
    Open a table's add-row dialog, by its caption where there is one.

    The heading is preferred for the reason widgets.add_row gives: the '+' icons
    only exist while their section is expanded, so an index that meant one table
    yesterday means another today. Falling back to the first '+' is still worth
    doing here — unlike the obligor tabs, these screens usually carry a single
    table, and the captions are not known for every deployment.
    """
    if spec.grid_heading and f.add_row(heading=spec.grid_heading):
        return True
    return f.add_row(spec.grid)


def _fill_discovered(f: W.Filler, res: CaseFlowResult, done: set[str],
                     say, cap: int = 120,
                     value_for: Optional[Callable] = None
                     ) -> tuple[int, list[str]]:
    """
    Fill the fields the screen is showing that nobody authored.

    Discovery returns NAMES; every one of them is then set by name through
    set_value, so the rule that no control is ever located by position still
    holds. Read-only fields and ones the app has disabled are passed over
    silently — the app owns those, and there is nothing to enter. So is a file
    input: nothing can be typed into one, and only the pass's own `attach`
    knows which file belongs on this form.

    `value_for` replaces _auto_value for one caller. It exists for a form that
    is nothing BUT discovered text boxes — see _attachment_value.
    """
    filled, refused = 0, []
    for label in f.field_labels(cap):
        key = _norm(label)
        if not key or key in done:
            continue
        done.add(key)
        kind = f.kind_of(label)
        if kind in ("missing", "readonly", "unknown", "radio", "file"):
            continue
        value = (value_for or _auto_value)(label, kind, res.marker)
        try:
            e = f.set_value(label, value, when=_auto_date(label))
            filled += 1
            say(f"    + {e.label} = {e.value[:50]!r}")
            continue
        except (W.FillError, Exception) as exc:  # noqa: BLE001
            first = exc

        # A dropdown pinned to 'No' that has no such option is not a refusal to
        # take a value, it is a field that was never a Yes/No question —
        # 'Syndication Currency' matches the same words as 'Is Syndicated
        # Limit?'. Pinning is a precaution, so where it does not apply the
        # field still gets filled rather than being reported as unsettable.
        if (value == "No" and kind in ("dropdown", "lookup", "select")
                and "no option matching" in str(first)):
            try:
                e = f.set_value(label, None, when=_auto_date(label))
                filled += 1
                say(f"    + {e.label} = {e.value[:50]!r}")
                continue
            except (W.FillError, Exception) as exc:  # noqa: BLE001
                first = exc

        if not flows._not_on_screen(first):
            refused.append(f"{label}: {str(first).splitlines()[0][:90]}")
    return filled, refused


# --------------------------------------------------------------------------
# Request Details
# --------------------------------------------------------------------------

def _do_request_details(s: Session, res: CaseFlowResult, dry_run: bool,
                        say, step) -> None:
    screen = SCREEN_LABEL[REQUEST_DETAILS]
    _open_screen(s, screen, step)
    s.screenshot("10-request-details")
    for spec in REQUEST_DETAILS_PASSES:
        # A table pass with no table to add to is not a failure — see the note
        # on the Purpose of Request pass.
        if spec.kind == LINKAGE and not _add_control_count(s):
            say(f"  {spec.name}: no add-row control on this screen — skipped")
            res.notes.append(R.observation(f"{spec.name} is filled", spec.note,
                                        screen=screen))
            continue
        say(f"  {spec.name} …")
        entries = _fill_pass(s, res, screen, spec, dry_run, say, step)
        res.entries.extend(e.as_dict() for e in entries)


# --------------------------------------------------------------------------
# Facilities
# --------------------------------------------------------------------------

# What the button that starts a facility request is called. Matched forgivingly
# and in this order: several of these exist on the screen at once on some
# builds, and "Add Facility" is the one that opens the product list.
_ADD_FACILITY = ["add facility", "request facility", "new facility",
                 "create facility", "add new facility", "add", "new", "create",
                 "request"]

# The pass that records WHICH facility was requested. It belongs to no tab —
# the choice was made in a dialog before any tab existed — so the verification
# leg must not try to pair it with one.
REQUESTED_FACILITY = "Facilities — requested facility"

# Which product to request, in order of preference, falling back to the first
# selectable entry in the tree.
#
# Named rather than "whatever is first" because the choice is not cosmetic: the
# product decides which tabs the facility then has, and the tabs are what this
# run exists to fill. First in this environment's tree is
# "8607 - DM - Direct Corp/Comm (EMI)" — a treasury instrument that does not
# carry Overdues or a profit structure. A plain funded running-finance line
# does, so one is asked for by name.
FACILITY_PREFERENCE = [
    "Running Finance", "Term Finance", "Demand Finance", "Cash Finance",
    "Finance Against Trust Receipt", "Overdraft",
]


def _do_facilities(s: Session, res: CaseFlowResult, dry_run: bool,
                   say, step) -> None:
    screen = SCREEN_LABEL[FACILITIES]
    _open_screen(s, screen, step)
    s.screenshot("20-facilities")

    # ---- select the requested facility --------------------------------
    chosen = _request_facility(s, res, say, step, dry_run)
    if chosen is None:
        return
    res.facility_ref = chosen

    # ---- walk whatever tabs that produced ------------------------------
    strip = s.tab_strip(limit=16)
    if not strip:
        # No tab strip: the facility detail is a single form. Fill it as one
        # pass against the section that fits best — Facility Details — and let
        # discovery pick up the rest.
        say("  the facility detail has no tab strip; filling it as one form")
        spec = Pass(name="Facilities — facility detail",
                    fields=[fld for p in FACILITY_PASSES if p.kind == FORM
                            for fld in p.fields])
        entries = _fill_pass(s, res, screen, spec, dry_run, say, step)
        res.entries.extend(e.as_dict() for e in entries)
        _fill_unmatched_tables(s, res, screen, dry_run, say, step)
        return

    # The strip is re-read after every pass, and that is the whole reason this
    # is a loop rather than a walk over one list.
    #
    # A newly requested facility opens showing ONE tab — Facility Request
    # Details. Facility Details, Limits and Exposures, Overdue, the pricing tab
    # and the rest do not exist until it has been saved, exactly as the
    # obligor's nine tabs are locked until the obligor is. Reading the strip
    # once up front therefore found a single tab and reported the other seven
    # as "no tab matching", on a facility that grows them a moment later.
    matched: set[str] = set()
    processed: set[str] = set()
    for sweep in range(1, 6):
        strip = [t for t in s.tab_strip(limit=16)
                 if _norm(t["label"]) not in processed]
        if not strip:
            break
        if sweep == 1:
            say(f"  the facility opens with {len(strip)} tab(s): "
                + ", ".join(t["label"] for t in strip))
        else:
            say(f"  {len(strip)} further tab(s) unlocked after saving: "
                + ", ".join(t["label"] for t in strip))

        for tab in strip:
            label = tab["label"]
            processed.add(_norm(label))
            spec = _pass_for_tab(label)
            if spec is not None:
                matched.add(spec.name)
            # `active` was read before the previous pass saved, so it cannot be
            # trusted now. open_tab reports an already-open tab as success, so
            # asking for it unconditionally costs nothing.
            try:
                s.open_tab(label)
            except NavigationError as e:
                res.checks.append(R.failed(
                    f"Facilities — {label} can be opened",
                    expected=f"the facility has a '{label}' tab that opens",
                    actual=str(e)[:250],
                    evidence=[s.screenshot(f"tab-{_safe(label)}-missing")],
                    screen=screen))
                step(f"open the {label} tab", R.FAIL, str(e)[:140])
                continue
            step(f"open the {label} tab")

            if spec is None:
                # A tab the specification does not name. It is still part of
                # the facility, so it is filled from what it shows rather than
                # skipped — skipping is how a tab quietly stops being tested.
                spec = Pass(name=f"Facilities — {label}",
                            note="No section of the specification is named "
                                 "after this tab, so every field on it was "
                                 "discovered from the screen.")
                say(f"  {label}: not named in the specification — filling by "
                    f"discovery")
            say(f"  {spec.name} …")
            entries = _fill_pass(s, res, screen, spec, dry_run, say, step)
            res.entries.extend(e.as_dict() for e in entries)
            # A tab may carry its own table as well as its fields.
            _fill_tables_on_tab(s, res, screen, label, dry_run, say, step)

    say(f"  {len(processed)} tab(s) filled in total")
    for spec in FACILITY_PASSES:
        if spec.name in matched:
            continue
        res.notes.append(R.observation(
            f"{spec.name} is filled",
            f"Even after saving, this facility never showed a tab matching "
            f"{' / '.join(spec.aliases) or spec.name!r}, so there was nothing "
            f"to fill. It showed: {', '.join(sorted(processed)) or 'none'}. "
            f"Which tabs a facility has depends on the product requested."
            + (f" {spec.note}" if spec.note else ""),
            screen=screen))


def _request_facility(s: Session, res: CaseFlowResult, say, step,
                      dry_run: bool) -> Optional[str]:
    """
    Start a facility request: choose the requested facility, then Proceed.

    Nothing on a facility is enterable until this has happened — the tabs do
    not exist yet — so this is not a preliminary, it is the step that creates
    the thing being filled. It is also a SEQUENCE of dialogs on some builds
    (the product, then the sub-product or the currency), each with its own
    Proceed, and answering only the first looks like it worked while creating
    nothing at all. So it keeps answering until no dialog is left.
    """
    screen = SCREEN_LABEL[FACILITIES]
    f = W.Filler(session=s, screen=screen, group=REQUESTED_FACILITY)

    btns = cr.collect_action_buttons(s.page)
    opener = None
    for want in _ADD_FACILITY:
        opener = next((b for b in btns
                       if want in (b["label"] or "").strip().lower()), None)
        if opener is not None:
            break

    if opener is not None:
        if not cr._click_stamped(s.page, "data-crawl-action", opener["index"],
                                 timeout=8000):
            res.checks.append(R.failed(
                "A facility can be requested",
                expected=f"the '{opener['label']}' button opens the facility "
                         f"request",
                actual="the button would not click",
                evidence=[s.screenshot("21-add-facility-stuck")], screen=screen))
            return None
        cr.wait_until_settled(s.page, s.recorder, timeout_ms=20000,
                              stable_polls=2)
        cr.stamp_content_root(s.page)
        step(f"click {opener['label']}", note=s.page.url)
    elif not f.add_row(0):
        res.checks.append(R.failed(
            "A facility can be requested",
            expected="a button or '+' on Facilities that starts a new facility",
            actual="none was found. Available: "
                   + (", ".join(b["label"] for b in btns[:8]) or "no buttons"),
            detail="Without it there is no facility to fill, so the rest of "
                   "this screen could not be exercised.",
            evidence=[s.screenshot("21-no-add-facility")], screen=screen))
        return None
    else:
        step("open the facility request form")

    s.screenshot("22-facility-request")

    # ---- answer the dialogs -------------------------------------------
    chosen_bits: list[str] = []
    for round_no in range(1, 5):
        dialog = s.page.locator(".modal.show")
        if not dialog.count():
            break
        title = _dialog_title(s, round_no)

        picked = _choose_requested_facility(f, s, say)
        if picked:
            chosen_bits.append(picked)
            step(f"select the requested facility in '{title}'", note=picked[:120])
        else:
            # Some builds open a full form rather than a picker. Filling it is
            # the same problem as any other pass.
            filled, _ = _fill_discovered(f, res, set(), say)
            step(f"fill '{title}'", note=f"{filled} field(s)")

        if dry_run:
            say("  dry run — not pressing Proceed, so no facility is created")
            f._close_modal()
            res.notes.append(R.observation(
                "A facility can be requested",
                "Dry run: the requested facility was selected but Proceed was "
                "deliberately not pressed, so no facility was created and its "
                "tabs could not be reached. Re-run without the dry-run option "
                "to fill them.", screen=screen))
            res.entries.extend(e.as_dict() for e in f.entries)
            return None

        proceeded = False
        for name in ("Proceed", "Add", "Save", "OK", "Select"):
            try:
                f.commit(name)
                proceeded = True
                step(f"click {name}")
                break
            except (W.FillError, W.WriteRefused):
                continue
        if not proceeded:
            res.checks.append(R.failed(
                "A facility can be requested",
                expected="a Proceed button on the facility request dialog",
                actual="no Proceed, Add, Save, OK or Select button was enabled",
                evidence=[s.screenshot(f"23-no-proceed-{round_no}")],
                screen=screen))
            f._close_modal()
            return None
        cr.wait_until_settled(s.page, s.recorder, timeout_ms=30000,
                              stable_polls=2)
        s.page.wait_for_timeout(1500)
        s.screenshot(f"24-after-proceed-{round_no}")

    res.entries.extend(e.as_dict() for e in f.entries)
    chosen = " / ".join(chosen_bits)
    say(f"  requested facility: {chosen or '(not read from the dialog)'}")

    # Proceed should have left us in the facility's own detail. If it did not,
    # the facility is in the grid and has to be opened.
    if not s.tab_strip(limit=4) and not _looks_like_detail(s):
        opened = s.open_row_detail(0)
        if opened:
            step("open the new facility from the grid", note=opened[:120])
        else:
            res.notes.append(R.observation(
                "The requested facility opens for editing",
                "Proceed did not open a facility detail and no row on the "
                "Facilities grid could be opened, so its tabs could not be "
                "filled.", evidence=[s.screenshot("25-no-facility-detail")],
                screen=screen))
            return None

    res.checks.append(R.passed(
        "A facility can be requested",
        detail=f"Selected {chosen or 'the first facility offered'} and pressed "
               f"Proceed; the facility opened for editing.", screen=screen))
    s.screenshot("26-facility-detail")
    return chosen


def _choose_requested_facility(f: W.Filler, s: Session, say) -> str:
    """
    Pick the facility in whichever shape the "Requested Facility" dialog offers.

    On this application it is an inline treeview: a "Select option" toggle that
    drops down a Search box over a tree whose top level is a group called
    "Facility". That is tried FIRST because it is what the dialog actually
    uses. The rest are kept as fallbacks — this dialog is configuration, and a
    build that renders it as a plain dropdown or a grid of rows should not stop
    the run dead. None of them failing is fatal either: the caller then fills
    the dialog as an ordinary form.
    """
    attempts = (
        lambda: f.choose_in_tree(label="Requested Facility",
                                 prefer=FACILITY_PREFERENCE),
        lambda: f.choose_in_dialog(None, label="Requested Facility"),
        lambda: f.pick_row_in_dialog(None, label="Requested Facility"),
    )
    for attempt in attempts:
        try:
            e = attempt()
            say(f"  selected: {e.value[:80]!r}")
            return e.value
        except W.FillError as exc:
            say(f"    (not this shape: {str(exc).splitlines()[0][:80]})")

    # A magnifier-style lookup inside the dialog: set the first one it has.
    for label in f.field_labels(30):
        try:
            if f.kind_of(label) != "lookup":
                continue
            e = f.lookup(label, None)
            say(f"  selected {label}: {e.value[:80]!r}")
            return e.value
        except W.FillError:
            continue
    return ""


def _dialog_title(s: Session, round_no: int) -> str:
    try:
        t = s.page.locator(".modal.show .modal-title, .modal.show h4, "
                           ".modal.show h5").first
        if t.count():
            return (t.inner_text() or "").strip() or f"dialog {round_no}"
    except Exception:  # noqa: BLE001
        pass
    return f"dialog {round_no}"


def _looks_like_detail(s: Session) -> bool:
    """Are we on a record's own screen rather than a summary grid?"""
    try:
        return bool(s.page.evaluate(
            """() => {
                const root = document.querySelector('[data-crawl-root]') || document.body;
                return root.querySelectorAll('label[title]').length > 3;
            }"""))
    except Exception:  # noqa: BLE001
        return False


def _pass_for_tab(tab: str,
                  passes: Optional[list[Pass]] = None) -> Optional[Pass]:
    """
    Which authored pass belongs to this tab.

    Word overlap rather than substring, because the app and the specification
    word things differently — the app's 'Overdues' against an authored
    '8.4 Overdue',
    and 'Limit and Exposure' is '8.3 Limits and Exposures'. A tab that matches
    nothing gets no pass and is filled by discovery instead, which is the honest
    answer: we do not know what the specification says it should hold, but it is
    still part of the facility.

    `passes` names the set to match against. It defaults to the facility's, so
    every existing caller behaves exactly as it did; Collaterals passes its own
    because a collateral's tabs are a different strip entirely.
    """
    want = _words(tab)
    if not want:
        return None
    best, best_score = None, 0.0
    for spec in (passes if passes is not None else FACILITY_PASSES):
        for alias in spec.aliases:
            have = _words(alias)
            if not have:
                continue
            overlap = len(want & have)
            if not overlap:
                continue
            covered = overlap / len(want)
            if covered < 0.6:
                continue
            score = covered + overlap / len(have)
            if score > best_score:
                best, best_score = spec, score
    return best


_STOP = {"and", "or", "the", "of", "a", "in", "to", "for", "details", "detail"}


def _words(text: str) -> set[str]:
    out = set()
    for word in _norm(text).split():
        if word.isdigit() or word in _STOP or len(word) < 2:
            continue
        out.add(word[:-1] if word.endswith("s") and len(word) > 3 else word)
    return out


def _fill_tables_on_tab(s: Session, res: CaseFlowResult, screen: str, tab: str,
                        dry_run: bool, say, step, cap: int = 4,
                        passes: Optional[list[Pass]] = None,
                        prefix: str = "Facilities") -> None:
    """
    Fill EVERY table on this tab, not only the ones the specification names.

    A facility tab can carry a grid as well as its fields — Limits and
    Exposures has Participant Banks under it, and other tabs carry schedules and
    breakdowns the specification gives no field table for. Filling only the
    named ones is how a table quietly stops being tested, so each is opened by
    its caption, filled from its authored pass where one matches and from what
    the dialog shows where none does, and saved on its own.

    Bounded by `cap`: a tab with a dozen grids is a screen this has not been
    designed against, and grinding through it silently would be worse than
    saying so.
    """
    headings = _table_headings(s)
    plus_count = _add_control_count(s)
    if not plus_count:
        return
    say(f"  {tab}: {plus_count} table(s) to add a row to")

    for i in range(min(plus_count, cap)):
        heading = headings[i] if i < len(headings) else ""
        spec = _linkage_pass_for(heading, tab, passes)
        if spec is None:
            spec = Pass(
                name=f"{prefix} — {tab} → {heading or f'table {i + 1}'}",
                kind=LINKAGE, grid_heading=heading, grid=i,
                note="The specification names no field table for this grid, so "
                     "every field in its add-row form was discovered from the "
                     "form itself.")
        else:
            # Keep the authored fields, but address THIS grid.
            spec = Pass(name=spec.name, kind=LINKAGE, aliases=spec.aliases,
                        fields=spec.fields,
                        grid_heading=heading or spec.grid_heading, grid=i,
                        note=spec.note)
        say(f"  {spec.name} …")
        entries = _fill_pass(s, res, screen, spec, dry_run, say, step)
        res.entries.extend(e.as_dict() for e in entries)


def _linkage_pass_for(heading: str, tab: str,
                      passes: Optional[list[Pass]] = None) -> Optional[Pass]:
    """The authored table pass whose name matches this grid's caption or tab."""
    for spec in (passes if passes is not None else FACILITY_PASSES):
        if spec.kind != LINKAGE:
            continue
        for alias in spec.aliases:
            words = _words(alias)
            if not words:
                continue
            if heading and words & _words(heading):
                return spec
            if not heading and words & _words(tab):
                return spec
    return None


def _table_headings(s: Session) -> list[str]:
    """The captions above the grids on this screen, in document order."""
    try:
        return [g.get("title", "") for g in s.read_grids()]
    except Exception:  # noqa: BLE001
        return []


def _add_control_count(s: Session) -> int:
    """How many add-row '+' controls the routed content is showing."""
    try:
        return int(s.page.evaluate(
            """() => {
                const root = document.querySelector('[data-crawl-root]') || document.body;
                const vis = (el) => el.offsetParent !== null || el.getClientRects().length;
                let n = 0;
                for (const el of root.querySelectorAll('i, span, a, button')) {
                    const c = typeof el.className === 'string' ? el.className : '';
                    if (/fa-plus(?!-)/.test(c) && !/user-plus/.test(c) && vis(el)) n++;
                }
                return n;
            }"""))
    except Exception:  # noqa: BLE001
        return 0


def _fill_unmatched_tables(s: Session, res: CaseFlowResult, screen: str,
                           dry_run: bool, say, step) -> None:
    """The facility's tables when it has no tab strip to hang them on."""
    _fill_tables_on_tab(s, res, screen, "facility detail", dry_run, say, step)


# --------------------------------------------------------------------------
# Collaterals
# --------------------------------------------------------------------------

def _do_collaterals(s: Session, res: CaseFlowResult, dry_run: bool,
                    say, step) -> None:
    """
    Ask for a collateral, then fill every tab it opens with.

    Deliberately the same shape as _do_facilities, because the screen is the
    same shape: a grid, an Add that opens a picker, a Proceed that creates the
    record, and a tab strip that depends on what was asked for. The strip is
    therefore re-read between passes rather than walked once — on the facility
    that mattered because saving unlocks further tabs, and there is no reason
    to assume a collateral behaves differently.
    """
    screen = SCREEN_LABEL[COLLATERALS]
    _open_screen(s, screen, step)
    s.page.wait_for_timeout(1200)
    s.screenshot("80-collaterals")

    chosen = _request_collateral(s, res, say, step, dry_run)
    if chosen is None:
        return
    res.collateral_ref = chosen

    strip = s.tab_strip(limit=16)
    if not strip:
        # No tab strip: the collateral detail is a single form. Fill it as one
        # pass carrying every authored field, and let discovery do the rest.
        say("  the collateral detail has no tab strip; filling it as one form")
        spec = Pass(name="Collaterals — collateral detail",
                    fields=[fld for p in COLLATERAL_PASSES if p.kind == FORM
                            for fld in p.fields])
        entries = _fill_pass(s, res, screen, spec, dry_run, say, step)
        res.entries.extend(e.as_dict() for e in entries)
        _fill_tables_on_tab(s, res, screen, "collateral detail", dry_run, say,
                            step, passes=COLLATERAL_PASSES,
                            prefix="Collaterals")
        return

    matched: set[str] = set()
    processed: set[str] = set()
    for sweep in range(1, 6):
        strip = [t for t in s.tab_strip(limit=16)
                 if _norm(t["label"]) not in processed]
        if not strip:
            break
        if sweep == 1:
            say(f"  the collateral opens with {len(strip)} tab(s): "
                + ", ".join(t["label"] for t in strip))
        else:
            say(f"  {len(strip)} further tab(s) unlocked after saving: "
                + ", ".join(t["label"] for t in strip))

        for tab in strip:
            label = tab["label"]
            processed.add(_norm(label))
            spec = _pass_for_tab(label, COLLATERAL_PASSES)
            if spec is not None:
                matched.add(spec.name)
            try:
                s.open_tab(label)
            except NavigationError as e:
                res.checks.append(R.failed(
                    f"Collaterals — {label} can be opened",
                    expected=f"the collateral has a '{label}' tab that opens",
                    actual=str(e)[:250],
                    evidence=[s.screenshot(f"coll-tab-{_safe(label)}-missing")],
                    screen=screen))
                step(f"open the {label} tab", R.FAIL, str(e)[:140])
                continue
            step(f"open the {label} tab")

            if spec is None:
                spec = Pass(name=f"Collaterals — {label}",
                            note="No section of the specification is named "
                                 "after this tab, so every field on it was "
                                 "discovered from the screen.")
                say(f"  {label}: not named in the specification — filling by "
                    f"discovery")
            say(f"  {spec.name} …")
            entries = _fill_pass(s, res, screen, spec, dry_run, say, step)
            res.entries.extend(e.as_dict() for e in entries)
            # A tab may carry its own table as well as its fields.
            _fill_tables_on_tab(s, res, screen, label, dry_run, say, step,
                                passes=COLLATERAL_PASSES,
                                prefix="Collaterals")

    say(f"  {len(processed)} tab(s) filled in total")
    for spec in COLLATERAL_PASSES:
        if spec.name in matched:
            continue
        res.notes.append(R.observation(
            f"{spec.name} is filled",
            f"Even after saving, this collateral never showed a tab matching "
            f"{' / '.join(spec.aliases) or spec.name!r}, so there was nothing "
            f"to fill. It showed: {', '.join(sorted(processed)) or 'none'}. "
            f"Which tabs a collateral has depends on what was asked for in "
            f"the Obligor Collateral dialog."
            + (f" {spec.note}" if spec.note else ""),
            screen=screen))


def _request_collateral(s: Session, res: CaseFlowResult, say, step,
                        dry_run: bool) -> Optional[str]:
    """
    Start a collateral: answer the Obligor Collateral dialog, then Proceed.

    Nothing on a collateral is enterable until this has happened — the tabs do
    not exist yet — so this is not a preliminary, it is the step that creates
    the thing being filled.

    The dialog's two dropdowns are answered IN ORDER, classification first.
    The classification is what populates the name list, so answering the name
    first finds an empty one; the same dependency the obligor form has between
    Regulatory Sector and Regulatory Industry. Both are left to the app's own
    first option: they are reference data, re-seeded per environment, and what
    this run needs is a collateral that exists rather than a particular one.
    """
    screen = SCREEN_LABEL[COLLATERALS]
    f = W.Filler(session=s, screen=screen, group=REQUESTED_COLLATERAL)

    btns = cr.collect_action_buttons(s.page)
    opener = None
    for want in _ADD_COLLATERAL:
        opener = next((b for b in btns
                       if want in (b["label"] or "").strip().lower()), None)
        if opener is not None:
            break

    if opener is None:
        res.checks.append(R.failed(
            "A collateral can be added",
            expected="an 'Add' button on the Collaterals screen",
            actual="none was found. Available: "
                   + (", ".join(b["label"] for b in btns[:8]) or "no buttons"),
            detail="Without it there is no collateral to fill, so nothing on "
                   "this screen could be exercised.",
            evidence=[s.screenshot("81-no-add-collateral")], screen=screen))
        return None

    if not cr._click_stamped(s.page, "data-crawl-action", opener["index"],
                             timeout=8000):
        res.checks.append(R.failed(
            "A collateral can be added",
            expected=f"the '{opener['label']}' button opens the Obligor "
                     f"Collateral dialog",
            actual="the button would not click",
            evidence=[s.screenshot("81-add-collateral-stuck")], screen=screen))
        return None
    cr.wait_until_settled(s.page, s.recorder, timeout_ms=20000, stable_polls=2)
    cr.stamp_content_root(s.page)
    s.page.wait_for_timeout(1200)
    step(f"click {opener['label']}")
    s.screenshot("82-collateral-dialog")

    # ---- answer the dialog(s) -----------------------------------------
    #
    # A loop, for the same reason the facility request is one: on some builds
    # this is a SEQUENCE of dialogs, each with its own Proceed, and answering
    # only the first looks like it worked while creating nothing at all.
    chosen_bits: list[str] = []
    last_picked = ""
    for round_no in range(1, 5):
        if not s.page.locator(".modal.show").count():
            break
        title = _dialog_title(s, round_no)

        picked = _choose_collateral(f, s, say)

        # A round that answers exactly what the last one answered is the same
        # dialog again, not the next one in a sequence: Proceed clicked
        # something and the dialog did not advance. Carrying on re-answers it
        # until the loop runs out and files every answer again — the run that
        # prompted this recorded 'Pledge / Pledge / Pledge / Pledge' as what
        # it had asked for.
        if picked and picked == last_picked:
            res.notes.append(R.observation(
                "A collateral can be added",
                f"The Obligor Collateral dialog is still showing after "
                f"Proceed and still offering the same choices "
                f"({picked[:80]}), so it did not advance and no collateral "
                f"was created. Nothing further was clicked rather than "
                f"answering the same dialog again.",
                evidence=[s.screenshot(f"83-dialog-stuck-{round_no}")],
                screen=screen))
            f._close_modal()
            return None
        last_picked = picked

        if picked:
            chosen_bits.append(picked)
            step(f"choose the collateral in '{title}'", note=picked[:120])
        else:
            # Not the two-dropdown shape this was written against. Filling it
            # as an ordinary form is the same problem as any other pass.
            filled, _ = _fill_discovered(f, res, set(), say)
            step(f"fill '{title}'", note=f"{filled} field(s)")

        if dry_run:
            say("  dry run — not pressing Proceed, so no collateral is created")
            f._close_modal()
            res.notes.append(R.observation(
                "A collateral can be added",
                "Dry run: the collateral was chosen but Proceed was "
                "deliberately not pressed, so no collateral was created and "
                "its tabs could not be reached. Re-run without the dry-run "
                "option to fill them.", screen=screen))
            res.entries.extend(e.as_dict() for e in f.entries)
            return None

        # Nothing can be clicked through an open option panel, so the way is
        # cleared first and the state is reported if it will not clear.
        clear = _dismiss_option_panel(s)

        proceeded, refusal = False, ""
        for name in ("Proceed", "Add", "Save", "OK", "Select"):
            try:
                f.commit(name)
                proceeded = True
                step(f"click {name}")
                break
            # PWError is in this list because of what its absence did: a
            # Proceed button covered by an option panel raises a Playwright
            # TIMEOUT, not a FillError, so it escaped this loop, escaped
            # _do_collaterals, and surfaced as "Collaterals could be filled"
            # with a raw locator dump for a detail — no screenshot, no
            # explanation, and the other screens' results thrown away with it.
            except (W.FillError, W.WriteRefused, PWError) as exc:
                refusal = refusal or str(exc).splitlines()[0][:200]
                continue
        if not proceeded:
            res.checks.append(R.failed(
                "A collateral can be added",
                expected="a Proceed button on the Obligor Collateral dialog",
                actual=refusal or "no Proceed, Add, Save, OK or Select button "
                                  "was enabled",
                detail=("An option panel was still open over the dialog and "
                        "would not close, so the click landed on the panel "
                        "rather than on the button."
                        if not clear else
                        f"The classification and name may not both have been "
                        f"answered — this dialog keeps Proceed disabled until "
                        f"they are. Chosen so far: "
                        f"{' / '.join(chosen_bits) or 'nothing'}."),
                evidence=[s.screenshot(f"83-no-proceed-{round_no}")],
                screen=screen))
            f._close_modal()
            return None
        cr.wait_until_settled(s.page, s.recorder, timeout_ms=30000,
                              stable_polls=2)
        s.page.wait_for_timeout(1500)
        s.screenshot(f"84-after-proceed-{round_no}")

    res.entries.extend(e.as_dict() for e in f.entries)
    chosen = " / ".join(chosen_bits)
    say(f"  collateral requested: {chosen or '(not read from the dialog)'}")

    # Proceed should have left us in the collateral's own detail. If it did
    # not, the collateral is in the grid and has to be opened.
    if not _anchor_on_screen(s, _COLLATERAL_ANCHOR) and not s.tab_strip(limit=4):
        opened = s.open_row_detail(0)
        if opened:
            step("open the new collateral from the grid", note=opened[:120])
        else:
            res.notes.append(R.observation(
                "The collateral opens for editing",
                "Proceed did not open a collateral detail and no row on the "
                "Collaterals grid could be opened, so its tabs could not be "
                "filled.",
                evidence=[s.screenshot("85-no-collateral-detail")],
                screen=screen))
            return None

    res.checks.append(R.passed(
        "A collateral can be added",
        detail=f"Chose {chosen or 'the first collateral offered'} in the "
               f"Obligor Collateral dialog and pressed Proceed; the "
               f"collateral opened for editing.", screen=screen))
    s.screenshot("86-collateral-detail")
    return chosen


def _choose_collateral(f: W.Filler, s: Session, say) -> str:
    """
    Answer the Obligor Collateral dialog's two dropdowns, in order.

    Classification first, then name, because the classification populates the
    name list. Each is tried in the shapes this application uses for a
    chooser — an ng-select, then the inline treeview, then a magnifier lookup
    — and a field that is simply not in this build's dialog is skipped rather
    than failing the step.

    Two waits matter here and neither is padding.

    The name list is FETCHED when the classification is answered, so reading
    it in the same breath finds it empty or still holding the previous
    classification's entries. The dialog is let settle in between.

    And the panel is dismissed after each pick. The Name control is a
    MULTI-select — it reads "Select options", plural — and a multi-select
    leaves its option panel open after a choice, where it covers whatever is
    beneath it: the other dropdown, and then Proceed. That is a click landing
    on an overlay rather than the button, which arrives as "no Proceed button
    was enabled" on a dialog that has one.

    Returns what was chosen, as "classification / name", or "" when neither
    dropdown is there at all — which tells the caller to treat the dialog as
    an ordinary form instead.
    """
    picked: list[str] = []
    for labels in (_COLLATERAL_CLASSIFICATION, _COLLATERAL_NAME):
        for label in labels:
            got = _choose_one_in_dialog(f, label, say)
            if not got:
                continue
            # The same value coming back for both dropdowns means the second
            # one was never really answered: the driver found the first
            # dropdown again and re-read its value. Saying so is the whole
            # difference between a readable failure and a run that reports a
            # classification as the collateral's name and presses on.
            if got in picked:
                say(f"  !! {label} came back as {got!r}, which is already the "
                    f"answer to the dropdown above it — so this one was not "
                    f"actually chosen")
                continue
            picked.append(got)
            _dismiss_option_panel(s)
            # Answering the classification triggers the fetch that fills the
            # name list.
            try:
                cr.wait_until_settled(s.page, s.recorder, timeout_ms=15000,
                                      stable_polls=2)
            except PWError:
                pass
            s.page.wait_for_timeout(900)
            break
    return " / ".join(picked)


# Every shape of option panel this dialog can leave hanging open over the
# button underneath it. The treeview is the one that mattered: only
# `.ng-dropdown-panel` was looked for, a treeview panel is none of those
# classes, so the panel stayed open across the whole dialog and Proceed was
# clicked through it — arriving as "Locator.click: Timeout 10000ms exceeded"
# on a button that was right there on screen.
_OPTION_PANELS = (".ng-dropdown-panel", "ngx-treeview", ".treeview-container",
                  ".dropdown-menu.show")


def _option_panel_open(s: Session) -> str:
    """Which option panel, if any, is open and visible right now."""
    for sel in _OPTION_PANELS:
        try:
            if s.page.locator(sel).first.is_visible(timeout=200):
                return sel
        except PWError:
            continue
    return ""


def _dismiss_option_panel(s: Session, tries: int = 3) -> bool:
    """
    Close whichever option panel is still open, and confirm it went.

    Escape only, and only while a panel is actually showing: pressing it with
    no panel open closes the DIALOG on this template, which would throw away
    the classification that had just been chosen.

    Returns whether the way is clear. The caller needs that answer before it
    presses Proceed: a panel left over the button is not a button that refuses
    to work, and reporting it as one sends somebody looking in the wrong
    place.
    """
    for _ in range(tries):
        panel = _option_panel_open(s)
        if not panel:
            return True
        try:
            s.page.keyboard.press("Escape")
            s.page.wait_for_timeout(400)
        except PWError:
            return False
    return not _option_panel_open(s)


def _choose_one_in_dialog(f: W.Filler, label: str, say) -> str:
    """
    Set one chooser in the open dialog, whichever shape it is drawn in.

    The inline TREEVIEW is tried first, and confined to this field's own
    container, because that is the shape both of this dialog's dropdowns
    actually are — a "Select option" toggle over a Search box and a tree — and
    because the dialog has two of them.

    Both halves of that mattered. Tried in the old order, set_value read the
    control as a magnifier lookup and drove it as one, which left the panel
    open; and the tree driver, asked for the name, found the FIRST tree in the
    dialog — the classification's — and picked from it. The run recorded
    'Pledge' as both the Collateral Classification and the Collateral Name,
    which is a classification appearing where a name belongs, and left an open
    option panel over Proceed.

    None is passed as the value throughout: these lists are reference data and
    the run needs a valid entry, not a named one.
    """
    if f.kind_of(label) == "missing":
        return ""

    # Stamps this field's container as [data-fill-field] and hands back a
    # locator for it. The stamp is what scopes the tree toggle to THIS
    # dropdown rather than to whichever one the dialog draws first.
    try:
        f._block(label)
        mine = "[data-fill-field]"
    except W.FillError:
        mine = ""

    attempts = (
        lambda: f.choose_in_tree(label=label, within=mine),
        lambda: f.set_value(label, None),
        lambda: f.choose_in_dialog(None, label=label),
        lambda: f.lookup(label, None),
    )
    for attempt in attempts:
        try:
            e = attempt()
            if e.value:
                say(f"  {label} = {e.value[:70]!r}")
                return e.value
        except (W.FillError, Exception):  # noqa: BLE001
            continue
    say(f"  !! {label} is on the dialog but would not take a value")
    return ""


# --------------------------------------------------------------------------
# Facility Coverage
# --------------------------------------------------------------------------

# The '+' controls on the two trees, with which side of the screen each is on.
#
# Side is decided by horizontal position, which is the only thing that
# distinguishes them: both trees are the same component rendering the same
# icon, and the facilities one is simply the left column. The midpoint of the
# ROOT container is used rather than half the window, so a collapsed sidebar
# or a narrower screen does not move the boundary across the icons.
#
# Each one is also STAMPED, so it can be addressed as an element and not only
# as a pair of coordinates. A blind mouse click at a point is the one strategy
# with no way of reporting that it missed: it cannot fail, it simply does
# nothing, and the run then blames the app for a dialog that never opened —
# which is what "no dialog appeared within twelve seconds" on the facility tree
# was, while the identical icon on the collateral tree opened its dialog
# perfectly well in the same run.
_COVERAGE_PLUS_JS = r"""() => {
    const root = document.querySelector('[data-crawl-root]') || document.body;
    const box = root.getBoundingClientRect();
    const mid = box.left + box.width / 2;
    const onScreen = (r) => r.width > 0 && r.height > 0 && r.right > 0
                         && r.bottom > 0 && r.left < innerWidth
                         && r.top < innerHeight;
    document.querySelectorAll('[data-cov-plus]').forEach(
        e => e.removeAttribute('data-cov-plus'));
    const out = [];
    for (const el of root.querySelectorAll('i, span, a, button')) {
        const c = typeof el.className === 'string' ? el.className : '';
        if (!/fa-plus(?!-)|ft-plus(?!-)|la-plus(?!-)/.test(c)) continue;
        if (/user-plus/.test(c)) continue;
        const r = el.getBoundingClientRect();
        if (!onScreen(r)) continue;
        // An icon nested inside another matching element is the same control
        // twice. Keep the innermost, which is the one that carries the glyph.
        if (el.querySelector('i, span')
            && /fa-plus(?!-)|ft-plus(?!-)|la-plus(?!-)/.test(
                   (el.querySelector('i, span').className || '').toString()))
            continue;
        // The node's own text, so the report can say WHICH facility or
        // collateral the association was added to.
        const row = el.closest('li, .treeview-item, div') || el;
        const id = String(out.length);
        el.setAttribute('data-cov-plus', id);
        out.push({
            id: id,
            x: Math.round(r.left + r.width / 2),
            y: Math.round(r.top + r.height / 2),
            side: r.left < mid ? 'facility' : 'collateral',
            node: (row.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 90),
        });
    }
    out.sort((a, b) => a.y - b.y);
    return out;
}"""

# How many dialogs are actually ON SCREEN. Counted rather than waited for,
# because `.modal.show` alone cannot answer the question this needs answering:
# this template leaves a closed dialog in the DOM still carrying `.show`, so
# wait_for_selector can be satisfied by a dialog that is not there — or, worse,
# sit waiting on a hidden one that happens to come first in document order
# while the dialog that has just opened is right there behind it.
_VISIBLE_MODALS_JS = r"""() => {
    const vis = (el) => {
        const r = el.getBoundingClientRect();
        return r.width > 0 && r.height > 0 && r.top < innerHeight
            && r.bottom > 0;
    };
    return [...document.querySelectorAll('.modal.show, .modal.in')]
        .filter(vis).length;
}"""

# Click a stamped '+' as an ELEMENT, through whatever wrapper carries the
# handler. The glyph is often an <i> inside the <a> that is actually bound.
_CLICK_PLUS_JS = r"""(id) => {
    const el = document.querySelector('[data-cov-plus="' + id + '"]');
    if (!el) return false;
    (el.closest('a, button') || el).click();
    return true;
}"""


def _coverage_plus_controls(s: Session, side: str) -> list[dict]:
    """The '+' controls on one tree of the Facility Coverage screen."""
    try:
        spots = s.page.evaluate(_COVERAGE_PLUS_JS) or []
    except PWError:
        return []
    return [x for x in spots if x.get("side") == side]


def _visible_modals(s: Session) -> int:
    try:
        return int(s.page.evaluate(_VISIBLE_MODALS_JS) or 0)
    except PWError:
        return 0


def _wait_for_modal(s: Session, was: int, timeout_ms: int = 6000) -> bool:
    """Wait for one more dialog to be on screen than there was."""
    waited = 0
    while waited < timeout_ms:
        s.page.wait_for_timeout(250)
        waited += 250
        if _visible_modals(s) > was:
            # Let it finish its fade before anything reads it.
            s.page.wait_for_timeout(800)
            return True
    return False


# The dialog that is actually ON SCREEN, stamped so it can be addressed.
#
# '.modal.show' is not good enough as a scope and a live run showed why. This
# template leaves a closed dialog in the DOM still carrying .show, so
# querySelector('.modal.show') can resolve to the one that has just been shut
# rather than the one that has just opened. The run filled the facility side,
# closed it, opened the collateral side — and then reported "No field labelled
# 'Coverage Percentage' is on this screen. Visible here:" with nothing after
# it, because it was looking inside a dialog that was no longer there.
#
# The LAST visible one is the live one: this template appends each new modal.
_MODAL_SCOPE_JS = r"""() => {
    document.querySelectorAll('[data-modal-scope]').forEach(
        e => e.removeAttribute('data-modal-scope'));
    const vis = (el) => {
        const r = el.getBoundingClientRect();
        return r.width > 0 && r.height > 0 && r.top < innerHeight
            && r.bottom > 0;
    };
    const open = [...document.querySelectorAll('.modal.show, .modal.in')]
        .filter(vis);
    if (!open.length) return false;
    open[open.length - 1].setAttribute('data-modal-scope', '1');
    return true;
}"""

# The dialog's own '+ Add', which appends a row. Matched on the button's text
# rather than its position, and confined to the open dialog so it can never be
# the '+' on a tree behind it — that one opens another dialog.
_ADD_ROW_JS = r"""() => {
    const dlg = document.querySelector('[data-modal-scope]');
    if (!dlg) return false;
    for (const el of dlg.querySelectorAll('button, a, .btn')) {
        const t = (el.textContent || '').replace(/\s+/g, ' ').trim();
        if (!/^\+?\s*add$/i.test(t)) continue;
        if (el.disabled) continue;
        el.click();
        return true;
    }
    return false;
}"""


def _association_dialog_scope(s: Session) -> str:
    """Stamp whichever Collateral Association dialog is on screen."""
    try:
        if s.page.evaluate(_MODAL_SCOPE_JS):
            return "[data-modal-scope]"
    except PWError:
        pass
    return ""


def _dialog_fields(s: Session, scope: str) -> list[str]:
    """The labelled fields the open dialog is showing."""
    try:
        return W.Filler(session=s, screen=SCREEN_LABEL[COVERAGE],
                        scope_selector=scope).field_labels(20)
    except Exception:  # noqa: BLE001 - an empty dialog is the answer, not a crash
        return []


def _counterpart_options(s: Session, scope: str,
                         labels: list[str]) -> Optional[list[str]]:
    """
    What this side still has left to associate, or None when it cannot be told.

    An empty list here is not a defect and must not be reported as one. An
    association is ONE relationship between a facility and a collateral, and
    the dialog offers only what is not already associated with the node it was
    opened from. On a case carrying a single facility and a single collateral,
    the first side associates the pair and the second is then correctly
    offered nothing at all — the app is right and the run has nothing left to
    do.

    Asked before anything is typed, because the alternative is finding out
    from a mandatory field that "opened no options to choose from" and filing
    the app's correct behaviour as a failed check.

    Filler.options opens the panel, reads it and dismisses it with a neutral
    click INSIDE the dialog. It must never press Escape to do that: Escape in
    a Bootstrap modal closes the modal, so asking this question used to destroy
    the dialog it was being asked about, and every field of it was then
    reported as missing from a screen that was working perfectly. See
    Filler._dismiss_overlay.

    ng-select's own "No items found" row is not an option, and is dropped
    here: an exhausted list has to read as empty, because empty is what tells
    the caller this side has nothing left to associate.

    None means the question could not be put: the control is not an ng-select
    on this build, so the ordinary fill should go ahead and report whatever it
    finds.
    """
    f = W.Filler(session=s, screen=SCREEN_LABEL[COVERAGE],
                 scope_selector=scope)
    for label in labels:
        try:
            if f.kind_of(label) == "missing":
                continue
            return [o for o in f.options(label)
                    if not re.match(r"^-?\s*select\s*-?$|^no items", o, re.I)]
        except Exception:  # noqa: BLE001 - not a dropdown, or would not open
            return None
    return None


def _press_plus(s: Session, spot: dict) -> bool:
    """
    Press one tree's '+' and wait for a dialog that was not there before.

    Three ways of pressing it, escalating only when the previous one produced
    no dialog. None of them is redundant:

      - The ELEMENT, clicked by Playwright. Scrolls it into view and waits for
        it to be hittable, so it reports a control that is covered or off
        screen instead of silently missing it.
      - A real MOUSE click at the point the icon occupies, which is how this
        worked before. Several of this template's icon controls ignore a
        synthetic element click, so this is not a fallback but a genuinely
        different gesture.
      - A scripted click on the wrapper that carries the handler, for the
        opposite case: an icon whose <a> is bound but whose hit area is a
        single pixel the mouse misses.

    Counting dialogs rather than waiting on a selector is what makes the
    escalation safe. Each attempt stops the moment the count goes up, so a
    click that DID work is never followed by a second one — which would stack
    a second dialog on the first.
    """
    was = _visible_modals(s)
    el = s.page.locator(f'[data-cov-plus="{spot["id"]}"]').first

    def landed() -> bool:
        return _wait_for_modal(s, was)

    try:
        if el.count():
            el.scroll_into_view_if_needed(timeout=3000)
            el.click(timeout=5000)
            if landed():
                return True
    except PWError:
        pass
    if _visible_modals(s) > was:
        return True

    # Where the icon is NOW, not where it was when the tree was surveyed:
    # scrolling it into view above may have moved it, and a mouse click at a
    # stale point lands on whatever has taken that spot.
    x, y = spot["x"], spot["y"]
    try:
        box = el.bounding_box(timeout=1000) if el.count() else None
        if box:
            x = box["x"] + box["width"] / 2
            y = box["y"] + box["height"] / 2
    except PWError:
        pass
    try:
        s.page.mouse.click(x, y)
        if landed():
            return True
    except PWError:
        pass
    if _visible_modals(s) > was:
        return True

    try:
        if s.page.evaluate(_CLICK_PLUS_JS, spot["id"]) and landed():
            return True
    except PWError:
        pass
    return _visible_modals(s) > was


def _open_association_dialog(s: Session, spot: dict) -> str:
    """
    Click one tree's '+', wait for the dialog, and make sure it has a row.

    Returns a selector for the dialog, or "" if it never arrived.

    Two things beyond "a modal appeared", both of which a live run needed.

    The dialog is addressed by a stamp on the one that is visible rather than
    by '.modal.show' — see _MODAL_SCOPE_JS for what that cost.

    And it is not enough for the dialog to be open: it has to be showing a ROW
    to fill. It opens holding an empty row when the node has no associations
    yet, and can open holding none at all otherwise — which is a dialog with
    no labelled field in it, and every field then reported as missing on a
    screen that was working correctly. Its own '+ Add' is what adds a row, so
    that is pressed rather than treating an empty dialog as a defect.
    """
    if not _press_plus(s, spot):
        return ""

    scope = _association_dialog_scope(s)
    if not scope:
        return ""
    if _dialog_fields(s, scope):
        return scope

    # No row to fill. Add one, and give the app a moment to render it.
    try:
        added = bool(s.page.evaluate(_ADD_ROW_JS))
    except PWError:
        added = False
    if not added:
        return scope           # report it as the empty dialog it is
    s.page.wait_for_timeout(1200)
    _association_dialog_scope(s)
    return scope


def _do_coverage(s: Session, res: CaseFlowResult, dry_run: bool,
                 say, step) -> None:
    """
    Associate a collateral with a facility, from BOTH sides of the screen.

    The same association recorded from either end, which is why both are
    exercised rather than one: a build can perfectly well save the facility
    side and drop the collateral side, and one pass would not notice.

    Nothing here is a grid, so none of the grid machinery applies — the '+'
    that opens the dialog is on a tree node, found by position, and the dialog
    it opens is filled as an ordinary form because widgets scopes itself to an
    open modal automatically.
    """
    screen = SCREEN_LABEL[COVERAGE]
    _open_screen(s, screen, step)
    s.page.wait_for_timeout(1500)
    s.screenshot("90-facility-coverage")

    before = _coverage_tree_text(s)
    specs = coverage_passes(res.marker)
    claimed: list[str] = []
    entered: set[str] = set()

    for spec, side in zip(specs, ("facility", "collateral")):
        spots = _coverage_plus_controls(s, side)
        if not spots:
            res.notes.append(R.observation(
                f"{spec.name}: an association can be added",
                f"The {side} tree shows no '+' to add an association with. "
                f"That tree is empty when the case has no "
                f"{'facility' if side == 'facility' else 'collateral'} yet — "
                f"this screen associates the two, so it has nothing to offer "
                f"until both exist.",
                evidence=[s.screenshot(f"91-no-plus-{side}")], screen=screen))
            say(f"  the {side} tree has no '+' to click")
            continue

        spot = spots[0]
        say(f"  {side} tree: adding to {spot['node'][:70]!r}")
        scope = _open_association_dialog(s, spot)
        if not scope:
            res.checks.append(R.failed(
                f"{spec.name}: an association can be added",
                expected=f"the '+' on {spot['node'][:60]!r} opens the "
                         f"{_COVERAGE_DIALOG} dialog",
                actual="no dialog appeared within twelve seconds of clicking it",
                evidence=[s.screenshot(f"91-no-dialog-{side}")], screen=screen))
            continue
        step(f"open {_COVERAGE_DIALOG} from the {side} tree",
             note=spot["node"][:120])
        s.screenshot(f"92-association-{side}")

        # An open dialog with nothing in it is not a dialog that can be
        # filled, and saying so once beats reporting each of its three fields
        # as missing — which is what the run before this did, three times over
        # with an empty "Visible here:" after each.
        shown = _dialog_fields(s, scope)
        if not shown:
            res.notes.append(R.observation(
                f"{spec.name}: an association can be added",
                f"The '+' on {spot['node'][:60]!r} opened the "
                f"{_COVERAGE_DIALOG} dialog, but it shows no enterable field "
                f"even after its own '+ Add' was pressed — so there was no row "
                f"to fill. On this side the dialog may offer nothing to "
                f"associate: the only counterpart on the case is already "
                f"associated with this node.",
                evidence=[s.screenshot(f"92-empty-dialog-{side}")],
                screen=screen))
            say(f"  the {side} dialog opened with no row to fill")
            _close_modal_hard(s)
            s.page.wait_for_timeout(1000)
            continue
        res.checks.append(R.passed(
            f"{spec.name}: an association can be added",
            detail=f"The '+' on {spot['node'][:70]!r} opened the "
                   f"{_COVERAGE_DIALOG} dialog, showing {len(shown)} "
                   f"field(s): " + ", ".join(shown[:6]), screen=screen))

        # Is there anything left on this side to associate? Asked before
        # anything is typed, because an exhausted list is the app being right
        # rather than a field that will not take a value — and filling on
        # regardless reports it as three failed checks.
        left = _counterpart_options(s, scope, spec.fields[0].labels)
        if left is not None and not left:
            res.notes.append(R.observation(
                f"{spec.name} is filled",
                f"Nothing is left to associate from this side: "
                f"{spot['node'][:60]!r} is already associated with every "
                f"{'collateral' if side == 'facility' else 'facility'} on the "
                f"case, so the dialog's {spec.fields[0].label} list is empty. "
                f"An association is one relationship between a facility and a "
                f"collateral, and the other side of this screen has already "
                f"made it — on a case carrying one of each there is only ever "
                f"one to make. Add a second "
                f"{'collateral' if side == 'facility' else 'facility'} to the "
                f"case to exercise this side too.",
                evidence=[s.screenshot(f"92-nothing-to-associate-{side}")],
                screen=screen))
            say(f"  the {side} side has nothing left to associate — the other "
                f"side already made the only association available")
            _close_modal_hard(s)
            s.page.wait_for_timeout(1000)
            continue
        if left:
            say(f"  {len(left)} left to associate on the {side} side")

        # Filled as a form, not as a linkage row: the dialog is already open,
        # so every field it holds is addressable by label without add_row
        # having to find a grid '+' that this screen does not have.
        #
        # The scope is the STAMPED dialog, not '.modal.show'. Filler falls
        # back to the whole page when it cannot see its scope, and one run's
        # Save went looking through `body` and timed out on the page behind
        # the dialog — losing the screen's whole result to it. The stamp keeps
        # both the fields and the Save button inside the dialog that is
        # actually open.
        say(f"  {spec.name} …")
        entries = _fill_pass(s, res, screen, spec, dry_run, say, step,
                             scope=scope)
        res.entries.extend(e.as_dict() for e in entries)
        # The share claimed on this side, recorded so the report can name what
        # it added on a screen that has no reference of its own to quote, and
        # so the tree is only searched for sides that actually claimed one.
        for e in entries:
            if _norm(e.label).startswith("coverage"):
                claimed.append(f"{side} {e.value}%")
                entered.add(spec.name)
                break

        # The dialog has to go before the tree behind it can be re-read, and
        # before the other side's '+' can be clicked.
        _close_modal_hard(s)
        s.page.wait_for_timeout(1200)

    res.coverage_ref = "; ".join(claimed)
    if dry_run:
        return

    # The tree gaining the percentage this run entered is the honest test of
    # whether Save stored anything, and a better one than the absence of an
    # error toast — this dialog closes on save whether or not it wrote.
    after = _coverage_tree_text(s)
    sides = _coverage_side_text(s, expand=False)
    both_shot = ""
    for offset, spec in enumerate(specs):
        # A side that entered nothing has no share to look for, and demanding
        # one invents a failure out of the app being right: a side with
        # nothing left to associate is reported above, as the non-event it is.
        if spec.name not in entered:
            continue
        pct = _coverage_percent(res.marker, offset)

        # Where the association ended up, which is the question this screen
        # exists to answer: it has to be under the facility on the left AND
        # under the collateral on the right.
        both_shot = both_shot or s.screenshot("93-coverage-both-trees")
        both = _check_both_trees(
            sides, pct,
            f"{spec.name} is on the facility tree and the collateral tree",
            screen, both_shot, notes=res.notes)
        if both is not None:
            res.checks.append(both)
            say(f"    {pct}% on both trees: {both.status}")

        if f"{pct}%" in after and f"{pct}%" not in before:
            res.checks.append(R.passed(
                f"{spec.name} appears on the coverage tree",
                detail=f"A node now reads {pct}%, which is this run's own "
                       f"share and was not on the tree before it ran.",
                screen=screen))
        elif f"{pct}%" in after:
            res.notes.append(R.observation(
                f"{spec.name} appears on the coverage tree",
                f"The tree shows {pct}%, but it showed it before this run "
                f"saved as well, so it cannot be told from an association "
                f"that was already there. The round trip below re-reads the "
                f"tree from a fresh load.", screen=screen))
        else:
            res.checks.append(R.failed(
                f"{spec.name} appears on the coverage tree",
                expected=f"a node carrying this run's share, {pct}%",
                actual="no node on either tree does",
                detail="Save raised no error but the association is not on "
                       "the tree, so nothing was stored.",
                evidence=[s.screenshot("93-coverage-not-listed")],
                screen=screen))


# The controls that OPEN a tree node, and nothing that adds to one.
#
# The distinction is the whole point. A '+' on this screen is the add control:
# clicking one opens the Collateral Association dialog, which the read-only
# verification leg must never do. An expander is a caret, chevron or angle,
# and only the right-pointing ones are worth clicking — a node already open
# points down, and clicking that would CLOSE it.
_EXPAND_TREE_JS = r"""() => {
    const root = document.querySelector('[data-crawl-root]') || document.body;
    const vis = (el) => {
        const r = el.getBoundingClientRect();
        return r.width > 0 && r.height > 0 && r.top < innerHeight
            && r.bottom > 0;
    };
    let clicked = 0;
    for (const el of root.querySelectorAll('i, span, a')) {
        const c = typeof el.className === 'string' ? el.className : '';
        // Never an add control, however it is spelled.
        if (/plus/.test(c)) continue;
        if (!/caret-right|chevron-right|angle-right|arrow-right|collapsed/
              .test(c)) continue;
        if (!vis(el)) continue;
        el.click();
        clicked++;
    }
    return clicked;
}"""


def _expand_coverage_trees(s: Session, rounds: int = 4) -> int:
    """
    Open every collapsed node on both trees, so what they hold can be read.

    A collapsed node's children are NOT in the document — this is a tree, not
    a list with things hidden by CSS — so reading the page while they are shut
    finds an association that is sitting there perfectly well and reports it
    as never stored. That is exactly what one run did: the app answered
    "Linkage Saved Successfully", the pass recorded the save, and the round
    trip then said no node carried this run's share.

    Repeated because opening a node can reveal further collapsed ones beneath
    it, and bounded because a tree that keeps producing them is a screen this
    was not designed against.
    """
    opened = 0
    for _ in range(rounds):
        try:
            n = int(s.page.evaluate(_EXPAND_TREE_JS) or 0)
        except PWError:
            break
        if not n:
            break
        opened += n
        s.page.wait_for_timeout(700)
    return opened


def _coverage_tree_text(s: Session, expand: bool = True) -> str:
    """
    Everything both trees are showing, as one string.

    The trees render their associations as text — "CC30445 - Consumer Goods
    (0) - 25%" — so this is where an association can be read back from. Field
    values are included as well for the same reason _readable_text does it:
    part of this screen is a form.

    Nodes are opened first unless the caller says otherwise, because a closed
    node's contents are not in the document to be read.
    """
    if expand:
        _expand_coverage_trees(s)
    return _readable_text(s)


# Each tree on its own, because "it is on the screen" is not the question this
# screen has to answer.
#
# An association is ONE relationship recorded at both ends: the collateral has
# to appear under the facility on the left, and the facility under the
# collateral on the right. Reading the whole page as one string cannot tell
# those apart — a share rendered on one tree alone satisfies it — and that is
# exactly the half-written association this screen is most likely to produce.
#
# The two cards are titled FACILITY and COLLATERAL, so the headings are used
# first. Where they are not found the panels are split down the middle of the
# screen, which is the same rule the '+' discovery uses to decide which tree an
# icon is on.
_COVERAGE_SIDE_TEXT_JS = r"""() => {
    const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
    const vis = (el) => {
        const r = el.getBoundingClientRect();
        return r.width > 0 && r.height > 0;
    };
    const out = {facility: '', collateral: ''};
    const TREE = '.treeview-item, tree-root, ngx-treeview, [class*="treeview"], ul';
    const want = {facility: /^facilit(y|ies)$/i, collateral: /^collaterals?$/i};
    // The case menu lists 'Facilities' and 'Collaterals' too, and it is a <ul>
    // of <li> — which is to say it looks exactly like a tree to the rule
    // below. Reading the sidebar as the facility panel would answer this
    // screen's question with the navigation.
    const inChrome = (el) => !!el.closest(
        'nav, aside, .sidebar, .main-menu, .menu-content, [class*="sidebar"]');

    for (const key of ['facility', 'collateral']) {
        for (const h of document.querySelectorAll(
                'h1, h2, h3, h4, h5, h6, .card-title, .card-header, legend')) {
            if (!vis(h) || inChrome(h)) continue;
            if (!want[key].test(norm(h.textContent))) continue;
            let node = h.parentElement;
            for (let i = 0; i < 5 && node; i++, node = node.parentElement) {
                if (node.querySelector(TREE)) { out[key] = norm(node.innerText); break; }
            }
            if (out[key]) break;
        }
    }
    if (out.facility && out.collateral) return out;

    const root = document.querySelector('[data-crawl-root]') || document.body;
    const box = root.getBoundingClientRect();
    const mid = box.left + box.width / 2;
    const bucket = {facility: [], collateral: []};
    for (const el of root.querySelectorAll(
            'li, .treeview-item, .node-content-wrapper')) {
        if (!vis(el) || inChrome(el)) continue;
        const t = norm(el.innerText);
        if (!t) continue;
        const r = el.getBoundingClientRect();
        bucket[r.left < mid ? 'facility' : 'collateral'].push(t);
    }
    return {facility: out.facility || bucket.facility.join(' | '),
            collateral: out.collateral || bucket.collateral.join(' | ')};
}"""


def _coverage_side_text(s: Session, expand: bool = True) -> dict:
    """What each tree is showing, keyed 'facility' and 'collateral'."""
    if expand:
        _expand_coverage_trees(s)
    try:
        sides = s.page.evaluate(_COVERAGE_SIDE_TEXT_JS) or {}
    except PWError:
        sides = {}
    return {"facility": sides.get("facility") or "",
            "collateral": sides.get("collateral") or ""}


def _check_both_trees(sides: dict, pct: str, name: str, screen: str, shot: str,
                      notes: Optional[list] = None) -> Optional[R.Check]:
    """
    Does this run's association show on BOTH trees?

    The share is what identifies it — this dialog has no free-text field, so
    there is no marker to look for and the percentage is derived from the run's
    own marker instead. See _coverage_percent.

    Returns None when neither tree could be read as a panel of its own. That is
    not a missing association and must not be reported as one: it is this
    reader failing to identify the two panels, which says nothing about the
    application. It is recorded as an observation instead, and the check above
    still says whether the share is on the screen at all.
    """
    on_facility = f"{pct}%" in sides["facility"]
    on_collateral = f"{pct}%" in sides["collateral"]

    if not sides["facility"] and not sides["collateral"]:
        if notes is not None:
            notes.append(R.observation(
                name,
                "Neither tree could be read as a panel of its own, so which "
                "side the association is on could not be told. The check above "
                "says whether it is on the screen at all.", screen=screen))
        return None

    if on_facility and on_collateral:
        return R.passed(
            name,
            detail=f"The facility tree shows the collateral at {pct}% and the "
                   f"collateral tree shows the facility at {pct}%, so the "
                   f"association is recorded at both ends.", screen=screen)

    if not on_facility and not on_collateral:
        return R.failed(
            name,
            expected=f"a node carrying {pct}% on the facility tree AND on the "
                     f"collateral tree",
            actual="neither tree carries it",
            detail="The association is on neither side, so nothing was "
                   "stored.",
            evidence=[shot], screen=screen)

    here, missing = (("facility", "collateral") if on_facility
                     else ("collateral", "facility"))
    return R.failed(
        name,
        expected=f"a node carrying {pct}% on both trees",
        actual=f"only the {here} tree carries it; the {missing} tree does not",
        detail=f"An association is one relationship recorded at both ends — "
               f"the collateral belongs under the facility and the facility "
               f"under the collateral. This one was written to the {here} "
               f"side only, so the {missing} tree does not show it.",
        evidence=[shot], screen=screen)


# --------------------------------------------------------------------------
# Risk Rating
# --------------------------------------------------------------------------

def _click_named_button(s: Session, wanted: list[str],
                        timeout: int = 8000) -> str:
    """
    Press the first action button whose text matches one of `wanted`.

    Returns the label it clicked, or "". Matched on the button's own text
    rather than on position, and `wanted` is tried in order so a page carrying
    both "Generate Score" and "Generate" presses the specific one.
    """
    try:
        btns = cr.collect_action_buttons(s.page)
    except PWError:
        return ""
    for want in wanted:
        hit = next((b for b in btns
                    if want == (b["label"] or "").strip().lower()), None)
        if hit is None:
            hit = next((b for b in btns
                        if want in (b["label"] or "").strip().lower()), None)
        if hit is None:
            continue
        if not cr._click_stamped(s.page, "data-crawl-action", hit["index"],
                                 timeout=timeout):
            return ""
        try:
            cr.wait_until_settled(s.page, s.recorder, timeout_ms=20000,
                                  stable_polls=2)
            cr.stamp_content_root(s.page)
        except PWError:
            pass
        return hit["label"]
    return ""


def _button_labels(s: Session) -> list[str]:
    """What this page offers to press, for an error message that helps."""
    try:
        return [b["label"] for b in cr.collect_action_buttons(s.page)][:10]
    except PWError:
        return []


# The card or panel under a given heading, stamped so it can be filled without
# touching the rest of the page.
#
# The model page is one long form of several sections, and only one of them is
# this run's business: the LOVs under Basic Information are the model's INPUTS,
# while the financial figures above them are computed and the result block
# below them is the answer. Filling the page unscoped would type into all
# three.
_SECTION_SCOPE_JS = r"""([wanted]) => {
    const norm = (s) => (s || '').toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim();
    const vis = (el) => {
        const r = el.getBoundingClientRect();
        return r.width > 0 && r.height > 0;
    };
    document.querySelectorAll('[data-section-scope]').forEach(
        e => e.removeAttribute('data-section-scope'));
    const FIELD = 'input:not([type=hidden]), select, textarea, ng-select, app-dropdown';
    for (const want of wanted) {
        const key = norm(want);
        for (const h of document.querySelectorAll(
                'h1, h2, h3, h4, h5, h6, legend, .card-title, .card-header, '
                + '.panel-title, .section-title, th, td, label, span, div')) {
            if (!vis(h)) continue;
            if (norm(h.textContent) !== key) continue;
            // The smallest ancestor that holds this heading AND some field.
            let node = h.parentElement;
            for (let i = 0; i < 8 && node; i++, node = node.parentElement) {
                if (node.querySelector(FIELD)) {
                    node.setAttribute('data-section-scope', '1');
                    return want;
                }
            }
        }
    }
    return '';
}"""


def _section_scope(s: Session, headings: list[str]) -> tuple[str, str]:
    """
    Stamp the section under one of these headings.

    Returns (selector, the heading that matched), or ("", "").
    """
    try:
        found = s.page.evaluate(_SECTION_SCOPE_JS, [headings])
    except PWError:
        return "", ""
    return ("[data-section-scope]", found) if found else ("", "")


# The model's result block, read as label/value pairs.
#
# These are not form fields — they are table rows the app writes its answer
# into — so Filler.value_of cannot see them. The label sits in one cell and the
# value in a later one on the same row, which is what this walks.
_RATING_RESULT_JS = r"""([wanted]) => {
    const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();
    const norm = (s) => clean(s).toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim();
    const vis = (el) => {
        const r = el.getBoundingClientRect();
        return r.width > 0 && r.height > 0;
    };
    const out = {};
    for (const want of wanted) {
        const key = norm(want);
        let value = null;
        for (const row of document.querySelectorAll('tr')) {
            if (!vis(row)) continue;
            const cells = [...row.querySelectorAll('td, th')];
            const at = cells.findIndex(c => norm(c.textContent) === key);
            if (at < 0) continue;
            // The first cell after the label that has anything in it. A layout
            // cell between the two is common here and is not the answer.
            for (let i = at + 1; i < cells.length; i++) {
                const cell = cells[i];
                const input = cell.querySelector('input, select, textarea');
                const text = input
                    ? clean(input.value || (input.selectedOptions
                            && input.selectedOptions[0]
                            && input.selectedOptions[0].textContent))
                    : clean(cell.textContent);
                if (text) { value = text; break; }
            }
            if (value !== null) break;
            value = '';          // the row is there, the answer is blank
        }
        if (value !== null) out[want] = value;
    }
    return out;
}"""


def _read_rating_results(s: Session) -> dict:
    """Whatever the model's result block is showing, by label."""
    try:
        return s.page.evaluate(_RATING_RESULT_JS, [RATING_RESULTS]) or {}
    except PWError:
        return {}


# A field the model has flagged, which on this page means RED.
#
# Colour is read from the COMPUTED style rather than from class names, because
# that is what the operator is actually looking at: the instruction for this
# screen is "if there are any fields flagged in red, that means there is an
# error". Class names are checked too, since a build can flag a cell with
# `.text-danger` and leave the colour to a stylesheet this cannot resolve.
#
# The threshold is deliberately strict — strongly red, and clearly not grey or
# black — so ordinary dark text and the app's own muted borders do not read as
# errors. A washed-out pink background would be missed; a flagged field would
# not.
_RED_FIELDS_JS = r"""() => {
    const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();
    const vis = (el) => {
        const r = el.getBoundingClientRect();
        return r.width > 0 && r.height > 0 && r.bottom > 0;
    };
    const rgb = (v) => {
        const m = (v || '').match(/rgba?\(([^)]+)\)/);
        if (!m) return null;
        const p = m[1].split(',').map(x => parseFloat(x));
        if (p.length > 3 && p[3] < 0.15) return null;    // transparent
        return p;
    };
    const isRed = (v) => {
        const p = rgb(v);
        if (!p) return false;
        const [r, g, b] = p;
        return r > 120 && r - g > 60 && r - b > 60;
    };
    const root = document.querySelector('[data-crawl-root]') || document.body;
    const out = [];
    const seen = new Set();
    for (const el of root.querySelectorAll(
            'td, th, tr, input, select, textarea, span, div, label, p')) {
        if (!vis(el)) continue;
        const cls = typeof el.className === 'string' ? el.className : '';
        const st = getComputedStyle(el);
        const flagged = /\b(text-danger|is-invalid|has-error|invalid|error|red)\b/
                            .test(cls)
                        || isRed(st.color) || isRed(st.backgroundColor)
                        || isRed(st.borderTopColor) && st.borderTopWidth !== '0px';
        if (!flagged) continue;
        // The smallest flagged element wins: flagging a cell also flags every
        // wrapper above it, and reporting the whole table says nothing.
        if (el.querySelector('td, th, input, select, textarea')) continue;
        const text = clean(el.innerText || el.value);
        if (!text || text.length > 120) continue;
        if (seen.has(text)) continue;
        seen.add(text);
        out.push(text);
        if (out.length >= 25) break;
    }
    return out;
}"""


def _red_flagged(s: Session) -> list[str]:
    """Everything on this page the app has flagged red."""
    try:
        return s.page.evaluate(_RED_FIELDS_JS) or []
    except PWError:
        return []


# How long the rating model is given to arrive, and how long its score is given
# to be computed. Both are generous on purpose. This app routes, fetches the
# model's configuration and renders a long form, and a live run judged it at a
# fixed second and a half — which reported "the browser did not go anywhere"
# about a page that was still loading, and threw away the whole screen over it.
_MODEL_WAIT_S = 45
_SCORE_WAIT_S = 40
_POLL_MS = 500

# Is the rating model on screen? Its own controls are the proof, because the
# URL is not always one: a route can resolve to the same path, and a model that
# opened in a new tab leaves the old page's URL exactly as it was.
_MODEL_ON_SCREEN_JS = r"""() => {
    const norm = (s) => (s || '').toLowerCase().replace(/\s+/g, ' ').trim();
    const vis = (el) => {
        const r = el.getBoundingClientRect();
        return r.width > 0 && r.height > 0;
    };
    for (const el of document.querySelectorAll('button, a.btn, input[type=button]')) {
        if (!vis(el)) continue;
        const t = norm(el.innerText || el.value);
        if (t === 'generate score' || t === 'edit') return true;
    }
    const body = norm(document.body.innerText).slice(0, 4000);
    return body.includes('model name') || body.includes('orr model summary')
        || body.includes('calculation sheet');
}"""


def _model_on_screen(s: Session) -> bool:
    try:
        return bool(s.page.evaluate(_MODEL_ON_SCREEN_JS))
    except PWError:
        return False


def _open_pages(s: Session) -> list:
    """Every page of this browser context, as a snapshot to compare against."""
    try:
        return list(s.page.context.pages)
    except PWError:
        return []


def _wait_for_model_page(s: Session, before_url: str, known: list, say) -> str:
    """
    Wait for the rating model to actually arrive, and say how it did.

    Returns a phrase for the report — "opened <url>", "opened in a new tab
    (<url>)", "rendered in place" — or "" if nothing arrived.

    Three ways it can arrive, and a fixed wait catches none of them reliably:

      a NEW TAB      the old page keeps its URL, so comparing URLs reports
                     that nothing happened while the model sits next door.
      a ROUTE        the URL changes, but only once Angular has resolved it
                     and fetched the model — well after the click returns.
      IN PLACE       neither URL changes; the screen is simply replaced.

    So this polls for all three rather than sleeping and guessing, and the
    model's own controls are what settle it. A page that is still loading is
    not a page that went nowhere — reporting it as one is what cost a live run
    its whole Risk Rating screen.

    `known` is the context's pages from BEFORE the button was pressed, and only
    a tab that is not among them counts. Following any other open tab would
    hand the rest of the run whatever happened to be lying around — a download
    window, a tab an earlier screen left behind — and then report on that
    instead of on the model.
    """
    seen = {id(p) for p in known}
    deadline = time.monotonic() + _MODEL_WAIT_S
    while time.monotonic() < deadline:
        # A new tab first: it is the case a URL comparison cannot see.
        fresh = [p for p in s.other_pages() if id(p) not in seen]
        if fresh:
            page = fresh[-1]
            try:
                page.wait_for_load_state("domcontentloaded", timeout=10000)
            except PWError:
                pass
            s.adopt_page(page)
            say("  the model opened in a new tab — following it")
            _settle(s)
            return f"opened in a new tab ({s.page.url})"

        if s.page.url != before_url:
            _settle(s)
            # The URL moved; give the model itself a moment to render into it.
            for _ in range(int(_MODEL_WAIT_S * 1000 / _POLL_MS)):
                if _model_on_screen(s):
                    break
                if time.monotonic() >= deadline:
                    break
                s.page.wait_for_timeout(_POLL_MS)
            return f"opened {s.page.url}"

        if _model_on_screen(s):
            _settle(s)
            return "rendered in place"

        s.page.wait_for_timeout(_POLL_MS)
    return ""


def _settle(s: Session) -> None:
    """Let the app finish whatever it started, and re-stamp the content root."""
    try:
        cr.wait_until_settled(s.page, s.recorder, timeout_ms=20000,
                              stable_polls=2)
        cr.stamp_content_root(s.page)
    except PWError:
        pass


def _wait_for_score(s: Session) -> dict:
    """
    Poll the result block until the model has answered, and return it.

    Generating a score is a server round trip on this screen, so reading the
    block straight after the click reads the empty one that was there before
    it. Stops as soon as any of the result fields carries a value, and gives up
    quietly — an empty block after the full wait is a model that did not score,
    which is the caller's finding to report rather than this function's.
    """
    deadline = time.monotonic() + _SCORE_WAIT_S
    results: dict = {}
    while time.monotonic() < deadline:
        results = _read_rating_results(s)
        if any(v for v in results.values()):
            return results
        s.page.wait_for_timeout(_POLL_MS)
    return results


def _open_model_tab(s: Session, wanted: list[str]) -> str:
    """Switch to one of the model page's own tabs. Returns the label, or ""."""
    for want in wanted:
        try:
            s.open_tab(want)
        except (NavigationError, PWError):
            continue
        s.page.wait_for_timeout(1200)
        return want
    return ""


def _do_risk_rating(s: Session, res: CaseFlowResult, dry_run: bool,
                    say, step) -> None:
    """
    Score the case's risk rating model, and check the rating it produces.

    The one screen here that navigates away from the case menu — Perform Risk
    Rating opens a scoring model of its own — so each hop is checked before the
    next is attempted. Failing to reach the model is a different fault from
    reaching it and having it refuse to score, and a run that conflates them
    sends somebody to the wrong screen.

    Nothing is asserted about WHICH rating comes out. The inputs are this
    deployment's reference data and the model is configuration; what is checked
    is that a rating comes out at all, that the model does not flag its own
    calculation, and that the rating the model produced is the one the case
    then shows. That last part is the round trip, and it is the reason the
    result block is read here rather than later.
    """
    screen = SCREEN_LABEL[RISK_RATING]
    _open_screen(s, screen, step)
    s.page.wait_for_timeout(1500)
    s.screenshot("95-risk-rating")

    # ---- Perform Risk Rating -> the model page --------------------------
    # Both taken BEFORE the click, so what arrived can be told from what was
    # already there — see _wait_for_model_page.
    before_url = s.page.url
    before_pages = _open_pages(s)
    clicked = _click_named_button(s, _PERFORM_RATING)
    if not clicked:
        res.checks.append(R.failed(
            "The risk rating model can be opened",
            expected="a 'Perform Risk Rating' button on the Risk Rating screen",
            actual="none was found. Available: "
                   + (", ".join(_button_labels(s)) or "no buttons"),
            detail="Without it there is no model to score, so nothing on this "
                   "screen could be exercised.",
            evidence=[s.screenshot("95-no-perform-button")], screen=screen))
        return
    how = _wait_for_model_page(s, before_url, before_pages, say)
    step(f"click {clicked}", note=s.page.url)
    # From here on the session may be driving the model's OWN tab, which is a
    # different application on a different host. Every exit below has to hand
    # the case back, or whatever runs next asks the rating model for a screen
    # only RiskNucleus has — see Session.release_page.
    try:
        _rate_the_model(s, res, screen, how, clicked, before_url, dry_run,
                        say, step)
    finally:
        back = s.release_page()
        if back:
            say("  closed the rating model's tab and returned to the case")
            _settle(s)


def _rate_the_model(s: Session, res: CaseFlowResult, screen: str, how: str,
                    clicked: str, before_url: str, dry_run: bool,
                    say, step) -> None:
    """
    Everything done ON the rating model, once it is open.

    Split from _do_risk_rating for one reason: it has six ways out and every
    one of them has to be followed by handing the case back. A `return` here
    is a `finally` there.
    """
    if not how:
        res.checks.append(R.failed(
            "The risk rating model can be opened",
            expected=f"{clicked!r} opens the rating model",
            actual=f"nothing arrived within {_MODEL_WAIT_S} seconds — the page "
                   f"is still {before_url}",
            detail="The button was pressed, the browser did not go anywhere, "
                   "no new tab opened and none of the model's own controls "
                   "appeared, so there was no model to fill.",
            evidence=[s.screenshot("95-model-not-opened")], screen=screen))
        return
    res.checks.append(R.passed(
        "The risk rating model can be opened",
        detail=f"{clicked!r} {how}.", screen=screen))
    say(f"  the model {how}")
    s.screenshot("96-rating-model")

    # ---- Edit ----------------------------------------------------------
    # The model opens read-only. Without this its LOVs are disabled and every
    # one of them would be reported as a field that refused a value.
    if not _click_named_button(s, _RATING_EDIT):
        res.checks.append(R.failed(
            "The rating model can be edited",
            expected="an 'Edit' button on the rating model",
            actual="none was found. Available: "
                   + (", ".join(_button_labels(s)) or "no buttons"),
            detail="The model opens read-only, so without Edit none of its "
                   "inputs can be answered.",
            evidence=[s.screenshot("96-no-edit")], screen=screen))
        return
    # Edit re-renders the form. Waiting for the app to go quiet beats a fixed
    # pause for the same reason the navigation above does.
    _settle(s)
    s.page.wait_for_timeout(1200)
    step("click Edit")
    res.checks.append(R.passed(
        "The rating model can be edited",
        detail="Edit put the model into a state where its inputs accept "
               "values.", screen=screen))

    # ---- the model's inputs --------------------------------------------
    _do_rating_inputs(s, res, screen, say, step)

    if dry_run:
        res.notes.append(R.note(
            "Dry run: the rating model's inputs were answered and then "
            "abandoned. Generate Score and Save were deliberately not "
            "pressed, so no rating was produced or stored.", screen=screen))
        return

    # ---- Generate Score -------------------------------------------------
    if not _click_named_button(s, _GENERATE_SCORE):
        res.checks.append(R.failed(
            "The model produces a rating",
            expected="a 'Generate Score' button on the rating model",
            actual="none was found. Available: "
                   + (", ".join(_button_labels(s)) or "no buttons"),
            evidence=[s.screenshot("97-no-generate")], screen=screen))
        return
    step("click Generate Score")
    # Scoring is a server round trip. Reading the block straight after the
    # click reads the empty one that was on screen before it.
    results = _wait_for_score(s)
    scored_shot = s.screenshot("97-score-generated")
    res.rating_values = results
    res.rating_ref = "; ".join(f"{k} {v}" for k, v in results.items()
                               if v and k in RATING_SHOWN)

    # A rating that is blank is a model that did not score, whatever the
    # absence of an error toast suggests.
    answered = {k: v for k, v in results.items() if v}
    if not answered:
        res.checks.append(R.failed(
            "The model produces a rating",
            expected="Generate Score fills the result block — "
                     + ", ".join(RATING_KEYS),
            actual="every field of it is still blank",
            detail="Generate Score raised no error and produced no rating, so "
                   "there is nothing to save and nothing to compare.",
            evidence=[scored_shot], screen=screen))
    else:
        res.checks.append(R.passed(
            "The model produces a rating",
            detail=f"Generate Score answered {len(answered)} of "
                   f"{len(RATING_RESULTS)} result field(s): "
                   + "; ".join(f"{k} = {v[:40]}" for k, v in answered.items()),
            screen=screen))
        say(f"  the model rated it: {res.rating_ref or '(no grade shown)'}")

    # The two the Rating Summary shows back are the ones the round trip turns
    # on, so a blank one is worth saying now rather than as a puzzling
    # mismatch later.
    empty = [k for k in RATING_KEYS if not results.get(k)]
    if answered and empty:
        res.notes.append(R.note(
            f"The model left {', '.join(empty)} blank, so the Rating Summary "
            f"cannot be checked against "
            + ("them" if len(empty) > 1 else "it") + ".", screen=screen))

    # ---- the Calculation Sheet -----------------------------------------
    _check_calculation_sheet(s, res, screen, say, step)

    # ---- Save -----------------------------------------------------------
    _save_rating(s, res, screen, say, step)


def _do_rating_inputs(s: Session, res: CaseFlowResult, screen: str,
                      say, step) -> None:
    """
    Answer the model's Basic Information LOVs.

    Every one takes the app's own first option. Nothing is authored: these are
    lists of this deployment's reference data, and a named value would break
    the moment the environment is re-seeded — see the note at the top of the
    Risk Rating section.

    Scoped to the Basic Information section rather than filled across the page,
    because the model page also carries the computed financial figures above it
    and the result block below. Typing into either would be typing into the
    model's own answer.
    """
    scope, heading = _section_scope(s, _RATING_INPUT_SECTION)
    if not scope:
        # Not fatal. The model may name its input section differently here, and
        # a rating can still be generated from whatever the app defaults to.
        res.notes.append(R.note(
            "No 'Basic Information' section was found on the rating model, so "
            "its inputs were left at whatever the app had them at. The model "
            "was still scored and checked.", screen=screen))
        say("  no Basic Information section on the model — leaving its inputs "
            "alone")
        return

    # cell_labels because this model is a legacy .aspx page, not one of the
    # Angular screens: its inputs are laid out as a two-column table — the name
    # in the left cell, the dropdown in the right — and carry no <label> at
    # all. Read as labels alone the section looks empty, and a screen whose
    # every input is answerable gets reported as offering none.
    f = W.Filler(session=s, screen=screen, group="Risk Rating — Basic Information",
                 scope_selector=scope, cell_labels=True)
    labels = f.field_labels(60)
    if not labels:
        res.checks.append(R.failed(
            "The rating model offers its inputs",
            expected=f"answerable fields under {heading!r}",
            actual="the section is on the page and shows no enterable field",
            detail="Edit was pressed, so they should be answerable by now.",
            evidence=[s.screenshot("96-no-inputs")], screen=screen))
        return

    say(f"  {heading}: {len(labels)} input(s) to answer")
    filled, refused = _fill_discovered(f, res, set(), say, cap=60,
                                       value_for=_rating_input_value)
    res.entries.extend(e.as_dict() for e in f.entries)
    s.screenshot("96-inputs-filled")

    if filled:
        res.checks.append(R.passed(
            "The rating model offers its inputs",
            detail=f"{filled} of {len(labels)} field(s) under {heading!r} were "
                   f"answered, each with the app's own first option.",
            screen=screen))
        step(f"fill {heading}", note=f"{filled} input(s)")
    else:
        res.checks.append(R.failed(
            "The rating model offers its inputs",
            expected=f"the {len(labels)} field(s) under {heading!r} accept "
                     f"values",
            actual="not one of them would take one",
            detail="Edit was pressed first, so these should have been "
                   "answerable. " + (" | ".join(refused[:4]) if refused else ""),
            evidence=[s.screenshot("96-inputs-refused")], screen=screen))

    if refused and filled:
        res.checks.append(R.failed(
            "Every input on the rating model accepts a value",
            expected=f"all {len(labels)} field(s) under {heading!r} take a value",
            actual=f"{len(refused)} did not: " + " | ".join(refused[:6]),
            screen=screen))


def _rating_input_value(label: str, kind: str, marker: str) -> Optional[str]:
    """
    What to answer one of the model's inputs with.

    None for anything with a list behind it, which is what widgets reads as
    "take the app's own first option" — and every input on this model is one.
    Anything else falls back to the ordinary discovery value, so a build that
    puts a number or a date among them is still answered rather than skipped.
    """
    if kind in ("dropdown", "lookup", "select", "switch"):
        return None
    return _auto_value(label, kind, marker)


def _check_calculation_sheet(s: Session, res: CaseFlowResult, screen: str,
                             say, step) -> None:
    """
    Read the Calculation Sheet and report anything the model flagged red.

    Red on this tab is the model objecting to its own arithmetic — a figure it
    could not compute, or one outside the range the grade depends on — and the
    instruction for this screen is exactly that: a field flagged red means
    there is an error, and no red means it is fine.

    A tab that will not open is reported as its own finding rather than as a
    clean sheet. "Nothing was flagged" and "nothing was looked at" are the same
    sentence in a report that does not separate them, and the second one is the
    dangerous half.
    """
    opened = _open_model_tab(s, _TAB_CALCULATION)
    if not opened:
        res.checks.append(R.failed(
            "The Calculation Sheet is free of flagged values",
            expected="the model's 'Calculation Sheet' tab opens",
            actual="it could not be opened, so nothing on it was read",
            detail="Whether the model flagged anything is unknown — this is "
                   "not a clean sheet.",
            evidence=[s.screenshot("98-no-calculation-sheet")], screen=screen))
        return
    s.page.wait_for_timeout(1500)
    step(f"open {opened}")
    shot = s.screenshot("98-calculation-sheet")

    flagged = _red_flagged(s)
    if flagged:
        res.checks.append(R.failed(
            "The Calculation Sheet is free of flagged values",
            expected="no figure on the Calculation Sheet flagged red",
            actual=f"{len(flagged)} flagged: " + " | ".join(flagged[:8]),
            detail="A value flagged red on this tab is the model reporting an "
                   "error in its own calculation, so the rating it produced "
                   "rests on it.",
            evidence=[shot], screen=screen))
        say(f"  !! {len(flagged)} value(s) flagged red on the Calculation "
            f"Sheet")
    else:
        res.checks.append(R.passed(
            "The Calculation Sheet is free of flagged values",
            detail="Nothing on the Calculation Sheet is flagged red, so the "
                   "model reports no error in the figures the rating was "
                   "computed from.", screen=screen))
        say("  the Calculation Sheet flags nothing")

    # Back to the summary, so Save is pressed on the tab that owns it.
    _open_model_tab(s, _TAB_MODEL)
    s.page.wait_for_timeout(1000)


def _save_rating(s: Session, res: CaseFlowResult, screen: str,
                 say, step) -> None:
    """Commit the rating, and read the app's own answer to the save."""
    f = W.Filler(session=s, screen=screen, group="Risk Rating")
    try:
        f.commit("Save")
    except (W.FillError, W.WriteRefused, PWError) as exc:
        res.checks.append(R.failed(
            "The rating is saved",
            expected="a Save button on the rating model",
            actual=str(exc).splitlines()[0][:250],
            detail="The rating was generated and there is no way to commit "
                   "it, so nothing reached the case.",
            evidence=[s.screenshot("99-no-save")], screen=screen))
        return
    step("save the risk rating")
    s.page.wait_for_timeout(2000)

    msgs = f.messages()
    refused = flows._refused_rules(s)
    shot = s.screenshot("99-rating-saved")
    if msgs["bad"] or refused:
        res.checks.append(R.failed(
            "The rating is saved",
            expected="the app confirms the rating was saved",
            actual=" | ".join((msgs["bad"] + refused)[:3]),
            detail="The application refused the save, so the rating is not on "
                   "the case.",
            evidence=[shot], screen=screen))
        for r in refused:
            say(f"    !! refused: {r}")
        return
    res.checks.append(R.passed(
        "The rating is saved",
        detail="Save raised no error"
               + (f". {' | '.join(msgs['ok'][:2])}" if msgs["ok"] else ".")
               + " The Rating Summary below is what says it was stored.",
        screen=screen))


# --------------------------------------------------------------------------
# Observations
# --------------------------------------------------------------------------

def _do_observations(s: Session, res: CaseFlowResult, dry_run: bool,
                     say, step) -> None:
    screen = SCREEN_LABEL[OBSERVATIONS]
    _open_screen(s, screen, step)
    s.screenshot("30-observations")

    before = _observation_count(s)
    if not _open_add_observation(s, res, say, step):
        return
    s.screenshot("31-add-observation")

    for spec in OBSERVATION_PASSES:
        say(f"  {spec.name} …")
        entries = _fill_pass(s, res, screen, spec, dry_run, say, step)
        res.entries.extend(e.as_dict() for e in entries)

    if dry_run:
        return

    # The list gaining an entry is the honest test of whether Save worked, and
    # a better one than the absence of an error toast — this panel closes on
    # save whether or not it stored anything.
    s.page.wait_for_timeout(1200)
    after = _observation_count(s)
    if after > before:
        res.checks.append(R.passed(
            "The observation appears in the list",
            detail=f"The list went from {before} to {after} entr(ies).",
            screen=screen))
    else:
        res.checks.append(R.failed(
            "The observation appears in the list",
            expected=f"one more entry than the {before} before saving",
            actual=f"still {after} entr(ies)",
            detail="Save raised no error but the list did not grow, so nothing "
                   "was stored.",
            evidence=[s.screenshot("32-observation-not-listed")], screen=screen))


def _open_add_observation(s: Session, res: CaseFlowResult, say, step) -> bool:
    """
    Open the Add Observation panel.

    This is NOT an add-row '+': the button says "Add Observation" and what it
    opens slides in down the right of the screen rather than as a modal. So the
    button is found the same way the facility's is — by label, through the
    crawler's own denylist — and the panel is confirmed by its fields
    appearing, not by a `.modal.show` that never arrives.
    """
    screen = SCREEN_LABEL[OBSERVATIONS]
    btns = cr.collect_action_buttons(s.page)
    opener = None
    for want in _ADD_OBSERVATION:
        opener = next((b for b in btns
                       if want in (b["label"] or "").strip().lower()), None)
        if opener is not None:
            break
    if opener is None:
        res.checks.append(R.failed(
            "An observation can be added",
            expected="an 'Add Observation' button on the Observations screen",
            actual="none was found. Available: "
                   + (", ".join(b["label"] for b in btns[:8]) or "no buttons"),
            detail="Without it there is no form to fill, so nothing on this "
                   "screen could be exercised.",
            evidence=[s.screenshot("31-no-add-observation")], screen=screen))
        return False

    if not cr._click_stamped(s.page, "data-crawl-action", opener["index"],
                             timeout=8000):
        res.checks.append(R.failed(
            "An observation can be added",
            expected=f"the '{opener['label']}' button opens the observation form",
            actual="the button would not click",
            evidence=[s.screenshot("31-add-observation-stuck")], screen=screen))
        return False
    cr.wait_until_settled(s.page, s.recorder, timeout_ms=20000, stable_polls=2)
    cr.stamp_content_root(s.page)
    s.page.wait_for_timeout(1200)
    step(f"click {opener['label']}")

    f = W.Filler(session=s, screen=screen)
    labels = f.field_labels(40)
    if not labels:
        res.checks.append(R.failed(
            "An observation can be added",
            expected="the Add Observation panel shows its fields",
            actual="no labelled field appeared after clicking "
                   f"{opener['label']!r}",
            evidence=[s.screenshot("31-observation-panel-empty")],
            screen=screen))
        return False
    say(f"  the panel shows {len(labels)} field(s)")
    res.checks.append(R.passed(
        "An observation can be added",
        detail=f"{opener['label']!r} opened a panel with {len(labels)} "
               f"field(s): " + ", ".join(labels[:8]), screen=screen))
    return True


def _observation_count(s: Session) -> int:
    """
    How many observations the middle list is showing.

    Not a <table>, so the grid row counter does not see it: each saved
    observation renders as an item carrying a "COB<n>" reference, and counting
    those references is both simpler and more specific than counting rows.
    """
    try:
        return int(s.page.evaluate(
            """() => {
                const root = document.querySelector('[data-crawl-root]') || document.body;
                const text = root.innerText || '';
                const hits = text.match(/\\bCOB\\d+\\b/g) || [];
                return new Set(hits).size;
            }"""))
    except Exception:  # noqa: BLE001
        return 0


def _open_observation(s: Session, needle: str) -> bool:
    """
    Open one saved observation from the list, by text it carries.

    The list is not a grid, so collect_row_openers — which walks <table> rows —
    finds nothing here. Clicking the item by its own text is read-only
    navigation and is how a person would do it.
    """
    if not needle:
        return False
    root = s.page.locator("[data-crawl-root]")
    if not root.count():
        root = s.page.locator("body")
    try:
        item = root.get_by_text(needle, exact=False).first
        if not item.count():
            return False
        item.click(timeout=6000)
    except PWError:
        return False
    try:
        cr.wait_until_settled(s.page, s.recorder, timeout_ms=15000,
                              stable_polls=2)
        cr.stamp_content_root(s.page)
    except PWError:
        pass
    s.page.wait_for_timeout(900)
    return True


# --------------------------------------------------------------------------
# Policies & Exceptions
# --------------------------------------------------------------------------

# The middle column is the same list component the Documents checklist and the
# Conditions register use — each row a <p class="todo-title"> carrying its
# badges — so it is counted the same way, and for the same reason: it reads
# like a grid and is not one, and the <table> row counter therefore returns 0
# on a list plainly full of rows.
_POLICY_ROWS_JS = r"""() => {
    const root = document.querySelector('[data-crawl-root]') || document.body;
    const vis = (el) => el.offsetParent !== null
                     || el.getClientRects().length > 0;
    const titles = [...root.querySelectorAll('p.todo-title')].filter(vis);
    if (!titles.length) return -1;      // not this component: cannot count
    // Rows, not titles. Deduplicating by the row container means a row that
    // renders its badges as separate <p>s still counts once.
    const rows = new Set();
    for (const p of titles) rows.add(p.closest('.todo-item, li, tr') || p);
    return rows.size;
}"""


def _exception_rows(s: Session) -> int:
    """
    How many rows the policy list is showing.

    -1 means "could not be counted", which is a different answer from 0 and
    has to stay that way: this count is only compared either side of a save,
    and an unreadable list reported as empty would turn into "nothing was
    stored" on a screen that stored perfectly well.
    """
    try:
        return int(s.page.evaluate(_POLICY_ROWS_JS))
    except PWError:
        return -1


def _is_exceptions_filter(label: str) -> bool:
    """
    Is this the 'Exceptions' filter, and nothing else on the screen?

    Matched exactly rather than by containment, which is the whole point of
    having this as its own function. Three controls on this screen have
    'exception' in their name and only one of them is a filter: 'Add
    Exception' opens the form — clicking that here would leave the list
    unfiltered and the count taken against the policy library — and 'Policy
    Exception Requirement' in the filter column narrows by policy TYPE, which
    is a different question and would still be counting library rows.

    The button carries a warning icon, but the icon is a font glyph with no
    text of its own, so the label the crawler reads is just the word.
    """
    return _norm(label) in ("exceptions", "exception")


def _show_exceptions_only(s: Session) -> str:
    """
    Narrow the middle list to the exceptions raised against this case.

    Read-only — a filter button — and it is what makes reading the list mean
    anything. Unfiltered, that column is the POLICY LIBRARY: every policy the
    deployment defines, sitting there before this run does anything. Filtered
    to Exceptions it is a register of what has actually been raised against
    this case, which is what the marker is then looked for in.

    Returns the label it clicked, or "" when the screen has no such filter —
    which the caller reports rather than treating as a failure.
    """
    try:
        btns = cr.collect_action_buttons(s.page)
    except PWError:
        return ""
    want = next((b for b in btns if _is_exceptions_filter(b["label"] or "")),
                None)
    if want is None:
        return ""
    if not cr._click_stamped(s.page, "data-crawl-action", want["index"],
                             timeout=6000):
        return ""
    try:
        cr.wait_until_settled(s.page, s.recorder, timeout_ms=15000,
                              stable_polls=2)
        cr.stamp_content_root(s.page)
    except PWError:
        pass
    s.page.wait_for_timeout(1200)
    return want["label"]


def _exception_anchor(s: Session) -> str:
    """Whichever of the form's own fields is on screen, or ""."""
    return next((a for a in _EXCEPTION_ANCHORS if _anchor_on_screen(s, a)), "")


def _wait_for_exception_form(s: Session, timeout_s: int = 15) -> str:
    """Poll until one of the form's own fields is in the viewport."""
    deadline = time.monotonic() + timeout_s
    while True:
        anchor = _exception_anchor(s)
        if anchor:
            return anchor
        if time.monotonic() >= deadline:
            return ""
        s.page.wait_for_timeout(600)


def _do_policies(s: Session, res: CaseFlowResult, dry_run: bool,
                 say, step) -> None:
    """
    Raise a policy exception against the case: fill the form, Save.

    The list is then re-read and searched for this run's MARKER, which is a
    better test of whether Save stored anything than the absence of an error
    toast — and a better one than counting rows, which is what the equivalent
    check on Conditions does. Two things about this screen make the count
    untrustworthy on its own: unfiltered, the middle column is the policy
    LIBRARY and does not grow when an exception is raised, and closing the
    form re-renders that column, so a filter applied before the save cannot be
    assumed to still be applied after it. A count that grew is reported as
    corroboration; the marker is what decides.
    """
    screen = SCREEN_LABEL[POLICIES]
    _open_screen(s, screen, step)
    s.page.wait_for_timeout(1200)
    s.screenshot("70-policies")

    filtered = _show_exceptions_only(s)
    before = _exception_rows(s) if filtered else -1
    if filtered:
        say(f"  filtered to {filtered!r}: {max(before, 0)} exception(s) already "
            f"on the case")
        s.screenshot("70-policies-exceptions")
    else:
        say("  the screen has no Exceptions filter, so the list cannot be "
            "counted either side of the save")

    anchor = _open_add_exception(s, res, say, step)
    if not anchor:
        return
    s.screenshot("71-add-exception")

    for spec in POLICY_PASSES:
        say(f"  {spec.name} …")
        entries = _fill_pass(s, res, screen, spec, dry_run, say, step)
        res.entries.extend(e.as_dict() for e in entries)
        # The title this run gave its exception. Recorded so the report names
        # the record, and so a later re-verification has something to look for
        # without re-deriving it from the entry list.
        for e in entries:
            if res.marker in str(e.value):
                res.exception_ref = str(e.value)
                break

    # The form has to go before the list behind it can be re-read, and nothing
    # after this can be clicked while it is up — on a dry run too, where the
    # screens after this one still have to be reachable. What it was holding is
    # already recorded, and the screenshots above it were taken while it was
    # still filled in.
    if not _close_side_panel(s, anchor):
        res.notes.append(R.observation(
            "The Add Exception form closes again",
            f"It stayed on screen after every close control on it was "
            f"clicked, so the list behind it could not be re-read to confirm "
            f"the exception was stored. {anchor!r} is still in the viewport.",
            evidence=[s.screenshot("72-form-stuck")], screen=screen))
        return
    res.checks.append(R.passed(
        "The Add Exception form closes again",
        detail="Closed cleanly, leaving the list readable.", screen=screen))

    if dry_run:
        return

    s.page.wait_for_timeout(1500)
    # Re-applied rather than assumed: closing the form re-renders the middle
    # column, and a count taken against the policy library instead of the
    # exceptions on this case would be a bigger number than `before` for
    # reasons that have nothing to do with the save.
    again = _show_exceptions_only(s)
    after = _exception_rows(s) if again else -1
    grew = ("" if before < 0 or after < 0 else
            f" The list went from {before} to {after} exception(s).")
    listed = flows._appears_in(res.marker, flows._screen_text(s))
    shot = s.screenshot("72-policies-saved")

    if listed:
        res.checks.append(R.passed(
            "The exception appears in the list",
            detail=f"A row carries this run's marker ({res.marker}), so the "
                   f"exception was stored and not one left by an earlier "
                   f"run.{grew}", screen=screen))
    elif after > before >= 0:
        res.notes.append(R.observation(
            "The exception appears in the list",
            f"The list gained a row, so something was stored, but no row "
            f"shows this run's marker ({res.marker}) — so it cannot be "
            f"confirmed from here that the row is the exception this run "
            f"raised. This list may not render the Title. The round trip "
            f"below opens the row and reads it.{grew}",
            evidence=[shot], screen=screen))
    else:
        res.checks.append(R.failed(
            "The exception appears in the list",
            expected=f"a row carrying this run's marker ({res.marker})",
            actual=("no row does" if again else
                    "no row does, and the screen showed no Exceptions filter "
                    "to narrow the list to what has been raised on this case"),
            detail="Save raised no error but the exception is not in the list "
                   "afterwards, so nothing was stored." + grew,
            evidence=[shot], screen=screen))


def _open_add_exception(s: Session, res: CaseFlowResult, say, step) -> str:
    """
    Open the Add Exception form, and confirm it actually arrived.

    Returns the anchor field that proved it opened — which is what closes it
    again afterwards — or "" when it did not.

    Same shape as Add Condition, and confirmed the same way. "Some labelled
    field appeared" is not proof of arrival on this screen: the filter column
    down the left carries labelled controls of its own, so that test would
    pass on a screen where nothing opened. A field only the form has is what
    answers it.
    """
    screen = SCREEN_LABEL[POLICIES]
    btns = cr.collect_action_buttons(s.page)
    opener = None
    for want in _ADD_EXCEPTION:
        opener = next((b for b in btns
                       if want in (b["label"] or "").strip().lower()), None)
        if opener is not None:
            break
    if opener is None:
        res.checks.append(R.failed(
            "An exception can be added",
            expected="an 'Add Exception' button on the Policies & Exceptions "
                     "screen",
            actual="none was found. Available: "
                   + (", ".join(b["label"] for b in btns[:8]) or "no buttons"),
            detail="Without it there is no form to fill, so nothing on this "
                   "screen could be exercised.",
            evidence=[s.screenshot("71-no-add-exception")], screen=screen))
        return ""

    if not cr._click_stamped(s.page, "data-crawl-action", opener["index"],
                             timeout=8000):
        res.checks.append(R.failed(
            "An exception can be added",
            expected=f"the '{opener['label']}' button opens the exception form",
            actual="the button would not click",
            evidence=[s.screenshot("71-add-exception-stuck")], screen=screen))
        return ""
    cr.wait_until_settled(s.page, s.recorder, timeout_ms=20000, stable_polls=2)
    cr.stamp_content_root(s.page)
    step(f"click {opener['label']}")

    anchor = _wait_for_exception_form(s)
    if not anchor:
        res.checks.append(R.failed(
            "An exception can be added",
            expected=f"'{opener['label']}' opens a form showing "
                     + " or ".join(repr(a) for a in _EXCEPTION_ANCHORS),
            actual="that form did not appear within fifteen seconds",
            detail="The button was clicked and the form never arrived, so "
                   "none of its fields could be filled.",
            evidence=[s.screenshot("71-exception-form-empty")], screen=screen))
        return ""

    f = W.Filler(session=s, screen=screen)
    labels = f.field_labels(40)
    say(f"  the form shows {len(labels)} field(s)")
    res.checks.append(R.passed(
        "An exception can be added",
        detail=f"{opener['label']!r} opened a form with {len(labels)} "
               f"field(s), {anchor!r} among them: " + ", ".join(labels[:8]),
        screen=screen))
    return anchor


# Everything that could carry the click handler for the row holding a given
# text, stamped innermost-first and handed back with the coordinates to click.
#
# A single guess at the handler is not good enough on this row, and a live run
# proved it: the exception saved, the register showed it, and the round trip
# reported all eleven values as unverifiable because the click went to the
# wrong element. `p.closest('.todo-item, li, tr')` is what the Conditions
# register needs; this list has no `.todo-item`, so that resolved to the bare
# <p>, and clicking a <p> here does nothing at all.
#
# So the candidates are enumerated rather than guessed, and the caller clicks
# them in turn until the form actually arrives. Innermost first because an
# anchor or a cursor-pointer span inside the row is the most likely handler,
# and the row container the least specific thing that still works.
#
# The climb outwards stops as soon as a container holds substantially more text
# than the row itself: past that point the candidate is the whole list or the
# whole screen, and clicking that would hit the filter column instead.
_EXCEPTION_ROW_JS = r"""([needle, budget]) => {
    const root = document.querySelector('[data-crawl-root]') || document.body;
    const vis = (el) => el.offsetParent !== null
                     || el.getClientRects().length > 0;
    const norm = (s) => (s || '').toLowerCase().replace(/\s+/g, ' ').trim();
    const want = norm(needle);
    if (!want) return [];

    document.querySelectorAll('[data-exc-click]').forEach(
        e => e.removeAttribute('data-exc-click'));

    // The DEEPEST element still holding the whole marker: the row's own text.
    let hit = null;
    for (const el of root.querySelectorAll('*')) {
        if (!vis(el)) continue;
        if (!norm(el.innerText).includes(want)) continue;
        hit = el;                      // later matches are deeper
    }
    if (!hit) return [];
    hit.scrollIntoView({block: 'center'});

    const own = norm(hit.innerText).length;
    const cands = [];
    const push = (el) => {
        if (el && vis(el) && !cands.includes(el)) cands.push(el);
    };

    // Inside the row: an explicit control, if it has one.
    for (const el of hit.querySelectorAll(
            'a, button, [class*="cursor-pointer"], i[class*="ft-"], '
            + 'i[class*="fa-"], p.todo-title'))
        push(el);
    push(hit);
    // Outwards, while the candidate is still about this row alone.
    let node = hit.parentElement;
    for (let i = 0; i < 6 && node && node !== document.body;
         i++, node = node.parentElement) {
        if (norm(node.innerText).length > own + budget) break;
        push(node);
    }

    return cands.map((el, i) => {
        el.setAttribute('data-exc-click', String(i));
        const r = el.getBoundingClientRect();
        return {
            idx: i,
            x: Math.round(r.left + r.width / 2),
            y: Math.round(r.top + r.height / 2),
            what: el.tagName.toLowerCase()
                  + (el.className ? '.' + String(el.className).trim()
                                            .split(/\s+/)[0] : ''),
            inView: r.width > 0 && r.height > 0 && r.top >= 0
                    && r.bottom <= innerHeight,
        };
    });
}"""


def _exception_row_targets(s: Session, needle: str,
                           budget: int = 120) -> list[dict]:
    """Click targets for the list row carrying `needle`, innermost first."""
    try:
        return s.page.evaluate(_EXCEPTION_ROW_JS, [needle, budget]) or []
    except PWError:
        return []


def _showing_our_exception(s: Session,
                           timeout_s: int = 6) -> tuple[str, str]:
    """
    Is the exception this run raised on screen, and under what title?

    Returns (anchor, title). Two independent signals, because either one alone
    has a way of being wrong here:

      the anchor      one of the form's own mandatory fields is in the
                      viewport, which says A form is open — but not which
                      record it holds.
      the title       a field labelled Title reading back a value, which says
                      WHICH record it holds. It cannot be satisfied by the
                      list behind the form: the list renders its rows as plain
                      text with no labels, so there is no 'Title' field there
                      for value_of to find.

    Polled rather than read once. The form is fetched when the row is clicked,
    and asking immediately after the click reports "nothing opened" on a form
    that was still on its way — which is exactly how the click that DID work
    would get discarded in favour of trying the next candidate.
    """
    deadline = time.monotonic() + timeout_s
    anchor, shown = "", ""
    while True:
        anchor = anchor or _exception_anchor(s)
        if anchor and not shown:
            try:
                shown = W.Filler(session=s,
                                 screen=SCREEN_LABEL[POLICIES]).value_of(
                                     "Title")
            except Exception:  # noqa: BLE001 - a control that will not read
                shown = ""
        if (anchor and shown) or time.monotonic() >= deadline:
            return anchor, shown
        s.page.wait_for_timeout(600)


def _open_our_exception(s: Session, res: CaseFlowResult, say,
                        cap: int = 25) -> str:
    """
    Open the exception THIS run raised, and return the title it opened.

    "" when it could not be opened. Opening the first row regardless would be
    worse than failing: this list holds every exception raised against the
    case, and comparing this run's values against somebody else's reports
    every one of them as lost.

    The row is found by the run's MARKER rather than walked by position, which
    is the opposite of how a condition is found and is safe for the reason the
    prefix match on Documents is not: the marker is the whole timestamped
    phrase, unique to this run down to the second, so a match is this run's
    row and not an earlier run's. It also means the report can say which row
    was clicked and what happened when it was.

    Presence of the marker in the row is not itself proof the form opened —
    the list stays on screen behind it — so arrival is confirmed separately.
    See _showing_our_exception for what each signal does and does not prove.
    """
    marker = flows._norm_value(res.marker)
    available = _exception_rows(s)
    say(f"  the list holds {max(available, 0)} row(s); looking for this run's "
        f"exception by its marker")

    targets = _exception_row_targets(s, res.marker)
    if not targets:
        say(f"  no row in the list carries {res.marker}")
        return ""
    say(f"  found the row; {len(targets)} way(s) to open it")

    for spot in targets[:cap]:
        if not _click_row_spot(s, spot):
            continue
        anchor, shown = _showing_our_exception(s)

        if marker and marker in flows._norm_value(shown):
            say(f"  opened by clicking {spot['what']} — the form's Title "
                f"carries the marker")
            return shown

        if not anchor:
            continue                    # nothing opened; try the next target

        if shown:
            # A form arrived holding a DIFFERENT record. Closed and moved on
            # rather than compared against: this run's values measured against
            # somebody else's exception is a page of false failures.
            say(f"  {spot['what']} opened {shown[:40]!r}, which is not ours")
            _close_side_panel(s, anchor)
            continue

        # A form arrived and has no readable Title to identify it by — some
        # builds re-open a saved exception as read-only text rather than as
        # labelled inputs. Accepted, because the element that was clicked is
        # itself the row carrying this run's marker: ownership rests on WHAT
        # WAS CLICKED rather than on what the form reads back, and the caller
        # is told which of the two it got.
        say(f"  {spot['what']} opened a form with no readable Title — taking "
            f"it as ours because that row carries the marker")
        return res.exception_ref or "the exception this run added"
    return ""


def _click_row_spot(s: Session, spot: dict) -> bool:
    """
    Click one candidate, as a real mouse click where the browser agrees one
    would land there.

    A real click at a point rather than a locator click, for the reason
    _close_side_panel gives: on several of this template's controls a
    scripted .click() does nothing at all. The JS click is kept as the
    fallback for a candidate that is scrolled out of the viewport, where a
    mouse click has no coordinates to use.
    """
    try:
        if spot.get("inView"):
            s.page.mouse.click(spot["x"], spot["y"])
        else:
            # Addressed by the stamp the enumeration left on it, not by a
            # re-derived selector: the row's classes are not unique on this
            # screen, and re-matching them would click a different row.
            s.page.evaluate(
                """(idx) => {
                    const el = document.querySelector(
                        '[data-exc-click="' + idx + '"]');
                    if (!el) return false;
                    el.scrollIntoView({block: 'center'});
                    el.click();
                    return true;
                }""", spot["idx"])
    except PWError:
        return False
    return _settle_after_click(s)


# --------------------------------------------------------------------------
# Documents
# --------------------------------------------------------------------------

def _do_documents(s: Session, res: CaseFlowResult, dry_run: bool,
                  say, step) -> None:
    """
    Download what is there, add a document, then action one of the existing
    ones.

    Three independent jobs, run in that order and each allowed to fail on its
    own. Downloading first is deliberate: it is read-only, so it reports on the
    case as it was found rather than on a case this run has already added a
    document to.
    """
    screen = SCREEN_LABEL[DOCUMENTS]
    _open_screen(s, screen, step)
    s.page.wait_for_timeout(1200)
    s.screenshot("50-documents")

    listed = _document_titles(s)
    say(f"  the checklist shows {len(listed)} document(s)")
    if listed:
        res.checks.append(R.passed(
            "The Documents screen lists the case's documents",
            detail=f"{len(listed)} document(s) on the checklist: "
                   + "; ".join(t[:50] for t in listed[:5]), screen=screen))
    else:
        # An empty checklist is this case having no documents, not the screen
        # failing to show them, so it is written down rather than asserted on.
        res.notes.append(R.observation(
            "The Documents screen lists the case's documents",
            "The screen opened but no document rows were found on it, so "
            "there was nothing to download and nothing to action.",
            evidence=[s.screenshot("50-documents-empty")], screen=screen))

    _download_attachments(s, res, say, step)
    _add_additional_document(s, res, dry_run, say, step)
    _action_one_document(s, res, dry_run, say, step)


# A document row on this screen is a <p class="todo-title"> holding two badges
# and the document's name — the app renders the checklist with the same "todo
# list" component it uses elsewhere. Anchoring on that class rather than on "an
# element whose text contains PRE-APPROVAL" matters: every container up to the
# page body contains that text too, and the loose version reported the toolbar
# and the filter sidebar as documents, then tried to open one of them.
#
# "elsewhere" includes CONDITIONS, which matters more than it sounds. That
# screen looks like a grid — it has a search box above it and says "No Data
# Found" when it is empty — and it is not one: a saved condition is a
# p.todo-title exactly like a document, carrying its reference and type as
# badges. Counting it with the <table> row counter therefore read 0 both
# before and after a save that had plainly worked, and reported a stored
# condition as "nothing was stored". See _condition_rows.
_DOC_ROW = "p.todo-title"

_DOC_TITLES_JS = r"""(cap) => {
    const root = document.querySelector('[data-crawl-root]') || document.body;
    const vis = (el) => el.offsetParent !== null
                     || el.getClientRects().length > 0;
    const out = [];
    for (const p of root.querySelectorAll('p.todo-title')) {
        if (!vis(p)) continue;
        // The badges are their own elements inside the row, so the name is
        // what is left once they are taken out — cleaner than stripping the
        // words off the front of the combined text, which would also eat a
        // document legitimately called "Other …".
        let t = '';
        for (const n of p.childNodes) {
            if (n.nodeType === 3) { t += n.textContent; continue; }
            if (n.nodeType === 1 && !/badge/.test(String(n.className)))
                t += n.textContent;
        }
        t = t.replace(/\s+/g, ' ').trim();
        if (!t) t = (p.innerText || '').replace(/\s+/g, ' ').trim();
        if (t && !out.includes(t)) out.push(t);
        if (out.length >= cap) break;
    }
    return out;
}"""


# High enough that the checklist is never truncated. It matters: the count is
# compared before and against after to decide whether the upload stored, and a
# cap the list already exceeds would read the same both times and call a
# working upload a failure.
_DOC_CAP = 2000


def _document_titles(s: Session, cap: int = _DOC_CAP) -> list[str]:
    """
    The documents the middle list is showing, by their titles.

    Not a <table>, so the grid row counter sees nothing here. Each row carries
    stage and type badges — PRE-APPROVAL, OTHER — followed by the document's
    name, and it is the name that identifies it. The badges are dropped so the
    title that comes back is the one a person would read off the screen.
    """
    try:
        return s.page.evaluate(_DOC_TITLES_JS, cap) or []
    except PWError:
        return []


def _download_attachments(s: Session, res: CaseFlowResult, say, step) -> None:
    """
    Press Download Attachments and keep whatever comes back.

    READ ONLY — this runs on a dry run too, because it changes nothing.

    A case that has no attachments yet is the ordinary state of a fresh record,
    and the button then does nothing at all: no download, no error, sometimes a
    toast. That is reported as "could not check" rather than as a failure,
    because a button that correctly declines to download nothing is not a
    defect. What IS reported as a failure is the button being missing.
    """
    screen = SCREEN_LABEL[DOCUMENTS]
    btns = cr.collect_action_buttons(s.page)
    opener = None
    for want in _DOWNLOAD_ATTACHMENTS:
        opener = next((b for b in btns
                       if want in (b["label"] or "").strip().lower()), None)
        if opener is not None:
            break
    if opener is None:
        res.checks.append(R.failed(
            "The case's attachments can be downloaded",
            expected="a 'Download Attachments' button on the Documents screen",
            actual="none was found. Available: "
                   + (", ".join(b["label"] for b in btns[:8]) or "no buttons"),
            evidence=[s.screenshot("51-no-download-button")], screen=screen))
        return

    # The button does not download anything itself: it opens a "Document
    # Attachments" MODAL listing what the case holds, with Select All, Cancel
    # and Download. Getting this wrong was expensive — the first version
    # treated the button as the download, waited 25 seconds for a file that was
    # never coming, and left the modal standing. Its backdrop then swallowed
    # every click for the rest of the screen, so five fields two passes later
    # failed with "Locator.click: Timeout" and nothing said why.
    cr._click_stamped(s.page, "data-crawl-action", opener["index"], timeout=8000)
    s.page.wait_for_timeout(1500)
    step(f"click {opener['label']}")
    shot = s.screenshot("51-attachments-dialog")

    modal = s.page.locator(".modal.show").last
    if not modal.count():
        res.checks.append(R.failed(
            "The case's attachments can be downloaded",
            expected=f"{opener['label']!r} opens the Document Attachments list",
            actual="no dialog opened",
            evidence=[shot], screen=screen))
        return

    offered = _attachment_rows(s)
    say(f"  the attachments dialog lists {offered} attachment(s)")

    into = os.path.join(res.artifacts_dir, "downloads")
    got: list[str] = []
    if offered:
        os.makedirs(into, exist_ok=True)
        _tick_select_all(s)
        try:
            # 30 seconds. Long enough for the server to build a zip of a
            # handful of files, short enough that a Download which is simply
            # not going to produce anything does not cost every run a minute.
            with s.page.expect_download(timeout=30000) as caught:
                modal.locator('button:has-text("Download")').first.click(
                    timeout=8000)
            dl = caught.value
            name = dl.suggested_filename or "attachments"
            target = os.path.join(into, name)
            dl.save_as(target)
            got.append(target)
            say(f"  downloaded {name} ({os.path.getsize(target)} bytes)")
        except Exception as exc:  # noqa: BLE001
            say(f"  the download did not complete: {str(exc)[:120]}")
            res.checks.append(R.failed(
                "The case's attachments can be downloaded",
                expected=f"Download produces a file for the {offered} "
                         f"attachment(s) listed",
                actual=str(exc)[:200],
                evidence=[s.screenshot("51-download-failed")], screen=screen))

    res.downloaded = got
    if got:
        sizes = ", ".join(f"{os.path.basename(p)} "
                          f"({os.path.getsize(p)} bytes)" for p in got)
        res.checks.append(R.passed(
            "The case's attachments can be downloaded",
            detail=f"The dialog listed {offered} attachment(s); Download "
                   f"produced {sizes}, saved beside this run's screenshots.",
            screen=screen))
    elif not offered:
        res.notes.append(R.observation(
            "The case's attachments can be downloaded",
            f"{opener['label']!r} opened the Document Attachments dialog and "
            f"it reported no attachments on this case. That is the screen "
            f"behaving correctly on a case nothing has been attached to yet, "
            f"so it is not a failure — but the download itself has not been "
            f"exercised. Run this again after a document has been uploaded.",
            evidence=[shot], screen=screen))

    # Whatever happened above, this dialog must not be left standing.
    if not _close_modal_hard(s):
        res.notes.append(R.observation(
            "The attachments dialog closes again",
            "The Document Attachments dialog would not close. Everything below "
            "is on the screen behind it, and a dialog's backdrop swallows every "
            "click — so the rest of this screen could not be exercised.",
            evidence=[s.screenshot("51-dialog-stuck")], screen=screen))
    else:
        res.checks.append(R.passed(
            "The attachments dialog closes again",
            detail="Cancelled cleanly, leaving the Documents screen usable.",
            screen=screen))


def _attachment_rows(s: Session) -> int:
    """
    How many attachments the Document Attachments dialog is offering.

    Two things in this table are not attachments and both were counted as one
    at first. An empty list renders as a row reading "No Attachments Found!",
    and this dialog puts its HEADER row — Select All / Title / File Name /
    Description — inside the <tbody> rather than a <thead>. Either one makes an
    empty dialog look like it holds a single attachment, and the run then
    pressed Download and waited a minute for a file that was never coming.

    So: a row counts only if it has <td> cells of its own and does not read
    like a "nothing here" message.
    """
    try:
        return int(s.page.evaluate(
            r"""() => {
                const m = [...document.querySelectorAll('.modal.show')].pop();
                if (!m) return 0;
                const EMPTY = /no attachment|no record|no data|not found/i;
                const HEADER = /^\s*select all/i;
                let n = 0;
                for (const tr of m.querySelectorAll('tbody tr')) {
                    if (tr.querySelector('th')) continue;
                    if (!tr.querySelector('td')) continue;
                    const t = (tr.innerText || '').trim();
                    if (!t || EMPTY.test(t) || HEADER.test(t)) continue;
                    n++;
                }
                return n;
            }"""))
    except PWError:
        return 0


def _tick_select_all(s: Session) -> bool:
    """Tick the dialog's Select All box, so Download has something to send."""
    try:
        return bool(s.page.evaluate(
            """() => {
                const m = [...document.querySelectorAll('.modal.show')].pop();
                if (!m) return false;
                for (const box of m.querySelectorAll('input[type=checkbox]')) {
                    if (box.checked) continue;
                    const lab = box.closest('label')
                        || m.querySelector('label[for="' + box.id + '"]');
                    (lab || box).click();
                    return true;
                }
                return false;
            }"""))
    except PWError:
        return False


def _close_modal_hard(s: Session, tries: int = 3) -> bool:
    """
    Close every open dialog, and confirm it actually went.

    Cancel first, then the ×, then Escape — in that order because Cancel is the
    control the app itself expects, and only fall back to the blunter ones.
    Returns whether the page is clear afterwards; the caller has to know,
    because a dialog left standing makes everything after it fail for a reason
    that has nothing to do with what it was testing.
    """
    for _ in range(tries):
        if not s.page.locator(".modal.show, .modal.in").count():
            return True
        try:
            s.page.evaluate(
                """() => {
                    const m = [...document.querySelectorAll('.modal.show')].pop();
                    if (!m) return;
                    // Cancel by name first — it is the control the app itself
                    // expects, and it cannot be confused with Download.
                    for (const b of m.querySelectorAll('button, a.btn')) {
                        const t = (b.innerText || '').trim().toLowerCase();
                        if (/^(cancel|close|no|back)$/.test(t)) {
                            b.click();
                            return;
                        }
                    }
                    for (const x of m.querySelectorAll(
                            'button.close, .close, [aria-label="Close"], '
                            + 'i.fa-times, i.ft-x')) {
                        (x.closest('button, a') || x).click();
                        return;
                    }
                }""")
        except PWError:
            pass
        s.page.wait_for_timeout(700)
        if not s.page.locator(".modal.show, .modal.in").count():
            return True
        try:
            s.page.keyboard.press("Escape")
        except PWError:
            pass
        s.page.wait_for_timeout(700)
    try:
        cr._dismiss_overlays(s.page)
    except Exception:  # noqa: BLE001
        pass
    s.page.wait_for_timeout(500)
    return not s.page.locator(".modal.show, .modal.in").count()


def _add_additional_document(s: Session, res: CaseFlowResult, dry_run: bool,
                             say, step) -> None:
    """Add a document that is not on the standard checklist, with a file."""
    screen = SCREEN_LABEL[DOCUMENTS]
    before = len(_document_titles(s))

    if not _open_named_panel(s, res, _ADD_DOCUMENT, "Additional Documents",
                             "A document can be added", "52", say, step,
                             anchor=_UPLOAD_ANCHOR, foreign=_ACTION_ANCHOR):
        return
    scope = _panel_scope(s, _UPLOAD_ANCHOR, _ACTION_ANCHOR)
    s.screenshot("52-additional-document")

    say(f"  {ADDITIONAL_DOCUMENT_PASS.name} …")
    entries = _fill_pass(s, res, screen, ADDITIONAL_DOCUMENT_PASS, dry_run,
                         say, step, scope=scope)
    res.entries.extend(e.as_dict() for e in entries)

    if not dry_run:
        s.page.wait_for_timeout(1800)

    # The next pass cannot start until this panel is gone, so a panel that
    # will not close is reported here rather than as a handful of unexplained
    # click timeouts further down.
    if not _close_side_panel(s, _UPLOAD_ANCHOR):
        res.notes.append(R.observation(
            "The Upload Other Document panel closes again",
            "It stayed on screen after every close control on it was clicked. "
            "It sits over the Document Action panel, so nothing below this "
            "could be exercised.",
            evidence=[s.screenshot("52-panel-stuck")], screen=screen))
    else:
        res.checks.append(R.passed(
            "The Upload Other Document panel closes again",
            detail="Closed cleanly, freeing the slot the Document Action panel "
                   "uses.", screen=screen))

    if dry_run:
        return

    after = len(_document_titles(s))
    if after > before:
        res.checks.append(R.passed(
            "The added document appears on the checklist",
            detail=f"The list went from {before} to {after} document(s).",
            screen=screen))
    else:
        res.checks.append(R.failed(
            "The added document appears on the checklist",
            expected=f"one more document than the {before} before saving",
            actual=f"still {after}",
            detail="Upload raised no error but the checklist did not grow, so "
                   "nothing was stored.",
            evidence=[s.screenshot("52-document-not-listed")], screen=screen))


def _action_one_document(s: Session, res: CaseFlowResult, dry_run: bool,
                         say, step) -> None:
    """
    Open one checklist document, attach a file to it, edit its fields, Save.

    WHICH document is recorded on the result, because the round trip has to
    re-open the same one. "Whichever is first" is not good enough there: adding
    a document a moment ago may well have changed what first means.
    """
    screen = SCREEN_LABEL[DOCUMENTS]
    titles = _document_titles(s)
    if not titles:
        res.notes.append(R.observation(
            "A document can be opened and actioned",
            "The checklist showed no documents, so there was none to open.",
            evidence=[s.screenshot("53-no-documents")], screen=screen))
        return

    # The first checklist entry, which on this screen is a standard
    # pre-approval document rather than anything this run created.
    want = titles[0]
    if not _open_document(s, want):
        res.checks.append(R.failed(
            "A document can be opened and actioned",
            expected=f"clicking {want[:60]!r} opens the Document Action panel",
            actual="no row on the checklist matched that title",
            evidence=[s.screenshot("53-document-would-not-open")],
            screen=screen))
        return

    scope = _wait_for_panel(s, _ACTION_ANCHOR, _UPLOAD_ANCHOR)
    if not scope:
        res.checks.append(R.failed(
            "A document can be opened and actioned",
            expected="the Document Action panel appears, showing Date of "
                     "Action",
            actual=f"it did not, within 15 seconds of clicking {want[:50]!r}",
            detail="The row was clicked and the panel never arrived, so none "
                   "of its fields could be filled.",
            evidence=[s.screenshot("53-panel-empty")], screen=screen))
        return

    res.document_ref = want
    step("open a document", note=want[:120])
    f = W.Filler(session=s, screen=screen, scope_selector=scope)
    labels = f.field_labels(40)
    say(f"  opened {want[:70]!r} — the panel shows {len(labels)} field(s)")
    res.checks.append(R.passed(
        "A document can be opened and actioned",
        detail=f"{want[:80]!r} opened the Document Action panel, showing "
               f"{len(labels)} field(s): " + ", ".join(labels[:8]),
        screen=screen))
    s.screenshot("53-document-action")

    say(f"  {DOCUMENT_ACTION_PASS.name} …")
    entries = _fill_pass(s, res, screen, DOCUMENT_ACTION_PASS, dry_run,
                         say, step, scope=scope)
    res.entries.extend(e.as_dict() for e in entries)


def _open_named_panel(s: Session, res: CaseFlowResult, wanted: list[str],
                      what: str, check_name: str, shot_prefix: str,
                      say, step, anchor: str = "", foreign: str = "") -> bool:
    """
    Click a button by label and confirm the panel it opens actually arrived.

    What these buttons open slides in down the right rather than arriving as a
    `.modal.show`, so there is no modal selector to wait on — the same shape as
    Add Observation. But "some labelled field appeared" is not proof here the
    way it is there: BOTH of this screen's panels are in the DOM at all times,
    so that test passes whichever one is showing, and it passes when neither
    is. The proof is a field only the wanted panel has.
    """
    screen = SCREEN_LABEL[DOCUMENTS]
    btns = cr.collect_action_buttons(s.page)
    opener = None
    for want in wanted:
        opener = next((b for b in btns
                       if want in (b["label"] or "").strip().lower()), None)
        if opener is not None:
            break
    if opener is None:
        res.checks.append(R.failed(
            check_name,
            expected=f"an '{what}' button on the Documents screen",
            actual="none was found. Available: "
                   + (", ".join(b["label"] for b in btns[:8]) or "no buttons"),
            detail="Without it there is no form to fill, so this part of the "
                   "screen could not be exercised.",
            evidence=[s.screenshot(f"{shot_prefix}-no-{_safe(what)}")],
            screen=screen))
        return False

    if not cr._click_stamped(s.page, "data-crawl-action", opener["index"],
                             timeout=8000):
        res.checks.append(R.failed(
            check_name,
            expected=f"the '{opener['label']}' button opens a form",
            actual="the button would not click",
            evidence=[s.screenshot(f"{shot_prefix}-stuck")], screen=screen))
        return False
    cr.wait_until_settled(s.page, s.recorder, timeout_ms=20000, stable_polls=2)
    cr.stamp_content_root(s.page)
    step(f"click {opener['label']}")

    scope = _wait_for_panel(s, anchor, foreign) if anchor else ""
    if anchor and not scope:
        res.checks.append(R.failed(
            check_name,
            expected=f"'{opener['label']}' opens a panel showing {anchor!r}",
            actual="that panel did not appear within 15 seconds",
            evidence=[s.screenshot(f"{shot_prefix}-empty")], screen=screen))
        return False

    f = W.Filler(session=s, screen=screen, scope_selector=scope)
    labels = f.field_labels(40)
    say(f"  the form shows {len(labels)} field(s)")
    res.checks.append(R.passed(
        check_name,
        detail=f"{opener['label']!r} opened a form with {len(labels)} "
               f"field(s): " + ", ".join(labels[:8]), screen=screen))
    return True


def _open_document(s: Session, title: str) -> bool:
    """
    Open one document from the checklist, by its title.

    The list is not a grid, so collect_row_openers — which walks <table> rows —
    finds nothing here, exactly as on Observations. The row is matched on the
    title it carries and the click goes to its `.todo-item` ancestor, which is
    what the app binds the handler to; clicking the <p> itself does nothing.

    Matching is on a normalised PREFIX. Several of these titles run to a full
    sentence with brackets and slashes that the DOM breaks across elements, so
    an exact match on the whole string finds nothing on rows that are plainly
    there.
    """
    if not title:
        return False
    try:
        return bool(s.page.evaluate(
            r"""(want) => {
                const root = document.querySelector('[data-crawl-root]')
                          || document.body;
                const vis = (el) => el.offsetParent !== null
                                 || el.getClientRects().length > 0;
                const norm = (t) => (t || '').toLowerCase()
                    .replace(/[^a-z0-9]+/g, ' ').trim();
                const w = norm(want).slice(0, 40);
                if (!w) return false;
                for (const p of root.querySelectorAll('p.todo-title')) {
                    if (!vis(p)) continue;
                    if (!norm(p.innerText || p.textContent).includes(w))
                        continue;
                    const row = p.closest('.todo-item, li, tr') || p;
                    row.click();
                    return true;
                }
                return false;
            }""", title)) and _settle_after_click(s)
    except PWError:
        return False


def _settle_after_click(s: Session) -> bool:
    try:
        cr.wait_until_settled(s.page, s.recorder, timeout_ms=15000,
                              stable_polls=2)
        cr.stamp_content_root(s.page)
    except PWError:
        pass
    s.page.wait_for_timeout(1500)
    return True


# Which panel is which, told apart by a field only that panel has.
#
# Both slide-in panels on this screen are in the DOM at the same time and both
# have a field called Title, so "the first Title on the page" is whichever the
# app happened to render first — not the one that is open. Anchoring on a label
# unique to each panel is what makes them addressable at all.
_UPLOAD_ANCHOR = "Upload File"        # only the Upload Other Document panel
_ACTION_ANCHOR = "Date of Action"     # only the Document Action panel

# Which panel each pass's values are read back from, and what tells that panel
# apart from the other one. Not the same anchor the FILL used: "Upload File" is
# on the panel that adds a document, but the panel that re-opens an added one
# is an edit form with Title and Comments and no upload box at all.
_VERIFY_ANCHORS = {
    "Documents — Additional Documents": ("Comments", _ACTION_ANCHOR),
    "Documents — Document Action": (_ACTION_ANCHOR, "Comments"),
}

# Fields whose value does NOT stay in the box that took it.
#
# Saving files the attachment against the document and clears all three boxes,
# so reading them back off the panel finds them empty and reports three stored
# values as lost. They are read from the document's attachments list instead —
# see _open_attachments.
_ATTACHMENT_FIELDS = {"attachment title", "attachment description",
                      "attachment", "upload file"}


def _is_attachment_field(entry: dict) -> bool:
    return (_norm(entry.get("label", "")) in _ATTACHMENT_FIELDS
            or entry.get("kind") == "file")

# "Is this panel open?" cannot be answered by asking the DOM whether its fields
# are visible. Both panels keep their fields rendered at all times — the closed
# one is slid out of the window rather than hidden — so offsetParent and
# getClientRects say yes to both, and a run that trusted them scoped itself to
# a panel sitting off the right-hand edge, filled it happily, and then timed
# out clicking fields the OTHER panel was covering.
#
# Being inside the viewport is the test that actually distinguishes them.
_ON_SCREEN_JS = r"""(label) => {
    const norm = (s) => (s || '').toLowerCase().replace(/&/g, ' and ')
        .replace(/[^a-z0-9]+/g, ' ').trim();
    const want = norm(label);
    for (const l of document.querySelectorAll(
            'label, .control-label, .form-label')) {
        if (norm(l.getAttribute('title') || l.textContent) !== want) continue;
        const r = l.getBoundingClientRect();
        if (r.width > 0 && r.height > 0 && r.right > 0 && r.bottom > 0
            && r.left < innerWidth && r.top < innerHeight) return true;
    }
    return false;
}"""

_PANEL_SCOPE_JS = r"""([anchor, foreign]) => {
    const onScreen = (el) => {
        const r = el.getBoundingClientRect();
        return r.width > 0 && r.height > 0 && r.right > 0 && r.bottom > 0
            && r.left < innerWidth && r.top < innerHeight;
    };
    const norm = (s) => (s || '').toLowerCase().replace(/&/g, ' and ')
        .replace(/[^a-z0-9]+/g, ' ').trim();
    const want = norm(anchor), other = norm(foreign);
    document.querySelectorAll('[data-fill-scope]').forEach(
        e => e.removeAttribute('data-fill-scope'));

    const holds = (el, text) => {
        for (const l of el.querySelectorAll('label, .control-label, .form-label'))
            if (norm(l.getAttribute('title') || l.textContent) === text)
                return true;
        return false;
    };

    let label = null;
    for (const l of document.querySelectorAll(
            'label, .control-label, .form-label')) {
        if (norm(l.getAttribute('title') || l.textContent) !== want) continue;
        if (!onScreen(l)) continue;     // slid out of the window: not open
        label = l;
        break;
    }
    if (!label) return false;

    // Grow outwards from the anchor's own field for as long as the container
    // still belongs to this panel alone. The step BEFORE it would swallow the
    // other panel's fields, so the last safe one is the panel.
    let best = null;
    let node = label.parentElement;
    for (let i = 0; i < 12 && node && node !== document.body;
         i++, node = node.parentElement) {
        if (other && holds(node, other)) break;
        best = node;
    }
    if (!best) return false;
    best.setAttribute('data-fill-scope', '1');
    return true;
}"""


def _panel_scope(s: Session, anchor: str, foreign: str) -> str:
    """
    Mark the slide-in panel that holds `anchor` and return a selector for it.

    Returns "" when the panel is not on screen, which the caller should read as
    "that panel is not open" rather than pressing on against the whole page —
    filling the page here means filling the panel next to the one that was
    asked for.
    """
    try:
        if s.page.evaluate(_PANEL_SCOPE_JS, [anchor, foreign]):
            return "[data-fill-scope]"
    except PWError:
        pass
    return ""


def _wait_for_panel(s: Session, anchor: str, foreign: str,
                    timeout_s: int = 15) -> str:
    """Poll until the panel carrying `anchor` is on screen, then scope to it."""
    deadline = time.monotonic() + timeout_s
    while True:
        scope = _panel_scope(s, anchor, foreign)
        if scope:
            return scope
        if time.monotonic() >= deadline:
            return ""
        s.page.wait_for_timeout(600)


def _anchor_on_screen(s: Session, label: str) -> bool:
    """Is the field named by `label` inside the viewport right now?"""
    if not label:
        return False
    try:
        return bool(s.page.evaluate(_ON_SCREEN_JS, label))
    except PWError:
        return False


# Close controls a real click would actually land on.
#
# Every part of this is load-bearing. The screen renders several `<i class=
# "ft-x">` icons — one per panel, plus the case sidebar's — and they are
# indistinguishable in the markup: no title, no aria-label, no id on the icon.
# So `locator('i.ft-x').first` resolves to whichever comes first in the DOM,
# which is not the one on screen, and Playwright then times out waiting for an
# element nobody can click.
#
# Two rules fix it. Only consider a control inside the viewport, and only one
# that elementFromPoint agrees is on top at its own centre — that is exactly
# the question "would a click land here", asked of the browser rather than
# guessed. The coordinates come back so the click can be a real mouse click at
# a point: a JS .click() on these does nothing at all, and a locator click
# re-resolves the selector and picks the wrong element again.
_CLOSE_CONTROLS_JS = r"""(scope) => {
    const root = (scope && document.querySelector(scope)) || document;
    const sel = 'button.close, .close, [aria-label="Close"], i.ft-x, '
              + 'i.fa-times, i.la-times, i.feather-x, span.sidebar-close-icon';
    const out = [];
    for (const el of root.querySelectorAll(sel)) {
        const r = el.getBoundingClientRect();
        if (r.width <= 0 || r.height <= 0) continue;
        const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
        if (cx < 0 || cy < 0 || cx > innerWidth || cy > innerHeight) continue;
        const top = document.elementFromPoint(cx, cy);
        if (!top || !(top === el || el.contains(top) || top.contains(el)))
            continue;
        out.push({x: Math.round(cx), y: Math.round(cy),
                  what: el.tagName + '.'
                        + String(el.className || '').slice(0, 40)});
    }
    return out;
}"""


def _close_side_panel(s: Session, anchor: str, tries: int = 4) -> bool:
    """
    Close the slide-in panel identified by `anchor`, and confirm it went.

    Getting this right matters more than it looks. Both of this screen's panels
    stay in the DOM once opened, so a panel left open still hands out its
    fields to anything that goes looking — the next pass scopes itself to the
    panel underneath, fills it, and then times out clicking through the one on
    top. Five unexplained "Locator.click: Timeout" failures, two passes after
    the actual mistake.

    Each candidate close control is clicked as a real mouse click at a point
    the browser confirms it occupies, and the panel is then re-checked. Trying
    the panel's own container first keeps the case sidebar's identical × out of
    the way; the whole page is only searched if that finds nothing.
    """
    for _ in range(tries):
        if not _anchor_on_screen(s, anchor):
            return True
        scope = _panel_scope(s, anchor, "")
        spots: list[dict] = []
        for where in (scope, ""):
            try:
                spots = s.page.evaluate(_CLOSE_CONTROLS_JS, where or None) or []
            except PWError:
                spots = []
            if spots:
                break
        if not spots:
            return False
        for spot in spots:
            try:
                s.page.mouse.click(spot["x"], spot["y"])
            except PWError:
                continue
            s.page.wait_for_timeout(900)
            if not _anchor_on_screen(s, anchor):
                return True
    return not _anchor_on_screen(s, anchor)


# The paperclip on an open document's panel, which is where a saved attachment
# actually lives.
#
# Excludes `extractIcon`, the little clip drawn INSIDE the Attachment field for
# picking a file, and anything on the left — the checklist rows carry their own
# clip and clicking one of those opens a different document.
_PAPERCLIP_JS = r"""() => {
    const onScreen = (el) => {
        const r = el.getBoundingClientRect();
        return r.width > 0 && r.height > 0 && r.right > 0 && r.bottom > 0
            && r.left < innerWidth && r.top < innerHeight;
    };
    const out = [];
    for (const el of document.querySelectorAll('i, span, a')) {
        const c = String(el.className || '');
        if (!/ft-paperclip|fa-paperclip/.test(c)) continue;
        if (/extractIcon/.test(c)) continue;
        if (!onScreen(el)) continue;
        const r = el.getBoundingClientRect();
        if (r.left < innerWidth * 0.60) continue;
        out.push({
            x: Math.round(r.left + r.width / 2),
            y: Math.round(r.top + r.height / 2),
            count: ((el.parentElement || el).innerText || '')
                .replace(/\s+/g, '').trim(),
        });
    }
    out.sort((a, b) => a.y - b.y);
    return out;
}"""


def _open_attachments(s: Session, stem: str = "") -> tuple[str, str, int]:
    """
    Open the attachments list of the document whose panel is showing.

    Returns (what it says, screenshot path, how many attachments).

    This exists because a saved attachment does NOT stay in the fields that
    uploaded it. Attachment Title, Attachment Description and Attachment are
    the boxes for adding one; on Save the app files the attachment against the
    document and clears all three. Reading them back off the panel therefore
    finds three empty inputs, and the round trip reported three values as lost
    on a document that was holding every one of them — which is exactly what
    it did, and exactly what a reader who clicks the paperclip can see is
    wrong.

    The paperclip on the panel header opens a DOCUMENTS dialog listing Title,
    File Name, Description and who uploaded it. That is the view a person
    checks, so it is the view this checks.
    """
    try:
        clips = s.page.evaluate(_PAPERCLIP_JS) or []
    except PWError:
        clips = []
    if not clips:
        return "", "", 0

    try:
        s.page.mouse.click(clips[0]["x"], clips[0]["y"])
    except PWError:
        return "", "", 0
    try:
        s.page.wait_for_selector(".modal.show", timeout=10000)
    except PWError:
        return "", "", 0
    s.page.wait_for_timeout(1200)

    # Named after the pass. Both documents' attachment lists are captured in
    # one run, and a fixed name meant the second overwrote the first — leaving
    # two findings pointing at one picture, of the wrong document.
    shot = s.screenshot(f"verify-attachments-{stem or 'documents'}")
    try:
        text = s.page.evaluate(
            """() => {
                const m = [...document.querySelectorAll('.modal.show')].pop();
                return m ? (m.innerText || '').replace(/\\s+/g, ' ') : '';
            }""") or ""
    except PWError:
        text = ""
    rows = _attachment_rows(s)
    return text, shot, rows


def _close_open_panels(s: Session) -> None:
    """Close whichever slide-in panel is showing, whichever one it is."""
    for anchor in (_UPLOAD_ANCHOR, "Comments", _ACTION_ANCHOR):
        if _anchor_on_screen(s, anchor):
            _close_side_panel(s, anchor)


def _panel_is_showing(s: Session, want: str, scope: str) -> str:
    """
    The document the open panel is actually about.

    Returns its Title, or "" when that does not look like `want`. This guard is
    the difference between a report and a rumour. Both panels stay in the DOM
    and only one can be on top, so a panel that failed to close leaves the
    NEXT document's comparison reading the previous document's boxes — every
    value mismatched, every one of them reported against the wrong record. The
    panel's own Title is read-only and filled by the app, so it is a reliable
    statement of which document is on screen.

    Matched by containment because the checklist row carries a type badge and
    an attachment count that the Title field does not.
    """
    shown = W.Filler(session=s, screen=SCREEN_LABEL[DOCUMENTS],
                     scope_selector=scope).value_of("Title")
    a, b = flows._norm_value(shown), flows._norm_value(want)
    if a and b and (a in b or b in a):
        return shown
    return ""


# --------------------------------------------------------------------------
# Conditions
# --------------------------------------------------------------------------

def _condition_rows(s: Session) -> int:
    """
    How many conditions the register is showing.

    NOT flows._grid_rows. The register reads like a grid and is not one: each
    saved condition is a <p class="todo-title"> carrying its reference and
    type as badges, the same component the Documents checklist uses, and
    "No Data Found" is that list's empty message rather than an empty table.
    The <table> row counter therefore returned 0 before AND after a save that
    had worked, and the run reported a condition it had just stored — visible
    on the screen, in a screenshot attached to the very same finding — as
    "nothing was stored".

    Rows are counted, not deduplicated by title: the count exists to be
    compared either side of a save, and two conditions that happen to share a
    title must still count as two or the comparison says nothing.
    """
    try:
        return int(s.page.evaluate(
            """() => {
                const root = document.querySelector('[data-crawl-root]')
                          || document.body;
                const vis = (el) => el.offsetParent !== null
                                 || el.getClientRects().length > 0;
                let n = 0;
                for (const p of root.querySelectorAll('p.todo-title'))
                    if (vis(p)) n++;
                return n;
            }"""))
    except PWError:
        return -1


def _open_condition_row(s: Session, index: int) -> bool:
    """
    Open one condition from the register, by position.

    By position and not by title, which is the opposite of how a document is
    opened, and deliberately. A condition's title is the run's own marker
    sentence, and _open_document matches on a normalised 40-character PREFIX —
    long enough for the documents, far too short here: two runs on the same
    day give "…autotest 0908 1…" at that length whether the run was at 16:53
    or 17:00, so the prefix would open the earlier run's condition and compare
    this run's values against it. Most of them would match, because the same
    code entered both. A silent pass against the wrong record is worse than
    any failure.

    So rows are opened one at a time and the PANEL's Title is checked against
    the full marker instead — see _open_our_condition.

    The click goes to the row's `.todo-item` ancestor, which is what the app
    binds the handler to; clicking the <p> itself does nothing.
    """
    try:
        return bool(s.page.evaluate(
            r"""(want) => {
                const root = document.querySelector('[data-crawl-root]')
                          || document.body;
                const vis = (el) => el.offsetParent !== null
                                 || el.getClientRects().length > 0;
                let n = 0;
                for (const p of root.querySelectorAll('p.todo-title')) {
                    if (!vis(p)) continue;
                    if (n++ !== want) continue;
                    const row = p.closest('.todo-item, li, tr') || p;
                    row.click();
                    return true;
                }
                return false;
            }""", index)) and _settle_after_click(s)
    except PWError:
        return False


def _do_conditions(s: Session, res: CaseFlowResult, dry_run: bool,
                   say, step) -> None:
    """
    Raise a condition against the case: fill the panel, attach a file, Save.

    The register's row count is read before and after, because that is the
    honest test of whether Save stored anything and a better one than the
    absence of an error toast — this panel closes on save whether or not the
    condition was written.
    """
    screen = SCREEN_LABEL[CONDITIONS]
    _open_screen(s, screen, step)
    s.page.wait_for_timeout(1200)
    s.screenshot("60-conditions")

    before = _condition_rows(s)
    say(f"  the register holds {max(before, 0)} condition(s)")

    if not _open_add_condition(s, res, say, step):
        return
    s.screenshot("61-add-condition")

    for spec in CONDITION_PASSES:
        say(f"  {spec.name} …")
        entries = _fill_pass(s, res, screen, spec, dry_run, say, step)
        res.entries.extend(e.as_dict() for e in entries)
        # The title this run gave its condition. Recorded so the report names
        # the record, and so a later re-verification has something to look for
        # without re-deriving it from the entry list.
        for e in entries:
            if res.marker in str(e.value):
                res.condition_ref = str(e.value)
                break

    # The panel has to go before the register behind it can be re-read, and
    # nothing after this can be clicked while it is up.
    if not _close_side_panel(s, _CONDITION_ANCHOR):
        res.notes.append(R.observation(
            "The Add Condition panel closes again",
            "It stayed on screen after every close control on it was clicked, "
            "so the register behind it could not be re-read to confirm the "
            "condition was stored.",
            evidence=[s.screenshot("62-panel-stuck")], screen=screen))
        return
    res.checks.append(R.passed(
        "The Add Condition panel closes again",
        detail="Closed cleanly, leaving the register readable.", screen=screen))

    if dry_run:
        return

    s.page.wait_for_timeout(1500)
    after = _condition_rows(s)
    if before < 0 or after < 0:
        res.notes.append(R.observation(
            "The condition appears in the register",
            "The register's rows could not be counted either side of the "
            "save, so whether the condition was stored cannot be told from "
            "the grid. The round trip below is the real answer.",
            evidence=[s.screenshot("62-register-unreadable")], screen=screen))
    elif after > before:
        res.checks.append(R.passed(
            "The condition appears in the register",
            detail=f"The register went from {before} to {after} condition(s).",
            screen=screen))
    else:
        res.checks.append(R.failed(
            "The condition appears in the register",
            expected=f"one more row than the {before} before saving",
            actual=f"still {after} row(s)",
            detail="Save raised no error but the register did not grow, so "
                   "nothing was stored.",
            evidence=[s.screenshot("62-condition-not-listed")], screen=screen))


def _open_add_condition(s: Session, res: CaseFlowResult, say, step) -> bool:
    """
    Open the Add Condition panel, and confirm it actually arrived.

    Same shape as Add Observation — a button whose panel slides in down the
    right rather than arriving as a `.modal.show` — so the button is found by
    label through the crawler's own denylist. But "some labelled field
    appeared" is not proof of arrival here the way it is there: the filter
    sidebar down the left of this screen carries labelled controls of its own,
    so that test would pass on a screen where nothing opened. A field only the
    panel has is what answers it.
    """
    screen = SCREEN_LABEL[CONDITIONS]
    btns = cr.collect_action_buttons(s.page)
    opener = None
    for want in _ADD_CONDITION:
        opener = next((b for b in btns
                       if want in (b["label"] or "").strip().lower()), None)
        if opener is not None:
            break
    if opener is None:
        res.checks.append(R.failed(
            "A condition can be added",
            expected="an 'Add Condition' button on the Conditions screen",
            actual="none was found. Available: "
                   + (", ".join(b["label"] for b in btns[:8]) or "no buttons"),
            detail="Without it there is no form to fill, so nothing on this "
                   "screen could be exercised.",
            evidence=[s.screenshot("61-no-add-condition")], screen=screen))
        return False

    if not cr._click_stamped(s.page, "data-crawl-action", opener["index"],
                             timeout=8000):
        res.checks.append(R.failed(
            "A condition can be added",
            expected=f"the '{opener['label']}' button opens the condition form",
            actual="the button would not click",
            evidence=[s.screenshot("61-add-condition-stuck")], screen=screen))
        return False
    cr.wait_until_settled(s.page, s.recorder, timeout_ms=20000, stable_polls=2)
    cr.stamp_content_root(s.page)
    step(f"click {opener['label']}")

    if not _wait_for_anchor(s, _CONDITION_ANCHOR):
        res.checks.append(R.failed(
            "A condition can be added",
            expected=f"'{opener['label']}' opens a panel showing "
                     f"{_CONDITION_ANCHOR!r}",
            actual="that panel did not appear within fifteen seconds",
            detail="The button was clicked and the panel never arrived, so "
                   "none of its fields could be filled.",
            evidence=[s.screenshot("61-condition-panel-empty")],
            screen=screen))
        return False

    f = W.Filler(session=s, screen=screen)
    labels = f.field_labels(40)
    say(f"  the panel shows {len(labels)} field(s)")
    res.checks.append(R.passed(
        "A condition can be added",
        detail=f"{opener['label']!r} opened a panel with {len(labels)} "
               f"field(s): " + ", ".join(labels[:8]), screen=screen))
    return True


def _wait_for_anchor(s: Session, label: str, timeout_s: int = 15) -> bool:
    """
    Poll until the field named by `label` is inside the viewport.

    Not _wait_for_panel, which wants a SECOND label to know where one panel
    ends and the next begins. That is the right question on Documents, where
    two panels sit in the DOM together; this screen has one, so there is no
    second label to give it — and asked to scope with none it walks twelve
    ancestors up and hands back most of the page. All that is wanted here is
    whether the panel arrived, which its own anchor answers on its own.
    """
    deadline = time.monotonic() + timeout_s
    while True:
        if _anchor_on_screen(s, label):
            return True
        if time.monotonic() >= deadline:
            return False
        s.page.wait_for_timeout(600)


def _first_paperclip(s: Session) -> Optional[dict]:
    """The topmost paperclip on the panel down the right of the screen."""
    try:
        clips = s.page.evaluate(_PAPERCLIP_JS) or []
    except PWError:
        clips = []
    return clips[0] if clips else None


# Every control on the Add Condition panel that could be the clip opening its
# attachments dialog, most likely first.
#
# The Documents paperclip finder came back EMPTY here, on a panel that plainly
# draws one: it matches ft-paperclip / fa-paperclip and deliberately excludes
# extractIcon, and this clip is either none of those or exactly the excluded
# one. So this asks a broader question and hands back what it found, rather
# than a narrower one that answers "nothing" with nothing to act on.
#
# The search is confined to the Title field's own row, which is where the
# screen draws the clip and its count — "Title* [clip] 0". Two rules keep that
# safe. Anything reading like a close, clear or delete control is refused:
# clicking one of those on a half-filled panel would throw the form away. And
# a candidate must carry no words of its own, because an icon does not — the
# attachment count beside the clip is digits, which is why a bare number is
# allowed through.
_CONDITION_CLIP_JS = r"""(labelText) => {
    const cls = (el) => (el.getAttribute && el.getAttribute('class')) || '';
    const norm = (s) => (s || '').toLowerCase().replace(/&/g, ' and ')
        .replace(/[^a-z0-9]+/g, ' ').trim();
    const onScreen = (el) => {
        const r = el.getBoundingClientRect();
        return r.width > 0 && r.height > 0 && r.right > 0 && r.bottom > 0
            && r.left < innerWidth && r.top < innerHeight;
    };
    const want = norm(labelText);
    let label = null;
    for (const l of document.querySelectorAll(
            'label, .control-label, .form-label')) {
        if (norm(l.textContent) !== want) continue;
        if (!onScreen(l)) continue;
        label = l;
        break;
    }
    if (!label) return [];

    const CLIPPY = /paperclip|attach|clip|upload|file|extracticon/i;
    const NEVER = /times|close|remove|trash|delete|cancel|cross/i;
    const out = [];
    const consider = (el, rank) => {
        if (!onScreen(el)) return;
        const c = cls(el) + ' ' + (el.id || '');
        if (NEVER.test(c)) return;
        const text = (el.textContent || '').replace(/\s+/g, ' ').trim();
        if (text && !/^\d+$/.test(text)) return;
        const r = el.getBoundingClientRect();
        out.push({x: Math.round(r.left + r.width / 2),
                  y: Math.round(r.top + r.height / 2),
                  what: el.tagName.toLowerCase()
                        + (cls(el) ? '.' + cls(el).slice(0, 48) : ''),
                  count: text});
        out[out.length - 1].rank = CLIPPY.test(c) ? rank : rank + 10;
    };

    // The label's own row first, then one container out: the clip is drawn on
    // the label line, and which side of the <label> boundary it falls on is a
    // detail of how the panel happens to be built.
    for (const el of label.querySelectorAll('i, span, a, button, svg, img'))
        consider(el, 0);
    const row = label.parentElement;
    if (row)
        for (const el of row.querySelectorAll('i, span, a, button, svg, img'))
            if (!label.contains(el)) consider(el, 1);

    out.sort((a, b) => a.rank - b.rank || a.x - b.x);
    // The same clip is usually an <i> inside an <a>, at the same centre.
    const seen = new Set(), keep = [];
    for (const o of out) {
        const k = o.x + ':' + o.y;
        if (seen.has(k)) continue;
        seen.add(k);
        keep.push(o);
    }
    return keep.slice(0, 6);
}"""


def _condition_clip_candidates(s: Session, cap: int = 4) -> list[dict]:
    """
    What to click to open a condition's attachments dialog, in order.

    The specific finder first — on a build that does use ft-paperclip it is
    the better answer — then whatever the Title row is showing.
    """
    out: list[dict] = []
    clip = _first_paperclip(s)
    if clip is not None:
        out.append({"x": clip["x"], "y": clip["y"],
                    "what": "the panel's ft-paperclip",
                    "count": clip.get("count", "")})
    try:
        found = s.page.evaluate(_CONDITION_CLIP_JS, "Title") or []
    except PWError:
        found = []
    for cand in found:
        if any(c["x"] == cand["x"] and c["y"] == cand["y"] for c in out):
            continue
        out.append(cand)
    return out[:cap]


def _dialog_buttons(s: Session) -> list[str]:
    """What the open dialog's buttons are called. Read only."""
    try:
        return s.page.evaluate(
            """() => {
                const m = [...document.querySelectorAll('.modal.show')].pop();
                if (!m) return [];
                const out = [];
                for (const b of m.querySelectorAll('button, a.btn')) {
                    const t = (b.innerText || '').replace(/\\s+/g, ' ').trim();
                    if (t && !out.includes(t)) out.push(t);
                }
                return out.slice(0, 8);
            }""") or []
    except PWError:
        return []


def _attachment_value(label: str, kind: str, marker: str) -> Optional[str]:
    """
    What to put in a field on the attachments dialog.

    _auto_value gives every plain text box on a screen the same sentence,
    which is fine where it is one box among authored fields and useless here:
    this dialog is nothing but discovered text boxes, and identical text in
    two of them makes the round trip vacuous — either would match the other.
    So the field's own name goes into its text. Anything _auto_value knows
    better about — an email box, a phone box, a numeric one — is left to it.
    """
    base = _auto_value(label, kind, marker)
    if kind in ("text", "textarea", "rich") and base and _NOTE in base:
        return f"{_NOTE} {marker}. Attachment field: {label[:50]}."
    return base


def _attach_through_clip(s: Session, res: CaseFlowResult, dry_run: bool,
                         say) -> W.Entry:
    """
    Put this run's file on the condition through the panel's PAPERCLIP.

    The Add Condition panel has no file field. What it has is the clip beside
    Title with an attachment count next to it, and the file box is inside the
    dialog that clip opens — so `upload` on the panel finds nothing there, and
    falling back to the first file input on the SCREEN would hand this run's
    generated image to the sidebar's bulk importer, which is a different
    feature nobody asked to exercise.

    The dialog's own fields are filled here as well, and recorded straight
    onto the result rather than handed back through the pass. That is
    deliberate: the dialog has a Title of its own, and the pass reads every
    value it recorded back off the PANEL before saving — so a dialog field
    called Title would be compared against the CONDITION's title and reported
    as lost. They are read back from the condition's attachments list instead,
    which is where the app actually puts them.

    Raises FillError, which the pass records as a blocker, rather than
    guessing at any of it.
    """
    screen = SCREEN_LABEL[CONDITIONS]
    candidates = _condition_clip_candidates(s)
    tried: list[str] = []
    opened = False
    for cand in candidates:
        tried.append(cand["what"])
        try:
            s.page.mouse.click(cand["x"], cand["y"])
        except PWError:
            continue
        try:
            s.page.wait_for_selector(".modal.show", timeout=4000)
            opened = True
            say(f"    {cand['what']} opened the attachments dialog")
            break
        except PWError:
            continue

    if not opened:
        # It may not be a dialog at all: a clip that triggers a hidden file
        # input directly needs no dialog, and set_input_files can drive that
        # input whether or not anything was clicked. `strict` is what keeps
        # this from reaching for the sidebar's bulk importer when the Title
        # field has no input of its own.
        try:
            return W.Filler(session=s, screen=screen,
                            group=CONDITION_ATTACHMENT_GROUP).upload(
                                res.upload_path, "Title", strict=True)
        except (W.FillError, Exception):     # noqa: BLE001
            pass
        raise W.FillError(
            "nothing on the Title row of the Add Condition panel opened an "
            "attachments dialog, and the Title field holds no file input of "
            "its own, so there was nowhere to put the file. Clicked, in "
            "order: " + ("; ".join(tried) if tried else "no candidate was "
                         "found at all")
            + ". Nothing was uploaded — attaching to the first file input on "
              "the screen would have fed the image to the sidebar's bulk "
              "import instead.")

    s.page.wait_for_timeout(1200)
    s.screenshot("61-condition-attachment")

    # A Filler of its own, which widgets scopes to the dialog that is now
    # open — see Filler._scope.
    d = W.Filler(session=s, screen=screen, group=CONDITION_ATTACHMENT_GROUP)
    try:
        e = d.upload(res.upload_path)
    except (W.FillError, Exception) as exc:      # noqa: BLE001
        _close_modal_hard(s)
        raise W.FillError(
            f"the attachments dialog opened but has no file input to attach "
            f"to: {str(exc)[:150]}")

    # Everything else the dialog shows. The specification names no fields for
    # it, so every one of them is discovered.
    extra, refused = _fill_discovered(d, res, {_norm(e.label)}, say,
                                      value_for=_attachment_value)
    say(f"    {extra} further field(s) on the attachments dialog filled")
    if refused:
        res.checks.append(R.failed(
            "Conditions: every field on the attachments dialog accepts a value",
            expected="every field on the attachments dialog takes a value",
            actual=f"{len(refused)} did not: " + " | ".join(refused[:4]),
            screen=screen))

    if dry_run:
        _close_modal_hard(s)
        res.notes.append(R.observation(
            "The file is added to the condition",
            f"Dry run: {os.path.basename(res.upload_path)} was attached and "
            f"{extra + 1} field(s) on the attachments dialog were filled, but "
            f"the dialog's own commit was deliberately not pressed, so "
            f"nothing was added.", screen=screen))
        res.entries.extend(x.as_dict() for x in d.entries)
        return e

    committed = ""
    for name in _ATTACH_COMMITS:
        try:
            d.commit(name)
            committed = name
            break
        except (W.FillError, W.WriteRefused):
            continue
    if not committed:
        offered = ", ".join(_dialog_buttons(s)) or "no buttons"
        _close_modal_hard(s)
        raise W.FillError(
            f"the attachments dialog has no enabled "
            f"{' / '.join(_ATTACH_COMMITS)} button, so the file could not be "
            f"added to the condition. It offers: {offered}.")

    # The dialog usually closes itself on commit. When it does, its list
    # cannot be re-read — which is not a failure and must not be reported as
    # one: the attachment is verified from the condition's own attachments
    # list after the round trip either way.
    shot = s.screenshot("61-condition-attached")
    still_open = bool(s.page.locator(".modal.show, .modal.in").count())
    rows = _attachment_rows(s) if still_open else 0
    if rows:
        res.checks.append(R.passed(
            "The file is added to the condition",
            detail=f"{committed} left the dialog listing {rows} "
                   f"attachment(s), {e.value} among them.", screen=screen))
    elif not still_open:
        res.notes.append(R.observation(
            "The file is added to the condition",
            f"{committed} closed the attachments dialog, so its list could "
            f"not be re-read here. Whether {e.value} is on the condition is "
            f"answered by the round trip, which re-opens the condition and "
            f"reads its attachments list.", screen=screen))
    else:
        res.checks.append(R.failed(
            "The file is added to the condition",
            expected=f"{committed} adds {e.value} to the dialog's list",
            actual="the dialog stayed open and its list is still empty",
            detail="The condition will save without an attachment.",
            evidence=[shot], screen=screen))

    if not _close_modal_hard(s):
        res.notes.append(R.observation(
            "The attachments dialog closes again",
            "It would not close. The Add Condition panel is behind it and a "
            "dialog's backdrop swallows every click, so the rest of the "
            "condition could not be filled or saved.",
            evidence=[s.screenshot("61-attachment-dialog-stuck")],
            screen=screen))

    res.entries.extend(x.as_dict() for x in d.entries)
    return e


def _open_our_condition(s: Session, res: CaseFlowResult, say,
                        cap: int = 25) -> str:
    """
    Open the condition THIS run added, and return the title it opened.

    "" when none of the rows is ours. Opening the first row instead would be
    worse than failing: a case can carry a register of conditions somebody
    else raised, and comparing this run's values against one of those reports
    every one of them as lost.

    The row's own text cannot be the test here, the way it is on Facilities.
    Clicking a condition slides a panel in OVER the register rather than
    replacing it, so the list — with every condition's title in it, this
    run's included — is still on screen behind whichever row was opened, and
    the marker would be found for all of them. The panel's own Title field is
    what actually says which record is showing.

    Rows come from _condition_rows, not from Session.row_opener_count. That
    was the reason this returned "" on a register with two conditions plainly
    in it: collect_row_openers walks <table> rows, the register is a list of
    <p class="todo-title">, so it counted none, and every value the run had
    entered was reported as unverifiable on a condition that had saved
    perfectly well.
    """
    marker = flows._norm_value(res.marker)
    available = _condition_rows(s)
    if available <= 0:
        say("  the register has no rows to open")
        return ""
    say(f"  looking for this run's condition among {available} row(s)")

    for i in range(min(cap, available)):
        if not _open_condition_row(s, i):
            continue
        if not _wait_for_anchor(s, _CONDITION_ANCHOR, timeout_s=8):
            s.leave_row_detail()
            continue
        shown = W.Filler(session=s, screen=SCREEN_LABEL[CONDITIONS]).value_of(
            "Title")
        if not marker or marker in flows._norm_value(shown):
            return shown or res.condition_ref or "the condition this run added"
        _close_side_panel(s, _CONDITION_ANCHOR)
    return ""


# --------------------------------------------------------------------------
# The round trip
# --------------------------------------------------------------------------

def verify_case_entries(s: Session, res: CaseFlowResult, say, step,
                        reopen: bool = True) -> list[R.Check]:
    """
    Re-read every value through a fresh load of the case and compare.

    This is the whole point of the exercise. The case is re-entered FROM MY
    BUCKET rather than read off the form that is still on screen, so what is
    compared came back from the server. A value the app dropped, truncated,
    re-formatted or failed to store then shows up as a failed check instead of
    being assumed good because Save raised no error.

    Comparison is deliberately forgiving about presentation and strict about
    content: an amount entered as 10000000 may render as "10,000,000" and a date
    entered as 31/12/2027 as "December 31st, 2027". Neither is a defect. A
    different VALUE is.
    """
    checks: list[R.Check] = []
    if reopen:
        # Three attempts, not the default two. This is the leg that has to get
        # back in after the run has hammered the case, and My Bucket is least
        # reliable exactly here.
        if not _open_case(s, res, say, step, "40", attempts=3):
            # _open_case has already made this an ERROR for the whole run.
            # Asserting nothing is the honest outcome: the case was never
            # reached, so nothing at all is known about what it holds.
            return checks

    by_screen: dict[str, list[dict]] = {}
    for e in res.entries:
        by_screen.setdefault(e.get("screen") or "(case)", []).append(e)

    for screen, items in by_screen.items():
        first = len(checks)
        try:
            # _open_reported_screen, not _open_screen: Business Performance and
            # Financials report under a SUB-MENU label that the case sidebar
            # does not carry, so getting back inside them means going through
            # the parent entry first. See SUB_MENU_PARENT.
            _open_reported_screen(s, screen, step)
        except NavigationError as exc:
            # A screen this run filled and saved, that the case then will not
            # open, is one fault. Reported once rather than once per value:
            # repeating it per field buries it under its own consequences.
            checks.append(R.failed(
                f"{screen} can be re-opened to verify what was entered",
                expected=f"the case offers the {screen!r} screen that was "
                         f"filled and saved earlier in this run",
                actual=str(exc),
                detail=f"{len(items)} value(s) were entered here and none of "
                       f"them could be read back.",
                screen=screen))
            continue

        if screen == SCREEN_LABEL[FACILITIES]:
            checks.extend(_verify_facility(s, res, items, say))
        elif screen == SCREEN_LABEL[OBSERVATIONS]:
            checks.extend(_verify_observations(s, res, items, say))
        elif screen == SCREEN_LABEL[DOCUMENTS]:
            checks.extend(_verify_documents(s, res, items, say))
        elif screen == SCREEN_LABEL[CONDITIONS]:
            checks.extend(_verify_conditions(s, res, items, say))
        elif screen == SCREEN_LABEL[POLICIES]:
            checks.extend(_verify_policies(s, res, items, say))
        elif screen == SCREEN_LABEL[COVERAGE]:
            checks.extend(_verify_coverage(s, res, items, say))
        elif screen == SCREEN_LABEL[COLLATERALS]:
            checks.extend(_verify_collateral(s, res, items, say))
        elif screen == SCREEN_LABEL[RISK_RATING]:
            checks.extend(_verify_risk_rating(s, res, items, say))
        elif screen == SCREEN_LABEL[LITIGATION]:
            # Its record is a grid row whose detail opens as a MODAL, and a
            # modal is outside the scope everything else reads. See
            # _verify_litigation.
            checks.extend(_verify_litigation(s, res, items, say))
        elif screen == SCREEN_LABEL[ECIB_DETAILS]:
            # Its own record, found by this run's marker, and its computed
            # figures checked a second time on the re-opened page.
            checks.extend(_verify_ecib_details(s, res, items, say))
        elif screen == SCREEN_LABEL[BANK_RELATIONSHIPS]:
            # A record of its own, found by this run's marker - the list shows
            # six of its columns and none of its editors.
            checks.extend(_verify_bank_relationship(s, res, items, say))
        elif screen.startswith(FIN_INPUT_LABEL):
            # A 450-row chart of accounts whose boxes have no labels: value_of
            # has nothing to anchor on, so the figures are read back by row
            # name. See _verify_financials.
            checks.extend(_verify_financials(s, res, screen, items, say))
        elif screen in (BP_CUSTOMER_LABEL, BP_GROUP_LABEL):
            # Not one labelled field on either of these: their figures are
            # cells of a grid whose row names are in a different table, so
            # value_of has nothing to work from. See _verify_bp.
            checks.extend(_verify_bp(s, res, screen, items, say))
        else:
            # The narrative screens come through here. They have no grid at
            # all: their editors are on the screen itself, so value_of reads
            # them straight out of the TinyMCE iframe and the row pass never
            # runs.
            checks.extend(_verify_screen(s, res, screen, items, say))
        flows._attribute(checks[first:], screen)

    say(f"  compared {len(res.entries)} entered value(s) across "
        f"{len(by_screen)} screen(s)")
    return checks


def _verify_screen(s: Session, res: CaseFlowResult, screen: str,
                   items: list[dict], say) -> list[R.Check]:
    """Compare the values entered on one case screen against what it now shows."""
    f = W.Filler(session=s, screen=screen)
    shot = s.screenshot(f"verify-{_safe(screen)}")
    page_text = _readable_text(s)

    # A summary table shows only a few of a row's columns; the rest of what was
    # entered is in the row's own detail. Opening the rows is what makes those
    # answers determinate instead of "not found".
    opened = 0
    if any(not f.value_of(e["label"]) for e in items) and flows._grid_rows(s) > 0:
        detail_text, opened = flows._read_row_details(s, say)
        if opened:
            page_text += " " + detail_text
            shot = s.screenshot(f"verify-{_safe(screen)}-row")
    return _compare(f, items, screen, page_text, shot, opened,
                    notes=res.notes)


def _verify_observations(s: Session, res: CaseFlowResult, items: list[dict],
                         say) -> list[R.Check]:
    """
    Re-read the observation this run saved.

    The list shows only the reference and the Title, so reading it alone would
    report every other field as lost. The observation is therefore re-OPENED —
    found by the run marker in its Title, so it is this run's and not one of
    the observations already on the case — and its panel read field by field.
    """
    screen = SCREEN_LABEL[OBSERVATIONS]
    page_text = flows._screen_text(s)
    shot = s.screenshot("verify-Observations-list")

    marked = next((e["value"] for e in items if res.marker in str(e["value"])),
                  res.marker)
    if not _open_observation(s, res.marker):
        say("  the saved observation could not be re-opened from the list")
        return _compare(W.Filler(session=s, screen=screen), items, screen,
                        page_text, shot, 0, notes=res.notes)

    say(f"  re-opened the observation: {marked[:80]}")
    f = W.Filler(session=s, screen=screen)
    shot = s.screenshot("verify-Observations-detail")
    # The list stays on screen behind the panel, so both are searchable.
    page_text += " " + _readable_text(s)
    return _compare(f, items, screen, page_text, shot, 1,
                    notes=res.notes)


def _verify_policies(s: Session, res: CaseFlowResult, items: list[dict],
                     say) -> list[R.Check]:
    """
    Re-read the policy exception this run raised.

    The list is filtered to Exceptions first, which is both how the marker is
    searched for and which rows get opened. Unfiltered, that column is the
    policy LIBRARY — every policy the deployment defines — so a search over it
    says nothing about what was raised against this case, and the rows opened
    from it are policy templates rather than saved exceptions.

    The list is then checked for the marker BEFORE anything is opened, and
    that is a check in its own right rather than a preliminary: the list shows
    each exception's Title, the Title carries this run's marker, and its
    absence there means the exception is not on the case at all — a different
    finding from an exception that is there with a field missing.
    """
    screen = SCREEN_LABEL[POLICIES]
    checks: list[R.Check] = []

    filtered = _show_exceptions_only(s)
    list_text = flows._screen_text(s)
    list_shot = s.screenshot("verify-Policies-list")
    where = (f"the list filtered to {filtered!r}" if filtered
             else "the policy list")

    if flows._appears_in(res.marker, list_text):
        checks.append(R.passed(
            "The saved exception is listed on the case",
            detail=f"A row in {where} carries this run's marker "
                   f"({res.marker}), so the exception was stored.",
            screen=screen))
    else:
        checks.append(R.failed(
            "The saved exception is listed on the case",
            expected=f"a row carrying this run's marker ({res.marker})",
            actual=f"no row in {where} does",
            detail="The list shows each exception's Title, and the Title this "
                   "run entered carries the marker — so its absence means the "
                   "exception was not stored against this case."
                   + ("" if filtered else
                      " Note the screen showed no Exceptions filter, so this "
                      "searched the whole policy list."),
            evidence=[list_shot], screen=screen))

    opened = _open_our_exception(s, res, say)
    if not opened:
        # Which of the two failed matters to whoever reads this, so it is said
        # rather than left as "could not check": a row that is not there at
        # all is a save that did not happen, and a row that is there but will
        # not open is this screen's own behaviour.
        why = ("The list has a row carrying this run's marker, but none of "
               "the ways of clicking it opened a form, so the values could "
               "not be read back. The exception itself IS stored — the check "
               "above found it in the list."
               if flows._appears_in(res.marker, list_text) else
               "No row in the list carries this run's marker, so there was "
               "nothing to re-open.")
        checks.append(R.failed(
            "The policy exception this run raised can be re-opened",
            expected="the row carrying this run's marker opens its form",
            actual=why,
            detail=f"{len(items)} value(s) were entered on it and none of them "
                   f"could be read back.",
            evidence=[list_shot], screen=screen))
        return checks

    say(f"  re-opened the exception: {opened[:80]!r}")
    panel_shot = s.screenshot("verify-Policies-detail")
    # The list stays on screen behind the form, so both are searchable — which
    # matters for the fields the form shows as text rather than in a labelled
    # box.
    checks.extend(_compare(
        W.Filler(session=s, screen=screen), items, screen,
        list_text + " " + _readable_text(s), panel_shot, 1,
        where=f"the re-opened exception {opened[:50]!r}",
        notes=res.notes))

    # Closed before leaving, and not merely tidiness: this screen is verified
    # BEFORE Conditions and Documents, and a panel left up hands its fields to
    # whatever goes looking next and covers the controls that screen has to
    # click. Best-effort — the comparison above is already recorded, so a form
    # that will not close is not worth a finding of its own here.
    anchor = _exception_anchor(s)
    if anchor:
        _close_side_panel(s, anchor)
    return checks


def _verify_documents(s: Session, res: CaseFlowResult, items: list[dict],
                      say) -> list[R.Check]:
    """
    Re-read the two documents this run touched.

    Both have to be re-opened, and for the same reason the observation does:
    the checklist shows a title and a couple of badges, so comparing against
    the list alone would report every other field as lost. The document this
    run ACTIONED is found by the title that was recorded when it was opened —
    not "the first one", which adding a document a moment earlier may well have
    changed — and the document this run ADDED is found by the run marker in its
    title.

    The uploaded file is checked by name. That is what makes the attachment
    verifiable at all: the marker is in the filename, so finding it on the
    re-opened document proves THIS run's upload is there rather than an
    attachment somebody added last week.
    """
    screen = SCREEN_LABEL[DOCUMENTS]
    checks: list[R.Check] = []
    list_shot = s.screenshot("verify-Documents-list")

    by_group: dict[str, list[dict]] = {}
    for e in items:
        by_group.setdefault(e.get("group") or screen, []).append(e)

    # Which title re-opens each pass's record.
    targets = {
        ADDITIONAL_DOCUMENT_PASS.name: next(
            (str(e["value"]) for e in items
             if res.marker in str(e.get("value", ""))
             and e.get("group") == ADDITIONAL_DOCUMENT_PASS.name), ""),
        DOCUMENT_ACTION_PASS.name: res.document_ref,
    }
    found_file = False

    for group, group_items in by_group.items():
        want = targets.get(group, "")
        stem = _safe(group)

        def unverifiable(reason: str, shot: str) -> None:
            """Say so, rather than compare against whatever is on screen."""
            say(f"  {reason}")
            checks.append(R.failed(
                f"{group} can be re-opened to verify what was entered",
                expected="the document this run actioned opens its own panel",
                actual=reason,
                detail=f"{len(group_items)} value(s) were entered here and "
                       f"none of them could be read back.",
                evidence=[shot], screen=screen))

        # A clean screen for each. The panel opened for the previous group sits
        # over the one this group needs, and leaving it there is how the last
        # version of this came to read one document's boxes and report them
        # against another.
        _close_open_panels(s)
        if not want:
            unverifiable(f"No record was identified for {group}, so there was "
                         f"nothing to re-open.", list_shot)
            continue
        if not _open_document(s, want):
            unverifiable(f"{want[:80]!r} is not on the checklist any more, so "
                         f"the values entered against it could not be read "
                         f"back.", list_shot)
            continue

        anchor, foreign = _VERIFY_ANCHORS.get(
            group, (_ACTION_ANCHOR, _UPLOAD_ANCHOR))
        scope = _wait_for_panel(s, anchor, foreign)
        panel_shot = s.screenshot(f"verify-Documents-{stem}")
        if not scope:
            unverifiable(f"{want[:60]!r} opened no panel carrying {anchor!r}, "
                         f"so its saved values could not be read.", panel_shot)
            continue

        # Which document is this panel actually showing? Asked out loud,
        # because comparing against the wrong record produces a page of
        # confident, wrong findings.
        showing = _panel_is_showing(s, want, scope)
        if not showing:
            unverifiable(
                f"The panel that opened is not {want[:60]!r} — its Title reads "
                f"otherwise — so nothing was compared against it.", panel_shot)
            continue
        say(f"  re-opened {showing[:70]!r}")

        # Split by where the value can actually be read back from. The
        # attachment fields are emptied on save and their values move into the
        # document's own attachments list; everything else stays on the panel.
        on_panel = [e for e in group_items if not _is_attachment_field(e)]
        on_clip = [e for e in group_items if _is_attachment_field(e)]

        if on_panel:
            # Scoped to the panel for the same reason the fill was: both panels
            # carry a field called Title, so an unscoped read would compare
            # this document's values against the other panel's boxes.
            checks.extend(_compare(
                W.Filler(session=s, screen=screen, scope_selector=scope),
                on_panel, screen, _readable_text(s), panel_shot, 1,
                where=f"the panel of {showing[:60]!r}",
                notes=res.notes))

        if on_clip:
            clip_text, clip_shot, rows = _open_attachments(s, stem)
            if rows:
                say(f"    its attachments list holds {rows} file(s)")
                checks.extend(_compare(
                    W.Filler(session=s, screen=screen),
                    on_clip, screen, clip_text, clip_shot, 1,
                    where=f"the attachments list of {showing[:50]!r}",
                    notes=res.notes))
                name = os.path.basename(res.upload_path or "")
                if name and flows._appears_in(name, clip_text):
                    found_file = True
            else:
                for e in on_clip:
                    checks.append(R.Check(
                        name=f"{group}: {e['label']} carried through",
                        status=R.FAIL,
                        expected=f"{e['value']!r} in the attachments list of "
                                 f"{showing[:50]!r}",
                        actual="that document has no attachments list, or it "
                               "is empty",
                        detail="An attachment was uploaded and saved against "
                               "this document, so its paperclip should open a "
                               "list holding it.",
                        evidence=[clip_shot or panel_shot], screen=screen))
            _close_modal_hard(s)

    # The file itself, by the name that carries this run's marker. Checked
    # separately from its title and description because a stored attachment
    # with the wrong filename is a different fault from a missing one.
    if res.upload_path:
        name = os.path.basename(res.upload_path)
        if found_file:
            checks.append(R.passed(
                "The uploaded file is on the case",
                detail=f"{name} is listed among the attachments of the "
                       f"document it was attached to. The filename carries "
                       f"this run's marker, so it is this run's upload and not "
                       f"one left by an earlier one.", screen=screen))
        else:
            checks.append(R.Check(
                name="The uploaded file is on the case",
                status=R.FAIL,
                expected=f"{name} in the attachments list of the document it "
                         f"was attached to",
                actual="not found there",
                detail="The file was attached and saved, but the document's "
                       "attachments list does not name it. Worth checking "
                       "whether the app stored it under a name of its own — "
                       "that would be a presentation difference rather than a "
                       "lost upload.",
                evidence=[list_shot], screen=screen))

    return checks


def _verify_conditions(s: Session, res: CaseFlowResult, items: list[dict],
                       say) -> list[R.Check]:
    """
    Re-read the condition this run raised.

    Two legs, for the two places the app keeps what was entered. The
    condition's own fields come back off its re-opened panel. The attachment's
    title and description do not — they were typed into a dialog that files
    them against the condition and clears itself — so those are read from the
    condition's attachments list, exactly as the Documents round trip reads
    its own.

    The register is checked for the marker BEFORE anything is opened, and that
    is a check in its own right rather than a preliminary: the grid shows the
    Title, the Title carries this run's marker, and its absence there means
    the condition is not on the case at all — which is a different finding
    from a condition that is there with a field missing.
    """
    screen = SCREEN_LABEL[CONDITIONS]
    checks: list[R.Check] = []
    list_text = flows._screen_text(s)
    list_shot = s.screenshot("verify-Conditions-list")

    if flows._appears_in(res.marker, list_text):
        checks.append(R.passed(
            "The saved condition is listed in the register",
            detail=f"A row on the register carries this run's marker "
                   f"({res.marker}), so the condition was stored.",
            screen=screen))
    else:
        checks.append(R.failed(
            "The saved condition is listed in the register",
            expected=f"a row carrying this run's marker ({res.marker})",
            actual="no row on the register does",
            detail="The register shows each condition's Title, and the Title "
                   "this run entered carries the marker — so its absence "
                   "means the condition was not stored against this case.",
            evidence=[list_shot], screen=screen))

    on_panel = [e for e in items
                if e.get("group") != CONDITION_ATTACHMENT_GROUP]
    on_clip = [e for e in items
               if e.get("group") == CONDITION_ATTACHMENT_GROUP]

    opened = _open_our_condition(s, res, say)
    if not opened:
        checks.append(R.failed(
            "The condition this run raised can be re-opened",
            expected="a row in the register whose Title carries this run's "
                     "marker opens its panel",
            actual="no row did",
            detail=f"{len(items)} value(s) were entered on it and none of them "
                   f"could be read back.",
            evidence=[list_shot], screen=screen))
        return checks
    say(f"  re-opened the condition: {opened[:80]!r}")
    panel_shot = s.screenshot("verify-Conditions-detail")

    if on_panel:
        # The register stays on screen behind the panel, so both are
        # searchable — which matters for the fields the panel shows as text
        # rather than in a labelled box.
        checks.extend(_compare(
            W.Filler(session=s, screen=screen), on_panel, screen,
            list_text + " " + _readable_text(s), panel_shot, 1,
            where=f"the re-opened condition {opened[:50]!r}",
            notes=res.notes))

    if on_clip or res.upload_path:
        clip_text, clip_shot, rows = _open_attachments(s, "conditions")
        name = os.path.basename(res.upload_path or "")
        empty = ("that condition has no attachments list, or it is empty")

        if rows:
            say(f"    its attachments list holds {rows} file(s)")
            checks.extend(_compare(
                W.Filler(session=s, screen=screen), on_clip, screen,
                clip_text, clip_shot, 1,
                where=f"the attachments list of {opened[:50]!r}",
                notes=res.notes))
            # The file itself, by the name that carries this run's marker.
            # Checked separately from its title and description because a
            # stored attachment under a name of the app's own is a different
            # fault from a missing one.
            if name and flows._appears_in(name, clip_text):
                checks.append(R.passed(
                    "The uploaded file is on the condition",
                    detail=f"{name} is listed among the condition's "
                           f"attachments. The filename carries this run's "
                           f"marker, so it is this run's upload and not one "
                           f"left by an earlier one.", screen=screen))
            elif name:
                checks.append(R.Check(
                    name="The uploaded file is on the condition",
                    status=R.FAIL,
                    expected=f"{name} among the {rows} attachment(s) of "
                             f"{opened[:50]!r}",
                    actual="not one of them names it",
                    detail="The condition has attachments, so something was "
                           "stored — just not under the name this run "
                           "uploaded. Worth checking whether the app renamed "
                           "it, which would be a presentation difference "
                           "rather than a lost upload.",
                    evidence=[clip_shot or list_shot], screen=screen))
        else:
            for e in on_clip:
                checks.append(R.Check(
                    name=f"{e.get('group') or screen}: {e['label']} carried "
                         f"through",
                    status=R.FAIL,
                    expected=f"{e['value']!r} in the attachments list of "
                             f"{opened[:50]!r}",
                    actual=empty,
                    detail="A file was attached and added to this condition, "
                           "so its paperclip should open a list holding it.",
                    evidence=[clip_shot or panel_shot], screen=screen))
            if name:
                checks.append(R.Check(
                    name="The uploaded file is on the condition",
                    status=R.FAIL,
                    expected=f"{name} in the attachments list of "
                             f"{opened[:50]!r}",
                    actual=empty,
                    detail="The file was attached and the condition saved, so "
                           "the condition's paperclip should open a list "
                           "holding it. Either the upload was not stored, or "
                           "this build keeps a condition's attachments "
                           "somewhere other than behind that clip.",
                    evidence=[clip_shot or panel_shot], screen=screen))
        _close_modal_hard(s)

    return checks


def _verify_facility(s: Session, res: CaseFlowResult, items: list[dict],
                     say) -> list[R.Check]:
    """
    Compare a facility's values, tab by tab, on the facility this run created.

    Every row is NOT opened here, and one specific row is: a case can carry
    several facilities, and reading the wrong one would report this run's values
    as lost when they are sitting safely on another record. The facility is
    found by the run marker typed into its Purpose of Facility, so it is the one
    this run made and no other.
    """
    screen = SCREEN_LABEL[FACILITIES]
    checks: list[R.Check] = []
    opened, which = _open_our_record(s, res, say, screen, "facility",
                                     res.facility_ref)
    if not opened:
        checks.append(R.failed(
            "The facility this run created can be re-opened",
            expected="the Facilities grid opens the facility carrying this "
                     "run's marker",
            actual=which or "it could not be re-opened",
            detail=f"{len(items)} value(s) were entered on it and none of them "
                   f"could be read back.",
            screen=screen))
        return checks
    say(f"  re-opened the facility: {which[:100]}")

    strip = {t["label"]: t for t in s.tab_strip(limit=16)}
    by_group: dict[str, list[dict]] = {}
    for e in items:
        by_group.setdefault(e.get("group") or screen, []).append(e)

    for group, group_items in by_group.items():
        tab = _tab_for_group(group, list(strip))
        if tab and not strip.get(tab, {}).get("active"):
            try:
                s.open_tab(tab)
            except NavigationError as exc:
                checks.append(R.failed(
                    f"{group} can be re-opened to verify what was entered",
                    expected=f"the saved facility offers its {tab!r} tab",
                    actual=str(exc),
                    detail=f"{len(group_items)} value(s) were entered here and "
                           f"none of them could be read back.", screen=screen))
                continue
        f = W.Filler(session=s, screen=screen, group=group)
        shot = s.screenshot(f"verify-{_safe(group)}")
        page_text = _readable_text(s)
        rows = 0
        if (any(not f.value_of(e["label"]) for e in group_items)
                and flows._grid_rows(s) > 0):
            detail_text, rows = flows._read_row_details(s, say, cap=6)
            if rows:
                page_text += " " + detail_text
        checks.extend(_compare(f, group_items, screen, page_text, shot,
                               rows, notes=res.notes))
    return checks


# A named grid on the Risk Rating screen, read as rows of cells.
#
# Rating Summary and Rating History are both tables under their own heading,
# and reading the page as one string cannot tell them apart — a rating present
# in the summary would satisfy a search meant for the history. The heading is
# what separates them.
_RATING_GRID_JS = r"""([wanted]) => {
    const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();
    const norm = (s) => clean(s).toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim();
    const vis = (el) => {
        const r = el.getBoundingClientRect();
        return r.width > 0 && r.height > 0;
    };
    for (const want of wanted) {
        const key = norm(want);
        for (const h of document.querySelectorAll(
                'h1, h2, h3, h4, h5, h6, legend, .card-title, .card-header, '
                + '.panel-title, a, span, div, li')) {
            if (!vis(h) || norm(h.textContent) !== key) continue;
            let node = h.parentElement;
            for (let i = 0; i < 8 && node; i++, node = node.parentElement) {
                const table = node.querySelector('table');
                if (!table || !vis(table)) continue;
                const head = [...table.querySelectorAll('thead th, thead td')]
                    .map(c => clean(c.textContent)).filter(Boolean);
                const rows = [];
                for (const tr of table.querySelectorAll('tbody tr')) {
                    if (!vis(tr)) continue;
                    const cells = [...tr.querySelectorAll('td, th')]
                        .map(c => {
                            const inp = c.querySelector('input, select');
                            return clean(inp ? (inp.value || '') : c.textContent);
                        });
                    if (cells.some(Boolean)) rows.push(cells);
                }
                return {heading: want, headers: head, rows: rows};
            }
        }
    }
    return null;
}"""


def _rating_grid(s: Session, headings: list[str]) -> Optional[dict]:
    """One of the Risk Rating screen's tables, by its heading."""
    try:
        return s.page.evaluate(_RATING_GRID_JS, [headings])
    except PWError:
        return None


_RATING_SUMMARY = ["Rating Summary", "RATING SUMMARY"]
_RATING_HISTORY = ["Rating History", "RATING HISTORY"]

_ANY_NUMBER = re.compile(r"\d+(?:\.\d+)?")


def _grade_of(text: str) -> str:
    """
    The grade a rating reads as, as a bare number.

    One grade, three spellings: the model answers '1', the Rating Summary shows
    '1' in its Final Rating column, and the Rating History shows '1-Excellent'.
    The leading number is the grade in every one of them.

    Comparing the text instead cannot work here and is not merely imprecise:
    '1' is a substring of '11-Doubtful', so a case rated 11 would satisfy a
    search for a rating of 1. Reading the number is what makes the comparison
    mean what it says.
    """
    t = (text or "").strip()
    if not t:
        return ""
    m = re.match(r"\d+(?:\.\d+)?", t)
    if not m:
        # 'Doubtful : 11' — how the model's own Risk Rating field words it.
        m = _ANY_NUMBER.search(t)
    if not m:
        return ""
    n = float(m.group(0))
    return str(int(n)) if n == int(n) else str(n)


def _rating_column(grid: dict, names: list[str]) -> int:
    """Where the rating sits in a grid, by exact header text. -1 if nowhere."""
    headers = [_norm(h) for h in (grid.get("headers") or [])]
    for name in names:
        key = _norm(name)
        if key in headers:
            return headers.index(key)
    return -1


def _cell(row: list, at: int) -> str:
    return (row[at] or "").strip() if 0 <= at < len(row) else ""


def _verify_risk_rating(s: Session, res: CaseFlowResult, items: list[dict],
                        say) -> list[R.Check]:
    """
    Re-read the rating this run produced, off a fresh load of the case.

    The screen has no free-text field, so there is no marker on it and the
    RATING ITSELF is what identifies this run's work — read off the model the
    moment Generate Score answered, and looked for here. That is why this does
    not go back into the model: the Rating Summary and the Rating History are
    the case's own copy of the answer, and they are what a user sees.

    Two separate questions, and they are different faults:

      Rating Summary   does the case show the rating the model produced? A
                       summary showing a DIFFERENT rating is worse than one
                       showing none, and only comparing the values catches it.
      Rating History   was the rating recorded as an event at all? A summary
                       can be re-rendered from a model the server never stored.

    Both questions are asked of the RATING and of nothing else — read out of
    the one column of each grid that holds it, rather than by searching the
    grid's text for every value the model produced. See RATING_KEYS for why.
    """
    screen = SCREEN_LABEL[RISK_RATING]
    checks: list[R.Check] = []
    s.page.wait_for_timeout(1500)
    shot = s.screenshot("verify-Risk-Rating")

    want = _grade_of((res.rating_values or {}).get(RATING_KEYS[0], ""))

    # ---- Rating Summary -------------------------------------------------
    summary = _rating_grid(s, _RATING_SUMMARY)
    if summary is None:
        checks.append(R.failed(
            "The Rating Summary shows the rating this run produced",
            expected="a Rating Summary table on the Risk Rating screen",
            actual="no table under that heading was found",
            detail="The rating was saved, so the case should be showing it.",
            evidence=[shot], screen=screen))
    elif not summary["rows"]:
        checks.append(R.failed(
            "The Rating Summary shows the rating this run produced",
            expected="a row in the Rating Summary carrying this run's rating",
            actual="the table is on the screen and holds no rows",
            detail="Save raised no error but the case shows no rating, so "
                   "nothing was stored.",
            evidence=[shot], screen=screen))
    else:
        say(f"  the Rating Summary holds {len(summary['rows'])} row(s)")
        at = _rating_column(summary, _SUMMARY_RATING_COL)
        if not want:
            # The model produced nothing to look for. The summary having rows
            # is not evidence this run put them there, and saying so beats
            # claiming a pass on somebody else's rating.
            checks.append(R.failed(
                "The Rating Summary shows the rating this run produced",
                expected="a rating from this run to compare against",
                actual=f"the model produced none, and the summary's "
                       f"{len(summary['rows'])} row(s) cannot be told from a "
                       f"rating that was already on the case",
                detail="See the model checks above for why no rating came "
                       "out.", evidence=[shot], screen=screen))
        elif at < 0:
            # Without the column there is no comparison to make. Reported as
            # its own fault rather than as a wrong rating: the grade may be
            # perfectly correct in a column this does not know the name of.
            checks.append(R.failed(
                "The Rating Summary shows the rating this run produced",
                expected=f"a {_SUMMARY_RATING_COL[0]!r} column in the Rating "
                         f"Summary",
                actual="its columns are: "
                       + (", ".join(summary["headers"]) or "unnamed"),
                detail="The rating could not be read, so whether the case "
                       "shows it is unknown — this is not a wrong rating.",
                evidence=[shot], screen=screen))
        else:
            shown = [_cell(r, at) for r in summary["rows"]]
            hit = next((c for c in shown if _grade_of(c) == want), "")
            if hit:
                checks.append(R.passed(
                    "The Rating Summary shows the rating this run produced",
                    detail=f"The summary's {summary['headers'][at]!r} column "
                           f"shows {hit!r}, which is the rating the model "
                           f"produced ({want}). Only the rating is compared — "
                           f"the rest of the row is the model's workings and "
                           f"the app's own bookkeeping.",
                    screen=screen))
            else:
                checks.append(R.failed(
                    "The Rating Summary shows the rating this run produced",
                    expected=f"a rating of {want} — what the model produced",
                    actual=f"the summary's {summary['headers'][at]!r} column "
                           f"shows " + (", ".join(repr(c) for c in shown[:4])
                                        or "nothing"),
                    detail="The model produced one rating and the case is "
                           "showing another, so the rating did not survive the "
                           "save.", evidence=[shot], screen=screen))

    # ---- Rating History -------------------------------------------------
    history = _rating_grid(s, _RATING_HISTORY)
    if history is None:
        # The history is a tab of its own on this screen, so it may simply not
        # be the one showing. Opening it is worth one attempt before reporting.
        if _open_model_tab(s, ["Rating History"]):
            s.page.wait_for_timeout(1200)
            shot = s.screenshot("verify-Rating-History")
            history = _rating_grid(s, _RATING_HISTORY)

    if history is None:
        checks.append(R.failed(
            "The Rating History records this run's rating",
            expected="a Rating History table on the Risk Rating screen",
            actual="no table under that heading was found, and the tab could "
                   "not be opened",
            detail="The history is where a rating is recorded as an event, so "
                   "whether this one was recorded cannot be told.",
            evidence=[shot], screen=screen))
    elif not history["rows"]:
        checks.append(R.failed(
            "The Rating History records this run's rating",
            expected="a row in the Rating History for the rating just saved",
            actual="the table is on the screen and holds no rows",
            detail="A rating that was stored is an event on the case, and "
                   "nothing was recorded.", evidence=[shot], screen=screen))
    else:
        rows = history["rows"]
        say(f"  the Rating History holds {len(rows)} row(s)")
        at = _rating_column(history, _HISTORY_RATING_COL)
        if not want:
            checks.append(R.failed(
                "The Rating History records this run's rating",
                expected="a rating from this run to look for",
                actual=f"the model produced none, and none of the {len(rows)} "
                       f"row(s) here can be told from an earlier rating",
                detail="See the model checks above for why no rating came out.",
                evidence=[shot], screen=screen))
        elif at < 0:
            checks.append(R.failed(
                "The Rating History records this run's rating",
                expected=f"a {_HISTORY_RATING_COL[0]!r} column in the Rating "
                         f"History",
                actual="its columns are: "
                       + (", ".join(history["headers"]) or "unnamed"),
                detail="The rating could not be read, so whether this run's "
                       "was recorded is unknown — this is not a missing entry.",
                evidence=[shot], screen=screen))
        else:
            # The NEWEST row, which is the one this run just added. Accepting
            # any row would pass on an earlier rating that happens to match —
            # and on this case it would have: the history already held a
            # 1-Excellent from a week before the run that produced another.
            newest = _cell(rows[-1], at)
            if _grade_of(newest) == want:
                checks.append(R.passed(
                    "The Rating History records this run's rating",
                    detail=f"{len(rows)} row(s), and the newest carries "
                           f"{newest!r} in its {history['headers'][at]!r} "
                           f"column — the rating this run produced ({want}). "
                           f"Only the rating is compared.", screen=screen))
            else:
                # Rows, but the newest is not this run's. Not a pass: the
                # history of a case that has been rated before is never empty,
                # so its length says nothing on its own.
                elsewhere = [i + 1 for i, r in enumerate(rows)
                             if _grade_of(_cell(r, at)) == want]
                checks.append(R.failed(
                    "The Rating History records this run's rating",
                    expected=f"the newest row to carry a rating of {want} — "
                             f"what the model produced and saved",
                    actual=f"the newest of {len(rows)} row(s) carries "
                           + (repr(newest) if newest else "nothing")
                           + (", though row "
                              + ", ".join(str(i) for i in elsewhere)
                              + (" does" if len(elsewhere) == 1 else " do")
                              if elsewhere else ", and no row does"),
                    detail="The history holds earlier ratings, so its length "
                           "alone proves nothing."
                           + (" A match on an older row is an earlier rating, "
                              "not this one." if elsewhere else
                              " This run's rating was not recorded."),
                    evidence=[shot], screen=screen))

    # The model's inputs are NOT compared here, and cannot honestly be. They
    # were answered on the rating model — a separate application in a tab of
    # its own — and the case's Risk Rating screen shows two grids and nothing
    # else. Comparing them the ordinary way looked for eighteen LOV answers on
    # a screen that never displays them and reported all eighteen as values
    # that did not survive the save. They survived; the screen simply is not
    # where they live. What the model did with them is already checked, at the
    # place it can be: the model scored, the Calculation Sheet flagged nothing,
    # and the rating it produced is the one the case now shows.
    if items:
        res.notes.append(R.note(
            f"{len(items)} input(s) answered on the rating model are not "
            f"listed here. They live on the model, not on the case's Risk "
            f"Rating screen, so the round trip checks what the case does show "
            f"— the rating itself.", screen=screen))
    return checks


def _verify_coverage(s: Session, res: CaseFlowResult, items: list[dict],
                     say) -> list[R.Check]:
    """
    Re-read the associations this run added, off a fresh load of the trees.

    Nothing is re-opened here, and nothing needs to be: this screen has no
    records to drill into. Both trees ARE the stored state — an association
    renders as "CC30445 - Consumer Goods (0) - 25%" on the node it was added
    to — so re-entering the screen through the case sidebar and reading the
    trees is the round trip.

    The coverage percentage is what each association is found by. The dialog
    has no free-text field, so there is no marker on this screen; the
    percentages are derived from the run's marker instead and are therefore
    this run's own. That is checked BEFORE the field comparison and is a
    finding in its own right: a share that is not on the tree means the
    association is not on the case at all, which is a different fault from one
    that is there with a value changed.
    """
    screen = SCREEN_LABEL[COVERAGE]
    checks: list[R.Check] = []
    opened = _expand_coverage_trees(s)
    say(f"  opened {opened} collapsed node(s) on the coverage trees")
    tree_text = _coverage_tree_text(s, expand=False)
    sides = _coverage_side_text(s, expand=False)
    shot = s.screenshot("verify-Facility-Coverage")

    for offset, group in enumerate((COVERAGE_FROM_FACILITY,
                                    COVERAGE_FROM_COLLATERAL)):
        # Only report on a side this run actually got to. A side whose tree
        # had no '+' entered nothing, and claiming its association is missing
        # would be inventing a failure.
        if not any(e.get("group") == group for e in items):
            continue
        pct = _coverage_percent(res.marker, offset)

        # Both ends, re-read from a fresh load of the case. The pass leg has
        # already asked this of the screen it was standing on; asking it again
        # here is what tells a tree that was merely re-rendered in the browser
        # from one the server actually stored both ends of.
        both = _check_both_trees(
            sides, pct,
            f"{group} is on the facility tree and the collateral tree",
            screen, shot, notes=res.notes)
        if both is not None:
            checks.append(both)

        if f"{pct}%" in tree_text:
            checks.append(R.passed(
                f"{group} is on the coverage tree",
                detail=f"A node carries {pct}%, the share this run entered on "
                       f"this side, so the association was stored.",
                screen=screen))
        else:
            checks.append(R.failed(
                f"{group} is on the coverage tree",
                expected=f"a node carrying this run's share, {pct}%",
                actual="no node on either tree does",
                detail="The tree shows each association as its collateral and "
                       "its share, so a share that is not there means the "
                       "association did not survive the round trip.",
                evidence=[shot], screen=screen))

    # What is left to compare, and what is deliberately not.
    #
    # The share is already answered above, so it is kept out of _compare: a
    # two-digit number is not distinctive enough for its presence in a page to
    # mean anything, and _compare would report it as unverifiable underneath a
    # check that has just answered it properly.
    #
    # The counterpart dropdown is kept out for the opposite reason. The dialog
    # names a collateral by its whole option text — "AUTOMATION TEST
    # 0909-1407-30445 - Entered by automated test. … (0) - 25%" — and the tree
    # names the same record "CC30445 - Consumer Goods". Neither is wrong, and
    # comparing one against the other reported a stored association as lost.
    rest, counterpart = [], []
    for e in items:
        key = _norm(e.get("label", ""))
        if key.startswith("coverage"):
            continue
        if key in ("collaterals", "collateral", "facilities", "facility"):
            counterpart.append(e)
        else:
            rest.append(e)

    for e in counterpart:
        found, how = _tree_shows(tree_text, e["value"])
        name = f"{e.get('group') or screen}: {e['label']} carried through"
        if found:
            checks.append(R.passed(
                name, detail=f"The tree {how}, so the association points at "
                             f"the record that was chosen.", screen=screen))
        else:
            # Not a finding: the dialog and the tree name the same record
            # differently, so there is no comparison to make. Whether the
            # association is there at all is already answered above, by its
            # share and by both trees carrying it.
            res.notes.append(R.observation(
                name,
                f"The dialog names this record by its full option text "
                f"({str(e['value'])[:60]}…) and the tree names it by its own "
                f"reference, so the two cannot be compared literally and "
                f"nothing in the tree matched a distinctive part of it "
                f"either. The share above is what says the association is "
                f"there.",
                evidence=[shot], screen=screen))

    checks.extend(_compare(W.Filler(session=s, screen=screen), rest, screen,
                           tree_text, shot, 0,
                           where="the case's Facility Coverage trees",
                           notes=res.notes))
    return checks


def _tree_shows(tree_text: str, value: str) -> tuple[bool, str]:
    """
    Does the tree name the record this value chose, and how?

    Two ways, because the dialog and the tree name the same record
    differently. The whole value first. Failing that, a distinctive run of
    digits out of it — a collateral's reference number survives into the
    tree ("…-30445" in the option, "CC30445" on the node) where the
    descriptive text around it does not.

    Four digits is the threshold: shorter runs are years, counts and
    percentages, which appear on almost any node and would make a match mean
    nothing.
    """
    text = flows._norm_value(tree_text)
    if flows._appears_in(value, tree_text):
        return True, f"shows {str(value)[:50]!r}"
    for token in sorted(re.findall(r"\d{4,}", str(value)), key=len,
                        reverse=True):
        if token in text:
            return True, (f"carries {token}, the reference out of "
                          f"{str(value)[:40]!r}")
    return False, ""


def _verify_collateral(s: Session, res: CaseFlowResult, items: list[dict],
                       say) -> list[R.Check]:
    """
    Compare a collateral's values, tab by tab, on the collateral this run
    created.

    The same two legs as _verify_facility, and for the same reasons: the
    record is found by the marker rather than by position, because a case
    carries several collaterals and reading somebody else's would report this
    run's values as lost; and each pass's values are compared on the TAB they
    were entered on, because that is where the app put them.
    """
    screen = SCREEN_LABEL[COLLATERALS]
    checks: list[R.Check] = []
    opened, which = _open_our_record(s, res, say, screen, "collateral",
                                     res.collateral_ref)
    if not opened:
        checks.append(R.failed(
            "The collateral this run created can be re-opened",
            expected="the Collaterals grid opens the collateral carrying this "
                     "run's marker",
            actual=which or "it could not be re-opened",
            detail=f"{len(items)} value(s) were entered on it and none of them "
                   f"could be read back.",
            screen=screen))
        return checks
    say(f"  re-opened the collateral: {which[:100]}")

    strip = {t["label"]: t for t in s.tab_strip(limit=16)}
    by_group: dict[str, list[dict]] = {}
    for e in items:
        by_group.setdefault(e.get("group") or screen, []).append(e)

    for group, group_items in by_group.items():
        tab = _tab_for_group(group, list(strip))
        if tab and not strip.get(tab, {}).get("active"):
            try:
                s.open_tab(tab)
            except NavigationError as exc:
                checks.append(R.failed(
                    f"{group} can be re-opened to verify what was entered",
                    expected=f"the saved collateral offers its {tab!r} tab",
                    actual=str(exc),
                    detail=f"{len(group_items)} value(s) were entered here and "
                           f"none of them could be read back.", screen=screen))
                continue
        f = W.Filler(session=s, screen=screen, group=group)
        shot = s.screenshot(f"verify-{_safe(group)}")
        page_text = _readable_text(s)
        rows = 0
        if (any(not f.value_of(e["label"]) for e in group_items)
                and flows._grid_rows(s) > 0):
            detail_text, rows = flows._read_row_details(s, say, cap=6)
            if rows:
                page_text += " " + detail_text
        checks.extend(_compare(f, group_items, screen, page_text, shot,
                               rows, notes=res.notes))
    return checks


def _tab_for_group(group: str, tabs: list[str]) -> str:
    """The tab a pass's values were entered on, matched back to the live strip."""
    if group in (REQUESTED_FACILITY, REQUESTED_COLLATERAL):
        # Chosen in a dialog before the record had tabs, so it belongs to none
        # of them. Pairing it with the closest-sounding tab would compare the
        # product or classification name against a tab that never held it.
        return ""
    name = group.split("—")[-1].strip() if "—" in group else group
    want = _words(name)
    if not want:
        return ""
    best, best_score = "", 0.0
    for tab in tabs:
        have = _words(tab)
        if not have:
            continue
        overlap = len(want & have)
        if not overlap:
            continue
        score = overlap / len(want) + overlap / len(have)
        if score > best_score:
            best, best_score = tab, score
    return best


# What the screen is HOLDING, as opposed to what it is displaying.
#
# flows._screen_text reads innerText, and the value of an <input> is never
# part of innerText — the browser keeps it as a property, not as a text node.
# That is not a detail: the facility's run marker is typed into Facility
# Purpose, which is a text input, so a marker search over innerText could
# never find it on any facility, however perfectly it had saved. The round
# trip then reported all sixty-one values as unverifiable on a facility whose
# every field was sitting there on screen.
#
# A <textarea> is the same trap wearing a different hat: its innerText is the
# markup it was SERVED with, not what has been typed into it since.
#
# So the values are read as values. Rich-text editors are read through their
# iframe's body, which is where TinyMCE keeps its content, and a cross-origin
# frame is skipped rather than allowed to throw.
_FIELD_VALUES_JS = r"""() => {
    const vis = (el) => el.offsetParent !== null
                     || el.getClientRects().length > 0;
    const out = [];
    for (const el of document.querySelectorAll(
            'input, textarea, [contenteditable="true"]')) {
        if (el.type === 'hidden' || !vis(el)) continue;
        const v = (el.value !== undefined && el.value !== null)
                ? el.value : el.innerText;
        if (v) out.push(String(v));
    }
    for (const fr of document.querySelectorAll('iframe')) {
        try {
            const b = fr.contentDocument && fr.contentDocument.body;
            if (b && b.innerText) out.push(b.innerText);
        } catch (e) { /* cross-origin: not ours to read */ }
    }
    return out.join(' \n ');
}"""


def _entered_values_text(s: Session) -> str:
    """Everything the screen's own fields are holding, as text."""
    try:
        return s.page.evaluate(_FIELD_VALUES_JS) or ""
    except PWError:
        return ""


def _readable_text(s: Session) -> str:
    """
    Everything a saved record is showing OR holding, for _compare's fallback.

    _compare asks two questions of every entered value: does its own field
    read it back, and failing that, is it anywhere on this screen? The second
    question was asked of innerText alone, so a value the app had stored and
    put back into an <input> answered "nowhere" — and a stored value reported
    as lost is the one outcome this suite exists to prevent.
    """
    return flows._screen_text(s) + " " + _entered_values_text(s)


def _carries_marker(s: Session, marker: str) -> bool:
    """
    Is this run's marker anywhere on the record showing right now?

    Both halves are needed. A saved value renders as TEXT in a grid or a
    read-only view and as a field VALUE on a form, and this application shows
    the same facility both ways on different tabs.
    """
    if not marker:
        return True
    if marker in flows._norm_value(flows._screen_text(s)):
        return True
    return marker in flows._norm_value(_entered_values_text(s))


def _marker_on_tabs(s: Session, marker: str) -> str:
    """
    Which tab of the open record carries the marker, or "".

    Every tab is searched, not just the one the record opens on: the marker is
    typed into Facility Purpose, which lives on Facility Request Details — and
    into Collateral Description, on Basic Information — and neither record
    necessarily opens on the tab that holds it.
    """
    if _carries_marker(s, marker):
        return "the tab it opened on"
    for tab in s.tab_strip(limit=12):
        if tab.get("active"):
            continue
        try:
            s.open_tab(tab["label"])
        except NavigationError:
            continue
        if _carries_marker(s, marker):
            return tab["label"]
    return ""


def _back_to_grid(s: Session, screen: str) -> bool:
    """
    Come back OUT of a record's detail to the screen's own grid.

    Session.leave_row_detail is not enough here and cannot be: it dismisses
    overlays, and a facility's — or a collateral's — detail is not an overlay.
    It is a full screen with its own tab strip that REPLACES the grid. So
    after looking at one record the run was still standing inside it, and
    every later open_row_detail found no grid rows to click and reported
    "would not open". On a case with three facilities that meant row 1 was
    searched and rows 2 and 3 were never opened at all — including the one
    this run had just created.

    Re-opening the screen from the case sidebar is what actually resets the
    position, and it is the same hop the verification leg used to reach the
    screen in the first place.
    """
    s.leave_row_detail()
    try:
        s._step_context_menu(NavStep(kind=CONTEXT_MENU, label=screen))
    except NavigationError:
        return False
    s.page.wait_for_timeout(1200)
    return True


def _open_our_record(s: Session, res: CaseFlowResult, say, screen: str,
                     noun: str, ref: str = "",
                     cap: int = 12) -> tuple[bool, str]:
    """
    Open the record THIS run created on a grid screen, by its run marker.

    Shared by Facilities and Collaterals because the problem is identical: a
    case carries several of them, only one is this run's, and the marker is in
    a field on one of the record's tabs.

    Rows are opened one at a time and the grid is RE-ENTERED between them —
    see _back_to_grid for why leaving is not automatic. Falling back to the
    first row would be worse than failing: it would compare this run's values
    against somebody else's record and call them lost. On the live case row 1
    is an earlier run's facility, so "first row" would have been wrong every
    time.

    What happened to each row is logged as it goes, and named in the failure.
    That is what turned "no facility carries this run's marker … that usually
    means it was not saved" — wrong on both counts — into "row 2 would not
    open", which says where to look.
    """
    marker = flows._norm_value(res.marker)
    try:
        available = s.row_opener_count(cap)
    except Exception:  # noqa: BLE001 - an empty grid is ordinary, not an error
        available = 0
    say(f"  looking for this run's {noun} among {available} row(s) on the "
        f"grid")

    tried: list[str] = []
    for i in range(min(cap, available)):
        # Back to the grid before every row after the first. The previous
        # iteration left us inside a record, and the grid is not on screen
        # from in there.
        if i and not _back_to_grid(s, screen):
            tried.append(f"the {screen} grid could not be re-opened to reach "
                         f"row {i + 1}")
            break
        try:
            if not s.open_row_detail(i):
                tried.append(f"row {i + 1} would not open")
                say(f"  row {i + 1} would not open")
                continue
        except Exception:  # noqa: BLE001
            tried.append(f"row {i + 1} could not be opened")
            break
        where = _marker_on_tabs(s, marker)
        if where:
            say(f"  row {i + 1} is ours — the marker is on {where}")
            return True, (ref or f"the {noun} carrying this run's marker, "
                                 f"found on {where}")
        tried.append(f"row {i + 1} carries no marker on any of its tabs")
        say(f"  row {i + 1} is not ours")

    return False, (f"No {noun} on this case carries this run's marker "
                   f"({res.marker}) — searched both the text each row shows "
                   f"and the values its fields are holding, across every tab "
                   f"of every row: "
                   + ("; ".join(tried) if tried else "the grid had no rows")
                   + f". The {noun} may not have saved, or it saved without "
                     f"the field the marker was typed into.")


def _compare(f: W.Filler, items: list[dict], screen: str, page_text: str,
             shot: str, opened: int, where: str = "",
             notes: Optional[list] = None) -> list[R.Check]:
    """
    One entered value against what the screen now shows.

    The difference between these outcomes is what keeps this report
    trustworthy:

      the field is on screen        compare the values. A difference is a real
                                    finding.
      no field, but the value is
      in the screen's text          it was entered through an add-row form and
                                    now lives in a table row. Present is enough.
      no field and nothing found    a FAIL if the value is distinctive enough
                                    that its absence means something.

    A value too short to search for is none of those. '1', 'No' and '12' match
    something incidental on almost any page, so neither their presence nor
    their absence is evidence and no check can honestly be recorded — they are
    gathered and reported once, as an observation, so that a value does not go
    unverified without anyone being told.
    """
    checks: list[R.Check] = []
    unverifiable: list[str] = []
    for e in items:
        label, typed, kind = e["label"], e["value"], e["kind"]
        name = f"{e.get('group') or screen}: {label} carried through"
        shown = f.value_of(label)

        if shown:
            if flows._same_value(typed, shown, kind):
                checks.append(R.passed(
                    name, detail=f"Entered {typed!r}, the case shows {shown!r}.",
                    screen=screen))
            else:
                checks.append(R.failed(
                    name,
                    expected=f"{typed!r} — the value that was entered",
                    actual=f"{shown!r} — what the case shows",
                    detail="The value did not survive the round trip. Worth "
                           "checking whether the field was truncated, "
                           "reformatted or mapped to another.",
                    evidence=[shot] if shot else [], screen=screen))
            continue

        # Where this comparison actually looked. A caller that opened one
        # specific record says so, because "somewhere on the case's Documents"
        # is not a claim a reader can check — and on a screen whose values live
        # behind a paperclip it is not even true.
        looked = where or (
            f"the case's {screen}, including the {opened} saved row(s) opened "
            f"from its tables" if opened else f"the case's {screen}")

        if flows._appears_in(typed, page_text):
            checks.append(R.passed(
                name,
                detail=f"{typed!r} is shown in — or held by a field of — "
                       f"{looked}, so it was stored. It has no labelled field "
                       f"of its own there because the form that took it is not "
                       f"the view that displays it.",
                screen=screen))
        elif not flows._distinctive(typed):
            unverifiable.append(f"{label} = {typed!r}")
        else:
            checks.append(R.Check(
                name=name, status=R.FAIL,
                expected=f"{typed!r} in {looked}",
                actual="not found there, as a field or as text",
                detail="Entered on this screen but absent when the case was "
                       "re-opened, so the value did not carry across."
                       + ("" if opened else
                          " Note the summary table shows only some columns; no "
                          "row could be opened to check the rest."),
                evidence=[shot] if shot else [], screen=screen))

    if unverifiable and notes is not None:
        notes.append(R.note(
            f"{len(unverifiable)} value(s) entered on {screen} have no "
            f"labelled field there and are too short to search the page for "
            f"reliably, so whether they carried across could not be told "
            f"either way: " + "; ".join(unverifiable[:8]), screen=screen))
    return checks


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------

def _finish(res: CaseFlowResult, say) -> CaseFlowResult:
    res.finished_at = _now()
    path = _persist(res)
    say(f"Done — {res.headline}")
    say(f"Report: {path}")
    return res


def _persist(res: CaseFlowResult) -> str:
    """
    Written twice, for the same reason the obligor flow writes twice.

      flow.json    everything, including the entered values — this run's own
                   record, and what a later re-verification reads.
      result.json  the same run in the shape runner/results.py produces, so the
                   web page renders it with the SAME code that renders every
                   other run rather than a second renderer that could drift.
    """
    os.makedirs(res.artifacts_dir, exist_ok=True)
    # Checks and notes are dataclasses, and json.dump will not take either —
    # both have to be turned into plain dicts here. Leaving `notes` out of this
    # list crashed every run that recorded one, at the very last step, after
    # all the work was done.
    payload = {k: v for k, v in res.__dict__.items()
               if k not in ("checks", "notes")}
    payload["checks"] = [c.__dict__ for c in res.checks]
    payload["notes"] = [n.__dict__ for n in res.notes]
    payload["overall"] = res.overall
    payload["headline"] = res.headline
    path = os.path.join(res.artifacts_dir, "flow.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)

    compat = {
        "run_id": res.run_id,
        "target_key": "case.screens",
        "target_title": res.title,
        "mode": settings.TRANSACT,
        "base_url": crawler_config.BASE_URL,
        "started_at": res.started_at,
        "finished_at": res.finished_at,
        "artifacts_dir": res.artifacts_dir,
        "error_reason": res.error_reason,
        "notes": [n.__dict__ for n in res.notes],
        "case_id": res.case_id,
        # What this run left on the case. Every record it can create belongs
        # here — a reader deciding whether a case needs cleaning up should not
        # have to know which screens the report happens to name.
        "created_records": [x for x in (res.facility_ref, res.collateral_ref,
                                        res.condition_ref, res.exception_ref,
                                        res.rating_ref)
                            if x],
        "checks": [c.__dict__ for c in res.checks],
        "steps": [{"index": st["index"], "kind": "step", "label": st["text"],
                   "status": st["status"], "note": st.get("note", "")}
                  for st in res.steps],
        "overall": res.overall,
        "headline": res.headline,
        # Extras the fill view shows and the read-only view ignores.
        "screens": [SCREEN_LABEL.get(x, x) for x in res.screens],
        "marker": res.marker,
        "facility_ref": res.facility_ref,
        "document_ref": res.document_ref,
        "condition_ref": res.condition_ref,
        "exception_ref": res.exception_ref,
        "collateral_ref": res.collateral_ref,
        "uploaded": os.path.basename(res.upload_path) if res.upload_path else "",
        "downloaded": [os.path.basename(p) for p in res.downloaded],
        "dry_run": res.dry_run,
        "entries": res.entries,
    }
    with open(os.path.join(res.artifacts_dir, "result.json"), "w",
              encoding="utf-8") as fh:
        json.dump(compat, fh, indent=2)
    return path


# ========================================================================
# The case screens contributed by the qa-automation-2 branch
#
# Litigation, Shariah Comments, RMG Memo, Credit Memorandum, Group Review,
# Business Performance, Relationship with Other Banks / FIs, eCIB Details,
# PR Checklist, Financials and History. They were written against the same
# scaffolding as the screens above - Pass/CField, _fill_pass, _open_screen -
# so they are carried over whole rather than rewritten.
# ========================================================================


# ==========================================================================
# Litigation
#
# The case menu's own Litigation screen: a grid of suits with a '+' that opens
# the entry form. Like Observations the specification describes it as a process
# and gives it no field table, so these ten fields are the ones the screen asks
# for, authored in the order it lays them out.
#
# The '+' is why this is a FORM pass rather than a LINKAGE one. Which shape the
# form takes — a modal over the grid, or a panel beside it — is a deployment
# detail, and it does not have to be known in advance: _open_add_litigation
# tries the named button and then the grid's '+', and Filler scopes itself to
# an open '.modal.show' automatically when there is one. So one pass covers
# both, and neither is assumed.
#
# The dates form a chronology on purpose — filed, then heard, then heard again,
# then decreed. A screen that validates one hearing against another (and this
# one plausibly does) would refuse a set of dates in any other order, and a
# refusal there reads as "the automation could not set the field" when it is
# really the form doing its job.
#
#     Date Of Suit Filing   30/06/2025   PAST
#     Last Date of Hearing  15/01/2026   RECENT
#     Next Date of Hearing  30/06/2027   REVIEW
#     Date Of Decree        31/12/2027   FUTURE
# ==========================================================================

LITIGATION_PASSES: list[Pass] = [
    Pass(
        name="Litigation",
        kind=FORM,
        fields=[
            # An LOV fed by the bank's suit-type reference data, which is
            # re-seeded per environment, so the first valid entry is taken
            # rather than a name that exists on one deployment only. Hard,
            # because a litigation record with no type of suit is not a
            # litigation record and the form is expected to say so.
            _p(["Type of Suit"], optional=False),
            _d(["Date Of Suit Filing", "Date of Suit Filing",
                "Date of Suit filing"], PAST, optional=False),

            # Each free-text box gets its OWN sentence. Identical text in
            # several boxes makes the round trip vacuous — any box would then
            # match any other.
            _t(["Relevant Court"], "Banking Court No. II, Karachi"),
            _t(["Bank's Lawyer Name", "Banks Lawyer Name",
                "Bank Lawyer Name"], "A. R. Siddiqui, Advocate High Court"),
            # "Law Firm Name" is what the form renders; the written
            # requirement has "Law firm Name". The observed spelling goes first,
            # as it does on every other field where the two disagree — the
            # title-attribute anchor is case-sensitive.
            _t(["Law Firm Name", "Law firm Name"],
               "Siddiqui & Co. Legal Consultants"),

            _d(["Last Date of Hearing", "Last Date Of Hearing"], RECENT),
            _d(["Next Date of Hearing", "Next Date Of Hearing"], REVIEW),

            # A numeric box: set_value refuses prose here rather than letting
            # the app reject it later with no message.
            _t(["Suit Amount"], AMOUNT),

            # The marker goes in Proceeding Details, and it is the only field
            # on this screen that could sensibly carry it. It is the screen's
            # narrative box, so it has no length limit to truncate the marker
            # into something the round trip cannot find — and because it is a
            # RICH TEXT editor, proving this one value came back is also the
            # only proof that the editor's content survives a save at all.
            _mark(["Proceeding Details", "Proceedings Details",
                   "Proceeding Detail"]),

            _d(["Date Of Decree", "Date of Decree"], FUTURE),
        ],
        note="The specification defines recording a litigation case as a "
             "process and gives it no field table, so every field here was "
             "read off the entry form rather than taken from the "
             "specification."),
]


# What the control that opens the litigation entry form is called.
#
# "Add Details" is what this build labels the '+' above the LITIGATION DETAILS
# grid, observed on a live run, so it is tried first. The rest are the names
# another deployment might use; the bare "add" stays last because it matches
# any of them and would otherwise shadow a more specific one. If none of these
# is on the screen, the '+' itself is clicked — see _open_add_litigation.
_ADD_LITIGATION = ["add details", "add litigation", "new litigation",
                   "create litigation", "add suit", "add case", "add"]


# ==========================================================================
# Shariah Comments
#
# The smallest screen in the case: one rich-text editor, SCD Remarks, and a
# Save under it. No grid, no '+', no dialog — so it is filled straight, the
# way Request Details is, and there is nothing to open first.
#
# Being one field changes what the run can conclude, and that is worth being
# explicit about. On Observations and Litigation a list or a grid gains an
# entry, which proves Save stored something without trusting the absence of an
# error toast. Here there is no such evidence: the only proof is the ROUND
# TRIP — re-open the case from My Bucket and see whether the editor comes back
# holding what was typed. So this screen leans on the round trip more than any
# other, which is also why its one field carries the run marker: text that is
# the same on every run would match a previous run's copy and prove nothing.
#
# It is a TinyMCE editor, not a textarea. widgets.rich_text knows the
# difference — the original <textarea> is hidden and the editor works inside
# an <iframe>, so filling the textarea sets a value the editor immediately
# overwrites with its own empty content — and kind_of reads which one this
# deployment renders rather than assuming.
# ==========================================================================

SHARIAH_PASSES: list[Pass] = [
    Pass(
        name="Shariah Comments",
        kind=FORM,
        fields=[
            # Casing is offered three ways for the usual reason: fields are
            # anchored on label[title="..."] and a CSS attribute match is
            # case-SENSITIVE, so 'SCD remarks' would not find 'SCD Remarks'.
            # Not optional — it is the only field on the screen, so a run that
            # cannot set it has exercised nothing at all, and saying "could not
            # check" there would be the silence this suite exists to avoid.
            _mark(["SCD Remarks", "SCD remarks", "Scd Remarks",
                   "SCD Remarks (Shariah)"]),
        ],
        note="The specification gives this screen no field table. It has one "
             "field, read off the screen itself: the SCD Remarks editor."),
]


# ==========================================================================
# RMG Memo  (the application's old name for this screen was CRMD Note, and
#            the key is still crmd_note — see the note by SCREEN_LABEL)
#
# Eight narrative editors and a Save. No grid, no '+', no dialog — the same
# shape as Shariah Comments, eight times over, so it is filled straight and
# there is nothing to open first.
#
# Eight boxes on one screen make ONE rule matter more than anything else here:
# every box gets its OWN text, and that text NAMES ITS OWN BOX. The obligor's
# BBFS Details tab is nineteen editors and taught this the hard way — typing
# the same sentence into all of them makes the round trip vacuous, because any
# box would then match any other and "the text came back" would prove nothing
# about where it came back from. So each sentence below says which section it
# belongs to, and the safety tests check they are all distinct.
#
# What a run can conclude here is the same as on Shariah Comments and worth
# repeating: nothing on this screen grows when Save works, so the ROUND TRIP
# is the only evidence anything was stored.
# ==========================================================================

CRMD_NOTE_PASSES: list[Pass] = [
    Pass(
        name="RMG Memo",
        kind=FORM,
        # In the order the screen lays them out.
        fields=[
            # The marker goes in Recommendation: it is the note's own headline
            # section, and one marked field per screen is what makes the round
            # trip mean something — a fixed sentence coming back would match
            # the previous run's copy just as well.
            _mark(["Recommendation", "Recommendations"]),

            _t(["Industry Strategy"],
               "Industry Strategy: the sector outlook is stable and the "
               "exposure sits within the bank's stated appetite. Recorded by "
               "an automated test."),

            # 94 characters long, which is over the 90-character cap in
            # widgets._visible_labels — so DISCOVERY would never offer this
            # field. It is fillable only because it is authored here by name,
            # and the short reading is kept behind it for a build that
            # truncates the label.
            _t(["Internal Indicators (including business reciprocity / "
                "account turnover / overdue history, etc)",
                "Internal Indicators"],
               "Internal Indicators: business reciprocity, account turnover "
               "and overdue history are all satisfactory, with no "
               "irregularity in the period under review. Recorded by an "
               "automated test."),

            _t(["Internal & External Audit Observations",
                "Internal and External Audit Observations"],
               "Internal & External Audit Observations: no adverse "
               "observation is outstanding against this obligor. Recorded by "
               "an automated test."),

            _t(["Return on Capital / Hurdle Rate",
                "Return on Capital/Hurdle Rate", "Return on Capital"],
               "Return on Capital / Hurdle Rate: the return on this exposure "
               "is above the applicable hurdle rate. Recorded by an automated "
               "test."),

            _t(["Key Credit Concerns / Additional Information",
                "Key Credit Concerns/Additional Information",
                "Key Credit Concerns"],
               "Key Credit Concerns / Additional Information: no key credit "
               "concern has been identified. This note was raised by an "
               "automated test and is not a real credit assessment."),

            _t(["Risk Observations / Covenant Compliance Status",
                "Risk Observations/Covenant Compliance Status",
                "Risk Observations"],
               "Risk Observations / Covenant Compliance Status: all covenants "
               "are being complied with and no waiver is being sought. "
               "Recorded by an automated test."),

            _t(["Risk Exposure Strategy"],
               "Risk Exposure Strategy: maintain the existing exposure at "
               "current levels pending the next annual review. Recorded by an "
               "automated test."),
        ],
        note="The specification gives this screen no field table. All eight "
             "fields here were read off the screen itself, and each is a "
             "rich-text editor."),
]


# ==========================================================================
# Credit Memorandum
#
# Thirty-three narrative editors and a Save. The largest screen in the case,
# and the same shape as the two before it: no grid, no '+', nothing to open.
#
# At this size the text for each box is BUILT rather than hand-written, which
# is the choice flows.py already made for the obligor's nineteen BBFS Details
# editors. Two reasons, and both get worse as the count grows:
#
#   - a sentence per box, written out by hand thirty-three times, is thirty-three
#     chances to paste the same one twice. Duplicate text makes the round trip
#     vacuous — any box would match any other — so the property has to hold by
#     construction, not by proof-reading;
#   - thirty-three paragraphs of invented credit analysis on a real obligor in a
#     shared environment is not something to leave behind. Every box here says
#     what it is instead: an automated test's note, naming its own section,
#     stating plainly that it is not a credit assessment.
#
# Six of these labels also appear on CRMD Note. Their text is deliberately
# DIFFERENT on the two screens, and that is load-bearing rather than tidy: if
# the application stored the memorandum's Industry Strategy into the note's
# box, identical text would let both round trips pass. Distinct text makes
# that class of defect visible. The safety tests pin it.
# ==========================================================================

def _memo_note(section: str) -> str:
    """
    The text for one editor on the Credit Memorandum.

    Naming the section is the point, exactly as it is in flows._note_for:
    "some text came back" says nothing, "THIS section's text came back" is a
    result. The disclaimer is not decoration either — this text lands on a
    real obligor's memorandum on a shared environment, and whoever reads it
    next should not have to guess whether it is a genuine assessment.
    """
    return (f"{_NOTE} Section: {section}. Written against a synthetic credit "
            f"memorandum and not a real credit assessment.")


CREDIT_MEMORANDUM_PASSES: list[Pass] = [
    Pass(
        name="Credit Memorandum",
        kind=FORM,
        # In the order the screen lays them out.
        #
        # Three labels carry a RIGHT SINGLE QUOTATION MARK (’) rather than an
        # ASCII apostrophe, and both readings are authored for each. It costs
        # nothing and it is the difference between a field being filled and
        # being reported missing: the label[title="..."] anchor is a literal
        # string match, so ’ and ' are simply different characters there. The
        # text fallback normalises both away, which is why the second reading
        # works at all.
        fields=[
            _t(["Company / Shareholder’s Background",
                "Company / Shareholder's Background"],
               _memo_note("Company / Shareholder’s Background")),

            # The marker goes here. Of the thirty-three this is the one where
            # "raised by an automated test" is not filler but the literal
            # answer to what the section asks for — and one marked field per
            # screen is what stops the round trip matching a previous run's
            # identical text.
            _mark(["Nature / Rationale of Request",
                   "Nature/Rationale of Request"]),

            _t(["Financial Observations"],
               _memo_note("Financial Observations")),
            _t(["Financial Projections - Base Case (Long Term Only)",
                "Financial Projections – Base Case (Long Term Only)"],
               _memo_note("Financial Projections - Base Case")),
            _t(["Financial Projections - Scenario Analysis (Long Term Only)",
                "Financial Projections – Scenario Analysis (Long Term Only)"],
               _memo_note("Financial Projections - Scenario Analysis")),
            _t(["Variance Analysis (Long Term Only)"],
               _memo_note("Variance Analysis")),

            # The six that CRMD Note also has. Same labels, different text —
            # see the note above this list for why that matters.
            _t(["Industry Strategy"],
               _memo_note("Industry Strategy")),
            # 94 characters, over the 90-character cap in
            # widgets._visible_labels, so discovery would never offer this
            # one. It is fillable only because it is authored by name.
            _t(["Internal Indicators (including business reciprocity / "
                "account turnover / overdue history, etc)",
                "Internal Indicators"],
               _memo_note("Internal Indicators")),
            _t(["Internal & External Audit Observations",
                "Internal and External Audit Observations"],
               _memo_note("Internal & External Audit Observations")),
            _t(["Return on Capital / Hurdle Rate",
                "Return on Capital/Hurdle Rate", "Return on Capital"],
               _memo_note("Return on Capital / Hurdle Rate")),

            _t(["Environmental and Social Risk Assessment",
                "Environmental & Social Risk Assessment"],
               _memo_note("Environmental and Social Risk Assessment")),
            _t(["Key Credit Concerns / Additional Information",
                "Key Credit Concerns/Additional Information",
                "Key Credit Concerns"],
               _memo_note("Key Credit Concerns / Additional Information")),
            _t(["Risk Observations / Covenant Compliance Status",
                "Risk Observations/Covenant Compliance Status",
                "Risk Observations"],
               _memo_note("Risk Observations / Covenant Compliance Status")),
            _t(["Risk Advice"], _memo_note("Risk Advice")),

            _t(["Industry Key Success Factors & Updates",
                "Industry Key Success Factors and Updates"],
               _memo_note("Industry Key Success Factors & Updates")),
            _t(["Ways Out Analysis"], _memo_note("Ways Out Analysis")),

            # ONE field, not two — settled by a live run. Authored as two
            # ("Major Obligor" and "Industry & Transaction Risks") neither was
            # found, and discovery then filled the real editor, which the
            # screen labels as the single combined heading below. The split
            # readings are kept behind it in case another build separates them.
            _t(["Major Obligor, Industry & Transaction Risks",
                "Major Obligor, Industry and Transaction Risks",
                "Major Obligor"],
               _memo_note("Major Obligor, Industry & Transaction Risks")),

            _t(["Analysis of Obligor’s Related Parties Transactions",
                "Analysis of Obligor's Related Parties Transactions"],
               _memo_note("Analysis of Obligor’s Related Parties "
                          "Transactions")),
            _t(["Repayment Record"], _memo_note("Repayment Record")),
            _t(["Company Brief / History", "Company Brief/History"],
               _memo_note("Company Brief / History")),

            _t(["RELATIONSHIP - (Credit History and Account Conduct to be "
                "enclosed as per Appendix B)",
                "RELATIONSHIP - (Credit History and Account Conduct to be "
                "enclosed as per Appendix-B)",
                "RELATIONSHIP"],
               _memo_note("RELATIONSHIP — credit history and account "
                          "conduct")),
            _t(["Ancillary Business (Company & Group)",
                "Ancillary Business (Company and Group)"],
               _memo_note("Ancillary Business (Company & Group)")),
            _t(["Key Drivers of Borrower’s Business and Key Risks Associated",
                "Key Drivers of Borrower's Business and Key Risks Associated"],
               _memo_note("Key Drivers of Borrower’s Business and Key Risks "
                          "Associated")),
            _t(["BUSINESS AND THEIR RISK MITIGANTS"],
               _memo_note("BUSINESS AND THEIR RISK MITIGANTS")),

            _t(["Rationale (Justification)"],
               _memo_note("Rationale (Justification)")),
            _t(["Credit History & Account Conduct",
                "Credit History and Account Conduct"],
               _memo_note("Credit History & Account Conduct")),
            _t(["Other Requests"], _memo_note("Other Requests")),

            _t(["DETAILS OF PAYMENT SCHEDULE IF TERM LOAN SOUGHT"],
               _memo_note("DETAILS OF PAYMENT SCHEDULE IF TERM LOAN SOUGHT")),
            _t(["LATEST INCOME TAX / WEALTH TAX FORM TO BE SUBMITTED BY THE "
                "BORROWER",
                "LATEST INCOME TAX / WEALTH TAX FORM TO BE SUBMITTED BY "
                "BORROWER"],
               _memo_note("LATEST INCOME TAX / WEALTH TAX FORM")),

            # Two separate sections with nearly the same name. Neither can be
            # reached by the other's label: the text anchor matches a label
            # EXACTLY once normalised, so "Collateral Justification" does not
            # find "Collateral Evaluation / Justification".
            _t(["Collateral Evaluation / Justification",
                "Collateral Evaluation/Justification"],
               _memo_note("Collateral Evaluation / Justification")),
            _t(["Collateral Justification"],
               _memo_note("Collateral Justification")),

            _t(["Governance"], _memo_note("Governance")),
            _t(["Discretionary Approval (Proposed)"],
               _memo_note("Discretionary Approval (Proposed)")),
        ],
        note="The specification gives this screen no field table. Every field "
             "here was read off the screen itself, and each is a rich-text "
             "editor. There are thirty-three: 'Major Obligor, Industry & "
             "Transaction Risks' is one combined heading, not two sections."),
]


# ==========================================================================
# Group Review
#
# Seven narrative editors and a Save — the same shape as CRMD Note, and
# filled by the same code.
#
# This screen is NOT on every case, and that is worth knowing before a run
# reports it missing. It is in the sidebar of 52224-2026, an Annual Renewal
# with twenty-three case-menu entries, and absent from a Borrower Credit
# Application with twenty-two. A case without it reports "Group Review can be
# opened" as BLOCKED with the reason, which is the honest answer rather than a
# failure.
#
# `Risk Advice` also exists on Credit Memorandum, so its text here is worded
# differently on purpose — the same reason the six sections shared between
# Credit Memorandum and CRMD Note are. Identical text on two screens would
# let a value stored against the wrong one pass both round trips.
# ==========================================================================

def _group_note(section: str) -> str:
    """
    The text for one editor on the Group Review.

    Same job as _memo_note and deliberately not the same words: a section
    that exists on both screens must not carry text that would match either
    one. Naming the section is what makes "this section's text came back" a
    result rather than "some text came back".
    """
    return (f"{_NOTE} Section: {section}. Recorded against a synthetic group "
            f"review and not a real assessment of any group.")


GROUP_REVIEW_PASSES: list[Pass] = [
    Pass(
        name="Group Review",
        kind=FORM,
        # In the order the screen lays them out.
        fields=[
            _t(["Group Background"], _group_note("Group Background")),
            _t(["Group Companies"], _group_note("Group Companies")),
            _t(["Industry Details / Peer Analysis / Financial Analysis",
                "Industry Details/Peer Analysis/Financial Analysis",
                "Industry Details"],
               _group_note("Industry Details / Peer Analysis / Financial "
                           "Analysis")),
            # Casing offered both ways: the title-attribute anchor is a
            # literal match, so 'through' and 'Through' are different there.
            _t(["Trade Business through NBP", "Trade Business Through NBP"],
               _group_note("Trade Business through NBP")),
            _t(["Group Relationship Yield"],
               _group_note("Group Relationship Yield")),

            # The marker goes in the recommendation, as it does on CRMD Note:
            # it is the section where "raised by an automated test" is the
            # literal answer to what is being asked for, and one marked field
            # per screen is what stops the round trip matching a previous
            # run's identical text.
            _mark(["Group Relationship Strategy / Recommendation",
                   "Group Relationship Strategy/Recommendation",
                   "Group Relationship Strategy"]),

            _t(["Risk Advice"], _group_note("Risk Advice")),
        ],
        note="The specification gives this screen no field table. All seven "
             "fields here were read off the screen itself, and each is a "
             "rich-text editor. The screen is not present on every case."),
]


# ==========================================================================
# Business Performance — Customer Business Performance
#                        Group Business Reciprocity
#
# Two sub-menus of one sidebar entry, reached as TABS of it, and not fields at
# all. Each is a grid: the ten row names in one table, twenty numeric boxes in
# another — two period columns, "Overall" and "Bank Value" — and a Save under
# it. Nothing on a box says which row it belongs to, so Filler.grid_cell joins
# the two tables by name; the guards that make that safe are documented there.
#
# Three of the ten rows are greyed and read-only because the application works
# them out for itself, and the arithmetic is checkable rather than something to
# take on trust. Read off the figures already on the screen and confirmed on
# both columns of both sub-menus:
#
#   Total Income of Business Group = Mark-up + Commission + Other Fee
#   Annualized Yield (%)           = Total Earnings / Average Funded x 100
#   Net Yield (%)                  = Annualized Yield - Borrowing Rate
#
# So seven rows per column are typed and three are checked, which is a better
# result than either "the app fills it, nothing to say" or a run that typed
# into a computed box and reported the app for overwriting it.
#
# The same ten row names appear on BOTH sub-menus. That is the trap here: with
# the same figures on both, a value stored against the wrong sub-menu would
# satisfy both round trips and the run would report twenty passes for a
# mix-up. Every figure is therefore unique to its row, its column, its
# sub-menu and its run — see _bp_cell_value.
#
# These are also the first numeric-only screens in the suite, which costs the
# run marker: AUTOTEST-<stamp> is a phrase and there is nowhere to type a
# phrase here. Per-run uniqueness comes from the digits instead.
# ==========================================================================

# Read ONCE, not per field. _stamp() is clock-based, so calling it for each
# field would hand different digits to fields either side of a second
# boundary, and the report would then carry two run numbers for one run.
_BP_RUN = re.sub(r"\D", "", _stamp())


def _bp_run_digits(n: int) -> str:
    """
    Digits unique to this run, for a screen with nowhere to put a phrase.

    Taken from the same clock as run_marker so that a report's numbers and its
    marker agree about which run they came from.

    Padded on the LEFT. Padding on the right and then taking the last n gives
    the padding rather than the clock — every run then typed the same numbers,
    which is precisely the vacuous round trip this exists to avoid.
    """
    return ("0" * n + _BP_RUN)[-n:]


# Two digits leading each amount, so that every figure on these screens is
# distinct from every other one AND from its opposite number on the other
# sub-menu.
_BP_CUSTOMER_BASE = 41


_BP_GROUP_BASE = 51


@dataclass
class BPRow:
    """
    One row of a Business Performance grid.

    Not a CField, because these are not fields: the row's NAME is in one table
    and its boxes are in another, and nothing on a box says which row it
    belongs to. See Filler.grid_cell for how the two are joined and for the
    guards that make joining them safe.
    """
    label: str                  # as the screen renders it
    rate: bool = False          # a percentage rather than an amount
    # How the application derives the figure, for the rows it computes itself.
    # Non-empty means nothing is typed here: the row is read back and checked
    # against this arithmetic instead.
    computed: str = ""


# In screen order. The order matters only for reading the report — every row
# is found by name — but keeping it is what lets the report be read against
# the screen.
BP_ROWS: list[BPRow] = [
    BPRow("Mark-up Earned / Accrued"),
    BPRow("Commission / Exchange Earned"),
    BPRow("Other Fee / Income"),
    BPRow("Total Income of Business Group",
          computed="Mark-up Earned / Accrued + Commission / Exchange Earned "
                   "+ Other Fee / Income"),
    BPRow("Treasury Income"),
    BPRow("Total Earnings of the Bank"),
    BPRow("Average Funded Outstanding"),
    BPRow("Borrowing Rate (%)", rate=True),
    BPRow("Annualized Yield (%)", rate=True,
          computed="Total Earnings of the Bank / Average Funded Outstanding "
                   "x 100"),
    BPRow("Net Yield (%)", rate=True,
          computed="Annualized Yield (%) - Borrowing Rate (%)"),
]


_BP_TOTAL_INCOME = 3


def _bp_cell_value(row: int, column: int, base: int, rate_base: int) -> str:
    """
    What to type into one cell: unique to the row, the column, the sub-menu
    and the run.

    All four matter. Two rows sharing a value would let a figure written to
    the wrong row pass; two columns sharing one would let the two periods be
    confused; the two sub-menus sharing one would let a figure stored against
    the wrong sub-menu satisfy both round trips; and two RUNS sharing one
    would let the previous run's figure satisfy this one's.
    """
    if BP_ROWS[row].rate:
        # Percentages have to stay percentages, so the row and column go in
        # the whole part and only the decimals carry the run.
        return f"{rate_base + row + column * 3}.{_bp_run_digits(2)}"
    return f"{base + row}{column + 1}{_bp_run_digits(4)}"


@dataclass
class BPSpec:
    """
    One Business Performance sub-menu.

    `name` is both the tab to click and the screen every entry and check from
    it is filed under, so the round trip can re-open the right sub-menu and
    the report can say which of the two a result came from.
    """
    name: str
    base: int                   # leading digits of this sub-menu's amounts
    rate_base: int              # whole part of this sub-menu's percentages
    note: str = ""

    def value(self, row: int, column: int) -> str:
        return _bp_cell_value(row, column, self.base, self.rate_base)


# ---- the '+ Add' dialog that makes a period to fill ----------------------
#
# Nothing is enterable on this screen until a PERIOD exists. '+ Add' opens a
# dialog titled BUSINESS PERFORMANCE with four required fields — read off the
# live dialog:
#
#   Actual/Projected*   ng-select: Actual | Projected | Management Accounts
#   Year*               ng-select: 2001 … 2012
#   From*  /  To*       bsdatepicker boxes, 'Choose a Date'
#
# Proceed adds a new COLUMN BLOCK to the grid — its own pair of Overall /
# Bank Value boxes showing 'insert values...', and its own Save — beside the
# periods already there. It does NOT persist: a reload with no Save shows the
# period gone again, which is how this was established.
#
# The year is parameterised because a period the case already holds cannot be
# added twice. Change it, or set LOS_BP_YEAR, when a run reports the dialog
# refusing the period.
BP_PERIOD_TYPE = os.getenv("LOS_BP_TYPE", "Actual")


BP_PERIOD_YEAR = os.getenv("LOS_BP_YEAR", "2011")


# The dialog's own convention, taken from the screen: a year runs 1 January to
# 31 December. Derived rather than hard-coded so changing the year is one edit.
BP_PERIOD_FROM = date(int(BP_PERIOD_YEAR), 1, 1)


BP_PERIOD_TO = date(int(BP_PERIOD_YEAR), 12, 31)


# In the order the panel lists them, which is the order they are filled in.
BP_SUB_MENUS: list[BPSpec] = [
    BPSpec(name=BP_CUSTOMER_LABEL, base=_BP_CUSTOMER_BASE, rate_base=7,
           note="The first of the two Business Performance sub-menus."),
    BPSpec(name=BP_GROUP_LABEL, base=_BP_GROUP_BASE, rate_base=21,
           note="The second sub-menu. The same ten rows as Customer Business "
                "Performance, deliberately given different figures."),
]


# ==========================================================================
# Relationship with Other Banks / FIs
#
# The most involved screen here, and the closest in shape to Facilities: a
# record is CREATED through a dialog before there is anything to fill.
#
#   list page -> '+ Add' -> a dialog with one field, Bank Name -> Proceed
#     -> the record's own detail page
#        -> the LIMITS section's '+' -> a Limits dialog -> Save, Close
#        -> the narrative fields and the two pickers below them
#        -> Save
#
# Two things about it are worth stating up front.
#
# The five figures under the LIMITS table — Total FE Limits, Total Limits,
# Total O/s., Total Overdue, Wallet Share (%) — are all READ-ONLY. The
# application aggregates them from the limit rows, so they are checked rather
# than entered, and checked on a record this run created: a fresh record with
# exactly one limit row makes the aggregation unambiguous, which it would not
# be on a record that already had rows.
#
# And the marker goes in Security, because Security is one of the columns the
# LIST page shows. That is what lets the round trip find the record this run
# created among the ones already on the case, rather than opening the first
# row and hoping.
# ==========================================================================

# Parameterised, as the specification asks. The app's own spelling has TWO
# spaces in it — 'Islamic  Bank' — which is why the plain reading is offered
# as well: choose() normalises, but a value that only ever appears here in one
# form would be a value nobody could search the report for.
BANK_NAME = os.getenv("LOS_BANK_NAME", "Islamic Bank")


_BANK_REQUIRED = "Bank Name is required"


# The Limits dialog, reached from the '+' in the LIMITS section.
BANK_LIMIT_PASS = Pass(
    name="Relationship with Other Banks / FIs — Limits",
    kind=LINKAGE,
    grid_heading="LIMITS",
    fields=[
        # First, because the rest of the dialog is about a type of limit.
        _p(["Limit Type"], optional=False),
        _t(["Total Limit"], AMOUNT, optional=False),
        _t(["Total Outstanding"], SMALL_AMOUNT),
        _d(["Outstanding Date"], RECENT),
        _t(["Total OverDue", "Total Overdue"], "250000"),
        # Left CLEAR deliberately. The list page renders this column as the
        # literal 'false', so a box ticked here and read back as 'true' is
        # only meaningful if the run knows which it asked for — and an
        # unticked box still counts as filled, so it is still compared.
        _p(["Sub Limit"], "false"),
        _t(["FE Limits"], "500000"),
    ],
    save_label="Save",
    note="Every one of these is on the dialog the LIMITS '+' opens, and each "
         "is label-anchored. The three amounts are deliberately different "
         "from each other so the aggregation below cannot be satisfied by the "
         "wrong column.")


# The detail page itself, once the record exists.
BANK_DETAIL_PASS = Pass(
    name="Relationship with Other Banks / FIs",
    kind=FORM,
    fields=[
        # Authored rather than left to discovery, and that is a safety point
        # rather than a tidiness one. Discovery fills an unauthored dropdown
        # with its FIRST option, and the first option here is a different
        # bank on any case where the list is ordered differently — so the run
        # could quietly change the bank of the record it had just created.
        # Setting it explicitly to the bank chosen in the dialog cannot.
        _p(["Select Bank"], BANK_NAME),

        _t(["Facility Details"],
           f"{_NOTE} Facility Details: a funded working-capital line recorded "
           f"against a synthetic bank relationship."),
        # The marker, and the reason it is here: Security is a column on the
        # LIST page, so this is what makes the record findable again.
        _mark(["Security"]),
        _t(["Pricing / Commission", "Pricing/Commission"],
           f"{_NOTE} Pricing / Commission: priced at KIBOR plus a spread, "
           f"with commission recovered quarterly."),
        _t(["Relationship Remarks"],
           f"{_NOTE} Relationship Remarks: conduct of the account has been "
           f"regular throughout the period under review."),
        _t(["Facilities, Security, Markup / Commission, "
            "Justification/Remarks",
            "Facilities, Security, Markup / Commission, Justification / "
            "Remarks",
            "Facilities, Security, Markup / Commission"],
           f"{_NOTE} Facilities, security, markup and justification are "
           f"recorded together here for a synthetic relationship."),
        # FUTURE (31/12/2027), not REVIEW (30/06/2027), and the reason is a
        # finding rather than a preference: this field refuses 30 June 2027
        # specifically. Its calendar settles on 1 June 2027 instead — the
        # same wrong date, from the same request, as Next Date of Hearing on
        # Litigation. 15/06/2027, 15/01/2026 and 31/12/2027 are all accepted
        # here, so it is neither month-ends nor this field: see the README.
        #
        # Using a date the app accepts is what lets the round trip actually
        # say whether an expiry date persists. The defect itself stays
        # exercised, and reported, by Litigation.
        _d(["Expiry Date"], FUTURE),
        _p(["Classification Status"]),
    ],
    save_label="Save",
    note="Three of these are TinyMCE editors, two are plain textareas, and "
         "the last two are a date and a dropdown — read off the screen, which "
         "agrees with the specification on all seven.")


# The read-only figures under the LIMITS table, and what each aggregates. The
# first four are a plain sum of the limit rows; Wallet Share compares this
# bank against the others and cannot be derived from this record alone, so it
# is reported for confirmation rather than asserted.
BANK_SUMMARY_FIELDS = ["Total FE Limits", "Total Limits", "Total O/s.",
                       "Total Overdue", "Wallet Share (%)"]


BANK_SUMMARY_FROM = {
    "Total Limits": "Total Limit",
    "Total O/s.": "Total Outstanding",
    "Total Overdue": "Total OverDue",
    "Total FE Limits": "FE Limits",
}


# ==========================================================================
# eCIB Details
#
# The largest screen in the suite: a dialog of three fields, then a record's
# page of fifty-three, across six sections. Same shape as the bank
# relationship — the record does not exist until the run makes it — with two
# differences that matter, both found by probing rather than by reading the
# specification.
#
# FIRST: three sections are GATED. 'Rescheduled / Restructured?', 'Write-Off?'
# and 'Overdue?' are Yes/No dropdowns, and on a fresh record the twenty-three
# fields beneath them are READ-ONLY. Answer the gate 'Yes' and they open. So
# the specification's "numeric input" is only a numeric input after its gate
# has been answered, and the gate is therefore authored immediately before the
# fields it opens. Authored the other way round, twenty-three fields would
# report as unsettable on a screen that was working perfectly.
#
# SECOND: only SEVEN fields are genuinely computed, and the specification's
# list of greyed-out fields is not that seven. 'Non-Fund Based (Loans)' and
# '(Investments)' are ordinary inputs; 'Number of Times Restructured' is
# gated, not computed. What is computed is checked against arithmetic read off
# the screen itself — see ECIB_COMPUTED.
#
# Proceed does NOT persist a record here, unlike the bank dialog: it only
# opens the page. Nothing exists until Save.
# ==========================================================================

# Parameterised, as the specification asks.
ECIB_TYPE = os.getenv("LOS_ECIB_TYPE", "Entity")


ECIB_BORROWER_CODE = os.getenv("LOS_ECIB_BORROWER_CODE", "67676")


# RELATIVE to the day the run happens, unlike every other date in this file,
# and for a reason the application supplied: a fixed 15/01/2026 was refused
# outright with "Warning (blocks the save): eCIB report is older than 2
# months". That is a server-side rule and the app was right — the data was
# stale, not the automation — but it means a constant here goes off after two
# months and the screen stops being checkable at all.
_ECIB_TODAY = date.today()


ECIB_REPORT_DATE = _ECIB_TODAY - timedelta(days=7)


# Before the report date, so the exposure is 'as on' a date the report could
# actually have covered, and inside the same two-month window.
ECIB_EXPOSURE_DATE = _ECIB_TODAY - timedelta(days=14)


# What the dialog refuses when Proceed is pressed on an empty form, and what
# the record's page refuses when Save is. Both sets were read off the live
# screens; the specification names all five and the app renders all five.
ECIB_DIALOG_RULES = ["Ecib type is required",
                     "eCIB Borrower Code is required",
                     "Ecib report date is required"]


ECIB_PAGE_RULES = ["Company/Individual Name is Required",
                   "Exposure As On is Required"]


# The three fields the dialog sets. ALL THREE arrive on the record's page,
# and all three are asserted rather than re-entered — which is what the
# specification asks for and, on this screen, also the only thing that works:
# the eCIB Type that arrives is an ng-select carrying the chosen value and NO
# options, so trying to re-select it fails with "opened no options" however
# correct its value is.
#
# Worth stating because it caught this work out: reading the raw <ng-select>
# element's own `value` attribute shows empty for every one of these, which
# looks exactly like a value that was not carried through. value_of reads the
# rendered selection instead, and that says 'Entity'.
ECIB_CARRIED = {
    "eCIB Borrower Code": (ECIB_BORROWER_CODE, "text"),
    "eCIB Report Date": (ECIB_REPORT_DATE.isoformat(), "date"),
    "eCIB Type": (ECIB_TYPE, "dropdown"),
}


# The seven read-only fields, and the sum each one is. Every source is a field
# this run enters, so the expected value never depends on another computed
# figure — 'Total (Loans)' is written out as its four contributing inputs
# rather than as 'Fund Based + Non-Fund Based', so a wrong Fund Based cannot
# quietly excuse a wrong Total.
#
# Derived empirically, on a fresh record, before anything else was entered:
#   800,000 + 200,000            -> Total Limit (Loans)  1,000,000
#   300,000 + 20,000 + 5,000     -> Fund Based (Loans)     325,000
#   325,000 + 70,000             -> Total (Loans)          395,000
ECIB_COMPUTED = {
    "Total Limit (Loans)": ["Funded Limit (Loans)",
                            "Non-Funded Limit (Loans)"],
    "Total Limit (Investments)": ["Funded Limit (Investments)",
                                  "Non-Funded Limit (Investments)"],
    "Fund Based (Loans)": ["Principal Outstanding (Loans)",
                           "Markup Outstanding (Loans)",
                           "Other Outstanding (Loans)"],
    "Fund Based (Investments)": ["Principal Outstanding (Investments)",
                                 "Markup Outstanding (Investments)",
                                 "Other Outstanding (Investments)"],
    "Total (Loans)": ["Principal Outstanding (Loans)",
                      "Markup Outstanding (Loans)",
                      "Other Outstanding (Loans)",
                      "Non-Fund Based (Loans)"],
    "Total (Investments)": ["Principal Outstanding (Investments)",
                            "Markup Outstanding (Investments)",
                            "Other Outstanding (Investments)",
                            "Non-Fund Based (Investments)"],
}


_ECIB_RUN = re.sub(r"\D", "", _stamp())


_ecib_n = itertools.count()


def _ecib_amount() -> str:
    """
    A distinct six-digit amount for the next numeric field.

    Distinct per field AND per run, for the same two reasons as Business
    Performance: two fields sharing a value would let a figure stored against
    the wrong one pass, and two runs sharing one would let the previous run's
    figure satisfy this one's round trip. Forty-odd numeric fields on one
    screen make the first of those a real risk rather than a theoretical one.
    """
    lead = 10 + next(_ecib_n)
    return f"{lead}{('0000' + _ECIB_RUN)[-4:]}"


def _ecib_count(i: int) -> str:
    """A plausible count for the 'number of times' fields — small, distinct."""
    return str(21 + i)


def _e(labels, optional: bool = True) -> CField:
    """A numeric field on this screen: the next distinct amount."""
    return _t(labels, _ecib_amount(), optional=optional)


ECIB_PASS = Pass(
    name="eCIB Details",
    kind=FORM,
    fields=[
        # ---- Basic Details -------------------------------------------
        # The marker, and it has to be this field: Company/Individual Name is
        # a column on the LIST page, which is what lets the round trip find
        # the record this run created.
        _mark(["Company/Individual Name"]),
        _d(["Exposure As On"], ECIB_EXPOSURE_DATE, optional=False),
        # eCIB Type is NOT here on purpose. It arrives from the dialog already
        # set, and its dropdown carries no options at all — so it is asserted
        # through ECIB_CARRIED rather than re-selected, which is both what the
        # specification asks for and the only thing this screen permits.
        _p(["eCIB Status"]),
        _p(["Infected"]),

        # ---- Limit ---------------------------------------------------
        _e(["Funded Limit (Loans)"]),
        _e(["Non-Funded Limit (Loans)"]),
        _e(["Funded Limit (Investments)"]),
        _e(["Non-Funded Limit (Investments)"]),

        # ---- Outstanding Liabilities ---------------------------------
        _e(["Principal Outstanding (Loans)"]),
        _e(["Markup Outstanding (Loans)"]),
        _e(["Other Outstanding (Loans)"]),
        # An ordinary input, despite the specification listing it as greyed
        # out. Fund Based and Total are the computed ones on this row.
        _e(["Non-Fund Based (Loans)"]),
        _e(["Principal Outstanding (Investments)"]),
        _e(["Markup Outstanding (Investments)"]),
        _e(["Other Outstanding (Investments)"]),
        _e(["Non-Fund Based (Investments)"]),

        # ---- Rescheduled/Restructured --------------------------------
        # THE GATE FIRST. Everything under it is read-only until this says
        # Yes.
        _p(["Rescheduled / Restructured?", "Rescheduled/Restructured?"],
           "Yes"),
        _t(["Number of Times Restructured/Rescheduled (in 5 yrs) - for "
            "entities only",
            "Number of Times Restructured/Rescheduled (in 5 yrs)"],
           _ecib_count(0)),
        _e(["Amount Under Litigation (Loans)"]),
        _e(["Amount Under Litigation (Investments)"]),

        # ---- Write-Offs ----------------------------------------------
        _p(["Write-Off?", "Write Off?"], "Yes"),
        _e(["Write-Offs (During last 10 Yrs)"]),
        _e(["Waived-Offs (During last 10 Yrs)"]),
        _e(["Recovery (During last 10 Yrs)"]),
        _e(["Settled Write-Offs (During last 5 Yrs)"]),
        _e(["Settled Waived-Offs (During last 5 Yrs)"]),
        _e(["Settled Recovery (During last 5 Yrs)"]),

        # ---- Overdue -------------------------------------------------
        _p(["Overdue?"], "Yes"),
        _e(["Amount in 30+ DPDs"]),
        _e(["Amount in 60+ DPDs"]),
        _e(["Amount in 90+ DPDs (Loans)"]),
        _e(["Amount in 90+ DPDs (Investments)"]),
        _e(["Amount in 120+ DPDs"]),
        _e(["Amount in 150+ DPDs"]),
        _e(["Amount in 180+ DPDs (Loans)"]),
        _e(["Amount in 180+ DPDs (Investments)"]),
        _e(["Amount in 365+ DPDs (Loans)"]),
        _e(["Amount in 365+ DPDs (Investments)"]),
        # The app writes '30+ days' where the specification writes '30 days'.
        # The app's spelling goes first; the specification's is kept behind
        # it, because neither is authoritative and only one answers the DOM.
        _t(["No. of times account went into overdue by 30+ days (only for "
            "Individuals)",
            "No. of times account went into overdue by 30 days (only for "
            "Individuals)"], _ecib_count(1)),
        _t(["No. of times account went into overdue by 60+ days (only for "
            "Individuals)",
            "No. of times account went into overdue by 60 days (only for "
            "Individuals)"], _ecib_count(2)),
        _t(["No. of times account went into overdue by 90+ days (only for "
            "Individuals)",
            "No. of times account went into overdue by 90 days (only for "
            "Individuals)"], _ecib_count(3)),
        _t(["No. of times account went into overdue by 120+ days (only for "
            "Individuals)",
            "No. of times account went into overdue by 120 days (only for "
            "Individuals)"], _ecib_count(4)),
        _t(["No. of times account went into overdue by 150+ days (only for "
            "Individuals)",
            "No. of times account went into overdue by 150 days (only for "
            "Individuals)"], _ecib_count(5)),
        _t(["No. of times account went into overdue by 180+ days (only for "
            "Individuals)",
            "No. of times account went into overdue by 180 days (only for "
            "Individuals)"], _ecib_count(6)),
    ],
    save_label="Save",
    note="Fifty-three fields across six sections, of which seven are computed "
         "by the application and twenty-three only open once their section's "
         "Yes/No gate is answered. The gates are authored immediately before "
         "the fields they open.")


# The gates, so a report can say which sections were opened rather than
# leaving an operator to infer it from twenty-three passing checks.
ECIB_GATES = ["Rescheduled / Restructured?", "Write-Off?", "Overdue?"]


# --------------------------------------------------------------------------
# Getting to the case
# --------------------------------------------------------------------------

def _reload_bucket(s: Session) -> None:
    """
    Load My Bucket through the BROWSER, not through the application's router.

    In-app navigation back to the bucket is what the round trip normally uses
    and it is normally fine. After Financials it is not: that screen leaves
    several 450-row statements in the DOM, and the bucket then renders with
    'Looked at 0 row(s) across 12 page(s)' — an empty grid, twice in a row,
    however long the run waits. A real page load throws all of it away and
    the grid comes back.
    """
    url = crawler_config.BASE_URL.rstrip("/") + "/master/bucket"
    try:
        s.page.goto(url, timeout=120000)
        cr.wait_until_settled(s.page, s.recorder, timeout_ms=45000,
                              stable_polls=2)
        cr.stamp_content_root(s.page)
    except PWError:
        pass

    # Wait for EVIDENCE the grid arrived, not for a fixed number of seconds.
    # A plain sleep was tried and is not enough: the page load returns, the
    # route settles, and the grid is still empty — so find_case read 'no
    # search box on this build' and paged through twelve empty pages. Rows on
    # screen is the only thing that means it is ready.
    for _ in range(15):
        s.page.wait_for_timeout(2000)
        try:
            rows = s.page.evaluate(
                """() => document.querySelectorAll(
                    'table tbody tr').length""") or 0
        except PWError:
            rows = 0
        if rows:
            try:
                cr.stamp_content_root(s.page)
            except PWError:
                pass
            return


def _open_sub_screen(s: Session, parent: str, label: str, step) -> str:
    """
    Open a screen that lives UNDER another sidebar entry.

    The parent is ALWAYS opened first, and that is not just the shortest path
    to the child — it is the only honest one. The driver matches menu labels
    forgivingly, by containment as well as by prefix, because the sidebar
    truncates its own text; and 'Business Performance' is contained in
    'Customer Business Performance'. So asking the sidebar for the child by
    name succeeds by matching the PARENT, and the run then reports "opened
    Customer Business Performance" while sitting on whichever tab the parent
    happened to open on. It worked by luck the first time — the child wanted
    was the default tab — which is the worst way for it to work.

    Once the parent is open the child is looked for as one of its tabs, which
    is what these two are, and then as a sidebar entry of its own for a
    deployment that promotes them.
    """
    parent_note = s._step_context_menu(NavStep(kind=CONTEXT_MENU, label=parent))
    step(f"open {parent}", note=parent_note)

    try:
        # satisfied_if_active because collect_tabs can omit the active tab,
        # and the first sub-menu is the one the parent opens on.
        note = s._step_tab(NavStep(kind=CONTEXT_MENU, label=label,
                                   satisfied_if_active=True))
        step(f"open {label}", note=f"tab of {parent}: {note}")
        return f"tab of {parent}: {note}"
    except NavigationError as as_tab:
        first = as_tab

    try:
        note = s._step_context_menu(NavStep(kind=CONTEXT_MENU, label=label))
    except NavigationError as as_entry:
        raise NavigationError(
            f"'{label}' could not be opened under {parent!r}: not one of its "
            f"tabs ({first}), and not a sidebar entry either ({as_entry})."
        ) from as_entry

    # A containment match on the parent is not the child. Saying so is the
    # difference between a blocked check and a screen reported as verified
    # when the run never reached it.
    if _norm(label) not in _norm(note) and _norm(parent) in _norm(note):
        raise NavigationError(
            f"'{label}' could not be opened under {parent!r}: it is not one "
            f"of its tabs ({first}), and asking the sidebar for it opened "
            f"{parent!r} again ({note}).")
    step(f"open {label}", note=f"under {parent}: {note}")
    return f"under {parent}: {note}"


def _open_reported_screen(s: Session, screen: str, step) -> str:
    """
    Open the screen a set of entries was recorded against.

    The round trip needs this because it works from the label on each entry
    rather than from the screen keys, and a nested screen cannot be reached by
    clicking its own name alone. Going back INSIDE the sub-menu is the whole
    requirement for Business Performance: re-reading the parent would compare
    ten values against a landing page that does not show them.
    """
    # Financials is a sidebar entry whose sub-tabs are not sidebar entries at
    # all, and whose reported names carry the financial year — 'Financials
    # Input (FY2010)'. Neither the sidebar nor _open_sub_screen can find that,
    # so the parent is opened and the tab clicked by its own caption.
    tab = _fin_tab_for(screen)
    if tab:
        note = _open_screen(s, SCREEN_LABEL[FINANCIALS], step)
        cr.wait_until_settled(s.page, s.recorder, timeout_ms=45000,
                              stable_polls=2)
        try:
            cr.stamp_content_root(s.page)
        except PWError:
            pass
        s.page.wait_for_timeout(3500)
        _fin_open_tab(s, tab, step)
        return note

    parent = SUB_MENU_PARENT.get(screen)
    if parent:
        return _open_sub_screen(s, parent, screen, step)
    return _open_screen(s, screen, step)


# --------------------------------------------------------------------------
# Litigation
#
# Same shape as Observations, and for the same reason: a grid of records with
# a control that opens an entry form. What differs is only how the form is
# reached — this screen leads with a '+' rather than a named button — so that
# one part is discovered and everything after it goes through _fill_pass, the
# machinery the other three screens already use.
# --------------------------------------------------------------------------

def _do_litigation(s: Session, res: CaseFlowResult, dry_run: bool,
                   say, step) -> None:
    screen = SCREEN_LABEL[LITIGATION]
    _open_screen(s, screen, step)
    s.screenshot("50-litigation")

    before = flows._grid_rows(s)
    if not _open_add_litigation(s, res, say, step):
        return
    s.screenshot("51-add-litigation")

    for spec in LITIGATION_PASSES:
        say(f"  {spec.name} …")
        entries = _fill_pass(s, res, screen, spec, dry_run, say, step)
        res.entries.extend(e.as_dict() for e in entries)

    # Whatever shape the form took, it is finished with. A modal left sitting
    # over the grid would hide the screen from the next thing that runs — and
    # a grid behind one is not visible, so it would count as empty below.
    W.Filler(session=s, screen=screen)._close_modal()

    if dry_run:
        return

    # The grid gaining a row is the honest test of whether Save worked, and a
    # better one than the absence of an error toast: this form closes on save
    # whether or not it stored anything.
    s.page.wait_for_timeout(1200)
    after = flows._grid_rows(s)
    if after > before:
        res.checks.append(R.passed(
            "The litigation record appears in the list",
            detail=f"The grid went from {before} to {after} row(s).",
            screen=screen))
    else:
        res.checks.append(R.failed(
            "The litigation record appears in the list",
            expected=f"one more row than the {before} before saving",
            actual=f"still {after} row(s)",
            detail="Save raised no error but the grid did not grow, so "
                   "nothing was stored.",
            evidence=[s.screenshot("52-litigation-not-listed")],
            screen=screen))


def _open_add_litigation(s: Session, res: CaseFlowResult, say, step) -> bool:
    """
    Open the Litigation entry form.

    Two shapes, tried in that order, because which one a deployment renders is
    configuration rather than code:

      1. a NAMED button — "Add Litigation", or a plain "Add" — found by label
         through the crawler's own denylist, exactly as the facility's and the
         observation's openers are;
      2. the grid's bare '+', which carries no label at all and so cannot be
         found that way. widgets.add_row is the one thing that clicks it, and
         it is the same call the linkage tables use.

    Either way the form is confirmed by its FIELDS appearing, never by a
    `.modal.show` — the panel shape opens no modal, and requiring one reported
    a missing form on a screen that had just opened one.
    """
    screen = SCREEN_LABEL[LITIGATION]
    f = W.Filler(session=s, screen=screen)
    before = set(f.field_labels(60))
    # The field only the entry form has. A deployment that renders the form
    # inline beneath the grid needs no opener at all, and pressing 'Add' on
    # one of those would open a second form over the first.
    anchors = LITIGATION_PASSES[0].fields[0].labels
    opened = ""
    btns: list[dict] = []

    if _form_is_open(f, before, anchors):
        opened = "the form already on the screen"
    else:
        btns = cr.collect_action_buttons(s.page)
        opener = None
        for want in _ADD_LITIGATION:
            opener = next((b for b in btns
                           if want in (b["label"] or "").strip().lower()),
                          None)
            if opener is not None:
                break

        if opener is not None and cr._click_stamped(
                s.page, "data-crawl-action", opener["index"], timeout=8000):
            cr.wait_until_settled(s.page, s.recorder, timeout_ms=20000,
                                  stable_polls=2)
            cr.stamp_content_root(s.page)
            s.page.wait_for_timeout(1200)
            opened = opener["label"]
            step(f"click {opened}")

        # Nothing named, or the button did nothing: the '+' above the grid.
        #
        # add_row's return value is deliberately not what decides this. It
        # reports False unless a '.modal.show' arrives, which is right for a
        # linkage table and wrong here — an entry form that opens as a panel
        # opens no modal, and the click still worked. So the '+' is counted
        # first, clicked, and then judged by whether a form appeared.
        if not _form_is_open(f, before, anchors) and _add_control_count(s):
            f.add_row()
            s.page.wait_for_timeout(900)
            opened = opened or "the grid's '+'"
            step("click the '+' on Litigation")

    if not _form_is_open(f, before, anchors):
        res.checks.append(R.failed(
            "A litigation record can be added",
            expected="'+' (or an 'Add Litigation' button) opens the "
                     "litigation entry form",
            actual=(f"{opened!r} opened nothing with fields on it" if opened
                    else "no '+' and no add button was found. Available: "
                         + (", ".join(b["label"] for b in btns[:8])
                            or "no buttons")),
            detail="Without the form there is nothing to fill, so nothing on "
                   "this screen could be exercised.",
            evidence=[s.screenshot("51-no-add-litigation")], screen=screen))
        return False

    labels = f.field_labels(60)
    say(f"  the litigation form shows {len(labels)} field(s)")
    res.checks.append(R.passed(
        "A litigation record can be added",
        detail=f"{opened or 'the add control'} opened a form with "
               f"{len(labels)} field(s): " + ", ".join(labels[:8]),
        screen=screen))
    return True


def _form_is_open(f: W.Filler, before: set[str],
                  anchors: tuple | list = ()) -> bool:
    """
    Is an entry form showing that was not showing before?

    Three ways of telling, cheapest and most specific first:

      - one of `anchors` is on screen. These are the names of a field only the
        form has, so it is the one test that says which form opened rather
        than merely that something did;
      - a field is showing that was not showing a moment ago. True of a panel,
        which renders beside the grid where the grid's own filter boxes are
        fields too;
      - the scope is a modal, which by itself means a dialog is up.
    """
    now = f.field_labels(60)
    if not now:
        return False
    seen = {_norm(x) for x in now}
    if any(_norm(a) in seen for a in anchors):
        return True
    return bool(set(now) - before) or f._scope() != "body"


# --------------------------------------------------------------------------
# Shariah Comments
#
# One editor and a Save. There is deliberately nothing here but opening the
# screen and running the pass: no opener to find, no grid to count, no modal
# to close. Everything that makes the result trustworthy — validation read
# before saving, the value read back off the form before saving, the save
# itself, and the round trip afterwards — already lives in _fill_pass and in
# verify_case_entries, and adding a second path for a one-field screen would
# be two places to keep in step for no gain.
# --------------------------------------------------------------------------

def _do_shariah_comments(s: Session, res: CaseFlowResult, dry_run: bool,
                         say, step) -> None:
    screen = SCREEN_LABEL[SHARIAH_COMMENTS]
    _open_screen(s, screen, step)
    s.screenshot("60-shariah-comments")

    for spec in SHARIAH_PASSES:
        say(f"  {spec.name} …")
        entries = _fill_pass(s, res, screen, spec, dry_run, say, step)
        res.entries.extend(e.as_dict() for e in entries)


# --------------------------------------------------------------------------
# RMG Memo  (key: crmd_note)
#
# Eight editors and a Save, and so exactly the same code as the one-editor
# screen above: open it, run the pass. The difference between the two screens
# is entirely in what CRMD_NOTE_PASSES declares, which is where a difference
# between two screens belongs.
# --------------------------------------------------------------------------

def _do_crmd_note(s: Session, res: CaseFlowResult, dry_run: bool,
                  say, step) -> None:
    screen = SCREEN_LABEL[CRMD_NOTE]
    _open_screen(s, screen, step)
    s.screenshot("70-crmd-note")

    for spec in CRMD_NOTE_PASSES:
        say(f"  {spec.name} …")
        entries = _fill_pass(s, res, screen, spec, dry_run, say, step)
        res.entries.extend(e.as_dict() for e in entries)


# --------------------------------------------------------------------------
# Credit Memorandum
#
# Thirty-three editors, and still the same two steps: open it, run the pass.
# That the largest screen in the case needs no code of its own beyond its
# field list is the point of _fill_pass, and the reason a screen of eight and
# a screen of thirty-three cannot drift apart in how they are checked.
#
# It is the slowest of these runs by a distance — every editor is typed into
# rather than filled, then read back before saving — which is why it is its
# own button rather than something the other screens wait for.
# --------------------------------------------------------------------------

def _do_credit_memorandum(s: Session, res: CaseFlowResult, dry_run: bool,
                          say, step) -> None:
    screen = SCREEN_LABEL[CREDIT_MEMORANDUM]
    _open_screen(s, screen, step)
    s.screenshot("80-credit-memorandum")

    for spec in CREDIT_MEMORANDUM_PASSES:
        say(f"  {spec.name} — {len(spec.fields)} authored editor(s) …")
        entries = _fill_pass(s, res, screen, spec, dry_run, say, step)
        res.entries.extend(e.as_dict() for e in entries)


# --------------------------------------------------------------------------
# Group Review
#
# Seven editors and a Save, and so the same two steps again. The one thing to
# know about this screen is that it does not exist on every case — see the
# note above GROUP_REVIEW_PASSES — and _open_screen reports that as BLOCKED
# with the sidebar's actual contents, which is the answer an operator needs.
# --------------------------------------------------------------------------

def _do_group_review(s: Session, res: CaseFlowResult, dry_run: bool,
                     say, step) -> None:
    screen = SCREEN_LABEL[GROUP_REVIEW]
    _open_screen(s, screen, step)
    s.screenshot("90-group-review")

    for spec in GROUP_REVIEW_PASSES:
        say(f"  {spec.name} …")
        entries = _fill_pass(s, res, screen, spec, dry_run, say, step)
        res.entries.extend(e.as_dict() for e in entries)


# --------------------------------------------------------------------------
# Business Performance, both sub-menus
#
# The only thing these two add to the pattern is that they are nested, which
# _open_sub_screen handles. They are otherwise the same two steps as every
# screen above: open it, run the pass. Keeping them as two functions over two
# field lists rather than one function over both sub-menus is what gives each
# its own Save result and its own round trip.
# --------------------------------------------------------------------------

def _bp_row_index(names: list[str], label: str) -> Optional[int]:
    """Which row of the rendered grid is this metric, if any."""
    want = _norm(label)
    for i, n in enumerate(names):
        if _norm(n) == want:
            return i
    return None


def _bp_columns(grid: dict) -> list[str]:
    """
    The column names, or plain numbers when the screen's headers cannot be
    read with confidence.

    Never guessed at: widgets refuses to return a name that repeats a row
    name or that reads like the period above it, and a number is a worse
    label but not a wrong one.
    """
    wide = int(grid.get("wide") or 0)
    cols = [str(c) for c in (grid.get("columns") or [])]
    if len(cols) != wide or len(set(cols)) != len(cols):
        return [f"column {i + 1}" for i in range(wide)]
    return cols


def _commit_and_report(f: W.Filler, res: CaseFlowResult, screen: str,
                       name: str, shot: str, say, step,
                       save_label: str = "Save") -> None:
    """
    Press Save and say what the application did about it.

    Deliberately a second copy of the tail of _fill_pass rather than a shared
    one. _fill_pass interleaves this with its LINKAGE branches — counting a
    grid's rows before and after, closing a modal on three separate paths —
    and pulling those apart would mean rewriting the path that every existing
    screen saves through, for a screen that has no modal and no row to count.
    The check NAMES are identical on purpose, so a report reads the same
    whichever path produced it, and test_safety pins them together.
    """
    try:
        f.commit(save_label)
        step(f"save {name}")
    except (W.FillError, W.WriteRefused) as exc:
        res.checks.append(R.failed(
            f"{name} is saved",
            expected=f"a {save_label} button on {name}",
            actual=str(exc)[:250],
            evidence=[f.session.screenshot(f"{shot}-nosave")], screen=screen))
        step(f"save {name}", R.FAIL, str(exc)[:140])
        return

    msgs = f.messages()
    still_invalid = f.unsatisfied()
    refused_rules = flows._refused_rules(f.session)
    saved_shot = f.session.screenshot(f"{shot}-saved")

    if msgs["bad"] or refused_rules:
        res.checks.append(R.failed(
            f"{name} is saved",
            expected="the app confirms it was saved",
            actual=" | ".join((msgs["bad"] + refused_rules)[:3]),
            detail="The application refused the save. Its own validation rules "
                   "are quoted above — a rule marked 'blocks the save' means "
                   "the values entered have to change, not the automation."
            if refused_rules else "",
            evidence=[saved_shot], screen=screen))
        for r in refused_rules:
            say(f"    !! refused: {r}")
        return

    if still_invalid:
        res.checks.append(R.failed(
            f"{name} is saved",
            expected="Save stores the values and clears the form's messages",
            actual=f"the form is still showing {len(still_invalid)} unmet "
                   f"rule(s): " + " | ".join(still_invalid[:4]),
            detail="Save was refused, so nothing was written for this pass.",
            evidence=[saved_shot], screen=screen))
        return

    if not f.entries:
        res.checks.append(R.Check(
            name=f"{name} is saved", status=R.BLOCKED,
            detail="Save reported no error, but not one figure could be "
                   "entered, so nothing was stored. See the blockers above.",
            evidence=[saved_shot], screen=screen))
        return

    res.checks.append(R.passed(
        f"{name} is saved",
        detail=f"{len(f.entries)} figure(s) entered and saved"
               + (f". {' | '.join(msgs['ok'][:2])}" if msgs["ok"] else "."),
        screen=screen))


def _bp_add_period(s: Session, res: CaseFlowResult, spec: BPSpec,
                   say, step) -> Optional[int]:
    """
    '+ Add' -> the four-field dialog -> Proceed, making a period to fill.

    Returns the INDEX of the block that was added, or None. The index is what
    everything downstream needs: the new period is a table of its own beside
    the ones already there, with its own boxes and its own Save.
    """
    screen = spec.name
    shot = _safe(spec.name)
    f = W.Filler(session=s, screen=screen, group=f"{spec.name} — add a period")

    before = f.period_blocks()
    had = int(before.get("count") or 0) if before.get("ok") else 0

    try:
        note = f.press_action("Add", settle_ms=15000)
        say(f"    {note}")
    except (W.FillError, W.WriteRefused) as exc:
        res.checks.append(R.failed(
            f"{spec.name}: a period can be added",
            expected="an '+ Add' button that opens the period dialog",
            actual=str(exc)[:200],
            evidence=[s.screenshot(f"{shot}-no-add")], screen=screen))
        return None

    s.page.wait_for_timeout(2000)
    if not _modal_open(s):
        res.checks.append(R.failed(
            f"{spec.name}: a period can be added",
            expected="a dialog asking for the type, year and dates",
            actual="no dialog opened",
            evidence=[s.screenshot(f"{shot}-no-dialog")], screen=screen))
        return None
    say(f"    the dialog opened: {_dialog_title(s, 1)!r}")

    # ---- its four required fields ------------------------------------
    for setter, label, value in (
            (f.choose, "Actual/Projected", BP_PERIOD_TYPE),
            (f.choose, "Year", BP_PERIOD_YEAR),
            (f.date, "From", BP_PERIOD_FROM),
            (f.date, "To", BP_PERIOD_TO)):
        try:
            e = setter(label, value)
            say(f"    {label} = {e.value!r}")
        except (W.FillError, Exception) as exc:      # noqa: BLE001
            res.checks.append(R.failed(
                f"{spec.name}: a period can be added",
                expected=f"{label} accepts {value!r}",
                actual=str(exc)[:200],
                evidence=[s.screenshot(f"{shot}-dialog-refused")],
                screen=screen))
            f.cancel_modal()
            return None

    outstanding = f.unsatisfied()
    try:
        f.commit("Proceed")
        step(f"add a {BP_PERIOD_YEAR} period to {spec.name}")
    except (W.FillError, W.WriteRefused) as exc:
        res.checks.append(R.failed(
            f"{spec.name}: a period can be added",
            expected="a Proceed button on the period dialog",
            actual=str(exc)[:200],
            evidence=[s.screenshot(f"{shot}-no-proceed")], screen=screen))
        f.cancel_modal()
        return None

    s.page.wait_for_timeout(3000)
    cr.wait_until_settled(s.page, s.recorder, timeout_ms=30000, stable_polls=2)
    cr.stamp_content_root(s.page)

    after = f.period_blocks()
    now = int(after.get("count") or 0) if after.get("ok") else 0
    s.screenshot(f"{shot}-period-added")

    if now <= had:
        res.checks.append(R.failed(
            f"{spec.name}: a period can be added",
            expected=f"a period beyond the {had} already on the screen",
            actual=f"the grid still shows {now}",
            detail=f"The dialog was given {BP_PERIOD_TYPE!r}, "
                   f"{BP_PERIOD_YEAR!r}, {BP_PERIOD_FROM} and {BP_PERIOD_TO}, "
                   f"and Proceed was pressed."
                   + (f" The dialog still reported: "
                      f"{' | '.join(outstanding[:3])}" if outstanding else ""),
            evidence=[s.screenshot(f"{shot}-no-period")], screen=screen))
        return None

    res.checks.append(R.passed(
        f"{spec.name}: a period can be added",
        detail=f"'+ Add' took {BP_PERIOD_TYPE!r} for {BP_PERIOD_YEAR} "
               f"({BP_PERIOD_FROM} to {BP_PERIOD_TO}) and Proceed added "
               f"period {now} of {now}. Proceed does not store it — only Save "
               f"does.", screen=screen))
    return now - 1


def _fill_bp_grid(s: Session, res: CaseFlowResult, spec: BPSpec,
                  dry_run: bool, say, step) -> list[W.Entry]:
    """
    Add a period to one Business Performance sub-menu, fill it, and save it.

    Not _fill_pass, because there is nothing here for _fill_pass to work on:
    it fills fields found by their labels, and this screen has no labelled
    fields at all. What it does have is a grid, so this walks the grid's rows
    by name and reports the same things in the same words — the rows it
    expected, what would not take a figure, what did not stay in its box,
    what the app computed, and whether Save took.

    WHAT IS NOT CHECKED, deliberately: the figures the application derives.
    Total Income, Annualized Yield and Net Yield are read-only and are left
    alone, and this run does NOT assert what they ought to come to. It reports
    that they are computed and what they show. Asking whether a percentage
    is arithmetically right is a question about the bank's formula, not about
    whether the screen stored what was typed into it, and the two were being
    conflated.
    """
    screen = spec.name
    shot = _safe(spec.name)
    f = W.Filler(session=s, screen=screen, group=spec.name)

    if dry_run:
        res.checks.append(R.blocked(
            f"{spec.name}: a period can be added",
            "Dry run: '+ Add' was not pressed, so no period was made and "
            "there was nothing to fill.", screen=screen))
        return []

    block = _bp_add_period(s, res, spec, say, step)
    if block is None:
        return []

    grid = f.period_blocks()
    if not grid.get("ok"):
        res.checks.append(R.Check(
            name=f"{spec.name} shows its figures in a grid",
            status=R.BLOCKED,
            expected="a table of row names beside a table of input boxes",
            actual=str(grid.get("why") or "the grid could not be read")[:250],
            detail="Nothing was entered. This screen keeps its row names in "
                   "one table and its boxes in another, and the two could not "
                   "be paired up — so which box belongs to which figure "
                   "cannot be told, and guessing would write a number into "
                   "the wrong row.",
            evidence=[s.screenshot(f"{shot}-nogrid")], screen=screen))
        return []

    names = [str(n) for n in (grid.get("names") or [])]
    blocks = grid.get("blocks") or []
    wide = int(blocks[block].get("wide") or 0) if block < len(blocks) else 0
    # The period just added has no column captions of its own in the DOM; the
    # panel's header carries them, and they read the same for every period.
    cols = _bp_columns(grid) or [f"column {i + 1}" for i in range(wide)]
    cols = cols[:wide] if len(cols) >= wide else \
        cols + [f"column {i + 1}" for i in range(len(cols), wide)]
    say(f"    the grid shows {len(names)} row(s) and {wide} column(s): "
        + ", ".join(cols))

    # ---- are the rows the specification names the rows on the screen? ---
    missing = [r.label for r in BP_ROWS if _bp_row_index(names, r.label) is None]
    extra = [n for n in names
             if not any(_norm(n) == _norm(r.label) for r in BP_ROWS)]
    # A row whose name matches once normalised but is spelled differently is
    # worth saying out loud without calling it a failure — 'Average Funded
    # Outstanding)' carries a stray bracket on one of the two sub-menus.
    spelling = [f"{r.label!r} renders as {names[i]!r}"
                for r in BP_ROWS
                if (i := _bp_row_index(names, r.label)) is not None
                and names[i].strip() != r.label]
    if missing:
        res.checks.append(R.Check(
            name=f"{spec.name}: every row in the specification is on the screen",
            status=R.BLOCKED,
            expected=f"all {len(BP_ROWS)} row(s)",
            actual=f"{len(missing)} could not be found: " + ", ".join(missing)
                   + (f"; the screen also shows {', '.join(extra)}"
                      if extra else ""),
            detail="These were not filled, so nothing below says anything "
                   "about them.",
            evidence=[s.screenshot(f"{shot}-missing")], screen=screen))
    else:
        res.checks.append(R.passed(
            f"{spec.name}: every row in the specification is on the screen",
            detail=f"All {len(BP_ROWS)} row(s) present, in {wide} column(s) "
                   f"({', '.join(cols)})."
                   + (" Spelled differently on the screen: "
                      + "; ".join(spelling) + "." if spelling else ""),
            screen=screen))

    if not wide:
        res.checks.append(R.Check(
            name=f"{spec.name} offers a period to enter figures against",
            status=R.BLOCKED,
            detail="The grid has row names but no columns of input boxes, so "
                   "there is nowhere to enter anything. A period may have to "
                   "be added with the screen's own '+ Add' first.",
            evidence=[s.screenshot(f"{shot}-nocolumns")], screen=screen))
        return []

    # ---- enter what is writable, note what the app computes -------------
    typed: dict[tuple[int, int], str] = {}
    app_computed: dict[tuple[int, int], str] = {}
    refused: list[str] = []

    for i, row in enumerate(BP_ROWS):
        if _bp_row_index(names, row.label) is None:
            continue
        for col in range(wide):
            # Scoped to the period this run added. The periods already on the
            # case are other people's figures and are not touched.
            cell = f.block_cell(row.label, col, block)
            if not cell.get("ok"):
                refused.append(f"{row.label} — {cols[col]}: "
                               f"{cell.get('why')}")
                continue
            # The DOM decides, not the specification: a box the application
            # has made read-only is one it fills itself, whatever this file
            # expected. Where the two disagree, the disagreement is reported
            # below rather than resolved in favour of either.
            if cell.get("readonly"):
                app_computed[(i, col)] = str(cell.get("value") or "")
                continue
            value = spec.value(i, col)
            try:
                e = f.set_block_cell(row.label, value, col, block, cols[col])
                typed[(i, col)] = value
                say(f"    {e.label} = {e.value!r}")
            except (W.FillError, Exception) as exc:  # noqa: BLE001
                refused.append(f"{row.label} — {cols[col]}: {str(exc)[:120]}")
                say(f"    !! {row.label} — {cols[col]}: {str(exc)[:110]}")

    if refused:
        res.checks.append(R.Check(
            name=f"{spec.name}: every writable figure accepts a value",
            status=R.BLOCKED,
            actual=f"{len(refused)} would not take one",
            detail=" | ".join(refused[:6]), screen=screen))

    # ---- does the app compute the rows it is expected to? ---------------
    expected_computed = {r.label for r in BP_ROWS if r.computed}
    actually = {BP_ROWS[i].label for (i, _c) in app_computed}
    if expected_computed == actually:
        res.checks.append(R.passed(
            f"{spec.name}: the application works out its own totals",
            detail="Read-only, and so left alone: "
                   + ", ".join(sorted(actually)) + ". What those figures come "
                   "to is the application's own arithmetic and is NOT judged "
                   "here — only that it fills them itself and that the "
                   "figures this run typed survive the round trip.",
            screen=screen))
    else:
        res.checks.append(R.Check(
            name=f"{spec.name}: the application works out its own totals",
            status=R.BLOCKED,
            expected="read-only: " + ", ".join(sorted(expected_computed)),
            actual="read-only: " + (", ".join(sorted(actually)) or "none"),
            detail="Which figures the application derives is not what this "
                   "screen was written against. Nothing was typed into a "
                   "read-only box either way.",
            screen=screen))

    say(f"    {len(typed)} figure(s) entered, {len(app_computed)} left to the "
        f"application")
    s.screenshot(f"{shot}-filled")

    # ---- the form's own rules, before saving ----------------------------
    outstanding = f.unsatisfied()
    if outstanding:
        res.checks.append(R.failed(
            f"{spec.name} has no unmet field rules before saving",
            expected="no validation message left on the form",
            actual=f"{len(outstanding)} outstanding: "
                   + " | ".join(outstanding[:5]),
            detail="The form will refuse to save while these stand, so the "
                   "figures below were entered but not stored.",
            evidence=[s.screenshot(f"{shot}-invalid")], screen=screen))
        say(f"    !! {len(outstanding)} unmet rule(s)")
    else:
        res.checks.append(R.passed(
            f"{spec.name} has no unmet field rules before saving",
            detail=f"{len(typed)} figure(s) entered, the form reports no "
                   f"outstanding validation.", screen=screen))

    # ---- read every figure back BEFORE saving ---------------------------
    held: list[str] = []
    for (i, col), value in typed.items():
        shown = f.block_cell_value(BP_ROWS[i].label, col, block)
        if not flows._same_value(value, shown, "number"):
            held.append(f"{BP_ROWS[i].label} — {cols[col]}: entered {value!r}, "
                        f"the box now shows {shown!r}")
    if held:
        res.checks.append(R.failed(
            f"{spec.name} holds every value that was typed into it",
            expected="each box still shows what was entered, before Save",
            actual=f"{len(held)} did not: " + " | ".join(held[:6]),
            detail="The value did not stay in the box, so anything the round "
                   "trip says about these figures is about the form, not "
                   "about what the application stored.",
            evidence=[s.screenshot(f"{shot}-not-held")], screen=screen))
        say(f"    !! {len(held)} figure(s) did not hold their value")
    elif typed:
        res.checks.append(R.passed(
            f"{spec.name} holds every value that was typed into it",
            detail=f"All {len(typed)} figure(s) still read back what was "
                   f"entered, so anything missing after Save was dropped by "
                   f"the application rather than never typed.", screen=screen))

    if dry_run:
        say(f"  {spec.name}: dry run — not saving")
        res.checks.append(R.blocked(
            f"{spec.name} is saved",
            f"Dry run: {len(typed)} figure(s) were entered and abandoned.",
            screen=screen))
        return f.entries

    # Each period carries its OWN Save, side by side. _commit_and_report goes
    # through commit(), which takes the first enabled match in document order
    # — the LEFTMOST, belonging to a period that was already on the case — so
    # this saves by block position instead. Everything else it reports is the
    # same, so the reporting half is reused as it stands.
    try:
        note = f.commit_block(block)
        say(f"    {note}")
        step(f"save {spec.name}")
        saved_error = ""
    except (W.FillError, W.WriteRefused) as exc:
        saved_error = str(exc)[:200]

    if saved_error:
        res.checks.append(R.failed(
            f"{spec.name} is saved",
            expected=f"a Save button for the period this run added",
            actual=saved_error,
            evidence=[s.screenshot(f"{shot}-no-save")], screen=screen))
        return f.entries

    errors = s.error_banners()
    invalid = s.validation_messages()
    saved_shot = s.screenshot(f"{shot}-saved")
    if errors:
        res.checks.append(R.failed(
            f"{spec.name} is saved",
            expected="Save stores the period",
            actual="the screen reported: " + " | ".join(errors[:3]),
            evidence=[saved_shot], screen=screen))
    elif invalid:
        res.checks.append(R.blocked(
            f"{spec.name} is saved",
            "Save raised no error but the form still shows: "
            + " | ".join(invalid[:3])
            + ". The round trip below is the real answer.",
            evidence=[saved_shot], screen=screen))
    else:
        res.checks.append(R.passed(
            f"{spec.name} is saved",
            detail=f"{len(typed)} figure(s) were entered into the "
                   f"{BP_PERIOD_YEAR} period and Save reported no error.",
            screen=screen))
    return f.entries


# --------------------------------------------------------------------------
# Relationship with Other Banks / FIs
# --------------------------------------------------------------------------

_ADD_BANK = ["add", "add bank", "add relationship", "new bank"]


def _bank_rows(s: Session) -> int:
    """How many bank relationships the list page is showing."""
    return flows._grid_rows(s)


def _modal_open(s: Session) -> bool:
    try:
        return bool(s.page.locator(".modal.show, .modal.in").count())
    except Exception:  # noqa: BLE001
        return False


def _check_required_refused(f: W.Filler, s: Session, res: CaseFlowResult,
                            say, step, *, name: str, commit_label: str,
                            wanted: list[str], screen: str,
                            in_dialog: bool, shot: str) -> bool:
    """
    Commit an EMPTY form and report whether the app refused it, and how.

    The negative check, generalised. Everything else in this suite asks
    whether the application accepts good data; this asks whether it refuses
    incomplete data, and that is worth asking because the answer is invisible
    from the happy path — a form that quietly saved an empty record would
    still let every later step pass.

    `wanted` is the messages the screen is expected to show. They are matched
    loosely: a message that says the same thing in different words is still
    the form refusing, so what is reported is which of the expected rules
    were and were not quoted, rather than an exact-string comparison that
    would fail on a wording change.

    Returns True if the form is still open and usable afterwards.
    """
    try:
        f.commit(commit_label)
    except (W.FillError, W.WriteRefused) as exc:
        res.checks.append(R.passed(
            name,
            detail=f"With nothing filled, {commit_label} could not be pressed "
                   f"at all: {str(exc)[:160]}", screen=screen))
        step(f"check {name[:40]}", note=f"{commit_label} was not pressable")
        return True

    # Read IMMEDIATELY and then keep polling. A single read at a fixed delay
    # made this flaky — it quoted both rules on one run and reported "said
    # nothing this run could read" on the next, against the same screen
    # behaving the same way. commit() waits for the page to settle before it
    # returns, and some of these messages have been rendered and cleared
    # again by then, so the first read has to happen before any further wait.
    # These messages arrive LATE rather than early — reading straight after
    # commit() found nothing where a single read 1.8s later had quoted both
    # rules, so the settle is not what clears them. Wait first, then keep
    # polling: that catches a message whenever it turns up, and stops as soon
    # as every expected rule has been seen.
    s.page.wait_for_timeout(1500)
    said: list[str] = []
    oks: list[str] = []
    for attempt in range(10):
        msgs = f.messages()
        for m in msgs["fields"] + msgs["bad"] + f.unsatisfied():
            if m not in said:
                said.append(m)
        for m in msgs["ok"]:
            if m not in oks:
                oks.append(m)
        if len([w for w in wanted
                if any(_norm(w) in _norm(m) or _norm(m) in _norm(w)
                       for m in said)]) == len(wanted):
            break
        s.page.wait_for_timeout(300)

    # A dialog that is gone was accepted. A PAGE cannot be judged that way —
    # it stays put either way — so its acceptance shows up as a success
    # message instead. Without this the page case could only ever report
    # "refused, silently", even when the commit had gone through.
    still_open = _modal_open(s) if in_dialog else not oks
    if oks and not said:
        res.checks.append(R.failed(
            name,
            expected=f"{commit_label} refused, showing: " + " | ".join(wanted),
            actual=f"{commit_label} was accepted with nothing filled: "
                   + " | ".join(oks[:3]),
            detail="A record may have been stored with none of its required "
                   "fields answered.",
            evidence=[s.screenshot(shot)], screen=screen))
        step(f"check {name[:40]}", R.FAIL, f"{commit_label} was accepted")
        return False

    quoted = [w for w in wanted
              if any(_norm(w) in _norm(m) or _norm(m) in _norm(w)
                     for m in said)]
    unquoted = [w for w in wanted if w not in quoted]

    if quoted and still_open and len(quoted) == len(wanted):
        res.checks.append(R.passed(
            name,
            detail=f"All {len(wanted)} rule(s) were shown and the commit was "
                   f"refused: " + " | ".join(said[:6]) + ". Nothing was "
                   f"created.", screen=screen))
        say(f"    the form refused it, quoting all {len(wanted)} rule(s)")
        step(f"check {name[:40]}", note=" | ".join(quoted)[:120])
        return True

    if said and still_open:
        res.checks.append(R.Check(
            name=name, status=R.BLOCKED,
            expected="these rule(s): " + " | ".join(wanted),
            actual="the form refused the commit and said: "
                   + " | ".join(said[:6]),
            detail=f"The commit WAS refused, which is the important part. But "
                   f"{len(unquoted)} of the expected message(s) were not "
                   f"shown — {', '.join(unquoted)} — so whether the operator "
                   f"would be told about those fields cannot be confirmed "
                   f"from here.",
            evidence=[s.screenshot(shot)], screen=screen))
        step(f"check {name[:40]}", R.BLOCKED,
             f"{len(unquoted)} rule(s) not shown")
        return True

    if still_open:
        res.checks.append(R.Check(
            name=name, status=R.BLOCKED,
            expected="these rule(s): " + " | ".join(wanted),
            actual="the form stayed open but said nothing this run could read",
            detail="The commit was refused, but with no message to quote, "
                   "whether the operator would know why cannot be told.",
            evidence=[s.screenshot(shot)], screen=screen))
        step(f"check {name[:40]}", R.BLOCKED, "no message read")
        return True

    res.checks.append(R.failed(
        name,
        expected=f"{commit_label} refused, showing: " + " | ".join(wanted),
        actual=f"the form closed, so {commit_label} was accepted with "
               f"nothing filled",
        detail="An incomplete record may have been created. Everything below "
               "is about whichever record the run then landed on.",
        evidence=[s.screenshot(shot)], screen=screen))
    step(f"check {name[:40]}", R.FAIL, f"{commit_label} was accepted")
    return False


def _check_bank_name_required(f: W.Filler, s: Session, res: CaseFlowResult,
                              say, step) -> None:
    """
    Press Proceed with no bank chosen, and report what the form does.

    A negative check, and the only one in this suite: everything else here
    asks whether the application accepts good data, and this asks whether it
    refuses incomplete data. It is worth having precisely because the answer
    is not obvious from the happy path — a dialog that quietly created an
    empty relationship would still let every later step pass.

    Nothing is created if the rule holds, and the check proves that rather
    than assuming it: the list's row count is read before and after.
    """
    screen = SCREEN_LABEL[BANK_RELATIONSHIPS]
    name = "Bank Name is required before the relationship can be created"

    try:
        f.commit("Proceed")
    except (W.FillError, W.WriteRefused) as exc:
        # A Proceed that cannot even be pressed is a different answer, and an
        # acceptable one: the form is refusing at the button instead.
        res.checks.append(R.passed(
            name,
            detail=f"With no bank chosen, Proceed could not be pressed at "
                   f"all: {str(exc)[:160]}", screen=screen))
        step("check Bank Name is required", note="Proceed was not pressable")
        return

    s.page.wait_for_timeout(1500)
    msgs = f.messages()
    unmet = f.unsatisfied()
    said = [m for m in (msgs["fields"] + msgs["bad"] + unmet)
            if "bank name" in _norm(m)]
    still_open = _modal_open(s)

    if said and still_open:
        res.checks.append(R.passed(
            name,
            detail=f"Proceed with an empty Bank Name was refused and the form "
                   f"said so: {said[0]!r}. The dialog stayed open, so nothing "
                   f"was created.", screen=screen))
        say(f"    the form refused it: {said[0]!r}")
        step("check Bank Name is required", note=said[0][:120])
        return

    if still_open:
        res.checks.append(R.Check(
            name=name, status=R.BLOCKED,
            expected=f"a message such as {_BANK_REQUIRED!r}",
            actual="the dialog stayed open but said nothing this run could "
                   "read",
            detail="Proceed was refused, which is the important part — but "
                   "with no message to quote, whether the operator would know "
                   "why cannot be told from here.",
            evidence=[s.screenshot("rel-02-empty-proceed")], screen=screen))
        step("check Bank Name is required", R.BLOCKED, "no message read")
        return

    res.checks.append(R.failed(
        name,
        expected=f"Proceed refused, with {_BANK_REQUIRED!r} on the dialog",
        actual="the dialog closed, so Proceed was accepted with no bank "
               "chosen",
        detail="An incomplete relationship may have been created. Everything "
               "below is about whichever record the run then landed on.",
        evidence=[s.screenshot("rel-02-empty-proceed")], screen=screen))
    step("check Bank Name is required", R.FAIL, "Proceed was accepted")


def _add_bank_relationship(s: Session, res: CaseFlowResult, say, step,
                           dry_run: bool) -> Optional[str]:
    """
    Step 1: '+ Add' -> the Bank Name dialog -> Proceed -> the record's page.

    Nothing on this screen is enterable until a relationship exists, so — as
    with a facility — this is not a preliminary but the step that creates the
    thing being filled.
    """
    screen = SCREEN_LABEL[BANK_RELATIONSHIPS]
    f = W.Filler(session=s, screen=screen, group="Add a bank relationship")
    before = _bank_rows(s)

    btns = cr.collect_action_buttons(s.page)
    opener = None
    for want in _ADD_BANK:
        opener = next((b for b in btns
                       if (b["label"] or "").strip().lower() == want), None)
        if opener is not None:
            break

    if opener is not None:
        if not cr._click_stamped(s.page, "data-crawl-action",
                                 opener["index"], timeout=8000):
            res.checks.append(R.failed(
                "A bank relationship can be added",
                expected=f"the '{opener['label']}' button opens the "
                         f"Bank Name dialog",
                actual="the button would not click",
                evidence=[s.screenshot("rel-01-add-stuck")], screen=screen))
            return None
        cr.wait_until_settled(s.page, s.recorder, timeout_ms=20000,
                              stable_polls=2)
        cr.stamp_content_root(s.page)
        step(f"click {opener['label']}")
    elif not f.add_row(0):
        res.checks.append(R.failed(
            "A bank relationship can be added",
            expected="an Add button or a '+' on the list page",
            actual="none was found. Available: "
                   + (", ".join(b["label"] for b in btns[:8]) or "no buttons"),
            detail="Without it there is no relationship to fill, so the rest "
                   "of this screen could not be exercised.",
            evidence=[s.screenshot("rel-01-no-add")], screen=screen))
        return None
    else:
        step("open the Bank Name dialog from the '+'")

    s.page.wait_for_timeout(1000)
    title = _dialog_title(s, 1)
    if not _modal_open(s):
        res.checks.append(R.failed(
            "A bank relationship can be added",
            expected="a dialog asking for the Bank Name",
            actual="no dialog opened",
            evidence=[s.screenshot("rel-01-no-dialog")], screen=screen))
        return None
    say(f"    the dialog opened: {title!r}")
    s.screenshot("rel-01-add-dialog")

    # ---- the required-field rule, BEFORE anything is chosen -----------
    _check_bank_name_required(f, s, res, say, step)
    if not _modal_open(s):
        # Proceed was accepted on the empty form. That is already reported as
        # a failure; re-open the dialog so the rest of the screen can still
        # be exercised.
        say("    the dialog closed, re-opening it to continue")
        return _reopen_bank_dialog(s, res, say, step, f)

    return _choose_bank_and_proceed(s, res, say, step, f, before, dry_run)


def _reopen_bank_dialog(s: Session, res: CaseFlowResult, say, step,
                        f: W.Filler) -> Optional[str]:
    """After an unexpectedly accepted Proceed, get back to a usable dialog."""
    _open_screen(s, SCREEN_LABEL[BANK_RELATIONSHIPS], step)
    s.page.wait_for_timeout(2000)
    btns = cr.collect_action_buttons(s.page)
    opener = next((b for b in btns
                   if (b["label"] or "").strip().lower() in _ADD_BANK), None)
    if opener is None:
        return None
    cr._click_stamped(s.page, "data-crawl-action", opener["index"],
                      timeout=8000)
    cr.wait_until_settled(s.page, s.recorder, timeout_ms=20000, stable_polls=2)
    s.page.wait_for_timeout(1000)
    if not _modal_open(s):
        return None
    return _choose_bank_and_proceed(s, res, say, step, f, _bank_rows(s), False)


def _choose_bank_and_proceed(s: Session, res: CaseFlowResult, say, step,
                             f: W.Filler, before: int,
                             dry_run: bool) -> Optional[str]:
    """Choose the bank, press Proceed, and land on the record's own page."""
    screen = SCREEN_LABEL[BANK_RELATIONSHIPS]

    try:
        e = _set_one(f, _p(["Bank Name"], BANK_NAME, optional=False))
        chosen = e.value
        say(f"    Bank Name = {chosen!r}")
        step("choose the bank", note=chosen[:120])
    except (W.FillError, Exception) as exc:  # noqa: BLE001
        res.checks.append(R.failed(
            "A bank relationship can be added",
            expected=f"Bank Name accepts {BANK_NAME!r}",
            actual=str(exc)[:250],
            evidence=[s.screenshot("rel-03-no-bank")], screen=screen))
        f.cancel_modal()
        return None

    if dry_run:
        say("  dry run — not pressing Proceed, so no relationship is created")
        cancelled = f.cancel_modal()
        res.checks.append(R.blocked(
            "A bank relationship can be added",
            f"Dry run: {chosen!r} was chosen and the dialog was dismissed "
            f"with {cancelled or 'its close control'} instead of Proceed, so "
            f"no relationship was created and its detail page could not be "
            f"reached. Re-run without the dry-run option to fill it.",
            screen=screen))
        res.entries.extend(x.as_dict() for x in f.entries)
        return None

    try:
        f.commit("Proceed")
        step("click Proceed")
    except (W.FillError, W.WriteRefused) as exc:
        res.checks.append(R.failed(
            "A bank relationship can be added",
            expected="a Proceed button on the Bank Name dialog",
            actual=str(exc)[:250],
            evidence=[s.screenshot("rel-03-no-proceed")], screen=screen))
        f.cancel_modal()
        return None

    cr.wait_until_settled(s.page, s.recorder, timeout_ms=30000, stable_polls=2)
    s.page.wait_for_timeout(1800)
    cr.stamp_content_root(s.page)
    s.screenshot("rel-04-after-proceed")

    refused = flows._refused_rules(s)
    bad = f.messages()["bad"]
    if refused or bad:
        res.checks.append(R.failed(
            "A bank relationship can be added",
            expected="Proceed creates the relationship and opens it",
            actual=" | ".join((bad + refused)[:3]),
            evidence=[s.screenshot("rel-04-refused")], screen=screen))
        f.cancel_modal()
        return None

    # Proceed should have left us on the record's own page. If it did not, the
    # relationship is in the list and has to be opened.
    if not _looks_like_detail(s):
        if s.open_row_detail(0):
            step("open the new relationship from the list")
            s.page.wait_for_timeout(2000)
        else:
            res.checks.append(R.blocked(
                "The new bank relationship opens for editing",
                f"Proceed raised no error but did not open a detail page, and "
                f"no row on the list could be opened. The list showed "
                f"{before} row(s) before.",
                evidence=[s.screenshot("rel-04-no-detail")], screen=screen))
            return None

    res.checks.append(R.passed(
        "A bank relationship can be added",
        detail=f"Chose {chosen!r} and pressed Proceed; the relationship "
               f"opened for editing.", screen=screen))
    res.entries.extend(x.as_dict() for x in f.entries)
    return chosen


def _bank_summary(f: W.Filler) -> dict:
    """The five read-only figures under the LIMITS table, as shown."""
    return {label: f.value_of(label) for label in BANK_SUMMARY_FIELDS}


def _check_selected_bank(s: Session, res: CaseFlowResult, chosen: str) -> None:
    """Step 2: the record's own page shows the bank chosen in the dialog."""
    screen = SCREEN_LABEL[BANK_RELATIONSHIPS]
    f = W.Filler(session=s, screen=screen)
    shown = f.value_of("Select Bank")
    name = "Select Bank shows the bank chosen in the dialog"
    if not shown:
        res.checks.append(R.Check(
            name=name, status=R.BLOCKED,
            detail="'Select Bank' has no readable value on the record's page, "
                   "so whether the choice carried through cannot be told.",
            evidence=[s.screenshot("rel-05-no-select-bank")], screen=screen))
        return
    if flows._same_value(chosen, shown, "dropdown"):
        res.checks.append(R.passed(
            name, detail=f"Chose {chosen!r} in the dialog, the record shows "
                         f"{shown!r}.", screen=screen))
    else:
        res.checks.append(R.failed(
            name,
            expected=f"{chosen!r} — the bank chosen in the dialog",
            actual=f"{shown!r} — what the record shows",
            detail="The record was created against a different bank from the "
                   "one asked for.",
            evidence=[s.screenshot("rel-05-wrong-bank")], screen=screen))


def _check_bank_summary(s: Session, res: CaseFlowResult, before: dict,
                        after: dict, entered: dict, say) -> None:
    """
    Do the five read-only figures reflect the limit row that was just added?

    Four of them are a plain sum of the limit rows, and this record has
    exactly one row — the one this run entered — so on a record created by
    this run the expected value is simply what was typed. That is why the
    record is created rather than reused: on a record that already had rows,
    'is it the sum' and 'is it the last row' look identical.

    Wallet Share (%) is the exception and is REPORTED rather than asserted.
    It compares this bank against the others on the case, so it cannot be
    derived from this record alone, and inventing a rule for it would be
    guessing. Its before-and-after values are quoted for confirmation.
    """
    screen = SCREEN_LABEL[BANK_RELATIONSHIPS]
    shot = s.screenshot("rel-08-summary")

    for label in BANK_SUMMARY_FIELDS:
        source = BANK_SUMMARY_FROM.get(label)
        shown = after.get(label, "")
        name = f"{label} reflects the limit row that was added"

        if source is None:
            # Wallet Share (%): flagged, not asserted.
            res.checks.append(R.Check(
                name=f"{label} is populated after the limit was added",
                status=R.PASS if shown.strip() else R.BLOCKED,
                detail=(f"Shows {shown!r} (it was {before.get(label, '')!r} "
                        f"before the limit was added). This figure compares "
                        f"this bank against the others on the case, so it "
                        f"cannot be derived from this record alone — the "
                        f"expected calculation needs confirming before it can "
                        f"be asserted."
                        if shown.strip() else
                        f"Empty after the limit was added; it was "
                        f"{before.get(label, '')!r} before. Whether that is "
                        f"correct depends on a calculation this run cannot "
                        f"derive from one record."),
                evidence=[shot], screen=screen))
            continue

        typed = entered.get(source, "")
        if not typed:
            res.checks.append(R.Check(
                name=name, status=R.BLOCKED,
                detail=f"{source!r} was not entered on the limit row, so "
                       f"there is nothing to expect here. The screen shows "
                       f"{shown!r}.", screen=screen))
            continue

        if shown and flows._same_value(typed, shown, "number"):
            res.checks.append(R.passed(
                name,
                detail=f"The one limit row on this record has {source} = "
                       f"{typed!r}, and {label} shows {shown!r}.",
                screen=screen))
        else:
            res.checks.append(R.failed(
                name,
                expected=f"{typed!r} — the {source} of the only limit row on "
                         f"this record",
                actual=f"{shown!r} — what the screen shows"
                       if shown else "empty",
                detail=f"This relationship was created by this run and has "
                       f"exactly one limit row, so this figure should equal "
                       f"that row's {source}. It was "
                       f"{before.get(label, '')!r} before the row was added.",
                evidence=[shot], screen=screen))

    say("    checked the five figures under the LIMITS table")


def _do_bank_relationships(s: Session, res: CaseFlowResult, dry_run: bool,
                           say, step) -> None:
    """
    The whole screen: create a relationship, add a limit, fill the rest, save.

    Ordered as the specification orders it, and the order matters: the limit
    row has to exist before the figures it feeds can be checked, and the page
    is saved once at the end so the round trip is about one Save rather than
    about whichever of several took.
    """
    screen = SCREEN_LABEL[BANK_RELATIONSHIPS]
    _open_screen(s, screen, step)
    s.screenshot("rel-00-list")

    chosen = _add_bank_relationship(s, res, say, step, dry_run)
    if not chosen:
        return

    # ---- Step 2 -------------------------------------------------------
    _check_selected_bank(s, res, chosen)

    # ---- Step 3: the LIMITS section's own '+' -------------------------
    before = _bank_summary(W.Filler(session=s, screen=screen))
    say(f"  {BANK_LIMIT_PASS.name} …")
    limit_entries = _fill_pass(s, res, screen, BANK_LIMIT_PASS, dry_run, say,
                               step, discover=False)
    res.entries.extend(e.as_dict() for e in limit_entries)
    entered = {e.label: e.value for e in limit_entries}
    s.page.wait_for_timeout(1200)

    # The Limits dialog is dismissed with Escape by the shared add-row path,
    # and Escape has closed more than a dialog on this application before —
    # so the record's own page is confirmed to still be there before anything
    # is read off it.
    f = W.Filler(session=s, screen=screen)
    if not f.value_of("Select Bank") and not _looks_like_detail(s):
        res.checks.append(R.blocked(
            "The record's page survives the Limits dialog",
            "After the Limits dialog closed, the relationship's own page was "
            "no longer readable, so the fields below could not be filled.",
            evidence=[s.screenshot("rel-07-lost-detail")], screen=screen))
        return
    res.checks.append(R.passed(
        "The record's page survives the Limits dialog",
        detail="The relationship's page is still open and readable after the "
               "Limits dialog was dismissed.", screen=screen))

    if not dry_run:
        _check_bank_summary(s, res, before, _bank_summary(f), entered, say)

    # ---- Steps 4 and 5: the rest of the page, then Save ---------------
    say(f"  {BANK_DETAIL_PASS.name} …")
    entries = _fill_pass(s, res, screen, BANK_DETAIL_PASS, dry_run, say, step)
    res.entries.extend(e.as_dict() for e in entries)


# --------------------------------------------------------------------------
# eCIB Details
# --------------------------------------------------------------------------

def _open_past_dues(f: W.Filler) -> str:
    """
    Open the record's 'View Past Dues' screen.

    Deliberately NOT part of the happy path: it navigates away from the page
    being filled, and pressing it before Save would abandon everything typed.
    Kept because the specification asks for a reusable helper for it, and a
    helper written now is written against a screen somebody has just looked
    at.

    Not a commit — it opens a related sub-screen — so it goes through the
    action-button path rather than through commit(), which would refuse it.
    """
    s = f.session
    btns = cr.collect_action_buttons(s.page)
    match = next((b for b in btns
                  if "past due" in (b["label"] or "").lower()), None)
    if match is None:
        raise W.FillError(
            "This record has no 'View Past Dues' control. Available: "
            + (", ".join(b["label"] for b in btns[:8]) or "none"))
    if not cr._click_stamped(s.page, "data-crawl-action", match["index"],
                             timeout=8000):
        raise W.FillError("'View Past Dues' would not click.")
    cr.wait_until_settled(s.page, s.recorder, timeout_ms=20000, stable_polls=2)
    cr.stamp_content_root(s.page)
    return f"opened {match['label']}"


def _add_ecib_record(s: Session, res: CaseFlowResult, say, step,
                     dry_run: bool) -> bool:
    """
    Step 1: Add -> the three-field dialog -> Proceed -> the record's page.

    Proceed here does NOT persist a record, unlike the bank dialog — it only
    opens the page. Nothing exists on this screen until Save, which is why a
    dry run can press Proceed and still leave the case untouched.
    """
    screen = SCREEN_LABEL[ECIB_DETAILS]
    f = W.Filler(session=s, screen=screen, group="Add an eCIB record")

    btns = cr.collect_action_buttons(s.page)
    opener = next((b for b in btns
                   if (b["label"] or "").strip().lower() == "add"), None)
    if opener is not None:
        if not cr._click_stamped(s.page, "data-crawl-action",
                                 opener["index"], timeout=8000):
            res.checks.append(R.failed(
                "An eCIB record can be added",
                expected="the Add button opens the eCIB Details dialog",
                actual="the button would not click",
                evidence=[s.screenshot("ecib-01-add-stuck")], screen=screen))
            return False
        cr.wait_until_settled(s.page, s.recorder, timeout_ms=20000,
                              stable_polls=2)
        cr.stamp_content_root(s.page)
        step("click Add")
    elif not f.add_row(0):
        res.checks.append(R.failed(
            "An eCIB record can be added",
            expected="an Add button or a '+' on the eCIB Details list",
            actual="none was found. Available: "
                   + (", ".join(b["label"] for b in btns[:8]) or "no buttons"),
            evidence=[s.screenshot("ecib-01-no-add")], screen=screen))
        return False
    else:
        step("open the eCIB dialog from the '+'")

    s.page.wait_for_timeout(1200)
    if not _modal_open(s):
        res.checks.append(R.failed(
            "An eCIB record can be added",
            expected="a dialog asking for the eCIB type, code and date",
            actual="no dialog opened",
            evidence=[s.screenshot("ecib-01-no-dialog")], screen=screen))
        return False
    say(f"    the dialog opened: {_dialog_title(s, 1)!r}")
    s.screenshot("ecib-02-dialog")

    # ---- the three required rules, BEFORE anything is filled ---------
    _check_required_refused(
        f, s, res, say, step,
        name="the eCIB dialog refuses an empty form",
        commit_label="Proceed", wanted=ECIB_DIALOG_RULES, screen=screen,
        in_dialog=True, shot="ecib-03-empty-proceed")

    if not _modal_open(s):
        res.checks.append(R.blocked(
            "An eCIB record can be added",
            "The dialog closed when Proceed was pressed on the empty form, so "
            "the record's page could not be reached from it.",
            evidence=[s.screenshot("ecib-03-dialog-gone")], screen=screen))
        return False

    # ---- fill it and Proceed -----------------------------------------
    for spec_field in (_p(["eCIB Type"], ECIB_TYPE, optional=False),
                       _t(["Borrower Code", "eCIB Borrower Code"],
                          ECIB_BORROWER_CODE, optional=False),
                       _d(["eCIB Report Date"], ECIB_REPORT_DATE,
                          optional=False)):
        try:
            e = _set_one(f, spec_field)
            say(f"    {e.label} = {e.value!r}")
        except (W.FillError, Exception) as exc:  # noqa: BLE001
            res.checks.append(R.failed(
                "An eCIB record can be added",
                expected=f"{spec_field.label} accepts a value",
                actual=str(exc)[:250],
                evidence=[s.screenshot("ecib-04-dialog-refused")],
                screen=screen))
            f.cancel_modal()
            return False
    # The dialog calls it 'Borrower Code' and the record's page calls the same
    # thing 'eCIB Borrower Code'. Recorded under the page's name, because the
    # round trip looks the value up by its label — under the dialog's name it
    # came back as "no labelled field on this screen", which is true and
    # useless.
    for e in f.entries:
        if _norm(e.label) == _norm("Borrower Code"):
            e.label = "eCIB Borrower Code"
    res.entries.extend(e.as_dict() for e in f.entries)
    step("fill the eCIB dialog")

    outstanding = f.unsatisfied()
    if outstanding:
        res.checks.append(R.failed(
            "the eCIB dialog accepts the values it asked for",
            expected="no validation message left once all three are filled",
            actual=f"{len(outstanding)} still outstanding: "
                   + " | ".join(outstanding[:4]),
            evidence=[s.screenshot("ecib-04-still-invalid")], screen=screen))
    else:
        res.checks.append(R.passed(
            "the eCIB dialog accepts the values it asked for",
            detail="All three were filled and the dialog reports no "
                   "outstanding validation.", screen=screen))

    if dry_run:
        cancelled = f.cancel_modal()
        say("  dry run — the dialog was cancelled rather than proceeded")
        res.checks.append(R.blocked(
            "An eCIB record can be added",
            f"Dry run: the three fields were filled and the dialog dismissed "
            f"with {cancelled or 'its close control'} instead of Proceed, so "
            f"the record's page was not opened. Re-run without the dry-run "
            f"option to fill it.", screen=screen))
        return False

    try:
        f.commit("Proceed")
        step("click Proceed")
    except (W.FillError, W.WriteRefused) as exc:
        res.checks.append(R.failed(
            "An eCIB record can be added",
            expected="a Proceed button on the eCIB dialog",
            actual=str(exc)[:250],
            evidence=[s.screenshot("ecib-04-no-proceed")], screen=screen))
        f.cancel_modal()
        return False

    cr.wait_until_settled(s.page, s.recorder, timeout_ms=30000, stable_polls=2)
    s.page.wait_for_timeout(2000)
    cr.stamp_content_root(s.page)
    s.screenshot("ecib-05-record-page")

    if not _looks_like_detail(s):
        res.checks.append(R.failed(
            "An eCIB record can be added",
            expected="Proceed opens the Customer eCIB Details page",
            actual=f"the run is still on {s.page.url}",
            evidence=[s.screenshot("ecib-05-no-page")], screen=screen))
        return False

    res.checks.append(R.passed(
        "An eCIB record can be added",
        detail=f"The dialog was filled with {ECIB_TYPE!r}, "
               f"{ECIB_BORROWER_CODE!r} and {ECIB_REPORT_DATE}, and Proceed "
               f"opened the record's page.", screen=screen))
    return True


def _check_ecib_carried(s: Session, res: CaseFlowResult, say) -> None:
    """
    Step 2: does the record's page show what the dialog was given?

    All three arrive, and none is re-entered: the borrower code and the report
    date arrive read-only, and the eCIB Type arrives set but with an empty
    option list, so asserting is the only thing available as well as the right
    thing to do.
    """
    screen = SCREEN_LABEL[ECIB_DETAILS]
    f = W.Filler(session=s, screen=screen)

    for label, (expected, kind) in ECIB_CARRIED.items():
        shown = f.value_of(label)
        name = f"{label} is carried through from the dialog"
        if shown and flows._same_value(expected, shown, kind):
            res.checks.append(R.passed(
                name,
                detail=f"The dialog was given {expected!r}; the record's page "
                       f"shows {shown!r}, and the run leaves it alone.",
                screen=screen))
        elif shown:
            res.checks.append(R.failed(
                name,
                expected=f"{expected!r} — what the dialog was given",
                actual=f"{shown!r} — what the record's page shows",
                evidence=[s.screenshot("ecib-06-not-carried")], screen=screen))
        else:
            res.checks.append(R.failed(
                name,
                expected=f"{expected!r}, pre-filled on the record's page",
                actual="the field is empty",
                detail="The specification expects this to arrive from the "
                       "dialog already filled.",
                evidence=[s.screenshot("ecib-06-not-carried")], screen=screen))
    say("    checked what the dialog carried through to the record's page")


def _check_ecib_sums(s: Session, res: CaseFlowResult, entered: dict,
                     say, when: str) -> None:
    """
    The seven read-only figures, against the arithmetic read off the screen.

    Every source is a value this run entered, so nothing here depends on
    another computed figure — a wrong Fund Based cannot excuse a wrong Total.
    Run twice: once with the page still on screen, and again after the record
    has been re-opened, because a figure that is right until reloaded and
    wrong afterwards is a different defect from one that was never computed.
    """
    screen = SCREEN_LABEL[ECIB_DETAILS]
    f = W.Filler(session=s, screen=screen)
    shot = s.screenshot(f"ecib-sums-{_safe(when)}")

    for label, sources in ECIB_COMPUTED.items():
        name = f"{label} is the sum of its own fields ({when})"
        shown = f.value_of(label)
        parts = [flows._as_number(entered.get(src, "")) for src in sources]

        if any(p is None for p in parts):
            missing = [src for src, p in zip(sources, parts) if p is None]
            res.checks.append(R.Check(
                name=name, status=R.BLOCKED,
                detail=f"{', '.join(missing)} was not entered, so there is "
                       f"nothing to expect here. The screen shows {shown!r}.",
                screen=screen))
            continue

        want = sum(parts)
        got = flows._as_number(shown)
        sum_text = " + ".join(f"{p:,.0f}" for p in parts) + f" = {want:,.0f}"

        if got is None:
            res.checks.append(R.failed(
                name,
                expected=f"{want:,.0f} — {sum_text}",
                actual=f"{shown!r}" if shown else "empty",
                detail="The application computes this field, and it is not "
                       "showing a number.",
                evidence=[shot], screen=screen))
        elif abs(got - want) <= 0.01:
            res.checks.append(R.passed(
                name, detail=f"{sum_text}, and the screen shows {shown!r}.",
                screen=screen))
        else:
            res.checks.append(R.failed(
                name,
                expected=f"{want:,.0f} — {sum_text}",
                actual=f"{shown!r}",
                detail="The figure the application derives does not agree "
                       "with the fields it derives it from. The rule was read "
                       "off this screen's own behaviour before anything else "
                       "was entered.",
                evidence=[shot], screen=screen))
    say(f"    checked the {len(ECIB_COMPUTED)} computed figures ({when})")


def _check_ecib_exposure(s: Session, res: CaseFlowResult, entered: dict,
                         say) -> None:
    """
    Did 'Exposure As On' take the DAY it was given?

    This field has its own check because the round trip alone reports it as a
    generic "value did not survive", and the diagnosis is worth more than
    that. Established by typing every format this suite knows into it, on a
    saved record, and reading each back:

        26/08/2026        -> January 1st, 2026
        08/26/2026        -> January 1st, 2008   (the first number as a year)
        2026-08-26        -> January 1st, 2026
        August 26, 2026   -> August 1st, 2026    (the month parses!)
        26-08-2026        -> '26-08-2026'        (kept as unparsed text)

    'August 26' becoming 'August 1st' is the telling one: the month is
    understood and the DAY is forced to the 1st. And the format that appears
    to work — 26-08-2026 — is simply the box keeping text the app never
    parsed, which then saves as 1 January. The eCIB Report Date on the same
    record, set through the dialog, stores its exact day.

    So the check asks two things: is the box holding a date the application
    has actually parsed, and is it the day that was asked for.
    """
    screen = SCREEN_LABEL[ECIB_DETAILS]
    label = "Exposure As On"
    typed = entered.get(label, "")
    if not typed:
        return

    f = W.Filler(session=s, screen=screen)
    shown = f.value_of(label)
    shot = s.screenshot("ecib-exposure")
    want = ECIB_EXPOSURE_DATE

    # A date the app has parsed comes back in ITS format — the same shape the
    # report date beside it uses. A box still holding exactly what was typed
    # has been parsed by nothing.
    unparsed = (flows._norm_value(shown) == flows._norm_value(typed)
                and "-" in shown)
    day_shown = re.findall(r"\b(\d{1,2})(?:st|nd|rd|th)\b", shown)
    took_day = bool(day_shown) and int(day_shown[0]) == want.day

    if took_day:
        res.checks.append(R.passed(
            f"{label} takes the day it was given",
            detail=f"Asked for {want}, and the field shows {shown!r}.",
            screen=screen))
    elif unparsed:
        res.checks.append(R.failed(
            f"{label} takes the day it was given",
            expected=f"a date the application has parsed, showing "
                     f"{want.day} as its day",
            actual=f"{shown!r} — exactly the text that was typed, so nothing "
                   f"has parsed it",
            detail="The box keeps text the application does not understand "
                   "rather than rejecting it, and a Save then stores 1 "
                   "January. Every other format tried is parsed but has its "
                   "day forced to the 1st — 'August 26, 2026' becomes "
                   "'August 1st, 2026' — while eCIB Report Date on this same "
                   "record stores its exact day.",
            evidence=[shot], screen=screen))
    else:
        res.checks.append(R.failed(
            f"{label} takes the day it was given",
            expected=f"{want.day} as the day, from {want}",
            actual=f"{shown!r}",
            detail="The month and year are parsed but the day is not: this "
                   "field forces the day to the 1st. eCIB Report Date on the "
                   "same record stores its exact day.",
            evidence=[shot], screen=screen))
    say(f"    {label} reads back as {shown!r}")


def _report_ecib_gates(s: Session, res: CaseFlowResult, entered: dict) -> None:
    """
    Say which sections were opened, rather than leaving it to be inferred.

    Twenty-three of this screen's fields are read-only until their section's
    Yes/No gate is answered. A run that failed to answer a gate would report
    twenty-three unsettable fields and no reason, so the gates get a check of
    their own.
    """
    screen = SCREEN_LABEL[ECIB_DETAILS]
    answered = [g for g in ECIB_GATES if entered.get(g)]
    if len(answered) == len(ECIB_GATES):
        res.checks.append(R.passed(
            "every gated section was opened before its fields were filled",
            detail="Answered " + ", ".join(
                f"{g} = {entered[g]!r}" for g in answered)
            + ". Each of these opens fields that are read-only until it is "
              "answered.", screen=screen))
    else:
        missing = [g for g in ECIB_GATES if g not in answered]
        res.checks.append(R.Check(
            name="every gated section was opened before its fields were filled",
            status=R.BLOCKED,
            expected="all three section gates answered",
            actual=f"{len(answered)} of {len(ECIB_GATES)} were: "
                   + (", ".join(answered) or "none"),
            detail=f"{', '.join(missing)} was not answered, so the fields "
                   f"under it stay read-only and anything reported about them "
                   f"is about the gate, not about those fields.",
            screen=screen))


def _do_ecib_add(s: Session, res: CaseFlowResult, dry_run: bool,
                 say, step) -> None:
    """
    The '+ Add' route: create the record, fill fifty-odd fields, save.

    Ordered as the specification orders it, with one departure that matters:
    each section's Yes/No gate is filled immediately before the fields it
    opens. See the note above ECIB_PASS.
    """
    screen = SCREEN_LABEL[ECIB_DETAILS]
    _open_screen(s, screen, step)
    s.screenshot("ecib-00-list")

    if not _add_ecib_record(s, res, say, step, dry_run):
        return

    # ---- Step 2: what the dialog carried through ----------------------
    _check_ecib_carried(s, res, say)

    # ---- the page's own required rules, before anything is filled ----
    _check_required_refused(
        W.Filler(session=s, screen=screen), s, res, say, step,
        name="the record's page refuses a Save with its required fields empty",
        commit_label="Save", wanted=ECIB_PAGE_RULES, screen=screen,
        in_dialog=False, shot="ecib-07-empty-save")

    # ---- Steps 3 to 7, then Save -------------------------------------
    say(f"  {ECIB_PASS.name} — {len(ECIB_PASS.fields)} authored field(s) …")
    # The three fields the dialog set are asserted, not re-entered — so
    # discovery has to be told to leave them alone. Without this it found the
    # report date writable on the record's page and typed a date of its own
    # over the key data the record had just been created with.
    entries = _fill_pass(s, res, screen, ECIB_PASS, dry_run, say, step,
                         leave_alone=list(ECIB_CARRIED))
    res.entries.extend(e.as_dict() for e in entries)

    entered = {e.label: e.value for e in entries}
    _report_ecib_gates(s, res, entered)
    if not dry_run:
        _check_ecib_exposure(s, res, entered, say)
        _check_ecib_sums(s, res, entered, say, "before reloading")


def _do_ecib_details(s: Session, res: CaseFlowResult, dry_run: bool,
                     say, step) -> None:
    """
    One check, both routes: the Add dialog first, then the Upload one.

    The same shape as _do_business_performance, and for the same reasons. Two
    ways of creating a record on ONE sidebar screen is one thing an operator
    wants checked, not two — but they stay separate in the result, so the
    report can say which route stored what.

    Each route is wrapped on its own. They are independent: the Add flow types
    a record and Saves it, the Upload flow hands the application a PDF and
    lets it build one. An Add dialog that will not open must not stop the
    uploads being checked, and a PDF the parser chokes on must not discard a
    Save that already worked — either way round, abandoning the second route
    would report nothing about a route that may be perfectly fine.

    Order is deliberate, not incidental — see the note above
    ECIB_UPLOAD_LABEL.
    """
    for name, fn in (("Add route", _do_ecib_add),
                     ("Upload route", _do_ecib_upload)):
        try:
            fn(s, res, dry_run, say, step)
        except NavigationError as e:
            res.checks.append(R.blocked(
                f"the eCIB {name} can be run", str(e),
                evidence=[s.screenshot(f"ecib-{_safe(name)}-unreachable")],
                screen=SCREEN_LABEL[ECIB_DETAILS]))
            step(f"run the eCIB {name}", R.BLOCKED, str(e)[:150])
            say(f"    !! {str(e)[:140]}")
        except Exception as e:      # noqa: BLE001
            res.checks.append(R.blocked(
                f"the eCIB {name} can be run", str(e)[:300],
                evidence=[s.screenshot(f"ecib-{_safe(name)}-error")],
                screen=SCREEN_LABEL[ECIB_DETAILS]))
            step(f"run the eCIB {name}", R.BLOCKED, str(e)[:150])
            say(f"    !! {str(e)[:140]}")


# --------------------------------------------------------------------------
# PR Checklist
# --------------------------------------------------------------------------

def _pr_norm(text: str) -> str:
    """Factor labels are long regulatory sentences; compare them loosely."""
    return _norm(text)


def _logged_in_user(s: Session) -> str:
    """
    Who the application thinks is signed in.

    Read off the shell rather than taken from the .env username, because the
    grid records a PERSON ('Usama Khan') and the credentials are a login id
    ('Uk_9080'). Comparing the grid against the login id would fail on a
    correct row.
    """
    # The selector matters and the obvious one is WRONG: '.user-name' on this
    # shell is a grid filter and reads 'All', which then compared 'All'
    # against 'Usama Khan' and failed a correct row. The name lives in the
    # avatar's dropdown card. Anything that comes back as the login id, or as
    # a one-word filter value, is rejected rather than used.
    try:
        got = (s.page.evaluate(
            """() => {
                const txt = e => e ? (e.textContent || '').trim() : '';
                for (const sel of ['li.dropdown-user .dropdown-menu p.card-title',
                                   '.dropdown-user .card-title',
                                   '.dropdown-user-name']) {
                    const t = txt(document.querySelector(sel));
                    if (t && t.length < 60) return t;
                }
                return '';
            }""") or "").strip()
    except PWError:
        return ""

    if not got or _pr_norm(got) == _pr_norm(crawler_config.USERNAME):
        return ""
    # A person's name, not a filter. 'All' is the one this actually hit.
    if " " not in got:
        return ""
    return got


def _looks_like_today(shown: str) -> bool:
    """Is a rendered date string today's date, however the app spells it?"""
    if not shown:
        return False
    today = date.today()
    for fmt in ("%B %d, %Y", "%d-%b-%Y", "%Y-%m-%d", "%d/%m/%Y", "%b %d, %Y"):
        try:
            if datetime.strptime(shown.strip()[:len(shown.strip())],
                                 fmt).date() == today:
                return True
        except ValueError:
            continue
    # Last resort: the app sometimes appends a time to the date.
    head = shown.strip().split(" ")[0:3]
    try:
        return datetime.strptime(" ".join(head), "%B %d, %Y").date() == today
    except ValueError:
        return False


def _pr_log_rows(s: Session) -> list[dict]:
    """The PR RISK RATING LOG as a list of {column: text}."""
    try:
        return s.page.evaluate(
            """() => {
                const txt = e => (e.textContent || '').trim().replace(/\\s+/g, ' ');
                const vis = e => e && (e.offsetParent !== null
                                       || e.getClientRects().length);
                const root = document.querySelector('[data-crawl-root]')
                             || document.body;
                for (const t of root.querySelectorAll('table')) {
                    if (!vis(t)) continue;
                    const heads = [...t.querySelectorAll('th')].map(txt);
                    if (!heads.some(h => /perform by/i.test(h))) continue;
                    return [...t.querySelectorAll('tbody tr')].filter(vis)
                        .map(tr => {
                            const tds = [...tr.querySelectorAll('td')].map(txt);
                            const o = {};
                            heads.forEach((h, i) => o[h] = tds[i] || '');
                            return o;
                        });
                }
                return [];
            }""") or []
    except PWError:
        return []


def _check_pr_log(s: Session, res: CaseFlowResult, say,
                  when: str) -> list[dict]:
    """Step 1: the log grid is there and carries the columns it should."""
    screen = SCREEN_LABEL[PR_CHECKLIST]
    rows = _pr_log_rows(s)
    shot = s.screenshot(f"pr-log-{_safe(when)}")

    heads = list(rows[0]) if rows else []
    if not heads:
        try:
            heads = s.page.evaluate(
                """() => {
                    const txt = e => (e.textContent||'').trim().replace(/\\s+/g,' ');
                    const r = document.querySelector('[data-crawl-root]')
                              || document.body;
                    for (const t of r.querySelectorAll('table')) {
                        const h = [...t.querySelectorAll('th')].map(txt);
                        if (h.some(x => /perform by/i.test(x))) return h;
                    }
                    return [];
                }""") or []
        except PWError:
            heads = []

    missing = [c for c in PR_LOG_COLUMNS
               if not any(_pr_norm(c) == _pr_norm(h) for h in heads)]
    name = f"the PR Risk Rating Log shows its columns ({when})"
    if heads and not missing:
        res.checks.append(R.passed(
            name,
            detail=f"All {len(PR_LOG_COLUMNS)} columns are present and the "
                   f"grid holds {len(rows)} row(s).", screen=screen))
    elif heads:
        res.checks.append(R.failed(
            name,
            expected=", ".join(PR_LOG_COLUMNS),
            actual=", ".join(heads) or "no columns",
            detail=f"Missing: {', '.join(missing)}.",
            evidence=[shot], screen=screen))
    else:
        res.checks.append(R.blocked(
            name, "No PR Risk Rating Log grid is on this screen.",
            evidence=[shot], screen=screen))
    say(f"    the log holds {len(rows)} row(s) ({when})")
    return rows


@contextlib.contextmanager
def _driving(s: Session, page):
    """
    Point the Session at another tab for the duration of a block.

    Everything in widgets reads session.page, so swapping it is what lets the
    whole toolkit — set_factor, press_action, commit, screenshot — work on the
    PR popup without a parallel implementation. Restored on the way out even
    if the block raises, because the run still has to get back to the case's
    own tab to check the log grid.
    """
    original = s._page
    s._page = page
    try:
        yield page
    finally:
        s._page = original


def _open_pr_form(s: Session, res: CaseFlowResult, say, step):
    """
    Step 1: press 'Perform PR' and catch the checklist form.

    THE FORM IS NOT IN THIS TAB. 'Perform PR' opens a different application on
    a different machine — an ASP.NET page,
    10.11.12.10/NationalBankInternal-RiskRating-Dev/.../pgPRNew.aspx — in a
    NEW TAB, carrying the case and the employee id in its query string. The
    Angular page left behind does not change by one character, which is why
    watching it for a form finds nothing however long it waits.

    Returns the popup Page, or None.
    """
    screen = SCREEN_LABEL[PR_CHECKLIST]
    f = W.Filler(session=s, screen=screen, group="Perform a PR")

    btns = cr.collect_action_buttons(s.page)
    labels = [b["label"] for b in btns]
    if not any("perform" in (l or "").lower() for l in labels):
        res.checks.append(R.failed(
            "the PR Checklist offers a 'Perform PR' control",
            expected="a 'Perform PR' button on the PR Checklist screen",
            actual="none was found. Available: "
                   + (", ".join(labels[:10]) or "no buttons"),
            evidence=[s.screenshot("pr-01-no-perform")], screen=screen))
        return None
    res.checks.append(R.passed(
        "the PR Checklist offers a 'Perform PR' control",
        detail="The button is present, visible and enabled.", screen=screen))

    popup = None
    try:
        with s.page.expect_popup(timeout=60000) as caught:
            f.press_action("Perform PR", settle_ms=5000)
        popup = caught.value
        step("click Perform PR")
    except (W.FillError, W.WriteRefused) as exc:
        res.checks.append(R.failed(
            "'Perform PR' opens the checklist form",
            expected="the PR checklist form opens",
            actual=str(exc)[:250],
            evidence=[s.screenshot("pr-01-perform-refused")], screen=screen))
        return None
    except PWError:
        # No popup within a minute. Fall back to scanning the context, in case
        # the tab opened without Playwright attributing it to this click.
        extra = [p for p in s.page.context.pages if p is not s.page]
        popup = extra[-1] if extra else None

    if popup is None:
        res.checks.append(R.failed(
            "'Perform PR' opens the checklist form",
            expected="a new tab holding the PR checklist",
            actual="no second tab opened within 60 seconds",
            detail="'Perform PR' opens the RiskRating application in a new "
                   "tab; the Angular page it is pressed from never changes. "
                   "No tab means the button's handler did not get as far as "
                   "opening one.",
            evidence=[s.screenshot("pr-02-no-popup")], screen=screen))
        return None

    try:
        popup.wait_for_load_state("domcontentloaded", timeout=60000)
        popup.wait_for_load_state("networkidle", timeout=45000)
    except PWError:
        pass
    popup.wait_for_timeout(3000)
    say(f"    the form opened in a new tab: {popup.url[:110]}")

    # ---- the write gate, against the host actually being typed into ----
    # Deliberately explicit rather than left to the first set_factor: a
    # refusal here names the host and stops before anything is touched.
    try:
        W.assert_writable(popup.url)
    except W.WriteRefused as exc:
        res.checks.append(R.blocked(
            "the PR checklist host is approved for data entry", str(exc),
            detail="The checklist is served by a different machine from the "
                   "rest of the application. Add its host to "
                   "LOS_ALLOWED_WRITE_HOSTS to allow the run to fill it.",
            screen=screen))
        return None

    with _driving(s, popup):
        rows = []
        for _ in range(10):
            rows = W.Filler(session=s, screen=screen).factor_rows()
            if rows:
                break
            popup.wait_for_timeout(2000)
        s.screenshot("pr-02-form")

    if not rows:
        res.checks.append(R.failed(
            "'Perform PR' opens the checklist form",
            expected="a form of PR Factor tables",
            actual=f"the tab opened ({popup.url[:80]}) but holds no factor "
                   f"table after 20 seconds",
            evidence=[s.screenshot("pr-02-no-form")], screen=screen))
        return None

    settable = [r for r in rows if not r.get("disabled")]
    res.checks.append(R.passed(
        "'Perform PR' opens the checklist form",
        detail=f"A new tab opened on {settings.host_of(popup.url)} holding "
               f"{len(rows)} PR factor(s) across "
               f"{len({r.get('section') for r in rows})} section(s); "
               f"{len(settable)} are settable and {len(rows) - len(settable)} "
               f"are frozen.", screen=screen))
    return popup


def _fill_pr_factors(s: Session, res: CaseFlowResult, dry_run: bool,
                     say, step) -> tuple[dict, list[dict]]:
    """
    Step 2: set every settable Factor Value; assert the frozen ones are frozen.

    Returns (chosen, skipped). Every factor gets its own check, because the
    brief asks for a per-factor report rather than one assert that hides the
    rest.
    """
    screen = SCREEN_LABEL[PR_CHECKLIST]
    f = W.Filler(session=s, screen=screen, group="PR factors")
    rows = f.factor_rows()
    chosen: dict = {}
    skipped: list[dict] = []

    say(f"  {len(rows)} PR factor(s) across "
        f"{len({r.get('section') for r in rows})} section(s) …")

    for r in rows:
        factor = r.get("factor") or ""
        section = r.get("section") or "(no section)"
        short = factor[:60] + ("…" if len(factor) > 60 else "")
        want = next((v for k, v in PR_FACTOR_VALUES.items()
                     if _pr_norm(k) in _pr_norm(factor)), None)

        # ---- the ones the application locks --------------------------
        expected_frozen = any(_pr_norm(p) in _pr_norm(factor)
                              for p in PR_FROZEN_FACTORS)
        if r.get("disabled"):
            skipped.append({"section": section, "factor": factor,
                            "why": "frozen field",
                            "shows": r.get("value") or ""})
            say(f"    skipped - frozen field: {short}")
            res.checks.append(R.passed(
                f"{short} is frozen, and is left alone",
                detail=f"[{section}] The application locks this Factor Value; "
                       f"it shows {r.get('value') or '(empty)'!r} and the run "
                       f"does not touch it."
                       + ("" if expected_frozen else
                          " The brief did not name this one as frozen, which "
                          "is worth a look."),
                screen=screen))
            continue

        if expected_frozen:
            res.checks.append(R.failed(
                f"{short} is frozen, and is left alone",
                expected="the Factor Value locked by the application",
                actual=f"a writable {r.get('kind')} control",
                detail=f"[{section}] The brief states this factor's value is "
                       f"greyed out. It is currently writable, which is a "
                       f"change in the screen rather than in the test.",
                evidence=[s.screenshot("pr-03-frozen-writable")],
                screen=screen))
            skipped.append({"section": section, "factor": factor,
                            "why": "expected frozen but writable",
                            "shows": r.get("value") or ""})
            continue

        if dry_run:
            continue

        # ---- the settable ones ---------------------------------------
        try:
            e = f.set_factor(factor, want)
            chosen[factor] = e.value
            origin = "pinned by the brief" if want else "first real option"
            say(f"    {short} = {e.value!r} ({origin})")
            res.checks.append(R.passed(
                f"{short} accepts a Factor Value",
                detail=f"[{section}] Set to {e.value!r} — {origin}."
                       + ("" if want else
                          " Nothing pins this factor, so the choice is "
                          "recorded here for review."),
                screen=screen))
        except W.FrozenField as exc:
            skipped.append({"section": section, "factor": factor,
                            "why": "frozen field", "shows": str(exc)[:120]})
            say(f"    skipped - frozen field: {short}")
        except (W.FillError, Exception) as exc:      # noqa: BLE001
            res.checks.append(R.failed(
                f"{short} accepts a Factor Value",
                expected="the dropdown offers a value and takes it",
                actual=str(exc)[:250],
                detail=f"[{section}]",
                evidence=[s.screenshot(f"pr-03-{_safe(short)}")],
                screen=screen))

    if not dry_run:
        res.entries.extend(
            W.Entry(label=k, value=v, kind="dropdown", screen=screen,
                    group="PR factors").as_dict()
            for k, v in chosen.items())
    step(f"fill {len(chosen)} PR factor(s)")

    # One line that says what the run actually did, so an operator does not
    # have to count checks to find out.
    res.checks.append(R.passed(
        "every PR factor on the page was accounted for",
        detail=f"{len(rows)} factor(s) found: {len(chosen)} set, "
               f"{len(skipped)} skipped. "
               + ("Skipped: " + "; ".join(
                   f"{x['factor'][:40]} ({x['why']})" for x in skipped)
                  if skipped else "None skipped."),
        screen=screen))

    # ---- did the form show the factors that were asked about? --------
    # Separate from the loop above on purpose. That loop can only report on
    # what rendered; this says whether anything expected FAILED to render,
    # which no amount of filling would reveal.
    seen = " | ".join(_pr_norm(r.get("factor") or "") for r in rows)
    absent = [w for w in PR_EXPECTED_FACTORS if _pr_norm(w) not in seen]
    name = "the form shows the PR factors the brief names"
    if not absent:
        res.checks.append(R.passed(
            name,
            detail=f"All {len(PR_EXPECTED_FACTORS)} named factor(s) are on "
                   f"the form, among {len(rows)} in total.", screen=screen))
    else:
        res.checks.append(R.failed(
            name,
            expected=f"all {len(PR_EXPECTED_FACTORS)} named factors on the "
                     f"form",
            actual=f"{len(absent)} of them are not on it: "
                   + "; ".join(absent),
            detail="The run fills every factor the page offers, so a factor "
                   "that never renders is invisible to the fill loop. This "
                   "check exists to catch exactly that.",
            evidence=[s.screenshot("pr-03-missing-factors")], screen=screen))
    return chosen, skipped


def _pr_snapshot(s: Session) -> dict:
    """
    What the application computed, keyed by factor.

    This is the 'expected' data for the verification leg, so it is read from
    the screen rather than derived: the whole point is to find out whether
    what the app showed after Generate is what it stored.
    """
    out = {}
    for r in W.Filler(session=s, screen=SCREEN_LABEL[PR_CHECKLIST]
                      ).factor_rows():
        cols = r.get("columns") or {}
        out[r.get("factor") or ""] = {
            "section": r.get("section") or "",
            "factor_value": r.get("value") or "",
            "aligned": bool(r.get("aligned", True)),
            "computed": {k: v for k, v in cols.items()
                         if any(_pr_norm(c) in _pr_norm(k)
                                for c in PR_SYSTEM_COLUMNS)},
        }
    return out


def _pr_generate(s: Session, res: CaseFlowResult, say, step) -> dict:
    """
    Step 3: Generate FIRST, then wait for the computed columns to populate.

    Returns the snapshot. Generate is an action rather than a commit — it
    computes, it does not store — so it goes through press_action, which
    still refuses anything on the NEVER list.
    """
    screen = SCREEN_LABEL[PR_CHECKLIST]
    f = W.Filler(session=s, screen=screen)

    before = _pr_snapshot(s)
    blank_before = sum(1 for v in before.values()
                       for c in v["computed"].values() if not c)

    try:
        note = f.press_action("Generate")
        say(f"    {note}")
        step("click Generate", note=note[:120])
    except (W.FillError, W.WriteRefused) as exc:
        res.checks.append(R.failed(
            "Generate computes the regulatory columns",
            expected="a Generate button on the PR checklist form",
            actual=str(exc)[:250],
            evidence=[s.screenshot("pr-04-no-generate")], screen=screen))
        return {}

    # Poll until the previously blank cells fill in.
    snap = {}
    for _ in range(12):
        s.page.wait_for_timeout(2500)
        try:
            cr.stamp_content_root(s.page)
        except PWError:
            pass
        snap = _pr_snapshot(s)
        filled = sum(1 for v in snap.values()
                     for c in v["computed"].values() if c)
        if filled:
            break

    shot = s.screenshot("pr-04-generated")
    filled = sum(1 for v in snap.values() for c in v["computed"].values() if c)
    total = sum(len(v["computed"]) for v in snap.values())

    if total and filled:
        res.checks.append(R.passed(
            "Generate computes the regulatory columns",
            detail=f"{filled} of {total} computed cell(s) across "
                   f"{len(snap)} factor(s) are populated. Before Generate, "
                   f"{blank_before} were blank.", screen=screen))
    elif total:
        res.checks.append(R.failed(
            "Generate computes the regulatory columns",
            expected=f"Regulatory Requirement / Actual Value / Compliant "
                     f"Status populated for {len(snap)} factor(s)",
            actual="every computed cell is still blank 30 seconds after "
                   "Generate was pressed",
            evidence=[shot], screen=screen))
    else:
        res.checks.append(R.blocked(
            "Generate computes the regulatory columns",
            "The form shows no Regulatory Requirement / Actual Value / "
            "Compliant Status columns to compute, so there was nothing to "
            "check.", evidence=[shot], screen=screen))
    return snap


def _pr_save(s: Session, res: CaseFlowResult, say, step) -> bool:
    """Step 3, second half: Save, AFTER Generate."""
    screen = SCREEN_LABEL[PR_CHECKLIST]
    f = W.Filler(session=s, screen=screen)
    try:
        f.commit("Save")
        step("click Save")
    except (W.FillError, W.WriteRefused) as exc:
        res.checks.append(R.failed(
            "the PR checklist is saved",
            expected="a Save button on the PR checklist form",
            actual=str(exc)[:250],
            evidence=[s.screenshot("pr-05-no-save")], screen=screen))
        return False

    errors = s.error_banners()
    invalid = s.validation_messages()
    shot = s.screenshot("pr-05-saved")
    if errors:
        res.checks.append(R.failed(
            "the PR checklist is saved",
            expected="Save stores the checklist",
            actual="the screen reported: " + " | ".join(errors[:3]),
            evidence=[shot], screen=screen))
        return False
    if invalid:
        res.checks.append(R.blocked(
            "the PR checklist is saved",
            "Save raised no error but the form still shows: "
            + " | ".join(invalid[:3])
            + ". Whether it stored is unclear; the log grid below is the real "
              "answer.", evidence=[shot], screen=screen))
        return True
    res.checks.append(R.passed(
        "the PR checklist is saved",
        detail="Save was pressed after Generate, and the screen reported no "
               "error.", screen=screen))
    return True


def _pr_edit(f: W.Filler) -> str:
    """
    Press 'Edit' on a saved PR checklist.

    Deliberately NOT part of the happy path, exactly as the brief asks — it
    is kept for an edit-flow test that does not exist yet. Written now
    because it is written against a screen somebody has just looked at, which
    is the only time this kind of helper is cheap to get right.
    """
    return f.press_action("Edit")


def _verify_pr_row(s: Session, res: CaseFlowResult, before: list[dict],
                   say, step) -> Optional[dict]:
    """
    Step 4a: a new row appears in the log, and its metadata is right.

    Returns the new row, or None.
    """
    screen = SCREEN_LABEL[PR_CHECKLIST]
    seen = {r.get("ID") for r in before}

    # The row does not land the instant Save returns. Measured: a run that
    # read the grid straight after saving saw the row count unchanged, and the
    # row it had just made turned up in the NEXT run's "before" reading. So
    # the screen is re-opened and re-read until a new id shows, rather than
    # judged on one look.
    after = []
    for attempt in range(6):
        _open_screen(s, screen, step)
        cr.wait_until_settled(s.page, s.recorder, timeout_ms=30000,
                              stable_polls=2)
        cr.stamp_content_root(s.page)
        after = _pr_log_rows(s)
        if any(r.get("ID") not in seen for r in after):
            break
        if attempt < 5:
            say(f"    the new row has not arrived yet "
                f"({len(after)} row(s)); re-reading the log")
            s.page.wait_for_timeout(5000)

    after = _check_pr_log(s, res, say, "after saving")
    shot = s.screenshot("pr-06-log-after")

    # A PR does not always ADD a row. Measured across two live runs on the
    # same case: the first took the log from 1 row to 2, the second left it at
    # 2 and rewrote the row it had already made. So the check is "the log
    # carries this run's PR", satisfied either way, and it SAYS which happened
    # — an app that silently overwrote a previous assessment would otherwise
    # look identical to one that recorded a new one.
    who = _logged_in_user(s)
    fresh = [r for r in after if r.get("ID") not in seen]

    def _mine(r: dict) -> bool:
        return (_looks_like_today(r.get("Performed On") or "")
                and (not who
                     or _pr_norm(who) == _pr_norm(r.get("Perform By") or "")))

    confirmed = bool(fresh)
    if fresh:
        row = fresh[0]
        res.checks.append(R.passed(
            "the saved PR appears in the PR Risk Rating Log",
            detail=f"Row ID {row.get('ID')} ({row.get('Request Type')}) was "
                   f"added. The log went from {len(before)} row(s) to "
                   f"{len(after)}.", screen=screen))
        say(f"    new log row: {row}")
    else:
        # NOT a pass, and deliberately not a failure either.
        #
        # This grid is slow to show a row it has just been given: a run that
        # polled it for half a minute saw no change, and the row it had made
        # turned up in the NEXT run's opening reading. So "no new row yet"
        # does not prove the save was lost.
        #
        # It must not be reported as a pass by matching some row carrying
        # today's date and this user, which is what an earlier version did —
        # on a case performed more than once in a day that matches a PREVIOUS
        # run's row, and would report success for a save that stored nothing.
        # There is no marker in the grid that distinguishes them, so the
        # honest answer is that this could not be determined.
        todays = [r for r in after if _mine(r)]
        res.checks.append(R.Check(
            name="the saved PR appears in the PR Risk Rating Log",
            status=R.BLOCKED,
            expected=f"a row beyond the {len(before)} that were there before",
            actual=f"the grid still holds {len(after)} row(s) after being "
                   f"re-read six times over ~30 seconds",
            detail="Save reported no error and the record re-opens from the "
                   "grid, so this is most likely the log lagging rather than "
                   "a lost save — a row made by an earlier run on this case "
                   "appeared only on the following run. It is left "
                   "undetermined rather than passed, because "
                   + (f"{len(todays)} row(s) already carry today's date and "
                      f"this user, so matching one of those would prove "
                      f"nothing about THIS save."
                      if todays else
                      "nothing in the grid can be attributed to this run."),
            evidence=[shot], screen=screen))
        say(f"    no new log row within the polling window "
            f"({len(after)} row(s))")
        # The newest of today's rows is still the best candidate to re-open,
        # so the View icon can at least be exercised. It is NOT this run's
        # record, though, and the caller is told so — comparing a fresh
        # Generate snapshot against some earlier run's saved record produced
        # three failures that said nothing about the application.
        row = todays[-1] if todays else (after[-1] if after else None)
        if row is None:
            return None, False

    # ---- Perform By is the person who is signed in -------------------
    who = _logged_in_user(s)
    shown = (row.get("Perform By") or "").strip()
    name = "the new row records who performed it"
    if not who:
        res.checks.append(R.blocked(
            name,
            f"The row says {shown!r}, but the signed-in user's name could not "
            f"be read off the page, so there is nothing to compare it "
            f"against.", evidence=[shot], screen=screen))
    elif _pr_norm(who) == _pr_norm(shown):
        res.checks.append(R.passed(
            name, detail=f"Perform By is {shown!r}, and that is who is signed "
                         f"in.", screen=screen))
    else:
        res.checks.append(R.failed(
            name, expected=f"{who!r} — the signed-in user",
            actual=f"{shown!r}", evidence=[shot], screen=screen))

    # ---- both dates are today ----------------------------------------
    for col in ("Date Of Rating", "Performed On"):
        got = (row.get(col) or "").strip()
        cname = f"the new row's {col} is today"
        if _looks_like_today(got):
            res.checks.append(R.passed(
                cname, detail=f"{col} reads {got!r}.", screen=screen))
        else:
            res.checks.append(R.failed(
                cname, expected=f"{date.today()} in the screen's own format",
                actual=f"{got!r}", evidence=[shot], screen=screen))
    return row, confirmed


def _open_pr_view(s: Session, res: CaseFlowResult, row: dict,
                  say, step):
    """
    Step 4b: open the new row through its eye / View icon.

    Returns the page holding the checklist — a NEW TAB if the eye opens one,
    as 'Perform PR' does, and the current tab if it renders in place. Both are
    handled because the two controls belong to the same screen and there is no
    reason to assume they behave alike.
    """
    screen = SCREEN_LABEL[PR_CHECKLIST]
    want = (row.get("ID") or "").strip()
    known = set(s.page.context.pages)

    opened = False
    try:
        opened = s.page.evaluate(
            """(want) => {
                const txt = e => (e.textContent || '').trim();
                const vis = e => e && (e.offsetParent !== null
                                       || e.getClientRects().length);
                const root = document.querySelector('[data-crawl-root]')
                             || document.body;
                for (const tr of root.querySelectorAll('table tbody tr')) {
                    if (!vis(tr)) continue;
                    const tds = [...tr.querySelectorAll('td')];
                    if (!tds.length) continue;
                    if (want && txt(tds[0]) !== want) continue;
                    const btn = tr.querySelector(
                        'button, a.btn, i[class*=eye], i[class*=view]');
                    if (!btn) continue;
                    (btn.closest('button, a') || btn).dispatchEvent(
                        new MouseEvent('click', {bubbles: true,
                                                 cancelable: true}));
                    return true;
                }
                return false;
            }""", want)
    except PWError as exc:
        say(f"    view click failed: {str(exc)[:120]}")

    if not opened:
        res.checks.append(R.failed(
            "the saved PR can be re-opened from its View icon",
            expected=f"the eye icon on row {want or 'the new row'} opens it",
            actual="no View control on that row would click",
            evidence=[s.screenshot("pr-07-no-view")], screen=screen))
        return None

    # A new tab, or this one? Give a popup a moment to appear before settling
    # for the page we are on.
    view = None
    for _ in range(10):
        s.page.wait_for_timeout(1500)
        fresh = [p for p in s.page.context.pages if p not in known]
        if fresh:
            view = fresh[-1]
            break
    if view is not None:
        try:
            view.wait_for_load_state("domcontentloaded", timeout=60000)
            view.wait_for_load_state("networkidle", timeout=45000)
        except PWError:
            pass
        view.wait_for_timeout(2000)
        say(f"    the view opened in a new tab: {view.url[:100]}")
    else:
        view = s.page
        cr.wait_until_settled(s.page, s.recorder, timeout_ms=40000,
                              stable_polls=2)
        try:
            cr.stamp_content_root(s.page)
        except PWError:
            pass

    step("open the saved PR from its View icon")

    with _driving(s, view):
        rows = []
        for _ in range(8):
            rows = W.Filler(session=s, screen=screen).factor_rows()
            if rows:
                break
            view.wait_for_timeout(2000)
        shot = s.screenshot("pr-07-view")

    if not rows:
        res.checks.append(R.failed(
            "the saved PR can be re-opened from its View icon",
            expected="the checklist, showing its factors",
            actual="the View icon was pressed and no factor table appeared",
            evidence=[shot], screen=screen))
        return None

    res.checks.append(R.passed(
        "the saved PR can be re-opened from its View icon",
        detail=f"The view shows {len(rows)} factor(s).", screen=screen))
    return view


def _verify_pr_factors(s: Session, res: CaseFlowResult, chosen: dict,
                       snapshot: dict, say) -> None:
    """
    Step 4c: every factor, one check each, against the Generate snapshot.

    One check per factor rather than one assert over all of them, because the
    brief asks for exactly that: a single mismatch must not hide the rest.
    """
    screen = SCREEN_LABEL[PR_CHECKLIST]
    now = _pr_snapshot(s)
    shot = s.screenshot("pr-08-verify")

    if not snapshot:
        res.checks.append(R.blocked(
            "the saved PR matches what Generate produced",
            "Nothing was captured after Generate, so there is nothing to "
            "compare the re-opened record against.", screen=screen))
        return

    for factor, want in snapshot.items():
        short = factor[:60] + ("…" if len(factor) > 60 else "")
        got = now.get(factor)
        if got is None:
            res.checks.append(R.failed(
                f"{short} survives the round trip",
                expected=f"the factor to still be on the re-opened checklist",
                actual="it is not on the re-opened view at all",
                evidence=[shot], screen=screen))
            continue

        # ---- the value that was chosen in Step 2 ---------------------
        if factor in chosen:
            if flows._same_value(chosen[factor], got["factor_value"],
                                 "dropdown"):
                res.checks.append(R.passed(
                    f"{short} kept its Factor Value",
                    detail=f"Chose {chosen[factor]!r}; the re-opened "
                           f"checklist shows {got['factor_value']!r}.",
                    screen=screen))
            else:
                res.checks.append(R.failed(
                    f"{short} kept its Factor Value",
                    expected=f"{chosen[factor]!r} — what was selected",
                    actual=f"{got['factor_value']!r} — what came back",
                    evidence=[shot], screen=screen))

        # ---- the columns the application computed --------------------
        # Only where BOTH readings had their cells lined up with their
        # headers. Where they did not, the values cannot be attributed to a
        # column at all, and comparing them would report a mismatch that says
        # nothing about the application.
        if not (want.get("aligned", True) and got.get("aligned", True)):
            res.checks.append(R.Check(
                name=f"{short} — computed columns survive the round trip",
                status=R.BLOCKED,
                detail="This row renders a different number of cells from its "
                       "header row on at least one of the two screens, so its "
                       "values cannot be tied to a named column. The Factor "
                       "Value check above is unaffected.",
                screen=screen))
            continue

        for col, expected in (want.get("computed") or {}).items():
            actual = (got.get("computed") or {}).get(col, "")
            cname = f"{short} — {col} survives the round trip"
            if not expected and not actual:
                res.checks.append(R.Check(
                    name=cname, status=R.BLOCKED,
                    detail=f"{col} was blank after Generate and is blank now, "
                           f"so this compares nothing. Whether it should have "
                           f"a value is a question for the screen, not the "
                           f"round trip.", screen=screen))
            elif flows._same_value(expected, actual, "text"):
                res.checks.append(R.passed(
                    cname, detail=f"{col} is {actual!r}, as it was after "
                                  f"Generate.", screen=screen))
            else:
                res.checks.append(R.failed(
                    cname,
                    expected=f"{expected!r} — captured right after Generate",
                    actual=f"{actual!r} — on the re-opened checklist",
                    evidence=[shot], screen=screen))
    say(f"    compared {len(snapshot)} factor(s) against the Generate snapshot")


def _check_pr_pdf(s: Session, res: CaseFlowResult, say, step) -> None:
    """
    Does 'Generate Pdf' produce a document?

    A download is the only honest evidence, so the click is wrapped in
    expect_download. A PDF that opens in a new tab instead is accepted too —
    both mean the button did its job — and anything else is reported as the
    button doing nothing, which is the defect worth knowing about.
    """
    screen = SCREEN_LABEL[PR_CHECKLIST]
    f = W.Filler(session=s, screen=screen)

    # WHERE the PDF control lives is not obvious, so every tab this flow has
    # open is searched rather than one being assumed. Measured on the live
    # form: after Generate and Save it offers only 'Generate', 'Edit' and
    # 'Save' — there is no PDF control on that tab at all — so looking only
    # there reported a missing button that was never supposed to be there.
    #
    # Controls are read out of the DOM rather than found with a locator
    # because this is an ASP.NET page: a control may be a <button> or an
    # <input type=button|submit> whose label lives in its `value`, and
    # Playwright's :has-text cannot see `value`.
    _READ_CONTROLS = """() => [...document.querySelectorAll(
            'button, a.btn, input[type=button], input[type=submit]')]
         .filter(b => b.offsetParent !== null || b.getClientRects().length)
         .map(b => ({text: (b.value || b.textContent || '').trim(),
                     id: b.id || '',
                     disabled: b.disabled === true
                               || b.hasAttribute('disabled')}))
         .filter(b => b.text || b.id)"""

    def _is_pdf(c):
        return "pdf" in f"{c['text']} {c['id']}".lower()

    candidates = [s.page] + [p for p in s.page.context.pages if p is not s.page]
    match, where, controls = None, None, []
    seen_desc = []
    for page in candidates:
        try:
            found = page.evaluate(_READ_CONTROLS) or []
        except PWError:
            continue
        seen_desc.append(
            f"{settings.host_of(page.url) or 'tab'}: "
            + (", ".join(repr(c["text"] or c["id"]) for c in found[:10])
               or "none"))
        hit = next((c for c in found if _is_pdf(c)), None)
        if hit is not None:
            match, where, controls = hit, page, found
            break
    offered = " | ".join(seen_desc) or "none"

    if match is None:
        res.checks.append(R.failed(
            "Generate Score Pdf produces a document",
            expected="a control that generates the PR score PDF",
            actual=f"no control on any of the {len(candidates)} open tab(s) "
                   f"mentions PDF",
            detail=f"Controls found — {offered}. If the PDF is produced by "
                   f"one of these under another name, name it here.",
            evidence=[s.screenshot("pr-09-no-pdf")], screen=screen))
        return

    if match["disabled"]:
        res.checks.append(R.failed(
            "Generate Score Pdf produces a document",
            expected="the PDF control enabled once a score has been generated "
                     "and saved",
            actual=f"{match['text'] or match['id']!r} is present but disabled",
            detail=f"Generate and Save both ran before this point. Controls "
                   f"offered: {offered}.",
            evidence=[s.screenshot("pr-09-pdf-disabled")], screen=screen))
        return

    label = match["text"] or match["id"]
    say(f"    PDF control {label!r} found on "
        f"{settings.host_of(where.url) or 'this tab'}")

    pages_before = len(where.context.pages)
    try:
        # Driven on the tab that HOLDS the control, which is not necessarily
        # the one the flow was last working in.
        with _driving(s, where):
            f = W.Filler(session=s, screen=screen)
            with where.expect_download(timeout=45000) as dl:
                f.press_action(label, settle_ms=5000)
            download = dl.value
    except (W.FillError, W.WriteRefused) as exc:
        # The press itself failed. Recorded, never raised: the PDF is the last
        # thing this screen is asked about, and letting it abort would take
        # the log-row check, the View icon and the whole per-factor round trip
        # with it — which is exactly what it did the first time.
        res.checks.append(R.failed(
            "Generate Score Pdf produces a document",
            expected=f"{label!r} to be clickable",
            actual=str(exc)[:200],
            detail=f"Controls offered: {offered}.",
            evidence=[s.screenshot("pr-09-pdf-refused")], screen=screen))
        return
    except PWError:
        download = None

    step(f"click {label}")
    if download is not None:
        name = download.suggested_filename
        path = os.path.join(s.artifacts_dir, name or "pr-checklist.pdf")
        try:
            download.save_as(path)
        except PWError as exc:
            res.checks.append(R.failed(
                "Generate Score Pdf produces a document",
                expected="the PDF saved to disk",
                actual=str(exc)[:180],
                evidence=[s.screenshot("pr-09-pdf-unsaved")], screen=screen))
            return
        size = os.path.getsize(path) if os.path.exists(path) else 0
        if size > 0:
            res.checks.append(R.passed(
                "Generate Score Pdf produces a document",
                detail=f"{label!r} downloaded {name!r} ({size:,} bytes) to "
                       f"{path}.", screen=screen))
        else:
            res.checks.append(R.failed(
                "Generate Score Pdf produces a document",
                expected="a PDF with content",
                actual=f"{name!r} downloaded but is empty",
                evidence=[path], screen=screen))
        return

    # No download. A PDF opened in a tab is just as good — both mean the
    # button did its job.
    where.wait_for_timeout(3000)
    if len(where.context.pages) > pages_before:
        opened = where.context.pages[-1]
        res.checks.append(R.passed(
            "Generate Score Pdf produces a document",
            detail=f"No file download, but {label!r} opened the report in a "
                   f"new tab: {opened.url[:120]}", screen=screen))
        try:
            opened.close()
        except PWError:
            pass
        return

    res.checks.append(R.failed(
        "Generate Score Pdf produces a document",
        expected="a PDF download, or the report opening in a new tab",
        actual=f"{label!r} was pressed and nothing happened within 45 seconds",
        detail=f"Controls offered: {offered}.",
        evidence=[s.screenshot("pr-09-no-pdf-result")], screen=screen))


def _do_pr_checklist(s: Session, res: CaseFlowResult, dry_run: bool,
                     say, step) -> None:
    """
    The whole screen: Perform PR -> fill every factor -> Generate -> Save ->
    back to the log -> View -> compare.

    Everything after Step 1 depends on the form opening, so each stage returns
    a flag rather than raising: a form that will not open must report itself
    once, clearly, rather than as a cascade of failures about fields that were
    never on screen.
    """
    screen = SCREEN_LABEL[PR_CHECKLIST]
    _open_screen(s, screen, step)
    cr.wait_until_settled(s.page, s.recorder, timeout_ms=30000, stable_polls=2)
    cr.stamp_content_root(s.page)
    s.screenshot("pr-00-list")

    before = _check_pr_log(s, res, say, "before performing")

    popup = _open_pr_form(s, res, say, step)
    if popup is None:
        res.checks.append(R.blocked(
            "the PR checklist can be filled, generated and saved",
            "The checklist form never opened, so filling the factors, "
            "Generate, Save, the new log row, the View icon and Generate "
            "Score Pdf could none of them be checked.", screen=screen))
        return

    # Everything from here to Save happens in the OTHER tab.
    chosen, snapshot = {}, {}
    saved = False
    try:
        with _driving(s, popup):
            chosen, _skipped = _fill_pr_factors(s, res, dry_run, say, step)

            if dry_run:
                res.checks.append(R.blocked(
                    "the PR checklist can be filled, generated and saved",
                    "Dry run: the form was opened and every factor read, but "
                    "Generate and Save were not pressed, so nothing was "
                    "computed or stored.", screen=screen))
                return

            snapshot = _pr_generate(s, res, say, step)
            saved = _pr_save(s, res, say, step)
    finally:
        # The popup must go whatever happened: leaving it open leaves the
        # run's own tab count wrong for anything that follows.
        try:
            if not popup.is_closed():
                popup.close()
        except PWError:
            pass

    if not saved:
        return

    row, confirmed = _verify_pr_row(s, res, before, say, step)
    if row is None:
        return
    view = _open_pr_view(s, res, row, say, step)
    if view is None:
        return
    try:
        with _driving(s, view):
            if confirmed:
                _verify_pr_factors(s, res, chosen, snapshot, say)
            else:
                # The row re-opened here belongs to an EARLIER run — this
                # run's has not surfaced in the log yet. Comparing the two
                # reports differences that are real between two records and
                # meaningless as a round trip, so it is not attempted.
                res.checks.append(R.blocked(
                    "the saved PR matches what Generate produced",
                    f"The log had not yet shown this run's row, so the View "
                    f"icon opened row {row.get('ID')} — a record from an "
                    f"earlier run on this case. Comparing this run's Generate "
                    f"snapshot against it would report differences between "
                    f"two different assessments, not a round trip. The View "
                    f"icon itself is confirmed working above.",
                    screen=screen))
            # Checked from HERE, not from the form tab. The form offers only
            # Generate / Edit / Save — no PDF control exists on it — so this
            # runs once the saved record is on screen, and _check_pr_pdf
            # searches every open tab rather than assuming this one.
            #
            # Guarded: the PDF is the last thing asked for, and a failure
            # pressing it must not discard the per-factor comparison above,
            # which is the expensive part of the run.
            try:
                _check_pr_pdf(s, res, say, step)
            except Exception as e:          # noqa: BLE001
                res.checks.append(R.blocked(
                    "Generate Score Pdf produces a document", str(e)[:300],
                    evidence=[s.screenshot("pr-09-pdf-error")], screen=screen))
    finally:
        try:
            if view is not s.page and not view.is_closed():
                view.close()
        except PWError:
            pass


# ==========================================================================
# eCIB Details — the UPLOAD route
#
# The same list screen as above offers a SECOND way to create a record:
# '+ Upload' takes a PDF of a State Bank of Pakistan CIB report, parses it
# server-side, and creates the record from what it read. Nothing is typed
# except the eCIB Type and eCIB Status, so what is under test here is the
# EXTRACTION — does the record the app builds actually say what the PDF says.
#
# Entirely separate from the '+ Add' flow above and it shares none of its
# code, deliberately: that flow works and is not to be disturbed.
#
# Four things were established by probing the live screen rather than by
# reading the brief, and each one contradicts a reasonable guess:
#
#   1. Proceed PERSISTS. Unlike the Add dialog — where Proceed only opens the
#      record's page and nothing exists until Save — Upload's Proceed creates
#      the record outright and drops the run back on the list. So the flow
#      handles BOTH outcomes: if a record page opens instead, it is filled in
#      and Saved; if the list comes back, the record is already there.
#
#   2. 'eCIB Borrower Code' is the CIB **Code**, not the NTN. AKHUWAT's report
#      carries Code 1058740 and NTN 3048949-7, and the created record shows
#      1058740. The check asserts that and also asserts it is NOT the NTN, so
#      a future build that switched them could not pass quietly.
#
#   3. The gate dropdowns arrive answered 'No', and the fields under them are
#      then read-only AND EMPTY — not zero. The PDF says zero (no
#      restructuring, no write-offs, no past dues), and 'No' plus an empty box
#      is a faithful reading of that, so those fields are checked as
#      blank-or-zero with the gate asserted separately. Demanding a literal 0
#      would report two dozen failures on a screen that is behaving correctly.
#
#   4. The entity PDF's Markup and Other columns are NOT what the brief says.
#      The SBP table's Fund-based columns run Principal, Markup, Other, Total:
#          75,239,802   0   214,922   75,454,724
#      so Markup is 0 and Other is 214,922 — and they add up, which settles
#      it. The brief has the two transposed. The app agrees with the PDF, so
#      the expectation follows the PDF and the disagreement is reported as a
#      note rather than silently asserted either way.
# ==========================================================================

# Where the sample PDFs live. Parameterised because they are inputs to the
# test, not part of it.
ECIB_PDF_DIR = os.getenv("LOS_ECIB_PDF_DIR",
                         os.path.join(os.path.expanduser("~"), "Downloads"))


# The reference list as the live dropdown offers it.
ECIB_STATUS_CHOICES = ["Clean", "Qualified", "R/R", "NA"]


def _upload_status(i: int) -> str:
    """
    The eCIB Status to select for case `i` of this run.

    The brief says "the first available option, parameterize", and an explicit
    LOS_ECIB_UPLOAD_STATUS does exactly that. The DEFAULT rotates instead, and
    the reason is worth stating because it is what makes this test survive
    being run twice.

    Upload UPSERTS: it matches an existing record by eCIB Borrower Code and
    OVERWRITES it rather than adding a second row. Established by uploading
    AKHUWAT's report twice — the row count stayed at four and the existing
    record's status changed from Clean to Qualified. So on any run after the
    first, "the record says what the PDF says" can be satisfied by the record
    the PREVIOUS run left behind, and the check would pass without the upload
    having written anything at all.

    Rotating the status per run fixes that: the status this run selects is
    almost never the one already stored, so asserting the record shows it
    proves the upload actually wrote. It is the only field on this screen the
    run chooses freely — everything else has to come from the PDF.
    """
    fixed = os.getenv("LOS_ECIB_UPLOAD_STATUS", "").strip()
    if fixed:
        return fixed
    seed = int(_ECIB_RUN or "0")
    return ECIB_STATUS_CHOICES[(seed + i) % len(ECIB_STATUS_CHOICES)]


# An expectation meaning "empty is as right as zero here". Used for the boxes
# the app leaves blank because their section's gate says No, and for the
# Investments row of an individual's report, which has no investments section
# at all — a blank there is the app declining to invent a figure, which is
# better behaviour than writing a 0 the document never stated.
BLANK_OR_ZERO = "<blank-or-zero>"


# The three gates, and the fields each one holds read-only. Same gates the Add
# flow answers 'Yes' to; the upload leaves them at the 'No' the PDF implies.
ECIB_GATED_BY = {
    "Rescheduled / Restructured?": [
        "Number of Times Restructured/Rescheduled (in 5 yrs) - for entities "
        "only"],
    "Write-Off?": [
        "Write-Offs (During last 10 Yrs)",
        "Waived-Offs (During last 10 Yrs)",
        "Recovery (During last 10 Yrs)",
        "Settled Write-Offs (During last 5 Yrs)",
        "Settled Waived-Offs (During last 5 Yrs)",
        "Settled Recovery (During last 5 Yrs)"],
    "Overdue?": [
        "Amount in 30+ DPDs", "Amount in 60+ DPDs",
        "Amount in 90+ DPDs (Loans)", "Amount in 90+ DPDs (Investments)",
        "Amount in 120+ DPDs", "Amount in 150+ DPDs",
        "Amount in 180+ DPDs (Loans)", "Amount in 180+ DPDs (Investments)",
        "Amount in 365+ DPDs (Loans)", "Amount in 365+ DPDs (Investments)",
        # The individual PDF's 'No. of times payments were made late' and its
        # over-due counters are all 0, so these belong to the same assertion.
        # The app spells these '30+ days' where the written requirement has
        # '30 days'; the app's spelling is what answers the DOM.
        "No. of times account went into overdue by 30+ days (only for "
        "Individuals)",
        "No. of times account went into overdue by 60+ days (only for "
        "Individuals)",
        "No. of times account went into overdue by 90+ days (only for "
        "Individuals)",
        "No. of times account went into overdue by 120+ days (only for "
        "Individuals)",
        "No. of times account went into overdue by 150+ days (only for "
        "Individuals)",
        "No. of times account went into overdue by 180+ days (only for "
        "Individuals)"],
}


@dataclass(frozen=True)
class UploadCase:
    """One PDF and everything the record built from it should say."""
    key: str
    title: str
    pdf_name: str
    ecib_type: str          # what to pick in the modal's eCIB Type
    name: str               # Company/Individual Name, per the PDF
    borrower_code: str      # eCIB Borrower Code, per the PDF
    report_date: str        # the PDF's 'Report Date'
    exposure_as_on: str     # what the PDF's exposure is 'as on'
    fields: dict            # record-page label -> expected value
    notes: dict             # label -> why a human should confirm the mapping
    status: str = ""        # set per case from _upload_status
    gates: dict = field(default_factory=lambda: {
        "Rescheduled / Restructured?": "No",
        "Write-Off?": "No",
        "Overdue?": "No"})

    @property
    def pdf(self) -> str:
        return os.path.join(ECIB_PDF_DIR, self.pdf_name)


# ---- Case A: a CORPORATE report ------------------------------------------
# SBP 'Corporate Credit Information Report' for AKHUWAT, exposure as on
# 31-12-2024. Every figure below is read straight off that table.
UPLOAD_ENTITY = UploadCase(
    key="entity",
    title="Entity (AKHUWAT)",
    pdf_name="AKHUWAT (Entity).pdf",
    # Confirmed against the live dropdown: it offers a plain 'Entity' as well
    # as several '- Entity' sub-types. AKHUWAT is not a partnership, an AOP or
    # a listed company, so the generic type is the honest choice — and it is
    # the one the Add flow already uses, which keeps the two comparable.
    ecib_type="Entity",
    name="AKHUWAT",
    # The CIB 'Code' from the corporate profile — NOT the NTN. Established
    # empirically; see note 2 above.
    borrower_code="1058740",
    report_date="17-Jan-2025",
    exposure_as_on="31-Dec-2024",
    status=_upload_status(0),
    fields={
        # ---- Limit ----------------------------------------------------
        "Funded Limit (Loans)": "132352500",
        "Non-Funded Limit (Loans)": "5500000",
        "Total Limit (Loans)": "137852500",
        "Funded Limit (Investments)": "0",
        "Non-Funded Limit (Investments)": "0",
        "Total Limit (Investments)": "0",
        # ---- Outstanding Liabilities ----------------------------------
        "Principal Outstanding (Loans)": "75239802",
        # Markup is the PDF's 0 and Other is its 214,922 — see note 4.
        "Markup Outstanding (Loans)": "0",
        "Other Outstanding (Loans)": "214922",
        "Fund Based (Loans)": "75454724",
        "Non-Fund Based (Loans)": "5500000",
        # The app's own arithmetic: Fund Based + Non-Fund Based. The PDF has
        # no such column, so this is checked as the app's computation of
        # figures that DID come from the PDF.
        "Total (Loans)": "80954724",
        "Principal Outstanding (Investments)": "0",
        "Markup Outstanding (Investments)": "0",
        "Other Outstanding (Investments)": "0",
        "Fund Based (Investments)": "0",
        "Non-Fund Based (Investments)": "0",
        "Total (Investments)": "0",
        # ---- Litigation ------------------------------------------------
        "Amount Under Litigation (Loans)": "0",
        "Amount Under Litigation (Investments)": "0",
    },
    notes={
        "Markup Outstanding (Loans) / Other Outstanding (Loans)":
            "The brief expects Markup 214,922 and Other 0. The PDF's "
            "fund-based columns run Principal, Markup, Other, Total and read "
            "75,239,802 / 0 / 214,922 / 75,454,724 — which sum correctly — so "
            "Markup is 0 and Other is 214,922. The expectation follows the "
            "PDF. Worth a second pair of eyes because it means the brief, not "
            "the application, is wrong.",
        "Total (Loans)":
            "The brief expects 75,454,724 for 'Fund Based (Loans)/Total'. "
            "Those are two different boxes on this form: Fund Based (Loans) "
            "is 75,454,724 (the PDF's fund-based Total) and Total (Loans) is "
            "80,954,724 (that plus Non-Fund Based). Both are checked; confirm "
            "which one the brief meant.",
    })


# ---- Case B: an INDIVIDUAL report ----------------------------------------
# SBP 'Individual Credit Information Report' for ABDUR RAUF KHAN. One credit
# line: an evergreen consumer credit card, position as of 31-Dec-2024, limit
# 599,000, present balance 0, nothing overdue, nothing written off.
UPLOAD_INDIVIDUAL = UploadCase(
    key="individual",
    title="Individual (ABDUR RAUF KHAN)",
    pdf_name="ABDUR RAUF KHAN (Individual).pdf",
    # The live list's '- Individual' options are all ROLE-based (Director,
    # Partner, Managing Partner …) and this report states no role for him at
    # all — only 'Salaried'. The generic individual type is the only one the
    # document supports, so asserting a role would be inventing data.
    ecib_type="Individual (incl. sponsors & allied proprietorships)",
    name="Mr. ABDUR RAUF KHAN",
    borrower_code="12101-4913388-7",          # the CNIC
    report_date="17-Jan-2025",
    # The Credit Details table's 'Position as of'.
    exposure_as_on="31-Dec-2024",
    status=_upload_status(1),
    fields={
        # The report's single 'Limit' column lands in Funded Limit (Loans).
        "Funded Limit (Loans)": "599000",
        "Non-Funded Limit (Loans)": "0",
        "Total Limit (Loans)": "599000",
        # 'Present Balance' is 0, so nothing is outstanding.
        "Principal Outstanding (Loans)": "0",
        "Markup Outstanding (Loans)": "0",
        "Other Outstanding (Loans)": "0",
        "Fund Based (Loans)": "0",
        "Non-Fund Based (Loans)": "0",
        "Total (Loans)": "0",
        "Amount Under Litigation (Loans)": "0",
        # An individual's report has no investments section. The app leaves
        # these blank rather than inventing zeros — see BLANK_OR_ZERO.
        "Funded Limit (Investments)": BLANK_OR_ZERO,
        "Non-Funded Limit (Investments)": BLANK_OR_ZERO,
        "Total Limit (Investments)": BLANK_OR_ZERO,
        "Principal Outstanding (Investments)": BLANK_OR_ZERO,
        "Markup Outstanding (Investments)": BLANK_OR_ZERO,
        "Other Outstanding (Investments)": BLANK_OR_ZERO,
        "Fund Based (Investments)": BLANK_OR_ZERO,
        "Non-Fund Based (Investments)": BLANK_OR_ZERO,
        "Total (Investments)": BLANK_OR_ZERO,
        "Amount Under Litigation (Investments)": BLANK_OR_ZERO,
    },
    notes={
        "Current Overdue (Y/N)":
            "The PDF's 'Current Overdue' is N and its DPD columns are all 0. "
            "This form has no single Overdue field — it has an 'Overdue?' "
            "gate plus ten DPD amount boxes. The gate reading 'No' with the "
            "boxes left empty is the mapping, and that is what is checked. "
            "Confirm that is the intended representation.",
    })


UPLOAD_CASES = [UPLOAD_ENTITY, UPLOAD_INDIVIDUAL]


# The list grid's columns, and which of a case's expectations each one holds.
_ECIB_GRID_COLUMNS = {
    "Company/Individual Name": "name",
    "eCIB Borrower Code": "borrower_code",
    "eCIB Report Date": "report_date",
    "Exposure As On": "exposure_as_on",
    "eCIB Type": "ecib_type",
}


_ECIB_ROWS_JS = """() => {
    const root = document.querySelector('[data-crawl-root]') || document.body;
    const txt = e => (e.textContent || '').trim().replace(/\\s+/g, ' ');
    const t = root.querySelector('table');
    if (!t) return {headers: [], rows: []};
    const headers = [...t.querySelectorAll('th')].map(txt).filter(Boolean);
    const rows = [];
    for (const tr of t.querySelectorAll('tbody tr')) {
        const cells = [...tr.querySelectorAll('td')].map(txt);
        if (!cells.length) continue;
        const joined = cells.join(' ');
        if (/no (matching |)records? found|no data/i.test(joined)) continue;
        rows.push(cells);
    }
    return {headers, rows};
}"""


def _ecib_grid(s: Session) -> tuple[list[str], list[dict]]:
    """The eCIB list grid as (headers, [{column: cell}]) in screen order."""
    try:
        got = s.page.evaluate(_ECIB_ROWS_JS)
    except PWError:
        return [], []
    headers = got.get("headers") or []
    out = []
    for cells in got.get("rows") or []:
        out.append({headers[i]: cells[i]
                    for i in range(min(len(headers), len(cells)))})
    return headers, out


def _upload_screen(case: UploadCase) -> str:
    """Reporting screen name, per case, so the two cases never share a line."""
    return f"{ECIB_UPLOAD_LABEL} — {case.title}"


def _attach_in_modal(f: W.Filler, path: str) -> str:
    """
    Put the PDF on the Upload dialog's file input.

    The attach itself goes through widgets.upload_in_scope, because every
    write in this project goes through widgets.py — that is the rule the
    safety suite enforces, and it is the rule that makes "refuses to write
    off the approved host" a property of one file rather than a promise made
    in forty places. All this adds is the test-data message, which belongs
    here where the sample PDFs are configured.
    """
    if not os.path.exists(path):
        raise W.FillError(
            f"There is no file at {path!r} to attach. Put the sample PDFs in "
            f"{ECIB_PDF_DIR!r} or set LOS_ECIB_PDF_DIR.")
    e = f.upload_in_scope(path, label="Uploaded File Name")
    return e.value


def _stored_status(s: Session, rows: list[dict], case: UploadCase,
                   step) -> str:
    """
    The eCIB Status already stored against this borrower, or "".

    Read before the upload, and only when the borrower is already on the
    list. Rotating the status per run makes a re-run's write PROBABLY
    detectable; knowing what is already there makes it CERTAIN, because the
    run can then pick something else deliberately. eCIB Status is not a
    column on the list grid, so the record has to be opened to see it.

    Best-effort: if it cannot be read the run carries on with the rotated
    status and says so, because a weaker proof is still worth having and this
    is not itself the thing under test.
    """
    col = "Company/Individual Name"
    idx = next((i for i, r in enumerate(rows)
                if _norm(r.get(col, "")) == _norm(case.name)), None)
    if idx is None:
        return ""
    openers = cr.collect_row_openers(s.page, max_rows=60)
    if idx >= len(openers):
        return ""
    try:
        if not s.open_row_detail(idx):
            return ""
        s.page.wait_for_timeout(2000)
        cr.stamp_content_root(s.page)
        got = W.Filler(session=s, screen=_upload_screen(case)).value_of(
            "eCIB Status")
    except (PWError, W.FillError):
        got = ""
    finally:
        s.leave_row_detail()
        _open_screen(s, SCREEN_LABEL[ECIB_DETAILS], step)
        cr.wait_until_settled(s.page, s.recorder, timeout_ms=25000,
                              stable_polls=2)
        cr.stamp_content_root(s.page)
    return got


def _open_upload_modal(s: Session, res: CaseFlowResult, case: UploadCase,
                       say, step) -> bool:
    """Click '+ Upload' on the list and confirm its dialog opened."""
    screen = _upload_screen(case)
    btns = cr.collect_action_buttons(s.page)
    opener = next((b for b in btns
                   if (b["label"] or "").strip().lower() == "upload"), None)
    if opener is None:
        res.checks.append(R.failed(
            "the eCIB list offers an Upload control",
            expected="an 'Upload' button beside 'Add' on the eCIB list",
            actual="none was found. Available: "
                   + (", ".join(b["label"] for b in btns[:8]) or "no buttons"),
            evidence=[s.screenshot(f"up-{case.key}-01-no-upload")],
            screen=screen))
        return False

    if not cr._click_stamped(s.page, "data-crawl-action", opener["index"],
                             timeout=8000):
        res.checks.append(R.failed(
            "the eCIB list offers an Upload control",
            expected="the Upload button opens the Upload dialog",
            actual="the button would not click",
            evidence=[s.screenshot(f"up-{case.key}-01-upload-stuck")],
            screen=screen))
        return False
    cr.wait_until_settled(s.page, s.recorder, timeout_ms=20000, stable_polls=2)
    cr.stamp_content_root(s.page)
    s.page.wait_for_timeout(1200)
    step(f"click Upload ({case.key})")

    if not _modal_open(s):
        res.checks.append(R.failed(
            "the eCIB list offers an Upload control",
            expected="a dialog asking for a file, an eCIB type and a status",
            actual="no dialog opened",
            evidence=[s.screenshot(f"up-{case.key}-01-no-dialog")],
            screen=screen))
        return False

    title = _dialog_title(s, 1)
    say(f"    the Upload dialog opened: {title!r}")
    res.checks.append(R.passed(
        "the eCIB list offers an Upload control",
        detail=f"'Upload' sits beside 'Add' on the list and opens a dialog "
               f"titled {title!r}.", screen=screen))
    s.screenshot(f"up-{case.key}-02-dialog")
    return True


def _fill_upload_modal(s: Session, res: CaseFlowResult, case: UploadCase,
                       say, step) -> bool:
    """Attach the PDF, then pick the type and the status."""
    screen = _upload_screen(case)
    f = W.Filler(session=s, screen=screen, group="Upload an eCIB report")

    # ---- the file ----------------------------------------------------
    try:
        attached = _attach_in_modal(f, case.pdf)
        say(f"    attached {attached!r}")
        step(f"attach {attached}")
    except (W.FillError, W.WriteRefused) as exc:
        res.checks.append(R.blocked(
            "the Upload dialog accepts the PDF", str(exc)[:300],
            evidence=[s.screenshot(f"up-{case.key}-03-attach-failed")],
            screen=screen))
        f.cancel_modal()
        return False

    shown = f.value_of("Uploaded File Name")
    if shown and _norm(attached) == _norm(shown):
        res.checks.append(R.passed(
            "the Upload dialog accepts the PDF",
            detail=f"'Uploaded File Name' shows {shown!r} after the file was "
                   f"attached through the paperclip's file input.",
            screen=screen))
    elif shown:
        res.checks.append(R.failed(
            "the Upload dialog accepts the PDF",
            expected=f"'Uploaded File Name' to show {attached!r}",
            actual=f"it shows {shown!r}",
            evidence=[s.screenshot(f"up-{case.key}-03-wrong-name")],
            screen=screen))
    else:
        res.checks.append(R.failed(
            "the Upload dialog accepts the PDF",
            expected=f"'Uploaded File Name' to show {attached!r}",
            actual="the box is empty, so the attachment did not register",
            evidence=[s.screenshot(f"up-{case.key}-03-no-name")],
            screen=screen))
        f.cancel_modal()
        return False

    # ---- the two dropdowns -------------------------------------------
    for label, want in (("eCIB Type", case.ecib_type),
                        ("eCIB Status", case.status)):
        try:
            e = f.choose(label, want)
            say(f"    {label} = {e.value!r}")
        except (W.FillError, W.WriteRefused) as exc:
            res.checks.append(R.failed(
                "the Upload dialog accepts the type and status it asks for",
                expected=f"{label} offers {want!r}",
                actual=str(exc)[:250],
                evidence=[s.screenshot(f"up-{case.key}-03-no-option")],
                screen=screen))
            f.cancel_modal()
            return False

    res.checks.append(R.passed(
        "the Upload dialog accepts the type and status it asks for",
        detail=f"eCIB Type {case.ecib_type!r} and eCIB Status {case.status!r} "
               f"were both selectable.", screen=screen))
    s.screenshot(f"up-{case.key}-04-filled")
    step(f"fill the Upload dialog ({case.key})")
    return True


def _watch_after_proceed(s: Session, f: W.Filler,
                         timeout_ms: int = 60000) -> dict:
    """
    Poll the screen while Proceed is being processed.

    This exists because of a real miss. Toasts on this app auto-dismiss after
    a few seconds, and widgets.commit() settles for up to 25 seconds before
    returning — so reading messages after it has returned reads them after
    they have gone. The first version of this flow reported "the dialog is
    still open, outstanding validation: none reported" on an upload the
    application had in fact refused out loud.

    So the click is made WITHOUT the built-in settle and the aftermath is
    watched from the moment it happens, accumulating anything the app says.
    """
    seen = {"ok": [], "bad": [], "fields": []}
    closed_at = None
    waited = 0
    while waited < timeout_ms:
        try:
            msg = f.messages()
        except PWError:
            msg = {"ok": [], "bad": [], "fields": []}
        for bucket in ("ok", "bad", "fields"):
            for line in msg.get(bucket) or []:
                if line not in seen[bucket]:
                    seen[bucket].append(line)
        if closed_at is None and not _modal_open(s):
            closed_at = waited
            # Give the app a moment past the close for a trailing toast, then
            # stop — there is nothing further to wait for.
            s.page.wait_for_timeout(1500)
            try:
                msg = f.messages()
            except PWError:
                msg = {}
            for bucket in ("ok", "bad", "fields"):
                for line in msg.get(bucket) or []:
                    if line not in seen[bucket]:
                        seen[bucket].append(line)
            break
        s.page.wait_for_timeout(500)
        waited += 500
    seen["closed_after_ms"] = closed_at
    return seen


def _proceed_upload(s: Session, res: CaseFlowResult, case: UploadCase,
                    say, step) -> bool:
    """
    Press Proceed and work out what the application did with it.

    Two outcomes are handled because only probing settled which one happens:
    Upload's Proceed creates the record and returns to the list, where the Add
    dialog's Proceed merely opens a page that saves later. A build that
    changed its mind about that would still be tested rather than crashing.
    """
    screen = _upload_screen(case)
    f = W.Filler(session=s, screen=screen)

    s.recorder.clear()
    try:
        # No built-in settle: the aftermath is watched instead, so a toast
        # that lives three seconds is not missed. See _watch_after_proceed.
        f.commit("Proceed", expect_settle=False)
        step(f"click Proceed ({case.key})")
    except (W.FillError, W.WriteRefused) as exc:
        res.checks.append(R.failed(
            "Proceed creates the record from the PDF",
            expected="a Proceed button on the Upload dialog",
            actual=str(exc)[:250],
            evidence=[s.screenshot(f"up-{case.key}-05-no-proceed")],
            screen=screen))
        f.cancel_modal()
        return False

    said = _watch_after_proceed(s, f)
    if said["bad"] or said["ok"] or said["fields"]:
        say(f"    the app said: {said['bad'] or said['ok'] or said['fields']}")

    cr.wait_until_settled(s.page, s.recorder, timeout_ms=45000, stable_polls=2)
    s.page.wait_for_timeout(2000)
    cr.stamp_content_root(s.page)
    s.screenshot(f"up-{case.key}-05-after-proceed")

    bad = s.failed_api_calls()
    if bad:
        res.checks.append(R.failed(
            "the upload is accepted without a server error",
            expected="no failing request while the PDF is processed",
            actual="; ".join(f"{c['status']} {c['method']} {c['url'][:70]}"
                             for c in bad[:3]),
            evidence=[s.screenshot(f"up-{case.key}-05-api-failed")],
            screen=screen))

    errs = s.error_banners() + said["bad"]
    if errs:
        res.checks.append(R.failed(
            "Proceed creates the record from the PDF",
            expected="the PDF is parsed and a record created",
            actual="the application reported: " + " | ".join(errs[:3]),
            evidence=[s.screenshot(f"up-{case.key}-05-error")], screen=screen))
        return False

    # ---- did a record page open, or did the list come back? ----------
    if _modal_open(s):
        spoke = (said["bad"] or said["fields"] or said["ok"]
                 or f.unsatisfied()[:4])
        res.checks.append(R.failed(
            "Proceed creates the record from the PDF",
            expected="the dialog closes and the record is created",
            actual="the Upload dialog is still open after Proceed",
            detail=("The application said: " + " | ".join(spoke[:4])) if spoke
                   else ("The dialog reset itself to empty — the file name box "
                         "and both dropdowns went back to their placeholders — "
                         "and the application reported nothing at all: no "
                         "toast, no banner, no inline validation, and no "
                         "failing request. A refusal with no message is the "
                         "finding here."),
            evidence=[s.screenshot(f"up-{case.key}-05-modal-stuck")],
            screen=screen))
        f.cancel_modal()
        return False

    if _looks_like_detail(s):
        # The other outcome: a page to fill and Save, as the Add flow does.
        say("    Proceed opened the record's page — verifying, then saving")
        res.checks.append(R.passed(
            "Proceed creates the record from the PDF",
            detail="Proceed opened the Customer eCIB Details page carrying "
                   "what the PDF was read as. The record is saved from there.",
            screen=screen))
        _check_upload_fields(s, res, case, say, "on the page Proceed opened")
        try:
            W.Filler(session=s, screen=screen).commit("Save")
            step(f"save the uploaded record ({case.key})")
            cr.wait_until_settled(s.page, s.recorder, timeout_ms=45000,
                                  stable_polls=2)
            s.page.wait_for_timeout(2000)
        except (W.FillError, W.WriteRefused) as exc:
            res.checks.append(R.blocked(
                "the uploaded record can be saved", str(exc)[:300],
                evidence=[s.screenshot(f"up-{case.key}-05-no-save")],
                screen=screen))
            return False
        return True

    say("    Proceed returned to the list — the record was written without a "
        "separate Save")
    res.checks.append(R.passed(
        "Proceed creates the record from the PDF",
        detail="Proceed wrote the record outright and returned to the eCIB "
               "list, with no page to Save. Worth knowing: the '+ Add' "
               "dialog's Proceed does NOT persist — it only opens a page that "
               "saves later — so the two routes differ here. "
               + ("The application said: " + " | ".join(said["ok"][:2])
                  if said["ok"] else ""), screen=screen))
    return True


def _check_upload_row(s: Session, res: CaseFlowResult, case: UploadCase,
                      before: int, existed: bool, say, step) -> Optional[int]:
    """
    Step 3: the record's row on the list, column by column against the PDF.

    `before` and `existed` describe the list as it was BEFORE the upload, and
    they are what let this distinguish two legitimate outcomes from one
    failure. Upload upserts on eCIB Borrower Code:

        borrower was NOT on the list  -> a row is added        (created)
        borrower WAS on the list      -> that row is rewritten (updated)
        neither                       -> the upload did nothing (FAIL)

    Both of the first two are correct behaviour, and the check says which
    happened rather than insisting on one. What stops an update being a free
    pass is the eCIB Status assertion in _check_upload_fields — see
    _upload_status.

    Returns the row's position so the record can then be opened, or None.
    """
    screen = _upload_screen(case)
    _open_screen(s, SCREEN_LABEL[ECIB_DETAILS], step)
    cr.wait_until_settled(s.page, s.recorder, timeout_ms=25000, stable_polls=2)
    cr.stamp_content_root(s.page)
    shot = s.screenshot(f"up-{case.key}-06-list")

    col = "Company/Individual Name"
    _, rows = _ecib_grid(s)
    if not rows:
        res.checks.append(R.failed(
            f"the uploaded record appears on the eCIB list ({case.key})",
            expected=f"a row for {case.name!r}",
            actual="the list has no rows at all",
            evidence=[shot], screen=screen))
        return None

    matches = [i for i, r in enumerate(rows)
               if _norm(r.get(col, "")) == _norm(case.name)]
    grew = len(rows) - before
    name = f"the upload puts the PDF's borrower on the eCIB list ({case.key})"

    if not matches:
        res.checks.append(R.failed(
            name,
            expected=f"a row whose {col} is {case.name!r} — the borrower "
                     f"named on the PDF",
            actual=f"{len(rows)} row(s), none of them that: "
                   + " | ".join(repr(r.get(col, ""))[:40] for r in rows[:4]),
            detail="Proceed reported the file was processed successfully, so "
                   "a record was expected.",
            evidence=[shot], screen=screen))
        return None

    if not existed and grew == 1:
        outcome = (f"The borrower was not on the list before; it now is, and "
                   f"the list grew from {before} row(s) to {len(rows)}. The "
                   f"upload CREATED the record.")
    elif existed and grew == 0:
        outcome = (f"The borrower was already on the list from an earlier run "
                   f"and the list is still {len(rows)} row(s). The upload "
                   f"UPDATED that record in place rather than adding a "
                   f"second — it upserts on eCIB Borrower Code. The eCIB "
                   f"Status check below is what proves this run wrote it.")
    elif grew > 1:
        res.checks.append(R.failed(
            name,
            expected="one record per PDF",
            actual=f"the list grew by {grew}, from {before} to {len(rows)}",
            detail="A single upload produced more than one record.",
            evidence=[shot], screen=screen))
        return 0 if 0 in matches else matches[0]
    elif not existed and grew == 0:
        res.checks.append(R.failed(
            name,
            expected=f"{before + 1} rows — the borrower was not on the list, "
                     f"so a row should have been added",
            actual=f"{len(rows)}, unchanged, yet a matching row is present",
            detail="The counts and the contents disagree, which usually "
                   "means the list was read before it refreshed.",
            evidence=[shot], screen=screen))
        return 0 if 0 in matches else matches[0]
    else:
        outcome = (f"The list went from {before} row(s) to {len(rows)} and a "
                   f"row for {case.name!r} is present.")

    # Newest first on this grid, so a freshly created row is at the top.
    idx = 0 if 0 in matches else matches[0]
    row = rows[idx]
    dupes = (f" {len(matches)} rows carry this name."
             if len(matches) > 1 else "")
    res.checks.append(R.passed(
        name,
        detail=f"{col} reads {row.get(col)!r}, taken from the PDF's borrower "
               f"name. {outcome}{dupes}", screen=screen))
    say(f"    row found: {' | '.join(str(v) for v in row.values())[:110]}")

    # ---- every other column the brief names --------------------------
    for column, attr in _ECIB_GRID_COLUMNS.items():
        if column == col:
            continue
        want = getattr(case, attr)
        got = row.get(column, "")
        name = f"{column} on the new row matches the PDF ({case.key})"
        kind = "date" if "Date" in column or "As On" in column else "text"
        if got and flows._same_value(want, got, kind):
            res.checks.append(R.passed(
                name, detail=f"The PDF gives {want!r}; the row shows {got!r}.",
                screen=screen))
        else:
            res.checks.append(R.failed(
                name, expected=f"{want!r} — from the PDF",
                actual=f"{got!r}" if got else "the cell is empty",
                evidence=[shot], screen=screen))

    # ---- the Code-not-NTN question, asserted rather than assumed -----
    if case.key == "entity":
        got = row.get("eCIB Borrower Code", "")
        if _norm(got) == _norm("3048949-7"):
            res.checks.append(R.failed(
                "eCIB Borrower Code is the CIB Code, not the NTN (entity)",
                expected="1058740 — the 'Code' on the corporate profile",
                actual="3048949-7 — the NTN",
                detail="The two are different identifiers on the same report "
                       "and the record has taken the wrong one.",
                evidence=[shot], screen=screen))
        else:
            res.checks.append(R.passed(
                "eCIB Borrower Code is the CIB Code, not the NTN (entity)",
                detail=f"AKHUWAT's report carries Code 1058740 and NTN "
                       f"3048949-7. The record shows {got!r}, so the "
                       f"application maps this column to the CIB Code. This "
                       f"answers the open question in the brief.",
                screen=screen))
    return idx


def _check_upload_fields(s: Session, res: CaseFlowResult, case: UploadCase,
                         say, when: str) -> None:
    """
    Step 4: the record's own form, field by field against the PDF.

    Every figure is compared as a NUMBER, because the app displays what it
    stores — 132352500 comes back '132,352,500' and comparing text would call
    a correct value lost.
    """
    screen = _upload_screen(case)
    f = W.Filler(session=s, screen=screen)
    shot = s.screenshot(f"up-{case.key}-07-record")

    # ---- the four fields that come from the DOCUMENT ------------------
    for label, want, kind in (
            ("Company/Individual Name", case.name, "text"),
            ("eCIB Borrower Code", case.borrower_code, "text"),
            ("eCIB Report Date", case.report_date, "date"),
            ("Exposure As On", case.exposure_as_on, "date")):
        got = f.value_of(label)
        name = f"{label} on the record matches the PDF ({case.key}, {when})"
        if got and flows._same_value(want, got, kind):
            res.checks.append(R.passed(
                name, detail=f"The PDF gives {want!r}; the form shows {got!r}.",
                screen=screen))
        else:
            res.checks.append(R.failed(
                name, expected=f"{want!r} — from the PDF",
                actual=f"{got!r}" if got else "the field is empty",
                evidence=[shot], screen=screen))

    # ---- the two the DIALOG chose -------------------------------------
    # eCIB Status carries extra weight. It is the one value this run picks
    # freely, it rotates per run, and Upload overwrites an existing record
    # rather than adding one — so this is what distinguishes "this run's
    # upload wrote the record" from "a record from a previous run happens to
    # be sitting here saying the right things". See _upload_status.
    for label, want, why in (
            ("eCIB Type", case.ecib_type, "what the Upload dialog was given"),
            ("eCIB Status", case.status,
             "what THIS run's Upload dialog was given, which is what proves "
             "the upload wrote this record")):
        got = f.value_of(label)
        name = (f"{label} on the record matches the Upload dialog "
                f"({case.key}, {when})")
        if got and flows._same_value(want, got, "dropdown"):
            res.checks.append(R.passed(
                name, detail=f"Selected {want!r}; the record shows {got!r} — "
                             f"{why}.", screen=screen))
        else:
            res.checks.append(R.failed(
                name, expected=f"{want!r} — {why}",
                actual=f"{got!r}" if got else "the field is empty",
                detail="The record does not carry the value this run's upload "
                       "selected." if label == "eCIB Status" else "",
                evidence=[shot], screen=screen))

    # ---- the money and count fields ----------------------------------
    for label, want in case.fields.items():
        got = f.value_of(label)
        name = f"{label} matches the PDF ({case.key}, {when})"

        if want is BLANK_OR_ZERO:
            n = flows._as_number(got)
            if not got.strip() or (n is not None and n == 0):
                res.checks.append(R.passed(
                    name,
                    detail=f"The PDF has nothing to say here, and the form "
                           f"shows {got!r} — blank or zero, either of which "
                           f"is a faithful reading.", screen=screen))
            else:
                res.checks.append(R.failed(
                    name,
                    expected="blank or 0 — this report has no figure for it",
                    actual=f"{got!r}",
                    detail="The application has put a figure in a box the "
                           "source document says nothing about.",
                    evidence=[shot], screen=screen))
            continue

        want_n = flows._as_number(want)
        got_n = flows._as_number(got)
        if got_n is not None and want_n is not None and abs(got_n - want_n) < 0.5:
            res.checks.append(R.passed(
                name, detail=f"The PDF gives {want_n:,.0f}; the form shows "
                             f"{got!r}.", screen=screen))
        elif not got.strip():
            res.checks.append(R.failed(
                name, expected=f"{want_n:,.0f} — from the PDF",
                actual="the field is empty",
                detail="The PDF states this figure and the record does not "
                       "carry it.",
                evidence=[shot], screen=screen))
        else:
            res.checks.append(R.failed(
                name, expected=f"{want_n:,.0f} — from the PDF",
                actual=f"{got!r}",
                evidence=[shot], screen=screen))

    # ---- the gates, and the fields they hold shut --------------------
    for gate, want in case.gates.items():
        got = f.value_of(gate)
        name = f"{gate} reflects the PDF ({case.key}, {when})"
        if got and flows._same_value(want, got, "dropdown"):
            res.checks.append(R.passed(
                name,
                detail=f"The PDF reports nothing under this heading, and the "
                       f"gate reads {got!r}.", screen=screen))
        else:
            res.checks.append(R.failed(
                name, expected=f"{want!r} — the PDF reports none of these",
                actual=f"{got!r}" if got else "the gate is not set",
                evidence=[shot], screen=screen))

        held = ECIB_GATED_BY.get(gate, [])
        wrong = []
        for label in held:
            v = f.value_of(label)
            n = flows._as_number(v)
            if v.strip() and not (n is not None and n == 0):
                wrong.append(f"{label} = {v!r}")
        gname = (f"the fields under {gate} agree with the PDF's zeros "
                 f"({case.key}, {when})")
        if wrong:
            res.checks.append(R.failed(
                gname,
                expected=f"all {len(held)} blank or 0 — the PDF reports zero "
                         f"for every one of them",
                actual=f"{len(wrong)} carry a figure: " + "; ".join(wrong[:4]),
                evidence=[shot], screen=screen))
        else:
            res.checks.append(R.passed(
                gname,
                detail=f"All {len(held)} are blank or 0. They are read-only "
                       f"while the gate says {want!r}, and the PDF reports "
                       f"zero for each, so blank is a faithful reading rather "
                       f"than a missing value.", screen=screen))

    # ---- anything the brief could not settle -------------------------
    for label, why in case.notes.items():
        res.checks.append(R.blocked(
            f"mapping needs confirmation: {label} ({case.key})", why,
            screen=screen))
    say(f"    checked {len(case.fields)} figures and {len(case.gates)} gates "
        f"({when})")


def _do_ecib_upload_one(s: Session, res: CaseFlowResult, case: UploadCase,
                        dry_run: bool, say, step) -> None:
    """One PDF, end to end: Upload -> Proceed -> the list -> the record."""
    screen = _upload_screen(case)
    say(f"  {case.title} …")
    _open_screen(s, SCREEN_LABEL[ECIB_DETAILS], step)
    cr.wait_until_settled(s.page, s.recorder, timeout_ms=25000, stable_polls=2)
    cr.stamp_content_root(s.page)
    s.screenshot(f"up-{case.key}-00-list")

    # Read BEFORE anything is uploaded. Upload upserts on borrower code, so
    # whether this borrower is ALREADY on the list decides which outcome is
    # correct afterwards — created or updated. See _check_upload_row.
    _, before_rows = _ecib_grid(s)
    before = len(before_rows)
    existed = any(_norm(r.get("Company/Individual Name", "")) == _norm(case.name)
                  for r in before_rows)
    say(f"    the list holds {before} row(s) before this upload; "
        f"{case.name!r} {'is already there' if existed else 'is not there yet'}")

    # When the borrower is already on the list this upload will OVERWRITE
    # that record, so the run deliberately selects a status it does not
    # already have. That turns "the record says the right things" into "this
    # run's upload made it say them". See _upload_status.
    if existed:
        prior = _stored_status(s, before_rows, case, step)
        if prior and _norm(prior) == _norm(case.status):
            alt = next((x for x in ECIB_STATUS_CHOICES
                        if _norm(x) != _norm(prior)), case.status)
            say(f"    it already has eCIB Status {prior!r} — selecting "
                f"{alt!r} instead so the overwrite is provable")
            case = replace(case, status=alt)
        elif prior:
            say(f"    it already has eCIB Status {prior!r}; this run selects "
                f"{case.status!r}, so the overwrite will be visible")
        else:
            say(f"    could not read its current eCIB Status — proceeding "
                f"with the rotated {case.status!r}")
    say(f"    this run will select eCIB Status {case.status!r}")

    if not os.path.exists(case.pdf):
        res.checks.append(R.blocked(
            f"the sample PDF is available ({case.key})",
            f"There is no file at {case.pdf!r}. This is test data, not a "
            f"defect: put the sample PDFs in {ECIB_PDF_DIR!r} or point "
            f"LOS_ECIB_PDF_DIR at the folder holding them.", screen=screen))
        return

    if not _open_upload_modal(s, res, case, say, step):
        return
    if not _fill_upload_modal(s, res, case, say, step):
        return

    if dry_run:
        cancelled = W.Filler(session=s, screen=screen).cancel_modal()
        say("  dry run — the Upload dialog was cancelled rather than "
            "proceeded")
        res.checks.append(R.blocked(
            f"the PDF is read into a new eCIB record ({case.key})",
            f"Dry run: the PDF was attached and the two dropdowns set, then "
            f"the dialog was dismissed with "
            f"{cancelled or 'its close control'} instead of Proceed. Proceed "
            f"on this dialog CREATES the record outright — unlike the '+ Add' "
            f"dialog, which only opens a page — so a dry run cannot go "
            f"further. Re-run without the dry-run option to check the "
            f"extraction.", screen=screen))
        return

    if not _proceed_upload(s, res, case, say, step):
        return

    # ---- Step 3: the list ---------------------------------------------
    idx = _check_upload_row(s, res, case, before, existed, say, step)
    if idx is None:
        res.checks.append(R.blocked(
            f"the uploaded record's fields match the PDF ({case.key})",
            "The new row could not be found on the list, so there was no "
            "record to open and nothing to compare field by field.",
            screen=screen))
        return

    # ---- Step 4: the record -------------------------------------------
    openers = cr.collect_row_openers(s.page, max_rows=60)
    if idx >= len(openers):
        res.checks.append(R.blocked(
            f"the uploaded record can be opened ({case.key})",
            f"The row is on the list at position {idx + 1} but only "
            f"{len(openers)} row(s) offer anything to click, so the record "
            f"could not be opened.",
            evidence=[s.screenshot(f"up-{case.key}-06-no-opener")],
            screen=screen))
        return

    note = s.open_row_detail(idx)
    s.page.wait_for_timeout(2500)
    cr.stamp_content_root(s.page)
    if not note or not _looks_like_detail(s):
        res.checks.append(R.failed(
            f"the uploaded record can be opened ({case.key})",
            expected="clicking the row opens Customer eCIB Details",
            actual=f"the run is still on {s.page.url}",
            evidence=[s.screenshot(f"up-{case.key}-06-not-opened")],
            screen=screen))
        return

    errs = s.error_banners()
    if errs:
        res.checks.append(R.failed(
            f"the uploaded record can be opened ({case.key})",
            expected="the record's form opens cleanly",
            actual="the application reported: " + " | ".join(errs[:3]),
            evidence=[s.screenshot(f"up-{case.key}-06-open-error")],
            screen=screen))
    else:
        res.checks.append(R.passed(
            f"the uploaded record can be opened ({case.key})",
            detail=f"The row opened Customer eCIB Details with no error "
                   f"banner ({note}).", screen=screen))

    _check_upload_fields(s, res, case, say, "after reopening")
    s.leave_row_detail()


def _do_ecib_upload(s: Session, res: CaseFlowResult, dry_run: bool,
                    say, step) -> None:
    """
    Both PDFs through the one function, as the brief asks.

    Each case is wrapped on its own. They create independent records from
    independent documents, so an entity PDF the parser choked on must not stop
    the individual one being checked — that would report nothing about a route
    that may be working.
    """
    say(f"  Upload route — {len(UPLOAD_CASES)} PDF(s) …")
    for case in UPLOAD_CASES:
        try:
            _do_ecib_upload_one(s, res, case, dry_run, say, step)
        except NavigationError as e:
            res.checks.append(R.blocked(
                f"{case.title} can be run", str(e),
                evidence=[s.screenshot(f"up-{case.key}-unreachable")],
                screen=_upload_screen(case)))
            step(f"run {case.title}", R.BLOCKED, str(e)[:150])
            say(f"    !! {str(e)[:140]}")
        except Exception as e:      # noqa: BLE001
            res.checks.append(R.blocked(
                f"{case.title} can be run", str(e)[:300],
                evidence=[s.screenshot(f"up-{case.key}-error")],
                screen=_upload_screen(case)))
            step(f"run {case.title}", R.BLOCKED, str(e)[:150])
            say(f"    !! {str(e)[:140]}")


# --------------------------------------------------------------------------
# Financials
# --------------------------------------------------------------------------

def _fin_row_key(name: str) -> str:
    """
    A chart-of-accounts row name that survives a reload.

    THE LEADING NUMBER MOVES. The chart numbers its rows in the order it
    renders them, and that order depends on how many statements the case
    holds — the same row read as '5.Cash & Marketable Securities' before a
    statement was added and '2.Cash & Marketable Securities' after it. Keying
    on the whole name therefore matched nothing on the way back, and reported
    forty perfectly good figures as rows that were 'not on the chart'.
    """
    return _norm(re.sub(r"^\s*\d+\s*\.\s*", "", name or ""))


def _verify_financials(s: Session, res: CaseFlowResult, screen: str,
                       items: list[dict], say) -> list[R.Check]:
    """
    Re-read one saved statement after the case was re-opened.

    Its own comparison, for the same reason Business Performance needs one:
    _compare works from value_of, which finds a field by its label, and not
    one box on the chart of accounts has a label. The figures are read back by
    ROW NAME instead.

    EVERY column of a row is searched rather than the one this run filled. A
    saved statement comes back read-only and the case may hold several, so the
    column positions shift; the figures are unique per cell and per run, so a
    match anywhere in the row is this run's and not a coincidence.
    """
    checks: list[R.Check] = []
    f = W.Filler(session=s, screen=screen)
    shot = s.screenshot(f"verify-{_safe(screen)}")

    rows = f.labelled_rows()
    if not rows:
        for e in items:
            checks.append(R.Check(
                name=f"{e.get('group') or screen}: {e['label']} carried "
                     f"through",
                status=R.BLOCKED,
                detail="The chart of accounts could not be read back on the "
                       "re-opened case.", screen=screen))
        return checks

    # IS THE STATEMENT EVEN ON SCREEN? On a fresh load this tab shows ONE
    # statement column — measured: a case holding a dozen of them still
    # rendered only 'Unaudited (Projected) Jan 1, 2001 - Dec 31, 2001', one
    # cell per row. Comparing against that produces forty failures saying
    # "the row shows 0", which describes somebody else's statement rather
    # than anything about this run's. One honest answer is worth more.
    year = "".join(ch for ch in screen if ch.isdigit())[-4:]
    try:
        heads = s.page.evaluate(
            """() => [...document.querySelectorAll('[data-crawl-root] th')]
                 .map(t => (t.textContent || '').trim())""") or []
    except PWError:
        heads = []
    if year and not any(year in h for h in heads):
        checks.append(R.Check(
            name=f"{screen}: the saved statement can be read back",
            status=R.BLOCKED,
            expected=f"a column for FY{year} on the re-opened tab",
            actual="the tab shows " + (
                "; ".join(h[:60] for h in heads[1:3]) or "no statement column"),
            detail=f"FINANCIALS INPUT renders one statement at a time, and it "
                   f"is not this run's — so the {len(items)} figure(s) entered "
                   f"into FY{year} cannot be re-read from here and are left "
                   f"unverified rather than reported as lost. Whether they "
                   f"persisted needs a way to select a statement on this tab, "
                   f"which the run has not found.",
            evidence=[shot], screen=screen))
        say(f"  FY{year}'s column is not on the re-opened tab — "
            f"{len(items)} figure(s) left unverified")
        return checks

    # A LIST per name, not one entry. Stripping the leading number makes
    # names collide: 'Other Receivables', 'Other Financial Current Assets
    # (Please Specify)' and others appear under several of the chart's 38
    # sections. Keeping only the last row with a given name compared this
    # run's figure against a different section's row and failed it — the
    # report read 'the row shows -40,000, 39,846, -40,000', which is some
    # other part of the statement entirely.
    #
    # Every row of that name is kept, and a figure counts as carried through
    # if it is in ANY of them. That is safe here precisely because the values
    # are unique per cell and per run: a match cannot be a coincidence.
    values: dict[str, list[list[str]]] = {}
    for r in rows:
        values.setdefault(_fin_row_key(r.get("name", "")), []).append(
            [c.get("value", "") for c in (r.get("cells") or [])])

    for e in items:
        label, typed = e["label"], e["value"]
        name = f"{e.get('group') or screen}: {label} carried through"
        shown = values.get(_fin_row_key(label))

        if shown is None:
            checks.append(R.Check(
                name=name, status=R.BLOCKED,
                detail=f"{label!r} is not a row on the chart as it was "
                       f"re-opened, so whether the figure carried across "
                       f"cannot be told from this.",
                evidence=[shot], screen=screen))
            continue

        if any(flows._same_value(typed, v, "number")
               for cells in shown for v in cells):
            checks.append(R.passed(
                name,
                detail=f"Entered {typed!r}, and it is on the re-opened chart"
                       + (f" (this name appears on {len(shown)} rows of the "
                          f"chart; it was found on one of them)"
                          if len(shown) > 1 else "") + ".", screen=screen))
        else:
            flat = [v for cells in shown for v in cells if v]
            checks.append(R.failed(
                name,
                expected=f"{typed!r} — the figure that was entered",
                actual=(f"the row shows {', '.join(flat[:6])}"
                        if flat else "every column of the row is empty"),
                detail="The figure did not survive the round trip."
                       + (f" This name appears on {len(shown)} rows of the "
                          f"chart and none of them holds it."
                          if len(shown) > 1 else ""),
                evidence=[shot], screen=screen))

    say(f"  re-read {len(items)} figure(s) from the chart of accounts")
    return checks


def _fin_tab_for(screen: str) -> str:
    """
    The sub-tab caption a reported Financials screen lives on, or "".

    The reported names are not the captions: the two statements are filed
    under 'Financials Input (FY2010)' and 'Financials Input (FY2011)' so their
    figures never share a line in a report, while both live on the one
    FINANCIALS INPUT tab.
    """
    if screen.startswith(FIN_INPUT_LABEL):
        return "FINANCIALS INPUT"
    return {
        FIN_VARIANCE_LABEL: "FINANCIAL VARIANCE ANALYSIS",
        FIN_PEER_LABEL: "FINANCIAL PEER ANALYSIS",
        FIN_ANALYSIS_LABEL: "FINANCIAL ANALYSIS",
    }.get(screen, "")


def _fin_open_tab(s: Session, name: str, step=None) -> bool:
    """Switch Financials sub-tab. Dispatched, for the usual overlay reason."""
    ok = False
    try:
        ok = bool(s.page.evaluate(
            """(want) => {
                const txt = e => (e.textContent || '').trim();
                const t = [...document.querySelectorAll('.nav-link, [role=tab]')]
                    .find(e => txt(e).toUpperCase() === want.toUpperCase());
                if (!t) return false;
                (t.closest('a, li, button') || t).dispatchEvent(
                    new MouseEvent('click', {bubbles: true, cancelable: true}));
                return true;
            }""", name))
    except PWError:
        return False
    if ok:
        cr.wait_until_settled(s.page, s.recorder, timeout_ms=45000,
                              stable_polls=2)
        try:
            cr.stamp_content_root(s.page)
        except PWError:
            pass
        s.page.wait_for_timeout(3500)
        if step:
            step(f"open {name}")
    return ok


def _fin_active_tab(s: Session) -> str:
    try:
        return (s.page.evaluate(
            """() => {
                const a = document.querySelector('.nav-link.active');
                return a ? (a.textContent || '').trim() : '';
            }""") or "").strip()
    except PWError:
        return ""


def _fin_add_statement(s: Session, res: CaseFlowResult, year: str,
                       screen: str, say, step) -> bool:
    """
    'New Statement' -> the Add Financials dialog -> Proceed.

    Returns whether a new statement column appeared.
    """
    f = W.Filler(session=s, screen=screen, group=f"Add Financials {year}")

    def _editable() -> int:
        return sum(1 for r in f.labelled_rows()
                   for c in (r.get("cells") or []) if not c.get("readonly"))

    had = _editable()
    try:
        say(f"    {f.press_action('New Statement', settle_ms=20000)}")
    except (W.FillError, W.WriteRefused) as exc:
        res.checks.append(R.failed(
            f"{screen}: a statement can be added ({year})",
            expected="a 'New Statement' button that opens the dialog",
            actual=str(exc)[:200],
            evidence=[s.screenshot(f"fin-{year}-no-new")], screen=screen))
        return False

    s.page.wait_for_timeout(2500)
    if not _modal_open(s):
        res.checks.append(R.failed(
            f"{screen}: a statement can be added ({year})",
            expected="the Add Financials dialog",
            actual="no dialog opened",
            evidence=[s.screenshot(f"fin-{year}-no-dialog")], screen=screen))
        return False
    say(f"    the dialog opened: {_dialog_title(s, 1)!r}")

    # ---- does it carry the fields the brief names? -------------------
    try:
        present = s.page.evaluate(
            """() => [...document.querySelectorAll(
                    '.modal.show fieldset, .modal.show .form-group')]
                 .map(g => {
                     const l = g.querySelector('label');
                     return l ? (l.getAttribute('title')
                                 || (l.textContent || '').trim()) : '';
                 }).filter(Boolean)""") or []
    except PWError:
        present = []
    absent = [w for w in FIN_EXPECTED_DIALOG_FIELDS
              if not any(_norm(w) == _norm(p) or _norm(w) in _norm(p)
                         for p in present)]
    name = f"{screen}: the Add Financials dialog shows the fields asked for"
    if not absent:
        res.checks.append(R.passed(
            name, detail=f"All {len(FIN_EXPECTED_DIALOG_FIELDS)} are on it.",
            screen=screen))
    else:
        res.checks.append(R.failed(
            name,
            expected=", ".join(FIN_EXPECTED_DIALOG_FIELDS),
            actual=f"{len(absent)} are not on the dialog: "
                   + ", ".join(absent),
            detail="The dialog shows: " + "; ".join(present[:12]) + ".",
            evidence=[s.screenshot(f"fin-{year}-dialog")], screen=screen))

    # ---- fill it, in the order the screen requires -------------------
    # The tick comes before the two auditor dropdowns because it is what
    # enables them, and the dates are never typed — see the note above.
    plan = [
        ("choose", "Actual/Projected Financials?", FIN_TYPE),
        ("choose", "Financial Period Tenor (Year / Quarter / etc.)", FIN_TENOR),
        ("choose", "Financial Year", year),
        ("check", "Audited financials?", True),
        ("choose", "Type of Auditor", FIN_AUDITOR_TYPE),
        ("choose", "Financial Auditor", FIN_AUDITOR),
        ("text", "Qualification / Emphasis of matter (If applicable)",
         f"Auto-generated qualification note for FY{year}. {_NOTE}"),
        ("choose", "UDIN Verification", FIN_UDIN),
    ]
    trouble: list[str] = []
    for kind, label, value in plan:
        try:
            if kind == "choose":
                e = f.choose(label, value)
            elif kind == "check":
                e = f.check(label, value)
            else:
                e = f.text(label, value)
            say(f"    {label} = {str(e.value)[:40]!r}")
        except (W.FillError, Exception) as exc:      # noqa: BLE001
            trouble.append(f"{label}: {str(exc)[:110]}")
            say(f"    !! {label}: {str(exc)[:110]}")

    if trouble:
        res.checks.append(R.failed(
            f"{screen}: the Add Financials dialog accepts its values ({year})",
            expected="every field on the dialog takes a value",
            actual=f"{len(trouble)} would not: " + " | ".join(trouble[:4]),
            evidence=[s.screenshot(f"fin-{year}-refused")], screen=screen))
    else:
        res.checks.append(R.passed(
            f"{screen}: the Add Financials dialog accepts its values ({year})",
            detail=f"{FIN_TYPE} / {FIN_TENOR} / FY{year}, audited by "
                   f"{FIN_AUDITOR} ({FIN_AUDITOR_TYPE}), UDIN {FIN_UDIN}.",
            screen=screen))

    # ---- the dates the app fills in for itself -----------------------
    _fin_check_dates(s, res, year, screen)

    try:
        f.commit("Proceed")
        step(f"add the FY{year} statement")
    except (W.FillError, W.WriteRefused) as exc:
        res.checks.append(R.failed(
            f"{screen}: a statement can be added ({year})",
            expected="a Proceed button on the dialog",
            actual=str(exc)[:200],
            evidence=[s.screenshot(f"fin-{year}-no-proceed")], screen=screen))
        f.cancel_modal()
        return False

    s.page.wait_for_timeout(6000)
    cr.wait_until_settled(s.page, s.recorder, timeout_ms=45000, stable_polls=2)
    cr.stamp_content_root(s.page)
    s.page.wait_for_timeout(2500)
    now = _editable()

    if now <= had:
        res.checks.append(R.failed(
            f"{screen}: a statement can be added ({year})",
            expected="a column of editable cells beyond the "
                     f"{had} already there",
            actual=f"the grid still offers {now}",
            detail=f"Proceed was pressed with FY{year} filled in. A year the "
                   f"case already holds cannot be added twice — set "
                   f"LOS_FIN_YEARS to years it does not.",
            evidence=[s.screenshot(f"fin-{year}-no-column")], screen=screen))
        return False

    res.checks.append(R.passed(
        f"{screen}: a statement can be added ({year})",
        detail=f"Proceed added the FY{year} column: {now - had} editable "
               f"cell(s) appeared, {now} in total.", screen=screen))
    return True


def _fin_check_dates(s: Session, res: CaseFlowResult, year: str,
                     screen: str) -> None:
    """
    Start Date and End Date are the application's to fill, not this run's.

    Picking the Financial Year sets both, and End Date stays disabled even
    afterwards — so typing one is refused, correctly. They are checked
    instead, which is the only thing that can be said about them honestly.
    """
    try:
        got = s.page.evaluate(
            """() => {
                const out = {};
                for (const g of document.querySelectorAll(
                        '.modal.show fieldset, .modal.show .form-group')) {
                    const l = g.querySelector('label');
                    const i = g.querySelector('input');
                    if (!l || !i) continue;
                    const key = (l.getAttribute('title')
                                 || (l.textContent || '').trim());
                    if (/date/i.test(key)) out[key] = i.value || '';
                }
                return out;
            }""") or {}
    except PWError:
        got = {}

    for label, want_month in (("Start Date", 1), ("End Date", 12)):
        shown = next((v for k, v in got.items() if _norm(label) in _norm(k)), "")
        name = f"{screen}: {label} follows the financial year ({year})"
        if shown and year in shown and _fin_month_matches(shown, want_month):
            res.checks.append(R.passed(
                name, detail=f"Choosing FY{year} set it to {shown!r}; the run "
                             f"leaves it alone.", screen=screen))
        elif shown:
            res.checks.append(R.failed(
                name,
                expected=f"a date in {year} in "
                         f"{'January' if want_month == 1 else 'December'}",
                actual=f"{shown!r}", screen=screen))
        else:
            res.checks.append(R.blocked(
                name, f"{label} is empty after the year was chosen, so there "
                      f"is nothing to compare.", screen=screen))


def _fin_month_matches(shown: str, month: int) -> bool:
    names = ["january", "february", "march", "april", "may", "june", "july",
             "august", "september", "october", "november", "december"]
    return names[month - 1] in (shown or "").lower()


def _fin_fill_statement(s: Session, res: CaseFlowResult, year: str,
                        screen: str, say, step) -> dict:
    """
    Fill the editable cells of the statement just added, then Save.

    Returns {row name: value} for the round trip. Which rows are editable is
    read off the screen, never authored: the chart of accounts is
    configuration, a saved statement's column comes back read-only, and the
    brief asks for every editable row rather than a named few.
    """
    f = W.Filler(session=s, screen=screen, group=f"FY{year}")
    rows = f.labelled_rows()
    headings = [r for r in rows if r.get("heading")]
    writable = [r for r in rows
                if any(not c.get("readonly") for c in (r.get("cells") or []))]
    say(f"    the chart shows {len(rows)} row(s): {len(writable)} editable, "
        f"{len(headings)} section heading(s)")

    # WHICH COLUMN IS THIS STATEMENT? Every statement on the chart has its own
    # Save, side by side under its own column, and commit() takes the first
    # enabled match in document order — a statement somebody else filled. So
    # the column index is worked out here and the matching Save pressed below.
    #
    # Position alone is not safe to reason from: adding a statement RE-FLOWS
    # the table. Measured, one statement's Save sat at x=1464; adding a second
    # moved that one to x=1078 and put the new one at x=1464. So the column is
    # identified by which cell is EDITABLE — a saved statement's column comes
    # back read-only, so the enabled one is this run's.
    col_index = next(
        (i for r in writable
         for i, c in enumerate(r.get("cells") or [])
         if not c.get("readonly")), 0)
    say(f"    this statement is column {col_index + 1} of "
        f"{max(len(r.get('cells') or []) for r in writable) if writable else 1}")

    capped = writable[:FIN_MAX_CELLS] if FIN_MAX_CELLS else writable
    entered: dict = {}
    refused: list[str] = []
    for r in capped:
        value = _fin_amount()
        try:
            f.set_row_cell(r["name"], value, -1)
            entered[r["name"]] = value
        except (W.FillError, Exception) as exc:      # noqa: BLE001
            refused.append(f"{r['name'][:40]}: {str(exc)[:90]}")

    say(f"    {len(entered)} figure(s) entered"
        + (f", {len(refused)} refused" if refused else ""))
    # Recorded so the round trip re-reads them from a fresh load of the case.
    # Filed under this statement's own screen name, so FY2010's figures and
    # FY2011's cannot be confused for one another in the report.
    res.entries.extend(e.as_dict() for e in f.entries)

    res.checks.append(R.passed(
        f"{screen}: every editable row was accounted for (FY{year})",
        detail=f"{len(rows)} row(s) on the chart: {len(writable)} editable, "
               f"{len(rows) - len(writable) - len(headings)} computed and "
               f"skipped, {len(headings)} section heading(s). "
               + (f"{len(capped)} of the editable ones were filled "
                  f"(LOS_FIN_MAX_CELLS={FIN_MAX_CELLS})."
                  if FIN_MAX_CELLS and len(writable) > FIN_MAX_CELLS
                  else f"All {len(entered)} were filled."),
        screen=screen))

    if refused:
        res.checks.append(R.Check(
            name=f"{screen}: every figure offered accepts a value (FY{year})",
            status=R.BLOCKED,
            actual=f"{len(refused)} would not take one",
            detail=" | ".join(refused[:5]), screen=screen))

    # ---- read them back before saving --------------------------------
    held = []
    for row_name, value in entered.items():
        vals = f.row_cell_values(row_name)
        if not any(flows._same_value(value, v, "number") for v in vals):
            held.append(f"{row_name[:36]}: entered {value!r}, shows "
                        f"{', '.join(vals[-2:]) or 'nothing'}")
    if held:
        res.checks.append(R.failed(
            f"{screen} holds every value typed into it (FY{year})",
            expected="each box still shows what was entered, before Save",
            actual=f"{len(held)} did not: " + " | ".join(held[:5]),
            evidence=[s.screenshot(f"fin-{year}-not-held")], screen=screen))
    elif entered:
        res.checks.append(R.passed(
            f"{screen} holds every value typed into it (FY{year})",
            detail=f"All {len(entered)} figure(s) read back what was entered.",
            screen=screen))

    # ---- Save, THIS statement's own -----------------------------------
    try:
        note = f.commit_block(col_index)
        say(f"    {note}")
        step(f"save the FY{year} statement")
    except (W.FillError, W.WriteRefused) as exc:
        res.checks.append(R.failed(
            f"{screen} is saved (FY{year})",
            expected=f"the Save button belonging to column {col_index + 1} — "
                     f"the FY{year} statement",
            actual=str(exc)[:200],
            evidence=[s.screenshot(f"fin-{year}-no-save")], screen=screen))
        return entered

    errors = s.error_banners()
    if errors:
        res.checks.append(R.failed(
            f"{screen} is saved (FY{year})",
            expected="Save stores the statement",
            actual="the screen reported: " + " | ".join(errors[:3]),
            evidence=[s.screenshot(f"fin-{year}-save-error")], screen=screen))
    else:
        res.checks.append(R.passed(
            f"{screen} is saved (FY{year})",
            detail=f"{len(entered)} figure(s) were entered into the FY{year} "
                   f"statement, and ITS OWN Save — the one under column "
                   f"{col_index + 1}, not another statement's — reported no "
                   f"error.", screen=screen))
    return entered


def _fin_loose_comment(s: Session, res: CaseFlowResult, screen: str,
                       column_hint: str, say, step) -> dict:
    """
    A comments section that is NOT a column of the grid.

    Peer Analysis puts its comments in a block of their own below the figures.
    Tried as a rich text editor first, because that is what this application
    reaches for, then as a plain text box.
    """
    f = W.Filler(session=s, screen=screen, group=screen)
    text = f"Auto-generated {column_hint.lower()} for the round trip. {_NOTE}"
    entered: dict = {}

    for label in [l for l in f.field_labels(limit=40) if l]:
        try:
            kind = f.kind_of(label)
        except Exception:                            # noqa: BLE001
            continue
        if kind not in ("rich", "rich-text", "text"):
            continue
        if "comment" not in _norm(label) and "remark" not in _norm(label):
            continue
        try:
            e = (f.rich_text(label, text) if kind in ("rich", "rich-text")
                 else f.text(label, text))
            entered[label] = e.value
            say(f"    {label} written")
        except (W.FillError, Exception) as exc:      # noqa: BLE001
            say(f"    !! {label}: {str(exc)[:110]}")

    if not entered:
        return {}

    res.checks.append(R.passed(
        f"{screen}: {column_hint} can be filled in",
        detail=f"Not a column of the grid — a section of its own: "
               + ", ".join(entered) + ".", screen=screen))

    try:
        f.commit("Save")
        step(f"save {screen}")
        errors = s.error_banners()
        if errors:
            res.checks.append(R.failed(
                f"{screen} is saved",
                expected="Save stores the comments",
                actual="the screen reported: " + " | ".join(errors[:3]),
                evidence=[s.screenshot(f"{_safe(screen)}-save-error")],
                screen=screen))
        else:
            res.checks.append(R.passed(
                f"{screen} is saved",
                detail=f"{len(entered)} comment(s) saved with no error.",
                screen=screen))
    except (W.FillError, W.WriteRefused) as exc:
        res.checks.append(R.failed(
            f"{screen} is saved",
            expected="a Save button below the comments",
            actual=str(exc)[:200],
            evidence=[s.screenshot(f"{_safe(screen)}-no-save")], screen=screen))
    return entered


def _fin_comment_grid(s: Session, res: CaseFlowResult, screen: str,
                      column_hint: str, say, step) -> dict:
    """
    Fill the one writable column of a read-only analysis grid, and Save.

    Both Variance Analysis and Peer Analysis are grids the application
    computes and a single column of free text. Which column that is is found
    by looking for the cells that are NOT read-only, rather than by counting
    across to a position that would move the first time a column was added.
    """
    f = W.Filler(session=s, screen=screen, group=screen)
    rows = f.labelled_rows()
    writable = [r for r in rows
                if any(not c.get("readonly") for c in (r.get("cells") or []))]
    computed = sum(1 for r in rows for c in (r.get("cells") or [])
                   if c.get("readonly"))

    if not writable:
        # The comments are not always a COLUMN. On Peer Analysis every cell of
        # the grid is the application's own working, and the comments are a
        # section of their own that opens after Calculate — an editor or a
        # plain box, outside the table entirely. So before concluding there is
        # nowhere to write, look there.
        loose = _fin_loose_comment(s, res, screen, column_hint, say, step)
        if loose:
            return loose
        res.checks.append(R.blocked(
            f"{screen}: {column_hint} can be filled in",
            f"The grid shows {len(rows)} row(s) and {computed} computed "
            f"cell(s), not one writable box, and no comment editor or text "
            f"box outside it either. Everything on this tab is the "
            f"application's own working.",
            evidence=[s.screenshot(f"{_safe(screen)}-nothing-writable")],
            screen=screen))
        return {}

    capped = writable[:FIN_MAX_COMMENTS] if FIN_MAX_COMMENTS else writable
    entered: dict = {}
    refused: list[str] = []
    for r in capped:
        text = f"Auto-generated variance note for {r['name'][:50]}"
        try:
            f.set_row_cell(r["name"], text, -1)
            entered[r["name"]] = text
        except (W.FillError, Exception) as exc:      # noqa: BLE001
            refused.append(f"{r['name'][:36]}: {str(exc)[:80]}")

    say(f"    {len(entered)} {column_hint} entered across {len(rows)} row(s)")
    s.screenshot(f"{_safe(screen)}-filled")

    res.checks.append(R.passed(
        f"{screen}: {column_hint} can be filled in",
        detail=f"{len(rows)} row(s), {computed} computed cell(s) left alone, "
               f"{len(writable)} writable. {len(entered)} filled"
               + (f" (LOS_FIN_MAX_COMMENTS={FIN_MAX_COMMENTS})."
                  if FIN_MAX_COMMENTS and len(writable) > FIN_MAX_COMMENTS
                  else ".")
               + (f" {len(refused)} refused: {refused[0]}" if refused else ""),
        screen=screen))

    try:
        f.commit("Save")
        step(f"save {screen}")
    except (W.FillError, W.WriteRefused) as exc:
        res.checks.append(R.failed(
            f"{screen} is saved",
            expected="a Save button under the grid",
            actual=str(exc)[:200],
            evidence=[s.screenshot(f"{_safe(screen)}-no-save")], screen=screen))
        return entered

    errors = s.error_banners()
    if errors:
        res.checks.append(R.failed(
            f"{screen} is saved",
            expected="Save stores the grid",
            actual="the screen reported: " + " | ".join(errors[:3]),
            evidence=[s.screenshot(f"{_safe(screen)}-save-error")],
            screen=screen))
    else:
        res.checks.append(R.passed(
            f"{screen} is saved",
            detail=f"{len(entered)} entry(ies) saved with no error reported.",
            screen=screen))
    return entered


def _fin_variance(s: Session, res: CaseFlowResult, say, step) -> dict:
    """Sub-tab 2: Perform Financial Analysis, then the comments column."""
    screen = FIN_VARIANCE_LABEL
    if not _fin_open_tab(s, "FINANCIAL VARIANCE ANALYSIS", step):
        res.checks.append(R.blocked(
            f"{screen} can be opened", "No such sub-tab is on Financials.",
            screen=screen))
        return {}

    f = W.Filler(session=s, screen=screen)
    try:
        say(f"    {f.press_action('Perform Financial Analysis', settle_ms=30000)}")
        step("click Perform Financial Analysis")
    except (W.FillError, W.WriteRefused) as exc:
        res.checks.append(R.failed(
            f"{screen}: an analysis can be performed",
            expected="a 'Perform Financial Analysis' button",
            actual=str(exc)[:200],
            evidence=[s.screenshot("fin-var-no-perform")], screen=screen))
        return {}

    s.page.wait_for_timeout(6000)
    cr.wait_until_settled(s.page, s.recorder, timeout_ms=45000, stable_polls=2)
    cr.stamp_content_root(s.page)
    s.page.wait_for_timeout(2500)

    # 'Performed' means the ANALYSIS grid replaced the empty list, not merely
    # that the click was accepted. Measured: the list keeps its own headers
    # (Financial Analysis ID / Year1 / Year2 / Year 3 / Financial Type 1-3)
    # and shows 'No data available in table' when nothing was produced, and
    # that is indistinguishable from success unless it is looked for.
    rows = f.labelled_rows()
    empty = any("no data" in _norm(r.get("name", "")) for r in rows)
    if not rows or empty:
        res.checks.append(R.failed(
            f"{screen}: an analysis can be performed",
            expected="a FINANCIAL ANALYSIS grid of particulars, with a "
                     "VARIANCE COMMENTS column",
            actual=("the list still reads 'No data available in table'"
                    if empty else
                    "no grid appeared within 45 seconds of pressing it"),
            detail="'Perform Financial Analysis' was pressed and the screen "
                   "settled, but produced no analysis. The list it leaves "
                   "behind is the one it started with — Financial Analysis "
                   "ID, Year1, Year2, Year 3 and their Financial Types — "
                   "which suggests the analysis needs statements it did not "
                   "find. Everything the brief asks for below this point "
                   "depends on that grid.",
            evidence=[s.screenshot("fin-var-no-grid")], screen=screen))
        return {}
    res.checks.append(R.passed(
        f"{screen}: an analysis can be performed",
        detail=f"The grid rendered {len(rows)} row(s).", screen=screen))

    return _fin_comment_grid(s, res, screen, "VARIANCE COMMENTS", say, step)


def _fin_peer(s: Session, res: CaseFlowResult, say, step) -> dict:
    """Sub-tab 3: Perform Peer Analysis, add a peer, Calculate, comment, Save."""
    screen = FIN_PEER_LABEL
    if not _fin_open_tab(s, "FINANCIAL PEER ANALYSIS", step):
        res.checks.append(R.blocked(
            f"{screen} can be opened", "No such sub-tab is on Financials.",
            screen=screen))
        return {}

    f = W.Filler(session=s, screen=screen)
    try:
        say(f"    {f.press_action('Perform Peer Analysis', settle_ms=30000)}")
        step("click Perform Peer Analysis")
    except (W.FillError, W.WriteRefused) as exc:
        res.checks.append(R.failed(
            f"{screen}: a peer analysis can be performed",
            expected="a 'Perform Peer Analysis' button",
            actual=str(exc)[:200],
            evidence=[s.screenshot("fin-peer-no-perform")], screen=screen))
        return {}

    s.page.wait_for_timeout(6000)
    cr.wait_until_settled(s.page, s.recorder, timeout_ms=45000, stable_polls=2)
    cr.stamp_content_root(s.page)
    s.page.wait_for_timeout(2500)

    rows = f.labelled_rows()
    if not rows:
        res.checks.append(R.failed(
            f"{screen}: a peer analysis can be performed",
            expected="a PEER ANALYSIS grid",
            actual="no grid appeared within 45 seconds of pressing it",
            evidence=[s.screenshot("fin-peer-no-grid")], screen=screen))
        return {}
    res.checks.append(R.passed(
        f"{screen}: a peer analysis can be performed",
        detail=f"The grid rendered {len(rows)} row(s).", screen=screen))

    # ---- '+ Add Peer', then SAVE, then Calculate ----------------------
    #
    # The order is the screen's, not a guess: pick the customer, SAVE, then
    # Calculate, and only then are the per-row comment boxes there to fill.
    # Calculating before saving the peer had nothing to calculate against.
    _fin_add_peer(s, res, screen, say, step)

    try:
        say(f"    {f.commit('Save')} (the peer)")
        step("save the peer")
        s.page.wait_for_timeout(4000)
        cr.wait_until_settled(s.page, s.recorder, timeout_ms=45000,
                              stable_polls=2)
        cr.stamp_content_root(s.page)
        res.checks.append(R.passed(
            f"{screen}: the peer is saved before calculating",
            detail="Save was pressed once the peer had been added, which is "
                   "what Calculate works from.", screen=screen))
    except (W.FillError, W.WriteRefused) as exc:
        # Not a failure. The brief's order is pick the peer, Save, then
        # Calculate — and this screen does not always offer a Save at that
        # point: measured, the peer grid had no enabled Save until Calculate
        # had run. Reported, and the run carries on to Calculate rather than
        # stopping, because the comments below are what actually matter.
        res.checks.append(R.blocked(
            f"{screen}: the peer is saved before calculating",
            f"No Save button is on the screen once the peer has been added — "
            f"{str(exc)[:120]}. The run went on to Calculate, and the "
            f"comments below are saved in the usual way.", screen=screen))

    try:
        say(f"    {f.press_action('Calculate', settle_ms=30000)}")
        step("click Calculate")
        s.page.wait_for_timeout(6000)
        cr.wait_until_settled(s.page, s.recorder, timeout_ms=45000,
                              stable_polls=2)
        cr.stamp_content_root(s.page)
        res.checks.append(R.passed(
            f"{screen}: Calculate runs",
            detail="The button was pressed and the screen settled.",
            screen=screen))
    except (W.FillError, W.WriteRefused) as exc:
        res.checks.append(R.failed(
            f"{screen}: Calculate runs",
            expected="a Calculate button below the grid",
            actual=str(exc)[:200],
            detail="The comment boxes only appear once Calculate has run, so "
                   "anything below this may not have been there to fill.",
            evidence=[s.screenshot("fin-peer-no-calculate")], screen=screen))

    s.page.wait_for_timeout(2500)
    return _fin_comment_grid(s, res, screen, "PEER ANALYSIS COMMENTS",
                             say, step)


def _fin_add_peer(s: Session, res: CaseFlowResult, screen: str,
                  say, step) -> None:
    """'+ Add Peer' -> the Add Peer dialog -> Add."""
    f = W.Filler(session=s, screen=screen, group="Add Peer")
    try:
        say(f"    {f.press_action('Add Peer', settle_ms=20000)}")
    except (W.FillError, W.WriteRefused) as exc:
        res.checks.append(R.failed(
            f"{screen}: a peer can be added",
            expected="an '+ Add Peer' button on the peer grid",
            actual=str(exc)[:200],
            evidence=[s.screenshot("fin-peer-no-addpeer")], screen=screen))
        return

    s.page.wait_for_timeout(2500)
    if not _modal_open(s):
        res.checks.append(R.failed(
            f"{screen}: a peer can be added",
            expected="an 'Add Peer' dialog",
            actual="no dialog opened",
            evidence=[s.screenshot("fin-peer-no-dialog")], screen=screen))
        return
    say(f"    the dialog opened: {_dialog_title(s, 1)!r}")

    # THE RADIO MUST BE CLICKED. An earlier version asserted it instead, on
    # the reading that 'Existing Customer' is selected by default — measured,
    # NEITHER radio starts checked, and the Customer field does not exist
    # until one is. That is why the flow got no further than this dialog.
    #
    # 'Exisitng' is the application's own spelling; radio() matches on a
    # fragment so neither spelling has to be copied into this file.
    try:
        e = f.radio("customer")
        say(f"    radio: {e.label!r} selected")
    except (W.FillError, Exception) as exc:          # noqa: BLE001
        res.checks.append(R.failed(
            f"{screen}: a peer can be added",
            expected="the 'Existing Customer' radio can be selected",
            actual=str(exc)[:200],
            detail="Nothing else on this dialog appears until it is.",
            evidence=[s.screenshot("fin-peer-no-radio")], screen=screen))
        f.cancel_modal()
        return

    s.page.wait_for_timeout(2500)

    # Choosing the radio reveals a DISABLED 'Customer' box with a magnifier
    # beside it, which opens an ngx-treeview of customers in a second modal
    # on top of this one. That is exactly what lookup() is for.
    picked = ""
    try:
        e = f.lookup("Customer", FIN_PEER_CUSTOMER)
        picked = e.value
        say(f"    peer = {picked!r}")
    except (W.FillError, Exception) as exc:          # noqa: BLE001
        res.checks.append(R.failed(
            f"{screen}: a peer can be added",
            expected="a customer can be picked from the Customer lookup",
            actual=str(exc)[:220],
            detail="The radio was selected and the Customer field appeared, "
                   "but no customer could be chosen from it.",
            evidence=[s.screenshot("fin-peer-no-customer")], screen=screen))
        f.cancel_modal()
        return

    try:
        f.commit("Add")
        step("add a peer")
    except (W.FillError, W.WriteRefused) as exc:
        res.checks.append(R.failed(
            f"{screen}: a peer can be added",
            expected="an Add button on the Add Peer dialog",
            actual=str(exc)[:200],
            evidence=[s.screenshot("fin-peer-no-add")], screen=screen))
        f.cancel_modal()
        return

    s.page.wait_for_timeout(5000)
    cr.wait_until_settled(s.page, s.recorder, timeout_ms=45000, stable_polls=2)
    cr.stamp_content_root(s.page)
    res.checks.append(R.passed(
        f"{screen}: a peer can be added",
        detail=f"'Existing Customer' was selected, {picked!r} was picked from "
               f"the Customer lookup, and Add was pressed.", screen=screen))


def _fin_analysis(s: Session, res: CaseFlowResult, say, step) -> dict:
    """Sub-tab 4: every rich text editor on the page, then Save."""
    screen = FIN_ANALYSIS_LABEL
    if not _fin_open_tab(s, "FINANCIAL ANALYSIS", step):
        res.checks.append(R.blocked(
            f"{screen} can be opened", "No such sub-tab is on Financials.",
            screen=screen))
        return {}

    f = W.Filler(session=s, screen=screen, group=screen)
    labels = [l for l in f.field_labels(limit=60) if l]
    say(f"    {len(labels)} labelled field(s) on the tab")

    entered: dict = {}
    for label in labels:
        # kind_of reports these as 'rich', not 'rich-text'. The first version
        # compared against 'rich-text' and skipped all seven editors on a tab
        # that was working perfectly — so the comparison is against what the
        # method actually returns, and both spellings are accepted in case it
        # is ever tidied.
        if f.kind_of(label) not in ("rich", "rich-text"):
            continue
        text = (f"{label}: auto-generated analysis for the round trip. "
                f"{_NOTE}")
        try:
            e = f.rich_text(label, text)
            entered[label] = e.value
            say(f"    {label} written")
        except (W.FillError, Exception) as exc:      # noqa: BLE001
            say(f"    !! {label}: {str(exc)[:120]}")

    if not entered:
        res.checks.append(R.blocked(
            f"{screen}: every editor is written",
            f"No rich text editor could be found among the {len(labels)} "
            f"labelled field(s) on this tab.",
            evidence=[s.screenshot("fin-analysis-no-editors")], screen=screen))
        return {}

    res.checks.append(R.passed(
        f"{screen}: every editor is written",
        detail=f"{len(entered)} editor(s) written: "
               + ", ".join(list(entered)[:6]) + ".", screen=screen))
    res.entries.extend(e.as_dict() for e in f.entries)

    try:
        f.commit("Save")
        step(f"save {screen}")
    except (W.FillError, W.WriteRefused) as exc:
        res.checks.append(R.failed(
            f"{screen} is saved",
            expected="a Save button at the foot of the tab",
            actual=str(exc)[:200],
            evidence=[s.screenshot("fin-analysis-no-save")], screen=screen))
        return entered

    errors = s.error_banners()
    if errors:
        res.checks.append(R.failed(
            f"{screen} is saved",
            expected="Save stores the editors",
            actual="the screen reported: " + " | ".join(errors[:3]),
            evidence=[s.screenshot("fin-analysis-save-error")], screen=screen))
    else:
        res.checks.append(R.passed(
            f"{screen} is saved",
            detail=f"{len(entered)} editor(s) saved with no error reported.",
            screen=screen))
    return entered


def _fin_check_print(s: Session, res: CaseFlowResult, say, step) -> None:
    """
    Does the Financials tab's own 'Print' button produce anything?

    Judged the same way the PR checklist's PDF is: a download, or the report
    opening in a tab. A button that raises no error and produces neither is
    a button that does nothing, and that is worth saying.
    """
    screen = SCREEN_LABEL[FINANCIALS]
    f = W.Filler(session=s, screen=screen)

    try:
        present = bool(s.page.evaluate(
            """() => [...document.querySelectorAll('button, a.btn')]
                 .some(b => /^\\s*print\\s*$/i.test(b.textContent || ''))"""))
    except PWError:
        present = False
    if not present:
        res.checks.append(R.blocked(
            f"{screen}: Print produces a document",
            "No Print button is on this tab.",
            evidence=[s.screenshot("fin-no-print")], screen=screen))
        return

    # PRINT OPENS A DIALOG. It does not download anything by itself, which is
    # why waiting for a download reported 'nothing happened within 45
    # seconds' — the Financial Print Model was sitting open the whole time.
    # It asks which statement to print and in which format, and only Submit
    # produces the file.
    pages_before = len(s.page.context.pages)
    try:
        f.press_action("Print", settle_ms=15000)
    except (W.FillError, W.WriteRefused) as exc:
        res.checks.append(R.failed(
            f"{screen}: Print produces a document",
            expected="the Print button to be clickable",
            actual=str(exc)[:200],
            evidence=[s.screenshot("fin-print-refused")], screen=screen))
        return
    step("click Print")
    s.page.wait_for_timeout(2500)

    if not _modal_open(s):
        res.checks.append(R.failed(
            f"{screen}: Print produces a document",
            expected="the Financial Print Model dialog",
            actual="Print was pressed and no dialog opened",
            evidence=[s.screenshot("fin-print-no-dialog")], screen=screen))
        return

    for label, value in (("Actual Financial Statement", None),
                         ("Export Format", FIN_PRINT_FORMAT)):
        try:
            e = f.choose(label, value)
            say(f"    {label} = {e.value!r}")
        except (W.FillError, Exception) as exc:      # noqa: BLE001
            res.checks.append(R.failed(
                f"{screen}: Print produces a document",
                expected=f"the print dialog's {label} can be set"
                         + (f" to {value!r}" if value else ""),
                actual=str(exc)[:200],
                evidence=[s.screenshot("fin-print-dialog")], screen=screen))
            f.cancel_modal()
            return

    download = None
    try:
        with s.page.expect_download(timeout=60000) as dl:
            f.commit("Submit")
        download = dl.value
    except (W.FillError, W.WriteRefused) as exc:
        res.checks.append(R.failed(
            f"{screen}: Print produces a document",
            expected="a Submit button on the print dialog",
            actual=str(exc)[:200],
            evidence=[s.screenshot("fin-print-no-submit")], screen=screen))
        f.cancel_modal()
        return
    except PWError:
        pass

    if download is not None:
        name = download.suggested_filename
        path = os.path.join(s.artifacts_dir, name or "financials-print.xlsx")
        try:
            download.save_as(path)
            size = os.path.getsize(path) if os.path.exists(path) else 0
        except PWError:
            size = 0
        if size:
            res.checks.append(R.passed(
                f"{screen}: Print produces a document",
                detail=f"The print dialog took {FIN_PRINT_FORMAT!r} and "
                       f"Submit downloaded {name!r} ({size:,} bytes) to "
                       f"{path}.", screen=screen))
        else:
            res.checks.append(R.failed(
                f"{screen}: Print produces a document",
                expected="a document with content",
                actual=f"{name!r} downloaded but is empty",
                evidence=[path], screen=screen))
        return

    s.page.wait_for_timeout(5000)
    if len(s.page.context.pages) > pages_before:
        opened = s.page.context.pages[-1]
        res.checks.append(R.passed(
            f"{screen}: Print produces a document",
            detail=f"No file download, but Submit opened the report in a new "
                   f"tab: {opened.url[:110]}", screen=screen))
        try:
            opened.close()
        except PWError:
            pass
        return

    res.checks.append(R.failed(
        f"{screen}: Print produces a document",
        expected=f"Submit to produce a {FIN_PRINT_FORMAT} document",
        actual="the dialog was filled and Submit pressed, and nothing was "
               "downloaded or opened within 60 seconds",
        evidence=[s.screenshot("fin-print-nothing")], screen=screen))


def _do_financials(s: Session, res: CaseFlowResult, dry_run: bool,
                   say, step) -> None:
    """
    All four sub-tabs the brief asks for, each guarded on its own.

    They are four separate jobs behind one menu entry — the same arrangement
    as Business Performance — and one of them failing must not take the other
    three with it.
    """
    screen = SCREEN_LABEL[FINANCIALS]
    _open_screen(s, screen, step)
    cr.wait_until_settled(s.page, s.recorder, timeout_ms=45000, stable_polls=2)
    cr.stamp_content_root(s.page)
    s.page.wait_for_timeout(4000)

    if dry_run:
        res.checks.append(R.blocked(
            "Financials can be filled",
            "Dry run: nothing on this screen was opened or filled. Every one "
            "of its four sub-tabs writes — a statement, a variance grid, a "
            "peer analysis and four editors — so there is no useful half "
            "measure here.", screen=screen))
        return

    # ---- 1: Financials Input, one statement per year ------------------
    for year in FIN_YEARS:
        sub = f"{FIN_INPUT_LABEL} (FY{year})"
        try:
            if not _fin_open_tab(s, "FINANCIALS INPUT", step):
                res.checks.append(R.blocked(
                    f"{sub} can be opened",
                    "The FINANCIALS INPUT sub-tab is not on this screen.",
                    screen=sub))
                continue
            if _fin_add_statement(s, res, year, sub, say, step):
                _fin_fill_statement(s, res, year, sub, say, step)
        except Exception as e:      # noqa: BLE001
            res.checks.append(R.blocked(
                f"{sub} could be filled", str(e)[:300],
                evidence=[s.screenshot(f"fin-{year}-error")], screen=sub))
            say(f"    !! {str(e)[:140]}")

    # ---- 2 to 4 -------------------------------------------------------
    for name, fn in (("Financial Variance Analysis", _fin_variance),
                     ("Financial Peer Analysis", _fin_peer),
                     ("Financial Analysis", _fin_analysis)):
        try:
            say(f"  {name} …")
            fn(s, res, say, step)
        except Exception as e:      # noqa: BLE001
            res.checks.append(R.blocked(
                f"{name} could be filled", str(e)[:300],
                evidence=[s.screenshot(f"{_safe(name)}-error")], screen=name))
            say(f"    !! {str(e)[:140]}")

    # ---- last: does Print work? --------------------------------------
    # Guarded like everything else, and last because it is the only check
    # here that may open a tab or start a download.
    try:
        say("  Print …")
        _fin_check_print(s, res, say, step)
    except Exception as e:          # noqa: BLE001
        res.checks.append(R.blocked(
            f"{screen}: Print produces a document", str(e)[:300],
            evidence=[s.screenshot("fin-print-error")], screen=screen))
        say(f"    !! {str(e)[:140]}")


# --------------------------------------------------------------------------
# History
# --------------------------------------------------------------------------

# Everything a click on this screen might change, in one read. Compared
# before and after so "did anything happen?" is answered with evidence rather
# than with the absence of an exception.
_HISTORY_STATE_JS = r"""() => {
    const txt = e => (e ? (e.textContent || '') : '').trim().replace(/\s+/g, ' ');
    const vis = e => e && (e.offsetParent !== null || e.getClientRects().length);
    const root = document.querySelector('[data-crawl-root]') || document.body;
    return {
        url: location.href,
        activeTab: txt(document.querySelector('.nav-link.active')),
        breadcrumb: [...document.querySelectorAll('.breadcrumb li, .breadcrumb a')]
            .filter(vis).map(txt).filter(Boolean).slice(0, 8),
        headings: [...root.querySelectorAll('h1,h2,h3,h4,.card-title')]
            .filter(vis).map(txt).filter(Boolean).slice(0, 8),
        modals: [...document.querySelectorAll(
            '.modal.show, .modal.in, [role=dialog]')].filter(vis)
            .map(e => txt(e).slice(0, 120)),
        rows: root.querySelectorAll('table tbody tr').length,
        textLen: (root.innerText || '').length,
    };
}"""


def _history_state(s: Session) -> dict:
    try:
        return s.page.evaluate(_HISTORY_STATE_JS) or {}
    except PWError:
        return {}


def _history_grid(s: Session, heading_match: str = "") -> dict:
    """Headers and rows of the visible grid whose headers match."""
    try:
        grids = s.page.evaluate(
            """() => {
                const txt = e => (e ? (e.textContent||'') : '').trim()
                    .replace(/\\s+/g, ' ');
                const vis = e => e && (e.offsetParent !== null
                                       || e.getClientRects().length);
                const root = document.querySelector('[data-crawl-root]')
                             || document.body;
                return [...root.querySelectorAll('table')].filter(vis).map(t => ({
                    headers: [...t.querySelectorAll('th')].map(txt),
                    rows: [...t.querySelectorAll('tbody tr')].filter(vis)
                        .map(tr => [...tr.querySelectorAll('td')].map(txt)),
                }));
            }""") or []
    except PWError:
        return {}
    if not grids:
        return {}
    if heading_match:
        for g in grids:
            if any(_norm(heading_match) in _norm(h) for h in g["headers"]):
                return g
    return grids[0]


def _history_activate(s: Session, finder_js: str) -> bool:
    """
    Click something on this screen, read-only.

    Dispatched rather than clicked for the reason documented on
    widgets.press_action: this application keeps an invisible full-screen
    overlay that silently swallows real clicks. Nothing here writes — these
    are a link and a grid row — so it does not go through the write gate, the
    same way the driver's own row and tab clicks do not.
    """
    try:
        return bool(s.page.evaluate(finder_js))
    except PWError:
        return False


def _history_settle(s: Session, ms: int = 8000) -> None:
    s.page.wait_for_timeout(ms)
    try:
        cr.wait_until_settled(s.page, s.recorder, timeout_ms=25000,
                              stable_polls=2)
        cr.stamp_content_root(s.page)
    except PWError:
        pass
    s.page.wait_for_timeout(1500)


def _history_response(before: dict, after: dict,
                      new_tabs: list[str]) -> tuple[bool, str]:
    """
    Did the screen respond, and how? Returns (responded, what was seen).

    Every shape of response is accepted because the brief asks for whatever
    the app actually does, not for a guessed one — and the answer turned out
    to be the least obvious of them.
    """
    seen = []
    if new_tabs:
        seen.append(f"a new tab opened ({new_tabs[0][:90]})")
    if before.get("url") != after.get("url"):
        seen.append(f"the route changed to {after.get('url', '')[-60:]}")
    gained = [m for m in after.get("modals", [])
              if m not in before.get("modals", [])]
    if gained:
        seen.append(f"a dialog opened: {gained[0][:80]!r}")
    row_growth = after.get("rows", 0) - before.get("rows", 0)
    char_growth = after.get("textLen", 0) - before.get("textLen", 0)
    if row_growth >= HISTORY_GROWTH_ROWS or char_growth >= HISTORY_GROWTH_CHARS:
        seen.append(f"the page expanded in place — {before.get('rows')} rows "
                    f"to {after.get('rows')}, {before.get('textLen')} "
                    f"characters to {after.get('textLen')}")
    if before.get("breadcrumb") != after.get("breadcrumb"):
        seen.append(f"the breadcrumb changed to "
                    f"{' / '.join(after.get('breadcrumb', [])[:3])}")
    return bool(seen), "; ".join(seen)


def _check_view_changes(s: Session, res: CaseFlowResult, say, step) -> None:
    """Step 1: the Workflow Log's 'View Changes' link does something."""
    screen = SCREEN_LABEL[HISTORY]

    grid = _history_grid(s, "Action Taken By")
    heads = grid.get("headers") or []
    missing = [c for c in HISTORY_WORKFLOW_COLUMNS
               if not any(_norm(c) == _norm(h) for h in heads)]
    if heads and not missing:
        res.checks.append(R.passed(
            "the Workflow Log grid shows its columns",
            detail=f"All {len(HISTORY_WORKFLOW_COLUMNS)} columns are present, "
                   f"over {len(grid.get('rows') or [])} row(s).",
            screen=screen))
    elif heads:
        res.checks.append(R.failed(
            "the Workflow Log grid shows its columns",
            expected=", ".join(HISTORY_WORKFLOW_COLUMNS),
            actual=", ".join(heads),
            detail=f"Missing: {', '.join(missing)}.",
            evidence=[s.screenshot("hist-01-workflow")], screen=screen))
    else:
        res.checks.append(R.blocked(
            "the Workflow Log grid shows its columns",
            "No Workflow Log grid is on this screen.",
            evidence=[s.screenshot("hist-01-no-grid")], screen=screen))
        return

    # ---- is the link there at all? -----------------------------------
    try:
        links = s.page.evaluate(
            """() => [...document.querySelectorAll('table tbody tr a')]
                 .filter(a => /view changes/i.test(a.textContent || ''))
                 .length""") or 0
    except PWError:
        links = 0

    if not links:
        res.checks.append(R.failed(
            "the Workflow Log offers a 'View Changes' link",
            expected="a 'View Changes' link on at least one row",
            actual="no row carries one",
            evidence=[s.screenshot("hist-01-no-link")], screen=screen))
        return
    res.checks.append(R.passed(
        "the Workflow Log offers a 'View Changes' link",
        detail=f"{links} row(s) carry one.", screen=screen))
    say(f"    {links} 'View Changes' link(s) on the Workflow Log")

    # ---- click it and watch ------------------------------------------
    before = _history_state(s)
    known = set(s.page.context.pages)
    clicked = _history_activate(s, """() => {
        const a = [...document.querySelectorAll('table tbody tr a')]
            .find(x => /view changes/i.test(x.textContent || ''));
        if (!a) return false;
        a.dispatchEvent(new MouseEvent('click',
            {bubbles: true, cancelable: true}));
        return true;
    }""")
    if not clicked:
        res.checks.append(R.failed(
            "'View Changes' opens the changes view",
            expected="the first row's 'View Changes' link to be clickable",
            actual="it could not be clicked",
            evidence=[s.screenshot("hist-02-unclickable")], screen=screen))
        return
    step("click View Changes")
    _history_settle(s)

    after = _history_state(s)
    new_tabs = [p.url for p in s.page.context.pages if p not in known]
    responded, what = _history_response(before, after, new_tabs)
    shot = s.screenshot("hist-02-view-changes")

    if responded:
        res.checks.append(R.passed(
            "'View Changes' opens the changes view",
            detail=f"Clicking it on the first row: {what}.", screen=screen))
        say(f"    View Changes -> {what}")
    else:
        res.checks.append(R.failed(
            "'View Changes' opens the changes view",
            expected="something observable — a dialog, a new tab, a route "
                     "change, or the row expanding",
            actual=f"nothing changed: still {after.get('rows')} row(s) and "
                   f"{after.get('textLen')} characters, no dialog, no new tab, "
                   f"same route",
            detail="The click itself was delivered; the screen did not "
                   "respond to it.",
            evidence=[shot], screen=screen))
        say("    View Changes -> nothing happened")

    # Leave the grid as it was found, so the next step starts clean.
    _open_screen(s, screen, step)
    _history_settle(s, 4000)


def _check_requests_history(s: Session, res: CaseFlowResult, say, step) -> None:
    """Step 2: a Requests History row click opens that transaction."""
    screen = SCREEN_LABEL[HISTORY]

    opened = _history_activate(s, """() => {
        const txt = e => (e.textContent || '').trim();
        const t = [...document.querySelectorAll('.nav-link, [role=tab]')]
            .find(e => /requests history/i.test(txt(e)));
        if (!t) return false;
        (t.closest('a, button, li') || t).dispatchEvent(
            new MouseEvent('click', {bubbles: true, cancelable: true}));
        return true;
    }""")
    if not opened:
        res.checks.append(R.failed(
            f"the {HISTORY_REQUESTS_TAB} tab can be opened",
            expected=f"a '{HISTORY_REQUESTS_TAB}' tab on History",
            actual="no such tab is on this screen",
            evidence=[s.screenshot("hist-03-no-tab")], screen=screen))
        return
    _history_settle(s, 4000)
    step(f"open {HISTORY_REQUESTS_TAB}")

    state = _history_state(s)
    if _norm(HISTORY_REQUESTS_TAB) in _norm(state.get("activeTab", "")):
        res.checks.append(R.passed(
            f"the {HISTORY_REQUESTS_TAB} tab can be opened",
            detail=f"The active tab is now "
                   f"{state.get('activeTab')!r}.", screen=screen))
    else:
        res.checks.append(R.failed(
            f"the {HISTORY_REQUESTS_TAB} tab can be opened",
            expected=f"{HISTORY_REQUESTS_TAB} to become the active tab",
            actual=f"the active tab is {state.get('activeTab')!r}",
            evidence=[s.screenshot("hist-03-tab-not-active")], screen=screen))
        return

    # ---- the grid ----------------------------------------------------
    grid = _history_grid(s, "Transaction ID")
    heads = grid.get("headers") or []
    rows = [r for r in (grid.get("rows") or [])
            if r and not re.match(r"^no matching", (r[0] or ""), re.I)]
    missing = [c for c in HISTORY_REQUESTS_COLUMNS
               if not any(_norm(c) == _norm(h) for h in heads)]
    if heads and not missing:
        res.checks.append(R.passed(
            f"the {HISTORY_REQUESTS_TAB} grid shows its columns",
            detail=f"All {len(HISTORY_REQUESTS_COLUMNS)} columns are present, "
                   f"over {len(rows)} row(s). The app also carries 'Info' and "
                   f"'Info More' — the 'i' icon at the end of each row.",
            screen=screen))
    elif heads:
        res.checks.append(R.failed(
            f"the {HISTORY_REQUESTS_TAB} grid shows its columns",
            expected=", ".join(HISTORY_REQUESTS_COLUMNS),
            actual=", ".join(heads),
            detail=f"Missing: {', '.join(missing)}.",
            evidence=[s.screenshot("hist-04-requests")], screen=screen))
    else:
        res.checks.append(R.blocked(
            f"the {HISTORY_REQUESTS_TAB} grid shows its columns",
            "No Requests History grid is on this screen.",
            evidence=[s.screenshot("hist-04-no-grid")], screen=screen))
        return

    if not rows:
        res.checks.append(R.blocked(
            "clicking a Requests History row opens that transaction",
            "The grid holds no transactions on this case, so there is no row "
            "to click.", evidence=[s.screenshot("hist-04-empty")],
            screen=screen))
        return

    # ---- click the ROW, not the 'i' ----------------------------------
    # The first CELL is targeted rather than the <tr>, because the row's last
    # cell holds the info icon and the brief asks specifically for the row
    # click rather than that icon.
    target = rows[0][0] if rows[0] else "(first row)"
    before = _history_state(s)
    known = set(s.page.context.pages)
    clicked = _history_activate(s, """() => {
        const root = document.querySelector('[data-crawl-root]') || document.body;
        for (const tr of root.querySelectorAll('table tbody tr')) {
            const tds = [...tr.querySelectorAll('td')];
            if (!tds.length) continue;
            if (/^no matching/i.test((tds[0].textContent || '').trim())) continue;
            tds[0].dispatchEvent(new MouseEvent('click',
                {bubbles: true, cancelable: true}));
            return true;
        }
        return false;
    }""")
    if not clicked:
        res.checks.append(R.failed(
            "clicking a Requests History row opens that transaction",
            expected=f"row {target!r} to be clickable",
            actual="no row would take a click",
            evidence=[s.screenshot("hist-05-unclickable")], screen=screen))
        return
    step(f"click the Requests History row {target}")
    _history_settle(s, 9000)

    after = _history_state(s)
    new_tabs = [p.url for p in s.page.context.pages if p not in known]
    responded, what = _history_response(before, after, new_tabs)
    shot = s.screenshot("hist-05-after-row-click")

    # Navigation is the specific thing asked for here — an expanding panel
    # would NOT satisfy this one, so the route and the breadcrumb are what
    # decide it rather than the generic "something happened".
    navigated = (before.get("url") != after.get("url")
                 or before.get("breadcrumb") != after.get("breadcrumb"))
    landed = " / ".join(after.get("breadcrumb", [])[:3]) or after.get("url", "")

    if navigated:
        res.checks.append(R.passed(
            "clicking a Requests History row opens that transaction",
            detail=f"Clicking {target!r} left the History grid and landed on "
                   f"{landed}. Route: {after.get('url', '')[-55:]}. Headings "
                   f"there: {', '.join(after.get('headings', [])[:5])}.",
            screen=screen))
        say(f"    row click -> {landed}")
    else:
        res.checks.append(R.failed(
            "clicking a Requests History row opens that transaction",
            expected="the row to navigate away from Requests History to the "
                     "transaction's own screen",
            actual=(f"the run is still on Requests History. {what}"
                    if responded else
                    "nothing changed at all — the row is not clickable"),
            detail="Only the row was clicked; the 'i' icon at the end of the "
                   "row was deliberately left alone.",
            evidence=[shot], screen=screen))
        say("    row click -> no navigation")


def _do_history(s: Session, res: CaseFlowResult, dry_run: bool,
                say, step) -> None:
    """
    Both of the History screen's sub-tabs, and nothing is written.

    This screen is read-only by nature: the two things asked of it are
    whether a link responds and whether a row navigates. Nothing here types
    into the application, so it behaves identically on a dry run — which is
    why, unlike every other screen in this file, it does not check dry_run.

    The case is put back at the end. The Requests History row click is a
    genuine navigation to ANOTHER transaction, so leaving it where it lands
    would hand the round trip a different case than the one it filled.
    """
    screen = SCREEN_LABEL[HISTORY]
    _open_screen(s, screen, step)
    _history_settle(s, 4000)
    s.screenshot("hist-00-history")

    for name, fn in (("Workflow Log check", _check_view_changes),
                     ("Requests History check", _check_requests_history)):
        try:
            fn(s, res, say, step)
        except Exception as e:      # noqa: BLE001
            res.checks.append(R.blocked(
                f"the History {name} could be run", str(e)[:300],
                evidence=[s.screenshot(f"hist-{_safe(name)}-error")],
                screen=screen))
            say(f"    !! {str(e)[:140]}")

    # ---- put the case back -------------------------------------------
    try:
        if _norm(res.case_id) not in _norm(" ".join(
                _history_state(s).get("breadcrumb", []))):
            say("  returning to the case the run started on …")
            _open_case(s, res, say, step, "hist-return")
    except Exception as e:          # noqa: BLE001
        res.checks.append(R.blocked(
            "the run returns to its own case after History",
            f"The Requests History row click navigates to another "
            f"transaction, and getting back to {res.case_id} afterwards "
            f"failed: {str(e)[:200]}", screen=screen))


def _do_business_performance(s: Session, res: CaseFlowResult, dry_run: bool,
                             say, step) -> None:
    """
    One check, both sub-menus: open each in turn, fill its grid, save it.

    Each sub-menu is wrapped on its own. They are two screens behind one menu
    entry and they save separately, so a sub-menu that cannot be opened — or
    that throws part-way through its grid — must not take the other one with
    it. The same reasoning as the per-screen guard in fill_case_screens, for
    the same reason: a run that abandons Group Business Reciprocity because
    the Customer tab misbehaved reports nothing about a screen that may have
    been perfectly fine.
    """
    for i, spec in enumerate(BP_SUB_MENUS):
        say(f"  {spec.name} …")
        try:
            _open_sub_screen(s, SUB_MENU_PARENT[spec.name], spec.name, step)
            s.screenshot(f"a{i}-{_safe(spec.name)}")
            entries = _fill_bp_grid(s, res, spec, dry_run, say, step)
            res.entries.extend(e.as_dict() for e in entries)
        except NavigationError as e:
            res.checks.append(R.blocked(
                f"{spec.name} can be opened", str(e),
                evidence=[s.screenshot(f"{_safe(spec.name)}-unreachable")],
                screen=spec.name))
            step(f"open {spec.name}", R.BLOCKED, str(e)[:150])
            say(f"    !! {str(e)[:140]}")
        except Exception as e:      # noqa: BLE001
            res.checks.append(R.blocked(
                f"{spec.name} could be filled", str(e)[:300],
                evidence=[s.screenshot(f"{_safe(spec.name)}-error")],
                screen=spec.name))
            say(f"    !! {str(e)[:140]}")


def _bp_column_index(cols: list[str], name: str) -> Optional[int]:
    """The column a recorded figure went into, from the name it was filed as."""
    if not name:
        return None
    for i, c in enumerate(cols):
        if _norm(c) == _norm(name):
            return i
    m = re.match(r"column\s+(\d+)$", name.strip(), re.I)
    if m:
        i = int(m.group(1)) - 1
        return i if 0 <= i < len(cols) else None
    return None


# How far a computed figure may differ from the arithmetic before it counts as
# a difference. The screen rounds its percentages to two places, so anything
# tighter than this would report rounding as a defect.
_BP_TOLERANCE = 0.02


def _verify_bp_arithmetic(grid: dict, cols: list[str], screen: str,
                          shot: str) -> list[R.Check]:
    # NOT CALLED any more, on request: the run no longer judges the
    # application's percentages or totals. Kept rather than deleted because
    # the arithmetic it encodes was read off the screen and is the record of
    # how these figures relate; a future run that is asked to check them again
    # should start from this rather than work it out afresh. Wire it back in
    # from _verify_bp to re-enable.
    """
    Check the three figures the application works out for itself.

    The rules are not invented. They were read off the figures the screen was
    already holding and confirmed on both columns of both sub-menus before
    anything was typed:

        Total Income of Business Group = Mark-up + Commission + Other Fee
        Annualized Yield (%)  = Total Earnings / Average Funded x 100
        Net Yield (%)         = Annualized Yield - Borrowing Rate

    Each is checked against what the application itself now stores in the
    other rows, so this says whether its own totals agree with its own
    figures. A row the application has NOT made read-only is skipped: it is
    then an ordinary input, this run will have typed into it, and its value is
    already covered by the round trip above.
    """
    checks: list[R.Check] = []
    cells = grid.get("cells") or []

    def num(row: int, col: int):
        try:
            return flows._as_number(cells[row][col]["value"])
        except (IndexError, KeyError, TypeError):
            return None

    def readonly(row: int, col: int) -> bool:
        try:
            return bool(cells[row][col]["readonly"])
        except (IndexError, KeyError, TypeError):
            return False

    for col, col_name in enumerate(cols):
        v = {i: num(i, col) for i in range(len(BP_ROWS))}

        def check(row: int, expected, sum_shown: str) -> None:
            label = BP_ROWS[row].label
            # "agrees with", not "is the total of": two of these three are a
            # ratio and a difference, and a check named after the wrong
            # operation is a check nobody can act on.
            name = (f"{screen}: {label} agrees with the rows it is derived "
                    f"from ({col_name})")
            if not readonly(row, col):
                checks.append(R.Check(
                    name=name, status=R.BLOCKED,
                    detail=f"{label} is not read-only in this column, so the "
                           f"application is not computing it here — it is an "
                           f"ordinary figure, and the round trip above covers "
                           f"it.", screen=screen))
                return
            shown = v[row]
            if expected is None or shown is None:
                checks.append(R.Check(
                    name=name, status=R.BLOCKED,
                    detail=f"The arithmetic could not be worked out from what "
                           f"the screen shows: {sum_shown}.", screen=screen))
                return
            if abs(expected - shown) <= _BP_TOLERANCE:
                checks.append(R.passed(
                    name,
                    detail=f"{sum_shown} — and the screen shows {shown:,.2f}.",
                    screen=screen))
            else:
                checks.append(R.failed(
                    name,
                    expected=f"{expected:,.2f} — {sum_shown}",
                    actual=f"{shown:,.2f}",
                    detail="The application stored the figures in the rows "
                           "this one is derived from, but the derived figure "
                           "itself does not follow from them — so the screen "
                           "is showing a total that belongs to different "
                           "numbers. The rule was read off this screen's own "
                           "figures before anything was entered, and holds on "
                           "the other sub-menu, so the likeliest reading is "
                           "that Save stored the inputs here without "
                           "recalculating this row.",
                    evidence=[shot] if shot else [], screen=screen))

        a, b, c = v[_BP_MARKUP], v[_BP_COMMISSION], v[_BP_OTHER_FEE]
        total = None if None in (a, b, c) else a + b + c
        check(_BP_TOTAL_INCOME, total,
              f"{a:,.2f} + {b:,.2f} + {c:,.2f} = {total:,.2f}"
              if total is not None else "a contributing row is not a number")

        earn, funded = v[_BP_TOTAL_EARNINGS], v[_BP_AVG_FUNDED]
        yield_pc = (None if earn is None or not funded
                    else earn / funded * 100)
        check(_BP_ANNUALIZED, yield_pc,
              f"{earn:,.2f} / {funded:,.2f} x 100 = {yield_pc:,.2f}"
              if yield_pc is not None else
              "Average Funded Outstanding is zero or not a number")

        # Against the annualized yield the screen is SHOWING, not the one
        # computed above: the application subtracts its own rounded figure,
        # and re-deriving it here would report that rounding as a difference.
        ann, borrow = v[_BP_ANNUALIZED], v[_BP_BORROWING]
        net = None if None in (ann, borrow) else ann - borrow
        check(_BP_NET, net,
              f"{ann:,.2f} - {borrow:,.2f} = {net:,.2f}"
              if net is not None else "a contributing row is not a number")

    return checks


def _verify_bp(s: Session, res: CaseFlowResult, screen: str,
               items: list[dict], say) -> list[R.Check]:
    """
    Re-read one Business Performance sub-menu after the case was re-opened.

    Needs its own comparison for the same reason the screen needed its own
    filling: _compare reads values with value_of, which works from a field's
    label, and not one box on this screen has a label. Everything here is
    read out of the grid by row name instead.
    """
    checks: list[R.Check] = []
    f = W.Filler(session=s, screen=screen)
    shot = s.screenshot(f"verify-{_safe(screen)}")

    grid = f.period_blocks()
    if not grid.get("ok"):
        for e in items:
            checks.append(R.Check(
                name=f"{e.get('group') or screen}: {e['label']} carried through",
                status=R.BLOCKED,
                detail=f"The grid could not be read back: "
                       f"{grid.get('why')}", screen=screen))
        return checks

    names = [str(n) for n in (grid.get("names") or [])]
    count = int(grid.get("count") or 0)
    blocks = grid.get("blocks") or []
    wide = int(blocks[-1].get("wide") or 0) if blocks else 0
    cols = _bp_columns(grid) or [f"column {i + 1}" for i in range(wide)]

    # EVERY period is searched, newest first, rather than only the one this
    # run filled. The case can hold several, the screen does not caption them
    # in the DOM, and a period added by an earlier run shifts the indices —
    # so a figure is confirmed by FINDING it, and the check says which period
    # it turned up in. The figures are unique per run and per cell, so a match
    # in any period is this run's and not a coincidence.
    order = list(range(count - 1, -1, -1))

    for e in items:
        label, typed = e["label"], e["value"]
        name = f"{e.get('group') or screen}: {label} carried through"
        metric, col_name = (label.rsplit(" — ", 1) if " — " in label
                            else (label, ""))
        col = _bp_column_index(cols, col_name)

        if _bp_row_index(names, metric) is None or col is None:
            checks.append(R.Check(
                name=name, status=R.BLOCKED,
                detail=f"{metric!r} in column {col_name!r} is not on the "
                       f"screen as it was re-opened — it now shows rows "
                       f"{', '.join(names[:4])}… in columns "
                       f"{', '.join(cols)}. Whether the figure carried "
                       f"across cannot be told from this.",
                evidence=[shot], screen=screen))
            continue

        found_in, shown = None, ""
        for b in order:
            got = f.block_cell_value(metric, col, b)
            if b == order[0]:
                shown = got
            if got and flows._same_value(typed, got, "number"):
                found_in, shown = b, got
                break

        if found_in is not None:
            checks.append(R.passed(
                name,
                detail=f"Entered {typed!r}, and period {found_in + 1} of "
                       f"{count} shows {shown!r}.", screen=screen))
        else:
            checks.append(R.failed(
                name,
                expected=f"{typed!r} — the figure that was entered",
                actual=(f"{shown!r} — what the newest period shows"
                        if shown else "the box is empty"),
                detail=f"The figure was not found in any of the {count} "
                       f"period(s) on the re-opened screen.",
                evidence=[shot], screen=screen))

    # NOTHING here checks the application's arithmetic. Total Income,
    # Annualized Yield and Net Yield are computed by the screen and are
    # reported, not judged: whether a percentage comes to the right number is
    # a question about the bank's formula, and a run that asserted one would
    # be testing this file's reading of that formula rather than whether the
    # application stored what was typed. _verify_bp_arithmetic is kept and
    # unused so the working can be read — see the note above it.
    say(f"  re-read {len(items)} figure(s) across {count} period(s); the "
        f"application's own totals were left unjudged")
    return checks


def _dialog_text(s: Session) -> str:
    """
    Everything the open dialog is showing, or "" when none is open.

    flows._screen_text reads [data-crawl-root], which is the right scope for
    every screen that renders its detail inline. A Bootstrap modal is appended
    to the BODY, outside that root, so the same call reads straight past it —
    see _verify_litigation for what that cost.
    """
    try:
        return s.page.evaluate(
            """() => {
                const d = document.querySelector('.modal.show, .modal.in');
                return d ? (d.innerText || '').replace(/\\s+/g, ' ') : '';
            }""") or ""
    except Exception:  # noqa: BLE001
        return ""


def _verify_litigation(s: Session, res: CaseFlowResult, items: list[dict],
                       say) -> list[R.Check]:
    """
    Re-read the litigation record this run saved.

    This screen needs its own comparison, and the first live run is why. Its
    grid shows FIVE columns — Type of Suit, Suit Number, Court Name and the
    two lawyer names — for a record of TEN fields, so reading the grid alone
    reported the dates and the proceeding details as lost on a record that
    plainly held them.

    The generic row-detail pass does not rescue it either, which is the part
    worth remembering: the record's detail is a MODAL appended outside
    [data-crawl-root], and _screen_text is scoped to that root, so it opened
    the record and then read the grid sitting behind it. Four values were
    reported as dropped by the application when nothing had looked at them.

    So the record is opened and its fields are read with value_of, which
    scopes itself to the open dialog. Same shape as _verify_observations, and
    for the same reason.
    """
    screen = SCREEN_LABEL[LITIGATION]
    page_text = flows._screen_text(s)
    shot = s.screenshot("verify-Litigation-list")

    ok, note = _open_our_litigation(s, res)
    if not ok:
        say(f"  {note}")
        return _compare(W.Filler(session=s, screen=screen), items, screen,
                        page_text, shot, 0)

    say(f"  re-opened the litigation record: {note[:80]}")
    f = W.Filler(session=s, screen=screen)
    shot = s.screenshot("verify-Litigation-detail")
    # The dialog's own text as well, so a value the app renders as read-only
    # text rather than in a control is still found.
    page_text += " " + _dialog_text(s)
    return _compare(f, items, screen, page_text, shot, 1)


def _open_our_litigation(s: Session, res: CaseFlowResult,
                         cap: int = 12) -> tuple[bool, str]:
    """
    Open the litigation record THIS run saved, identified by its run marker.

    Rows are opened one at a time and closed again until the marker is found,
    exactly as _open_our_facility does. Falling back to the first row would be
    worse than failing: a case can carry several suits, and comparing this
    run's values against somebody else's record would call them lost.

    The marker is read with value_of, NOT out of the dialog's text, and that
    distinction cost a live run to learn. `innerText` contains neither an
    <input>'s value nor anything inside a TinyMCE <iframe> — so a dialog full
    of correctly restored values reads back as nothing but its labels, and the
    marker is never found. value_of knows how to read both.

    Which field carries the marker is taken from the pass itself rather than
    named again here, so moving the marker cannot silently break this.
    """
    marker = flows._norm_value(res.marker)
    marked = next((fl for p in LITIGATION_PASSES for fl in p.fields
                   if fl.marked), None)
    labels = list(marked.labels) if marked else ["Proceeding Details"]

    try:
        available = s.row_opener_count(cap)
    except Exception:  # noqa: BLE001 - an empty grid is ordinary
        available = 0
    for i in range(min(cap, available)):
        try:
            if not s.open_row_detail(i):
                continue
        except Exception:  # noqa: BLE001
            break
        f = W.Filler(session=s, screen=SCREEN_LABEL[LITIGATION])
        got = ""
        for label in labels:
            got = f.value_of(label)
            if got:
                break
        if not marker or marker in flows._norm_value(got):
            return True, f"row {i + 1} of {available}: {got[:110]}"
        s.leave_row_detail()
    return False, (f"No litigation record on this case carries this run's "
                   f"marker ({res.marker}) in {labels[0]!r}, so the one it "
                   f"saved could not be identified among the {available} "
                   f"row(s) on the grid.")


def _open_our_bank_relationship(s: Session, res: CaseFlowResult,
                                cap: int = 12) -> tuple[bool, str]:
    """
    Open the bank relationship THIS run created, identified by its marker.

    Same shape as _open_our_litigation and for the same reason: this case
    already carries two relationships against the same bank, so 'open the
    first row' would compare this run's values against somebody else's record
    and call them lost.

    The list is SEARCHED for the marker before any row is opened, and the
    first live run is why. This list pages at five rows and the record it had
    just created was on page two, so scanning the rows on screen found two
    pre-existing relationships, neither carrying the marker, and reported
    fifteen values as unverifiable on a record that was sitting one page away
    holding every one of them. Typing the marker into the list's own search
    box brings it back as the only row.

    Paging is still the fallback, for a build with no search box, a host where
    typing is refused, or a search that returns nothing.

    The marker is in Security, one of the columns the list renders, but it is
    still read with value_of after opening: the grid truncates a long cell,
    and a truncated marker matches nothing.
    """
    marker = flows._norm_value(res.marker)
    marked = next((fl for fl in BANK_DETAIL_PASS.fields if fl.marked), None)
    labels = list(marked.labels) if marked else ["Security"]

    searched = False
    try:
        searched = W.Filler(session=s,
                            screen=SCREEN_LABEL[BANK_RELATIONSHIPS]
                            ).search_grid(res.marker)
    except (W.WriteRefused, W.FillError):
        searched = False
    if searched:
        s.page.wait_for_timeout(800)

    try:
        available = s.row_opener_count(cap)
    except Exception:  # noqa: BLE001 - an empty list is ordinary
        available = 0
    if searched and not available:
        # The search box answered but matched nothing. Clear it so the paging
        # fallback below sees the whole list rather than an empty filter.
        try:
            W.Filler(session=s, screen=SCREEN_LABEL[BANK_RELATIONSHIPS]
                     ).search_grid("")
            available = s.row_opener_count(cap)
        except Exception:  # noqa: BLE001
            available = 0

    for i in range(min(cap, available)):
        try:
            if not s.open_row_detail(i):
                continue
        except Exception:  # noqa: BLE001
            break
        f = W.Filler(session=s, screen=SCREEN_LABEL[BANK_RELATIONSHIPS])
        got = ""
        for label in labels:
            got = f.value_of(label)
            if got:
                break
        if not marker or marker in flows._norm_value(got):
            return True, f"row {i + 1} of {available}: {got[:110]}"
        s.leave_row_detail()
    return False, (f"No bank relationship on this case carries this run's "
                   f"marker ({res.marker}) in {labels[0]!r}, so the one it "
                   f"saved could not be identified among the {available} "
                   f"row(s) on the list"
                   + (" — and the list was searched for the marker first, "
                      "not just paged." if searched else
                      ", and the list's search box could not be used, so "
                      "only the rows on screen were looked at."))


_LIMITS_TABLE_JS = r"""() => {
    const vis = (e) => e.offsetParent !== null || e.getClientRects().length > 0;
    const root = document.querySelector('[data-crawl-root]') || document.body;
    for (const t of root.querySelectorAll('table')) {
        if (!vis(t)) continue;
        const head = [...t.querySelectorAll('thead th, thead td')]
            .map(h => (h.innerText || '').replace(/\s+/g, ' ').trim());
        if (!head.some(h => /limit type/i.test(h))) continue;
        const rows = [...t.querySelectorAll('tbody tr')]
            .map(r => [...r.children].map(
                c => (c.innerText || '').replace(/\s+/g, ' ').trim()))
            .filter(cells => cells.some(c => c));
        return {head: head, rows: rows};
    }
    return null;
}"""


def _verify_bank_limits(s: Session, items: list[dict], screen: str,
                        shot: str) -> list[R.Check]:
    """
    Compare the limit row against the LIMITS table's own columns.

    Not through _compare, and the first live run is why. A limit's values are
    not labelled fields on the record's page — they are cells of a table — so
    _compare falls through to searching the page's text, and that search
    normalises punctuation away: '10000000' was entered, the table renders
    '10,000,000', and the two do not match as strings. Four values were
    reported as dropped by the application while the aggregate figures
    directly above them proved they were stored.

    Loosening the text search to compare digits only would have been worse
    than the bug: '1000000' is a substring of '10000000', so Total
    Outstanding would have matched the Total Limit cell and a genuinely lost
    value could pass. Comparing each value against its OWN column instead is
    both correct and stricter, and _same_value already knows that 10000000
    and '10,000,000' are the same number.

    Two of the seven fields on the Limits dialog — Outstanding Date and FE
    Limits — are not columns of this table, so the record's page does not
    show them at all. That is reported as such rather than as a loss; FE
    Limits is covered by the Total FE Limits aggregate.
    """
    checks: list[R.Check] = []
    table = s.page.evaluate(_LIMITS_TABLE_JS)

    if not table or not table.get("rows"):
        for e in items:
            checks.append(R.Check(
                name=f"{e.get('group') or screen}: {e['label']} carried "
                     f"through",
                status=R.FAIL if table else R.BLOCKED,
                expected=f"{e['value']!r} in the LIMITS table",
                actual="the LIMITS table has no rows" if table else
                       "no LIMITS table is on the record's page",
                detail="The limit row was saved — the run confirmed the table "
                       "gained a row — so an empty table here means it did "
                       "not survive the round trip."
                if table else
                "Without the table there is nothing to compare against.",
                evidence=[shot], screen=screen))
        return checks

    head = [str(h) for h in table["head"]]
    row = [str(c) for c in table["rows"][0]]
    say_row = ", ".join(f"{h}={c!r}" for h, c in zip(head, row))

    for e in items:
        label, typed = e["label"], e["value"]
        name = f"{e.get('group') or screen}: {label} carried through"
        col = next((i for i, h in enumerate(head)
                    if _norm(h) == _norm(label)), None)

        if col is None:
            checks.append(R.Check(
                name=name, status=R.BLOCKED,
                detail=f"The LIMITS table has no {label!r} column — it shows "
                       f"{', '.join(head)} — so what the record stored for it "
                       f"cannot be read from this page."
                       + (" Its value is covered by the Total FE Limits "
                          "figure, which is checked separately."
                          if "fe limit" in _norm(label) else ""),
                evidence=[shot], screen=screen))
            continue

        shown = row[col] if col < len(row) else ""
        if shown and flows._same_value(typed, shown, e.get("kind", "text")):
            checks.append(R.passed(
                name,
                detail=f"Entered {typed!r}; the LIMITS table's {head[col]!r} "
                       f"column shows {shown!r}.", screen=screen))
        else:
            checks.append(R.failed(
                name,
                expected=f"{typed!r} in the LIMITS table's {head[col]!r} "
                         f"column",
                actual=f"{shown!r}" if shown else "an empty cell",
                detail=f"The saved limit row reads: {say_row}.",
                evidence=[shot], screen=screen))
    return checks


def _open_our_ecib_record(s: Session, res: CaseFlowResult,
                          cap: int = 12) -> tuple[bool, str]:
    """
    Open the eCIB record THIS run saved, identified by its run marker.

    The marker is in Company/Individual Name, which is one of the columns the
    list renders — so the list is SEARCHED for it first and only paged as a
    fallback. Same reasoning as the bank relationship list, which pages at
    five rows and had the new record on page two.
    """
    marker = flows._norm_value(res.marker)
    marked = next((fl for fl in ECIB_PASS.fields if fl.marked), None)
    labels = list(marked.labels) if marked else ["Company/Individual Name"]

    searched = False
    try:
        searched = W.Filler(session=s, screen=SCREEN_LABEL[ECIB_DETAILS]
                            ).search_grid(res.marker)
    except (W.WriteRefused, W.FillError):
        searched = False
    if searched:
        s.page.wait_for_timeout(800)

    try:
        available = s.row_opener_count(cap)
    except Exception:  # noqa: BLE001 - an empty list is ordinary
        available = 0
    if searched and not available:
        try:
            W.Filler(session=s, screen=SCREEN_LABEL[ECIB_DETAILS]
                     ).search_grid("")
            available = s.row_opener_count(cap)
        except Exception:  # noqa: BLE001
            available = 0

    for i in range(min(cap, available)):
        try:
            if not s.open_row_detail(i):
                continue
        except Exception:  # noqa: BLE001
            break
        f = W.Filler(session=s, screen=SCREEN_LABEL[ECIB_DETAILS])
        got = ""
        for label in labels:
            got = f.value_of(label)
            if got:
                break
        if not marker or marker in flows._norm_value(got):
            return True, f"row {i + 1} of {available}: {got[:110]}"
        s.leave_row_detail()
    return False, (f"No eCIB record on this case carries this run's marker "
                   f"({res.marker}) in {labels[0]!r}, so the one it saved "
                   f"could not be identified among the {available} row(s) on "
                   f"the list"
                   + (" — and the list was searched for the marker first, "
                      "not just paged." if searched else
                      ", and the list's search box could not be used."))


def _verify_ecib_details(s: Session, res: CaseFlowResult,
                         items: list[dict], say) -> list[R.Check]:
    """
    Step 8: re-open the eCIB record and re-read every field, section by
    section.

    One check per field, so a single mismatch cannot hide the rest. The
    computed figures are checked AGAIN here, because the specification asks
    for exactly that: a figure that is right until the page is reloaded and
    wrong afterwards is a different defect from one that was never computed.
    """
    screen = SCREEN_LABEL[ECIB_DETAILS]
    checks: list[R.Check] = []
    shot = s.screenshot("verify-ecib-list")

    found, note = _open_our_ecib_record(s, res)
    if not found:
        for e in items:
            checks.append(R.Check(
                name=f"{e.get('group') or screen}: {e['label']} carried "
                     f"through",
                status=R.BLOCKED, detail=note, evidence=[shot],
                screen=screen))
        say(f"  {note}")
        return checks

    say(f"  re-opened the eCIB record this run created — {note}")
    s.page.wait_for_timeout(1500)
    f = W.Filler(session=s, screen=screen)
    shot = s.screenshot("verify-ecib-detail")

    # The dialog's three fields are recorded against a different group and
    # are not all on the record's page under the same names, so they go
    # through the ordinary comparison along with everything else — _compare
    # reports a field it cannot find as BLOCKED rather than as a loss.
    page_text = flows._screen_text(s)
    checks.extend(_compare(f, items, screen, page_text, shot, 1))

    # And the computed figures, on the re-opened record.
    entered = {e["label"]: e["value"] for e in items}
    before = len(res.checks)
    _check_ecib_sums(s, res, entered, say, "after re-opening")
    checks.extend(res.checks[before:])
    del res.checks[before:]
    return checks


def _verify_bank_relationship(s: Session, res: CaseFlowResult,
                              items: list[dict], say) -> list[R.Check]:
    """
    Step 6: re-open the relationship this run created and re-read every field.

    One check per field, as the specification asks, so a single mismatch
    cannot hide the rest — which is what the whole suite does anyway, and the
    reason it does.

    The list page shows only six of the record's columns, so reading it alone
    would report the editors, the dates and the limit row as lost. The record
    is therefore re-OPENED, and then its own fields, its LIMITS table and its
    five aggregate figures are all read off the record's page.
    """
    screen = SCREEN_LABEL[BANK_RELATIONSHIPS]
    checks: list[R.Check] = []
    shot = s.screenshot("verify-rel-list")

    found, note = _open_our_bank_relationship(s, res)
    if not found:
        for e in items:
            checks.append(R.Check(
                name=f"{e.get('group') or screen}: {e['label']} carried "
                     f"through",
                status=R.BLOCKED, detail=note, evidence=[shot],
                screen=screen))
        say(f"  {note}")
        return checks

    say(f"  re-opened the relationship this run created — {note}")
    s.page.wait_for_timeout(1500)
    f = W.Filler(session=s, screen=screen)
    shot = s.screenshot("verify-rel-detail")

    # The record's own fields go through the ordinary comparison — they ARE
    # labelled fields here. The limit row does not: it is a table row, and it
    # gets its own column-by-column comparison. See _verify_bank_limits.
    limits = [e for e in items
              if (e.get("group") or "") == BANK_LIMIT_PASS.name]
    own = [e for e in items if e not in limits]

    page_text = flows._screen_text(s)
    checks.extend(_compare(f, own, screen, page_text, shot, 1))
    if limits:
        checks.extend(_verify_bank_limits(s, limits, screen, shot))

    # The five aggregates, read back after the round trip rather than only
    # before it: an aggregate that was right on screen and wrong after a
    # reload is a different defect from one that was never computed.
    for label in BANK_SUMMARY_FIELDS:
        shown = f.value_of(label)
        checks.append(R.Check(
            name=f"{screen}: {label} is still populated after re-opening",
            status=R.PASS if shown.strip() else R.BLOCKED,
            detail=f"Shows {shown!r}." if shown.strip() else
                   "Empty when the record was re-opened.",
            evidence=[] if shown.strip() else [shot], screen=screen))
    return checks


def _open_our_facility(s: Session, res: CaseFlowResult, say,
                       cap: int = 12) -> tuple[bool, str]:
    """
    Open the facility THIS run created, identified by its run marker.

    Rows are opened one at a time and closed again until the marker is found.
    Falling back to the first row would be worse than failing: it would compare
    this run's values against somebody else's facility and call them lost.
    """
    marker = flows._norm_value(res.marker)
    try:
        available = s.row_opener_count(cap)
    except Exception:  # noqa: BLE001 - an empty grid is ordinary, not an error
        available = 0
    for i in range(min(cap, available)):
        try:
            if not s.open_row_detail(i):
                continue
        except Exception:  # noqa: BLE001
            break
        text = flows._screen_text(s)
        if not marker or marker in flows._norm_value(text):
            return True, (res.facility_ref or text[:120])
        # Not ours — check its tabs too before moving on, since the marker was
        # typed into Purpose of Facility which may not be the tab it opens on.
        found = False
        for tab in s.tab_strip(limit=8):
            if tab["active"]:
                continue
            try:
                s.open_tab(tab["label"])
            except NavigationError:
                continue
            if marker in flows._norm_value(flows._screen_text(s)):
                found = True
                break
        if found:
            return True, (res.facility_ref or "the facility carrying this "
                                              "run's marker")
        s.leave_row_detail()
    return False, (f"No facility on this case carries this run's marker "
                   f"({res.marker}), so the one it created could not be "
                   f"identified among the {available} row(s) on the grid. That "
                   f"usually means the facility was not saved.")


# The three the application works out for itself, by index into BP_ROWS. Named
# so the arithmetic checks below read as arithmetic rather than as offsets.
_BP_MARKUP, _BP_COMMISSION, _BP_OTHER_FEE = 0, 1, 2


_BP_TREASURY, _BP_TOTAL_EARNINGS, _BP_AVG_FUNDED = 4, 5, 6


_BP_BORROWING, _BP_ANNUALIZED, _BP_NET = 7, 8, 9
