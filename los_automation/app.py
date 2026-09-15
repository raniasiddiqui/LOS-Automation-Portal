"""
LOS Automation Portal — the web page.

Run it from the project root:

    streamlit run los_automation/app.py

Design notes that matter:

  - Runs happen in a SEPARATE PROCESS (runner/jobs.py). Streamlit re-executes
    this file top to bottom on every interaction, so anything long-running has to
    outlive the script run that started it. It also keeps Playwright's sync API
    away from Streamlit's asyncio loop, which would otherwise refuse to start.
  - While a run is going the page shows the live step list and the latest
    screenshot, refreshing itself. That is the "watch it work" view, and unlike a
    headed browser it still works when this is hosted on a server.
  - Nothing here can write to the application. Phase 1 is read-only.
  - Every download is built by runner/exports.py, from the same rows the tables
    on this page are built from. A result has one description, not three.
"""
import os
import re
import sys
import time

# Allow `streamlit run los_automation/app.py` from the project root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import streamlit as st

import config as crawler_config
from los_automation import settings
from los_automation.runner import exports as X
from los_automation.runner import jobs
from los_automation.runner import results as R
from los_automation.runner import targets as tg

st.set_page_config(page_title="LOS Automation Portal", page_icon="🧪",
                   layout="wide")

REFRESH_SECONDS = 2

BADGE = {
    R.PASS: ("✅", "#1a7f37", "Passed"),
    R.FAIL: ("❌", "#b42318", "Failed"),
    R.BLOCKED: ("⏸️", "#b54708", "Could not check"),
}


# --------------------------------------------------------------------------
# Sidebar: environment
# --------------------------------------------------------------------------

def render_sidebar() -> bool:
    st.sidebar.title("Environment")
    st.sidebar.markdown("**Client:** NBP")
    st.sidebar.markdown(f"**Environment:** `{crawler_config.BASE_URL}`")

    # COMMENTED OUT ON REQUEST — the sign-in badge and the data-entry
    # banner. The sidebar is to match the agreed design, which carries the
    # client and the environment and nothing else above 'Record under test'.
    # Nothing here did any work: both blocks only rendered text. Uncomment to
    # put them back.
    #
    # if crawler_config.USERNAME:
    #     st.sidebar.success("Sign-in details configured", icon="🔑")
    # else:
    #     st.sidebar.error("No sign-in details — set QA_USERNAME / QA_PASSWORD "
    #                      "in .env", icon="🔑")
    #
    # allowed, reason = settings.write_allowed(crawler_config.BASE_URL)
    # if allowed:
    #     st.sidebar.warning(f"Data entry is permitted here.\n\n{reason}",
    #                        icon="✍️")
    #     st.sidebar.caption("Checks never change anything. Only **Create test "
    #                        "data** writes, and it asks first.")
    # else:
    #     st.sidebar.info(f"Data entry is blocked here.\n\n{reason}", icon="🔒")
    #     st.sidebar.caption("Every action on this page is read-only.")

    st.sidebar.divider()
    st.sidebar.subheader("Record under test")
    case_id = st.sidebar.text_input(
        "Case / obligor ID", value=st.session_state.get("case_id", settings.CASE_ID),
        help="Every check runs against this one record, so results are "
             "comparable between runs. A fill run types this id into My "
             "Bucket's own search box, so the case is found whatever page it "
             "is on; a read-only check pages the grid instead, because it "
             "types nothing at all.")
    st.session_state["case_id"] = case_id.strip()
    if not case_id.strip():
        st.sidebar.error("Enter a record id before running a check.")

    # NO 'Occurances' / 'How many obligors to create' control here, on
    # request. It belonged to Create test data, which is commented out below,
    # and the agreed sidebar does not carry it.

    # COMMENTED OUT ON REQUEST — the id-format hint, and the 'What we compare
    # against' note. Display only; suggested_target is still used by
    # render_menu, so nothing else is affected.
    #
    # else:
    #     # The two grids hold different id formats; say which route fits so
    #     # the operator does not have to know.
    #     suggested = tg.suggested_target(case_id.strip())
    #     if suggested:
    #         st.sidebar.success(
    #             f"Looks like a {tg.get(suggested).id_description[:-1]} — use "
    #             f"**{tg.get(suggested).title}**.", icon="🧭")
    #     else:
    #         st.sidebar.warning(
    #             "That id does not match either grid's usual format "
    #             "(case IDs look like 52224-2026, customer IDs like "
    #             "CIBG-186283-2026).", icon="🧭")
    #
    # st.sidebar.divider()
    # st.sidebar.subheader("What we compare against")
    # st.sidebar.caption(
    #     "Only what this tool entered itself. A **Check** confirms a screen "
    #     "opens, renders, and reports no error; the **Fill and check** buttons "
    #     "type a value, save it, re-open the record and compare what came back "
    #     "against what went in. Both halves of that comparison come from the "
    #     "same run, which is what makes it trustworthy.")

    st.sidebar.divider()
    headed = st.sidebar.checkbox(
        "Show the browser window", value=False,
        help="Opens a real browser on the machine running this page. Useful on "
             "your own laptop; it will not be visible if this is hosted on a "
             "server — the screenshots below work either way.")
    st.session_state["headed"] = headed
    return True


