"""
Declarative navigation targets.

These screens have no URL you can navigate to directly. Obligor Basic Information
is reached by a PATH, and the crawl trail proved it:

    All Obligors -> open a record -> Basic Information / Sector And Industry / ...

So a target is a list of hops, each resolved with machinery crawler.py already
has: navigate_in_app for the route, collect_row_openers to open a record,
collect_context_menu for the case menu, collect_tabs for the tab strip.

A target may then walk SEVERAL sub-screens of that record, checking each one
opens and works — which is how one run covers Basic Information, Sector And
Industry, Management & Shareholders and the rest in sequence.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# Step kinds
MENU = "menu"                  # navigate to a top-level route by URL path
ROW = "row"                    # open the first record in the grid
ROW_BY_ID = "row_by_id"        # open a SPECIFIC record, searching page by page
CONTEXT_MENU = "context_menu"  # click a case-scoped sidebar entry
TAB = "tab"                    # click a tab / wizard step
ACTION = "action"              # click a named action button (create flows, Phase 2)


@dataclass
class NavStep:
    kind: str
    label: str = ""
    path: str = ""
    # For ROW_BY_ID: the record identifier to look for. "{case_id}" is filled in
    # at run time from --case-id / LOS_CASE_ID, so the same target can be pointed
    # at a different record without editing code.
    value: str = ""
    # A tab that is already active needs no click. `collect_tabs` skips active
    # tabs, which is why the crawl never listed "Basic Information" — it is the
    # default tab. Treating that as a failure would break the main target.
    satisfied_if_active: bool = False
    # Is a missing hop the app's fault or the environment's? A missing menu entry
    # is a regression (FAIL); a record that is not in the grid is missing test
    # data (BLOCKED).
    missing_is_blocked: bool = False

    def describe(self) -> str:
        if self.kind == MENU:
            return f"open {self.label or self.path}"
        if self.kind == ROW:
            return "open the first record in the grid"
        if self.kind == ROW_BY_ID:
            return f"find and open record {self.value or self.label}"
        if self.kind == CONTEXT_MENU:
            return f"click '{self.label}' in the case menu"
        if self.kind == TAB:
            return f"open the '{self.label}' tab"
        if self.kind == ACTION:
            return f"click '{self.label}'"
        return f"{self.kind} {self.label}".strip()


@dataclass
class SubScreen:
    """One sub-screen of an opened record."""
    label: str                                   # the tab label in the app
    satisfied_if_active: bool = False            # true for the default tab

    # How this screen is reached from the record that is already open:
    #
    #   TAB          — a tab / pill inside the screen currently showing. The
    #                  obligor sub-screens work this way.
    #   CONTEXT_MENU — an entry in the CASE's own sidebar. Facilities,
    #                  Collaterals, Queries and the rest are each their own
    #                  route, reached by clicking the sidebar rather than a tab,
    #                  so they cannot be modelled as tabs.
    kind: str = TAB

    # Screens nested inside this one, visited straight after it. Obligor Details
    # (BIR) is a sidebar entry whose own content is a tab strip, so it is a
    # CONTEXT_MENU parent holding TAB children.
    children: list["SubScreen"] = field(default_factory=list)

    # False for a parent that is only a doorway to its children. Without it
    # Obligor Details (BIR) would be reported twice — once as itself and once as
    # its default tab, Basic Information, which is the same screen.
    check_self: bool = True

    # Open the first row of the first grid and check the detail it reveals.
    #
    # This matters more than it looks. Facilities, Collaterals, Documents and
    # Conditions present a SUMMARY grid whose columns are only a few of a
    # record's values — the rest live in the row's detail view, so checking the
    # summary alone says nothing about a screen that does hold the data.
    open_row_detail: bool = False

    # Once the row detail is open, walk whatever tab strip it exposes. A
    # facility spreads its data over six tabs (Facility Details, Limits and
    # Exposures, Overdue, the pricing tab, Payment, Utilisation & Outstanding),
    # and their labels are discovered live rather than hard-coded, because the
    # strip varies by facility type.
    walk_inner_tabs: bool = False

    # What to call this screen in the report, when that has to differ from the
    # label clicked in the app. Two different screens are both labelled
    # "History" — the obligor's audit tab and the case's audit page — and
    # results are grouped by screen name, so without this the two merge into one
    # group and a finding on either points at the wrong screen.
    report_as: str = ""

    @property
    def name(self) -> str:
        """The screen's name in the report. `label` stays the DOM label."""
        return self.report_as or self.label


