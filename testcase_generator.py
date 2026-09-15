"""
Reads the knowledge base built by pipeline.py (pages + forms/fields/
buttons/tables + navigation edges + API calls), asks an LLM to generate
test cases from it, and writes everything to an Excel workbook with two
sheets:

  - "Page-Level Test Cases"  — one page's UI understood in isolation
    (field validation, required-field checks, button behavior, table
    presence, etc.)
  - "Workflow Test Cases"    — multi-page end-to-end scenarios, built by
    walking the navigation edges the crawler discovered (e.g. login ->
    dashboard -> raise a query), so you get scenario-level coverage too,
    not just per-screen coverage.

Requires: pip install anthropic pandas openpyxl
Reads your API key from the ANTHROPIC_API_KEY environment variable.
"""
import argparse
import json
import os
import re
import threading
import time
from collections import deque
from typing import Optional

import pandas as pd
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.utils import get_column_letter

import config
import knowledge_graph as ks
import llm

PAGE_LEVEL_SYSTEM_PROMPT = """You are a senior QA engineer generating test cases for a banking \
risk-management web application (Angular frontend). You will be given a structured, \
machine-extracted inventory of ONE screen: its forms, fields (with type/required/maxlength/ \
pattern where known), buttons, tables, and any network calls the screen fires.

Generate a thorough but non-redundant set of test cases covering:
- Positive/happy-path cases for each form
- Required-field validation (submit with each required field empty)
- Boundary/invalid-input cases where maxlength/pattern/type give you something to test
  (e.g. maxlength=100 -> test at/over the limit; type=password -> masking; email fields ->
  malformed email)
- Button/action behavior (including disabled states you can reasonably infer, e.g. a submit
  button should be disabled until required fields are filled, if the fields are marked required)
- Table/data-display sanity checks if tables are present (empty state, pagination if implied)
- Security-relevant checks ONLY at the level of "verify the app rejects X" (e.g. SQL-injection-
  looking input in a text field should be rejected/sanitized, not accepted) — do not include
  exploit payloads, just name the check.

The inventory may also include "fsd_context" — what the Functional Specification Document says
about THIS screen:
  - business_rules: limits, authority levels, routing and sequencing rules
  - validations: field rules with valid_examples and invalid_examples
  - field_specs: the FSD's data dictionary for this screen, each field with its type,
    mandatory flag, format, and allowed_values (enumerations)

When fsd_context is present it is AUTHORITATIVE and takes priority over anything you infer
from the HTML:
- Write a case for every mandatory field in field_specs: submitting without it must be refused.
- Write a case for every field with allowed_values: each permitted value is accepted, and a
  value outside the list is rejected. Name the values explicitly.
- Write a case for every format in values_format (e.g. "NNNNN-YYYY", "13 digits"): one
  conforming value accepted, one malformed value rejected.
- Use valid_examples / invalid_examples verbatim. They are real test data; inventing
  substitutes destroys their value.
- Prefer an FSD-derived case over a generic UI case when both cover the same control.

WRITE FOR A TESTER WHO HAS NEVER SEEN THIS APPLICATION. Every step must be one concrete
action naming the real control and the exact value:
  GOOD: "In the 'Account Number' field, enter '0010012345678'."
  GOOD: "Leave the 'Nature of Account' dropdown unselected and click 'Save'."
  BAD:  "Enter invalid data." / "Verify the field works."

Do NOT invent fields, buttons, or business rules that are not present in the given inventory.
If the inventory is sparse (e.g. only a login form), keep the output focused — do not pad with
speculative cases about screens you cannot see.

Return ONLY a JSON array (no prose, no markdown fences). Each element:
{
  "title": "specific and outcome-oriented, e.g. 'Reject an Account Number shorter than 13 digits' — never a bare control name like 'Search field'",
  "description": "2-3 sentences: what this verifies and why it matters",
  "type": "positive | negative | boundary | security | ui",
  "priority": "High | Medium | Low",
  "preconditions": "the exact state required before step 1, including who is logged in and what records must exist",
  "test_data": "the exact values this case uses, or empty",
  "steps": ["1. ...", "2. ...", "..."],
  "expected_result": "the observable outcome: exact message text if known, resulting status, which screen is shown. Never just 'works as expected'.",
  "postconditions": "the state the system is left in, or empty",
  "related_element": "name/label of the field or button this targets, or 'page' for page-level checks"
}
"""