# --------------------------------------------------------------------------
# Menu of things that can be checked
# --------------------------------------------------------------------------

def render_menu() -> None:
    case_id = st.session_state.get("case_id", settings.CASE_ID)
    st.subheader("Choose what to check")
    st.caption(f"All checks run against record **{case_id or '(not set)'}** — "
               f"change it in the sidebar.")
    job = st.session_state.get("job")
    busy = (job is not None and job.running) or not case_id
    suggested = tg.suggested_target(case_id)

    # Matching route first, so the obvious choice is also the top one.
    areas = sorted(tg.by_area().items(),
                   key=lambda kv: (0 if any(t.key == suggested for t in kv[1])
                                   else 1, kv[0]))
    for area, items in areas:
        fits = any(t.key == suggested for t in items)
        st.markdown(f"**{area}**" + ("  ✅ matches your ID" if fits else ""))
        for t in items:
            recommended = t.key == suggested
            # Whether the id fits THIS route, not whether this is the one route
            # suggested. Two targets can share a grid — the obligor-only check
            # and the whole-case check both read case ids out of My Bucket — and
            # keying the warning off `recommended` told the operator their id was
            # not in a grid it plainly is in.
            fits_this = bool(t.id_pattern and re.match(t.id_pattern, case_id))
            mismatch = bool(suggested) and not fits_this
            left, right = st.columns([5, 1])
            with left:
                st.markdown(("⭐ " if recommended else "") + t.title)
                st.caption(t.description or t.path_description())
                if t.sub_screens:
                    st.caption("Screens: " +
                               " · ".join(s.label for s in t.sub_screens))
                if mismatch:
                    st.caption(f"⚠️ {case_id} is not one of the "
                               f"{t.id_description or 'ids'} in this grid — this "
                               f"will report 'not found'.")
            with right:
                if st.button("Check", key=f"run-{t.key}", disabled=busy,
                             type="primary" if recommended else "secondary",
                             width="stretch"):
                    start(t.key)
        st.divider()


def start(target_key: str) -> None:
    job = jobs.start_verify(target_key,
                            headed=st.session_state.get("headed", False),
                            case_id=st.session_state.get("case_id"))
    st.session_state["job"] = job
    st.session_state["last_target"] = target_key
    st.rerun()


# --------------------------------------------------------------------------
# Phase 2: creating test data
#
# Deliberately a separate section with its own heading and its own warning.
# Everything above this point only reads; this writes. Blurring the two in one
# list of buttons is how somebody eventually creates a record by accident.
# --------------------------------------------------------------------------

