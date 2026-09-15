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
  - The page holds a LIST of runs, not one. "Occurances" in the sidebar creates
    several obligors, ONE AFTER THE OTHER: each is its own process with its own
    artifacts directory, and the next is launched from main() when the previous
    one ends — off the same refresh that drives the live view. Never two at
    once, because two browsers driving this application starves both. A list of
    one renders through the same single-run views as before, so the ordinary
    case is unchanged.
  - Nothing here can write to the application. Phase 1 is read-only.
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
from los_automation.runner import jobs
from los_automation.runner import results as R
from los_automation.runner import targets as tg

st.set_page_config(page_title="LOS Automation Portal", page_icon="🧪",
                   layout="wide")

REFRESH_SECONDS = 2

# The width of an input or an action button, as a column split of the content
# area. One constant rather than a number per section: the two write sections
# ask the same thing of the operator and their controls line up on the same
# left edge because they share this. Left at a fraction of the page on purpose
# — a full-width primary button on a layout="wide" page is a stripe, not a
# button.
FORM_COLUMNS = [2, 3]

BADGE = {
    R.PASS: ("✅", "#1a7f37", "Passed"),
    R.FAIL: ("❌", "#b42318", "Failed"),
    R.ERROR: ("⚠️", "#b54708", "Could not run"),
}


# --------------------------------------------------------------------------
# The runs in flight
#
# A LIST, because "Occurances" in the sidebar can start several creates at
# once. Every other button still starts exactly one, and a list of one renders
# through the same single-run views it always did — so the ordinary case looks
# and behaves as it did before batches existed.
# --------------------------------------------------------------------------

def _jobs() -> list[jobs.Job]:
    return st.session_state.get("jobs") or []


def _plan() -> dict:
    """The occurrences of the current batch still to be launched."""
    return st.session_state.get("batch") or {}


def _pending() -> int:
    """How many occurrences have not been started yet."""
    plan = _plan()
    if not plan:
        return 0
    return max(0, int(plan.get("total", 0)) - int(plan.get("started", 0)))


def _busy() -> bool:
    """
    Is anything still running, or still to come?

    A batch counts as busy between occurrences too. Nothing has a browser
    open at that moment, but the next obligor is about to start and letting a
    button launch something else into the gap would put two runs on the
    machine at once — which is the thing running them one at a time exists to
    avoid.
    """
    return any(j.running for j in _jobs()) or _pending() > 0


def _start(launched, target_key: str) -> None:
    """Take over the page with one job."""
    st.session_state["batch"] = {}
    st.session_state["jobs"] = ([launched] if isinstance(launched, jobs.Job)
                                else list(launched))
    st.session_state["last_target"] = target_key
    st.rerun()


def _launch_next_occurrence() -> bool:
    """
    Start the next obligor of a batch, if the last one has finished.

    This is what makes a batch sequential, and it runs off the page's own
    refresh: the live view already re-runs this script every couple of
    seconds while a job is going, so the moment the current process ends the
    next occurrence is launched from here. One browser at a time, start to
    finish.

    The cost of driving it from the page is that the page has to stay open
    between occurrences — the run in flight is a detached process and
    survives regardless, but nothing would start the one after it. The
    section says so where it can be read before starting.
    """
    plan = _plan()
    if not plan or _pending() <= 0:
        return False
    if any(j.running for j in _jobs()):
        return False

    index = int(plan["started"]) + 1
    job = jobs.start_create_occurrence(
        index=index, total=int(plan["total"]), batch_stamp=plan["stamp"],
        dry_run=bool(plan["dry_run"]), obligor_name=plan.get("name", ""),
        headed=bool(plan.get("headed", False)),
        stop_after_save=bool(plan.get("stop_after_save", False)))
    plan["started"] = index
    st.session_state["batch"] = plan
    st.session_state["jobs"] = _jobs() + [job]
    return True


def _abandon_batch() -> None:
    """Stop what is running and launch nothing further."""
    for job in _jobs():
        job.stop()
    plan = _plan()
    if plan:
        # Nothing left to come: `started` is what _pending measures against,
        # so marking the batch complete is what stops the next launch.
        plan["started"] = plan.get("total", 0)
        st.session_state["batch"] = plan


# --------------------------------------------------------------------------
# Sidebar: environment
# --------------------------------------------------------------------------