WORKFLOW_SYSTEM_PROMPT = """You are a senior QA engineer generating END-TO-END test cases for a \
banking risk-management web application (Angular frontend). You will be given an ordered \
sequence of screens representing one navigable path through the app (screen 1 links to screen 2 \
links to screen 3, etc.), each with its forms/fields/buttons.

Generate test cases that exercise the FULL PATH as a single user journey — not per-screen checks \
(those are handled elsewhere). Focus on:
- The happy path from first screen to last
- What happens if a required step is skipped or fails partway through (e.g. login fails — does
  the flow correctly stop before reaching later screens?)
- State/data that should carry across screens if the inventory suggests it (e.g. a value entered
  on screen 1 that a later screen's form seems to depend on)
- Session/permission-relevant checks if the path involves a login as its first step

Do NOT invent screens, fields, or business logic not present in the given sequence.

Return ONLY a JSON array (no prose, no markdown fences), same schema as page-level cases plus a
"workflow_path" field naming the screens involved in order:
{
  "title": "short imperative title",
  "type": "positive | negative | boundary | security | ui",
  "priority": "High | Medium | Low",
  "preconditions": "string, can be empty",
  "steps": ["step 1", "step 2", "..."],
  "expected_result": "string",
  "workflow_path": ["url1", "url2", "..."]
}
"""

FSD_WORKFLOW_SYSTEM_PROMPT = """You are a senior QA engineer writing END-TO-END test cases for a \
banking credit-risk application. You have TWO sources and MUST use BOTH in every case:

SOURCE 1 - the Functional Specification Document (what the system is SUPPOSED to do):
  name, module, actors, preconditions, outcomes; ordered steps with actor/action/expected;
  business_rules (limits, authority levels, routing, sequencing); validations with valid and
  invalid example values; alternate_flows (rejection, return, timeout).

SOURCE 2 - the LIVE APPLICATION as crawled (what the system ACTUALLY presents):
  each step carries "screen", the real screen found for it, containing:
    url, opened_by (the button that opens it, if it is a modal), fields (real labels, input
    types, required flags, maxlength, dropdown options), buttons (real clickable labels),
    tables (real column headers).
  A step with "screen": null has NO screen found — the FSD describes it but the crawl never
  reached it.

FUSE THEM. The FSD tells you WHAT to verify and with WHICH data; the crawl tells you WHERE to
click and WHAT the control is really called. A case that uses only one source is a failure:
- Never write "enter the obligor details" — name the actual field label from "fields".
- Never invent a button — use the exact label from "buttons".
- Never invent a value when the FSD gives allowed_values or valid/invalid examples.

WRITE FOR A TESTER WHO HAS NEVER SEEN THIS APPLICATION. Every case must be executable by
someone following it literally, with no prior knowledge and no guessing.

Each step must be ONE concrete action, written as: navigate where, act on which named control,
with what exact value. Include the screen URL the first time you arrive at a screen.
  GOOD: "On http://host/riskNucleus/master/obligorCustomer, click the 'Create Obligor' button
         in the page header."
  GOOD: "In the 'Nature of Account' dropdown, select 'Fixed Deposit'."
  GOOD: "In the 'Account Number' field, enter '0010012345678' (13 digits)."
  BAD:  "Enter valid data and submit."
  BAD:  "Verify the functionality works."

Cover, in priority order:
1. The happy path end to end, walking every grounded step in sequence.
2. Each business rule: a case proving it is enforced, and where meaningful one proving a
   violation is rejected. State the concrete limit, authority level or status.
3. Role/authority cases when there is more than one actor (a Maker must not complete a
   Checker's step; an unauthorised role must be refused).
4. Each alternate flow as its own case.
5. Field validations using the FSD's own valid_examples and invalid_examples verbatim.
6. State transitions: assert the record's status after the step that changes it.

Return ONLY a JSON array (no prose, no markdown fences). Each element:
{
  "title": "specific and outcome-oriented, e.g. 'Reject a facility request whose amount exceeds the RM approval limit' — never a bare control name",
  "description": "2-3 sentences: what this verifies, why it matters, and which business rule or spec section it comes from",
  "type": "positive | negative | boundary | security | ui | permission",
  "priority": "High | Medium | Low",
  "actor": "the role performing this case, e.g. 'Maker (Relationship Manager)'",
  "preconditions": "numbered list as one string: the exact state required before step 1, including who is logged in, what records must already exist, and their status",
  "test_data": "the exact field-by-field values this case uses, e.g. 'Obligor Name: ACME Textiles Ltd | Segment: CBG | Amount: 50,000,000'",
  "steps": ["1. On <url>, ...", "2. In the '<field>' field, enter '<value>'.", "..."],
  "expected_result": "the observable outcome per checkpoint: exact message text if known, the resulting record status, which screen is shown, and what changed. Never just 'works as expected'.",
  "postconditions": "the state the system is left in, so the next tester can reset it",
  "business_rule": "the FSD rule this case verifies, quoted, or empty",
  "workflow_path": ["screen url or name in order"],
  "coverage_gap": false
}
Set "coverage_gap": true when the case relies on a step whose "screen" is null, and say so in
the description.
"""