# COMMENTED OUT ON REQUEST — the entire Create test data menu, both the
# section and the job it started. This also removes the 'Occurances / how
# many obligors to create' idea along with it.
#
# NOTHING BEHIND IT WAS TOUCHED: flows.create_obligor still exists,
# jobs.start_create still exists, and the CLI still offers --create-obligor.
# Only the page stops offering it. render_create_progress and render_created
# are deliberately NOT commented out — they render a create run's result, and
# a run started from the CLI still reports through this page.
#
# def render_create() -> None:
#     st.subheader("Create test data")
#     allowed, reason = settings.write_allowed(crawler_config.BASE_URL)
#
#     if not allowed:
#         st.info(f"Data entry is blocked on this environment, so this section "
#                 f"cannot run.\n\n{reason}", icon="🔒")
#         return
#
#     st.caption(
#         "Creates a **new obligor**, fills **every enterable field** of Basic "
#         "Information — not just the thirteen mandatory ones — saves it, then "
#         "fills each of the nine tabs that unlock, including every one of their "
#         "eighteen tables. It then raises a **Borrower Credit Application**, "
#         "finds the resulting case in My Bucket, opens Obligor Details (BIR) "
#         "and checks every value it entered actually carried through.")
#     st.caption("Roughly 270 fields across 25 forms, so a full run takes a "
#                "while — the live view below shows which field it is on.")
#     st.warning(
#         f"This writes to {crawler_config.BASE_URL}. A live run leaves a real "
#         f"obligor and a real credit case behind — they cannot be removed from "
#         f"here. Use a dry run first: it fills every field and reports what the "
#         f"form holds without clicking Save.", icon="✍️")
#
#     job = st.session_state.get("job")
#     busy = job is not None and job.running
#
#     left, right = st.columns([3, 2])
#     with left:
#         name = st.text_input(
#             "Obligor name", value=st.session_state.get("new_obligor_name", ""),
#             placeholder="leave blank for AUTOMATION TEST <timestamp>",
#             help="Blank gives a timestamped name, which keeps runs unique — the "
#                  "app de-duplicates, and the verification step has to find this "
#                  "exact record in My Bucket afterwards.")
#         st.session_state["new_obligor_name"] = name.strip()
#     with right:
#         stop_after = st.checkbox(
#             "Stop after saving the obligor", value=False,
#             help="Creates the obligor but does not raise a transaction, so no "
#                  "credit case is created.")
#
#     a, b = st.columns(2)
#     with a:
#         if st.button("Dry run — fill everything, save nothing",
#                      disabled=busy, width="stretch"):
#             start_create(dry_run=True, name=name.strip(),
#                          stop_after_save=stop_after)
#     with b:
#         confirm = st.checkbox("I understand this creates real records",
#                               key="confirm_write")
#         if st.button("Create for real", type="primary",
#                      disabled=busy or not confirm, width="stretch"):
#             start_create(dry_run=False, name=name.strip(),
#                          stop_after_save=stop_after)
#
#
# def start_create(dry_run: bool, name: str, stop_after_save: bool) -> None:
#     job = jobs.start_create(dry_run=dry_run, obligor_name=name,
#                             headed=st.session_state.get("headed", False),
#                             stop_after_save=stop_after_save)
#     st.session_state["job"] = job
#     st.session_state["last_target"] = "obligor.create"
#     st.rerun()


# --------------------------------------------------------------------------
# Phase 2b: filling the case's own screens
#
# A separate button per screen, because they are separate jobs with separate
# costs. Facilities requests a facility and then walks every tab of it, which
# takes minutes and leaves a facility on the case; someone checking Request
# Details should be able to do that alone.
# --------------------------------------------------------------------------