def render_sidebar() -> bool:
    st.sidebar.title("Environment")

    st.sidebar.markdown("**Client:** NBP")
    st.sidebar.markdown(f"**Environment:** `{crawler_config.BASE_URL}`")

    


    st.sidebar.divider()
    st.sidebar.subheader("Record under test")
    case_id = st.sidebar.text_input(
        "Case / obligor ID", value=st.session_state.get("case_id", settings.CASE_ID),
        help="Every check runs against this one record, so results are "
             "comparable between runs. The automation pages through the grid "
             "until it finds it.")
    st.session_state["case_id"] = case_id.strip()
    if not case_id.strip():
        st.sidebar.error("Enter a record id before running a check.")
    else:
        # The two grids hold different id formats; say which route fits so the
        # operator does not have to know.
        suggested = tg.suggested_target(case_id.strip())
        if suggested:
            st.sidebar.success(
                f"Looks like a {tg.get(suggested).id_description[:-1]} — use "
                f"**{tg.get(suggested).title}**.", icon="🧭")
        # else:
        #     st.sidebar.warning(
        #         "That id does not match either grid's usual format "
        #         "(case IDs look like 52224-2026, customer IDs like "
        #         "CIBG-186283-2026).", icon="🧭")

    # st.sidebar.divider()
    # st.sidebar.subheader("What we compare against")
    # st.sidebar.caption(
    #     "Only what this tool entered itself. A **Check** confirms a screen "
    #     "opens, renders, and reports no error; the **Fill and check** buttons "
    #     "type a value, save it, re-open the record and compare what came back "
    #     "against what went in. Both halves of that comparison come from the "
    #     "same run, which is what makes it trustworthy.")

    # st.sidebar.divider()
    st.sidebar.subheader("Occurances")
    occurrences = int(st.sidebar.number_input(
        "How many obligors to create", min_value=1, max_value=10,
        value=int(st.session_state.get("occurrences", 1)), step=1,
        help="Applies to **Create test data** only. 2 means two brand-new "
             "obligors are created from scratch ONE AFTER THE OTHER: the "
             "first runs to the end and its browser closes, then the second "
             "starts. At the end you get one combined report covering all of "
             "them. Every other button on this page runs once, as before."))
    st.session_state["occurrences"] = occurrences
    if occurrences > 1:
        st.sidebar.caption(
            f"**Create test data** will create {occurrences} obligors one "
            f"after the other — never two at once — and finish with a single "
            f"combined report.")
        st.sidebar.caption(
            f"A full create takes a few minutes, so allow roughly "
            f"{occurrences}× that, and leave this page open: the next obligor "
            f"is started when the previous one finishes.")

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
    # st.subheader("Choose what to check")
    # st.caption(f"All checks run against record **{case_id or '(not set)'}** — "
    #            f"change it in the sidebar.")
    busy = _busy() or not case_id
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
    _start(jobs.start_verify(target_key,
                             headed=st.session_state.get("headed", False),
                             case_id=st.session_state.get("case_id")),
           target_key)


# --------------------------------------------------------------------------
# Phase 2: creating test data
#
# Deliberately a separate section with its own heading and its own warning.
# Everything above this point only reads; this writes. Blurring the two in one
# list of buttons is how somebody eventually creates a record by accident.
# --------------------------------------------------------------------------