# Set from --max-tokens in main(); every generation call reads it so the flag
# doesn't have to be threaded through each generate_* signature.
_GEN_MAX_TOKENS: Optional[int] = None


# ---------------------------------------------------------------------------
# Token-per-minute pacing
#
# Groq enforces a rolling TPM cap per model — e.g. 12,000 TPM on the free
# tier for llama-3.3-70b-versatile, counting input AND output tokens
# together. GEN_MAX_TOKENS defaulting to 32000 means a single call's
# *requested* output ceiling alone is already ~3x that whole budget, and the
# generation loop in main() fires one call per page plus one per FSD process
# back-to-back with no pacing — so the first call or two burns the entire
# minute's allowance and every call after it 429s until the window rolls.
#
# This limiter tracks tokens used in a trailing 60s window and sleeps before
# dispatching a call that would push it over budget, instead of firing and
# eating a 429 + blind exponential backoff inside llm.py's retry logic.
# Gating uses a *bounded* pre-call estimate (input + up to 6000 tokens of
# typical output), not the full max_tokens ceiling — otherwise a 32000
# ceiling would make the limiter conclude no call could ever fit under a
# 12000 budget and stall forever. It then corrects the ledger with the real
# response size after the call returns.
#
# Set TPM_BUDGET = 0 (or leave it unset) in config, or pass --tpm-budget 0,
# to disable pacing entirely — e.g. once GEN_LLM_MODEL points at Anthropic
# instead of Groq, where this class of throttling mostly goes away.
# ---------------------------------------------------------------------------
class TokenRateLimiter:
    def __init__(self, tpm_budget: Optional[int], window_seconds: float = 60.0,
                 safety_margin: float = 0.85):
        self.budget = int(tpm_budget * safety_margin) if tpm_budget else None
        self.window = window_seconds
        self._events: deque[tuple[float, int]] = deque()
        self._lock = threading.Lock()

    def _used(self, now: float) -> int:
        while self._events and now - self._events[0][0] > self.window:
            self._events.popleft()
        return sum(t for _, t in self._events)

    def wait_for_budget(self, estimated_tokens: int):
        if self.budget is None:
            return
        while True:
            now = time.monotonic()
            used = self._used(now)
            if used + estimated_tokens <= self.budget or not self._events:
                return  # fits, or nothing to wait on (a lone call bigger than budget)
            wait = self.window - (now - self._events[0][0]) + 0.1
            print(f"    [rate-limit] {used}/{self.budget} TPM used — "
                  f"pacing {wait:.1f}s before next call...")
            time.sleep(max(wait, 0.5))

    def record(self, tokens_used: int):
        if self.budget is None:
            return
        with self._lock:
            self._events.append((time.monotonic(), tokens_used))


_RATE_LIMITER: Optional[TokenRateLimiter] = None


def _estimate_tokens(*texts: str) -> int:
    """Rough ~4 chars/token heuristic — good enough for pacing decisions,
    doesn't need to be exact."""
    return sum(len(t) for t in texts if t) // 4