def flatten_screen_names(subs: list[SubScreen]) -> list[str]:
    """
    The screens a run PLANS to visit, parents before children.

    Row-detail and inner-tab passes are discovered while running and so are not
    in this list — the UI treats them as extra screens beyond the plan.
    """
    out: list[str] = []
    for s in subs:
        if s.check_self:
            out.append(s.name)
        out.extend(flatten_screen_names(s.children))
    return out


@dataclass
class Target:
    key: str
    title: str                       # shown in the UI, plain language
    area: str                        # UI grouping
    steps: list[NavStep]
    description: str = ""
    sub_screens: list[SubScreen] = field(default_factory=list)
    writable: bool = False           # True only for authored create/save flows

    # Which ids this route's grid actually contains. The two grids hold two
    # different formats, and an operator should not have to know that: when a
    # record is not found, these let the run say "that looks like a case id,
    # use the other route" instead of just "not found".
    id_pattern: str = ""             # regex ids here usually match
    id_description: str = ""         # e.g. "case / request IDs"
    id_example: str = ""             # e.g. "52224-2026"
    alternative_key: str = ""        # target to suggest if the id looks wrong

    def path_description(self) -> str:
        return " -> ".join(s.describe() for s in self.steps)

    def screen_names(self) -> list[str]:
        return flatten_screen_names(self.sub_screens) or ["(record)"]


# --------------------------------------------------------------------------
# Where the obligor sub-screens live
#
# Two routes reach obligor data, and they are different screens:
#
#   All Obligors -> open a record  =>  /obligorCustomer/customerDetails
#                                      <app-customer-details>, "Customer Profile",
#                                      inner menu <ul class="nav nav-pills">
#
#   My Bucket -> open a case       =>  the CA package, whose sidebar becomes
#                                      Credit Approval Memo, Obligor Details
#                                      (BIR), Queries, Facilities, ...
#
# "Obligor Details (BIR)" exists only on the second route.
# --------------------------------------------------------------------------

_OBLIGOR_LIST_PATH = "/riskNucleus/master/obligorCustomer"
_BUCKET_PATH = "/riskNucleus/master/bucket"

OBLIGOR_SUB_SCREENS = [
    SubScreen("Basic Information", satisfied_if_active=True),
    SubScreen("Sector And Industry"),
    SubScreen("Management & Shareholders"),
    SubScreen("Additional Information"),
    SubScreen("Contact and Address"),
    SubScreen("Limits"),
    SubScreen("Corporate Governance"),
    SubScreen("BBFS Details"),
    SubScreen("Attachments"),
    SubScreen("History"),
]


# --------------------------------------------------------------------------
# The credit case's OWN sidebar — every entry in snippet order
#
# Once a case is open from My Bucket the app replaces the main menu with the
# case menu: Credit Approval Memo, Obligor Details (BIR), Queries, Request
# Details, Facilities, Observations, Collaterals, Facility Coverage, Risk
# Rating, Financials, Credit Memorandum, eCIB Details, Policies & Exceptions,
# Conditions, Documents, CRMD Note, History, Relationship with Other Banks /
# FIs, Business Performance.
#
# Each of those is a ROUTE, not a tab, hence kind=CONTEXT_MENU. Labels are the
# ones the app renders; the driver matches them forgivingly, so the truncated
# "Relationship with Oth…" in the sidebar still resolves.
# --------------------------------------------------------------------------

CASE_SUB_SCREENS = [
    SubScreen("Credit Approval Memo", kind=CONTEXT_MENU),

    # The obligor screens, reached through the sidebar and then walked as tabs.
    # check_self=False because this entry IS the Basic Information tab: checking
    # both would report the same screen twice.
    SubScreen("Obligor Details (BIR)", kind=CONTEXT_MENU,
              check_self=False, children=OBLIGOR_SUB_SCREENS),

    SubScreen("Queries", kind=CONTEXT_MENU, open_row_detail=True),

    SubScreen("Request Details", kind=CONTEXT_MENU),

    # Facilities is the deepest screen in the case: a grid of facilities whose
    # detail view spreads its data across its own tab strip.
    SubScreen("Facilities", kind=CONTEXT_MENU, open_row_detail=True,
              walk_inner_tabs=True),

    SubScreen("Observations", kind=CONTEXT_MENU),

    # A collateral's data is specific to its type and spread over the record's
    # tabs (Basic Information, Collateral Policy, Pledge, and one tab per
    # collateral type), so the whole strip is walked.
    SubScreen("Collaterals", kind=CONTEXT_MENU, open_row_detail=True,
              walk_inner_tabs=True),

    SubScreen("Facility Coverage", kind=CONTEXT_MENU, open_row_detail=True),

    SubScreen("Risk Rating", kind=CONTEXT_MENU),

    SubScreen("Financials", kind=CONTEXT_MENU, open_row_detail=True,
              walk_inner_tabs=True),

    SubScreen("Credit Memorandum", kind=CONTEXT_MENU),

    # eCIB is populated by uploading the bureau file, into the liability tables
    # on its two tabs (e-CIB Details and e-CIB Trends).
    SubScreen("eCIB Details", kind=CONTEXT_MENU, open_row_detail=True,
              walk_inner_tabs=True),

    SubScreen("Policies & Exceptions", kind=CONTEXT_MENU),

    SubScreen("Conditions", kind=CONTEXT_MENU, open_row_detail=True),

    SubScreen("Documents", kind=CONTEXT_MENU, open_row_detail=True),

    # Renamed in the application from 'CRMD Note' — the sidebar entry is now
    # 'RMG Memo' (/ca-package/rmg-memo).
    SubScreen("RMG Memo", kind=CONTEXT_MENU),

    # report_as distinguishes this from the obligor's own History tab, which is
    # a different screen with the same label.
    SubScreen("History", kind=CONTEXT_MENU, report_as="History (case)"),

    SubScreen("Relationship with Other Banks / FIs", kind=CONTEXT_MENU,
              open_row_detail=True),

    SubScreen("Business Performance", kind=CONTEXT_MENU),
]