CASE_SCREENS = [
    # COMMENTED OUT ON REQUEST — Request Details, Facilities and Observations.
    # Only their BUTTONS are removed. The flows themselves are untouched and
    # still reachable from the CLI (--fill-case request_details, facilities,
    # observations), and they are still in case_flows.ORDER — see the note on
    # the 'Check all' button below, which matters.
    #
    # ("request_details", "Request Details",
    #  "Fills the request form — request type, the proposed expiry and the rest "
    #  "of section 7.2 — and adds a row to its Purpose of Request table."),
    # ("facilities", "Facilities",
    #  "Selects the requested facility and presses Proceed, then fills every tab "
    #  "the facility opens with: request details, facility details, limits and "
    #  "exposures, overdue, pricing, payment and utilisation."),
    # ("observations", "Observations",
    #  "Records an observation against the case — all twelve fields on the Add "
    #  "Observation panel, then Save."),
    ("litigation", "Litigation",
     "Opens the litigation entry form from the screen's '+' and records a "
     "suit — type of suit, filing date, the court, the bank's lawyer and law "
     "firm, both hearing dates, the suit amount, the proceeding details and "
     "the date of decree — then Save."),
    ("shariah_comments", "Shariah Comments",
     "Writes the screen's one field — the SCD Remarks rich-text editor — and "
     "saves it. The quickest of these checks, and the one that rests entirely "
     "on the round trip: there is no list or grid to grow, so whether the "
     "editor's content was stored is only answered by re-opening the case."),
    # Renamed in the application from 'CRMD Note'. The key stays crmd_note —
    # it is what --fill-case and the jobs layer take.
    ("crmd_note", "RMG Memo",
     "Writes all eight narrative editors — recommendation, industry strategy, "
     "internal indicators, audit observations, return on capital, key credit "
     "concerns, risk observations and risk exposure strategy — and saves "
     "them. Each box gets its own text naming its own section, so the round "
     "trip can tell which one came back."),
    ("credit_memorandum", "Credit Memorandum",
     "Writes all thirty-three narrative editors — background and rationale, "
     "the financial projections and variance sections, industry and internal "
     "indicators, ways out, collateral justification, governance and the rest "
     "— and saves them. The longest of these runs by a distance: every editor "
     "is typed into and then read back before saving."),
    ("group_review", "Group Review",
     "Writes all seven narrative editors — group background, group companies, "
     "industry and peer analysis, trade business through NBP, relationship "
     "yield, relationship strategy and risk advice — and saves them. This "
     "screen is not on every case: where the case menu does not offer it, the "
     "run says so rather than reporting a failure."),
    ("business_performance", "Business Performance",
     "Covers BOTH sub-menus in one check — Customer Business Performance and "
     "Group Business Reciprocity. Each is a grid of ten figures against a "
     "period: it opens the sub-menu, enters every figure the screen lets it, "
     "saves, and then goes back inside that same sub-menu to read each one "
     "back. The two are given deliberately different numbers, because they "
     "share all ten row names and identical values would let a figure stored "
     "against the wrong sub-menu pass both checks. The three figures the "
     "application works out for itself — total income, annualized yield, net "
     "yield — are left alone and checked against its own arithmetic instead."),
    ("bank_relationships", "Relationship with Other Banks / FIs",
     "Creates a bank relationship end to end: checks the Add dialog refuses "
     "an empty Bank Name, chooses a bank and Proceeds, adds a row to the "
     "LIMITS table through its own '+', writes the three editors, the two "
     "text areas, the expiry date and the classification status, and Saves. "
     "The five figures under the LIMITS table are computed by the "
     "application, so they are checked against the limit row rather than "
     "entered. Because the relationship is created by this run, the round "
     "trip re-opens that record specifically rather than the first on the "
     "list."),
    ("ecib_details", "eCIB Details",
     "The biggest screen here, and BOTH ways a record can be created on it — "
     "one after the other, reported separately.\n\n"
     "**1. The '+ Add' route.** Checks the dialog refuses an empty form with "
     "all three of its messages, creates the record, then fills the record's "
     "page — basic details, limits, outstanding liabilities, rescheduled, "
     "write-offs and overdue — and Saves. Three sections are gated by a "
     "Yes/No dropdown that has to be answered before their fields will "
     "accept anything, and seven figures are computed by the application: "
     "those are checked against their own arithmetic both before and after "
     "the record is re-opened.\n\n"
     "**2. The '+ Upload' route.** Attaches a PDF of a State Bank CIB "
     "report, picks the eCIB type and status, presses Proceed — then checks "
     "the record the application BUILT against what the document actually "
     "says, on the list and again on the record's own page. Nothing is typed "
     "except the type and the status, so what is under test is the "
     "extraction. Two documents go through: a corporate report (AKHUWAT) and "
     "an individual one (ABDUR RAUF KHAN). Unlike the Add dialog, Proceed "
     "here writes the record outright — there is no Save — and it OVERWRITES "
     "any record held against the same borrower code rather than adding a "
     "second, so a dry run can only check the dialog."),
    ("pr_checklist", "PR Checklist",
     "Presses **Perform PR**, then walks the checklist form section by "
     "section. The sections are configuration, so nothing is authored: it "
     "iterates every 'PR Factor' row it finds, sets each editable 'Factor "
     "Value', and asserts the frozen ones (Margin Requirement) really are "
     "frozen rather than skipping them quietly. 'Regulatory Requirement', "
     "'Actual Value' and 'Compliant Status' are never typed into — the "
     "application computes those.\n\n"
     "Then **Generate first, Save second** — that order matters, since "
     "Generate is what fills the three computed columns and saving first "
     "would store them empty. The computed values are snapshotted between "
     "the two, and that snapshot is the expected data: the run goes back to "
     "the PR RISK RATING LOG, checks a new row arrived with Perform By "
     "matching the signed-in user and both dates reading today, re-opens it "
     "through the eye icon, and compares every factor against the snapshot — "
     "one check per factor, so one mismatch cannot hide the rest. Finally it "
     "presses Generate Pdf and confirms a document actually comes back."),
    ("financials", "Financials",
     "One menu entry over four sub-tabs, each reported separately.\n\n"
     "**Financials Input:** presses **New Statement**, fills the Add "
     "Financials dialog and Proceeds — twice, for two different financial "
     "years. The tick on 'Audited financials?' goes first because it is what "
     "ENABLES Type of Auditor and Financial Auditor; the Start and End dates "
     "are never typed, because choosing the year fills them in and the app "
     "refuses a typed End Date. Then it walks the chart of accounts — 453 "
     "rows, of which 177 are editable and the rest are the application's own "
     "totals — filling what is enabled and skipping what is grey, and "
     "Saves.\n\n"
     "**Financial Variance Analysis:** presses Perform Financial Analysis "
     "and fills the VARIANCE COMMENTS column, which is the only writable one."
     "\n\n"
     "**Financial Peer Analysis:** presses Perform Peer Analysis, adds a peer "
     "through its dialog, presses Calculate, and writes the comments.\n\n"
     "**Financial Analysis:** writes all seven rich text editors and Saves — "
     "these are verified by the round trip.\n\n"
     "A financial year the case already holds cannot be added again, so set "
     "**LOS_FIN_YEARS** to years it does not have. LOS_FIN_MAX_CELLS caps how "
     "many of the 177 chart cells are filled (40 by default — every cell is a "
     "round trip through Angular, so filling all of them takes a long time)."),
    ("history", "History",
     "The only **read-only** check here — it writes nothing, so a dry run "
     "and a live run do exactly the same thing.\n\n"
     "**Workflow Log:** checks the grid's five columns, then clicks the "
     "**View Changes** link on the first row and asserts the screen actually "
     "responded — it does not treat a click that raised no error as a pass. "
     "Any real response counts (a dialog, a new tab, a route change, or the "
     "row expanding in place) and the report says which one happened; on "
     "this build it expands inline, taking the grid from 8 rows to 151.\n\n"
     "**Requests History:** checks that tab's six columns, then clicks a grid "
     "**row** — deliberately not the 'i' icon beside it — and asserts it "
     "navigates to that transaction, logging whichever screen it lands on. "
     "That click really does change transaction, so this check runs last and "
     "returns to the original case afterwards."),
]