def render_create() -> None:
    st.subheader("Create New Obligor and Raise a Credit Application")
    allowed, reason = settings.write_allowed(crawler_config.BASE_URL)

    if not allowed:
        st.info(f"Data entry is blocked on this environment, so this section "
                f"cannot run.\n\n{reason}", icon="🔒")
        return

    # st.caption(
    #     "Creates a **new obligor** and fills **Basic Information only** — "
    #     "every enterable field on it, not just the thirteen mandatory ones, "
    #     "and **Client Number/CIF deliberately left empty**. It saves, raises a "
    #     "**Borrower Credit Application**, finds the resulting case in My "
    #     "Bucket, opens Obligor Details (BIR) and checks every value it entered "
    #     "actually carried through.")
    # st.caption("The nine tabs below Basic Information are the next section — "
    #            "run this first, then point **Finish Obligor Details (BIR)** at "
    #            "the case id this leaves behind.")

    # How many at once, set in the sidebar. Named there and read here so there
    # is one place to change it and no second copy to disagree.
    n = int(st.session_state.get("occurrences", 1))
    if n > 1:
        st.info(
            f"**Occurances: {n}** — this section will create {n} obligors "
            f"from scratch, **one after the other**. Each run finishes and "
            f"its browser closes before the next one starts, so only one is "
            f"ever open. At the end you get a single combined report covering "
            f"all {n}. Change it in the sidebar.", icon="🔁")
        st.caption("Leave this page open until it finishes: the run in "
                   "flight is its own process and will complete regardless, "
                   "but the next obligor is started from here when the "
                   "previous one ends.")

    # st.warning(
    #     f"This writes to {crawler_config.BASE_URL}. A live run leaves "
    #     + (f"{n} real obligors and {n} real credit cases"
    #        if n > 1 else "a real obligor and a real credit case")
    #     + f" behind — they cannot be removed from here. Use a dry run first: "
    #       f"it fills every field and reports what the form holds without "
    #       f"clicking Save.", icon="✍️")

    busy = _busy()

    # The input and the button share one left column of the same width, so
    # they line up with each other and with the text above them. They were in
    # two halves of a split row before, which left the remaining control
    # stranded in the right half once its neighbour went.
    field, _ = st.columns(FORM_COLUMNS)
    with field:
        name = st.text_input(
            "Obligor name", value=st.session_state.get("new_obligor_name", ""),
            placeholder="leave blank for AUTOMATION TEST <timestamp>",
            help="Blank gives a timestamped name, which keeps runs unique — the "
                 "app de-duplicates, and the verification step has to find this "
                 "exact record in My Bucket afterwards.")
    st.session_state["new_obligor_name"] = name.strip()
    if n > 1:
        # Said plainly, because the operator typed one name and is about to
        # get several records. Identical names would be worse than a numbered
        # set: the app de-duplicates on the name and each run's verification
        # looks for "this exact record".
        st.caption(
            f"Each of the {n} runs numbers this name — "
            + ", ".join(f"`{jobs.batch_obligor_name(name.strip(), 'mmdd-hhmmss', i)}`"
                        for i in (1, 2))
            + f"{', …' if n > 2 else ''} — so no two obligors share one "
              f"and each run can find its own afterwards.")

    confirm = st.checkbox(
        f"I understand this creates {n} real records" if n > 1
        else "I understand this creates real records", key="confirm_write")
    action, _ = st.columns(FORM_COLUMNS)
    with action:
        if st.button(f"Create {n} for real" if n > 1 else "Create for real",
                     type="primary", disabled=busy or not confirm,
                     width="stretch"):
            # stop_after_save is not offered on the page any more. False is
            # what it always defaulted to — the obligor is created AND its
            # transaction raised, which is what leaves a case id behind for
            # the section below to work on. The CLI still has
            # --stop-after-save for the other behaviour.
            start_create(dry_run=False, name=name.strip(),
                         stop_after_save=False, occurrences=n)


def start_create(dry_run: bool, name: str, stop_after_save: bool,
                 occurrences: int = 1) -> None:
    """
    Begin a batch: record the plan, then launch its first occurrence.

    Only the first. The rest are launched by _launch_next_occurrence as each
    process ends, so exactly one browser is ever open.
    """
    total = max(1, int(occurrences or 1))
    st.session_state["batch"] = {
        "total": total,
        "started": 0,
        "stamp": jobs.new_batch_stamp(),
        "dry_run": dry_run,
        "name": name,
        "stop_after_save": stop_after_save,
        "headed": st.session_state.get("headed", False),
    }
    st.session_state["jobs"] = []
    st.session_state["last_target"] = "obligor.create"
    _launch_next_occurrence()
    st.rerun()


# --------------------------------------------------------------------------
# Phase 2c: finishing the obligor inside a case that already exists
#
# The other half of "Create test data", which now stops at Basic Information.
# Separate section, separate button, and it takes a case id — because the two
# are done at two different moments and the second one is the long run.
# --------------------------------------------------------------------------

def render_obligor_details() -> None:
    st.subheader("Finish Obligor Details (BIR) on a case")
    allowed, reason = settings.write_allowed(crawler_config.BASE_URL)
    case_id = st.session_state.get("case_id", settings.CASE_ID)

    if not allowed:
        st.info(f"Data entry is blocked on this environment, so this section "
                f"cannot run.\n\n{reason}", icon="🔒")
        return

    # st.caption(
    #     f"Takes the case **{case_id or '(not set)'}** — which must already be "
    #     f"in My Bucket — opens **Obligor Details (BIR)** inside it, and fills "
    #     f"**every tab below Basic Information**: Sector And Industry, "
    #     f"Management & Shareholders, Additional Information, BBFS Details, "
    #     f"Contact and Address, Limits and Corporate Governance, including "
    #     f"every one of their eighteen tables. Then it leaves, comes back in "
    #     f"through My Bucket, and checks each value came back.")
    # st.caption("Roughly 250 fields across 25 forms, so a full run takes a "
    #            "while — the live view shows which field it is on. BBFS Details "
    #            "alone is nineteen free-text editors, each given text that "
    #            "names the box it went into so the round trip can tell one from "
    #            "another.")
    # st.warning(
    #     f"This WRITES to case {case_id or '(not set)'} on "
    #     f"{crawler_config.BASE_URL}. What it saves cannot be removed from "
    #     f"here. A dry run fills every field and reports what each form holds "
    #     f"without saving — use that first.", icon="✍️")

    busy = _busy() or not case_id

    # Same shape as Create test data above: the confirmation on its own line,
    # the button under it in a column of the standard width. Two sections that
    # ask the same question of the operator should not look different.
    confirm = st.checkbox("I understand this writes to the case",
                          key="confirm_details_write")
    action, _ = st.columns(FORM_COLUMNS)
    with action:
        if st.button("Fill and verify for real", key="details-live",
                     type="primary", disabled=busy or not confirm,
                     width="stretch"):
            start_obligor_details(dry_run=False)


