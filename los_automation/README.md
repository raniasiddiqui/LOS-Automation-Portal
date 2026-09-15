# LOS Automation Portal

Runs automated checks against the Loan Origination System and reports **pass / fail /
blocked** — eventually from a web page, so someone who does not write code can pick a
menu and get an answer.

Built in phases. **Phase 1 (this) is read-only** and cannot change anything in the
application.

---

## What "blocked" means (read this first)

Every check ends in one of three states, and the difference matters:

| Result | Meaning | What to do |
|---|---|---|
**PASS** | The screen worked, or a value that was entered came back unchanged. | Nothing. |
**FAIL** | The screen behaved *differently*. | A real finding — raise it. |
**BLOCKED** | We could not tell. | An environment or test-data problem, **not** an application defect. Fix the cause and re-run. |

A login timeout, a network outage, or a grid with no records to open are all **BLOCKED**.
They are never reported as FAIL, because a tool that cries wolf stops being trusted.

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

Override per run with `--case-id`, or in the sidebar of the web page.

**How the record is found differs between the two phases, deliberately.**

- **Phase 1 (read-only)** pages through the grid — up to `LOS_MAX_GRID_PAGES`
  (12) pages — and types nothing at all. That is what keeps the guarantee that
  a read-only check cannot put a single character into the application.
- **Phase 2 (the fill flows)** asks **My Bucket's own search box** for the id
  first, and only pages if that is unavailable.

Phase 2 searches because paging is capped, and the cap was silently wrong:
case `52224-2026` sits beyond page 12, so twelve pages of scanning reported
*"is not in My Bucket"* about a case that was there the whole time — and the
message even suggested the transaction had not completed. Searching asks the
server, so the record is found wherever it is.

Typing a case id into a search box is still a **write** by this suite's
definition, so it lives in `widgets.py` behind the same host allowlist as
every other one, and it is deliberately **not recorded as an entered value** —
a search term is navigation, not data, and recording it would drag it into the
round-trip comparison. Paging remains the fallback for a build with no search
box, a host where typing is refused, or a search that comes back empty on a
grid that holds the record anyway; so Phase 2 is never worse at finding a case
than paging alone was.

Note the two ID formats are not interchangeable: `52224-2026` is a **case/request ID**
and lives in **My Bucket**, while **All Obligors** holds customer IDs like
`CIBG-186283-2026`. Pointing a My Bucket id at the All Obligors grid reports BLOCKED —
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

### Taking the report away with you

Every result page ends with three downloads, all built by `runner/exports.py`
from the same rows the tables above them are built from — so a downloaded
report cannot disagree with the page it came from:

| Button | What is in it |
|---|---|
**CSV Download** | What was checked: every check, its result, what it expected, what the application actually showed |
**CSV Download (entered values)** | What was entered: every field the run filled, with its value. On a Litigation run this is the record itself — type of suit through to date of decree |
**PDF Download** | The whole report in one document: what it ran against, what it entered, how it got there, and every check with its result |

Rich text needs unpicking on the way out, and this is where it happens. A
TinyMCE box — Proceeding Details on Litigation, the obligor's nineteen BBFS
editors — does not come back as a plain string: it arrives broken by newlines,
and can carry markup. A raw newline would split a CSV row in two, so markup is
**unwound rather than deleted**: a block tag becomes a paragraph break, `<br>`
a line break, and the CSV marks a paragraph boundary with `¶` inside the one
cell. The PDF lays the paragraphs out as paragraphs.

The PDF needs `reportlab` (in `requirements.txt`). Without it the button says
so and stays disabled — the CSVs beside it, and the run's own findings, are
unaffected.

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

Exit codes suit scheduled runs: `0` pass, `1` a real failure, `2` blocked.

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

Everything above only reads. Phase 2 adds one authored flow that writes, exposed as
the **Create test data** section of the portal and `--create-obligor` on the CLI:

```
All Obligors -> Create Obligor
  -> fill the 13 mandatory fields of Basic Information -> Save     (obligor exists)
  -> fill the 7 tabs that Save unlocks, saving each                (~69 fields)
  -> Raise Transaction -> Borrower Credit Application -> Proceed
                       -> Profile for Initiation      -> Proceed   (case exists)
  -> My Bucket -> find that case -> open it
  -> Obligor Details (BIR) -> walk every tab and compare each value
                              against what was typed
```

```bash
python -m los_automation.runner.cli --create-obligor --dry-run   # fill, save nothing
python -m los_automation.runner.cli --create-obligor             # commit
python -m los_automation.runner.cli --create-obligor --obligor-name "MY TEST CO"
python -m los_automation.runner.cli --create-obligor --stop-after-save
```

### The tabs