def render_case_screens() -> None:
    st.subheader("Fill and check a case's screens")
    allowed, reason = settings.write_allowed(crawler_config.BASE_URL)
    case_id = st.session_state.get("case_id", settings.CASE_ID)

    if not allowed:
        st.info(f"Data entry is blocked on this environment, so this section "
                f"cannot run.\n\n{reason}", icon="🔒")
        return

    st.caption(
        f"Takes the case **{case_id or '(not set)'}** — which must already "
        f"exist in My Bucket — opens one of its screens, fills **every field "
        f"on it**, saves, and then re-opens the case from My Bucket and checks "
        f"each value came back. Coming back in through the grid is what makes "
        f"it a real check: a value the app dropped or reformatted is invisible "
        f"on the form that is still holding it.")
    st.warning(
        f"These buttons WRITE to case {case_id or '(not set)'} on "
        f"{crawler_config.BASE_URL}. What they save cannot be removed from "
        f"here.", icon="✍️")

    job = st.session_state.get("job")
    busy = (job is not None and job.running) or not case_id

    # COMMENTED OUT ON REQUEST — the dry-run / live choice. Every run from
    # this page is now a real one, so the confirmation below is no longer
    # conditional: it is always required.
    #
    # The dry-run capability itself is NOT removed — fill_case_screens still
    # takes dry_run, and `--fill-case <screen> --dry-run` still works from the
    # CLI. Only the control on this page is gone.
    #
    # mode = st.radio(
    #     "How should these run?",
    #     ["Dry run — fill everything, save nothing",
    #      "Fill, save, and verify by round trip"],
    #     index=0, horizontal=False, key="case_fill_mode",
    #     help="A dry run cannot reach the facility tabs: they do not exist "
    #          "until the requested facility has been submitted with Proceed, "
    #          "which a dry run deliberately does not press.")
    # dry_run = mode.startswith("Dry run")
    dry_run = False
    confirm = st.checkbox(
        "I understand this writes to the case", key="confirm_case_write")

    for key, title, blurb in CASE_SCREENS:
        left, right = st.columns([5, 1])
        with left:
            st.markdown(f"**{title}**")
            st.caption(blurb)
        with right:
            if st.button("Check", key=f"case-{key}",
                         disabled=busy or not confirm, width="stretch"):
                start_case_fill(key, dry_run)
    st.divider()
    # 'all' is NOT the list above. It goes through --fill-case all, which the
    # runner reads off case_flows.ORDER, so it still covers every screen —
    # including Request Details, Facilities and Observations, whose buttons
    # are commented out of CASE_SCREENS.
    #
    # The button therefore counts ORDER rather than CASE_SCREENS, and says so.
    # Counting the visible buttons would have promised "all 10" and quietly
    # run 13 — Facilities among them, which takes minutes and leaves a
    # facility on the case. To keep those three out of this button as well,
    # they have to come out of case_flows.ORDER, which is a change to the
    # runner rather than to this page.
    # No count in the label, deliberately. It used to read len(CASE_SCREENS),
    # and with three of those commented out it would have promised "all 10"
    # while running 13. The honest number lives in case_flows.ORDER, and this
    # page does not import case_flows on purpose — see the module docstring
    # about keeping Playwright out of the Streamlit process.
    if st.button("Check every case screen, in order",
                 disabled=busy or not confirm, type="primary",
                 width="stretch"):
        start_case_fill("all", dry_run)
    st.caption("They run one after the other and are verified together in a "
               "single round trip. This is EVERY case screen the runner "
               "knows — including Request Details, Facilities and "
               "Observations, whose individual buttons are commented out "
               "above. Slower than the individual buttons, and one long "
               "report rather than several short ones.")