def start_obligor_details(dry_run: bool) -> None:
    _start(jobs.start_obligor_details(
        case_id=st.session_state.get("case_id", settings.CASE_ID),
        dry_run=dry_run, headed=st.session_state.get("headed", False)),
        "case.obligor_details")


# --------------------------------------------------------------------------
# Phase 2b: filling the case's own screens
#
# A separate button per screen, because they are separate jobs with separate
# costs. Facilities requests a facility and then walks every tab of it, which
# takes minutes and leaves a facility on the case; someone checking Request
# Details should be able to do that alone.
# --------------------------------------------------------------------------

CASE_SCREENS = [
    ("request_details", "Request Details",
     "Fills the request form — request type, the proposed expiry and the rest "
     "of section 7.2 — and adds a row to its Purpose of Request table."),
    ("facilities", "Facilities",
     "Selects the requested facility and presses Proceed, then fills every tab "
     "the facility opens with: request details, facility details, limits and "
     "exposures, overdue, pricing, payment and utilisation."),
    ("observations", "Observations",
     "Records an observation against the case — all twelve fields on the Add "
     "Observation panel, then Save."),
    ("collaterals", "Collaterals",
     "Asks for a collateral — picks a classification and a name in the "
     "Obligor Collateral dialog and presses Proceed — then fills every tab it "
     "opens with: basic information, collateral policy, shares collateral and "
     "stocks hypothecation, saving each."),
    ("coverage", "Facility Coverage",
     "Associates a collateral with a facility from BOTH sides of the screen: "
     "clicks the '+' on the facility tree, fills the Collateral Association "
     "dialog — collateral, priority and coverage % — and saves, then does the "
     "same from the collateral tree. Needs a facility and a collateral on the "
     "case already, so run those two first."),
    ("risk_rating", "Risk Rating",
     "Scores the case's rating model: presses Perform Risk Rating, puts the "
     "model into Edit, answers the Basic Information LOVs, presses Generate "
     "Score, then checks the Calculation Sheet for anything the model flagged "
     "red and saves. Finally re-opens Risk Rating and checks the rating it "
     "produced is the one the Rating Summary and Rating History now show."),
    ("policies", "Policies & Exceptions",
     "Raises a policy exception — opens Add Exception and fills every field on "
     "the form it shows: title, description, compliance status, request type "
     "and the rationale for the deviation — then saves and checks the "
     "exception reached the case's list of exceptions."),
    ("conditions", "Conditions",
     "Raises a condition against the case — description, title, category, "
     "type, effective and expiry dates and everything else the Add Condition "
     "panel shows — attaches a generated test image to it through the panel's "
     "paperclip, then saves and checks the condition reached the register."),
    ("documents", "Documents",
     "Downloads whatever the case already has attached, adds a document of its "
     "own with a generated test image, then opens one of the checklist "
     "documents and actions it — attaching a file, editing its fields and "
     "saving."),
]


