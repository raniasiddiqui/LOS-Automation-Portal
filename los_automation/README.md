# LOS Automation Portal

Runs automated checks against the Loan Origination System and reports **pass / fail** —
eventually from a web page, so someone who does not write code can pick a menu and get an
answer.

Built in phases. **Phase 1 (this) is read-only** and cannot change anything in the
application.

---

## How a run reports (read this first)

Every check ends in one of **two** states, and everything else is kept out of them:

| Result | Meaning | What to do |
|---|---|---|
**PASS** | The screen worked, or a value that was entered came back unchanged. | Nothing. |
**FAIL** | The screen behaved *differently*. | A real finding — raise it. |

There is deliberately no third check status. "Could not check" used to be one, and it grew
to carry everything from a dry run to a field left blank on purpose, until a report could
be a third amber and still mean nothing was wrong.

Two things sit **outside** the pass/fail counts instead:

**Observations** — things worth knowing that are not verdicts on the application. A field
the screen does not have under the name this suite knows it by is the main one: it may
have been renamed in this deployment, or not be configured here. The run notes it, carries
on filling everything else, and the form's own validation decides whether it mattered. A
missing field never stops a run or throws away a record that was otherwise complete.

**Error** — the run could not proceed at all: login timed out, the environment is
unreachable, the case id is not in the grid, the host is not approved for data entry. An
errored run reports no verdict on the application, and counts nothing. These are never
reported as FAIL, because a tool that cries wolf stops being trusted.

Exit codes follow: `0` pass, `1` a real failure, `2` could not run.

---

## One-time setup

```bash
pip install -r requirements.txt        # plus ../requirements.txt
playwright install chromium
```

The application address and sign-in details come from `.env` in the parent folder
(`QA_BASE_URL`, `QA_USERNAME`, `QA_PASSWORD`). **Credentials are never entered in the web
page** — they stay on the server.

### The only expectation is one this tool created itself

Nothing here is compared against a document. There is no field list to load, nothing to
ingest, and no second source of truth to drift out of date. Two things are checked, and
both are self-contained:

| | What it asserts |
|---|---|
| **Check** (read-only) | The screen opens, renders content, and reports no failed request, no error banner, no missing mandatory data on the record, and no browser error. |
| **Fill and check** (writes) | A value was typed in, saved, and came back unchanged when the record was re-opened through a fresh load. |

The second is the one that catches real defects, and it is trustworthy precisely because
both halves of the comparison come from the same run: what went in is known exactly, so a
value the app drops, truncates or reformats is unambiguous. A comparison against an
external field list cannot make that claim — when the document and the application word a
field differently, the failure is in the pairing, not in the product, and a report full of
those is one an operator learns to ignore.

### One record, every run

Every check runs against a single pinned record so results are comparable between runs:

```
LOS_CASE_ID=52224-2026
```

Override per run with `--case-id`, or in the sidebar of the web page. The automation pages
through the grid until it finds it — up to `LOS_MAX_GRID_PAGES` (12) pages. It pages
rather than typing in the grid's search box, which keeps the guarantee that this tool
never types into the application.

Note the two ID formats are not interchangeable: `52224-2026` is a **case/request ID**
and lives in **My Bucket**, while **All Obligors** holds customer IDs like
`CIBG-186283-2026`. Pointing a My Bucket id at the All Obligors grid ends the run as an error —
correctly, since the record genuinely is not there.

Confirm readiness at any time:

```bash
python -m los_automation.runner.cli --check-env
```

---

## The web page (for non-technical users)

```bash
streamlit run los_automation/app.py --server.address 127.0.0.1
```

Then open <http://localhost:8501>. Pick a screen, press **Check**, and watch it work:
the page shows each navigation step as it happens, with a live screenshot of what the
automation is looking at, then a result card per check with the evidence inline.

> **Bind the address deliberately.** Streamlit listens on every network interface by
> default and will happily print an "External URL" on a public IP. This page can start
> browser sessions against a banking application using stored credentials, so bind it to
> `127.0.0.1` for local use, or to the internal interface only when hosting it — never
> leave it open to the internet.

Two things worth knowing:

- **Runs happen in a separate process.** Streamlit re-executes its script on every
  interaction, and Playwright's synchronous API will not start inside its asyncio loop.
  The subprocess also means a wedged browser can be stopped without restarting the page.
- **"Show the browser window"** (sidebar) opens a real browser on the machine running the
  page. Useful on your own laptop; invisible to users once this is hosted on a server.
  The live screenshots work in both cases, which is why they are the default.

### Hosting it

The application is on an internal host, so this must run inside the network — Streamlit
Community Cloud cannot reach it. An internal VM works, as does the
`mcr.microsoft.com/playwright/python` Docker image, which ships the browsers.

## Running a check from the command line

```bash
python -m los_automation.runner.cli --list                          # what can be checked
python -m los_automation.runner.cli --verify case.obligor           # Obligor Details (BIR)
python -m los_automation.runner.cli --verify case.all_screens       # the whole case menu
python -m los_automation.runner.cli --verify case.obligor --show    # include passes
python -m los_automation.runner.cli --verify case.obligor --headed  # watch the browser
python -m los_automation.runner.cli --verify case.all_screens --case-id 52224-2026
```

`case.all_screens` visits 45-odd screens and takes several minutes; `case.obligor` is the
quick one.

Exit codes suit scheduled runs: `0` pass, `1` a real failure, `2` could not run.

Screenshots and a machine-readable `result.json` are written to
`artifacts/<run_id>/`, alongside `events.jsonl` — the progress stream the web page reads.

---

## One run covers many screens

A target opens the record once, then walks each of its sub-screens, checking every one
opens and works.

`case.all_screens` — **the whole case menu**, all nineteen entries the app puts in the
sidebar once a case is open: Credit Approval Memo, Obligor Details (BIR) and its ten tabs,
Queries, Request Details, Facilities, Observations, Collaterals, Facility Coverage, Risk
Rating, Financials, Credit Memorandum, eCIB Details, Policies & Exceptions, Conditions,
Documents, CRMD Note, History, Relationship with Other Banks / FIs and Business
Performance.

Each one is asked the same five questions:

| Check | A FAIL means |
|---|---|
Opens and shows its content | The screen is blank — it did not render, or its content failed to load. |
No server errors while loading | A request came back 4xx/5xx, so data on the screen is missing or broken. |
No error message shown | The app itself put an error on the page. |
No missing mandatory data | The record is incomplete. Expected on a draft; worth knowing on a finished one. |
Loads without application errors | The browser logged an unhandled error, which usually leaves part of the screen unusable. |

Results are grouped per screen, so a failure says which one it came from.

### Screens that hold their data in a grid

Facilities, Collaterals, Documents, Conditions, Facility Coverage, Queries, eCIB Details,
Financials and Relationship with Other Banks are **summary lists**. A row shows a few of a
record's values; the rest live in its detail view. So on those screens the runner opens the
first row and checks the detail it reveals, then walks whatever tab strip that detail
exposes — a facility spreads its data over six tabs, discovered live because the strip
varies by product.

A screen with **no records for this case** simply says so and moves on: missing test data,
not a defect.

Opening a row is still read-only: row openers already clear the destructive denylist,
nothing is typed, and the detail is left via Escape / Cancel only.

## How a screen is reached

Most of these screens have **no address of their own**. Obligor Basic Information is
reached by a path, so targets are defined as a sequence of hops:

```
open All Obligors
  -> open the first record in the grid
    -> click 'Obligor Details (BIR)' in the case menu
      -> open the 'Basic Information' tab
```

The navigation section of every report shows exactly which hop it got to, so a failure
says *where* it stopped rather than just that it did.

Note: `Basic Information` is normally the tab that is already open when Obligor Details
(BIR) loads, so the runner accepts "already active" as success.

---

## Phase 2 — creating test data

Everything above only reads. Phase 2 writes, and it is **two flows, run in order**,
because creating a record and finishing it are two jobs done at two different moments.

### 2 — create the obligor (Basic Information only)

**Create test data** in the portal, `--create-obligor` on the CLI:

```
All Obligors -> Create Obligor
  -> fill EVERY enterable field of Basic Information, leaving
     Client Number/CIF empty                          -> Save     (obligor exists)
  -> Raise Transaction -> Borrower Credit Application -> Proceed
                       -> Profile for Initiation      -> Proceed   (case exists)
  -> My Bucket -> find that case -> open it
  -> Obligor Details (BIR) -> compare each value against what was typed
```

```bash
python -m los_automation.runner.cli --create-obligor --dry-run   # fill, save nothing
python -m los_automation.runner.cli --create-obligor             # commit
python -m los_automation.runner.cli --create-obligor --obligor-name "MY TEST CO"
python -m los_automation.runner.cli --create-obligor --stop-after-save
python -m los_automation.runner.cli --create-obligor --fill-tabs # the old one-run form
```

**Client Number/CIF is deliberately left empty.** It identifies the customer in the core
banking system, and a made-up number on a synthetic obligor is worse than a blank one.
`flows.check_left_empty` reads it back afterwards and records a check, so the report says
so in writing rather than leaving a reader to infer it from a missing line in the field
list.

It stops at Basic Information on purpose. Everything below is the second flow, so
creating a record stays a short run that either worked or did not, instead of a
twenty-minute one in which the interesting failure is buried behind eighteen grids.

### 2c — finish the obligor on the case

**Finish Obligor Details (BIR) on a case** in the portal, `--fill-obligor-details` on the
CLI. Give it the case id the first flow left behind — or any other case in My Bucket:

```
My Bucket -> find the case by id -> open it
  -> Obligor Details (BIR)
  -> fill every tab below Basic Information and all 18 of their tables (~250 fields)
  -> leave, come back in through My Bucket, open Obligor Details (BIR) again
  -> compare every value entered against what the case now shows
```

```bash
python -m los_automation.runner.cli --fill-obligor-details --case-id 52287-2026 --dry-run
python -m los_automation.runner.cli --fill-obligor-details --case-id 52287-2026
```

It goes in through **My Bucket**, not All Obligors, deliberately: the case carries its
own copy of the obligor and that copy is what an approver reads. Filling the customer
record instead would be writing to a different place from the one the round trip checks.
The second trip goes all the way back out to the grid and in again, which is what makes
it a round trip rather than a re-reading of a form the browser never left.

### The tabs

The nine tabs are **locked until the obligor has been saved** — the app ignores clicks on
them on a fresh Create Obligor form — which is why they belong to a flow that runs
against a case that already exists. Two shapes, filled completely differently:

| Tab | Shape | Mandatory |
|---|---|---|
Sector And Industry | linkage table — `+` opens an add-row dialog | 0 |
Management & Shareholders | 3 linkage tables (shareholders / directors / related-party) | 5 |
Additional Information | form, plus 12 linkage grids | 0 |
BBFS Details | form of 19 TinyMCE narrative boxes | 0 |
Contact and Address | linkage table | 5 |
Limits | form | 0 |
Corporate Governance | form, 3 date fields | 8 |
Attachments | read-only map of which field holds which document — nothing to enter |
History | audit trail written by the app — nothing to enter |

Because those tabs are locked pre-save, a dry run of the create flow can never reach
them. `--fill-obligor-details` reaches them through a case that already exists, and
`flows.fill_obligor_tabs()` does the same through the customer record — which is how the
tab filling is exercised without creating a record every time.

### Three traps worth knowing

**Nineteen editors on one tab is nineteen chances to write to the first one.** BBFS
Details is nineteen TinyMCE boxes, and the flow reported nineteen successes while the
text only ever reached the first. Driving the iframe by hand is what allowed it: the
click that places the caret has to land on an `<iframe>` TinyMCE may still be
initialising — its container renders `visibility: hidden` until it is ready — and every
failure mode of that click is a Playwright timeout the code swallowed. Nothing then
checked that the text had arrived.

Three things fix it, and all three matter:

  - `widgets.rich_text` sets the box through **TinyMCE's own API**, resolving the editor
    instance from the block's own hidden `<textarea>` id — identity, never position —
    and waiting for `editor.initialized`. It calls `save()` and fires `change`/`input`,
    which is what pushes the value into the Angular model; without them the box shows
    the text and the form saves nothing.
  - Every route **reads the value back** and raises rather than record a value the
    control does not hold. A field that could not be filled is now a blocker in the
    report, not a silent success.
  - Each entry records **which editor** took it, and `flows.check_editors_distinct`
    fails the tab when two labels resolve to one editor. This is the only check that can
    catch the original bug: reading a value back from the control you wrote to reads
    back the one box that *does* hold the text.