The nine tabs are **locked until the obligor has been saved** — the app ignores clicks on
them on a fresh Create Obligor form — so they are a second pass after Save, not part of
the same fill. Two shapes, filled completely differently:

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
them. `flows.fill_obligor_tabs()` fills them on an obligor that already exists, which is
how the tab filling is exercised without creating a record every time.

### Two traps worth knowing

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
| rich text | TinyMCE — hidden `<textarea>` plus an `<iframe>` | type into the iframe's `body`; filling the textarea sets a value the editor immediately overwrites |

Field kinds on the nine tabs are **detected from the DOM**, not declared: the same label
is a dropdown on one tab and a switch on another, `Nationality` is a disabled auto-filled
box, and fields that look like textareas are often TinyMCE. Basic Information still
declares its kinds, because there the exact control matters and is known.

Raising a transaction is a **sequence** of dialogs — Request Type, then Profile for
Initiation — each with its own Proceed. Answering only the first looks like it worked,
shows no error, and creates no case.

---

## Phase 2b — filling the case's own screens

Phase 2 creates a case. This fills ten of the screens **inside** a case that
already exists in My Bucket, and then proves the data actually stuck:

```
My Bucket -> find the case -> open it
  -> Request Details    fill the form, add a Purpose of Request row, Save
  -> Facilities         select the REQUESTED FACILITY -> Proceed
                        -> fill every tab it opens, Saving each
  -> Observations       record an observation, Save
  -> Litigation         open the entry form from '+', record a suit, Save
  -> Shariah Comments   write the SCD Remarks editor, Save
  -> CRMD Note          write all eight narrative editors, Save
  -> Credit Memorandum  write all thirty-three narrative editors, Save
  -> Group Review       write all seven narrative editors, Save
  -> Business Performance   BOTH sub-menus, one after the other:
       -> Customer Business Performance   enter the grid's figures, Save
       -> Group Business Reciprocity      enter the grid's figures, Save
  -> Relationship with Other Banks / FIs
                        '+ Add' -> choose a bank -> Proceed
                        -> add a LIMITS row, then fill the editors, Save
  -> re-open the case FROM MY BUCKET and compare every value against what
     was typed
```

Business Performance is **one** check covering both of its sub-menus, because
that is one thing to ask for rather than two. They stay apart in the *result* —
each sub-menu carries its own name on every figure and every check, so each
keeps its own save result, its own round trip and its own line in the
per-screen summary. See *Business Performance is two sub-menus*.

Each screen is its own button and its own command, because they cost different
amounts. Facilities requests a facility and then walks its whole tab strip;
somebody checking Request Details should not have to wait for that, nor have a
facility left on the case they never asked for.

```bash
python -m los_automation.runner.cli --fill-case request_details --dry-run
python -m los_automation.runner.cli --fill-case facilities --case-id 52224-2026
python -m los_automation.runner.cli --fill-case observations
python -m los_automation.runner.cli --fill-case litigation --dry-run
python -m los_automation.runner.cli --fill-case shariah_comments --dry-run
python -m los_automation.runner.cli --fill-case crmd_note --dry-run
python -m los_automation.runner.cli --fill-case credit_memorandum --dry-run
python -m los_automation.runner.cli --fill-case group_review --dry-run
python -m los_automation.runner.cli --fill-case business_performance --case-id 52224-2026
python -m los_automation.runner.cli --fill-case bank_relationships --dry-run
python -m los_automation.runner.cli --fill-case all --show
```

In the portal they are the ten **Check** buttons under *Fill and check a
case's screens*, plus one that runs all ten. The dry run / live choice is a
radio above them, and a live run needs the tick-box as well.

`bank_relationships` takes the bank from `LOS_BANK_NAME`, defaulting to
`Islamic Bank`.

Adding a screen means adding it to `case_flows.SCREEN_LABEL` and `ORDER`, a
`_do_<screen>` to dispatch to, and a line to `app.CASE_SCREENS`; a screen that
hides under another sidebar entry also needs a line in `SUB_MENU_PARENT`, and
the round trip then re-opens it through its parent. Everything
else — the CLI choice, `all`, the live view, the result page, the round trip
and the downloads — reads off those, and the safety tests check that a screen
in `SCREEN_LABEL` really is reachable from the CLI and really is dispatched to
a flow of its own. A screen that is registered but not dispatched would be
filled as an observation, on the wrong form.

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

### Litigation is a grid with a '+'

The case menu's own **Litigation** screen is a grid of suits with a `+` that
opens the entry form. It is the same shape as Observations — a list, a control
that opens a form, ten fields, Save — and it goes through the same
`_fill_pass`, so it is filled, validated before saving, read back before
saving, saved and then round-tripped by exactly the machinery the other three
screens use.

On this build the screen is `/riskNucleus/master/ca-package/litigation`: a
**LITIGATION DETAILS** grid — *Type of Suit, Suit Number, Court Name, Bank's
Lawyer Name, Law Firm Name* — with a `+` at its top right labelled **Add
Details**, which opens a modal called **Litigation Details** carrying all ten
fields and a `Save` / `Close` footer.