def render_case_screens() -> None:
    st.subheader("Fill and check a case's screens")
    allowed, reason = settings.write_allowed(crawler_config.BASE_URL)
    case_id = st.session_state.get("case_id", settings.CASE_ID)

    if not allowed:
        st.info(f"Data entry is blocked on this environment, so this section "
                f"cannot run.\n\n{reason}", icon="🔒")
        return

    # st.caption(
    #     f"Takes the case **{case_id or '(not set)'}** — which must already "
    #     f"exist in My Bucket — opens one of its screens, fills **every field "
    #     f"on it**, saves, and then re-opens the case from My Bucket and checks "
    #     f"each value came back. Coming back in through the grid is what makes "
    #     f"it a real check: a value the app dropped or reformatted is invisible "
    #     f"on the form that is still holding it.")
    # st.warning(
    #     f"These buttons WRITE to case {case_id or '(not set)'} on "
    #     f"{crawler_config.BASE_URL}. What they save cannot be removed from "
    #     f"here. A dry run fills every field and reports what each form holds "
    #     f"without saving — use that first.", icon="✍️")

    busy = _busy() or not case_id

    mode = st.radio(
        "How should these run?",
        [
         "Fill, save, and verify by round trip"],
        index=0, horizontal=False, key="case_fill_mode",
        help="A dry run cannot reach the facility tabs: they do not exist "
             "until the requested facility has been submitted with Proceed, "
             "which a dry run deliberately does not press.")
    dry_run = mode.startswith("Dry run")
    confirm = True
    if not dry_run:
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
    if st.button(f"Check all {len(CASE_SCREENS)}, in order",
                 disabled=busy or not confirm, type="primary",
                 width="stretch"):
        start_case_fill("all", dry_run)
    st.caption("All of them run one after the other and are verified together "
               "in a single round trip. Slower than the individual buttons, "
               "and one long report rather than several short ones.")


def start_case_fill(screen: str, dry_run: bool) -> None:
    _start(jobs.start_case_fill(
        screen=screen, case_id=st.session_state.get("case_id",
                                                    settings.CASE_ID),
        dry_run=dry_run, headed=st.session_state.get("headed", False)),
        "case.screens")


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

    # Keyed by run id: several of these views are on the page at once when a
    # batch is running, and two buttons Streamlit cannot tell apart is an
    # error rather than two buttons.
    if st.button("Stop this run", key=f"stop-{job.run_id}"):
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
    elif job.target_key == "case.obligor_details":
        st.subheader("Running: fill Obligor Details (BIR) on the case")
        st.caption(f"Case {job.case_id}")
    else:
        st.subheader("Running: create an obligor and raise a credit application")
    steps = job.steps
    st.caption(f"{len(steps)} step(s) done")

    # A tab-by-tab run emits a screen list; showing how far through it is turns
    # "twenty minutes of scrolling steps" into something with an end in sight.
    planned = job.planned_screens
    if planned:
        done = {e.get("screen") for e in job.screens}
        st.progress(min(len(done) / len(planned), 1.0))
        cur = job.current_screen
        st.caption(f"Tab {len(done)} of {len(planned)} done"
                   + (f" — now on {cur.get('screen', '')}"
                      if cur and job.running else ""))

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

    # Keyed by run id: several of these views are on the page at once when a
    # batch is running, and two buttons Streamlit cannot tell apart is an
    # error rather than two buttons.
    if st.button("Stop this run", key=f"stop-{job.run_id}"):
        job.stop()
        st.rerun()


