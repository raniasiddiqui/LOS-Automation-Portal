# Agentic Test-Case Generator

Generates detailed, reproducible QA test cases for an Angular web application by
combining **two independent sources of truth**:

| Source | What it knows | What it cannot know |
|---|---|---|
| **The running application** (crawled with Playwright) | What screens exist, what every field/button/modal is really called, what the grid columns are | *Why* any of it exists, who is allowed to do it, what the limits are |
| **The Functional Specification Document** (FSD) | Business processes, actors and authority levels, approval routing, field formats, mandatory flags, allowed values | Whether the app actually implements any of it, or what the controls are labelled |

Neither alone produces useful output. Crawl-only gives you *"click Cancel → changes
are discarded"*. FSD-only gives you steps a tester cannot follow because no control
is named. **Joining them** gives you *"On `/master/obligorCustomer`, click 'Create
Obligor'; in the 'Nature of Account' dropdown select 'Fixed Deposit' — the FSD
permits only Current, Savings, Fixed Deposit, Call Deposit."*

---

## The flow

```
                    ┌──────────────────────────────────────────┐
   Live app  ──────▶│ crawler.py      Playwright, logs in,     │
                    │                 clicks through the menu  │
                    └────────────────────┬─────────────────────┘
                                         │ output/site_map.json
                    ┌────────────────────▼─────────────────────┐
                    │ parser.py       HTML → structured        │
                    │                 forms/fields/buttons     │
                    └────────────────────┬─────────────────────┘
                                         │
                    ┌────────────────────▼─────────────────────┐
   FSD.docx ───────▶│ fsd_ingest.py   tables → field specs     │
                    │                 prose  → processes       │
                    └────────────────────┬─────────────────────┘
                                         │
                    ┌────────────────────▼─────────────────────┐
                    │ knowledge_graph.py   SQLite. Screens AND │
                    │                      FSD in ONE graph    │
                    └────────────────────┬─────────────────────┘
                                         │
                    ┌────────────────────▼─────────────────────┐
                    │ fsd_grounding.py  match FSD steps ↔      │
                    │                   real screens           │
                    └────────────────────┬─────────────────────┘
                                         │
                    ┌────────────────────▼─────────────────────┐
                    │ testcase_generator.py  LLM, both sources │
                    └────────────────────┬─────────────────────┘
                                         │
                              output/test_cases.xlsx
```

### Run order

```bash
pip install -r requirements.txt
playwright install chromium

python pipeline.py --recrawl              # 1. crawl + parse + index
python fsd_ingest.py --file FSD.docx      # 2. read the FSD
python fsd_grounding.py --explain         # 3. join the two
python testcase_generator.py              # 4. generate the workbook
```

Steps 1 and 2 are independent — either order. Step 3 needs both. Re-run 3 and 4
freely; they are cheap relative to 1 and 2.

---

## Stage 1 — `crawler.py`: exploring the live app

An Angular SPA cannot be crawled by fetching URLs. `page.goto()` re-bootstraps the
app with no in-app state, and the router bounces deep links back to the default
route. An early version of this crawler recorded 25 distinct URLs whose HTML was
only 14 distinct documents — `/master/obligorCustomer` was rendering `<app-bucket>`.

So the crawler behaves like a user:

1. **Logs in**, then reads the sidebar once to build a menu inventory.
2. **Navigates by clicking** menu entries so Angular routes client-side and the app
   stays alive. It sets a `window.__crawlMarker` before each click and checks it
   survived — a silent full-page reload is reported, not hidden.
3. **Finds the real screen content** by walking to the element following the
   *innermost* `<router-outlet>`, which is where Angular renders the active page
   component:

   ```html
   <router-outlet></router-outlet><app-master>
       <app-navbar>…</app-navbar>          <!-- shell -->
       <app-sidebar>…</app-sidebar>        <!-- shell -->
       <router-outlet></router-outlet><app-bucket>   <!-- the actual screen -->
   ```

   Guessing at `.content-body` / `.content-wrapper` does not work here: several
   nodes match `.content-body` and the first is not the routed one, while
   `.content-wrapper` is *wider* than the screen and drags the navbar in.
4. **Fingerprints each screen** (component tags including ancestors, headings, table
   headers, field names). Two URLs producing the same fingerprint are reported as
   duplicates. A fingerprint that carries too little information is treated as
   *unreadable*, not as a duplicate — otherwise an unreadable screen silently
   masquerades as one already seen.
5. **Clicks the screen's own action controls** to capture what they open. This is
   where the business functionality lives: `Create Obligor`, `Raise Query`,
   `Apply Filter`. Four kinds of resulting state are recognised — `modal`,
   `sub_route` (the button *navigates* rather than opening a dialog),
   `inline_panel`, `expanded_form` — and each is stored as its own record with its
   own fields. Icon-only controls are picked up via their icon class
   (`<i class="ft-plus">` → `icon:plus`).