None of that is hard-coded, because how the form opens is the one thing about
it that varies. `_open_add_litigation` tries the **named** control first — by
label, through the crawler's own denylist, like the facility's and the
observation's openers — then the grid's bare `+`, which only
`widgets.add_row` can click; and if the form is already on the screen it uses
that instead of opening a second one. Either way the form is confirmed **by its
fields appearing**, never by a `.modal.show`: `add_row` reports failure unless a
modal arrives, which is right for a linkage table and wrong here, because a
form that opens as a side panel opens no modal and the click still worked.
`Filler` scopes itself to a modal automatically when there is one, so one pass
covers both shapes and neither is assumed.

| # | Field | Control | What goes in |
|---|---|---|---|
1 | Type of Suit | dropdown | first valid entry — the suit-type reference data is re-seeded per environment (`Against the Bank`, on this one) |
2 | Date Of Suit Filing | date | 30/06/2025 |
3 | Relevant Court | text | its own sentence |
4 | Bank's Lawyer Name | text | its own sentence |
5 | Law Firm Name | text | its own sentence |
6 | Last Date of Hearing | date | 15/01/2026 |
7 | Next Date of Hearing | date | 30/06/2027 |
8 | Suit Amount | number | 10,000,000 |
9 | Proceeding Details | TinyMCE | the **run marker** |
10 | Date Of Decree | date | 31/12/2027 |

The **control kind is never declared** — `Filler.kind_of` reads it off the DOM,
which is why `Type of Suit` needed no decision here. The requirement calls it an
LOV; this build renders it as an `<ng-select>`, so `choose` drives it. Had it
been the magnifier-and-tree kind, `lookup` would have driven it instead, from
the same authored line.

Four things are pinned in the safety tests:

- **the dates are a chronology** — filed, heard, heard again, decreed. A screen
  that validates one hearing against another would refuse any other order, and
  that refusal would read as "the automation could not set the field";
- `Type of Suit` and `Date Of Suit Filing` are **mandatory**. Everything else is
  a note if this deployment does not have it; without those two there is nothing
  to store and the form is expected to say so;
- the run marker goes in **Proceeding Details**, and it is the only field here
  that could carry it. It is the narrative box, so there is no length limit to
  truncate the marker into something the round trip cannot find — and because it
  is a **rich-text editor**, proving that one value came back is also the only
  proof the editor's content survives a save;
- **label casing is offered both ways.** The form writes `Date Of Suit Filing`
  and `Last Date of Hearing` — capital and lowercase `Of` on the same screen —
  and the title-attribute anchor is case-sensitive, so both readings are
  authored and the first that exists wins. `Law Firm Name` is the same story
  against the requirement, which writes `Law firm Name`; the app's spelling
  goes first.

### Shariah Comments is one editor and a Save

The smallest screen in the case, and the one with the least to go on. It holds
a single **SCD Remarks** rich-text editor with a **Save** under it — no grid,
no `+`, no dialog — so `_do_shariah_comments` opens the screen and runs the
pass. There is deliberately nothing else in it: no opener to find, no rows to
count, no modal to close.

That is not laziness, it is the shape of the screen, and it changes what a run
can honestly conclude. On Observations and Litigation a list or a grid **gains
an entry**, which proves Save stored something without trusting the absence of
an error toast. Here there is no such evidence, so the **round trip is the only
proof** — re-open the case from My Bucket and see whether the editor comes back
holding what was typed. Which is also why its one field carries the **run
marker**: text that is the same on every run would match a previous run's copy
and prove nothing at all.

| # | Field | Control | What goes in |
|---|---|---|---|
1 | SCD Remarks | TinyMCE | the **run marker** |

Two details are pinned in the safety tests. `SCD Remarks` is **not optional** —
it is the only field on the screen, so a run that cannot set it has exercised
nothing, and reporting "could not check" there would be exactly the silence
this suite exists to avoid. And its casing is offered three ways, because
fields are anchored on `label[title="..."]` and a CSS attribute match is
case-sensitive.

It is a **TinyMCE editor, not a textarea**, and `widgets.rich_text` knows the
difference: TinyMCE hides the original `<textarea>` and edits inside an
`<iframe>`, so filling the textarea sets a value the editor overwrites with
its own empty content a moment later. `kind_of` reads which one this deployment
renders rather than assuming, and `value_of` reads the value back out of the
iframe — without that, the round trip reports a value it can plainly see on
screen as lost.

### CRMD Note is eight of the same thing

Eight narrative editors and a Save, and therefore **the same code as the
one-editor screen above** — open it, run the pass. The whole difference
between the two screens lives in what `CRMD_NOTE_PASSES` declares, which is
where a difference between two screens belongs.