def render_created(result: dict) -> None:
    """What a run that WRITES produced — the first thing to look for."""
    key = result.get("target_key")
    if key not in ("obligor.create", "case.screens", "case.obligor_details"):
        return
    if result.get("dry_run"):
        st.info("Dry run — every field was filled and then abandoned. Nothing "
                "was written.", icon="🧪")

    if key == "obligor.create":
        a, b, c = st.columns(3)
        a.metric("Obligor", result.get("obligor_name") or "—")
        b.metric("Customer ID", result.get("customer_id") or "—")
        c.metric("Request ID", result.get("request_id") or "—")
        if result.get("request_id"):
            st.info(f"Basic Information is done. To fill the rest of the "
                    f"obligor, put **{result['request_id']}** in the sidebar "
                    f"and run **Finish Obligor Details (BIR) on a case**.",
                    icon="➡️")
    elif key == "case.obligor_details":
        entries = result.get("entries") or []
        tabs = {e.get("screen") for e in entries if e.get("screen")}
        a, b, c = st.columns(3)
        a.metric("Case", result.get("case_id") or "—")
        b.metric("Tabs filled", len(tabs) or "—")
        c.metric("Fields entered", len(entries) or "—")
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
            st.dataframe(
                pd.DataFrame([{"Screen": e.get("group") or e.get("screen", ""),
                               "Field": e["label"], "Value": e["value"],
                               "Kind": e["kind"]} for e in entries]),
                hide_index=True, width="stretch")


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
    overall = result.get("overall", R.ERROR)
    icon, colour, word = BADGE.get(overall, ("•", "", overall))

    st.subheader(f"{icon} {target_title} — {word}")
    if result.get("case_id"):
        st.caption(f"Record {result['case_id']}")
    render_created(result)
    counts = {R.PASS: 0, R.FAIL: 0}
    for c in result.get("checks", []):
        counts[c["status"]] = counts.get(c["status"], 0) + 1
    notes = result.get("notes") or []

    a, b = st.columns(2)
    a.metric("Passed", counts[R.PASS])
    b.metric("Failed", counts[R.FAIL])

    if result.get("error_reason"):
        st.warning(result["error_reason"], icon="⚠️")

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
    order = {R.FAIL: 0, R.PASS: 1}
    checks = sorted(result.get("checks", []),
                    key=lambda c: (order.get(c["status"], 9), c["name"]))

    by_screen: dict[str, list[dict]] = {}
    for c in checks:
        by_screen.setdefault(c.get("screen") or "(record)", []).append(c)

    if len(by_screen) > 1:
        st.markdown("### Screens checked")
        rows = []
        for screen, items in by_screen.items():
            cnt = {R.PASS: 0, R.FAIL: 0}
            for c in items:
                cnt[c["status"]] = cnt.get(c["status"], 0) + 1
            worst = R.FAIL if cnt[R.FAIL] else R.PASS
            rows.append({"": BADGE.get(worst, ("•",))[0], "Screen": screen,
                         "Passed": cnt[R.PASS], "Failed": cnt[R.FAIL]})
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

    # ---- observations ----------------------------------------------------
    #
    # Deliberately not checks and deliberately not counted. These are the
    # things a run could not make an assertion about — a field the screen does
    # not have under the name this suite knows it by, a pass left blank on
    # purpose, a dry run. They were a third "could not check" result until it
    # became possible for a report to be entirely amber and still mean nothing
    # was wrong.
    if notes:
        st.markdown("### Observations")
        st.caption("Things worth knowing that are not verdicts on the "
                   "application. None of these counts as a pass or a failure.")
        for n in notes:
            where = f"**{n.get('screen')}** — " if n.get("screen") else ""
            st.markdown(f"- {where}{n.get('text', '')}")
            for path in n.get("evidence", []):
                if path and os.path.exists(path):
                    st.image(path, width="stretch")

    # ---- exports -------------------------------------------------------
    st.markdown("### Take this away")
    render_downloads(job, result, checks)
    st.caption(f"Evidence saved in {result.get('artifacts_dir', '')}")


def render_downloads(job: jobs.Job, result: dict, checks: list[dict]) -> None:
    """
    The run as files: the whole report as a PDF, and the checks as a
    spreadsheet.

    The PDF is the one people actually asked for — everything on this page in
    the order it happened, screenshots included, in a single file that can be
    attached to a defect or sent to somebody who will never open this portal.
    The runner draws it as it finishes, so it is normally waiting here already;
    the button below is for runs that predate that, and for the occasional one
    where the browser could not print.
    """
    run_id = result.get("run_id", "run")
    pdf, html_report = job.report_pdf(), job.report_html()
    left, right = st.columns(2)

    with left:
        if pdf:
            with open(pdf, "rb") as fh:
                st.download_button(
                    "📄 Download the full report (PDF)", fh.read(),
                    file_name=f"{run_id}.pdf", mime="application/pdf",
                    type="primary", width="stretch")
            st.caption("Every screenshot and every observation on this page, "
                       "in the order the run made them.")
        else:
            if st.button("📄 Build the PDF report", width="stretch",
                         key=f"mkpdf-{run_id}"):
                with st.spinner("Rendering the report — this takes a few "
                                "seconds …"):
                    made = jobs.build_pdf(job.artifacts_dir)
                if made:
                    st.rerun()
                else:
                    st.warning(
                        "The PDF could not be rendered here — that needs the "
                        "Playwright browser (`playwright install chromium`). "
                        "The same report is available as HTML below.")
            if html_report:
                with open(html_report, "rb") as fh:
                    st.download_button(
                        "Download the full report (HTML)", fh.read(),
                        file_name=f"{run_id}.html", mime="text/html",
                        width="stretch")

    with right:
        rows = [{"Screen": c.get("screen", ""), "Check": c["name"],
                 "Result": c["status"], "Should be": c.get("expected", ""),
                 "Actually": c.get("actual", ""),
                 "Notes": c.get("detail", "")} for c in checks]
        if rows:
            st.download_button(
                "Download the checks (CSV)",
                pd.DataFrame(rows).to_csv(index=False),
                file_name=f"{run_id}.csv", mime="text/csv", width="stretch")
            st.caption("The results table only — for filtering in a "
                       "spreadsheet.")
        entries = result.get("entries") or []
        if entries:
            st.download_button(
                "Download what it entered (CSV)",
                pd.DataFrame([
                    {"Screen": e.get("group") or e.get("screen", ""),
                     "Field": e.get("label", ""), "Value": e.get("value", ""),
                     "Control": e.get("kind", "")} for e in entries
                ]).to_csv(index=False),
                file_name=f"{run_id}-entered.csv", mime="text/csv",
                width="stretch")