6. **Records `/api/` traffic**, including response bodies, because this app is
   config-driven (`getDynamicFields`, `menuButtons`, `workflowWithColumns`).

### Safety

This can be pointed at a production banking system. Every click is checked against
a denylist **before** an allowlist, and the denylist always wins:

- **Clicked:** Add, New, Create, Raise, Filter, Search, View, Detail, Expand,
  Select options, Columns, Preview
- **Never clicked:** Save, Submit, Approve, Reject, Delete, Remove, Post, Confirm,
  Proceed, Authorize, Verify, Release, Withdraw, Update, Edit, Bulk Action,
  Execute, Print, Export, Download, Upload, Sign out

Native dialogs are auto-dismissed and file pickers swallowed, so nothing can block
the run or commit a change. `QA_ALLOW_INTERACTION=false` disables clicking entirely.

**Prefer a UAT environment over production.**

---

## Stage 2 — `parser.py`: HTML → structure

Turns each captured screen (and each captured modal) into forms, fields, buttons,
tables and links.

Two things it handles that a naive parser does not:

- **Angular reactive forms bind to `[formGroup]` divs, not `<form>`.** Restricting
  field extraction to `<form>` descendants loses most real inputs — on one screen it
  found 3 fields where there were 20. `parse_unformed_fields` picks up anything with
  a `formControlName`.
- **This app nests `<app-navbar>` inside each page component.** So even a correctly
  scoped capture still contains the shell, and every screen was being credited with
  `Preferences`, `Sign out` and the navbar's `Explore Stack…` search box.
  `_strip_shell()` removes those subtrees with lxml before parsing.

---

## Stage 3 — `fsd_ingest.py`: reading the FSD

Supports `.docx`, `.pdf`, `.md`, `.txt`. **Two passes, because an FSD contains two
very different kinds of content.**

### Pass A — field-specification tables (deterministic, no LLM)

Most of this FSD is a data dictionary, not prose. Roughly 50 of its 65 tables share
one header:

```
Field Name | Type | Mandatory | Values / Format
```

Those tables hold **56% of the document's text**. Asking an LLM for "business
processes" over them correctly returns nothing — which is how more than half the
document was originally discarded, with 5 of 9 chunks yielding zero.

Because the header is consistent, they are parsed **deterministically**: total
recall, no hallucinated fields, zero token cost. On the reference FSD this extracts
**677 fields across 48 sections — 170 mandatory, 101 with enumerated values.**

Enumerations are split, format masks are not:

| Cell value | Parsed as |
|---|---|
| `Current; Savings; Fixed Deposit` | 3 allowed values |
| `CBG, SME, Agriculture, FI` | 4 allowed values |
| `NNNNN-YYYY (e.g. 50651-2025)` | *not* an enum — a format |
| `Free text`, `13 digits` | *not* an enum |

A `Sub Menu` column, where present, is captured as the tab a field lives on
(Collaterals lists 115 fields across 8 tabs), and dedupe keys on `(tab, field)` so
the same label on different tabs is not dropped.

### Pass B — prose (LLM)

The remaining narrative is chunked at ~4,000 chars and each chunk is asked for
business processes: name, module, actors, preconditions, ordered steps
(actor / action / screen hint / data / expected), business rules, validations with
valid+invalid examples, alternate flows, outcomes.

- Boilerplate (revision history, glossary, ToC, sign-off) is filtered before any
  call.
- A chunk that fails to parse is **split in half and retried** rather than dropped.
- The run reports which chunks *failed* versus which legitimately *found nothing*,
  so silent loss is visible.

```bash
python fsd_ingest.py --file FSD.docx --dry-run       # section/chunk breakdown, no LLM
python fsd_ingest.py --file FSD.docx --limit-chunks 3  # cheap trial
python fsd_ingest.py --file FSD.docx --specs-only    # tables only, free, keeps processes
```

---

## Stage 4 — `knowledge_graph.py`: one graph, two sources

SQLite. Crawl side: `pages`, `elements` (form/field/button/link/table/state),
`edges`, `api_calls`. FSD side: `fsd_processes`, `fsd_steps`, `fsd_rules`,
`fsd_field_specs`. Join tables: `fsd_links` (step → screen), `fsd_spec_links`
(spec section → screen).

Keeping both in *one* graph is what makes the join — and the gap reporting —
possible.

---

## Stage 5 — `fsd_grounding.py`: matching spec to reality

For each FSD step, score every crawled screen and modal; keep the best match.