**Phone fields validate on LENGTH — 14 digits.** A shorter number is accepted into the
box and then fails an inline rule, at which point Save does *nothing*: no toast, no
navigation, no error. The first pass at these tabs silently saved nothing for exactly
this reason. So every tab is now checked for outstanding validation **before** Save, and
a linkage row's save is confirmed by the grid **gaining a row** rather than by the
absence of an error.

**Compliance switches default the wrong way.** `Politically Exposed?`, `Is Customer on
NAB / FIA List?` and `Is Obligor a Related Party?` are `<ui-switch>` toggles, so "take
the first valid option" turns them **on**. They are pinned to No: marking a synthetic
obligor as politically exposed in a shared environment invites real handling of a record
that is not real.

The last step is the point. Values are re-read through a **different route** than they
were entered — the credit case's copy of the obligor, not the customer record that was
filled in — so a value the app drops, truncates or reformats becomes a failed check
instead of being assumed correct because Save raised no error. That is not theoretical:
it caught a date entered as 01/01/2020 arriving in the case as *January 1st, 2001*.

**Always dry-run first.** A dry run fills every field and reports what the form holds
without clicking Save, so a change to the flow can be checked without leaving a record
behind. A live run leaves a real obligor and a real credit case that this tool cannot
remove.

### How the form's controls actually work

Learned by probing the live form, not assumed — and the reason the flow is reliable:

| Control | In the markup | How it is set |
|---|---|---|
| any field | `<label title="Field Name">` | the `title` attribute is the only dependable anchor: labels carry a red `*`, ids are reused (several share `id="title"`), and `formcontrolname` is not emitted at all |
| text | plain `<input>` in the label's fieldset | typed, then blurred so Angular validates |
| date | `<input bsdatepicker>` | **calendar clicks**, year → month → day. Typing is unreliable: this picker parsed `01/01/2020` as 1 January 2001 and kept it silently |
| dropdown | `<app-dropdown><ng-select>` | a **real** click to open it — a JS `.click()` does not — then pick from `.ng-dropdown-panel` |
| lookup (LOV) | `<app-dropdown-tree-dynamic-single-select>`, input **disabled** | click the `<i class="fa fa-search">`, then click a `<label class="form-check-label">` in the `ngx-treeview` modal; its checkbox is `display:none`, and entries carrying `tree-item-disabled` are unselectable grouping nodes |
| Yes/No switch | `<ui-switch>` with `button[role="switch"]` | no input exists; read `aria-checked`, click only if it must change |
| checkbox | Bootstrap `custom-control-checkbox`, input hidden | click the `<label class="custom-control-label">`, not the input |
| rich text | TinyMCE — hidden `<textarea id="tiny-angular_…">` plus an `<iframe>` | `tinymce.get(<that id>).setContent(…)`, then `save()` and fire `change`/`input` so Angular's model follows. Filling the textarea sets a value the editor immediately overwrites, and typing into the iframe is unreliable while the editor is initialising |

Field kinds on the nine tabs are **detected from the DOM**, not declared: the same label
is a dropdown on one tab and a switch on another, `Nationality` is a disabled auto-filled
box, and fields that look like textareas are often TinyMCE. Basic Information still
declares its kinds, because there the exact control matters and is known.

Raising a transaction is a **sequence** of dialogs — Request Type, then Profile for
Initiation — each with its own Proceed. Answering only the first looks like it worked,
shows no error, and creates no case.

---

## Phase 2b — filling the case's own screens

Phase 2 creates a case. This fills four of the screens **inside** a case that
already exists in My Bucket, and then proves the data actually stuck:

```
My Bucket -> find the case -> open it
  -> Request Details  fill the form, add a Purpose of Request row, Save
  -> Facilities       select the REQUESTED FACILITY -> Proceed
                      -> fill every tab it opens, Saving each
  -> Observations     record an observation, Save
  -> Documents        download what is attached; add a document with a file;
                      action one of the checklist documents, Save
  -> re-open the case FROM MY BUCKET and compare every value against what
     was typed
```

Each screen is its own button and its own command, because they cost different
amounts. Facilities requests a facility and then walks its whole tab strip;
somebody checking Request Details should not have to wait for that, nor have a
facility left on the case they never asked for.

```bash
python -m los_automation.runner.cli --fill-case request_details --dry-run
python -m los_automation.runner.cli --fill-case facilities --case-id 52224-2026
python -m los_automation.runner.cli --fill-case observations
python -m los_automation.runner.cli --fill-case documents --case-id 52293-2026
python -m los_automation.runner.cli --fill-case all --show
```

In the portal they are the four **Check** buttons under *Fill and check a
case's screens*, plus one that runs all of them. The dry run / live choice is a
radio above them, and a live run needs the tick-box as well.

### The facility is a request, not a form

**+ Add** on Facilities opens a dialog called **Requested Facility**, and
nothing about the facility is enterable until a product has been picked there
and **Proceed** pressed. The tabs do not exist before that, and which tabs
appear depends on which product was picked.

The picker is a **third kind of chooser**, and it is neither of the two the
obligor form uses. `choose` drives an `<ng-select>`; `lookup` clicks a magnifier
that opens a tree in its own modal. This one has no magnifier and opens no
modal: a "Select option" toggle drops a Search box and a tree down inside the
dialog that is already there. `widgets.choose_in_tree` drives it, and two
details matter:

- the tree's top level is a **group** called `Facility`, not a choice. Only
  leaves are clicked — an item with no `ngx-treeview-item` inside it;
- the open panel **covers Proceed**, so it is closed again before the click.

The product is **named, not taken first**. This environment's tree opens on
`8607 - DM - Direct Corp/Comm (EMI)`, a treasury instrument that carries neither
Overdues nor a profit structure — so `Running Finance` is searched for first,
then `Term Finance`, `Demand Finance` and the rest of `FACILITY_PREFERENCE`,
falling back to the first selectable leaf. Then:

- the tab strip is **re-read after every save**, not walked once. A newly
  requested facility opens showing a single tab, Facility Request Details;
  Facility Details, Limits and Exposures, Overdue and the rest do not exist
  until it has been saved, exactly as the obligor's nine tabs are locked until
  the obligor is. Reading the strip once found one tab and reported the other
  seven as "no tab matching", on a facility that grew them a moment later;
- a tab with an **authored field list** is filled from it. The lists are matched
  to tabs on word overlap, since the names are close but not equal — `Overdues`
  to `Overdue`, `Limit and Exposure` to `Limits and Exposures`,
  `Profit / Rental / Service Charges Structure` to the pricing list. All five of
  the tabs asked for by name are pinned in the safety tests;
- a tab that matches **nothing** is still opened and filled from what it shows.
  Skipping it is how a tab quietly stops being tested;
- **every table on every tab is filled too**, not only the ones with an authored
  list. Each grid is opened by its caption, filled — from its authored list
  where one matches, from the add-row form itself where none does — and saved on
  its own, so twelve stored rows cannot hide behind one that was refused;
- **a dry run cannot reach the tabs at all.** It picks the requested facility
  and then deliberately does not press Proceed, so no facility is created and
  there is nothing to fill. The report says so rather than reporting the tabs
  as missing.

`Is Syndicated Limit?` is pinned to **No**. Turning it on makes Participant
Banks mandatory and puts real bank names on a synthetic facility, so that table
reports "could not check" with the reason instead.

### Observations is a panel, not a table

**+ Add Observation** slides a form in down the right of the screen. It is not
a modal and not an add-row `+`, so the linkage machinery finds nothing there:
the button is located by label like the facility's, and the panel is confirmed
by its fields appearing rather than by a `.modal.show` that never arrives.

All twelve fields are authored from the panel itself, in the order it renders
them:

| # | Field | What goes in |
|---|---|---|
1 | Title | text carrying the **run marker** |
2 | Date of audit visit | 30/06/2025 |
3 | Region Response | first valid option |
4 | Date of audit | 30/06/2025 |
5 | Observation | its own sentence |
6 | Complied | first valid option |
7 | Comments | its own sentence |
8 | Report Date | 31/01/2026 |
9 | Cutoff Date | 31/12/2025 |
10 | Overall risk | first valid option |
11 | Type of Audit | first valid option |
12 | Recommendation by BRR | its own sentence |

Then **Save**, then the round trip. Three details are pinned in the safety
tests because each has a way of going quietly wrong:

- the **cut-off precedes the report date**. An audit reported before the period
  it covers had closed is what a date rule on this panel would refuse, and that
  refusal would read as "the automation could not set the field";
- each free-text box gets **its own sentence**. The same text in three boxes
  makes the round trip vacuous — any box would match any other;
- label casing is copied from the panel, not tidied. Fields are anchored on
  `label[title="..."]` and a CSS attribute match is **case-sensitive**, so
  `Overall Risk` would not find `Overall risk`. Both spellings are offered and
  the first that exists wins.

Anything below the fold beyond these is still picked up by discovery.

Saved observations render in the middle list as `COB<n> <Title>`, which drives
both halves of the check: the list **gaining an entry** is what proves Save
worked, and the run marker goes in **Title** because that is the one field the
list shows. Verification re-opens the observation by clicking it — the list is
not a `<table>`, so the grid row opener would find nothing — and reads its
panel field by field, rather than reporting every field but the title as lost.

### Documents: two panels in one slot, and a value that moves

Three jobs on one screen, run in that order and each able to fail alone:

| | What it does | Writes? |
|---|---|---|
**Download Attachments** | opens a *Document Attachments* dialog listing what the case holds, ticks Select All and presses Download, keeping the file beside the run's screenshots | no |
**Additional Documents** | opens *Upload Other Document* — Title, Upload File, Comments — attaches a generated black PNG and presses **Upload** | yes |
**Document Action** | opens one checklist document, edits Description, Date of Action, Discrepant and Justification, attaches a file, Saves | yes |

The uploaded file is a **black PNG generated by `_black_png`** from `struct` and
`zlib` — no image library, no binary in the repository — and it is **named after
the run** (`AUTOTEST-0908-124550.png`). The name is the point: it is the one
part of an attachment the app displays back, so it is what the round trip looks
for, and a fixed name would match a previous run's upload just as well.

Four things about this screen are not guessable and all four cost a debugging
session:

**Download Attachments does not download.** It opens a dialog. Treating the
button as the download meant waiting a minute for a file that was never coming
and leaving the dialog standing — and a dialog's backdrop swallows every click,
so five fields two passes later failed with `Locator.click: Timeout` and nothing
said why. The dialog's empty state also renders as a row reading *No Attachments
Found!*, and it puts its **header row inside `<tbody>`**, so either one makes an
empty list look like it holds one attachment.

**Both panels are always in the DOM.** *Upload Other Document* and *Document
Action* share one slot and both carry a field called `Title`, so an unscoped
Filler types into whichever the app rendered first. Each pass therefore runs
against a stamped panel — `Filler(scope_selector=...)` — found by a label only
that panel has: `Upload File` for one, `Date of Action` for the other. And
"is it open" cannot be answered by `offsetParent`: the closed panel is slid out
of the window, not hidden, so the test is whether its anchor is **inside the
viewport**.

**Closing a panel needs a real mouse click at a point.** The screen renders
several identical `<i class="ft-x">` icons with no title, no aria-label and no
id, so `locator('i.ft-x').first` resolves to one nobody can click. A JS
`.click()` on the right one does nothing. So candidates are filtered to those
`elementFromPoint` agrees are on top at their own centre, and clicked with
`page.mouse.click(x, y)`.

**A saved attachment leaves the boxes that took it.** `Attachment Title`,
`Attachment Description` and `Attachment` are inputs for *adding* an attachment;
Save files it against the document and clears all three. Reading them back off
the panel finds three empty inputs, so the round trip reported three stored
values as lost — on a document any human could see was holding them. They are
read from the document's **attachments list** instead, opened by the paperclip
on the panel header, which lists Title, File Name, Description and who uploaded
it. `_ATTACHMENT_FIELDS` is what routes them there.