def _call_llm(client, model: str, system: str, user_content: str,
              retries: int = 3, max_tokens: Optional[int] = None) -> list[dict]:
    """
    Delegates to llm.py so generation gets the same protections as ingestion:
    the per-model output clamp, JSON-repair retries that feed the parse error
    back, full (untruncated) provider messages, and no wasted backoff on
    non-retryable 400s.

    The previous hardcoded max_tokens=4096 was the real constraint here. A
    detailed case runs 250-400 tokens, so a screen worth 25 cases had its JSON
    array truncated mid-object, the parse failed, and the page silently
    produced nothing.
    """
    ceiling = (max_tokens or _GEN_MAX_TOKENS or getattr(config, "GEN_MAX_TOKENS", 8000))

    if _RATE_LIMITER is not None:
        pre_estimate = _estimate_tokens(system, user_content) + min(ceiling, 6000)
        _RATE_LIMITER.wait_for_budget(pre_estimate)

    result = llm.complete_json(
        client, system, user_content, expect="array", model=model,
        max_tokens=ceiling,
        retries=retries,
    )
    if isinstance(result, dict):
        # Some models wrap the array in {"test_cases": [...]}; unwrap rather
        # than discarding a perfectly good response.
        for key in ("test_cases", "cases", "testcases", "results", "items"):
            if isinstance(result.get(key), list):
                result = result[key]
                break
        else:
            result = [result]
    elif not isinstance(result, list):
        result = []

    if _RATE_LIMITER is not None:
        # Correct the ledger with the real response size so the next
        # wait_for_budget() call works from actual numbers, not the
        # worst-case pre-estimate.
        actual = _estimate_tokens(system, user_content, json.dumps(result))
        _RATE_LIMITER.record(actual)

    return result


def _slim_fields(fields, limit: int = 40) -> list[dict]:
    """
    Keep only what a test author needs. The parsed form data carries each
    field's entire attribute dict, which on this Angular app means
    _ngcontent-ng-c*/ng-reflect-* noise — that alone made one screen's payload
    188,000 chars (~47k tokens), over any Groq per-minute allowance.
    """
    out = []
    for f in fields or []:
        label = f.get("label") or f.get("placeholder") or f.get("name")
        if not label:
            continue
        item = {"label": label, "type": f.get("type")}
        for key in ("required", "maxlength", "pattern"):
            if f.get(key):
                item[key] = f[key]
        if f.get("options"):
            item["options"] = [o.get("text") for o in f["options"] if o.get("text")][:15]
        out.append(item)
    return out[:limit]


def _slim_state(state: dict) -> dict:
    fields = list(state.get("fields_outside_forms") or [])
    for form in state.get("forms") or []:
        fields.extend(form.get("fields") or [])
    return {
        "opened_by": state.get("trigger_label"),
        "kind": state.get("kind"),
        "dialog_title": state.get("title") or "",
        "fields": _slim_fields(fields),
        "buttons": [b.get("text") for b in (state.get("buttons") or []) if b.get("text")][:20],
        "tables": [t.get("headers") for t in (state.get("tables") or []) if t.get("headers")][:3],
    }


def _bundle_is_worth_testing(bundle: dict) -> bool:
    return bool(bundle["forms"] or bundle["standalone_buttons"] or bundle["tables"]
                or bundle.get("fields_outside_forms") or bundle.get("states"))


def generate_page_level_cases(client, model: str, bundle: dict,
                              fsd_context: Optional[dict] = None) -> list[dict]:
    if not _bundle_is_worth_testing(bundle):
        return []
    form_fields = []
    for form in bundle["forms"]:
        form_fields.extend(form.get("fields") or [])

    payload = {
        "url": bundle["url"],
        "title": bundle["title"],
        "fields": _slim_fields(form_fields + list(bundle.get("fields_outside_forms") or []), 60),
        "buttons": [b.get("text") for b in bundle["standalone_buttons"] if b.get("text")][:25],
        "tables": [t.get("headers") for t in bundle["tables"] if t.get("headers")][:5],
        # The modals/panels each button opens, with their own forms — this is
        # where the actual business functionality is, so it is the single most
        # important part of the payload for producing non-generic cases.
        "action_states": [_slim_state(s) for s in (bundle.get("states") or [])],
        # URL + method only. Response bodies are useful for understanding the
        # app's config but would dwarf everything else in the prompt.
        "api_calls": [{"method": c.get("method"), "url": c.get("url")}
                      for c in bundle["api_calls"]][:25],
    }
    # FSD rules that ground to THIS screen. Without them the model can only
    # infer constraints from HTML attributes; with them it can assert the
    # actual limits, statuses and authority levels the spec mandates.
    if fsd_context and (fsd_context.get("business_rules") or fsd_context.get("validations")
                        or fsd_context.get("field_specs")):
        payload["fsd_context"] = fsd_context

    cases = _call_llm(
        client, model, PAGE_LEVEL_SYSTEM_PROMPT,
        json.dumps(payload, indent=2),
    )
    for c in cases:
        c["page_url"] = bundle["url"]
        c["page_title"] = bundle["title"]
    return cases