| # | Field | Control | What goes in |
|---|---|---|---|
1 | Recommendation | TinyMCE | the **run marker** |
2 | Industry Strategy | TinyMCE | its own sentence |
3 | Internal Indicators (including business reciprocity / account turnover / overdue history, etc) | TinyMCE | its own sentence |
4 | Internal & External Audit Observations | TinyMCE | its own sentence |
5 | Return on Capital / Hurdle Rate | TinyMCE | its own sentence |
6 | Key Credit Concerns / Additional Information | TinyMCE | its own sentence |
7 | Risk Observations / Covenant Compliance Status | TinyMCE | its own sentence |
8 | Risk Exposure Strategy | TinyMCE | its own sentence |

Eight boxes on one screen make one rule matter more than any other, and BBFS
Details' nineteen editors taught it the hard way: **every box gets its own
text, and that text names its own box.** The same sentence in eight editors
makes the round trip vacuous — any box would match any other, so "the text
came back" would prove nothing about *where* it came back from. Both halves
are pinned in the safety tests: the eight values are all distinct, and each
one names its own section.

Field 3 is worth knowing about. Its label is **94 characters**, over the
90-character cap in `widgets._visible_labels`, so **discovery would never
offer it** — it is fillable only because it is authored by name. That is
pinned too, because tidying that label out of the list would stop the field
being filled *and* stop it being discovered: a field that vanishes without a
trace. A shorter reading is kept behind it for a build that truncates the
label, and the `&` in field 4 is offered spelled out as well, since
normalisation turns `&` into "and" but the `label[title="..."]` anchor is
literal.

As on Shariah Comments, nothing here grows when Save works, so the **round
trip is the only evidence** anything was stored.

### Credit Memorandum is thirty-three of the same thing

The largest screen in the case: thirty-three narrative editors and a Save.
`_do_credit_memorandum` is the same two steps as the one-editor screen — open
it, run the pass — and that the biggest screen needs no code of its own beyond
its field list is the point of `_fill_pass`. A screen of one and a screen of
thirty-three cannot drift apart in how they are checked.

**Counting them is the first thing to get right, and only the application can
settle it.** Read as prose the section list looks like thirty-four, because
*"Major Obligor, Industry & Transaction Risks"* can be read as two sections.
It is **one combined heading**: authored as two, a live run found neither, and
discovery then filled the real editor under its full name. There are
**thirty-three**, and that is pinned — with the two split readings kept behind
the combined one in case another build separates them.

At this size the text for each box is **built by `_memo_note` rather than
written out**, which is the choice `flows.py` already made for BBFS Details'
nineteen editors. Two reasons, both worse the higher the count:

- thirty-three hand-written sentences are thirty-three chances to paste the same
  one twice, and duplicate text makes the round trip vacuous — any box would
  match any other. The property has to hold **by construction**, not by
  proof-reading;
- thirty-three paragraphs of invented credit analysis, left on a real obligor's
  memorandum in a shared environment, is not something to walk away from. Every
  box says what it actually is instead: an automated test's note, naming its
  own section, stating plainly that it is **not a credit assessment**. That
  sentence is checked for on every one of them.

Three things about the labels earn their own pins:

- **six of these sections also exist on CRMD Note**, and their text is
  deliberately *different* on the two screens. That is load-bearing, not tidy:
  if the application stored the memorandum's `Industry Strategy` into the
  note's box, identical text would let **both** round trips pass and the defect
  would be invisible. Different text makes it a finding.
- **three labels carry a right single quotation mark** (`Shareholder’s`,
  `Obligor’s`, `Borrower’s`) rather than an ASCII apostrophe, and both readings
  are authored for each. `label[title="..."]` is a literal string match, so `’`
  and `'` are simply different characters there; the text fallback normalises
  both away, which is the only reason the second reading works.
- `Collateral Evaluation / Justification` and `Collateral Justification` are
  **two different sections** whose names nearly contain one another. The text
  anchor matches a label exactly once normalised, so neither can be reached by
  the other's name — pinned, because if that ever loosened, one would be filled
  twice and the other never.

`Internal Indicators (…)` is 94 characters here too, so the same discovery cap
applies: authored by name or not filled at all.

### Group Review, and the container that spanned seven fields

Seven narrative editors and, on paper, a Save. All seven labels and their
order are exactly as authored — `Group Background`, `Group Companies`,
`Industry Details / Peer Analysis / Financial Analysis`, `Trade Business
through NBP`, `Group Relationship Yield`, `Group Relationship Strategy /
Recommendation`, `Risk Advice`. Two things about it are worth the space.

**The screen is not on every case.** It is in the sidebar of `52224-2026`, an
Annual Renewal with twenty-three case-menu entries, and absent from a Borrower
Credit Application with twenty-two. A case without it reports *"Group Review
can be opened"* as **BLOCKED** with the sidebar's actual contents — which is
the answer an operator needs, not a failure.