# The two grids hold different id formats, which is the single most common way
# to pick the wrong route:
#   My Bucket     -> case / request ids,  NNNNN-YYYY        e.g. 52224-2026
#   All Obligors  -> customer ids,        PREFIX-NNNNNN-YYYY e.g. CIBG-186283-2026
CASE_ID_PATTERN = r"^\d{4,6}-\d{4}$"
CUSTOMER_ID_PATTERN = r"^[A-Za-z]{2,6}-\d{4,8}-\d{4}$"


TARGETS: dict[str, Target] = {
    # ---- the main Phase 1 target: via the credit case ----------------------
    # "case.obligor": Target(
    #     key="case.obligor",
    #     title="Obligor Details (BIR) — all sub-screens",
    #     area="Credit case (case IDs like 52224-2026)",
    #     description=(
    #         "Finds the case in My Bucket, opens Obligor Details (BIR), then walks "
    #         "every sub-screen — Basic Information, Sector And Industry, Management "
    #         "& Shareholders, Additional Information and the rest — checking each "
    #         "checking that each one opens and works."
    #     ),
    #     steps=[
    #         NavStep(kind=MENU, label="My Bucket", path=_BUCKET_PATH),
    #         NavStep(kind=ROW_BY_ID, value="{case_id}", missing_is_blocked=True),
    #         NavStep(kind=CONTEXT_MENU, label="Obligor Details (BIR)"),
    #     ],
    #     sub_screens=OBLIGOR_SUB_SCREENS,
    #     id_pattern=CASE_ID_PATTERN,
    #     id_description="case / request IDs",
    #     id_example="52224-2026",
    #     alternative_key="obligor.record",
    # ),

    # ---- the whole case: every entry in the case's own sidebar -------------
    # "case.all_screens": Target(
    #     key="case.all_screens",
    #     title="Credit case — every screen in the case menu",
    #     area="Credit case (case IDs like 52224-2026)",
    #     description=(
    #         "Finds the case in My Bucket and then walks its entire sidebar — "
    #         "Credit Approval Memo, Obligor Details (BIR) and all its tabs, "
    #         "Queries, Request Details, Facilities, Observations, Collaterals, "
    #         "Facility Coverage, Risk Rating, Financials, Credit Memorandum, "
    #         "eCIB Details, Policies & Exceptions, Conditions, Documents, CRMD "
    #         "Note, History, Relationship with Other Banks / FIs and Business "
    #         "Performance — checking that each one opens, renders its content, "
    #         "and reports no server error, validation gap or browser error. On "
    #         "the grid screens it also opens the first record and walks its "
    #         "tabs, because that is where a record's data actually lives."
    #     ),
    #     steps=[
    #         NavStep(kind=MENU, label="My Bucket", path=_BUCKET_PATH),
    #         NavStep(kind=ROW_BY_ID, value="{case_id}", missing_is_blocked=True),
    #     ],
    #     sub_screens=CASE_SUB_SCREENS,
    #     id_pattern=CASE_ID_PATTERN,
    #     id_description="case / request IDs",
    #     id_example="52224-2026",
    # ),

    # ======================================================================
    # DISABLED ON REQUEST — two menu sections were commented out here so the
    # portal offers only the credit-case routes.
    #
    # To bring either back: delete the '# ' from its block below and nothing
    # else. Both were left byte-for-byte intact, and app.py / cli.py pick up
    # whatever is in this dict, so no other file needs touching.
    #
    #   1. "case.obligor_basic" — "Obligor Details (BIR) — Basic Information
    #      only". The quick single-screen variant of case.obligor.
    #   2. "obligor.record" — "Customer Profile — all sub-screens", the whole
    #      "All Obligors (customer IDs like CIBG-186283-2026)" section. It is
    #      the only target in that area, so restoring it restores the section.
    #
    # Note while (2) is commented out, a customer ID such as CIBG-186283-2026
    # matches no remaining route, so the sidebar will say the id does not fit
    # either grid. That is accurate: there is no route for it right now.
    # ======================================================================

    # "case.obligor_basic": Target(
    #     key="case.obligor_basic",
    #     title="Obligor Details (BIR) — Basic Information only",
    #     area="Credit case (case IDs like 52224-2026)",
    #     description="The same route, but checks Basic Information alone. Quicker "
    #                 "when that is all you need.",
    #     steps=[
    #         NavStep(kind=MENU, label="My Bucket", path=_BUCKET_PATH),
    #         NavStep(kind=ROW_BY_ID, value="{case_id}", missing_is_blocked=True),
    #         NavStep(kind=CONTEXT_MENU, label="Obligor Details (BIR)"),
    #     ],
    #     sub_screens=[OBLIGOR_SUB_SCREENS[0]],
    #     id_pattern=CASE_ID_PATTERN,
    #     id_description="case / request IDs",
    #     id_example="52224-2026",
    #     alternative_key="obligor.record",
    # ),

    # ---- the other route, straight from All Obligors -----------------------
    # "obligor.record": Target(
    #     key="obligor.record",
    #     title="Customer Profile — all sub-screens",
    #     area="All Obligors (customer IDs like CIBG-186283-2026)",
    #     description=(
    #         "Finds a customer record directly in All Obligors and walks its "
    #         "sub-screens. Use this only with a customer ID — a case ID such as "
    #         "52224-2026 is not in this grid."
    #     ),
    #     steps=[
    #         NavStep(kind=MENU, label="All Obligors", path=_OBLIGOR_LIST_PATH),
    #         NavStep(kind=ROW_BY_ID, value="{case_id}", missing_is_blocked=True),
    #     ],
    #     sub_screens=OBLIGOR_SUB_SCREENS,
    #     id_pattern=CUSTOMER_ID_PATTERN,
    #     id_description="customer IDs",
    #     id_example="CIBG-186283-2026",
    #     alternative_key="case.obligor",
    # ),
}