Verification also **checks which document it is looking at** before comparing.
The panel's read-only `Title` is compared against the document that was opened,
and a mismatch is reported as "could not check" rather than compared anyway —
a panel that failed to close would otherwise have every value of one document
reported against another, as a page of confident, wrong findings.

### Fields are found by title OR by the label's text

`label[title="..."]` is what the obligor form emits on every field, and Phase 2
treated it as the universal anchor. It is not. On the Add Observation panel
**exactly one label of twelve carries a title** — the other eleven are a bare
`<label>Date of audit visit</label>` inside a `fieldset.form-group`. Anchoring
only on the title attribute therefore found `Title`, missed the other eleven,
and the run announced *7 passed / 0 failed* on a form where one box
had been filled.

So `widgets._block` now tries the title attribute first and falls back to the
label's own **text**, climbing to the smallest ancestor that holds that label
and a control — and refusing one that holds a second field's label, since a
container spanning two fields would hand the wrong control to whichever was
asked for first. Discovery reads the same way, and skips labels with no control
under them so the Observations sidebar's `Condition Type` heading is not offered
as a field.

Two smaller corrections came from the same run: date inputs on these screens say
`placeholder="Select Date"` where the obligor form says `Choose a Date`, so they
were being classified as text boxes and typed into as prose; and the field names
these flows were first written with are not the ones the app renders —
`Purpose of Facility` is really `Facility Purpose`, `Request Type (Facility)` is
`Facility Request Type`, `Facility Details` is `Facility Description`,
`Proposed Expiry Date` is `Proposed Expiry`. Each field now carries a list of
names with the **observed one first**, and the order is pinned in the safety
tests.

### Nothing is skipped in silence

Three checks exist because their absence let a run look clean when it was not:

- **every authored field is on the screen.** A field that was authored and then
  not found is now reported with the list of what could not be found. It used to
  be logged and passed over, which is how eleven missing fields became a PASS.
- **the form holds every value that was typed into it**, read back *before*
  Save. Without it, "the automation never typed it" and "the app dropped it on
  save" are indistinguishable in the final report. With it, they are two
  different lines.
- **the date the field took is the date that was asked for** — day and month,
  not just the year. Asked for 31/01/2026, this picker settled on
  *January 20th, 2026*: the year matched, the value was accepted, and the round
  trip then compared the stored 20th against the recorded 20th and called it a
  pass. Dates used by these flows are mid-month for the same reason — month-end
  is where a picker is most likely to clamp.

### Authored fields, then everything else

The obligor form was probed field by field and its labels are written down. The
case screens are built from a **per-deployment product configuration**, so the
exact label text on any one environment is not knowable in advance. Each pass
therefore does both:

1. its authored fields, in order, with values chosen to suit what each field is
   for — and with **alternative names** tried in turn, so a build that words a
   field differently still gets it filled;
2. then whatever else the screen turns out to be showing, discovered by label.

Discovery returns **names**. Every one is still set by name through
`widgets.set_value`, so the rule that no control is ever located by position
still holds. Values follow what the label says the field is for — a numeric box
gets a number, a phone box gets fourteen digits — and compliance or structural
switches (`Politically Exposed?`, `Is Syndicated Limit?`, `Life Time Expiry?`)
are pinned to No, because "first valid option" turns a Yes/No control **on**.

| Control | Where it is | How it is set |
|---|---|---|
| inline treeview | `Requested Facility`'s "Select option" | `choose_in_tree` — open, search, click a **leaf** (never the `Facility` group), close before Proceed |
| ng-select | most dropdowns | `choose` / `choose_in_dialog` |
| tree in a modal | fields with a magnifier | `lookup` |
| row grid | a dialog listing choices as rows | `pick_row_in_dialog` |

The first is the one the facility dialog actually uses; the rest are fallbacks,
because these dialogs are configuration and a build that renders one differently
should not stop the run dead.

### The round trip

The case is re-entered **through My Bucket**, not read off the form that is
still on screen. That is the whole point: a value the app dropped, truncated or
reformatted is invisible on a form still holding it in memory, and only a fresh
fetch from the server proves what was stored.