**Deliberately deterministic — weighted token overlap, no LLM.** It is auditable
(every link stores its evidence), free to re-run after each crawl, and a wrong match
is diagnosable from the score breakdown. An LLM here would be slower, costlier and
much harder to debug when it silently mismatches.

| Signal | Weight |
|---|---|
| screen hint vs menu label / title / URL path | 0.45 |
| step's data fields vs the screen's real field names | 0.25 |
| action verb vs the screen's real button labels | 0.20 |
| module vs URL path | 0.10 |

Plus a bonus when a modal's trigger label is named by the step, so "click Raise
Query" matches the *dialog* rather than its parent screen. Tokenising splits
camelCase and kebab-case (`obligorCustomer` → `{obligor, customer}`), and a
`SYNONYMS` map absorbs the FSD calling things by other names
(`obligor`/`customer`/`borrower`) — extend it as you find mismatches.

Field-spec sections are grounded separately, weighted toward field-name overlap
since a data dictionary has no action.

**Both gaps are reported**, and this is genuinely useful output on its own:

- FSD steps with no screen → feature missing, or the crawler cannot reach it
- Screens with no FSD coverage → undocumented functionality

```bash
python fsd_grounding.py --explain --threshold 0.30
```

Tune `--threshold` after the first real run: too many gaps → lower it or add
synonyms; steps matching the wrong screen → raise it. Runners-up are stored in
`fsd_links` for exactly this diagnosis.

---

## Stage 6 — `testcase_generator.py`: how test cases are actually made

Two generators, both fed **both sources**.

### Page-level cases — one screen at a time

The prompt receives the screen's real fields, buttons, tables and captured modals,
**plus `fsd_context`**: the business rules, validations and data-dictionary fields
grounded to that screen. When `fsd_context` is present it is authoritative and
drives the cases:

- one case per mandatory field (submitting without it must be refused)
- one case per enumerated field (each permitted value accepted, an outside value
  rejected — named explicitly)
- one case per format mask (`NNNNN-YYYY`, `13 digits`: conforming accepted,
  malformed rejected)
- `valid_examples` / `invalid_examples` used **verbatim** — they are real test data,
  and substituting invented values destroys their worth

### Workflow cases — one FSD business process at a time

**FSD processes *are* the workflows.** A link-graph BFS is only used as a fallback
when no FSD is loaded, because every screen links to all ~14 sidebar targets, so
those "paths" are combinatorial noise rather than user journeys.

Each step in the payload carries its grounded screen inventory — URL, the button
that opens it, real field labels with types/required/maxlength/dropdown options,
real button labels. Only matches at or above the grounding threshold are passed
through; a below-threshold near-miss arrives as `null` so the model marks a coverage
gap instead of being told a screen exists when it does not.