def start_case_fill(screen: str, dry_run: bool) -> None:
    job = jobs.start_case_fill(
        screen=screen, case_id=st.session_state.get("case_id",
                                                    settings.CASE_ID),
        dry_run=dry_run, headed=st.session_state.get("headed", False))
    st.session_state["job"] = job
    st.session_state["last_target"] = "case.screens"
    st.rerun()


# --------------------------------------------------------------------------
# Live view + results
# --------------------------------------------------------------------------

def render_progress(job: jobs.Job) -> None:
    job.poll()
    # A create run is not one of the read-only targets, so there is no Target to
    # look up and no fixed screen list to measure against — its progress is the
    # step list it emits as it goes.
    target = tg.TARGETS.get(job.target_key)
    if target is None:
        render_create_progress(job)
        return

    st.subheader(f"Running: {target.title}")
    st.caption(f"Record {job.case_id}")

    # Progress spans getting there plus every screen checked.
    steps_done, screens_done = job.steps, job.screens
    planned = job.planned_screens or target.screen_names()
    total = len(target.steps) + max(len(planned), 1)
    st.progress(min((len(steps_done) + len(screens_done)) / total, 1.0))

    cur_screen, cur_step = job.current_screen, job.current_step
    if job.running and cur_screen and len(screens_done) < len(planned):
        st.caption(f"Checking screen {cur_screen.get('index')} of "
                   f"{cur_screen.get('total')}: {cur_screen.get('screen', '')}")
    elif job.running and cur_step:
        st.caption(f"Step {cur_step.get('index')} of {cur_step.get('total')}: "
                   f"{cur_step.get('text', '')}")

    left, right = st.columns([2, 3])
    with left:
        st.markdown("**Getting there**")
        for e in steps_done:
            icon = BADGE.get(e.get("status"), ("•", "", ""))[0]
            st.markdown(f"{icon} {e.get('index')}. {e.get('text', '')}")
            if e.get("note"):
                st.caption(e["note"][:160])

        if planned:
            st.markdown("**Screens**")
            done_by_name = {e.get("screen"): e for e in screens_done}
            for nm in planned:
                e = done_by_name.get(nm)
                if e:
                    st.markdown(f"{BADGE.get(e.get('status'), ('•',))[0]} {nm}")
                elif cur_screen and cur_screen.get("screen") == nm:
                    st.markdown(f"⏳ {nm}")
                else:
                    st.markdown(f"◻️ {nm}")
            # Screens found while running — a grid row's detail view and the tabs
            # inside it. They are not in the plan because their existence depends
            # on the record, but they are where most of the checking happens on
            # the grid screens, so hiding them would make the run look stalled.
            extra = [e.get("screen") for e in screens_done
                     if e.get("screen") not in planned]
            if extra:
                st.caption("Opened from a record")
                for e in screens_done:
                    nm = e.get("screen")
                    if nm in planned:
                        continue
                    st.markdown(f"{BADGE.get(e.get('status'), ('•',))[0]} {nm}")
        for line in job.logs[-3:]:
            st.caption(line)
    with right:
        shot = job.latest_shot
        if shot:
            st.markdown("**What the automation is looking at**")
            st.image(shot, width='stretch')
        else:
            st.info("Starting a browser and signing in …")

    if st.button("Stop this run"):
        job.stop()
        st.rerun()


def render_create_progress(job: jobs.Job) -> None:
    """
    Live view of a run that writes.

    Shows the step list rather than a screen list: a fill flow is a sequence of
    actions on a form, and which step it reached is the useful thing to see when
    something stalls — that is the field it is stuck on.
    """
    if job.target_key == "case.screens":
        st.subheader("Running: fill and check the case's screens")
        st.caption(f"Case {job.case_id}")
    else:
        st.subheader("Running: create an obligor and raise a credit application")
    steps = job.steps
    st.caption(f"{len(steps)} step(s) done")

    left, right = st.columns([2, 3])
    with left:
        st.markdown("**What it has done**")
        for e in steps:
            icon = BADGE.get(e.get("status"), ("•", "", ""))[0]
            st.markdown(f"{icon} {e.get('index')}. {e.get('text', '')}")
            if e.get("note"):
                st.caption(str(e["note"])[:120])
        for line in job.logs[-4:]:
            st.caption(line)
    with right:
        shot = job.latest_shot
        if shot:
            st.markdown("**What the automation is looking at**")
            st.image(shot, width="stretch")
        else:
            st.info("Starting a browser and signing in …")

    if st.button("Stop this run"):
        job.stop()
        st.rerun()