def _screen_inventory(conn, page_url: str, state_trigger: Optional[str]) -> dict:
    """
    The real, crawled contents of one screen (or of one modal on it), trimmed
    to what a test author needs. This is SOURCE 2 in the workflow prompt —
    without it the model knows a step happens on some URL but not what any
    control is called, which is exactly what made earlier steps unfollowable.
    """
    try:
        b = ks.get_page_bundle(conn, page_url)
    except KeyError:
        return {}

    # A modal state is far more specific than its parent screen; when the step
    # was grounded to one, describe THAT rather than the whole page.
    if state_trigger:
        for st in b.get("states", []):
            if (st.get("trigger_label") or "").lower() == state_trigger.lower():
                return {"url": page_url, **_slim_state(st)}

    fields = list(b.get("fields_outside_forms", []))
    for form in b["forms"]:
        fields.extend(form.get("fields", []))
    return {
        "url": page_url,
        "opened_by": None,
        "title": b.get("title") or "",
        "fields": _slim_fields(fields),
        "buttons": [x.get("text") for x in b["standalone_buttons"] if x.get("text")][:20],
        "tables": [t.get("headers") for t in b["tables"] if t.get("headers")][:3],
        "opens_dialogs": [s.get("trigger_label") for s in b.get("states", [])
                          if s.get("trigger_label")][:12],
    }


def generate_fsd_workflow_cases(client, model: str, process: dict,
                                threshold: float, conn=None) -> list[dict]:
    """
    Generate cases for one FSD business process, with its steps already
    grounded to real screens. This is the unit that produces workflow-level
    coverage — the previous approach walked sidebar links, but every screen
    links to all 14 menu targets, so those "paths" were combinatorial noise
    rather than user journeys.
    """
    # Keep only the best-scoring grounding per step; runners-up are stored for
    # diagnosis but would just be noise in the prompt.
    #
    # The threshold matters here, not merely score > 0: every step scores
    # SOMETHING against SOME screen (a step naming a non-existent "Document
    # Vault" still scored 0.10 against an approvals screen). Passing those
    # through as grounded would tell the model a screen exists when it does
    # not — the exact false positive the coverage report exists to surface.
    slim_steps = []
    screens_seen: set[tuple] = set()
    for s in process["steps"]:
        best = s["grounded_to"][0] if s["grounded_to"] else None
        grounded = best if best and best["score"] >= threshold else None

        screen = None
        if grounded and conn is not None:
            key = (grounded["page_url"], grounded["state_trigger"])
            inv = _screen_inventory(conn, grounded["page_url"], grounded["state_trigger"])
            if inv:
                # Repeat visits to the same screen carry only a reference, so a
                # ten-step process doesn't send the same field list ten times.
                screen = inv if key not in screens_seen else {
                    "url": inv["url"], "opened_by": inv.get("opened_by"),
                    "same_as_earlier_step": True}
                screens_seen.add(key)

        slim_steps.append({
            "seq": s["seq"], "actor": s["actor"], "action": s["action"],
            "expected": s["expected"], "data": s["data"],
            "screen_hint": s["screen_hint"],
            "screen": screen,
            "match_confidence": round(grounded["score"], 2) if grounded else None,
        })

    # Data-dictionary fields for every screen this process touches: mandatory
    # flags, formats and enumerated values, straight from the FSD's tables.
    field_specs = []
    if conn is not None:
        urls = {s["screen"]["url"] for s in slim_steps if s["screen"]}
        sections = set()
        for url in urls:
            for r in conn.execute(
                    "SELECT section FROM fsd_spec_links WHERE page_url = ? AND score >= ?",
                    (url, threshold)):
                sections.add(r["section"])
        field_specs = ks.field_specs_for_sections(conn, sections)

    payload = {
        "process_id": process["process_id"],
        "name": process["name"],
        "module": process["module"],
        "actors": process["actors"],
        "preconditions": process["preconditions"],
        "outcomes": process["outcomes"],
        "steps": slim_steps,
        "business_rules": process["business_rules"],
        "validations": process["validations"],
        "alternate_flows": process["alternate_flows"],
        "field_specifications": field_specs,
    }
    cases = _call_llm(client, model, FSD_WORKFLOW_SYSTEM_PROMPT, json.dumps(payload, indent=2))
    for c in cases:
        c.setdefault("workflow_path", [])
        c["process"] = process["name"]
        c["module"] = process.get("module") or ""
        c["fsd_section"] = process.get("source_section") or ""
    return cases