Generated cases cover: the happy path end to end; each business rule enforced and
violated; role/authority cases where there are multiple actors (a Maker must not
complete a Checker's step); each alternate flow; field validations; and state
transitions after the step that changes them.

### What makes the output reproducible

Every case carries `test_id`, `title`, `description`, `type`, `priority`,
`preconditions`, `test_data`, `steps`, `expected_result`, `postconditions`. The
prompts require one concrete action per step, naming the real control and the exact
value:

> **Good:** `In the 'Account Number' field, enter '0010012345678'.`
> **Good:** `Leave the 'Nature of Account' dropdown unselected and click 'Save'.`
> **Bad:** `Enter invalid data.` · `Verify the field works.`

`expected_result` must state the observable outcome — exact message text where
known, resulting record status, which screen is shown. "Works as expected" is
explicitly disallowed.

### Output: `output/test_cases.xlsx`

| Sheet | Contents |
|---|---|
| **Page-Level Test Cases** | per-screen, `PG-####` |
| **Workflow Test Cases** | per FSD process, `WF-####`, with actor / business_rule / process / module / workflow_path |
| **Coverage Gaps** | ungrounded FSD steps and undocumented screens |

Prose columns wrap at width 55; header row frozen with auto-filter.

---

## Token limits (Groq)

Groq charges **prompt + `max_tokens`** against a per-minute allowance, and the free
`on_demand` tier is small — 8,000 TPM for `openai/gpt-oss-120b`, 12,000 for
`llama-3.3-70b-versatile`. Two consequences:

1. **`GEN_MAX_TOKENS` cannot be set generously.** Asking for 32,000 output fails
   with 413 no matter how short the prompt. Default is 5,000.
2. **Prompt payloads must stay small.** `_slim_fields` / `_slim_state` strip
   Angular's `_ngcontent-ng-c…`/`ng-reflect-*` attribute noise, which alone had made
   one screen's payload 47,000 tokens. After slimming, the worst screen is ~4,900
   and the worst workflow ~5,900.

`llm.py` clamps `max_tokens` to each model's real ceiling, halves it and retries on
a per-minute 413, and fails fast with a clear message on a per-day 429 (waiting
hours mid-run helps nobody).

`TokenRateLimiter` additionally paces calls client-side inside a rolling 60-second
window, at 85% of `TPM_BUDGET`, so requests queue instead of failing. **Pacing is on
by default at 12,000**, which matches `llama-3.3-70b-versatile`. If you switch
generation to `openai/gpt-oss-120b` you must lower it to 8,000 — that model has the
tightest limit of the two, which makes it a poor fit for this stage despite its
larger output ceiling.

```bash
python testcase_generator.py --tpm-budget 8000 --max-tokens 4000   # gpt-oss-120b
python testcase_generator.py --tpm-budget 0                        # Anthropic / paid tier
```

Note `llama-3.3-70b-versatile` also has a **100,000 tokens/day** cap. A full run is
roughly 30 calls — plan on batching across days (`--skip-workflows` one day,
workflows the next), upgrading the tier, or pointing generation at Anthropic, where
this class of throttling largely disappears.

---

## Configuration

All settings live in `config.py` and are overridable from `.env`. **Never commit
`.env`** — it holds application credentials and API keys.

### Required

| Variable | Purpose |
|---|---|
| `QA_BASE_URL` | Application root |
| `QA_USERNAME`, `QA_PASSWORD` | Login credentials |
| `GROQ_API_KEY` *or* `ANTHROPIC_API_KEY` | LLM access |
| `QA_FSD_FILE` | Path to the FSD |

### Commonly adjusted

| Variable | Default | Notes |
|---|---|---|
| `QA_LOGIN_USER_SELECTOR` etc. | `#username`, `#password`, `button[type=submit]` | Per-app login selectors |
| `QA_MAX_PAGES` | 25 | Screen cap |
| `QA_HEADLESS` | `true` | `false` to watch the crawl |
| `QA_ALLOW_INTERACTION` | `true` | `false` for a zero-click crawl |
| `QA_MAX_ACTION_STATES` | 15 | Controls clicked per screen |
| `QA_GROUNDING_THRESHOLD` | 0.25 | Match score to count as covered |
| `QA_LLM_PROVIDER` | `groq` | `groq` or `anthropic` |
| `QA_LLM_MODEL` | `llama-3.3-70b-versatile` | FSD ingestion |
| `QA_GEN_LLM_MODEL` | = `QA_LLM_MODEL` | Generation, set separately |
| `QA_GEN_MAX_TOKENS` | 5000 | See token limits above |
| `QA_TPM_BUDGET` | 12000 | Client-side pacing; match your model's TPM, 0 to disable |

Ingestion and generation are configured separately on purpose: ingestion is
input-heavy and wants faithfulness to the FSD's exact wording, generation is
output-heavy.

---

## Files

| File | Role |
|---|---|
| `config.py` | All settings, `.env`-backed |
| `crawler.py` | Playwright exploration of the live app |
| `parser.py` | Captured HTML → structured elements |
| `fsd_ingest.py` | FSD → field specs + business processes |
| `knowledge_graph.py` | SQLite store for both sources |
| `fsd_grounding.py` | Deterministic spec ↔ screen matching |
| `testcase_generator.py` | Prompting, generation, Excel output |
| `llm.py` | Shared LLM client: clamping, JSON repair, rate-limit handling |
| `pipeline.py` | Orchestrates crawl → parse → index |

### Outputs

| File | Contents |
|---|---|
| `output/site_map.json` | Raw crawl: per-screen HTML, states, API traffic |
| `output/knowledge.db` | The knowledge graph |
| `output/fsd_processes.json` | Extracted business processes |
| `output/fsd_field_specs.json` | Extracted data dictionary |
| `output/fsd_coverage.json` | Grounding gap report |
| `output/test_cases.xlsx` | **The deliverable** |

---

## Troubleshooting

**All screens look identical / duplicates skipped.** Check the crawl's
`Distinct signatures: N/N` line. If signatures are weak, the `? weak signature for …
(root=… via fallback-scored)` line names the container it fell back to — that screen
does not follow the router-outlet pattern.

**Few action states captured.** The crawler explores one level deep. Deep data-entry
screens behind a create flow are not reached, so their FSD steps land in Coverage
Gaps. Recursive exploration into those forms is the natural next step; the FSD's
`sub_menu` values give the tab names to look for.

**413 Request Entity Too Large.** `prompt + max_tokens` exceeds the tier's TPM.
Lower `--max-tokens` or pass `--tpm-budget`.

**429 with a multi-hour wait.** Daily quota exhausted. Switch model, upgrade, or
resume tomorrow.

**Low grounding coverage.** Usually genuine — the crawl reached fewer screens than
the FSD documents. Confirm with `--explain` before lowering the threshold.