def render_created(result: dict) -> None:
    """What a run that WRITES produced — the first thing to look for."""
    key = result.get("target_key")
    if key not in ("obligor.create", "case.screens"):
        return
    if result.get("dry_run"):
        st.info("Dry run — every field was filled and then abandoned. Nothing "
                "was written.", icon="🧪")

    if key == "obligor.create":
        a, b, c = st.columns(3)
        a.metric("Obligor", result.get("obligor_name") or "—")
        b.metric("Customer ID", result.get("customer_id") or "—")
        c.metric("Request ID", result.get("request_id") or "—")
    else:
        a, b, c = st.columns(3)
        a.metric("Case", result.get("case_id") or "—")
        a.caption("Screens: " + (", ".join(result.get("screens") or []) or "—"))
        b.metric("Facility requested", result.get("facility_ref") or "—")
        c.metric("Run marker", result.get("marker") or "—")
        c.caption("Typed into one free-text field per screen, so the round "
                  "trip is checking THIS run's values and not a previous "
                  "run's identical ones.")

    entries = result.get("entries") or []
    if entries:
        with st.expander(f"What it entered ({len(entries)} fields)"):
            # The same rows the CSV and the PDF are built from — see
            # runner/exports.py. One description of what a result holds, so
            # the table on screen and the file you download cannot disagree.
            st.dataframe(pd.DataFrame(X.entry_rows(result)),
                         hide_index=True, width="stretch")
            st.caption("A rich-text box keeps its paragraphs; '¶' marks where "
                       "one ended, so a single cell still shows what was "
                       "written.")


def render_result(job: jobs.Job) -> None:
    crash = job.crashed()
    if crash:
        st.error("The run did not finish", icon="💥")
        st.code(crash)
        return

    result = job.result()
    if result is None:
        st.warning("The run finished but no report was found.")
        return

    target_title = result.get("target_title", job.target_key)
    overall = result.get("overall", R.BLOCKED)
    icon, colour, word = BADGE.get(overall, ("•", "", overall))

    st.subheader(f"{icon} {target_title} — {word}")
    if result.get("case_id"):
        st.caption(f"Record {result['case_id']}")
    render_created(result)
    counts = {R.PASS: 0, R.FAIL: 0, R.BLOCKED: 0}
    for c in result.get("checks", []):
        counts[c["status"]] = counts.get(c["status"], 0) + 1

    # COMMENTED OUT ON REQUEST — the 'Could not check' tile.
    #
    # Only the TILE is gone. Blocked checks are still produced, still counted,
    # still listed below with their own icon, and still exported — so nothing
    # is hidden from the report, only from this summary row.
    #
    # a, b, c = st.columns(3)
    # a.metric("Passed", counts[R.PASS])
    # b.metric("Failed", counts[R.FAIL])
    # c.metric("Could not check", counts[R.BLOCKED])
    a, b = st.columns(2)
    a.metric("Passed", counts[R.PASS])
    b.metric("Failed", counts[R.FAIL])

    if result.get("blocked_reason"):
        st.warning(result["blocked_reason"], icon="⏸️")

    # ---- navigation ----------------------------------------------------
    steps = result.get("steps", [])
    if steps:
        with st.expander("How it got there", expanded=counts[R.FAIL] > 0):
            for s in steps:
                ic = BADGE.get(s.get("status"), ("•",))[0]
                st.markdown(f"{ic} **{s.get('index')}. {s.get('kind')}** "
                            f"{s.get('label', '')}")
                if s.get("note"):
                    st.caption(s["note"])

    # ---- checks, grouped by screen, failures first ----------------------
    order = {R.FAIL: 0, R.BLOCKED: 1, R.PASS: 2}
    checks = sorted(result.get("checks", []),
                    key=lambda c: (order.get(c["status"], 9), c["name"]))

    by_screen: dict[str, list[dict]] = {}
    for c in checks:
        by_screen.setdefault(c.get("screen") or "(record)", []).append(c)

    if len(by_screen) > 1:
        st.markdown("### Screens checked")
        rows = []
        for screen, items in by_screen.items():
            cnt = {R.PASS: 0, R.FAIL: 0, R.BLOCKED: 0}
            for c in items:
                cnt[c["status"]] = cnt.get(c["status"], 0) + 1
            worst = R.FAIL if cnt[R.FAIL] else (R.PASS if cnt[R.PASS] else R.BLOCKED)
            # 'Could not check' column commented out on request, to match the
            # summary tiles above. `worst` still takes BLOCKED into account,
            # so the row's icon is unchanged.
            rows.append({"": BADGE.get(worst, ("•",))[0], "Screen": screen,
                         "Passed": cnt[R.PASS], "Failed": cnt[R.FAIL]})
            #             "Could not check": cnt[R.BLOCKED]})
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")

    st.markdown("### What was checked")
    for screen, items in by_screen.items():
        failed_here = sum(1 for c in items if c["status"] == R.FAIL)
        head = BADGE.get(R.FAIL if failed_here else R.PASS, ("•",))[0]
        with st.expander(f"{head} {screen} — {len(items)} checks, "
                         f"{failed_here} failed", expanded=failed_here > 0):
            for chk in items:
                ic, _, word = BADGE.get(chk["status"], ("•", "", chk["status"]))
                st.markdown(f"**{ic} {chk['name']}**")
                if chk.get("expected"):
                    st.markdown(f"- Should be: {chk['expected']}")
                if chk.get("actual"):
                    st.markdown(f"- Actually: {chk['actual']}")
                if chk.get("detail"):
                    st.caption(chk["detail"])
                for path in chk.get("evidence", []):
                    if path and os.path.exists(path):
                        st.image(path, width="stretch")
                st.divider()

    # ---- exports -------------------------------------------------------
    #
    # Every file here is built from runner/exports.py, which is also what fills
    # the tables above. The CSVs are the two halves of a run — what was
    # checked, and what was entered — and the PDF is the whole report in one
    # document, for sending to somebody who will not be opening this page.
    render_downloads(result, bool(checks))
    st.caption(f"Evidence saved in {result.get('artifacts_dir', '')}")


