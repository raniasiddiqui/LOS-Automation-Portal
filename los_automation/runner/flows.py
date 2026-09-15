"""
Phase 2 flows: authored sequences that create data and then verify it.

TWO flows, deliberately split, because they are two jobs a person does at two
different moments.

CREATE (`create_obligor`) — make the record and prove it exists:

    All Obligors -> Create Obligor
      -> fill Basic Information ONLY, leaving Client Number/CIF empty, Save
                                                 (the obligor now exists)
      -> Raise Transaction -> Borrower Credit Application -> Proceed
                                                 (a credit case now exists)
      -> My Bucket -> find that case -> open it
      -> Obligor Details (BIR) -> compare every value against what was typed

    It stops at Basic Information on purpose. The nine tabs that unlock after
    Save belong to the second flow, so creating a record stays a short run that
    either worked or did not, instead of a twenty-minute one in which the
    interesting failure is buried behind eighteen grids.

DETAIL (`fill_case_obligor_details`) — take a case that already exists and
finish its obligor:

    My Bucket -> find the case by id -> open it
      -> Obligor Details (BIR)
      -> fill every remaining tab and every one of their eighteen tables
      -> come back in through My Bucket and compare every value entered

The last step of each is the reason the whole thing is worth automating: it
closes the loop. Anything typed is re-read from the app through a completely
different route (the credit case, not the customer record) and compared, so a
value the app silently dropped, truncated or transformed shows up as a failed
check rather than being assumed correct because Save returned no error.

Nothing here runs unless settings.write_allowed passes — widgets.assert_writable
is called before the browser is even asked to navigate.
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Callable, Optional

import config as crawler_config
import crawler as cr

from .. import settings
from . import results as R
from . import widgets as W
from .driver import NavigationError, Session, new_run_id
from .targets import CONTEXT_MENU, NavStep

_OBLIGOR_LIST_PATH = "/riskNucleus/master/obligorCustomer"
_BUCKET_PATH = "/riskNucleus/master/bucket"

# The request type the user asked for. Named, not "the first option", because
# picking the wrong one starts the wrong workflow.
REQUEST_TYPE = "Borrower Credit Application"

# Raising the transaction then asks which of the signed-in user's profiles is
# initiating. A credit application is initiated by the relationship side, so
# those are preferred; anything valid is accepted if none is offered.
PROFILE_PREFERENCE = ["Relationship Manager", "Relationship Associate",
                      "Senior Relationship Manager"]


def _stamp() -> str:
    return datetime.now().strftime("%m%d-%H%M")


def test_obligor_name() -> str:
    """
    Obviously machine-created and unique per run.

    Unique matters twice over: the app de-duplicates on identity fields, and the
    verification leg has to find this exact record in My Bucket afterwards among
    thousands of rows.
    """
    return f"AUTOMATION TEST {_stamp()}"


# --------------------------------------------------------------------------
# What Basic Information needs
#
# EVERY enterable field on the Create Obligor form, not only the thirteen that
# carry a red asterisk. The list was read off the live form — see
# `Filler.kind_of` for how each control is recognised in this app's markup —
# and the only thing left out is Customer ID, which the app generates and
# renders read-only.
#
# `kind` here is documentation. The control is dispatched on what the DOM
# actually holds, exactly as the tabs are: the same label is a dropdown on one
# build and a Yes/No switch on the next, and a declared kind that has drifted
# fails a field that would otherwise have set cleanly.
#
# `value=None` means "take the first valid option the app offers", which is what
# was asked for: any valid value will do, and hard-coding one would break the
# moment the environment's reference data is re-seeded. Values ARE named where
# the choice matters — a currency that makes the conversion rate meaningful, a
# country the branch data is consistent with, and "No" wherever the first option
# would flag a synthetic obligor as something a human would have to deal with.
#
# ORDER IS SIGNIFICANT:
#   - Regulatory Industry is filtered by Regulatory Sector, so the sector is
#     chosen first or the industry lookup opens empty.
#   - Obligor Id Type is populated from Obligor Type, so it follows it.
#   - The two "has life time expiry" switches DISABLE the expiry date beside
#     them, so each is set to No before its date field is reached.
# --------------------------------------------------------------------------

@dataclass
class Field:
    label: str
    kind: str                  # text | date | dropdown | lookup | checkbox | ...
    value: Optional[str] = None
    when: Optional[date] = None
    optional: bool = False     # a missing optional field is a note, not a failure
    # Rendered only once some other field has been set — Identity Details' ID
    # Value appears when an ID Type is chosen, and Obligor Id Type is populated
    # from Obligor Type. Its absence is the form behaving correctly, so it is
    # logged and passed over rather than recorded as a blocker.
    conditional: bool = False
    # Other names this same field goes by. Tried in order after `label`.
    #
    # The label written here is one build's; the screen's is configuration. A
    # live run proved the cost of assuming they are the same: this form calls
    # its incorporation date "Date of Incorporation/Establishment", the field
    # was authored as "Date of Incorporation", nothing matched, and the app
    # then refused the save with "Date of Incorporation/Establishment is
    # Required" — a mandatory field left empty over a naming difference. The
    # first alias that is actually on the screen wins.
    aliases: list[str] = field(default_factory=list)

    @property
    def names(self) -> list[str]:
        """Every name to try for this field, best first."""
        return [self.label] + [a for a in self.aliases if a != self.label]


_NOTE = "Entered by automated test."

# Phone fields validate on LENGTH, not just on being numeric: a shorter number
# is accepted into the box and then silently fails the form's Save with an
# inline message. 14 digits is what they ask for, and getting this wrong is why
# the first pass at the obligor tabs never saved anything.
PHONE = "03001234567890"          # 14 digits
FAX = "03001234567891"
EMAIL = "automation.test@example.com"
ADDRESS = "1 Automation Street, Test City"
CNIC = "3520112345671"            # 13 digits, the CNIC length
NTN = "1234567"                   # 7 digits, the FBR NTN length

_PROFILE = (
    "Synthetic obligor created by the LOS automation suite to exercise every "
    "field on the Create Obligor form. This record is not a real customer and "
    "carries no real business, financial or identity data.")

# Regulatory Segment and Net Sales are NOT independent, and getting this wrong
# is invisible until Save.
#
# The app runs a server-side rule called "Regulatory Segment Range Check" —
# `dbo.getRegulatoryExposureCheckFlag(regulatorySegment, netSale,
# noOfEmployees)` — whose message is "Net Sale and Number of Employees must
# fall within the regulatory segment range". It carries saveAllowed=false, so
# it BLOCKS the save, and the only place it appears is an HTTP 417 on
# POST /api/customerservice/config/customer/customer/0. On the page it shows as
# a "Validations not met" toast and a badge with a count, with no field marked
# and no message text — which is why filling Net Sales with a plausible-looking
# number silently stopped every obligor from being created.
#
# The thresholds are configuration, not the SBP defaults, so they were measured
# against this environment rather than assumed. For Medium Enterprise:
#
#     100,000,000  refused        750,000,000  accepted
#     150,000,000  refused        800,000,000  accepted
#     200,000,000  refused      1,000,000,000  accepted
#     400,000,000  refused
#
# The boundary is therefore somewhere in (400m, 750m] with no observed ceiling.
# 800m sits comfortably inside, and blank also saves — the rule only fires when
# Net Sales has a value, which is why this never bit before Net Sales was
# filled at all.
#
# Change one of these two and you must re-measure the other.
REGULATORY_SEGMENT = "Medium Enterprise"
NET_SALES = "800000000"


BASIC_INFORMATION: list[Field] = [
    # ---- identity ----------------------------------------------------
    Field("Existing Customer?", "dropdown", value="No"),
    Field("Obligor Name", "text"),                      # filled from the run
    # Client Number/CIF is deliberately NOT filled. It is left empty by
    # request: it identifies the customer in the core banking system, so a
    # made-up number on a synthetic obligor is worse than a blank one.
    # `check_left_empty` afterwards proves it really was left alone, rather
    # than leaving that to the absence of a line in this list.
    Field("eCIB Borrower Code", "text", value="AUTOECIB0001", optional=True),

    # ---- who owns the relationship ------------------------------------
    Field("Business Segment", "lookup"),
    Field("Relationship Branch", "lookup"),
    Field("Dealing Branch", "lookup"),
    Field("Relationship Manager", "text", value="AUTOMATION RM", optional=True),

    # ---- classification. Sector before Industry, always. ---------------
    Field("Obligor Type", "lookup", value="JOINT"),
    # Populated FROM Obligor Type, which is why it follows it — and mandatory,
    # which is why it is not optional. It is still conditional: the app renders
    # it only once Obligor Type is answered, and on a live run it was not there
    # yet at the moment this line was reached. Being conditional no longer means
    # being given up on, though: _retry_conditional comes back to it once the
    # rest of the form is filled, and the form's own validation is answered
    # after that. This one cost a save — "Obligor Id Type is Required" — while
    # being logged as "not rendered yet" and passed over.
    Field("Obligor Id Type", "dropdown", conditional=True),
    Field("Legal Constitution", "lookup"),
    Field("Regulatory Segment", "dropdown", value=REGULATORY_SEGMENT),
    Field("Regulatory Sector", "lookup"),
    Field("Regulatory Industry", "lookup"),             # after the sector
    Field("Sector Strategy", "dropdown", optional=True),
    Field("Line of Business", "dropdown", optional=True),
    Field("SBP Classification (Current)", "lookup", optional=True),
    Field("Obligor Group", "lookup", optional=True),
    Field("Green Field Project", "dropdown", value="No", optional=True),
    # A corporate obligor has no gender; the app offers the option, so it is
    # the honest answer rather than picking the first name in the list.
    Field("Gender", "dropdown", value="Not Applicable", optional=True),

    # ---- money ---------------------------------------------------------
    # Pakistan and PKR are named, not taken first: the country list starts at
    # Afghanistan and the currency list at GBP, and a Pakistani branch holding
    # a sterling obligor makes the conversion rate below meaningless.
    Field("Country of Incorporation", "dropdown", value="Pakistan"),
    Field("Obligor Currency", "dropdown", value="PKR"),
    Field("Conversion Rate", "text", value="1", optional=True),
    # Must sit inside REGULATORY_SEGMENT's band — see the note above.
    Field("Net Sales", "text", value=NET_SALES, optional=True),
    Field("Net Income (as per FBR)", "text", value="5000000", optional=True),
    Field("Net Exposure", "text", value="10000000", optional=True),

    # ---- dates ---------------------------------------------------------
    # This form calls it "Date of Incorporation/Establishment". The shorter
    # name is kept first because other builds use it, and the screen's own
    # wording follows — see Field.aliases for what assuming one name cost.
    Field("Date of Incorporation", "date", when=date(2020, 1, 1),
          aliases=["Date of Incorporation/Establishment",
                   "Date of Incorporation / Establishment",
                   "Date of Establishment"]),
    Field("Date of Commencement", "date", when=date(2020, 6, 1), optional=True),
    Field("Financing Relationship Since", "date", when=date(2021, 3, 15),
          optional=True),
    Field("Account Opening Date", "date", when=date(2021, 4, 1), optional=True),

    # ---- registrations -------------------------------------------------
    Field("Import Registration Number", "text", value="IMP0000001",
          optional=True),
    Field("Export Registration Number", "text", value="EXP0000001",
          optional=True),
    Field("Sales Tax Number (STRN)", "text", value="1234567891234",
          optional=True),
    # Ten characters is the cap the app enforces on this one.
    Field("Credit Memorandum / Application Reference Number", "text",
          value="AUTO-00001", optional=True),

    # ---- the obligor's own ID. 'No' first, or the date is disabled. -----
    Field("Has LifeTime Expiry", "dropdown", value="No", optional=True),
    Field("ID Expiry Date", "date", when=date(2030, 12, 31), optional=True),

    # ---- guardian details (an individual obligor's) ---------------------
    Field("Father/Guardian Name", "text", value="AUTOMATION GUARDIAN",
          optional=True),
    Field("Father / Guardian ID Type", "dropdown", value="CNIC", optional=True),
    Field("Father/Guardian ID Value", "text", value=CNIC, optional=True),
    Field("Father/Guardian ID Has life time expiry", "dropdown", value="No",
          optional=True),
    Field("Father/Guardian ID Expiry Date", "date", when=date(2030, 12, 31),
          optional=True),

    # ---- narrative and contact -----------------------------------------
    Field("Business Profile / Brief Background", "textarea", value=_PROFILE,
          optional=True),
    Field("Website", "text", value="www.automation-test.example", optional=True),
    Field("Address", "text", value=ADDRESS, optional=True),
    Field("Email", "text", value=EMAIL, optional=True),
    Field("Fax #", "text", value=FAX, optional=True),

    Field("Is Relationship With Other Bank?", "checkbox", optional=True),
    Field("Initiated By", "dropdown", optional=True, conditional=True),
]


# Fields that must come out of the fill UNTOUCHED. Naming them here and then
# reading them back is what turns "we did not write a line for it" into a
# result a reader can see. The spellings vary by build, so each is a list of
# what the label might read as; the first one found on screen is the one
# checked, and finding none is reported as "could not check" rather than a pass.
LEAVE_EMPTY: list[list[str]] = [
    ["Client Number/CIF", "Client Number / CIF", "Client No/CIF",
     "Client Number", "CIF", "CIF Number", "Client No."],
]


@dataclass
class FlowResult:
    run_id: str
    started_at: str
    finished_at: str = ""
    artifacts_dir: str = ""
    obligor_name: str = ""
    customer_id: str = ""
    request_id: str = ""
    entries: list[dict] = field(default_factory=list)
    steps: list[dict] = field(default_factory=list)
    checks: list[R.Check] = field(default_factory=list)
    dry_run: bool = True
    # Why the run could not proceed at all — an environment problem, never a
    # finding about the application.
    error_reason: str = ""
    # Observations: things worth telling the operator that are not verdicts.
    # A field the screen does not have under the name this suite knows it by
    # goes here, and the run carries on.
    notes: list[R.Note] = field(default_factory=list)
    # Which flow produced this. The page picks its layout from it, and the two
    # write flows show different things: a create run is about the ids it
    # minted, a detail run about the tabs it filled on a case that already had
    # them.
    target_key: str = "obligor.create"
    target_title: str = ""

    @property
    def counts(self) -> dict:
        out = {R.PASS: 0, R.FAIL: 0}
        for c in self.checks:
            out[c.status] = out.get(c.status, 0) + 1
        return out

    @property
    def overall(self) -> str:
        if self.error_reason:
            return R.ERROR
        c = self.counts
        if c[R.FAIL]:
            return R.FAIL
        return R.PASS if c[R.PASS] else R.ERROR

    @property
    def headline(self) -> str:
        c = self.counts
        out = f"{c[R.PASS]} passed / {c[R.FAIL]} failed"
        if self.notes:
            out += f" / {len(self.notes)} observation(s)"
        return out

    def to_dict(self) -> dict:
        d = {k: v for k, v in self.__dict__.items()
             if k not in ("checks", "notes")}
        d["checks"] = [c.__dict__ for c in self.checks]
        d["notes"] = [n.__dict__ for n in self.notes]
        d["overall"] = self.overall
        d["headline"] = self.headline
        return d


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _refused_rules(s: Session) -> list[str]:
    """
    The validation rules the SERVER used to refuse the last commit.

    This exists because the refusal is not on the page. When a configured rule
    blocks a save this app answers HTTP 417 with the rule objects in the body,
    and shows the operator a "Validations not met" toast plus a badge with a
    count — no field is marked, no message text is rendered, and
    Filler.messages() therefore comes back empty. The run would then say "Save
    reported no error, but no Customer ID appeared", which is true and useless.

    widgets.commit clears the recorder immediately before clicking, so
    everything here belongs to that one click.

    Returns readable one-liners, most useful first: the blocking rules before
    the advisory ones.
    """
    found: list[tuple[int, str]] = []
    seen: set[str] = set()
    for resp in (s.recorder.responses if s.recorder else []):
        if (resp.get("status") or 0) < 400:
            continue
        try:
            payload = json.loads(resp.get("response_body") or "")
        except (ValueError, TypeError):
            continue
        # The rules arrive either as the body's "body" key or as a bare list.
        rules = payload.get("body") if isinstance(payload, dict) else payload
        if not isinstance(rules, list):
            continue
        for rule in rules:
            if not isinstance(rule, dict) or not rule.get("title"):
                continue
            kind = (rule.get("validationType") or {}).get("title") or "Validation"
            blocking = rule.get("saveAllowed") is False
            text = (f"{kind}{' (blocks the save)' if blocking else ''}: "
                    f"{rule['title']}")
            why = (rule.get("description") or "").strip()
            if why:
                text += f" — {why}"
            if text in seen:
                continue
            seen.add(text)
            found.append((0 if blocking else 1, text))
    found.sort(key=lambda x: x[0])
    return [t for _, t in found]


def _not_on_screen(exc: Exception) -> bool:
    """
    Was there nothing here to set yet, as opposed to a field refusing a value?

    The two need telling apart: a conditional field that has not been populated
    yet is the form working as designed, while one that is present and will not
    take a value is worth reporting.

    "Nothing to set yet" has two shapes in this application. The control may be
    absent from the DOM entirely — Identity Details renders ID Value only once
    an ID Type is chosen — or it may be present but EMPTY, which is what a
    dependent dropdown looks like before the field it depends on has been
    answered: Obligor Id Type is populated from Obligor Type, and Initiated By
    from the signed-in user's profiles. Both open a panel with nothing in it.
    """
    text = str(exc)
    return any(p in text for p in (
        "No field labelled",
        "opened no options",
        "offered only placeholder options",
        "offered nothing",
        "every one is unselectable",
    ))


# --------------------------------------------------------------------------

def create_obligor(headless: Optional[bool] = None, dry_run: bool = True,
                   obligor_name: str = "", progress: Optional[Callable] = None,
                   run_id: str = "", emit=None,
                   stop_after_save: bool = False,
                   fill_tabs: bool = False) -> FlowResult:
    """
    Create an obligor from Basic Information alone, raise a credit application
    against it, then verify.

    ONLY Basic Information is filled, and Client Number/CIF is left empty
    within it. The nine tabs that unlock after Save are the job of
    `fill_case_obligor_details`, which runs against the case this leaves
    behind — keeping them apart means creating a record is a short run whose
    outcome is legible, and finishing one is a separate decision.

    `fill_tabs=True` restores the old single-run behaviour. Nothing calls it
    that way; it is kept so the tab filling can still be exercised against a
    brand new record while working on it.

    dry_run=True fills every field and reports what the form then contains
    WITHOUT clicking Save, which is how a change to this flow gets checked
    without leaving a record behind. dry_run=False commits.

    stop_after_save=True creates the obligor but does not raise a transaction —
    useful when only the create leg is being worked on.
    """
    reason = W.assert_writable()          # before a browser is even launched
    run_id = run_id or new_run_id("create" if not dry_run else "dryrun")
    _emit = emit or (lambda e: None)
    name = obligor_name or test_obligor_name()

    res = FlowResult(run_id=run_id, started_at=_now(), dry_run=dry_run,
                     obligor_name=name,
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
    say(f"{'DRY RUN — nothing will be saved' if dry_run else 'LIVE — a record will be created'}")
    say(f"Obligor name: {name}")
    say("Filling Basic Information only"
        + (" — the other tabs are a separate run" if not fill_tabs
           else " and then every tab that unlocks"))
    say("Client Number/CIF is left empty, by request.")

    try:
        with Session(run_id, mode=settings.TRANSACT, headless=headless,
                     emit=_emit) as s:
            s.login()
            step("sign in")

            # ---- All Obligors -> Create Obligor -------------------------
            nav = cr.navigate_in_app(
                s.page,
                {"label": "All Obligors", "path": _OBLIGOR_LIST_PATH,
                 "href": _OBLIGOR_LIST_PATH,
                 "url": crawler_config.BASE_URL.rstrip("/") + _OBLIGOR_LIST_PATH},
                s.recorder)
            if not nav.get("ok"):
                raise NavigationError("Could not open All Obligors.", environmental=True)
            step("open All Obligors")
            s.screenshot("01-all-obligors")

            btns = cr.collect_action_buttons(s.page)
            create = next((b for b in btns
                           if "create" in (b["label"] or "").lower()
                           and "obligor" in (b["label"] or "").lower()), None)
            if create is None:
                raise NavigationError(
                    "There is no 'Create Obligor' button on All Obligors. "
                    "Available: "
                    + ", ".join(b["label"] for b in btns[:8]))
            if not cr._click_stamped(s.page, "data-crawl-action",
                                     create["index"], timeout=8000):
                raise NavigationError("'Create Obligor' could not be clicked.")
            cr.wait_until_settled(s.page, s.recorder, timeout_ms=20000,
                                  stable_polls=2)
            cr.stamp_content_root(s.page)
            step("click Create Obligor", note=s.page.url)
            s.screenshot("02-create-form")

            # ---- fill Basic Information --------------------------------
            f = W.Filler(session=s, screen="Basic Information")
            # Conditional fields that were not on screen when they came round.
            # Not the end of the matter — see _retry_conditional.
            deferred: list[Field] = []
            for spec in BASIC_INFORMATION:
                try:
                    e = set_field(
                        f, spec,
                        name if spec.label == "Obligor Name" else "")
                    say(f"  {spec.label} = {e.value[:60]!r}")
                    step(f"set {spec.label}", note=e.value)
                # A raw Playwright timeout on an odd control has to be caught
                # too, or one awkward optional field aborts the whole create.
                except (W.FillError, Exception) as exc:  # noqa: BLE001
                    if spec.conditional and _not_on_screen(exc):
                        say(f"  - {spec.label}: not rendered yet — will come "
                            f"back to it")
                        deferred.append(spec)
                        continue

                    # A field this screen does not HAVE is not a field that
                    # refused a value, and it must never stop the run.
                    #
                    # These labels are authored here; the screen's are
                    # configuration. A field that has been renamed, or that
                    # this deployment does not switch on, reads exactly like a
                    # missing one — and aborting threw away a form that was
                    # otherwise completely filled. That is what happened when
                    # 'Date of Incorporation' could not be found: nothing was
                    # saved, and the nine tabs behind Save were never reached,
                    # over a label.
                    #
                    # Whether it was really required is not this loop's call to
                    # make either. The APP is the authority on that, it says so
                    # in its own validation messages, and those are read a few
                    # lines below before Save is pressed. So: record what was
                    # not found, carry on, and let the form answer.
                    if _not_on_screen(exc):
                        res.notes.append(R.note(
                            f"{spec.label!r} is not on Basic Information under "
                            f"that name, so it was not filled. It may have been "
                            f"renamed in this deployment, or not be configured "
                            f"here. Everything else was filled and saved; the "
                            f"form's own validation below says whether the "
                            f"screen actually required it.",
                            screen="Basic Information"))
                        say(f"  - {spec.label}: not on this screen — noted, "
                            f"carrying on")
                        step(f"set {spec.label}", note="not on this screen")
                        continue

                    # Present, and refusing a value. That IS a finding.
                    shot = s.screenshot(f"fill-failed-{spec.label[:24]}")
                    res.checks.append(R.failed(
                        f"Basic Information accepts {spec.label}",
                        expected=f"{spec.label} can be set",
                        actual=str(exc),
                        detail="The field is on the screen and would not take "
                               "a value.",
                        evidence=[shot] if shot else [],
                        screen="Basic Information"))
                    step(f"set {spec.label}", status=R.FAIL, note=str(exc)[:160])
                    say(f"    !! {spec.label}: {str(exc)[:110]}")

            # A conditional field that was not rendered when its turn came may
            # well be rendered now that the field it depends on is answered.
            _retry_conditional(f, deferred, res, say, step)

            # Then whatever the form ITSELF still says it needs. This is the
            # last line of defence for "fill every mandatory field", and the
            # only one that does not depend on anybody having authored the
            # right label: the screen names its own required fields, so they
            # are read and acted on BEFORE Save rather than quoted afterwards
            # as the reason it failed.
            _satisfy_required(f, res, say, step)

            res.entries = [e.as_dict() for e in f.entries]
            s.screenshot("03-filled")

            # Asked for explicitly, so it is checked explicitly rather than
            # inferred from the absence of a line in BASIC_INFORMATION.
            res.checks.extend(check_left_empty(f, say, res.notes))

            # Every mandatory field set means no validation message should be
            # left standing. Checking BEFORE Save is what turns "Save did
            # nothing and we do not know why" into a readable result.
            msgs = f.messages()
            outstanding = [m for m in msgs["fields"] if "required" in m.lower()]
            if outstanding:
                res.checks.append(R.failed(
                    "Every mandatory field on Basic Information is filled",
                    expected="no 'is Required' message left on the form",
                    actual=f"{len(outstanding)} still flagged: "
                           + " | ".join(outstanding[:5]),
                    detail="The form would refuse to save in this state.",
                    evidence=[s.screenshot("04-validation")],
                    screen="Basic Information"))
            else:
                res.checks.append(R.passed(
                    "Every mandatory field on Basic Information is filled",
                    detail=f"{len(f.entries)} fields set, no validation "
                           f"messages outstanding."))

            if dry_run:
                say("Dry run — stopping before Save. Nothing was written.")
                step("stop before Save (dry run)")
                res.notes.append(R.note(
                    "Dry run: Save was deliberately not clicked, so no record "
                    "was created and nothing below was verified against a "
                    "saved record. Re-run without --dry-run to commit.",
                    screen="Basic Information"))
                return _finish(res, s, say)

            # ---- commit -------------------------------------------------
            say("Saving …")
            f.commit("Save")
            step("click Save")
            s.screenshot("05-after-save")
            msgs = f.messages()
            # Read BEFORE anything else clicks: the recorder holds only this
            # click's traffic, and a configured rule that blocks the save says
            # so nowhere else.
            refused = _refused_rules(s)
            if msgs["bad"] or refused:
                res.checks.append(R.failed(
                    "Obligor is saved",
                    expected="the app confirms the obligor was created",
                    actual=" | ".join((msgs["bad"] + refused)[:3]),
                    detail="The application refused the save. Its own "
                           "validation rules are quoted above — a rule marked "
                           "'blocks the save' means the values entered have to "
                           "change, not the automation."
                    if refused else "",
                    evidence=[s.screenshot("06-save-error")],
                    screen="Basic Information"))
                for r in refused:
                    say(f"  !! refused: {r}")
                return _finish(res, s, say)

            # A Customer ID appearing is the app's own proof it saved: the field
            # is system-generated and empty until then.
            cid = f.value_of("Customer ID")
            res.customer_id = cid
            if cid:
                res.checks.append(R.passed(
                    "Obligor is saved",
                    detail=f"The app assigned Customer ID {cid}."))
                say(f"Created obligor {cid}")
            else:
                # No Customer ID is a FAILURE, not an unknown. The field is
                # system-generated and empty until the app has stored the
                # record, so its absence is the app declining to show any
                # evidence that it did — and this is the one assertion the
                # whole flow exists to make. Reporting it as anything softer
                # would let silence read as success.
                res.checks.append(R.failed(
                    "Obligor is saved",
                    expected="the app assigns a Customer ID",
                    actual="no Customer ID appeared on the form",
                    detail="Save reported no error and named no failing rule, "
                           "but the system-generated Customer ID is still "
                           "empty, so nothing confirms the record was created. "
                           + (" | ".join(msgs["ok"][:2]) if msgs["ok"] else ""),
                    evidence=[s.screenshot("06-no-customer-id")],
                    screen="Basic Information"))
                return _finish(res, s, say)

            # ---- the nine tabs that just unlocked -----------------------
            # More passes than tabs: each grid is filled separately. See
            # TAB_SPECS.
            if fill_tabs:
                say(f"Filling the remaining tabs — {len(TAB_SPECS)} passes …")
                for tab in TAB_SPECS:
                    say(f"  {tab.title} …")
                    entries = fill_tab(s, tab, dry_run, say, step, res)
                    res.entries.extend(e.as_dict() for e in entries)
                say(f"  {len(res.entries)} field(s) entered in total")

            if stop_after_save:
                return _finish(res, s, say)

            # ---- Raise Transaction -> a credit case ---------------------
            say("Raising the transaction …")
            rid = raise_transaction(s, f, say, step)
            res.request_id = rid
            if rid:
                res.checks.append(R.passed(
                    "Raising the transaction creates a credit case",
                    detail=f"The app assigned request ID {rid}."))
                say(f"Created case {rid}")
            else:
                # Not a verdict either way: the case may well have been
                # created and simply not have its id on screen. The My Bucket
                # check immediately below is what actually answers it, by
                # obligor name instead of by id.
                s.screenshot("09-no-request-id")
                res.notes.append(R.note(
                    "Proceed reported no error but no request ID could be read "
                    "from the page, so the case was looked up by obligor name "
                    "instead."))

            # ---- find it in My Bucket and verify ------------------------
            needle = rid or name
            say(f"Finding {needle!r} in My Bucket …")
            try:
                row = find_case(s, needle, say)
                res.checks.append(R.passed(
                    "The new case appears in My Bucket",
                    detail=f"Found and opened: {row[:120]}"))
                step("open the case from My Bucket", note=row[:120])
                s.screenshot("09-case-open")
            except NavigationError as e:
                # A case that was just created and is not in the grid is a
                # finding unless the environment itself is what stopped us.
                if e.environmental:
                    res.error_reason = str(e)
                    step("open the case from My Bucket", R.ERROR, str(e)[:160])
                else:
                    res.checks.append(R.failed(
                        "The new case appears in My Bucket",
                        expected="the case just created is listed in My Bucket",
                        actual=str(e),
                        evidence=[s.screenshot("09-case-not-found")]))
                    step("open the case from My Bucket", R.FAIL, str(e)[:160])
                return _finish(res, s, say)

            # Obligor Details (BIR) is a case-menu entry, so it is reached the
            # same way the read-only runner reaches it — but the case package's
            # sidebar is fetched after the route settles, so looking too early
            # finds an empty menu and reports the entry missing. Wait for the
            # menu to actually populate first.
            if not wait_for_case_menu(s, say):
                s.screenshot("10-no-case-menu")
                res.error_reason = (
                    "The case opened but its sidebar never populated, so "
                    "Obligor Details (BIR) could not be reached. This is the "
                    "environment being slow rather than a missing screen.")
                return _finish(res, s, say)
            try:
                note = s._step_context_menu(
                    NavStep(kind=CONTEXT_MENU, label="Obligor Details (BIR)"))
                step("open Obligor Details (BIR)", note=note)
            except NavigationError as e:
                res.checks.append(R.failed(
                    "Obligor Details (BIR) can be opened in the new case",
                    expected="the case menu offers Obligor Details (BIR)",
                    actual=str(e),
                    evidence=[s.screenshot("10-no-obligor-details")]))
                return _finish(res, s, say)

            say("Verifying what was entered …")
            res.checks.extend(
                verify_entered_values(s, res.entries, say, res.notes))
            return _finish(res, s, say)

    except W.WriteRefused as e:
        res.error_reason = str(e)
    except NavigationError as e:
        res.error_reason = str(e)
    except Exception as e:  # noqa: BLE001 - environment failure, not a defect
        res.error_reason = str(e)[:400]

    res.finished_at = _now()
    _persist(res)
    return res


# --------------------------------------------------------------------------
# The nine tabs that unlock once the obligor is saved
#
# They are locked until then — the app ignores clicks on them on a fresh Create
# Obligor form — which is why they are a separate pass after Save rather than
# part of the same fill.
#
# Two shapes, and they are filled completely differently:
#
#   form      fields sit on the tab; fill them and press the tab's own Save.
#   linkage   the tab holds a GRID with a '+' that opens an add-row dialog. The
#             fields are in the dialog, and the dialog has its own Save. The
#             live application has EIGHTEEN of them across these tabs,
#             thirteen on Additional Information alone.
#
# A tab is therefore not one entry in this list. Additional Information appears
# fourteen times — once for the fields on the tab itself and once per grid —
# because each grid is a separate dialog with its own Save and its own success
# or failure to report. `name` is what the checks call each pass; `label` stays
# the tab's own name, because that is what the verification leg has to re-open
# inside the credit case.
#
# Grids are addressed by their HEADING, never by position. The '+' icons are
# only in the DOM while their section is expanded, so an index that meant
# "Major Buyers" on one build means "Major Brands" on the next.
#
# Values are chosen to be obviously synthetic and to satisfy the field's type
# and its length limit — several boxes here cap at 3, 10 or 14 characters and
# reject anything longer AFTER accepting the keystrokes. `None` on a dropdown or
# lookup means "first valid option", as agreed: hard-coding reference data would
# break when an environment is re-seeded.
# --------------------------------------------------------------------------

FORM = "form"
LINKAGE = "linkage"


@dataclass
class TabSpec:
    label: str                  # the tab to open, and the screen entries record
    kind: str
    fields: list[Field] = field(default_factory=list)
    grid: int = 0               # fallback: which '+' when the grid has no caption
    grid_heading: str = ""      # preferred: the caption above the grid
    name: str = ""              # what the checks call this pass; defaults to label
    save_label: str = "Save"
    note: str = ""
    # Nothing to type: History is an audit trail, and Attachments on this tab
    # is a read-only map of which field holds which document.
    skip: bool = False

    @property
    def title(self) -> str:
        return self.name or self.label


def _text(label: str, value: str, optional: bool = True) -> Field:
    return Field(label, "text", value=value, optional=optional)


def _rich(label: str, value: str) -> Field:
    return Field(label, "rich", value=value, optional=True)


def _pick(label: str, optional: bool = True,
          value: Optional[str] = None) -> Field:
    """A dropdown or switch. `value=None` takes whatever the app offers first."""
    return Field(label, "dropdown", value=value, optional=optional)


def _look(label: str, optional: bool = True) -> Field:
    return Field(label, "lookup", optional=optional)


def _when(label: str, when: date, optional: bool = True) -> Field:
    return Field(label, "date", when=when, optional=optional)


def _tick(label: str, on: bool = True) -> Field:
    return Field(label, "checkbox", value="Yes" if on else "No", optional=True)


def _note_for(what: str) -> str:
    """
    A narrative value that names the box it went into.

    BBFS Details is nineteen free-text editors on one screen, and Additional
    Information adds another six. Typing the same sentence into all of them
    makes the round-trip check vacuous: any box would match any other. Naming
    the box is what turns "some text came back" into "THIS box's text came
    back".
    """
    return f"{_NOTE} Section: {what}."


# Compliance answers are pinned to No wherever the control offers Yes/No.
# "First valid option" would turn them ON, and marking a synthetic obligor as
# politically exposed, NAB/FIA listed or a related party in a shared
# environment invites real downstream handling of a record that is not real.
NO = "No"


TAB_SPECS: list[TabSpec] = [

    # ======================================================================
    # Sector And Industry — one grid
    # ======================================================================
    TabSpec(
        "Sector And Industry", LINKAGE,
        grid_heading="FINANCING SECTOR AND INDUSTRY",
        fields=[
            # Both NBP lookups render with a disabled input plus a magnifier,
            # so they are set through the lookup modal like any other. Sector
            # first: the industry tree is filtered by it.
            _look("NBP Sector", optional=False),
            _look("NBP Industry", optional=False),
            _pick("Sector Strategy"),
            _pick("Industry Strategy"),
        ]),

    # ======================================================================
    # Management & Shareholders — three grids, and all three are filled
    # ======================================================================
    TabSpec(
        "Management & Shareholders", LINKAGE,
        name="Management & Shareholders — Shareholders and Directors",
        grid_heading="SHAREHOLDERS AND DIRECTORS DETAILS",
        fields=[
            _text("Shareholder Name", "AUTOMATION SHAREHOLDER", optional=False),
            _pick("Shareholder Type"),
            _text("No. of shared held", "1000"),
            Field("Shareholding Percentage", "text", value="25",
                  optional=False),
            _text("Shareholding Amount", "2500000"),
            _pick("Major / Minor Holdings"),
            _pick("Gender", optional=False),
            _text("Father / Husband Name", "AUTOMATION FATHER"),
            # Nationality was previously left out on the grounds that the app
            # fills it from the shareholder's ID and renders it disabled. It is
            # a lookup on this build, and a lookup is set through its magnifier
            # rather than by typing, so it sets cleanly either way.
            _look("Nationality"),
            _text("Email", EMAIL),
            _text("Phone Number", PHONE),
            _text("Fax No", FAX),
            _text("Address", ADDRESS),
            _pick("PEP", optional=False, value=NO),
            _pick("On NAB / FIA List", optional=False, value=NO),
            _pick("Related Party?", optional=False, value=NO),
            _text("Position in BOD", "Director"),
            _text("Experience", "10"),
            _text("Since", "2015"),
            _pick("Successor"),
            _text("Name of Nominating Agency (If nominated)",
                  "AUTOMATION NOMINATING AGENCY"),
            _rich("Other Entities (BOD/Ownership)",
                  _note_for("Other Entities (BOD/Ownership)")),
            _rich("Ultimate Beneficial Owners (UBOs)",
                  _note_for("Ultimate Beneficial Owners")),
            _text("Comments", _NOTE),
        ]),

    TabSpec(
        "Management & Shareholders", LINKAGE,
        name="Management & Shareholders — Management",
        grid_heading="MANAGEMENT",
        fields=[
            _pick("Title"),
            _text("Full Name", "AUTOMATION MANAGER", optional=False),
            _pick("Designation"),
            _pick("Nature of Directorship"),
            _text("Father / Husband Name", "AUTOMATION FATHER"),
            _pick("Gender"),
            _when("Date Of Birth", date(1980, 5, 20)),
            _look("Nationality"),
            _pick("Country"),
            _text("Contact Number", PHONE),
            _text("Fax No", FAX),
            _text("Email", EMAIL),
            _text("Official Address", ADDRESS),
            _text("Qualification", "MBA Finance"),
            _text("Industry Experience (Yrs.)", "15"),   # capped at 3 characters
            _text("With Company Since (Yrs.)", "2015"),  # capped at 4
            _text("Net Worth", "5000000"),
            _pick("Known To Bank"),
            _pick("Successor"),
            _pick("Is also a Shareholder?", value=NO),
            _pick("PEP Status", value=NO),
            _pick("On NAB / FIA List?", value=NO),
            _pick("Related Party?", value=NO),
            _tick("Is Guarantor"),
            _text("Guarantee Limit (PKR)", "1000000"),
            _rich("Profile/Biography", _note_for("Manager profile")),
            _rich("Succession Planning", _note_for("Succession planning")),
            _text("Management", _NOTE),
            _text("Comments", _NOTE),
            # Shareholding % is computed by the app from the shareholder record
            # this manager is linked to, and rendered read-only.
        ]),

    TabSpec(
        "Management & Shareholders", LINKAGE,
        name="Management & Shareholders — Related Party Transactions",
        grid_heading="RELATED PARTY TRANSACTION",
        fields=[
            _text("Related Party Name", "AUTOMATION RELATED PARTY",
                  optional=False),
            _text("Brief Description Of Transaction",
                  "Automated test transaction recorded against a synthetic "
                  "obligor.", optional=False),
            _text("Rationale Of RPT",
                  "Exercises the related party transaction table end to end."),
            _text("Amount (in Actual)", "1000000", optional=False),
            _pick("Impaired / Past Due", value=NO),
        ]),

    # ======================================================================
    # Additional Information — the tab's own fields, then thirteen grids
    # ======================================================================
    TabSpec(
        "Additional Information", FORM,
        name="Additional Information — the tab's own fields",
        fields=[
            _pick("Large Exposure"),
            _pick("Obligor’s Classification Status"),
            _pick("Rated by ECAI?"),
            _text("FI Country Rank", "10"),
            _text("FI World Rank", "100"),
            _text("Key Person Contact No.", PHONE),
            _text("No. of Total Employees", "250"),
            _text("Annual Export Business (PKR)", "1000000"),
            _text("Annual Import Business (PKR)", "2000000"),
            _when("Customer Visit Date", date(2025, 6, 30)),
            _pick("Borrowers Declaration Regarding Aggregate Clean Exposure "
                  "Obtained?"),
            # Compliance flags are set to No deliberately — see NO above.
            _pick("Is Obligor a Related Party?", value=NO),
            _pick("Politically Exposed?", value=NO),
            _pick("Is Customer on NAB / FIA List?", value=NO),
            _tick("Export Oriented"),
            _tick("Listed on PSX"),
            _rich("Highlight AML/ Compliance Status of Client",
                  _note_for("AML / compliance status")),
            _rich("Details about relationships with other Financial "
                  "Institutions",
                  _note_for("Relationships with other financial institutions")),
            _rich("Market Check / Reputation", _note_for("Market check")),
            _rich("Business Process End to End", _note_for("Business process")),
            _rich("Organization Structure",
                  _note_for("Organization structure")),
            _rich("Relationship with other banks changes over the years",
                  _note_for("Relationship changes over the years")),
            _rich("Any Other Comments", _note_for("Any other comments")),
        ]),

    TabSpec(
        "Additional Information", LINKAGE,
        name="Additional Information — Obligor's Deposit Accounts",
        grid_heading="OBLIGORS DEPOSIT ACCOUNTS",
        fields=[
            _pick("Nature of Account", optional=False),
            _text("Account Title", "AUTOMATION TEST ACCOUNT", optional=False),
            _text("Account Number", "PK00AUTO0000000001"),
            # Bank before Branch: the branch lookup is filtered by the bank.
            _pick("Select Bank"),
            _look("Branch Name", optional=False),
            _text("Swift Code", "NBPAPKKA"),
            _text("Purpose of Account", "Operating account for automated test."),
            _text("Current Balance", "1000000"),
            _when("Date of Account Opened", date(2021, 4, 1)),
            _when("Date of Current Balance", date(2025, 12, 31)),
            _when("Turnover Period From", date(2025, 1, 1)),
            _when("Turnover Period To", date(2025, 12, 31)),
            _text("Credit Turnover - Min Balance", "100000"),
            _text("Credit Turnover - Max Balance", "900000"),
            _text("Credit Turnover - Avg Balance", "500000"),
            _text("Credit Turnover - Turn Over", "6000000"),
            _text("Debit Turnover - Min Balance", "80000"),
            _text("Debit Turnover - Max Balance", "800000"),
            _text("Debit Turnover - Avg Balance", "400000"),
            _text("Debit Turnover - Turn Over", "4800000"),
        ]),

    TabSpec(
        "Additional Information", LINKAGE,
        name="Additional Information — References",
        grid_heading="REFERENCES",
        fields=[
            _text("Name", "AUTOMATION REFERENCE", optional=False),
            _text("Phone", PHONE, optional=False),
            _text("Email Address", EMAIL),
            _text("Address", ADDRESS),
            _text("NIC", CNIC),
            _text("NTN", NTN),
        ]),

    TabSpec(
        "Additional Information", LINKAGE,
        name="Additional Information — Bank Check",
        grid_heading="BANK CHECK",
        fields=[
            _pick("Bank", optional=False),
            _text("Name Of Person", "AUTOMATION CONTACT"),
            _text("Designation", "Branch Manager"),
            _when("Date", date(2025, 6, 30)),
            _text("Remarks", _note_for("Bank check")),
        ]),

    TabSpec(
        "Additional Information", LINKAGE,
        name="Additional Information — Customer's External Ratings",
        grid_heading="CUSTOMERS EXTERNAL RATINGS",
        fields=[
            _pick("Agency", optional=False),
            _pick("Short Term Rating"),
            _pick("Long Term Rating"),
            _when("Date Of External Rating", date(2025, 6, 30)),
            _pick("Outlook"),
            _pick("Rating Applicable On"),
        ]),

    TabSpec(
        "Additional Information", LINKAGE,
        name="Additional Information — Company Market Check",
        grid_heading="COMPANY MARKET CHECK",
        fields=[
            _pick("Audience Type", optional=False),
            _text("Name & Designation", "AUTOMATION CONTACT, Director",
                  optional=False),
            _text("Business Relationship", "Supplier", optional=False),
            _when("Date", date(2025, 6, 30), optional=False),
            _text("Remarks", _note_for("Company market check"), optional=False),
        ]),

    TabSpec(
        "Additional Information", LINKAGE,
        name="Additional Information — Manufacturing / Operating Facilities",
        grid_heading="MANUFACTURING FACILITIES / OPERATING FAC",
        fields=[
            _text("Product", "AUTOMATION PRODUCT"),
            _text("Capacity Unit", "Units"),
            _text("Installed Capacity", "100000"),
            _text("Capacity Utilization %", "75"),      # capped at 3 characters
            _text("% of Sales", "60"),
            _pick("Ownership Status"),
            _pick("City"),
            _text("Production Period", "Jan 2025 - Dec 2025"),
            _rich("Description", _note_for("Facility description")),
            _rich("Location", _note_for("Facility location")),
        ]),

    TabSpec(
        "Additional Information", LINKAGE,
        name="Additional Information — Major Products",
        grid_heading="MAJOR PRODUCTS",
        fields=[
            _text("Product", "AUTOMATION PRODUCT"),
            _pick("Business Type"),
            _pick("Status", optional=False),
            _text("Capacity type (Kgs, Units, etc.)", "Units"),
            _text("Production Capacity(Current Year)", "1000000"),
            _text("Production Capacity(Previous Year)", "900000"),
            _text("Actual Production(Current Year)", "800000"),
            _text("Actual Production(Previous Year)", "700000"),
            _rich("Remarks", _note_for("Major products")),
        ]),

    TabSpec(
        "Additional Information", LINKAGE,
        name="Additional Information — Major Suppliers",
        grid_heading="MAJOR SUPPLIER(S)",
        fields=[
            _text("Supplier", "AUTOMATION SUPPLIER", optional=False),
            _pick("Status", optional=False),
            _text("Main Items", "Raw material for automated test."),
            _text("% of Total Purchases", "30"),
            _text("RM as % of COGS", "40"),
            _text("Selling Terms", "30 days credit."),
            _pick("Country / Currency (Supplier)"),
            _when("Relationship Since (Supplier)", date(2021, 1, 1)),
            _pick("NBP's Customer (Supplier)", value=NO),
            _pick("Obligor's Related Party (Supplier)", value=NO),
            _text("Supplier(s) Other Information",
                  _note_for("Supplier other information")),
        ]),

    TabSpec(
        "Additional Information", LINKAGE,
        name="Additional Information — Major Buyers",
        grid_heading="MAJOR BUYER(S)",
        fields=[
            _text("Buyer Name", "AUTOMATION BUYER", optional=False),
            _pick("Status", optional=False),
            _pick("Product(s)"),
            _text("% of Total Sales", "35"),
            _text("Buying Terms", "45 days credit."),
            _pick("Country / Currency (Buyer)"),
            _when("Relationship Since", date(2021, 1, 1)),
            _pick("NBP's Customer (Buyer)", value=NO),
            _pick("Obligor's Related Party (Buyer)", value=NO),
            _text("Product(s) Other Information",
                  _note_for("Buyer product information")),
            _text("Buyer(s) Other Information",
                  _note_for("Buyer other information")),
        ]),

    TabSpec(
        "Additional Information", LINKAGE,
        name="Additional Information — Major Competitors",
        grid_heading="MAJOR COMPETITOR(S)",
        fields=[
            _text("Competitors", "AUTOMATION COMPETITOR", optional=False),
            _pick("Status", optional=False),
            _text("Estimated Market Share %", "15"),
        ]),

    TabSpec(
        "Additional Information", LINKAGE,
        name="Additional Information — Major Brands",
        grid_heading="MAJOR BRAND(S)",
        fields=[
            _text("Brand Name(s)", "AUTOMATION BRAND", optional=False),
        ]),

    TabSpec(
        "Additional Information", LINKAGE,
        name="Additional Information — Identity Details",
        grid_heading="IDENTITY DETAILS",
        fields=[
            _pick("ID Type", optional=False, value="CNIC"),
            # Left clear on purpose: ticking it DISABLES the expiry date, and
            # the point here is to populate both columns of the row.
            _tick("Life Time Expiry?", on=False),
            # Both appear only once an ID Type has been chosen.
            Field("ID Value", "text", value=CNIC, optional=True,
                  conditional=True),
            Field("Expiry Date", "date", when=date(2030, 12, 31),
                  optional=True, conditional=True),
        ]),

    TabSpec(
        "Additional Information", LINKAGE,
        name="Additional Information — Employee Type Details",
        grid_heading="EMPLOYEE TYPE DETAILS",
        fields=[
            _pick("Employee Type"),
            _text("Employee Count", "250"),
        ]),

    # ======================================================================
    # BBFS Details — nineteen narrative editors, every one of them filled
    # ======================================================================
    TabSpec(
        "BBFS Details", FORM,
        fields=[
            _rich("ANY WRITE-OFF / WAIVER, RESCHEDULING / RESTRUCTURING "
                  "AVAILED DURING THE LAST THREE YEARS FOR OWN AND SISTER "
                  "CONCERN", _note_for("Write-off / waiver / restructuring")),
            _rich("BUSINESS HANDLED / EFFECTED WITH ALL FINANCIAL INSTITUTIONS "
                  "DURING THE LAST THREE ACCOUNTING YEARS",
                  _note_for("Business handled with financial institutions")),
            _rich("EXISTING LIMITS AND STATUS WITH OTHER BANKS",
                  _note_for("Existing limits with other banks")),
            _rich("DETAILS OF CLEAN FACILITIES CURRENTLY AVAILED",
                  _note_for("Clean facilities currently availed")),
            _rich("AGAINST EXISTING FACILITIES WITH OTHER BANKS (DETAILS OF "
                  "PRIME SECURITIES MORTGAGED / PLEDGED)",
                  _note_for("Prime securities against existing facilities")),
            _rich("AGAINST REQUESTED / FRESH / ADDITIONAL FACILITIES (DETAILS "
                  "OF PRIME SECURITIES MORTGAGED / PLEDGED)",
                  _note_for("Prime securities against requested facilities")),
            _rich("AGAINST EXISTING FACILITIES WITH OTHER BANKS (DETAILS OF "
                  "SECONDARY COLLATERAL MORTGAGED / PLEDGED)",
                  _note_for("Secondary collateral against existing facilities")),
            _rich("AGAINST REQUESTED / FRESH / ADDITIONAL FACILITIES (DETAILS "
                  "OF SECONDARY COLLATERAL MORTGAGED / PLEDGED)",
                  _note_for("Secondary collateral against requested facilities")),
            _rich("CREDIT RATING DETAILS", _note_for("Credit rating")),
            _rich("ASSOCIATED CONCERNS DETAILS",
                  _note_for("Associated concerns")),
            _rich("ASSOCIATED CONCERNS FACILITIES",
                  _note_for("Associated concerns facilities")),
            _rich("PERSONAL GUARANTEES DETAILS",
                  _note_for("Personal guarantees")),
            _rich("DIVIDEND DETAILS", _note_for("Dividend")),
            _rich("SHARE PRICE DETAILS", _note_for("Share price")),
            _rich("NET WORTH DETAILS", _note_for("Net worth")),
            _rich("DETAILS OF ALL OVERDUES (IF OVER 90 DAYS):",
                  _note_for("Overdues over 90 days")),
            _rich("PURPOSE / UTILIZATION OF LOAN:",
                  _note_for("Purpose / utilization of loan")),
            _rich("DETAILS OF PAYMENT SCHEDULE IF TERM LOAN SOUGHT",
                  _note_for("Payment schedule")),
            _rich("LATEST AUDITED FINANCIAL STATEMENTS AS PER REQUIREMENTS OF "
                  "REGULATION R-3 IS ATTACHED",
                  _note_for("Latest audited financial statements")),
        ],
        note="Nineteen free-text editors. Each is given text that names the "
             "box it went into, so the round-trip check can tell one from "
             "another instead of matching any box against any other."),

    # ======================================================================
    # Contact and Address — one grid
    # ======================================================================
    TabSpec(
        "Contact and Address", LINKAGE,
        grid_heading="OBLIGOR ADDRESSES",
        fields=[
            _pick("Address Type", optional=False),
            _pick("Primary/Secondary?", optional=False),
            Field("Address", "text", value=ADDRESS, optional=False),
            _pick("City", optional=False),
            _look("District"),
            _pick("Country", optional=False),
            _text("Phone No.", PHONE),
            _text("Email", EMAIL),
        ]),

    # ======================================================================
    # Limits
    # ======================================================================
    TabSpec(
        "Limits", FORM,
        fields=[
            _pick("Annual Clean up in last year of loan"),
            _text("Overall Capping (PKR)", "5000000"),
            _text("Funded Capping", "3000000"),
            _text("Non-Funded Capping", "2000000"),
            _pick("Borrowers Declaration Regarding Aggregate Clean Exposure "
                  "Obtained?"),
        ]),

    # ======================================================================
    # Corporate Governance
    # ======================================================================
    TabSpec(
        "Corporate Governance", FORM,
        fields=[
            _text("No. of Non-Executive independent Director(s) on the Board",
                  "2", optional=False),
            _text("Total Number of Directors", "7"),
            _text("No. of Board Meetings Held during the Year", "4"),
            _text("Total Net worth & expected income (Individual borrowers only)",
                  "5000000"),
            _pick("Is there a Head of Internal Audit?"),
            _pick("Is Chairman of Audit Committee a Non-Executive Independent "
                  "Director"),
            _pick("Does company provide Quarterly / Half Year Financial "
                  "Statement"),
            _pick("External Auditor Has Satisfactory QCR Rating", optional=False),
            _pick("Obligor timely submitted audited accounts?", optional=False),
            Field("Latest Audited Financial Published?", "date",
                  when=date(2025, 12, 31), optional=False),
            Field("CNIC issuance date", "date", when=date(2015, 1, 15)),
            Field("Date of Last Audit", "date", when=date(2025, 12, 31),
                  optional=False),
            _pick("Is The Auditor QCR Rated?", optional=False),
            _pick("Audit Type (Qualified)", optional=False),
            _rich("Governance", _note_for("Governance")),
        ]),

    TabSpec(
        "Attachments", FORM, skip=True,
        note="This tab is a read-only map of which field on which section "
             "holds a document — it has no file input of its own. Documents "
             "are attached through the paperclip on the individual field they "
             "belong to, which is a different flow."),

    TabSpec(
        "History", FORM, skip=True,
        note="An audit trail written by the application. There is nothing to "
             "enter."),
]



def check_left_empty(f: W.Filler, say,
                     notes: Optional[list] = None) -> list[R.Check]:
    """
    Confirm the fields that were meant to be skipped really are still empty.

    Client Number/CIF is the one that matters: it is the customer's identity in
    the core banking system, and a synthetic obligor carrying a made-up one is
    worse than a blank. A field that is simply absent from the fill list leaves
    no trace in the report, so a reader has no way to tell "deliberately left
    alone" from "quietly forgotten" — this puts it in writing.

    A field this form does not have is an observation rather than a check.
    There is nothing to assert about an application that does not show it, and
    it was not filled — which is what was asked for in the first place.
    """
    checks: list[R.Check] = []
    on_screen = {_norm_value(x) for x in f.field_labels()}
    for names in LEAVE_EMPTY:
        label = next((n for n in names if _norm_value(n) in on_screen), "")
        title = names[0]
        if not label:
            if notes is not None:
                notes.append(R.note(
                    f"No field reading {' / '.join(names[:3])} is on this form, "
                    f"so there was nothing to leave empty. Nothing was typed "
                    f"into it either way.", screen="Basic Information"))
            say(f"  - {title}: not on this form")
            continue
        shown = f.value_of(label)
        if shown:
            checks.append(R.failed(
                f"{title} is left empty",
                expected="an empty field — it is deliberately not filled",
                actual=f"{shown!r}",
                detail="Something put a value in a field this flow is meant to "
                       "leave alone.",
                screen="Basic Information"))
            say(f"  !! {label}: expected empty, holds {shown[:40]!r}")
        else:
            checks.append(R.passed(
                f"{title} is left empty",
                detail=f"{label!r} is on the form and was left blank, as asked."))
            say(f"  {label}: left empty, as asked")
    return checks


def set_field(f: W.Filler, spec: Field, name_override: str = "") -> W.Entry:
    """
    Set one field, trying each of the names it goes by in turn.

    The declared kind is only a hint on these tabs: widgets.set_value reads the
    control out of the DOM and dispatches on what is actually there. That is
    deliberate — the same label is a dropdown on one tab and a Yes/No toggle on
    another, and several fields the discovery pass called textareas are really
    TinyMCE editors. Basic Information still declares its kinds, because there
    the exact control matters and is known.

    Only "no such field" moves on to the next name. A field that IS on the
    screen and refuses a value is reported as itself, rather than being retried
    under a name that does not exist and reported as missing.
    """
    value = name_override or spec.value
    last: Exception = W.FillError(f"No field labelled {spec.label!r} is on this "
                                  f"screen.")
    for name in spec.names:
        try:
            return f.set_value(name, value, when=spec.when)
        except (W.FillError, Exception) as exc:  # noqa: BLE001
            if not _not_on_screen(exc):
                raise
            last = exc
    raise last


def _retry_conditional(f: W.Filler, deferred: list[Field], result: FlowResult,
                       say, step) -> None:
    """
    Come back to the fields that were not rendered when their turn came.

    "Not on the screen yet" is a statement about a moment, not about the form.
    Obligor Id Type is populated FROM Obligor Type and is rendered once that is
    answered — but on a live run it was still absent in the instant after
    Obligor Type was set, so it was logged as conditional, passed over, and
    never revisited. The app then refused the save with "Obligor Id Type is
    Required": a mandatory field left empty by a race with the app's own
    rendering.

    So the deferred ones are tried again with the whole form answered around
    them. One that is STILL not there is genuinely not part of this deployment's
    form, and is recorded as the observation it is rather than as a defect.
    """
    for spec in deferred:
        try:
            e = set_field(f, spec)
        except (W.FillError, Exception) as exc:  # noqa: BLE001
            if _not_on_screen(exc):
                result.notes.append(R.note(
                    f"{spec.label!r} never appeared on Basic Information, even "
                    f"once every other field was answered. The app renders it "
                    f"only in some configurations, so it was not filled.",
                    screen="Basic Information"))
                say(f"  - {spec.label}: still not on the screen — noted")
                continue
            # Present now, and refusing a value. That is a finding like any
            # other; it just took a second pass to be able to say so.
            result.checks.append(R.failed(
                f"Basic Information accepts {spec.label}",
                expected=f"{spec.label} can be set once the field it depends "
                         f"on is answered",
                actual=str(exc),
                detail="The field is on the screen and would not take a value.",
                screen="Basic Information"))
            say(f"    !! {spec.label}: {str(exc)[:110]}")
            continue
        say(f"  {spec.label} = {e.value[:60]!r} (on the second pass, once the "
            f"field it depends on was set)")
        step(f"set {spec.label}", note=e.value)


# The wording a validation message wraps a field's name in. Stripped so what
# is left can be matched against the labels the screen is actually showing —
# "Obligor Id Type is Required" has to resolve to the field called "Obligor Id
# Type" and to nothing else.
#
# Whole CLAUSES are removed, never individual words. Stripping a word list
# would eat parts of the field's own name: "Father/Guardian ID Value is
# Required" would lose its "Value" and then match nothing at all.
_RULE_WORDS = re.compile(
    r"\b(is|are|was|were)?\s*(required|mandatory|invalid|not valid)\b.*$"
    r"|^\s*(please\s+)?(select|enter|provide|choose|specify|input)\b"
    r"|\bmust be\b.*$|\bcannot be\b.*$|\bshould be\b.*$|\bis empty\b.*$",
    re.I)


def _norm_label(text: str) -> str:
    s = (text or "").strip().lower().replace("&", " and ")
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", s)).strip()


def _field_in_message(message: str, labels: list[str]) -> str:
    """
    Which field on the screen a validation message is about, or "".

    Matched by looking for each visible label INSIDE the message rather than by
    parsing the message into a name: the wording varies per control and per
    build — "Obligor Id Type is Required", "Please select Gender" — while the
    field's own label is a fixed string the screen is already showing.

    The LONGEST match wins. Several labels can be contained in one message — a
    form carrying both "Obligor Type" and "Obligor Id Type" matches both
    against "Obligor Id Type is Required" — and the longer one is the field the
    message names; the shorter is a coincidence of substrings. Getting this
    backwards would answer the wrong field and leave the real one empty.

    This lives here rather than in case_flows because BOTH the obligor form and
    the case screens need it, and two copies of a matcher this fiddly would
    drift apart. case_flows imports it.
    """
    body = _norm_label(_RULE_WORDS.sub(" ", message or ""))
    if not body:
        return ""
    best = ""
    for label in labels:
        key = _norm_label(label)
        if key and key in body and len(key) > len(_norm_label(best)):
            best = label
    return best


def _satisfy_required(f: W.Filler, result: FlowResult, say, step,
                      rounds: int = 3) -> None:
    """
    Answer the fields the form's own validation names as required.

    Read-and-act rather than read-and-quote. Everything above works from a
    list — the fields authored in BASIC_INFORMATION — and a mandatory field
    that is not on that list, or is on it under another name, stops the save
    for the entire form. The screen itself knows which ones those are and says
    so in its own messages, so that text is acted on before Save is pressed.

    Three rounds, because answering one field can reveal the next. It stops as
    soon as a round changes nothing, so a rule that no amount of filling can
    satisfy — a range check, a cross-field rule — costs one pass over the
    messages and is then left to be reported as the blocker it is by the
    validation check that follows.

    A chooser gets the app's own first valid option; anything typed gets a
    value shaped for its label. Both are recorded as entries like any other, so
    the round trip verifies them too.
    """
    answered: list[str] = []
    for _ in range(rounds):
        msgs = [m for m in f.unsatisfied()
                if re.search(r"required|mandatory", m, re.I)]
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
            value = (None if kind in ("dropdown", "lookup", "select", "switch")
                     else _auto_required_value(label, kind))
            try:
                e = f.set_value(label, value, when=date(2020, 1, 1))
            except (W.FillError, Exception):  # noqa: BLE001
                continue      # reported by the unmet-rules check that follows
            answered.append(f"{e.label} = {e.value[:40]}")
            say(f"  * {e.label} = {e.value[:50]!r} — the form said it was "
                f"required")
            step(f"set {e.label}", note=f"{e.value[:100]} (form demanded it)")
            progressed = True
        if not progressed:
            break

    if answered:
        result.notes.append(R.note(
            f"{len(answered)} mandatory field(s) were answered from the form's "
            f"own validation messages rather than from this suite's field "
            f"list: " + " | ".join(answered[:8])
            + ". They are worth authoring in BASIC_INFORMATION — the values "
              "here came from the application's own first option or from a "
              "generated one.", screen="Basic Information"))


def _auto_required_value(label: str, kind: str) -> Optional[str]:
    """A value for a typed field the form demanded and nobody authored."""
    low = (label or "").lower()
    if kind == "date":
        return None                      # set_value takes `when` for these
    if "email" in low:
        return EMAIL
    if "fax" in low:
        return FAX
    if any(w in low for w in ("phone", "mobile", "contact no", "cell")):
        return PHONE
    if "cnic" in low or "nic" in low:
        return CNIC
    if "ntn" in low:
        return NTN
    if any(w in low for w in ("amount", "sales", "income", "exposure",
                             "limit", "rate", "number of", "no of")):
        return "1000000"
    return "AUTOMATION"


def fill_tab(s: Session, tab: TabSpec, dry_run: bool, say, step,
             result: FlowResult) -> list[W.Entry]:
    """
    Fill and save ONE pass over an obligor tab.

    A pass is either the fields on the tab itself or a single one of its grids,
    which is why Additional Information appears fourteen times in TAB_SPECS.
    Each grid is its own dialog with its own Save, so each has to succeed or
    fail on its own account; lumping them together would let twelve stored rows
    hide behind one that was refused.

    Unlike Basic Information, a field that will not set here does NOT abandon
    the run. Basic Information is the gate — a partial record must never be
    saved — but these passes are independent of one another, so the useful
    behaviour is to record what could not be set and carry on. Stopping at the
    first awkward dropdown would mean every later grid went unfilled.
    """
    f = W.Filler(session=s, screen=tab.label, group=tab.title)
    shot_name = _safe(tab.title)

    if tab.skip:
        say(f"  {tab.title}: nothing to enter — {tab.note}")
        result.notes.append(R.note(f"{tab.title} was not filled. {tab.note}",
                                   screen=tab.label))
        step(f"skip {tab.title}", note=tab.note[:120])
        return []

    # Clear anything a previous pass left open before switching. Safe here —
    # nothing underneath is being filled — and necessary: a dialog left half
    # closed leaves a backdrop that silently swallows clicks on the next tab,
    # which is what made every Limits field time out.
    f._close_modal()

    # Open the tab. It only exists once the obligor has been saved. Successive
    # passes over the same tab are cheap: open_tab reports it is already open.
    try:
        s.open_tab(tab.label)
    except NavigationError as e:
        result.checks.append(R.failed(
            f"{tab.title} can be opened",
            expected=f"the saved obligor has a '{tab.label}' tab",
            actual=str(e), evidence=[s.screenshot(f"tab-{shot_name}-missing")],
            screen=tab.label))
        step(f"open {tab.label}", R.FAIL, str(e)[:140])
        return []
    step(f"open {tab.label}")

    # A linkage table's fields are in its add-row dialog.
    if tab.kind == LINKAGE:
        where = (f"the '+' under {tab.grid_heading!r}" if tab.grid_heading
                 else f"the '+' at position {tab.grid}")
        if not f.add_row(tab.grid, heading=tab.grid_heading):
            result.checks.append(R.failed(
                f"{tab.title} offers an add-row form",
                expected=f"{where} opens a form",
                actual="no dialog opened",
                detail="Without the dialog there is nowhere to enter a row. "
                       "Either the grid is not on this tab any more, or its "
                       "section is collapsed so the '+' is not rendered.",
                evidence=[s.screenshot(f"tab-{shot_name}-noadd")],
                screen=tab.label))
            step(f"add row on {tab.title}", R.FAIL, "no dialog opened")
            return []
        step(f"open the add form on {tab.title}")

    failed_required = 0
    skipped = 0
    for spec in tab.fields:
        try:
            e = set_field(f, spec)
            say(f"    {spec.label} = {e.value[:60]!r}")
        # W.FillError is the expected way a field refuses. Anything else — a
        # raw Playwright timeout on an odd control, say — must be caught too:
        # one awkward field killing the whole run leaves every later pass
        # unchecked, which is a far worse outcome than one recorded blocker.
        except (W.FillError, Exception) as exc:  # noqa: BLE001
            # A conditional field that simply is not rendered is the form
            # behaving correctly — ID Value appears only once an ID Type has
            # been chosen — so it is logged rather than counted against the tab.
            if spec.conditional and _not_on_screen(exc):
                skipped += 1
                say(f"    - {spec.label}: not rendered here")
                continue
            # A field this tab does not have under the name authored here is
            # an observation, not a defect: it may be named differently in
            # this deployment, or not be configured at all. The form's own
            # validation, read below, is what decides whether it mattered.
            if _not_on_screen(exc):
                skipped += 1
                result.notes.append(R.note(
                    f"{spec.label!r} is not on {tab.title} under that name, so "
                    f"it was not filled. It may have been renamed in this "
                    f"deployment, or not be configured here.",
                    screen=tab.label))
                say(f"    - {spec.label}: not on this tab — noted")
                continue
            if not spec.optional:
                failed_required += 1
            result.checks.append(R.failed(
                f"{tab.title}: {spec.label} can be set",
                expected=f"{spec.label} accepts a value",
                actual=str(exc),
                detail="The field is on the tab and would not take a value."
                       + ("" if spec.optional else
                          " It is marked mandatory, so the tab may not save."),
                screen=tab.label))
            say(f"    !! {spec.label}: {str(exc)[:110]}")

    say(f"    {len(f.entries)} of {len(tab.fields)} field(s) set"
        + (f", {skipped} not rendered" if skipped else ""))
    s.screenshot(f"tab-{shot_name}-filled")

    result.checks.extend(check_editors_distinct(f, tab.title, tab.label, s))

    # ---- validate BEFORE saving --------------------------------------
    #
    # This is the check that was missing. A pass whose fields all accepted their
    # values can still refuse to save: the phone boxes here validate on LENGTH,
    # so an 11-digit number goes in cleanly and then fails an inline rule. Save
    # then does nothing at all — no error toast, no navigation — and the run
    # would happily report success on a record that was never written.
    outstanding = f.unsatisfied()
    if outstanding:
        result.checks.append(R.failed(
            f"{tab.title} has no unmet field rules before saving",
            expected="no validation message left on the form",
            actual=f"{len(outstanding)} outstanding: "
                   + " | ".join(outstanding[:5]),
            detail="The form will refuse to save while these stand, so the "
                   "values below were entered but not stored.",
            evidence=[s.screenshot(f"tab-{shot_name}-invalid")],
            screen=tab.label))
        say(f"    !! {len(outstanding)} unmet rule(s): "
            f"{' | '.join(outstanding[:3])}")
    else:
        result.checks.append(R.passed(
            f"{tab.title} has no unmet field rules before saving",
            detail=f"{len(f.entries)} field(s) filled, form reports no "
                   f"outstanding validation."))

    if dry_run:
        say(f"  {tab.title}: dry run — not saving")
        if tab.kind == LINKAGE:
            f._close_modal()
        result.notes.append(R.note(
            f"{tab.title}: dry run — {len(f.entries)} field(s) were filled and "
            f"abandoned without saving.", screen=tab.label))
        return f.entries

    # For a linkage pass, the row count is the honest test of whether Save
    # worked — far better than trusting the absence of an error toast. It counts
    # every grid on the tab, which is what makes it work on Additional
    # Information: whichever of the thirteen gained the row, the total goes up.
    rows_before = _grid_rows(s) if tab.kind == LINKAGE else -1

    # ---- save --------------------------------------------------------
    try:
        f.commit(tab.save_label)
        step(f"save {tab.title}")
    except (W.FillError, W.WriteRefused) as exc:
        result.checks.append(R.failed(
            f"{tab.title} is saved",
            expected=f"a {tab.save_label} button on {tab.title}",
            actual=str(exc),
            evidence=[s.screenshot(f"tab-{shot_name}-nosave")],
            screen=tab.label))
        step(f"save {tab.title}", R.FAIL, str(exc)[:140])
        return f.entries

    msgs = f.messages()
    still_invalid = f.unsatisfied()
    # A configured server rule refuses with HTTP 417 and shows nothing on the
    # page beyond a badge, so this is the only way to learn why.
    refused = _refused_rules(s)
    shot = s.screenshot(f"tab-{shot_name}-saved")

    if msgs["bad"] or refused:
        result.checks.append(R.failed(
            f"{tab.title} is saved",
            expected="the app confirms it was saved",
            actual=" | ".join((msgs["bad"] + refused)[:3]),
            detail="The application refused the save. A rule marked 'blocks "
                   "the save' means the values entered have to change."
            if refused else "",
            evidence=[shot], screen=tab.label))
        for r in refused:
            say(f"    !! refused: {r}")
        f._close_modal()
        return f.entries

    if still_invalid:
        # The dialog is still open with its rules unmet: Save was pressed and
        # refused. Silently the first time round — which is exactly the failure
        # the phone-length rule caused.
        result.checks.append(R.failed(
            f"{tab.title} is saved",
            expected="Save stores the row and closes the form",
            actual=f"the form is still open with {len(still_invalid)} unmet "
                   f"rule(s): " + " | ".join(still_invalid[:4]),
            detail="Save was refused, so nothing was written for this pass.",
            evidence=[shot], screen=tab.label))
        f._close_modal()
        return f.entries

    if tab.kind == LINKAGE:
        f._close_modal()
        rows_after = _grid_rows(s)
        if rows_after > rows_before:
            result.checks.append(R.passed(
                f"{tab.title} is saved",
                detail=f"The tab's grids went from {rows_before} to "
                       f"{rows_after} row(s), so the row was stored."))
        else:
            result.checks.append(R.failed(
                f"{tab.title} is saved",
                expected=f"one more row than the {rows_before} before saving",
                actual=f"still {rows_after} row(s)",
                detail="Save raised no error but no grid gained a row, so "
                       "nothing was stored.",
                evidence=[s.screenshot(f"tab-{shot_name}-norow")],
                screen=tab.label))
        return f.entries

    if not f.entries:
        # Save was pressed where nothing was actually entered. It reports
        # success because an empty form is valid — but calling that "saved" is a
        # hollow pass, and it hid a run where every Limits field had timed out
        # behind a leftover dialog backdrop.
        result.checks.append(R.failed(
            f"{tab.title} is saved",
            expected=f"the {len(tab.fields)} field(s) on this tab entered and "
                     f"stored",
            actual="not one field could be set, so nothing was stored",
            detail="Save reported no error because an empty form is valid. "
                   "See the field failures above for why nothing went in.",
            evidence=[shot], screen=tab.label))
    elif failed_required:
        result.checks.append(R.failed(
            f"{tab.title} is saved",
            expected="every mandatory field entered before Save",
            actual=f"{failed_required} mandatory field(s) could not be set",
            detail="Save raised no error, but the tab was committed with "
                   "mandatory fields missing. The round trip below says what "
                   "actually reached the record.",
            evidence=[shot], screen=tab.label))
    else:
        result.checks.append(R.passed(
            f"{tab.title} is saved",
            detail=f"{len(f.entries)} field(s) entered and saved"
                   + (f". {' | '.join(msgs['ok'][:2])}" if msgs["ok"] else ".")))

    f._close_modal()
    return f.entries


def check_editors_distinct(f: W.Filler, title: str, screen: str,
                           s: Session) -> list[R.Check]:
    """
    Confirm each narrative box on this tab got its OWN text.

    BBFS Details is nineteen TinyMCE editors on one screen, and the failure
    that made this necessary is a quiet one: every field reported success while
    the text only ever reached the first box. Reading a value back from the
    control that was written to cannot catch that — it reads back the box that
    was written to, which is exactly the box holding the text.

    So the identity of the editor is compared instead. Two labels resolving to
    one editor is the bug, and it is a FAIL rather than a note: it means the
    other seventeen boxes are empty on a record the report would otherwise call
    complete.
    """
    seen: dict[str, str] = {}
    clashes: list[str] = []
    for e in f.entries:
        if not e.target:
            continue
        if e.target in seen:
            clashes.append(f"{e.label!r} and {seen[e.target]!r} both wrote to "
                           f"editor {e.target}")
        else:
            seen[e.target] = e.label

    rich = [e for e in f.entries if e.kind == "rich-text"]
    if not rich:
        return []
    if clashes:
        return [R.failed(
            f"{title}: each narrative box gets its own text",
            expected=f"{len(rich)} boxes, {len(rich)} separate editors",
            actual=f"{len(seen)} distinct editor(s) — "
                   + " | ".join(clashes[:4]),
            detail="Two field labels resolved to the same editor, so one box "
                   "holds the text and the others are empty. Everything those "
                   "labels recorded as entered is wrong.",
            evidence=[s.screenshot(f"tab-{_safe(title)}-editor-clash")],
            screen=screen)]
    return [R.passed(
        f"{title}: each narrative box gets its own text",
        detail=f"{len(rich)} narrative box(es) filled, each a distinct editor, "
               f"each holding the text that names it.")]


def _safe(name: str) -> str:
    """A short filename-safe stem, so two passes over one tab do not overwrite
    each other's screenshots."""
    return re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-")[:40]