`Risk Advice` also exists on Credit Memorandum, so `_group_note` words it
differently from `_memo_note` — the same reason the six sections shared with
CRMD Note are worded apart, and pinned the same way.

#### The bug it exposed was a wrong WRITE, not a false failure

Every previous finding in this file was a check that cried wolf. This one was
the opposite, and it is the worse kind. Group Review's seven labels and seven
TinyMCE editors are **all direct children of one
`<fieldset class="form-group">`**. So `fieldset:has(> label[title="Group
Background"])` matched that fieldset — and the same selector for the other six
matched *the same fieldset*. `block.locator("iframe").first` is then editor
number one every time, so all seven values were typed into the first box, each
overwriting the last, and every read-back returned the seventh section's text.

`widgets._block` now refuses any candidate that holds **more than one field's
label**, which was previously only checked on the text-based anchor and not on
the title-attribute one. And a third anchor was added for a form laid out
flat: **the control that follows this label**, stopping at the next field's
label. It is still anchored on the label — never an index among like controls —
and it is tried last, so a field that resolves to its own container is
unaffected.

Confirmed on the live screen: *"All 7 field(s) still read back what was
entered"*, where six of seven had not. Re-verified against CRMD Note (8
editors) and Credit Memorandum (34) to be sure nothing that already worked
changed.

#### Finding: the screen offers no way to save

On `52224-2026`, as **Regional Corporate Head**, the seven editors are present
and accept text — and there is **no save control anywhere in the DOM**. Not
disabled, not hidden, not icon-only: every button on the page belongs to the
app shell (role switcher, Preferences, Sign out) or to TinyMCE's own toolbars,
and nothing in the document mentions save, submit, update or apply. The routed
content ends after the Risk Advice editor.

So the run reports *"Group Review is saved"* as a **failure**, correctly: the
values went in and could not be stored. Whether that is a permissions state
for this role or a gap on the screen is for the NBP team to say — the
automation's job was to establish that typing works and saving is not offered,
and it did.

### Business Performance is two sub-menus, and not a form at all

The first screen pair here that is a **grid**, and the first reached as
**sub-menus** of another sidebar entry. Both facts were read off the live
screen rather than assumed, and both changed the implementation.

`Business Performance` opens on a panel with two vertical tabs — `CUSTOMER
BUSINESS PERFORMANCE` and `GROUP BUSINESS RECIPROCITY`. Each shows the same
ten rows against a period (`Actual, Jan 1 2003 – Dec 31 2003` on one, `Jan 2
2004 – Jan 1 2005` on the other) in two columns, and its own Save.

**One check does both.** `--fill-case business_performance` opens each
sub-menu in turn, fills its grid and saves it, and the single round trip
afterwards goes back inside *each* of them. Each sub-menu is wrapped
separately, so one that cannot be opened does not take the other with it — and
because every figure and every check carries the sub-menu it came from as its
screen, the report still says which of the two a result belongs to:

```
per screen, as the report groups it:
  Customer Business Performance    pass= 25  fail=  0
  Group Business Reciprocity       pass= 19  fail=  6
```

#### There are no fields on this screen

`field_labels()` finds **nothing** here, and it is right to. The ten row names
are one `<table>`; the twenty numeric boxes are **another**; and a box carries
no label, no `title`, no `aria-label`, no `formcontrolname`, `name="id"` on
every one of them and a duplicate `id` of `"false"` or `"true"`. Nothing on an
input says which figure it belongs to.

So the two tables have to be joined on **row position** — the one thing
`widgets` refuses to target by anywhere else, because an index can silently
point at the wrong control. `Filler.grid_cell` makes it safe by never letting
the position be the caller's:

* the caller names a metric; the name is looked up in the label table.
* the two tables must have **the same number of rows**. A layout change that
  broke the correspondence changes a row count and is refused, not typed into.
* the metric must name **exactly one** row; two matches is refused.
* only one label/value table pairing may be possible; two visible grids is
  refused.

Every one of those raises rather than falling back to a guess. A wrong guess
here writes a number into another metric's box — the same class of fault as
the shared fieldset above, and just as silent.

#### The opener must not match its own parent

`Business Performance` is *contained in* `Customer Business Performance`, and
the driver matches menu labels by containment because the sidebar truncates
its own text. So asking the sidebar for the child **succeeds by opening the
parent**, and the run then reports a screen it never reached. It worked the
first time by luck — the child wanted happened to be the parent's default tab.
`_open_sub_screen` now always opens the parent first, looks for the child among
its tabs, and rejects a note that names the parent but not the child.

#### Three of the ten rows are the application's own arithmetic

`Total Income of Business Group`, `Annualized Yield (%)` and `Net Yield (%)`
are greyed and read-only, because the app derives them. Rather than skip them,
the rules were read off the figures the screen was already holding and
confirmed on both columns of both sub-menus before anything was typed:

```
Total Income of Business Group = Mark-up + Commission + Other Fee
Annualized Yield (%)           = Total Earnings / Average Funded x 100
Net Yield (%)                  = Annualized Yield - Borrowing Rate
```

Seven rows per column are therefore typed and three are **checked** — against
what the application itself stores in the rows they come from.

Both sub-menus show the same ten row names, which is the trap: with the same
figures on both, a value stored against the wrong sub-menu would satisfy both
round trips and a mix-up would report as twenty passes. Every figure is unique
to its row, its column, its sub-menu and its **run** — and the run's
uniqueness has to come from the digits, since a numeric screen has nowhere to
type `AUTOTEST-<stamp>`.

#### Finding: Group Business Reciprocity does not recalculate its totals

One live run of the single check: **46 passed / 6 failed**, 28 figures entered
across the two sub-menus and verified in one round trip.

**Customer Business Performance: 25 passed, 0 failed.** Fourteen figures
entered, saved, and read back after re-opening the case from My Bucket; all
six derived figures recomputed and consistent.

**Group Business Reciprocity: 19 passed, 6 failed.** The fourteen typed
figures all survived the round trip — and all six derived figures came back
**stale**:

| | shown | the rows it is derived from |
|---|---|---|
| Total Income of Business Group | 12,700 | 15,643,329 |
| Annualized Yield (%) | 23.08 | 98.25 |
| Net Yield (%) | 4.20 | −5.35 |

12,700 is exactly the total of the figures that were there *before* the run
(`11,000 + 1,500 + 200`), and 23.08 and 4.20 likewise. So Save stored the
inputs and left the derived rows alone, and the case now holds a Total Income
of 12,700 beside three rows that add up to fifteen million.

Confirmed in a **separate read-only session** on a fresh load of the case, so
it is what the server returns and not an artefact of the run that typed it.
The same three rules hold on the Customer sub-menu, on the same data, through
the same code — which is what makes this specific to this sub-menu rather than
a guess about the formulas.

### Relationship with Other Banks / FIs creates the record it fills

The most involved screen here, and the only other one — with Facilities —
where the thing being filled does not exist until the run makes it:

```
list page -> '+ Add' -> a dialog with one field, Bank Name -> Proceed
  -> the record's own page
     -> the LIMITS section's '+' -> a Limits dialog -> Save, Close
     -> three TinyMCE editors, two text areas, a date and a dropdown
     -> Save
```

Live: **36 passed / 0 failed / 2 blocked**, and the two blocked are honest —
`Outstanding Date` and `FE Limits` are entered on the Limits dialog but are
**not columns of the LIMITS table**, so the record's page does not show them.
FE Limits is covered by the `Total FE Limits` aggregate; Outstanding Date is
not displayed anywhere after saving.

#### The one negative check in the suite

Everything else here asks whether the application accepts good data. Step 1
asks whether it refuses incomplete data: **Proceed is pressed with Bank Name
empty**, and the run asserts the form says `Bank Name is required` *and* that
the dialog stayed open, so nothing was created. It holds. A Proceed that was
accepted on the empty form would be a FAILURE — a dialog that quietly created
an empty relationship would still let every later step pass.

The dialog's `Cancel` is exercised too, by the **dry run**, which chooses a
bank and then dismisses rather than creating anything. `Filler.cancel_modal`
presses a dialog's own Cancel or Close rather than reaching for Escape, and it
refuses any label that could commit — see *Escape closes the form* for why
Escape is not a safe default here.

#### Four aggregates are checkable, one is not

The five figures under the LIMITS table are all read-only — the application
sums them. Because the relationship is **created by this run**, it has exactly
one limit row, so four of them have an unambiguous expected value:

| | expected | shown |
|---|---|---|
| Total Limits | the row's Total Limit | `10,000,000` |
| Total O/s. | the row's Total Outstanding | `1,000,000` |
| Total Overdue | the row's Total OverDue | `250,000` |
| Total FE Limits | the row's FE Limits | `500,000` |

All four pass. On a record that already had rows, "is it the sum" and "is it
the last row" would look identical — which is why the record is created rather
than reused.

`Wallet Share (%)` is **reported, not asserted**: it compares this bank
against the others on the case. The run does now know the rule — adding a
10,000,000 limit moved the pre-existing record's share from `100` to `0.99`,
and 100,000 / 10,100,000 is 0.99% — but a rule inferred from one observation
is not one to fail a build on, so the check quotes the before and after and
asks for confirmation.

#### The list pages at five rows, and the new record was on page two

The first live run reported **fifteen values as unverifiable** on a record
that was sitting one page away holding every one of them. `row_opener_count`
saw the two pre-existing relationships on page one, neither carrying the
marker, and stopped there.