# --------------------------------------------------------------------------

def _batch_rows(batch: list[jobs.Job]) -> list[dict]:
    """One summary line per occurrence, from each run's own report."""
    rows = []
    for i, job in enumerate(batch, 1):
        result = job.result() or {}
        counts = {R.PASS: 0, R.FAIL: 0}
        for c in result.get("checks", []):
            counts[c["status"]] = counts.get(c["status"], 0) + 1
        finished = not job.running and bool(result)
        overall = result.get("overall", R.ERROR)
        rows.append({
            "": (BADGE.get(overall, ("•",))[0] if finished
                 else "⏳" if job.running else "—"),
            "Obligor #": i,
            "Obligor": (result.get("obligor_name") or job.case_id or "—"),
            "Customer ID": result.get("customer_id") or "—",
            "Request ID": result.get("request_id") or "—",
            "Passed": counts[R.PASS],
            "Failed": counts[R.FAIL],
            "Observations": len(result.get("notes") or []),
            "_overall": overall,
            "_finished": finished,
            "_job": job,
        })
    return rows


def _shown(rows: list[dict]) -> pd.DataFrame:
    """The summary without the columns that are for this code's own use."""
    return pd.DataFrame([{k: v for k, v in r.items()
                          if not k.startswith("_")} for r in rows])


def render_batch_progress(batch: list[jobs.Job]) -> None:
    """
    A batch in flight, one obligor at a time.

    The finished occurrences are summarised, and the one still going gets the
    SAME live view a single run gets. Reusing that view rather than writing a
    smaller one is the point: there is one place where "what the automation is
    looking at" is rendered, and it cannot drift from itself.
    """
    for job in batch:
        job.poll()
    plan = _plan()
    total = int(plan.get("total") or len(batch))
    done = [j for j in batch if not j.running]

    st.subheader(f"Creating {total} obligors, one after the other")
    st.caption(f"{len(done)} of {total} finished — obligor "
               f"{min(len(done) + 1, total)} is running now. Each run closes "
               f"its browser before the next one starts.")
    st.progress(len(done) / total)

    if len(done) > 1 or (done and any(j.running for j in batch)):
        st.markdown("**Finished so far**")
        st.dataframe(_shown(_batch_rows(done)), hide_index=True,
                     width="stretch")

    if st.button("Stop the batch", key="stop-all",
                 help="Stops the obligor running now and starts none of the "
                      "ones after it. Records already created stay created."):
        _abandon_batch()
        st.rerun()

    current = next((j for j in batch if j.running), None)
    if current is not None:
        st.divider()
        st.markdown(f"**Obligor {len(done) + 1} of {total}** — "
                    f"`{current.case_id or current.run_id}`")
        render_create_progress(current)
    else:
        st.info("Starting the next obligor …")