def build_workflows(db_path: Optional[str], entry_url: str, max_depth: int, max_workflows: int) -> list[list[str]]:
    """
    Walk navigate_to edges breadth-first from entry_url and return the
    distinct root-to-leaf paths up to max_depth hops, capped at
    max_workflows. Simple linear paths only — good enough for
    "login -> screen A -> screen B" style flows without needing full
    graph-cycle handling.
    """
    with ks.connect(db_path) as conn:
        adjacency: dict[str, list[str]] = {}
        for row in conn.execute("SELECT source_page, target_page FROM edges"):
            adjacency.setdefault(row["source_page"], []).append(row["target_page"])

    if entry_url not in adjacency and entry_url not in {r for rows in adjacency.values() for r in rows}:
        # entry_url isn't even in the graph — still return it alone so the
        # caller gets at least a single-page "workflow"
        return [[entry_url]] if entry_url else []

    paths = []
    queue = deque([[entry_url]])
    seen_paths = set()
    while queue and len(paths) < max_workflows:
        path = queue.popleft()
        current = path[-1]
        neighbors = [n for n in adjacency.get(current, []) if n not in path]  # avoid cycles
        if not neighbors or len(path) >= max_depth:
            key = tuple(path)
            if key not in seen_paths and len(path) > 1:
                seen_paths.add(key)
                paths.append(path)
            continue
        for n in neighbors:
            queue.append(path + [n])

    if not paths:
        paths = [[entry_url]]
    return paths[:max_workflows]


def generate_workflow_cases(client, model: str, db_path: Optional[str], path: list[str]) -> list[dict]:
    with ks.connect(db_path) as conn:
        bundles = []
        for url in path:
            try:
                bundles.append(ks.get_page_bundle(conn, url))
            except KeyError:
                continue
    if len(bundles) < 2:
        return []  # not a real multi-step workflow

    payload = [
        {
            "step": i + 1,
            "url": b["url"],
            "title": b["title"],
            "forms": b["forms"],
            "standalone_buttons": b["standalone_buttons"],
        }
        for i, b in enumerate(bundles)
    ]
    cases = _call_llm(
        client, model, WORKFLOW_SYSTEM_PROMPT,
        json.dumps(payload, indent=2),
    )
    for c in cases:
        c.setdefault("workflow_path", path)
    return cases


# Multi-line prose columns need width and wrapping to be readable; short
# metadata columns should stay narrow so the sheet still fits on a screen.
_WIDE_COLS = {"description", "preconditions", "test_data", "steps",
              "expected_result", "postconditions", "business_rule", "workflow_path"}


def _style_sheet(writer, sheet_name: str, df: pd.DataFrame):
    ws = writer.sheets[sheet_name]
    header_fill = PatternFill(start_color="1F4E78", end_color="1F4E78", fill_type="solid")
    header_font = Font(color="FFFFFF", bold=True)

    for col_idx, col_name in enumerate(df.columns, start=1):
        cell = ws.cell(row=1, column=col_idx)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(vertical="center", wrap_text=True)

        letter = get_column_letter(col_idx)
        if col_name in _WIDE_COLS:
            width = 55
        else:
            longest = max([len(col_name)] + [len(str(v)) for v in df[col_name].astype(str)])
            width = min(max(longest + 2, 10), 30)
        ws.column_dimensions[letter].width = width

        # Wrap the body too — steps are now numbered multi-line strings, and
        # without wrapping they render as one unreadable line.
        for row_idx in range(2, len(df) + 2):
            ws.cell(row=row_idx, column=col_idx).alignment = Alignment(
                vertical="top", wrap_text=col_name in _WIDE_COLS)

    for row_idx in range(2, len(df) + 2):
        ws.row_dimensions[row_idx].height = None  # let Excel auto-fit wrapped text
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions


def rows_to_dataframe(rows: list[dict], extra_cols: list[str]) -> pd.DataFrame:
    flat = []
    for r in rows:
        row = dict(r)
        if "steps" in row and isinstance(row["steps"], list):
            # The prompts now ask the model to number its own steps, so only
            # add a number when it hasn't — otherwise cells read "1. 1. Open...".
            numbered = []
            for i, s in enumerate(row["steps"]):
                s = str(s).strip()
                numbered.append(s if re.match(r"^\s*\d+[.)]\s", s) else f"{i+1}. {s}")
            row["steps"] = "\n".join(numbered)
        if "workflow_path" in row and isinstance(row["workflow_path"], list):
            row["workflow_path"] = " -> ".join(row["workflow_path"])
        flat.append(row)
    # Reading order for a tester: what/why, then setup, then data, then do,
    # then check, then clean up.
    base_cols = ["test_id", "title", "description", "type", "priority",
                 "preconditions", "test_data", "steps", "expected_result",
                 "postconditions", "related_element"]
    cols = [c for c in base_cols + extra_cols if any(c in r for r in flat)]
    df = pd.DataFrame(flat)
    for c in cols:
        if c not in df.columns:
            df[c] = ""
    return df[cols] if not df.empty else pd.DataFrame(columns=cols)