One free-text field per screen carries a **run marker** — `AUTOTEST-<stamp>`.
It does two jobs. A fixed sentence coming back proves nothing, because the
previous run's identical copy would match just as well. And it is how the
facility this run created is told from the ones already on the case: the
verification leg opens rows until it finds the one carrying the marker, rather
than assuming the new facility is first in the grid. If no row carries it, the
answer is "the facility was not saved" — not a comparison against somebody
else's record.

Comparison is forgiving about presentation and strict about content, exactly as
the obligor round trip is: `10000000` coming back as `10,000,000` is not a
finding, `31/12/2027` rendering as `December 31st, 2027` is not a finding, and a
value too short to search for (`1`, `No`, `12`) reports **could not check**
rather than a failure.

---

## Safety

The application is a live banking system, so the checks are read-only by construction:

- **Nothing is ever typed** into the application. The only text entered anywhere is the
  password on the sign-in page.
- **Destructive controls are unreachable.** Save, Submit, Approve, Reject, Delete, Post,
  Authorize, Bulk Action, Export and Sign out are all refused before a click happens.
- **Data entry is host-restricted.** Phase 2 refuses to run anywhere except the approved
  dev host, and refuses *before a browser opens*. An unset allowlist blocks everything
  rather than allowing everything.
- **Exactly one module writes.** `runner/widgets.py` is the only file that can type or
  commit; `flows.py` authors the sequence but must go through it. Two gates, both fail
  closed: the host allowlist, and a per-control allowlist that permits Save / Proceed /
  Add / OK and refuses Approve / Reject / Delete / Forward *even when a flow asks for
  them*. There is no "fill every field on the page" helper and no control is located by
  position — always by its label.

Verify all of that at any time:

```bash
python -m los_automation.tests.test_safety
```

---

## Layout

| Path | Purpose |
|---|---|
`app.py` | The Streamlit web page |
`runner/jobs.py` | Runs the automation as a subprocess, streams progress back |
`runner/targets.py` | Screens and the menu path to reach each |
`runner/driver.py` | Browser session and path navigation (wraps `../crawler.py`) |
`runner/checks.py` | The read-only assertions |
`runner/results.py` | PASS / FAIL checks, observations, and the run-level error state |
`runner/run.py` | Orchestrates one read-only run — used by the CLI and the web page |
`runner/widgets.py` | **Phase 2.** The only module that types or commits. Both gates live here |
`runner/flows.py` | **Phase 2.** The authored create → raise → find → verify sequence |
`runner/case_flows.py` | **Phase 2b.** Request Details, Facilities and Observations on an existing case, then the round trip |
`runner/cli.py` | Command line |
`tests/test_safety.py` | Proves the safety properties above |
`artifacts/` | Screenshots and reports, one folder per run |

Nothing in this folder modifies the crawler or test-case generation in the parent project,
and nothing in it reads a specification: the field lists here were read off the live
application and are authored in code.

---

## Coming next

Phase 2 fills Basic Information and the seven enterable tabs. What is not covered yet:

- **The 12 linkage grids inside Additional Information** — deposit accounts, external
  ratings, major products / buyers / suppliers / competitors / brands, manufacturing
  facilities. Its own form fields are filled; these nested tables are not. The machinery
  exists (`Filler.add_row(grid=n)` selects which `+`), so each is a spec entry rather
  than new code.
- **The second and third grids on Management & Shareholders** — directors/management and
  related-party transactions. Only the shareholders grid is filled.
- **Attachments** — documents attach through a paperclip on the individual field they
  belong to, not through the Attachments tab, which is a read-only map. That is a
  different flow from the one built here.
- **The 16 other request types.** Only Borrower Credit Application is authored.
- **The rest of the case menu.** Phase 2b fills Request Details, Facilities and
  Observations. Collaterals, Facility Coverage, Financials, Conditions,
  Documents and the others are still read-only — each is a `Pass` list in
  `case_flows.py` rather than new code.
- **A second facility on one case.** The flow requests one facility and fills
  it; a case that needs two needs the button pressing twice.
- **Phase 4+** — the remaining menus, then the other modules.