def render_downloads(result: dict, has_checks: bool) -> None:
    st.markdown("### Download this report")
    name = X.base_name(result)
    entries = result.get("entries") or []

    left, middle, right = st.columns(3)
    with left:
        st.download_button(
            "CSV Download", X.checks_csv(result) if has_checks else "",
            file_name=f"{name}.csv", mime="text/csv", width="stretch",
            disabled=not has_checks,
            help="What was checked: every check, its result, what it expected "
                 "and what the application actually showed.")
    with middle:
        st.download_button(
            "CSV Download (entered values)",
            X.entries_csv(result) if entries else "",
            file_name=f"{name}-entered-values.csv", mime="text/csv",
            width="stretch", disabled=not entries,
            help="What was entered: every field this run filled, with its "
                 "value. On a Litigation run this is the record itself — type "
                 "of suit through to date of decree.")
    with right:
        render_pdf_button(result, name)

    if not entries:
        st.caption("This run entered nothing, so there are no entered values "
                   "to export — a read-only check never types anything.")


def render_pdf_button(result: dict, name: str) -> None:
    """
    The PDF, or a plain explanation of why there isn't one.

    Wrapped because a report is not worth a broken results page: if reportlab
    is missing or the document will not build, the run's own findings still
    have to be readable, and the CSVs beside this button still work.
    """
    if not X.pdf_available():
        st.button("PDF Download", disabled=True, width="stretch",
                  help=X.PDF_HINT)
        st.caption(X.PDF_HINT)
        return
    try:
        pdf = X.build_pdf(result)
    except Exception as exc:  # noqa: BLE001 - never break the results page
        st.button("PDF Download", disabled=True, width="stretch")
        st.caption(f"The PDF could not be built for this run: "
                   f"{str(exc)[:200]}. The CSVs beside this are unaffected.")
        return
    st.download_button(
        "PDF Download", pdf, file_name=f"{name}.pdf",
        mime="application/pdf", width="stretch",
        help="The whole report as one document: what it ran against, what it "
             "entered — rich text included — how it got there, and every "
             "check with its result.")


# --------------------------------------------------------------------------

def main() -> None:
    st.title("🧪 Loan Origination System — Automation Portal")
    st.caption("Pick a screen. The automation opens it in a browser, checks it "
               "works, and reports what it found. Checks never change "
               "anything; entering data is a separate section, and says so.")

    render_sidebar()

    job = st.session_state.get("job")
    if job is not None:
        if job.running:
            render_progress(job)
            time.sleep(REFRESH_SECONDS)
            st.rerun()
        else:
            job.poll()
            render_result(job)
            if st.button("← Check something else"):
                st.session_state["job"] = None
                st.rerun()
            st.divider()

    if job is None or not job.running:
        render_menu()
        render_case_screens()
        # COMMENTED OUT ON REQUEST — the whole Create test data menu.
        # render_create and start_create are commented out with it; the flow
        # behind them (flows.create_obligor) and its CLI entry point
        # (--create-obligor) are untouched.
        # st.divider()
        # render_create()


if __name__ == "__main__":
    main()