def main():
    ap = argparse.ArgumentParser(description="Generate test cases from the crawled knowledge base.")
    # Defaults to GEN_LLM_MODEL (QA_GEN_LLM_MODEL in .env), which is separate
    # from the ingestion model — this stage is output-heavy, ingestion is not.
    ap.add_argument("--model", default=getattr(config, "GEN_LLM_MODEL",
                                               getattr(config, "LLM_MODEL", "claude-sonnet-5")))
    ap.add_argument("--max-tokens", type=int, default=getattr(config, "GEN_MAX_TOKENS", 8000),
                    help="Output ceiling per generation call; clamped to the model's limit. "
                         "On Groq's free tier (12000 TPM for llama-3.3-70b) this alone can "
                         "exceed your whole per-minute budget in one call — try 6000-8000.")
    ap.add_argument("--tpm-budget", type=int,
                    default=getattr(config, "TPM_BUDGET", 12000),
                    help="Client-side tokens-per-minute pacing budget (0 disables pacing). "
                         "Set to your Groq plan's actual TPM for the model you're using; "
                         "0 if you're on Anthropic or a provider you don't need to pace against.")
    ap.add_argument("--out", default=os.path.join(config.OUTPUT_DIR, "test_cases.xlsx"))
    ap.add_argument("--skip-workflows", action="store_true")
    ap.add_argument("--max-workflow-depth", type=int, default=4)
    ap.add_argument("--max-workflows", type=int, default=15)
    ap.add_argument("--entry-url", default=config.BASE_URL)
    ap.add_argument("--grounding-threshold", type=float,
                    default=getattr(config, "GROUNDING_THRESHOLD", 0.25),
                    help="Minimum score for an FSD step to count as covered by a screen.")
    args = ap.parse_args()

    if not ks.has_pages():
        print("Knowledge base is empty. Run pipeline.py first.")
        return

    global _GEN_MAX_TOKENS, _RATE_LIMITER
    _GEN_MAX_TOKENS = args.max_tokens
    _RATE_LIMITER = TokenRateLimiter(args.tpm_budget) if args.tpm_budget else None

    # Built by llm.py so provider choice (groq/anthropic) lives in one place.
    client = llm.get_client()
    cap = llm.model_max_output(client, args.model)
    print(f"Generating with {args.model} "
          f"(output ceiling: {min(args.max_tokens, cap) if cap else args.max_tokens} tokens)")
    if _RATE_LIMITER:
        print(f"Pacing calls to stay under {args.tpm_budget} tokens/minute "
              f"(pass --tpm-budget 0 to disable)")
    if args.max_tokens > (args.tpm_budget or float("inf")):
        print(f"  ! --max-tokens ({args.max_tokens}) exceeds --tpm-budget ({args.tpm_budget}) — "
              f"a single large call can still eat your whole minute. Consider lowering "
              f"--max-tokens to ~{max(int(args.tpm_budget * 0.5), 1000)} if you see repeated "
              f"pacing waits below.")

    has_fsd = ks.has_fsd()
    if has_fsd:
        print(f"FSD found in the knowledge base: {len(ks.get_process_ids())} business processes.")
    else:
        print("No FSD in the knowledge base — running crawl-only. "
              "Run fsd_ingest.py then fsd_grounding.py for workflow-level cases.")

    page_urls = ks.get_pages()
    print(f"\nGenerating page-level test cases for {len(page_urls)} pages...")
    page_rows = []
    with ks.connect() as conn:
        bundles = [ks.get_page_bundle(conn, u) for u in page_urls]
        fsd_by_url = ({u: ks.rules_for_page(conn, u, args.grounding_threshold)
                       for u in page_urls} if has_fsd else {})

    for bundle in bundles:
        if not _bundle_is_worth_testing(bundle):
            print(f"  skip (no testable elements): {bundle['url']}")
            continue
        ctx = fsd_by_url.get(bundle["url"])
        extra = ""
        if ctx:
            spec_fields = sum(len(g["fields"]) for g in ctx.get("field_specs", []))
            if ctx["business_rules"] or ctx["validations"] or spec_fields:
                extra = (f"  [+FSD: {len(ctx['business_rules'])} rules, "
                         f"{len(ctx['validations'])} validations, "
                         f"{spec_fields} spec fields]")
        print(f"  {bundle['url']} ...{extra}")
        try:
            cases = generate_page_level_cases(client, args.model, bundle, fsd_context=ctx)
        except Exception as e:
            print(f"  ! failed for {bundle['url']}: {e}")
            cases = []
        page_rows.extend(cases)

    workflow_rows = []
    coverage_rows = []
    if not args.skip_workflows:
        if has_fsd:
            # FSD processes ARE the workflows. Sidebar-adjacency BFS is not
            # used when a spec is available: every screen links to all menu
            # targets, so its "paths" are combinatorial noise, not journeys.
            process_ids = ks.get_process_ids()
            print(f"\nGenerating workflow test cases for {len(process_ids)} FSD processes...")
            # Held open for the whole pass: each process needs live lookups of
            # the crawled screen inventory behind its grounded steps.
            with ks.connect() as conn:
                processes = [ks.get_process_bundle(conn, pid) for pid in process_ids]
                for proc in processes:
                    grounded = sum(
                        1 for s in proc["steps"]
                        if s["grounded_to"]
                        and s["grounded_to"][0]["score"] >= args.grounding_threshold)
                    print(f"  {proc['name'][:52]:52s} "
                          f"({len(proc['steps'])} steps, {grounded} grounded, "
                          f"{len(proc['business_rules'])} rules)")
                    try:
                        workflow_rows.extend(generate_fsd_workflow_cases(
                            client, args.model, proc, args.grounding_threshold, conn=conn))
                    except Exception as e:
                        print(f"  ! failed for process {proc['process_id']}: {e}")

            with ks.connect() as conn:
                report = ks.coverage_report(conn, args.grounding_threshold)
            for s in report["ungrounded_steps"]:
                coverage_rows.append({
                    "gap_type": "FSD step with no screen",
                    "detail": f"{s['process_name']} step {s['seq']}: {s['action']}",
                    "screen_hint": s["screen_hint"] or "",
                    "best_score": round(s["best_score"] or 0, 3),
                })
            for s in report["screens_without_fsd"]:
                coverage_rows.append({
                    "gap_type": "Screen with no FSD coverage",
                    "detail": s["url"], "screen_hint": "", "best_score": "",
                })
        else:
            print("\nBuilding navigation workflows (no FSD — falling back to link graph)...")
            workflows = build_workflows(None, args.entry_url, args.max_workflow_depth,
                                        args.max_workflows)
            print(f"Generating workflow test cases for {len(workflows)} paths...")
            for path in workflows:
                print(f"  {' -> '.join(path)}")
                try:
                    workflow_rows.extend(generate_workflow_cases(client, args.model, None, path))
                except Exception as e:
                    print(f"  ! failed for path {path}: {e}")

    for i, r in enumerate(page_rows, 1):
        r["test_id"] = f"PG-{i:04d}"
    for i, r in enumerate(workflow_rows, 1):
        r["test_id"] = f"WF-{i:04d}"

    page_df = rows_to_dataframe(page_rows, extra_cols=["page_url", "page_title"])
    workflow_df = rows_to_dataframe(
        workflow_rows,
        extra_cols=["actor", "business_rule", "process", "module",
                    "workflow_path", "coverage_gap", "fsd_section"])

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with pd.ExcelWriter(args.out, engine="openpyxl") as writer:
        page_df.to_excel(writer, sheet_name="Page-Level Test Cases", index=False)
        _style_sheet(writer, "Page-Level Test Cases", page_df)
        workflow_df.to_excel(writer, sheet_name="Workflow Test Cases", index=False)
        _style_sheet(writer, "Workflow Test Cases", workflow_df)
        if coverage_rows:
            cov_df = pd.DataFrame(coverage_rows)
            cov_df.to_excel(writer, sheet_name="Coverage Gaps", index=False)
            _style_sheet(writer, "Coverage Gaps", cov_df)

    print(f"\nWrote {len(page_df)} page-level and {len(workflow_df)} workflow test cases to {args.out}")
    if coverage_rows:
        print(f"  Plus {len(coverage_rows)} coverage gaps on the 'Coverage Gaps' sheet.")


if __name__ == "__main__":
    main()