def _grid_rows(s: Session) -> int:
    """Data rows across the visible grids of the routed content."""
    try:
        return int(s.page.evaluate(
            """() => {
                const root = document.querySelector('[data-crawl-root]') || document.body;
                const vis = (el) => el.offsetParent !== null || el.getClientRects().length;
                let n = 0;
                for (const t of root.querySelectorAll('table')) {
                    if (!vis(t)) continue;
                    for (const r of t.querySelectorAll('tbody tr')) {
                        if (!vis(r)) continue;
                        const txt = (r.textContent || '').trim();
                        if (txt && !/no (data|record|result)/i.test(txt)) n++;
                    }
                }
                return n;
            }"""))
    except Exception:  # noqa: BLE001
        return -1


def open_obligor(s: Session, needle: str, say, max_pages: int = 16) -> str:
    """
    Open an existing obligor from All Obligors by name or customer id.

    Pages the grid rather than typing in its search box, consistent with how
    every other record lookup here works. Note the grid does NOT show the
    customer id in the form the Customer Profile does, so searching by NAME is
    the reliable route.
    """
    nav = cr.navigate_in_app(
        s.page,
        {"label": "All Obligors", "path": _OBLIGOR_LIST_PATH,
         "href": _OBLIGOR_LIST_PATH,
         "url": crawler_config.BASE_URL.rstrip("/") + _OBLIGOR_LIST_PATH},
        s.recorder)
    if not nav.get("ok"):
        raise NavigationError("Could not open All Obligors.", environmental=True)
    cr.wait_until_settled(s.page, s.recorder, timeout_ms=20000, stable_polls=2)
    cr.stamp_content_root(s.page)

    want = needle.strip().lower()
    scanned = 0
    for page_no in range(1, max_pages + 1):
        rows = cr.collect_row_openers(s.page, max_rows=400)
        scanned += len(rows)
        m = next((r for r in rows
                  if want in (r.get("row_preview") or "").lower()), None)
        if m:
            if not cr._click_stamped(s.page, "data-crawl-row", m["index"],
                                     timeout=8000):
                raise NavigationError(f"{needle!r} was found but would not open.")
            cr.wait_until_settled(s.page, s.recorder, timeout_ms=25000,
                                  stable_polls=2)
            cr.stamp_content_root(s.page)
            say(f"  opened on page {page_no}: "
                f"{(m.get('row_preview') or '')[:90]}")
            return m.get("row_preview") or ""
        if not s._next_grid_page():
            break
    raise NavigationError(
        f"{needle!r} is not in All Obligors. Looked at {scanned} row(s) across "
        f"{page_no} page(s).", environmental=True)