The fix is the one this project already learned on My Bucket: **search, don't
page.** The marker goes in `Security` precisely because Security is one of the
six columns the list renders, so typing the marker into the list's own search
box brings the record back as the only row — `row 1 of 1`. Paging remains the
fallback for a build with no search box, a host where typing is refused, or a
search that matches nothing.

#### A table cell is not a labelled field

Four more false failures, and the cause is worth remembering. A limit's values
are **cells of the LIMITS table**, not labelled fields, so the generic
comparison fell through to searching the page's text — and that search
normalises punctuation away. `10000000` was entered, the table renders
`10,000,000`, and the two do not match as strings. The run reported four
values as dropped while the aggregates directly above them proved they were
stored.

The tempting fix — make the text search compare digits only — would have been
**worse than the bug**: `1000000` is a substring of `10000000`, so Total
Outstanding would have matched the Total Limit cell and a genuinely lost value
could have passed. Each value is now compared against **its own column**,
matched by header name, through the same presentation-insensitive comparison
everything else uses.

#### Finding: 30 June 2027 is refused here too

`Expiry Date` would not take 30/06/2027. Its calendar settles on **1 June
2027** — the same wrong date, from the same request, as `Next Date of Hearing`
on Litigation, now on a second and unrelated screen.

It is not month-ends and it is not this field. Tested directly on the record,
without saving:

| asked for | stored |
|---|---|
| 15/06/2027 | taken |
| 15/01/2026 | taken |
| 31/12/2027 | taken |
| **30/06/2027** | **refused — 1 June 2027** |

Typing rather than picking is stranger still: the field parses the *first*
number as a year and sets 1 January of it — `30/06/2027` became **January
1st, 2030**, and `06/30/2027` became **January 1st, 2006**.

This screen therefore uses 31/12/2027, so that the round trip can say
something about whether an expiry date persists at all. The defect stays
exercised, and reported, by Litigation.

### Escape closes the form, not just the picker

Found here, and it was doing real damage. **Escape** is what closes this app's
date picker and its dropdown panels — and inside a Bootstrap modal it closes
the **modal** as well. `_calendar_pick` pressed it unconditionally after
clicking a day, so on the Litigation form the entry form disappeared mid-fill:
`Type of Suit`, already answered, read back **empty**, and the seven fields
after it reported *"not on this screen"*. The run blamed the application for
losing a value the automation had thrown away itself — the exact failure mode
this suite exists not to produce.

`widgets._close_calendar` and `_close_dropdown` now press **nothing unless
something is actually open** — clicking a day closes the calendar already, so
the usual case costs nothing — and never use Escape while a modal is up, where
the trigger is clicked again to toggle the overlay shut instead. On a screen
with no modal the behaviour is exactly what it was.

This is not a Litigation detail. Every add-row dialog in the suite is a modal
with dates and dropdowns in it, so the same trap was sitting under the obligor
tabs and the facility tables, waiting for a picker that did not close itself.

Save is judged by **the grid gaining a row**, the same way the observation is
judged by its list gaining an entry: this form closes on save whether or not it
stored anything, so the absence of an error toast proves nothing. Any modal
still open is dismissed before the rows are counted, since a grid behind one is
not visible and would count as empty.

#### Finding: Next Date of Hearing will not take 30/06/2027

Reproduced on two live runs, and reported as **"could not check"** rather than
a failure, because the automation could not get the value in at all. Asked for
30 June 2027, the field settled on **1 June 2027** — the calendar read back
empty, and every typed format was re-parsed into a different date
(`30/06/2027` came back as *January 1st, 2030*).

It is specific to this field, not to the date or to the automation: `Date Of
Suit Filing` took 30 June **2025**, `Date Of Decree` took 31 December **2027**,
and `Last Date of Hearing` took 15 January 2026 — all through the same
`widgets.date`, on the same form, in the same run. The authored value has
deliberately **not** been changed to something the field will swallow: a date
silently stored as another date is the defect this suite exists to surface, and
moving the value would hide it.

### A modal is invisible to everything that reads the screen

Verification of Litigation needed **its own comparison**, and two live runs
taught why. The grid shows five columns — *Type of Suit, Suit Number, Court
Name* and the two lawyer names — for a record of **ten** fields. So the first
live run reported the dates and the proceeding details as *lost* on a record
that plainly held them: four failures, none of them real.

The generic row-detail pass does not rescue it, and this is the part worth
remembering. `flows._screen_text` reads `[data-crawl-root]`, and a Bootstrap
modal is appended to the **body**, outside that root — so the pass opened the
record and then read the grid sitting behind it. It even reported *"opened 1
saved row(s)"* while looking at nothing.

The second attempt matched the run marker against the dialog's `innerText`,
and still failed — for a different reason worth knowing: **`innerText`
contains neither an `<input>`'s value nor anything inside a TinyMCE
`<iframe>`.** A dialog full of correctly restored values reads back as nothing
but its labels.