def render_batch_result(batch: list[jobs.Job]) -> None:
    """
    ONE report covering every obligor the batch created.

    Combined rather than N reports in a row, which is what makes it readable:
    the totals and the records created answer "did that work?" in a glance,
    the summary says which obligor to look at, and each run's own full report
    is one expander away rather than something to scroll past.
    """
    rows = _batch_rows(batch)
    st.subheader(f"Combined report — {len(batch)} obligors")

    totals = {R.PASS: 0, R.FAIL: 0}
    observed = 0
    for r in rows:
        totals[R.PASS] += r["Passed"]
        totals[R.FAIL] += r["Failed"]
        observed += r["Observations"]
    created = [r for r in rows if r["Customer ID"] != "—"]

    a, b, c, d = st.columns(4)
    a.metric("Obligors created", f"{len(created)} of {len(batch)}")
    b.metric("Passed", totals[R.PASS])
    c.metric("Failed", totals[R.FAIL])
    d.metric("Observations", observed)

    failed = [r["Obligor #"] for r in rows if r["_overall"] == R.FAIL]
    if failed:
        st.error(f"Obligor(s) {', '.join(str(x) for x in failed)} reported a "
                 f"failure — open them below.", icon="❌")
    crashed = [r["Obligor #"] for r in rows if r["_job"].crashed()]
    if crashed:
        st.warning(f"Obligor(s) {', '.join(str(x) for x in crashed)} did not "
                   f"finish at all.", icon="💥")
    if not failed and not crashed:
        st.success(f"All {len(batch)} runs finished with no failures.",
                   icon="✅")

    st.markdown("### The obligors this batch created")
    st.dataframe(_shown(rows), hide_index=True, width="stretch")
    st.caption("Each obligor is numbered so no two share a name — the app "
               "de-duplicates on it, and each run's verification has to find "
               "its own record in My Bucket afterwards.")

    # ---- the whole batch as one file ----------------------------------
    all_checks = []
    all_entries = []
    for r in rows:
        result = r["_job"].result() or {}
        for chk in result.get("checks", []):
            all_checks.append({
                "Obligor #": r["Obligor #"], "Obligor": r["Obligor"],
                "Screen": chk.get("screen", ""), "Check": chk["name"],
                "Result": chk["status"],
                "Should be": chk.get("expected", ""),
                "Actually": chk.get("actual", ""),
                "Notes": chk.get("detail", ""),
            })
        for ent in result.get("entries", []):
            all_entries.append({
                "Obligor #": r["Obligor #"], "Obligor": r["Obligor"],
                "Screen": ent.get("group") or ent.get("screen", ""),
                "Field": ent.get("label", ""), "Value": ent.get("value", ""),
                "Control": ent.get("kind", ""),
            })

    if all_checks:
        st.markdown("### Take the whole batch away")
        left, right = st.columns(2)
        with left:
            st.download_button(
                f"Download all {len(all_checks)} checks (CSV)",
                pd.DataFrame(all_checks).to_csv(index=False),
                file_name=f"batch-{len(batch)}-obligors-checks.csv",
                mime="text/csv", type="primary", width="stretch",
                key="batch-checks-csv")
            st.caption("Every check from every obligor in one sheet, with an "
                       "Obligor # column to filter on.")
        with right:
            if all_entries:
                st.download_button(
                    f"Download all {len(all_entries)} entered values (CSV)",
                    pd.DataFrame(all_entries).to_csv(index=False),
                    file_name=f"batch-{len(batch)}-obligors-entered.csv",
                    mime="text/csv", width="stretch", key="batch-entries-csv")
                st.caption("What each run typed, per obligor.")
        st.caption("Each obligor's own PDF is inside its section below.")

    # ---- each obligor's full report, unchanged -------------------------
    st.markdown("### Each obligor in full")
    for r in rows:
        job = r["_job"]
        result = job.result() or {}
        head = (f"{r['']} Obligor {r['Obligor #']} — {r['Obligor']} — "
                f"{result.get('headline', 'no report')}")
        with st.expander(head, expanded=bool(failed and
                                             r["_overall"] == R.FAIL)):
            render_result(job)


def main() -> None:
    st.title("🧪 Loan Origination System — Automation Portal")
    st.caption("Pick a screen. The automation opens it in a browser, checks it "
               "works, and reports what it found. Checks never change "
               "anything; entering data is a separate section, and says so.")

    render_sidebar()

    # A batch is sequential, and this is what makes it so: the moment the
    # occurrence in flight ends, the next one is launched from here, off the
    # page's own refresh. Done before anything is rendered so the live view
    # shows the new run immediately rather than a gap.
    for job in _jobs():
        job.poll()
    if _launch_next_occurrence():
        st.rerun()

    batch = _jobs()
    running = _busy()
    plan = _plan()
    # Whether this is a batch is the PLAN's question, not the list's: between
    # occurrences the list holds one finished job and the combined report is
    # still what should come at the end.
    is_batch = int(plan.get("total") or len(batch)) > 1

    if batch:
        # One run renders exactly as it always did. The batch views are only
        # reached when there is genuinely more than one obligor to show.
        if running:
            if is_batch:
                render_batch_progress(batch)
            else:
                render_progress(batch[0])
            time.sleep(REFRESH_SECONDS)
            st.rerun()
        else:
            if is_batch:
                render_batch_result(batch)
            else:
                render_result(batch[0])
            if st.button("← Check something else"):
                st.session_state["jobs"] = []
                st.session_state["batch"] = {}
                st.rerun()
            st.divider()

    if not running:
        # In the order the work is actually done: create the obligor, finish
        # its details on the case that creates, then fill the case's own
        # screens. Only the ORDER of these calls changed — each section is
        # the same function doing the same thing, and Streamlit keys widget
        # state by key rather than by position, so the boxes and choices
        # inside them are unaffected by where they sit.
        render_create()
        st.divider()
        render_obligor_details()
        st.divider()
        render_case_screens()
        # The read-only check menu last. It is above nothing now, which is
        # what "Create test data on the top" asks for, and on this deployment
        # it renders only its heading — every Phase 1 target in targets.py is
        # commented out.
        st.divider()
        render_menu()


if __name__ == "__main__":
    main()