def fill_obligor_tabs(needle: str, headless: Optional[bool] = None,
                      dry_run: bool = True,
                      progress: Optional[Callable] = None,
                      run_id: str = "", emit=None) -> FlowResult:
    """
    Fill the nine tabs of an obligor that already exists.

    Exists as its own entry point for a practical reason: those tabs are locked
    until an obligor has been saved, so a dry run of the create flow can never
    reach them. Pointing this at an obligor created by an earlier run is the
    only way to exercise the tab filling without creating another record every
    time.
    """
    reason = W.assert_writable()
    run_id = run_id or new_run_id("tabs" if not dry_run else "tabsdry")
    _emit = emit or (lambda e: None)

    res = FlowResult(run_id=run_id, started_at=_now(), dry_run=dry_run,
                     obligor_name=needle,
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
    say(f"{'DRY RUN — nothing will be saved' if dry_run else 'LIVE — tabs will be saved'}")
    say(f"Obligor: {needle}")

    try:
        with Session(run_id, mode=settings.TRANSACT, headless=headless,
                     emit=_emit) as s:
            s.login()
            step("sign in")
            row = open_obligor(s, needle, say)
            res.checks.append(R.passed("The obligor can be opened",
                                       detail=row[:140]))
            step("open the obligor", note=row[:120])
            s.screenshot("01-obligor-open")

            for tab in TAB_SPECS:
                say(f"  {tab.title} …")
                entries = fill_tab(s, tab, dry_run, say, step, res)
                res.entries.extend(e.as_dict() for e in entries)
            say(f"  {len(res.entries)} field(s) entered in total")
            return _finish(res, s, say)

    except W.WriteRefused as e:
        res.error_reason = str(e)
    except NavigationError as e:
        res.error_reason = str(e)
    except Exception as e:  # noqa: BLE001
        res.error_reason = str(e)[:400]

    res.finished_at = _now()
    _persist(res)
    return res


OBLIGOR_DETAILS = "Obligor Details (BIR)"


def fill_case_obligor_details(case_id: str, headless: Optional[bool] = None,
                              dry_run: bool = True,
                              verify: bool = True,
                              progress: Optional[Callable] = None,
                              run_id: str = "", emit=None) -> FlowResult:
    """
    Finish the obligor on a case that already exists.

    The second half of the pair. `create_obligor` fills Basic Information and
    stops; this takes the case id that came out of it — or any other case in My
    Bucket — opens Obligor Details (BIR) inside that case, and fills every
    remaining tab and every one of their eighteen tables.

    Reached through My Bucket rather than through All Obligors, deliberately.
    The case carries its OWN copy of the obligor, and that copy is what an
    approver reads; filling the customer record instead would be writing to a
    different place from the one the round trip then checks.

    Then it leaves, comes back in through My Bucket a second time, and compares
    every value it entered against what the case shows. Coming back in through
    the grid is what makes it a real check — a value the app dropped or
    reformatted is invisible on the form that is still holding it in memory.
    """
    reason = W.assert_writable()          # before a browser is even launched
    case_id = (case_id or settings.CASE_ID).strip()
    run_id = run_id or new_run_id("details" if not dry_run else "detailsdry")
    _emit = emit or (lambda e: None)

    res = FlowResult(
        run_id=run_id, started_at=_now(), dry_run=dry_run, request_id=case_id,
        target_key="case.obligor_details",
        target_title=f"Fill Obligor Details (BIR) on case {case_id}",
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
    say("DRY RUN — nothing will be saved" if dry_run
        else f"LIVE — data will be written to case {case_id}")
    say(f"Case: {case_id}")

    if not case_id:
        res.error_reason = ("No case id was given, so there is no case to "
                            "open. Set one in the sidebar or pass --case-id.")
        return _finish_nosession(res, say)

    try:
        with Session(run_id, mode=settings.TRANSACT, headless=headless,
                     emit=_emit) as s:
            s.login()
            step("sign in")

            if not _open_obligor_details(s, res, case_id, say, step, "01"):
                return _finish(res, s, say)

            say(f"Filling the tabs — {len(TAB_SPECS)} passes …")
            planned = [t.title for t in TAB_SPECS]
            _emit({"kind": "start", "screens": planned})
            for i, tab in enumerate(TAB_SPECS, start=1):
                say(f"  {tab.title} …")
                _emit({"kind": "screen_start", "index": i,
                       "total": len(TAB_SPECS), "screen": tab.title})
                before = len(res.checks)
                # One awkward tab must not take the other twenty-four with it.
                # They are independent passes over independent grids, and a run
                # that abandons BBFS Details because a lookup on Sector And
                # Industry misbehaved reports nothing about nineteen boxes that
                # may have been perfectly fine.
                try:
                    entries = fill_tab(s, tab, dry_run, say, step, res)
                    res.entries.extend(e.as_dict() for e in entries)
                except Exception as exc:      # noqa: BLE001
                    res.checks.append(R.failed(
                        f"{tab.title} could be filled",
                        expected="the tab fills and saves without the run "
                                 "falling over",
                        actual=str(exc)[:300],
                        evidence=[s.screenshot(f"tab-{_safe(tab.title)}-error")],
                        screen=tab.label))
                    step(f"fill {tab.title}", R.FAIL, str(exc)[:150])
                mine = res.checks[before:]
                _emit({"kind": "screen_done", "index": i,
                       "total": len(TAB_SPECS), "screen": tab.title,
                       "status": _worst(mine), "note": f"{len(mine)} check(s)"})
            say(f"  {len(res.entries)} field(s) entered in total")

            # ---- the round trip ------------------------------------------
            #
            # Each of these is a reason the comparison did not happen, not a
            # verdict on the application, so none of them records a check.
            if dry_run:
                res.notes.append(R.note(
                    "Dry run: nothing was saved, so the entered values were "
                    "not read back. Re-run without the dry-run option to "
                    "verify them."))
                return _finish(res, s, say)
            if not verify:
                res.notes.append(R.note(
                    "Verification was switched off for this run, so nothing "
                    "was read back."))
                return _finish(res, s, say)
            if not res.entries:
                res.notes.append(R.note(
                    "Nothing was entered, so there was nothing to read back."))
                return _finish(res, s, say)

            say("Re-opening the case from My Bucket to verify …")
            if not _open_obligor_details(s, res, case_id, say, step, "50",
                                         again=True):
                return _finish(res, s, say)
            res.checks.extend(
                verify_entered_values(s, res.entries, say, res.notes))
            return _finish(res, s, say)

    except W.WriteRefused as e:
        res.error_reason = str(e)
    except NavigationError as e:
        res.error_reason = str(e)
    except Exception as e:  # noqa: BLE001 - environment failure, not a defect
        res.error_reason = str(e)[:400]

    return _finish_nosession(res, say)


def _open_obligor_details(s: Session, res: FlowResult, case_id: str, say, step,
                          shot_prefix: str, again: bool = False) -> bool:
    """
    My Bucket -> the case -> Obligor Details (BIR).

    Used twice in a run: once to fill, and once more afterwards to read back.
    The second trip goes all the way out to the grid and in again on purpose —
    that is what makes the comparison a round trip rather than a re-reading of
    a form the browser never left.
    """
    when = " again" if again else ""
    try:
        row = find_case(s, case_id, say)
    except NavigationError as e:
        # The case named for this run is not in the grid. That is the run
        # having nothing to work on rather than the application misbehaving,
        # so it ends the run as an ERROR and asserts nothing.
        res.error_reason = str(e)
        s.screenshot(f"{shot_prefix}-case-not-found")
        step(f"open the case from My Bucket{when}", R.ERROR, str(e)[:150])
        return False
    res.checks.append(R.passed(f"The case can be opened from My Bucket{when}",
                               detail=f"Found and opened: {row[:120]}"))
    step(f"open the case from My Bucket{when}", note=row[:120])
    if not res.obligor_name:
        res.obligor_name = row[:120]

    if not wait_for_case_menu(s, say):
        res.error_reason = ("The case opened but its sidebar never populated, "
                            "so Obligor Details (BIR) could not be reached.")
        s.screenshot(f"{shot_prefix}-no-case-menu")
        step("wait for the case menu", R.ERROR, res.error_reason[:150])
        return False
    s.screenshot(f"{shot_prefix}-case-open")

    try:
        note = s._step_context_menu(
            NavStep(kind=CONTEXT_MENU, label=OBLIGOR_DETAILS))
    except NavigationError as e:
        res.checks.append(R.failed(
            f"{OBLIGOR_DETAILS} can be opened in the case",
            expected=f"the case menu offers {OBLIGOR_DETAILS}",
            actual=str(e),
            evidence=[s.screenshot(f"{shot_prefix}-no-obligor-details")]))
        step(f"open {OBLIGOR_DETAILS}", R.FAIL, str(e)[:150])
        return False
    step(f"open {OBLIGOR_DETAILS}{when}", note=note)
    s.screenshot(f"{shot_prefix}-obligor-details")
    return True


def _worst(checks: list[R.Check]) -> str:
    """The status a tab should show as: any failure dominates."""
    return R.FAIL if any(c.status == R.FAIL for c in checks) else R.PASS


# --------------------------------------------------------------------------
# Raise Transaction -> a credit case
# --------------------------------------------------------------------------

# Request IDs look like NNNNN-YYYY (52224-2026). The case header shows one once
# the transaction has been raised, which is how the case is found again in
# My Bucket without having to guess.
_REQUEST_ID = re.compile(r"\b(\d{4,6}-\d{4})\b")


def _header_text(s: Session) -> str:
    """
    The case banner: obligor name, group, and 'NNNNN-YYYY - TYPE (STATUS)'.

    Looks for the SHORTEST element that carries a request ID rather than the
    first container that happens to contain one. The navbar wraps both the
    banner and the user's profile menu, so taking the first match returned the
    signed-in user's name and profile list and no ID at all.
    """
    try:
        return s.page.evaluate(
            """() => {
                const clean = (x) => (x || '').trim().replace(/\\s+/g, ' ');
                const ID = /\\b\\d{4,6}-\\d{4}\\b/;
                let best = '';
                for (const el of document.querySelectorAll(
                        'app-navbar *, .header-navbar *, .content-header *, '
                        + 'h1, h2, h3, h4, span, div, a')) {
                    if (el.children.length > 3) continue;
                    const t = clean(el.innerText || el.textContent);
                    if (!t || t.length > 200 || !ID.test(t)) continue;
                    if (!best || t.length < best.length) best = t;
                }
                return best || clean(document.body.innerText).slice(0, 400);
            }""")
    except Exception:  # noqa: BLE001
        return ""


def raise_transaction(s: Session, f: W.Filler, say, step,
                      request_type: str = REQUEST_TYPE) -> str:
    """
    Turn the saved obligor into a credit case.

    Returns the request ID the app assigns, or "" when it cannot be read from
    the page — in which case the caller falls back to finding the case by
    obligor name.
    """
    btns = cr.collect_action_buttons(s.page)
    raise_btn = next((b for b in btns
                      if "raise" in (b["label"] or "").lower()
                      and "transaction" in (b["label"] or "").lower()), None)
    if raise_btn is None:
        raise NavigationError(
            "There is no 'Raise Transaction' button on the saved obligor. "
            "Available: " + ", ".join(b["label"] for b in btns[:10]))
    if not cr._click_stamped(s.page, "data-crawl-action", raise_btn["index"],
                             timeout=8000):
        raise NavigationError("'Raise Transaction' could not be clicked.")
    s.page.wait_for_timeout(1500)
    step("click Raise Transaction")
    s.screenshot("07-request-type-dialog")

    if not s.page.locator(".modal.show").count():
        raise NavigationError(
            "'Raise Transaction' did not open the Request Type dialog.")

    # Raising a transaction is a SEQUENCE of dialogs, not one. Request Type is
    # followed by "Profiles — Select Profile for Initiation", and each has its
    # own Proceed. Stopping after the first one looks like it worked — no error
    # is shown — but no case is created, which is exactly what happened before
    # this loop existed. So: keep answering dialogs until none is left.
    dialog_titles: list[str] = []
    for round_no in range(1, 5):
        dialog = s.page.locator(".modal.show").first
        if not dialog.count():
            break
        title = ""
        try:
            t = dialog.locator(".modal-title, h4, h5").first
            if t.count():
                title = (t.inner_text() or "").strip()
        except Exception:  # noqa: BLE001
            pass
        dialog_titles.append(title or f"dialog {round_no}")

        # The first dialog needs the named request type; later ones take the
        # preferred profile if it is offered, else the first valid option.
        if round_no == 1:
            chosen = f.choose_in_dialog(request_type, label="Request Type")
        else:
            chosen = None
            for pref in PROFILE_PREFERENCE:
                try:
                    chosen = f.choose_in_dialog(pref, label=title or "Profile")
                    break
                except W.FillError:
                    continue
            if chosen is None:
                chosen = f.choose_in_dialog(None, label=title or "Profile")
        say(f"  {title or 'dialog'}: {chosen.value!r}")
        step(f"answer '{title or 'dialog'}'", note=chosen.value)

        # Proceed is what actually creates the case. It is in the crawler's
        # destructive denylist for good reason; widgets.commit allows it only
        # because this flow names it and the host is allowlisted.
        f.commit("Proceed")
        step("click Proceed")
        cr.wait_until_settled(s.page, s.recorder, timeout_ms=30000,
                              stable_polls=2)
        s.page.wait_for_timeout(2000)
        s.screenshot(f"08-after-proceed-{round_no}")

    say(f"  dialogs answered: {', '.join(dialog_titles) or 'none'}")
    header = _header_text(s)
    m = _REQUEST_ID.search(header)
    rid = m.group(1) if m else ""
    say(f"  case header: {header[:160]!r}")
    return rid


# --------------------------------------------------------------------------
# Verify: re-read the obligor THROUGH the credit case
# --------------------------------------------------------------------------

def _open_row_matching(s: Session, needle: str) -> tuple[Optional[str], int]:
    """
    Click the row on THIS grid page whose text contains `needle`.

    Returns (row text, rows examined), with the row text None when no row on
    this page matches. Shared by the search path and the paging path so both
    open a record exactly the same way.
    """
    want = needle.strip().lower()
    openers = cr.collect_row_openers(s.page, max_rows=400)
    match = next((r for r in openers
                  if want in (r.get("row_preview") or "").lower()), None)
    if match is None:
        return None, len(openers)
    if not cr._click_stamped(s.page, "data-crawl-row", match["index"],
                             timeout=8000):
        raise NavigationError(f"The case for {needle!r} could not be opened.")
    cr.wait_until_settled(s.page, s.recorder, timeout_ms=25000, stable_polls=2)
    cr.stamp_content_root(s.page)
    return (match.get("row_preview") or ""), len(openers)


def _search_bucket(s: Session, needle: str, say) -> bool:
    """
    Ask My Bucket's own search box for this record.

    False — with a reason logged — when there was no search box, or when
    typing is not permitted on this host. Either way the caller pages
    instead, so a refusal costs nothing but time.
    """
    try:
        found = W.Filler(session=s, screen="My Bucket").search_grid(needle)
    except W.WriteRefused:
        say("  the grid's search box needs data entry to be permitted on this "
            "host; paging through the grid instead")
        return False
    except W.FillError as exc:
        say(f"  the grid's search box would not take {needle!r} "
            f"({str(exc)[:80]}); paging through the grid instead")
        return False
    if not found:
        say("  My Bucket has no search box on this build; paging instead")
    return found


def find_case(s: Session, needle: str, say, max_pages: int = 12) -> str:
    """
    Find and open a case in My Bucket by request ID or obligor name.

    The grid's own SEARCH box is asked first. A case can be on any page of
    the bucket, and paging to it is both slow and CAPPED — twelve pages, after
    which a record that exists is reported as missing, which is exactly what
    happened to a case sitting on page 13. Searching asks the server, so it
    finds the record wherever it is.

    Paging is kept as the fallback and it is not vestigial. It is what runs
    when the build has no search box, when typing is not permitted on the host
    (a read-only re-verification), and when a search comes back empty on a
    grid that turns out to hold the record anyway. So this is never worse at
    finding a case than paging alone was.

    Returns the row text that matched.
    """
    nav = cr.navigate_in_app(
        s.page,
        {"label": "My Bucket", "path": _BUCKET_PATH, "href": _BUCKET_PATH,
         "url": crawler_config.BASE_URL.rstrip("/") + _BUCKET_PATH},
        s.recorder)
    if not nav.get("ok"):
        raise NavigationError("Could not open My Bucket.", environmental=True)
    # Essential, not cosmetic: collect_row_openers scopes itself to
    # [data-crawl-root], so without re-stamping after the navigation it reads
    # the PREVIOUS screen's root and finds zero rows in a grid full of them.
    cr.wait_until_settled(s.page, s.recorder, timeout_ms=20000, stable_polls=2)
    cr.stamp_content_root(s.page)

    searched = False
    if _search_bucket(s, needle, say):
        searched = True
        row, seen = _open_row_matching(s, needle)
        if row is not None:
            say(f"  found by searching My Bucket for {needle!r}")
            return row
        say(f"  the search returned {seen} row(s), none matching {needle!r} — "
            f"paging through the grid instead")
        # Clear the filter, or the paging below walks the filtered grid.
        _search_bucket(s, "", say)

    scanned, page_no = 0, 0
    for page_no in range(1, max_pages + 1):
        row, seen = _open_row_matching(s, needle)
        scanned += seen
        if row is not None:
            say(f"  found on page {page_no} after {scanned} row(s)")
            return row
        # Advance the grid.
        if not s._next_grid_page():
            break

    # Nought rows is not a grid that lacks this case — it is a grid that was
    # never read. Saying "the case is not there" about an empty screen sends
    # somebody to look for a missing transaction when what actually happened
    # was that the browser was somewhere else entirely; the rating model opens
    # in a tab of its own, and a session still driving that tab answers this
    # question with a blank page. The two deserve different sentences.
    #
    # A search that ran still counts as having looked, so this only fires when
    # neither route saw a single row.
    if scanned == 0 and not searched:
        raise NavigationError(
            f"My Bucket showed no rows at all, so nothing was searched for "
            f"{needle!r}. The screen reached was {s.page.url} — an empty grid, "
            f"a bucket that had not loaded, or a page that is not My Bucket. "
            f"This says nothing about whether the case exists.",
            environmental=True)
    raise NavigationError(
        f"{needle!r} is not in My Bucket. "
        + (f"The grid's search box was asked for it directly, and then "
           f"{scanned} row(s) across {page_no} page(s) were checked. "
           if searched else
           f"Looked at {scanned} row(s) across {page_no} page(s). ")
        + "A newly raised case should appear immediately, so this suggests "
          "the transaction did not complete.",
        environmental=True)


def verify_case(request_id: str, entries: list[dict],
                headless: Optional[bool] = None,
                progress: Optional[Callable] = None,
                run_id: str = "", emit=None) -> FlowResult:
    """
    Re-run ONLY the verification leg against a case that already exists.

    Read-only: it opens the case, walks Obligor Details (BIR) and compares.
    Exists so the comparison can be corrected and re-checked without creating
    another obligor and another case each time — the full chain takes minutes
    and leaves two permanent records behind.

    `entries` is what a previous run recorded; flow.json from that run holds it.
    """
    run_id = run_id or new_run_id("verify-case")
    _emit = emit or (lambda e: None)
    res = FlowResult(run_id=run_id, started_at=_now(), dry_run=True,
                     request_id=request_id, entries=list(entries),
                     artifacts_dir=os.path.join(settings.ARTIFACTS_DIR, run_id))

    def say(msg: str) -> None:
        if progress:
            progress(msg)
        _emit({"kind": "log", "text": msg})

    def step(text: str, status: str = R.PASS, note: str = "") -> None:
        res.steps.append({"index": len(res.steps) + 1, "text": text,
                          "status": status, "note": note})

    say(f"Verifying case {request_id} against {len(entries)} recorded value(s)")
    say("This leg is read-only — nothing is written.")

    try:
        with Session(run_id, mode=settings.VERIFY, headless=headless,
                     emit=_emit) as s:
            s.login()
            step("sign in")
            row = find_case(s, request_id, say)
            res.checks.append(R.passed("The case can be opened",
                                       detail=row[:140]))
            step("open the case", note=row[:120])

            if not wait_for_case_menu(s, say):
                res.error_reason = ("The case opened but its sidebar never "
                                    "populated.")
                return _finish(res, s, say)
            try:
                note = s._step_context_menu(
                    NavStep(kind=CONTEXT_MENU, label="Obligor Details (BIR)"))
                step("open Obligor Details (BIR)", note=note)
            except NavigationError as e:
                res.checks.append(R.failed(
                    "Obligor Details (BIR) can be opened in the case",
                    expected="the case menu offers Obligor Details (BIR)",
                    actual=str(e)))
                return _finish(res, s, say)

            res.checks.extend(
                verify_entered_values(s, res.entries, say, res.notes))
            return _finish(res, s, say)

    except NavigationError as e:
        res.error_reason = str(e)
    except Exception as e:  # noqa: BLE001
        res.error_reason = str(e)[:400]

    res.finished_at = _now()
    _persist(res)
    return res


def wait_for_case_menu(s: Session, say, timeout_s: int = 45) -> bool:
    """
    Wait until the opened case's sidebar actually has entries.

    Opening a case settles the route before the case menu's own request comes
    back, so reading the menu immediately finds nothing and concludes that
    Obligor Details (BIR) does not exist. That reported a missing screen on a
    case that had it, and stopped the round trip before it started.

    The wait was 25 seconds and is now 45. Re-opening the case after a heavy
    screen is slower than opening it fresh: a Financials run leaves several
    450-row statements behind, and its round trip was blocking with "the
    sidebar never populated" on a case whose sidebar was simply still coming.
    Being more patient can only turn a spurious block into a real answer —
    a menu that genuinely never arrives is still reported, just later.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            items = cr.collect_context_menu(s.page, crawler_config.BASE_URL,
                                            s.top_level_paths)
        except Exception:  # noqa: BLE001
            items = []
        if items:
            say(f"  case menu has {len(items)} entr(ies)")
            return True
        s.page.wait_for_timeout(1500)
        try:
            cr.stamp_content_root(s.page)
        except Exception:  # noqa: BLE001
            pass
    return False


def verify_entered_values(s: Session, entries: list[dict], say,
                          notes: Optional[list] = None) -> list[R.Check]:
    """
    Compare Obligor Details (BIR) inside the case against what was typed.

    This is the whole point of the exercise. The values are re-read through a
    DIFFERENT route than they were entered — the credit case's own copy of the
    obligor, not the customer record that was filled in — so a value the app
    dropped, truncated, re-formatted or failed to carry across shows up as a
    failed check instead of being assumed good because Save raised no error.

    Comparison is deliberately forgiving about presentation and strict about
    content: a lookup entered as "AIBG  - Aitemaad Islamic Banking Group" may
    render as "AIBG - Aitemaad Islamic Banking Group", and a date entered as
    01/01/2020 may render as "January 1st, 2020". Neither is a defect. A
    different VALUE is.
    """
    checks: list[R.Check] = []

    # Entries carry the tab they were entered on, so the comparison follows the
    # same route: open that tab inside the case, then compare its values.
    by_screen: dict[str, list[dict]] = {}
    for e in entries:
        by_screen.setdefault(e.get("screen") or "Basic Information", []).append(e)

    for screen, items in by_screen.items():
        first = len(checks)
        f = W.Filler(session=s, screen=screen)
        if screen != "Basic Information":
            try:
                s.open_tab(screen)
            except NavigationError as exc:
                # A tab this run filled and saved, that the case then will not
                # open, is a finding. Reported once for the tab rather than
                # once per value: one unreachable tab is one fault, and
                # repeating it per field buries it.
                checks.append(R.failed(
                    f"{screen} can be re-opened to verify what was entered",
                    expected=f"the case offers the {screen!r} tab that was "
                             f"filled and saved earlier in this run",
                    actual=str(exc),
                    detail=f"{len(items)} value(s) were entered here and none "
                           f"of them could be read back.",
                    screen=screen))
                continue
        shot = s.screenshot(f"verify-{screen[:26]}")
        # Everything the tab is showing, for the values that live in a grid
        # rather than in a labelled field.
        page_text = _screen_text(s)

        # A summary grid shows only a few of a row's columns. Email, Phone,
        # Address and the rest of a shareholder live in the row's own detail,
        # so their absence from the summary is NOT evidence they were dropped.
        # Opening the rows is what makes those answers determinate instead of
        # "not found" — the same reason the read-only runner drills into rows.
        #
        # EVERY row is opened, not just the first. Additional Information holds
        # thirteen grids and Management & Shareholders three, so reading only
        # the first row of the first table would report everything entered into
        # the others as lost — dozens of failures manufactured out of rows that
        # saved perfectly.
        opened = 0
        if any(not f.value_of(e["label"]) for e in items) and _grid_rows(s) > 0:
            detail_text, opened = _read_row_details(s, say)
            if opened:
                page_text += " " + detail_text
                shot = s.screenshot(f"verify-{screen[:22]}-row")
        opened_detail = opened > 0
        # Values too short to search for. Gathered per tab and reported once,
        # rather than one entry each.
        unverifiable: list[str] = []

        for e in items:
            label, typed, kind = e["label"], e["value"], e["kind"]
            # Named after the PASS, not the tab: four of Additional
            # Information's grids have a field called "Status", and four checks
            # all called "Additional Information: Status carried through" tell
            # nobody which grid is at fault.
            name = f"{e.get('group') or screen}: {label} carried through"
            shown = f.value_of(label)

            if shown:
                if _same_value(typed, shown, kind):
                    checks.append(R.passed(
                        name,
                        detail=f"Entered {typed!r}, the case shows {shown!r}."))
                else:
                    checks.append(R.failed(
                        name,
                        expected=f"{typed!r} — the value that was entered",
                        actual=f"{shown!r} — what the case shows",
                        detail="The value did not survive the round trip. "
                               "Worth checking whether the field was "
                               "truncated, reformatted or mapped to another.",
                        evidence=[shot] if shot else [], screen=screen))
                continue

            # No labelled field. On a linkage tab that is expected: the value
            # was typed into an add-row dialog and now shows as a GRID ROW, so
            # the question becomes "is it in the table" rather than "what does
            # this field read".
            if _appears_in(typed, page_text):
                checks.append(R.passed(
                    name,
                    detail=f"{typed!r} appears in the table on {screen} — the "
                           f"row was stored. It has no labelled field here "
                           f"because it was entered through the add-row form."))
            elif not _distinctive(typed):
                # '25', 'No', '10' would match something incidental on almost
                # any page, so their absence from a text search proves nothing
                # either way — and reporting FAIL here manufactured dozens of
                # findings out of values that were simply too short to look
                # for. No check is recorded, because there is no evidence to
                # record one from; they are counted and reported once per tab
                # as an observation below, so a value does not go unverified
                # without anyone being told.
                unverifiable.append(f"{label} = {typed!r}")
            else:
                where = (f"the case's {screen}, including the {opened} saved "
                         f"row(s) opened from its grids" if opened_detail
                         else f"the case's {screen}")
                checks.append(R.Check(
                    name=name, status=R.FAIL,
                    expected=f"{typed!r} somewhere on {where}",
                    actual="not found, as a field or in a table",
                    detail="Entered on this tab but absent from the case's "
                           "copy, so the value did not carry across."
                           + ("" if opened_detail else
                              " Note the summary grid shows only some columns; "
                              "no row could be opened to check the rest."),
                    evidence=[shot] if shot else [], screen=screen))

        if unverifiable and notes is not None:
            notes.append(R.note(
                f"{len(unverifiable)} value(s) entered on {screen} have no "
                f"labelled field there and are too short to search the page "
                f"for reliably, so whether they carried across could not be "
                f"told either way: " + "; ".join(unverifiable[:8]),
                screen=screen))

        _attribute(checks[first:], screen)

    say(f"  compared {len(entries)} entered value(s) across "
        f"{len(by_screen)} tab(s)")
    return checks


def _read_row_details(s: Session, say, cap: int = 20) -> tuple[str, int]:
    """
    Open each grid row on this screen in turn and collect what it shows.

    Returns (text, rows opened). A screen's summary grid shows three or four of
    a row's columns; the rest of the row — the email, the phone number, the
    turnover figures — is only in its detail view. With thirteen grids on
    Additional Information, reading just the first row of the first table is
    the difference between a report that says the values carried across and one
    that invents dozens of failures.

    Read-only throughout: open_row_detail refuses any opener whose label fails
    the destructive denylist, and each row is left via Escape / Cancel / Back.
    Row openers are re-collected each time round because opening and closing a
    detail view re-renders the grid and drops the previous stamps.
    """
    text, opened = "", 0
    for i in range(cap):
        try:
            if i >= s.row_opener_count(cap):
                break
            if not s.open_row_detail(i):
                continue
            opened += 1
            text += " " + _screen_text(s)
        except Exception:  # noqa: BLE001 - a grid with no opener is ordinary
            break
        finally:
            s.leave_row_detail()
    if opened:
        say(f"    opened {opened} saved row(s) to read their detail")
    return text, opened


def _attribute(checks: list[R.Check], screen: str) -> list[R.Check]:
    """
    Stamp the screen onto every check from a tab, passes included.

    R.passed takes no screen, so without this the passing comparisons all
    grouped under "(run)" and the per-tab summary showed zero passes for tabs
    that had plenty — which made a readable report look like a total failure.
    """
    for c in checks:
        if not c.screen:
            c.screen = screen
    return checks


def _screen_text(s: Session) -> str:
    """Everything visible in the routed content, for grid-row comparisons."""
    try:
        return s.page.evaluate(
            """() => {
                const r = document.querySelector('[data-crawl-root]') || document.body;
                return (r.innerText || '').replace(/\\s+/g, ' ');
            }""") or ""
    except Exception:  # noqa: BLE001
        return ""


def _distinctive(value: str) -> bool:
    """
    Is this value specific enough that finding it in a screen's text means
    something?

    '25', 'No' and '10' appear incidentally on almost any page of this
    application, so neither their presence nor their absence is evidence. Six
    normalised characters is the threshold; below it the honest verdict is
    "cannot tell", not pass and not fail.
    """
    return len(_norm_value(value)) >= 6


def _appears_in(value: str, haystack: str) -> bool:
    """Is this entered value present in the text of a screen?"""
    if not _distinctive(value):
        return False
    return _norm_value(value) in _norm_value(haystack)


def _norm_value(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


_NUMERIC = re.compile(r"^[\d,\s]*\d(?:\.\d+)?$")


def _as_number(text: str):
    """
    The number a string represents, ignoring thousands separators.

    Needed because this app DISPLAYS what it stores: 5000000 was entered and
    comes back as "5,000,000". Comparing the normalised text called that a lost
    value and produced seven false findings across Limits, Additional
    Information and Corporate Governance — on values the screenshots plainly
    showed were stored correctly.
    """
    t = (text or "").strip()
    if not t or not _NUMERIC.match(t):
        return None
    try:
        return float(t.replace(",", "").replace(" ", ""))
    except ValueError:
        return None


def _same_value(typed: str, shown: str, kind: str) -> bool:
    """Presentation-insensitive, content-sensitive."""
    a, b = _norm_value(typed), _norm_value(shown)
    if not a or not b:
        return False
    if a == b or a in b or b in a:
        return True

    # Numbers before text: "5000000" and "5,000,000" are the same value.
    na, nb = _as_number(typed), _as_number(shown)
    if na is not None and nb is not None:
        return na == nb
    if kind == "date":
        # 01/01/2020 vs "January 1st, 2020" — compare the numbers that matter.
        da = set(re.findall(r"\d+", typed))
        db = set(re.findall(r"\d+", shown))
        year_a = {x for x in da if len(x) == 4}
        year_b = {x for x in db if len(x) == 4}
        if year_a and year_a == year_b:
            # same year, and every remaining number agrees once padding is gone
            rest_a = {x.lstrip("0") or "0" for x in da - year_a}
            rest_b = {x.lstrip("0") or "0" for x in db - year_b}
            return rest_a.issubset(rest_b) or rest_b.issubset(rest_a)
        return False
    # A lookup often renders only its description, or only its code.
    if kind == "lookup":
        parts = [p for p in re.split(r"\s*-\s*", typed, maxsplit=1) if p.strip()]
        return any(_norm_value(p) and _norm_value(p) in b for p in parts)
    return False


def _finish(res: FlowResult, s: Session, say) -> FlowResult:
    return _finish_nosession(res, say)


def _finish_nosession(res: FlowResult, say) -> FlowResult:
    """Write the report and say where it went. Separate only because the paths
    that never got a browser have no Session to hand over."""
    res.finished_at = _now()
    path = _persist(res)
    say(f"Done — {res.headline}")
    say(f"Report: {path}")
    return res


def _persist(res: FlowResult) -> str:
    """
    The report doubles as the input to the verification leg: it records exactly
    what was typed, which is what the case's Obligor Details is later compared
    against.

    Written twice, deliberately:

      flow.json    everything, including the entered values — the flow's own
                   record, and what a later re-verification would read.
      result.json  the same run in the shape runner/results.py produces, so the
                   web page renders a create run with the SAME code that renders
                   a read-only one. Without this the UI would need a second
                   result renderer that could drift out of step with the first.
    """
    os.makedirs(res.artifacts_dir, exist_ok=True)
    path = os.path.join(res.artifacts_dir, "flow.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(res.to_dict(), fh, indent=2)

    compat = {
        "run_id": res.run_id,
        "target_key": res.target_key,
        "target_title": (res.target_title
                         or "Create an obligor and raise a credit application")
                        + (" (dry run)" if res.dry_run else ""),
        "mode": settings.TRANSACT,
        "base_url": crawler_config.BASE_URL,
        "started_at": res.started_at,
        "finished_at": res.finished_at,
        "artifacts_dir": res.artifacts_dir,
        "error_reason": res.error_reason,
        "notes": [n.__dict__ for n in res.notes],
        # The record this run is about. For a create run that is what it made.
        "case_id": res.request_id or res.customer_id or res.obligor_name,
        "created_records": [x for x in (res.customer_id, res.request_id) if x],
        "checks": [c.__dict__ for c in res.checks],
        "steps": [{"index": st["index"], "kind": "step",
                   "label": st["text"], "status": st["status"],
                   "note": st.get("note", "")} for st in res.steps],
        "overall": res.overall,
        "headline": res.headline,
        # Extras the create view shows and the read-only view ignores.
        "obligor_name": res.obligor_name,
        "customer_id": res.customer_id,
        "request_id": res.request_id,
        "dry_run": res.dry_run,
        "entries": res.entries,
    }
    with open(os.path.join(res.artifacts_dir, "result.json"), "w",
              encoding="utf-8") as fh:
        json.dump(compat, fh, indent=2)
    return path