def get(key: str) -> Target:
    if key not in TARGETS:
        raise KeyError(f"Unknown target {key!r}. Known: {', '.join(sorted(TARGETS))}")
    return TARGETS[key]


def by_area() -> dict[str, list[Target]]:
    out: dict[str, list[Target]] = {}
    for t in TARGETS.values():
        out.setdefault(t.area, []).append(t)
    for v in out.values():
        v.sort(key=lambda t: t.title)
    return out


def resolve(target: Target, case_id: str) -> Target:
    """Substitute the run's case id into any {case_id} placeholders."""
    steps = [
        NavStep(kind=s.kind, label=s.label, path=s.path,
                value=(s.value or "").replace("{case_id}", case_id or ""),
                satisfied_if_active=s.satisfied_if_active,
                missing_is_blocked=s.missing_is_blocked)
        for s in target.steps
    ]
    return Target(
        key=target.key, title=target.title, area=target.area, steps=steps,
        description=target.description, sub_screens=target.sub_screens,
        writable=target.writable,
        id_pattern=target.id_pattern, id_description=target.id_description,
        id_example=target.id_example, alternative_key=target.alternative_key)


def route_advice(case_id: str, current_key: str = "") -> str:
    """
    Which route suits this id? Returns operator-readable guidance, or "" when
    the id already matches the route in use.

    This exists because the failure it prevents was a usability bug, not a
    technical one: an operator saw "not found after 92 rows" and had no way to
    know the id simply belongs to the other grid.
    """
    import re
    cid = (case_id or "").strip()
    if not cid:
        return ""
    for t in TARGETS.values():
        if t.id_pattern and re.match(t.id_pattern, cid):
            if current_key and current_key == t.key:
                return ""
            if current_key and TARGETS.get(current_key) and \
                    TARGETS[current_key].id_pattern and \
                    re.match(TARGETS[current_key].id_pattern, cid):
                return ""
            return (f"{cid} looks like one of the {t.id_description} "
                    f"(e.g. {t.id_example}), which are found via "
                    f"“{t.title}”.")
    return ""


def suggested_target(case_id: str) -> str:
    """The target key whose id format matches, if any."""
    import re
    cid = (case_id or "").strip()
    for key, t in TARGETS.items():
        if t.id_pattern and re.match(t.id_pattern, cid):
            return key
    return ""