So `_verify_litigation` opens the record and reads it with **`value_of`**,
which knows how to read an input, an `ng-select`, a `ui-switch` and an iframe.
`_open_our_litigation` opens rows one at a time until `value_of` finds this
run's marker — so a case carrying several suits is never confused for this
one — and which field holds the marker is read off the pass rather than named
again, so moving it cannot silently break the search. With that, all nine
values came back: **16 passed / 0 failed**.

Observations needed its own branch for the same family of reasons; its list is
not a `<table>` at all.

### Fields are found by title OR by the label's text

`label[title="..."]` is what the obligor form emits on every field, and Phase 2
treated it as the universal anchor. It is not. On the Add Observation panel
**exactly one label of twelve carries a title** — the other eleven are a bare
`<label>Date of audit visit</label>` inside a `fieldset.form-group`. Anchoring
only on the title attribute therefore found `Title`, missed the other eleven,
and the run announced *7 passed / 0 failed / 0 blocked* on a form where one box
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

- **A read-only check types nothing** into the application. The only text a Phase 1 run
  enters anywhere is the password on the sign-in page — it even finds its record by paging
  the grid rather than using the grid's search box, and `driver.py` is asserted to contain
  no input-writing call at all. (Phase 2 types, by definition; that is gated separately
  below, and a case id typed into My Bucket's search box is gated with it.)
- **Destructive controls are unreachable.** Save, Submit, Approve, Reject, Delete, Post,
  Authorize, Bulk Action, Export and Sign out are all refused before a click happens.
- **Data entry is host-restricted.** Phase 2 refuses to run anywhere except the approved
  dev host, and refuses *before a browser opens*. An unset allowlist blocks everything
  rather than allowing everything.
- **Exactly one module writes.** `runner/widgets.py` is the only file that can type or
  commit; `flows.py` authors the sequence but must go through it. Two gates, both fail
  closed: the host allowlist, and a per-control allowlist that permits Save / Proceed /
  Add / OK and refuses Approve / Reject / Delete / Forward *even when a flow asks for
  them*. There is no "fill every field on the page" helper, and a control is located by
  its label rather than by position — with one documented exception, below.
- **The one place position is used, and what makes it safe.** Business Performance has no
  labelled fields at all: its row names are in one `<table>` and its input boxes in
  another, and a box carries no label, no `title`, no `formcontrolname` and a duplicate
  `id`. `Filler.grid_cell` therefore joins the two tables on row position — but the
  position is never the caller's. The caller names a metric; the name is looked up in the
  label table; and the crossing is refused unless the two tables have the same number of
  rows, the name matches exactly one row, and only one pairing of tables is possible.
  Every one of those refuses rather than guessing, because a guess here writes a figure
  into another metric's box.

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
`runner/results.py` | PASS / FAIL / BLOCKED result types |
`runner/run.py` | Orchestrates one read-only run — used by the CLI and the web page |
`runner/widgets.py` | **Phase 2.** The only module that types or commits. Both gates live here |
`runner/flows.py` | **Phase 2.** The authored create → raise → find → verify sequence |
`runner/case_flows.py` | **Phase 2b.** Request Details, Facilities, Observations, Litigation, Shariah Comments, CRMD Note, Credit Memorandum, Group Review, both Business Performance sub-menus and Relationship with Other Banks / FIs on an existing case, then the round trip |
`runner/exports.py` | The CSV and PDF downloads — one description of what a result contains |
`runner/cli.py` | Command line |
`tests/test_safety.py` | Proves the safety properties above |
`artifacts/` | Screenshots and reports, one folder per run |

Nothing in this folder modifies the crawler, FSD ingestion, or test-case generation in the
parent project.

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
- **The rest of the case menu.** Phase 2b fills Request Details, Facilities,
  Observations, Litigation, Shariah Comments, CRMD Note, Credit Memorandum,
  Group Review, both Business Performance sub-menus and Relationship with
  Other Banks / FIs. Collaterals, Facility Coverage, Financials, Conditions,
  Documents and the others are still read-only — each is a `Pass` list in
  `case_flows.py` rather than new code.
- **A cancel-flow test on the bank dialog.** `Filler.cancel_modal` exists and
  the dry run uses it; a test that asserts a cancelled dialog leaves no record
  behind has not been written.
- **A second limit row.** One row is added per relationship, which is what
  makes the aggregates unambiguous. Two rows would test the summing itself.
- **A second period on Business Performance.** Both sub-menus are filled
  against the period already on the case. The screen's own `+ Add` opens a new
  period, which would be a new column group to fill; nothing here presses it.
- **A second facility on one case.** The flow requests one facility and fills
  it; a case that needs two needs the button pressing twice.
- **Phase 4+** — the remaining menus, then the other modules.
