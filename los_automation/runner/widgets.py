"""
Phase 2: the ONLY module that types into the application or commits anything.

Everything else in this package stays read-only. Keeping every write in one file
is what makes the safety story checkable: `tests/test_safety.py` asserts that
driver.py, checks.py, run.py and targets.py contain no input-writing calls, and
that this module refuses to act on a host outside the allowlist.

Two gates, both fail closed:

  1. HOST      settings.write_allowed(BASE_URL) must pass. It reads a
               server-side environment variable only and is never settable from
               the UI. An unset allowlist blocks everything.
  2. AUTHORED  only the controls a flow names are touched. There is no
               "fill every field on the page" helper, and no control is found by
               position — always by its label. A form this app renders has 62
               controls, half of them invisible duplicates of each other, so
               anything positional would eventually type into the wrong one.

The crawler's DESTRUCTIVE_ACTION_PATTERNS denylist still governs every
exploratory click in the rest of the package. This module deliberately steps
around it for Save and Proceed, which is the whole point of Phase 2 — so it does
so only for a button a flow named explicitly, and only after gate 1 passes.

How this app's controls actually work, learned by probing the live form rather
than assumed:

  every field   <label title="Field Name"> - the title attribute carries the
                clean field name, which makes it the one reliable anchor. Text
                matching is not: labels carry a red '*' span, ids are reused
                (several controls share id="title"), and formcontrolname is not
                emitted by this build at all.
  text/number   a plain <input> inside the label's fieldset.
  date          <input bsdatepicker placeholder="Choose a Date">. Typing works
                when the format matches; the calendar is the fallback.
  dropdown      <app-dropdown><ng-select>. Options live in a
                .ng-dropdown-panel appended to the body, and only ever exist
                while the panel is open. Needs a REAL click - a JS .click()
                does not open it.
  lookup (LOV)  <app-dropdown-tree-dynamic-single-select> whose <input> is
                DISABLED, so it cannot be typed into. An <i class="fa fa-search">
                next to the label opens a modal holding an <ngx-treeview>; each
                choice is an <ngx-treeview-item> whose checkbox is display:none,
                so the clickable thing is its <label class="form-check-label">.
"""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from playwright.sync_api import Error as PWError

import config as crawler_config
import crawler as cr

from .. import settings


class WriteRefused(Exception):
    """The host is not approved for data entry. Raised before anything is typed."""


class FillError(Exception):
    """A named control could not be found or set. Never silent: a flow that
    half-filled a form must fail loudly rather than save a partial record."""


def assert_writable() -> str:
    """Gate 1. Called by every entry point here, and again before each commit."""
    allowed, reason = settings.write_allowed(crawler_config.BASE_URL)
    if not allowed:
        raise WriteRefused(
            f"Data entry is not permitted against {crawler_config.BASE_URL}. "
            f"{reason}")
    return reason


# Controls a flow may commit with. Anything else is refused even here, so a
# typo in a flow cannot reach Approve, Reject, Delete or a workflow transition
# that was never intended.
COMMITTABLE = [
    r"^save$", r"^save\s*&?\s*next$", r"^proceed$", r"^add$", r"^ok$",
    r"^done$", r"^select$", r"^apply$", r"^update$", r"^submit$",
    # The Documents screen's "Upload Other Document" panel commits with Upload
    # rather than Save. It stores a record like any other Save — it is not a
    # workflow transition and it removes nothing — so it belongs here rather
    # than being worked around by a flow.
    r"^upload$", r"^upload file$",
]

# Never committed by this module regardless of what a flow asks for. These end a
# case or a session rather than saving a record.
NEVER = [
    r"\bapprove\b", r"\breject\b", r"\bdelete\b", r"\bremove\b",
    r"\bauthorize\b", r"\bsign ?out\b", r"\blog ?out\b", r"\bwithdraw\b",
    r"\brelease\b", r"\bbulk\b", r"\bforward\b", r"\breturn\b",
]


def _text_arrived(wanted: str, got: str) -> bool:
    """
    Did the text that was sent actually end up in the box?

    Deliberately forgiving about presentation and strict about content. A rich
    editor wraps what it is given in a paragraph, normalises runs of whitespace
    and may render a typographic quote where a straight one went in, so
    comparing the strings byte for byte would fail every time. What matters is
    that this box now holds this sentence rather than nothing, or worse, the
    previous field's sentence.
    """
    def norm(t: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", (t or "").lower()).strip()

    a, b = norm(wanted), norm(got)
    if not a:
        return True
    return bool(b) and (a in b or b in a)


def _committable(label: str) -> bool:
    low = (label or "").strip().lower()
    if not low:
        return False
    if any(re.search(p, low) for p in NEVER):
        return False
    return any(re.match(p, low) for p in COMMITTABLE)


@dataclass
class Entry:
    """One thing that was entered, so it can be verified later."""
    label: str
    value: str
    kind: str
    screen: str = ""
    # Which pass over that screen put it there — "Additional Information —
    # Major Buyers" rather than just "Additional Information". `screen` is the
    # tab the verification leg has to re-open; `group` is what a human needs to
    # read a report in which four different grids all have a field called
    # "Status".
    group: str = ""
    # The identity of the CONTROL that took the value, where the control has
    # one — currently the TinyMCE editor id. It exists to catch a whole class of
    # silent bug: nineteen fields on BBFS Details all reporting success while
    # every one of them wrote into the same editor. Two entries sharing a target
    # means the label was resolved to the wrong control, which no amount of
    # reading the value back from that control would ever reveal.
    target: str = ""

    def as_dict(self) -> dict:
        return {"label": self.label, "value": self.value, "kind": self.kind,
                "screen": self.screen, "group": self.group or self.screen,
                "target": self.target}


@dataclass
class Filler:
    """
    Types into ONE screen of the application, recording everything it enters.

    The record is the point: the verification leg re-opens the saved record
    through the credit case and compares what the app shows against exactly
    what was typed, which is only possible if every write is logged as it
    happens.
    """
    session: object                      # runner.driver.Session
    screen: str = ""                     # the tab; what verification re-opens
    group: str = ""                      # this pass over it; for the report
    # Confine everything this Filler does to one part of the page.
    #
    # A dialog is detected automatically — see _scope — but not every form that
    # shadows the page's fields is a dialog. The Documents screen keeps TWO
    # slide-in panels in the DOM at once, "Upload Other Document" and "Document
    # Action", and both have a field called Title. Without a scope the first
    # one in document order wins whichever is actually open, which means typing
    # into a panel nobody can see. A caller that knows which panel it just
    # opened says so here.
    scope_selector: str = ""
    # Also read a field's name from the TABLE CELL beside its control.
    #
    # Off by default, and deliberately so. The Angular screens name every field
    # with a <label>, and a data grid whose rows are <td>Name</td><td><input>
    # </td> would otherwise hand back one "field" per ROW — labels that are
    # really data.
    #
    # The legacy risk-rating model is the screen that needs it. It is an .aspx
    # page laid out as a two-column table — the name in the left cell, the
    # <select> in the right — with no <label> anywhere on it, so a Filler that
    # only reads labels finds nothing and reports a working screen as offering
    # no enterable field. A caller that knows it is on such a screen, and has
    # scoped the Filler to one section of it, says so here.
    cell_labels: bool = False
    entries: list[Entry] = field(default_factory=list)
    _gate_reason: str = ""

    def __post_init__(self) -> None:
        self._gate_reason = assert_writable()

    # ---- locating -------------------------------------------------------
    @property
    def page(self):
        return self.session.page

    def _scope(self) -> str:
        """
        Where this Filler is allowed to look.

        An explicit scope wins — the caller has told us which of several forms
        on the page is the live one. Otherwise an open dialog, because an
        add-row form's fields shadow the page's. Otherwise the whole page.

        An explicit scope that is no longer on screen is ignored rather than
        obeyed: a panel that has closed underneath us should fall back to the
        ordinary rules, not make every field look missing.
        """
        if self.scope_selector:
            try:
                if self.page.locator(
                        self.scope_selector).first.is_visible(timeout=300):
                    return self.scope_selector
            except PWError:
                pass
        try:
            for sel in (".modal.show", ".modal.in"):
                if self.page.locator(sel).first.is_visible(timeout=200):
                    return sel
        except PWError:
            pass
        return "body"

    # Controls a field can be built from. `app-dropdown` and `ui-switch` are in
    # the list because neither contains an <input> of its own.
    _CTRL_SEL = ("input:not([type=hidden]), textarea, select, ng-select, "
                 "ui-switch, app-dropdown, [contenteditable=\"true\"], iframe")

    # Find a field by the TEXT of its label and stamp the block that holds it.
    #
    # The title attribute is not the universal anchor the obligor form made it
    # look like. On the Add Observation panel exactly ONE label of twelve
    # carries a title — every other one is a bare <label>Date of audit visit
    # </label> inside a fieldset.form-group. Anchoring only on title therefore
    # found Title, silently missed the other eleven, and reported the screen as
    # filled.
    #
    # Matching climbs to the SMALLEST ancestor that holds this label and a
    # control, and refuses one that holds a second field's label — a container
    # spanning two fields would hand the wrong control to whichever was asked
    # for first, which is worse than not finding the field at all.
    _STAMP_FIELD_JS = r"""([scope, wanted, ctrlSel]) => {
        const root = document.querySelector(scope) || document.body;
        const vis = (el) => el.offsetParent !== null || el.getClientRects().length > 0;
        const norm = (s) => (s || '').toLowerCase().replace(/&/g, ' and ')
            .replace(/[^a-z0-9]+/g, ' ').trim();
        const want = norm(wanted);
        if (!want) return -1;
        document.querySelectorAll('[data-fill-field]').forEach(
            e => e.removeAttribute('data-fill-field'));

        // Labels belonging to a styled checkbox or a tree item are part of a
        // control, not the name of a field, so they never count as a second
        // field's label.
        const ownLabel = (el) => {
            const c = typeof el.className === 'string' ? el.className : '';
            return !/custom-control-label|form-check-label/.test(c)
                   && (el.textContent || '').trim().length > 0;
        };

        for (const l of root.querySelectorAll('label, .control-label, .form-label')) {
            if (!vis(l)) continue;
            if (norm(l.textContent) !== want) continue;
            let node = l.parentElement;
            for (let i = 0; i < 6 && node; i++, node = node.parentElement) {
                if (!node.querySelector(ctrlSel)) continue;
                const labels = [...node.querySelectorAll(
                    'label, .control-label, .form-label')].filter(
                        x => vis(x) && ownLabel(x));
                if (labels.length > 1) return -1;   // spans another field
                node.setAttribute('data-fill-field', '0');
                return 0;
            }
        }
        return -1;
    }"""

    # Does this candidate hold ONE field, or several?
    #
    # BBFS Details is why this question has to be asked. Its nineteen boxes are
    # not nineteen fieldsets — they are one flat <fieldset> holding nineteen
    # label/editor pairs as siblings:
    #
    #     <fieldset class="form-group">
    #       <label title="ANY WRITE-OFF …">…</label>  <editor>…</editor>
    #       <label title="BUSINESS HANDLED …">…</label> <editor>…</editor>
    #       … seventeen more …
    #     </fieldset>
    #
    # `fieldset:has(> label[title="…"])` matches that one fieldset for EVERY
    # one of the nineteen titles, and `block.locator("iframe").first` inside it
    # is then always the first editor. Nineteen fields, one box, and nothing in
    # the old code noticed: each write succeeded, into the same place.
    #
    # Bootstrap's styled checkboxes and tree items carry labels that are part
    # of a control rather than the name of a field, so they do not count.
    _ONE_FIELD_JS = r"""(el) => {
        const vis = (x) => x.offsetParent !== null
                        || x.getClientRects().length > 0;
        const own = (l) => {
            const c = typeof l.className === 'string' ? l.className : '';
            return !/custom-control-label|form-check-label/.test(c)
                   && (l.textContent || '').trim().length > 0;
        };
        const labels = [...el.querySelectorAll(
            'label, .control-label, .form-label')].filter(l => vis(l) && own(l));
        return labels.length <= 1;
    }"""

    # The control that FOLLOWS a label, for forms laid out flat.
    #
    # _STAMP_FIELD_JS climbs from the label to the smallest ancestor holding a
    # control, and refuses one that also holds another field's label. On a flat
    # container that refusal is correct and leaves nothing — the only ancestor
    # is the shared fieldset. The field is not an ancestor there; it is the run
    # of siblings between this label and the next one, so that is what this
    # walks. It stops at the next label because everything past it belongs to
    # the next field.
    _STAMP_SIBLING_JS = r"""([scope, wanted, ctrlSel]) => {
        const root = document.querySelector(scope) || document.body;
        const vis = (el) => el.offsetParent !== null
                         || el.getClientRects().length > 0;
        const norm = (s) => (s || '').toLowerCase().replace(/&/g, ' and ')
            .replace(/[^a-z0-9]+/g, ' ').trim();
        const want = norm(wanted);
        if (!want) return -1;
        document.querySelectorAll('[data-fill-field]').forEach(
            e => e.removeAttribute('data-fill-field'));

        const LABEL = 'label, .control-label, .form-label';
        for (const l of root.querySelectorAll(LABEL)) {
            if (!vis(l)) continue;
            const text = l.getAttribute('title') || l.textContent;
            if (norm(text) !== want) continue;
            for (let sib = l.nextElementSibling; sib;
                 sib = sib.nextElementSibling) {
                if (sib.matches(LABEL)) break;      // the next field's name
                if (sib.matches(ctrlSel) || sib.querySelector(ctrlSel)) {
                    sib.setAttribute('data-fill-field', '0');
                    return 0;
                }
            }
        }
        return -1;
    }"""

    # A field whose NAME is a table cell and whose control is a later cell on
    # the same row — the legacy risk-rating model's layout, and the layout of
    # every .aspx screen in this application that predates the Angular rewrite:
    #
    #     <tr><td>Ownership Structure</td><td><select>…</select></td></tr>
    #
    # Only reached when the caller asked for it (see Filler.cell_labels), and
    # only for a cell that reads as a NAME rather than as data: it holds text
    # and no control of its own, it is short, and its row is a label/value row
    # rather than a grid row of many columns.
    _CELL_ROW_LIMIT = 4
    _STAMP_CELL_JS = r"""([scope, wanted, ctrlSel, maxCells]) => {
        const root = document.querySelector(scope) || document.body;
        const vis = (el) => el.offsetParent !== null || el.getClientRects().length > 0;
        const norm = (s) => (s || '').toLowerCase().replace(/&/g, ' and ')
            .replace(/[^a-z0-9]+/g, ' ').trim();
        const want = norm(wanted);
        if (!want) return -1;
        document.querySelectorAll('[data-fill-field]').forEach(
            e => e.removeAttribute('data-fill-field'));

        for (const row of root.querySelectorAll('tr')) {
            if (!vis(row)) continue;
            const cells = [...row.children].filter(
                c => c.matches('td, th'));
            if (!cells.length || cells.length > maxCells) continue;
            // The row's FIRST name cell only. On a name/spacer/value row the
            // spacer reads as a second name and would resolve to the SAME
            // control, which is the one failure worth more than a missing
            // field: two labels, one box, both reporting success. Discovery
            // offers that first cell and nothing else, so the two stay level.
            const at = cells.findIndex(c => !c.querySelector(ctrlSel)
                && (c.textContent || '').trim().length > 0);
            if (at < 0 || norm(cells[at].textContent) !== want) continue;
            // The first cell after the name that actually holds a control. A
            // spacer cell between the two is common on these pages.
            for (let i = at + 1; i < cells.length; i++) {
                if (cells[i].querySelector(ctrlSel)) {
                    cells[i].setAttribute('data-fill-field', '0');
                    return 0;
                }
            }
        }
        return -1;
    }"""

    # The same rule, read the other way round: every name/control pair on the
    # page, for discovery. Deliberately a mirror of _STAMP_CELL_JS — a label
    # this returns must be one _block can resolve, or discovery offers fields
    # that then cannot be set.
    _CELL_LABELS_JS = r"""([scope, ctrlSel, maxCells]) => {
        const root = document.querySelector(scope) || document.body;
        const vis = (el) => el.offsetParent !== null || el.getClientRects().length > 0;
        const out = [];
        for (const row of root.querySelectorAll('tr')) {
            if (!vis(row)) continue;
            const cells = [...row.children].filter(c => c.matches('td, th'));
            if (!cells.length || cells.length > maxCells) continue;
            // The row's first name cell, and only that one — see the note in
            // _STAMP_CELL_JS, which resolves exactly this cell and no other.
            const i = cells.findIndex(c => !c.querySelector(ctrlSel)
                && (c.textContent || '').trim().length > 0);
            if (i < 0 || i === cells.length - 1) continue;
            const t = (cells[i].textContent || '').replace(/\*/g, '')
                .replace(/\s+/g, ' ').trim().replace(/\s*:\s*$/, '');
            if (!t || t.length > 90 || out.includes(t)) continue;
            let holds = false;
            for (let j = i + 1; j < cells.length && !holds; j++) {
                if (cells[j].querySelector(ctrlSel)) holds = true;
            }
            if (holds) out.push(t);
        }
        return out;
    }"""

    def _cell_labels(self) -> list[str]:
        """Field names read from table cells. Empty unless cell_labels is on."""
        if not self.cell_labels:
            return []
        try:
            return self.page.evaluate(
                self._CELL_LABELS_JS,
                [self._scope(), self._CTRL_SEL, self._CELL_ROW_LIMIT]) or []
        except PWError:
            return []

    def _holds_one_field(self, loc) -> bool:
        try:
            return bool(self._eval_on(loc, self._ONE_FIELD_JS, timeout=400))
        except PWError:
            # Cannot tell — behave exactly as this did before the check existed
            # rather than reject a block that is probably fine.
            return True

    def _block(self, label: str):
        """
        The element that holds one field: its label plus its control.

        Three anchors, tried in that order. `label[title="..."]` is the
        strongest where it exists — the obligor form emits it on every field,
        and it carries the clean name without the mandatory asterisk. Where it
        does NOT exist, the label's own text is used instead; the case screens
        are built by different components and mostly emit a bare <label>. Where
        the form is laid out flat, with labels and controls as siblings of one
        container, the control that FOLLOWS the label is the field.

        Whichever route answers, the answer has to be ONE field. A container
        holding several is worse than no answer at all: it hands back the first
        control in it, so every field in that container is filled into the same
        box and every one of them reports success. That is not hypothetical —
        see _ONE_FIELD_JS. Such a candidate is kept only as a last resort, so
        nothing that worked before this check stops working now.

        Scoped to an open dialog when there is one, because this app reuses the
        same field names inside add-row forms.
        """
        scope = self._scope()
        esc = label.replace('"', '\\"')
        spans_several = None
        for sel in (
            # the lookup component wraps label + magnifier + input together
            f'{scope} app-dropdown-tree-dynamic-single-select:has(label[title="{esc}"])',
            f'{scope} fieldset:has(> label[title="{esc}"])',
            f'{scope} fieldset:has(label[title="{esc}"])',
            f'{scope} app-dropdown:has(label[title="{esc}"])',
            f'{scope} div:has(> label[title="{esc}"])',
        ):
            loc = self.page.locator(sel).first
            try:
                if not (loc.count() and loc.is_visible(timeout=400)):
                    continue
            except PWError:
                continue
            if self._holds_one_field(loc):
                return loc
            if spans_several is None:
                spans_several = loc

        # The label's own text, then the control that follows the label, then —
        # only where the caller asked for it — the cell beside the name. A real
        # <label> always wins: on a screen that has both, the label is the
        # field's own name and a cell merely happens to read the same way.
        routes = [self._STAMP_FIELD_JS, self._STAMP_SIBLING_JS]
        if self.cell_labels:
            routes.append(self._STAMP_CELL_JS)
        for js in routes:
            args = [scope, label, self._CTRL_SEL]
            if js is self._STAMP_CELL_JS:
                args.append(self._CELL_ROW_LIMIT)
            try:
                found = self.page.evaluate(js, args)
            except PWError:
                found = -1
            if found < 0:
                continue
            loc = self.page.locator('[data-fill-field]').first
            try:
                if loc.count() and loc.is_visible(timeout=400):
                    return loc
            except PWError:
                pass

        if spans_several is not None:
            return spans_several

        raise FillError(
            f"No field labelled {label!r} is on this screen — no label carries "
            f"that title attribute and none reads that way either. Check the "
            f"exact spelling against the app.")

    def _visible_labels(self, limit: int = 40) -> list[str]:
        """
        The fields on screen right now — for error messages, and for discovery.

        Three rules, each of which was a bug before it was one:

        VISIBLE is meant literally. This app keeps every tab of a record in the
        DOM and only hides the inactive ones, so an unfiltered list hands back
        the labels of six tabs at once. An error message naming fields from
        another tab is misleading, and discovery would spend its time clicking
        at controls that are not on screen.

        The label's own TEXT counts, not only its title attribute. Most screens
        outside the obligor form emit a bare <label>, and reading only titles
        made the Add Observation panel look like it had one field.

        A label must actually NAME a field. "Condition Type" is a filter
        heading in the Observations sidebar with no control under it, and
        offering it for filling would produce a blocker on every run.

        On a screen whose caller turned cell_labels on, the names read out of
        table cells are added after these, so a real <label> still comes first.
        """
        found = self._labels_from_labels()
        for t in self._cell_labels():
            if t not in found:
                found.append(t)
        return found[:limit]

    def _labels_from_labels(self) -> list[str]:
        """The <label>-borne half of _visible_labels."""
        try:
            return self.page.evaluate(
                """([scope, ctrlSel]) => {
                    const root = document.querySelector(scope) || document.body;
                    const vis = (el) => el.offsetParent !== null
                                     || el.getClientRects().length > 0;
                    const out = [];
                    for (const l of root.querySelectorAll(
                            'label, .control-label, .form-label')) {
                        if (!vis(l)) continue;
                        const c = typeof l.className === 'string' ? l.className : '';
                        // part of a control, not the name of a field
                        if (/custom-control-label|form-check-label/.test(c)) continue;
                        const t = (l.getAttribute('title') || l.textContent || '')
                            .replace(/\\*/g, '').trim()
                            .replace(/\\s+/g, ' ').replace(/\\s*:\\s*$/, '');
                        if (!t || t.length > 90 || out.includes(t)) continue;
                        let node = l.parentElement, named = false;
                        for (let i = 0; i < 5 && node && !named;
                             i++, node = node.parentElement) {
                            if (node.querySelector(ctrlSel)) named = true;
                        }
                        if (named) out.push(t);
                    }
                    return out;
                }""", [self._scope(), self._CTRL_SEL]) or []
        except PWError:
            return []

    def field_labels(self, limit: int = 140) -> list[str]:
        """
        Every field label the screen (or the open dialog) is showing, in
        document order. READ ONLY — it types nothing and clicks nothing.

        This is discovery, not a "fill every field" helper: it returns NAMES,
        and a caller still has to set each one by name through set_value, which
        is what keeps the guarantee that no control is ever located by position.

        It exists for the credit case's own screens. The obligor form was probed
        field by field and its labels are written down in flows.BASIC_INFORMATION
        and TAB_SPECS; the case screens are built from a per-deployment product
        configuration, so the specification names the fields but the exact label
        text on any given environment is not knowable in advance. Authoring the
        specified fields and then asking the screen what ELSE it is showing is
        the only way to fill a screen completely without guessing.
        """
        return self._visible_labels(limit)

    def kind_of(self, label: str) -> str:
        """
        What sort of control is behind this label, read from the DOM.

        Declaring each field's kind up front does not survive this application:
        the same label is a dropdown on one tab and a Yes/No toggle on another,
        'Nationality' is a disabled auto-filled box in one dialog, and several
        narrative fields are TinyMCE rather than textareas. Asking the page is
        both shorter and more honest than maintaining a table of guesses.

        Order matters — a lookup also contains an <input>, and a TinyMCE editor
        also has a hidden <textarea> — so the most specific test wins.
        """
        try:
            block = self._block(label)
        except FillError:
            return "missing"
        try:
            return self.page.evaluate(
                """(el) => {
                    const has = (s) => !!el.querySelector(s);
                    const vis = (x) => x && (x.offsetParent !== null ||
                                             x.getClientRects().length > 0);
                    if (el.tagName.toLowerCase()
                            === 'app-dropdown-tree-dynamic-single-select'
                        || has('i.fa-search')) return 'lookup';
                    if (has('ng-select')) return 'dropdown';
                    // A <ui-switch> Yes/No toggle: no input at all, just a
                    // button[role=switch]. Tested before the input checks
                    // because there is no input to find.
                    if (has('ui-switch') || has('button[role="switch"]'))
                        return 'switch';
                    if (has('iframe') || has('[contenteditable="true"]'))
                        return 'rich';
                    // A file input is not a text box, whatever it reads like
                    // once the app has hidden it behind an icon. .fill() on
                    // one throws, so a discovery pass that took it for text
                    // would report the application refusing a value on a
                    // control that never accepted one — upload() is the only
                    // way to set it. Tested before the input checks below,
                    // which would otherwise call it 'text'.
                    if (has('input[type=file]')) return 'file';
                    const inp = el.querySelector('input');
                    // "Choose a Date" on the obligor form, "Select Date" on the
                    // case screens. Matching only the first typed a sentence
                    // into every date box on the Add Observation panel, because
                    // they read back as ordinary text inputs.
                    if (inp && (inp.hasAttribute('bsdatepicker')
                        || /date/i.test(inp.getAttribute('placeholder') || '')
                        || /datepicker/i.test(inp.className || ''))) return 'date';
                    if (has('input[type=checkbox]')) return 'checkbox';
                    if (has('input[type=radio]')) return 'radio';
                    if (has('textarea')) return 'textarea';
                    if (has('select')) return 'select';
                    if (inp) {
                        if (inp.disabled || inp.readOnly) return 'readonly';
                        // Numeric boxes reject prose outright. Saying so lets a
                        // caller choose a number instead of discovering the
                        // refusal as an unexplained validation message.
                        const t = (inp.getAttribute('type') || '').toLowerCase();
                        const mode = (inp.getAttribute('inputmode') || '').toLowerCase();
                        if (t === 'number' || mode === 'numeric' || mode === 'decimal')
                            return 'number';
                        return 'text';
                    }
                    return 'unknown';
                }""", block.element_handle())
        except PWError:
            return "unknown"

    def set_value(self, label: str, value: Optional[str] = None,
                  when: Optional[date] = None) -> Entry:
        """
        Set a field, working out for itself what kind of control it is.

        `value=None` means "whatever the app offers first" for a dropdown or
        lookup, and a default sentence for free text.
        """
        assert_writable()
        kind = self.kind_of(label)
        if kind == "missing":
            raise FillError(
                f"No field labelled {label!r} is on this screen. Visible here: "
                + ", ".join(self._visible_labels(12)))
        if kind == "readonly":
            raise FillError(
                f"{label!r} is disabled or read-only — the app fills it, so "
                f"there is nothing to enter.")
        if kind == "file":
            raise FillError(
                f"{label!r} is a file input, so there is nothing to type into "
                f"it. Attach a file with upload({label!r}) instead.")
        if kind == "lookup":
            return self.lookup(label, value)
        if kind == "dropdown":
            return self.choose(label, value)
        if kind == "rich":
            return self.rich_text(label, value or "Entered by automated test.")
        if kind == "date":
            return self.date(label, when or date(2020, 1, 1))
        if kind == "switch":
            on = True
            if value is not None and str(value).strip().lower() in ("no", "false"):
                on = False
            return self.toggle(label, on)
        if kind == "checkbox":
            # A checkbox can be set deliberately OFF and still count as filled.
            # 'Life Time Expiry?' is the reason: ticking it DISABLES the expiry
            # date beside it, so a flow that wants both boxes populated has to be
            # able to say "leave this one clear" and have that recorded.
            on = True
            if value is not None and str(value).strip().lower() in (
                    "no", "false", "off", "0", "unticked"):
                on = False
            return self.check(label, on)
        if kind == "select":
            return self.select_native(label, value)
        if kind == "number":
            # A numeric box will not take prose, and a caller that did not
            # supply a number is better served by an explicit refusal than by a
            # value the field silently rejects at save time.
            if value is None or not re.match(r"^-?\d+(\.\d+)?$", str(value).strip()):
                raise FillError(
                    f"{label!r} is a numeric field, so {value!r} is not a value "
                    f"it can take. Give it a number.")
            return self.text(label, value)
        if kind in ("text", "textarea"):
            return self.text(label, value or "Entered by automated test.")
        raise FillError(
            f"{label!r} is a {kind!r} control, which there is no way to set "
            f"safely without knowing what it expects.")

    def select_native(self, label: str, value: Optional[str] = None) -> Entry:
        """A plain <select>, as opposed to the ng-select this app mostly uses."""
        assert_writable()
        block = self._block(label)
        sel = block.locator("select").first
        if not sel.count():
            raise FillError(f"{label!r} is not a native select.")
        opts = [o.strip() for o in sel.locator("option").all_inner_texts()]
        real = [o for o in opts
                if o and not re.match(r"^-?\s*select\s*-?$", o, re.I)]
        if not real:
            raise FillError(f"{label!r} offers no selectable options.")

        want = value if value in real else real[0]
        sel.select_option(label=want)
        return self._record(label, want, "select")

    def _record(self, label: str, value: str, kind: str,
                target: str = "") -> Entry:
        e = Entry(label=label, value=str(value), kind=kind, screen=self.screen,
                  group=self.group or self.screen, target=target)
        self.entries.append(e)
        return e

    def _settle(self, ms: int = 8000) -> None:
        try:
            cr.wait_until_settled(self.page, self.session.recorder,
                                  timeout_ms=ms, stable_polls=2)
        except PWError:
            pass

    # ---- plain inputs ---------------------------------------------------
    def text(self, label: str, value: str) -> Entry:
        """Type into a text / number / textarea field."""
        assert_writable()
        block = self._block(label)
        ctl = block.locator("input:not([type=hidden]), textarea").first
        if not ctl.count():
            raise FillError(f"{label!r} has no text input to type into.")
        try:
            if ctl.is_disabled(timeout=500):
                raise FillError(
                    f"{label!r} is disabled, so it cannot be typed into. It is "
                    f"probably a lookup — use lookup({label!r}) instead.")
        except PWError:
            pass
        ctl.click(timeout=6000)
        ctl.fill("")
        ctl.fill(str(value))
        # Angular validates on blur; without this the field can still read as
        # untouched and Save will refuse.
        ctl.press("Tab")
        return self._record(label, value, "text")

    # Formats tried in order. The app renders a chosen date as
    # "January 1st, 2001" but accepts a typed numeric date, and which of
    # day-first or month-first it uses is a per-deployment locale setting — so
    # the value is read back and the year confirmed rather than trusted.
    _DATE_FORMATS = ("%d/%m/%Y", "%m/%d/%Y", "%Y-%m-%d", "%d-%m-%Y",
                     "%B %d, %Y")

    def _calendar_open(self) -> bool:
        cal = self.page.locator(
            "bs-datepicker-container, bs-daterangepicker-container").first
        try:
            return bool(cal.count()) and cal.is_visible(timeout=300)
        except PWError:
            return False

    def _open_modal(self):
        """
        The dialog that is actually on screen, or None.

        The LAST visible one, not the first. This template leaves a dialog it
        has closed in the DOM still carrying `.show`, so `.modal.show` first
        can resolve to one that is no longer there — and a neutral click aimed
        at a dialog that has gone lands on the page behind it.
        """
        modals = self.page.locator(".modal.show, .modal.in")
        try:
            for i in range(modals.count() - 1, -1, -1):
                m = modals.nth(i)
                if m.is_visible(timeout=200):
                    return m
        except PWError:
            pass
        return None

    def _dismiss_overlay(self, ctl=None) -> None:
        """
        Close whatever transient overlay is open — a calendar, a dropdown
        panel, a treeview — WITHOUT pressing Escape inside a dialog.

        Escape inside a Bootstrap modal does not close the overlay: it closes
        the MODAL, and takes the half-filled form with it. That one keystroke
        is behind every "No field labelled ... Visible here:" with nothing
        after the colon this suite has produced — twenty-three fields of
        Management & Shareholders, and every field of the Collateral
        Association dialog after the first dropdown that had nothing to offer.
        The dialog those fields lived on had been dismissed by the field above
        them, and everything later was looking at a page with no form on it.

        Leaving the overlay open is not an option either. It covers the control
        it belongs to, so the next click on that control fails Playwright's
        actionability check and times out — which is why those retries all read
        "Locator.click: Timeout 6000ms exceeded" on screens with no modal at
        all.

        So: click a neutral spot instead. The dialog's own header when there is
        a dialog, the page body when there is not. Escape stays as the last
        resort, and only where there is no dialog for it to destroy.
        """
        modal = self._open_modal()
        if modal is not None:
            # A real mouse click at a point, not a click on an element: the
            # overlay may be over part of the dialog, and an element click
            # would refuse rather than land. The header's left edge holds the
            # title — the close 'x' is at the other end.
            head = modal.locator(".modal-header, .modal-title").first
            spot = head if head.count() else modal
            try:
                box = spot.bounding_box()
                if box:
                    self.page.mouse.click(box["x"] + 12,
                                          box["y"] + box["height"] / 2)
                    self.page.wait_for_timeout(250)
            except PWError:
                pass
            return

        try:
            if ctl is not None:
                ctl.press("Escape")
            else:
                self.page.keyboard.press("Escape")
            self.page.wait_for_timeout(200)
        except PWError:
            pass

    def _dismiss_calendar(self, ctl) -> None:
        """Close an open bsDatepicker without destroying the dialog under it."""
        if not self._calendar_open():
            return
        self._dismiss_overlay(ctl)

    def _calendar_pick(self, ctl, when: date) -> bool:
        """
        Set a date by driving the calendar, which needs no format at all.

        This is the primary strategy because typing is not reliable here: this
        bsDatepicker parsed "01/01/2020" as 1 January 2001 and kept it, and it
        only reveals that after a save, far too late to react to. Clicking
        year -> month -> day cannot be misparsed.

        ngx-bootstrap's day view collapses to month and then year view on
        successive clicks of the header's current-period button, so two clicks
        gets to the year grid wherever it started.
        """
        try:
            ctl.click(timeout=6000)
        except PWError:
            return False
        self.page.wait_for_timeout(700)
        cal = self.page.locator(
            "bs-datepicker-container, bs-daterangepicker-container, "
            ".bs-datepicker").first
        if not cal.count():
            return False

        def head_current():
            return cal.locator(".bs-datepicker-head button.current")

        # day -> month -> year
        for _ in range(2):
            cur = head_current().first
            if not cur.count():
                break
            try:
                cur.click(timeout=3000)
            except PWError:
                break
            self.page.wait_for_timeout(350)

        def click_cell(text: str) -> bool:
            cell = cal.locator(
                f'.bs-datepicker-body span:not(.disabled):text-is("{text}")').first
            if not cell.count():
                return False
            try:
                cell.click(timeout=3000)
                self.page.wait_for_timeout(400)
                return True
            except PWError:
                return False

        # The year grid shows a 16-year window; page back until the year is in it.
        year = str(when.year)
        for _ in range(14):
            if click_cell(year):
                break
            prev = cal.locator(".bs-datepicker-head button.previous").first
            if not prev.count():
                return False
            try:
                prev.click(timeout=3000)
            except PWError:
                return False
            self.page.wait_for_timeout(300)
        else:
            return False

        if not click_cell(when.strftime("%B")[:3]):        # 'Jan'
            if not click_cell(when.strftime("%B")):        # 'January'
                return False

        if not click_cell(str(when.day)):
            return False

        self.page.wait_for_timeout(500)
        self._dismiss_calendar(ctl)
        return True

    def date(self, label: str, when: date) -> Entry:
        """
        Set a bsDatepicker field, and prove it took.

        Two traps here, both hit for real on this form:

          1. Typing character by character lets the picker reformat the input
             mid-entry, so the tail of the string lands in a rewritten value.
             "01/01/2020" typed with a delay was stored as 2001. The whole
             string therefore goes in as ONE fill().
          2. The app silently keeps whatever it managed to parse, and it
             REFORMATS the input a beat after the blur — "01/01/2020" became
             "January 1st, 2001". Reading the value straight back therefore
             sees the raw text and confirms a date the app never accepted, so
             the read-back has to wait for the reformat first. A wrong-but-
             plausible date is far worse than a rejected one: it saves, and
             then quietly fails verification against a value nobody chose.
             Every candidate format is tried until the year comes back right.
        """
        assert_writable()
        block = self._block(label)
        ctl = block.locator("input").first
        if not ctl.count():
            raise FillError(f"{label!r} has no date input.")

        # Whether this field is on an add-row dialog, remembered BEFORE
        # anything is clicked. A date field that dismisses the dialog it sits
        # on has to be reported as that, not as twenty-three later fields
        # mysteriously going missing.
        was_modal = self._scope() != "body"
        attempts: list[str] = []

        def taken(got: str) -> bool:
            return self._date_taken(when, got)

        # Calendar first — it cannot be misparsed.
        if self._calendar_pick(ctl, when):
            got = (ctl.input_value() or "").strip()
            attempts.append(f"calendar -> {got or '(empty)'}")
            if taken(got):
                return self._record(label, got, "date")

        for fmt in self._DATE_FORMATS:
            wanted = when.strftime(fmt)
            # An overlay left open covers the very input this is about to
            # click, and Playwright refuses to click through it. Every retry
            # therefore starts from a closed calendar.
            self._dismiss_calendar(ctl)
            try:
                ctl.click(timeout=6000)
                ctl.fill("")                       # clear any partial value
                ctl.fill(wanted)                   # one operation, not per-key
                self._dismiss_calendar(ctl)        # never Escape inside a modal
                ctl.press("Tab")                   # blur so Angular parses it
            except PWError as exc:
                attempts.append(f"{wanted} -> {str(exc)[:40]}")
                continue
            # Long enough for the picker to normalise the text. Without this the
            # raw string is read back and a misparse looks like a success.
            self.page.wait_for_timeout(1200)
            got = (ctl.input_value() or "").strip()
            attempts.append(f"{wanted} -> {got or '(empty)'}")
            if taken(got):
                return self._record(label, got, "date")

        self._dismiss_calendar(ctl)
        if was_modal and self._scope() == "body":
            raise FillError(
                f"{label!r} closed the form it is on. The dialog was open "
                f"before this field was touched and is gone now, so nothing "
                f"below it could be filled either. Tried: "
                + "; ".join(attempts))
        raise FillError(
            f"{label!r} would not accept {when.isoformat()}. Every attempt "
            f"read back a DIFFERENT date, so the app was storing a day nobody "
            f"asked for. Tried: " + "; ".join(attempts))

    @staticmethod
    def _date_taken(when: date, got: str) -> bool:
        """
        Did the field really take THIS date — day, month and year?

        Checking only the year was not enough. Asked for 31/01/2026, this
        picker settled on "January 20, 2026": the year matched, the value was
        accepted, and the round trip then compared the stored 20th against the
        recorded 20th and called it a pass. A date silently changed to another
        date in the same year is exactly the kind of defect this suite exists
        to catch, so it must not be the automation that hides it.
        """
        if not got:
            return False
        nums = {n.lstrip("0") or "0" for n in re.findall(r"\d+", got)}
        if str(when.year) not in nums:
            return False
        if str(when.day) not in nums:
            return False
        low = got.lower()
        return (str(when.month) in nums
                or when.strftime("%B").lower() in low
                or when.strftime("%b").lower() in low)

    # Find THIS block's TinyMCE instance.
    #
    # BY ITS IFRAME, and only by its iframe unless there is no other way. That
    # is the whole lesson of BBFS Details, and it is worth spelling out because
    # the obvious approach is wrong in a way nothing complains about.
    #
    # tinymce-angular replaces a hidden <textarea> with an editor and registers
    # the instance under that textarea's id. So `tinymce.get(theTextarea.id)`
    # looks like the exact, identity-based lookup this module insists on. On
    # this build it is not: every <editor> on the tab emits the SAME textarea
    # id — nineteen elements on BBFS Details all called
    # `tiny-angular_13708206111788763912213` — so nineteen different fields
    # resolve to one editor and write over each other. Nineteen fields report
    # success, one box holds text, and the round trip afterwards finds the
    # other eighteen values missing from a record that looked fully entered.
    #
    # Duplicate ids are invalid HTML and getElementById-style lookups have no
    # defined answer for them, which is exactly why this cannot be trusted. The
    # <iframe> TinyMCE creates per instance IS unique per instance, and every
    # editor holds a reference to its own, so matching on it is real identity.
    # The id route survives only for a build with no iframe at all, and only
    # after checking the id is actually unique on the page.
    #
    # `idx` — the instance's position in tinymce.editors — comes back as well,
    # because editor.id is that same duplicated string and so cannot tell two
    # instances apart. It is what flows.check_editors_distinct compares.
    _TINY_FIND_JS = r"""
        const tm = window.tinymce;
        const findEditor = (el) => {
            if (!tm) return {state: 'no-tinymce'};
            const ifr = el.querySelector('iframe');
            const ta = el.querySelector('textarea[id]');
            let ed = null;
            if (ifr)
                ed = (tm.editors || []).find(e => e.iframeElement === ifr)
                     || null;
            if (!ed && ta) {
                const same = document.querySelectorAll(
                    'textarea[id="' + ta.id.replace(/"/g, '\\"') + '"]').length;
                if (same === 1) ed = tm.get(ta.id) || null;
            }
            if (!ed)
                // An iframe with no editor yet means TinyMCE is still starting
                // up on this box; no iframe at all means it has not got here.
                // Both are worth waiting for, neither is worth guessing past.
                return {state: ifr ? 'not-ready' : 'no-editor'};
            if (ed.initialized === false) return {state: 'not-ready'};
            return {state: 'found', ed: ed,
                    key: (ed.id || 'editor') + '#'
                         + (tm.editors || []).indexOf(ed)};
        };
    """

    _TINY_SET_JS = r"""([el, value]) => {
        """ + _TINY_FIND_JS + r"""
        const hit = findEditor(el);
        if (hit.state !== 'found') return hit;
        const ed = hit.ed;

        // TinyMCE 6 renamed fire() to dispatch(); this build could be either.
        const fire = (name) => {
            try { (ed.dispatch ? ed.dispatch(name) : ed.fire(name)); }
            catch (e) { /* an event this build does not know is harmless */ }
        };
        const esc = (s) => String(s).replace(/&/g, '&amp;')
            .replace(/</g, '&lt;').replace(/>/g, '&gt;');

        ed.setContent('<p>' + esc(value) + '</p>');
        // save() writes the editor's content back into the hidden <textarea>,
        // and the change/input events are what tinymce-angular listens on to
        // push the value into the Angular form — without them the box shows
        // the text and the model stays empty, which saves nothing.
        try { ed.save(); } catch (e) { /* not every build exposes save() */ }
        fire('input');
        fire('change');
        const ta = el.querySelector('textarea[id]');
        if (ta) {
            ta.dispatchEvent(new Event('input', {bubbles: true}));
            ta.dispatchEvent(new Event('change', {bubbles: true}));
        }
        let text = '';
        try { text = ed.getContent({format: 'text'}) || ''; } catch (e) {}
        return {state: 'set', id: hit.key,
                text: text.replace(/\s+/g, ' ').trim()};
    }"""

    _TINY_GET_JS = r"""(el) => {
        """ + _TINY_FIND_JS + r"""
        const hit = findEditor(el);
        if (hit.state !== 'found') return '';
        try { return (hit.ed.getContent({format: 'text'}) || '').trim(); }
        catch (e) { return ''; }
    }"""

    def _eval_on(self, block, js: str, arg=None, timeout: int = 3000):
        """
        Run JS against the element behind a locator, disposing of the handle.

        The disposal is the reason this exists. A tab of nineteen editors, each
        polled while it initialises, leaks a few hundred live element handles
        into the browser over one pass otherwise — harmless individually,
        and exactly the sort of thing that makes a long run mysteriously slow.
        """
        handle = block.element_handle(timeout=timeout)
        if handle is None:
            raise PWError("the element behind this field went away")
        try:
            return self.page.evaluate(
                js, handle if arg is None else [handle, arg])
        finally:
            try:
                handle.dispose()
            except PWError:
                pass

    # A stable, unique name for the control a field resolved to, stamped onto
    # the element the first time it is asked for. Used where TinyMCE cannot
    # supply one, so the duplicate-target check still works on a build with no
    # editor — the check is about "did two labels land on one control", and
    # that question is worth answering whatever the control turns out to be.
    _CONTROL_KEY_JS = r"""(el) => {
        const t = el.querySelector(
            'iframe, [contenteditable="true"], textarea, input');
        if (!t) return '';
        if (!t.getAttribute('data-fill-target')) {
            window.__fillTargetSeq = (window.__fillTargetSeq || 0) + 1;
            t.setAttribute('data-fill-target', 'ctl-' + window.__fillTargetSeq);
        }
        return t.getAttribute('data-fill-target');
    }"""

    def _control_key(self, block) -> str:
        try:
            return self._eval_on(block, self._CONTROL_KEY_JS) or ""
        except PWError:
            return ""

    def _tiny_set(self, block, value: str, wait_ms: int = 12000) -> dict:
        """
        Put `value` into the TinyMCE editor inside `block`, and report back what
        the editor then holds. Waits for the editor to finish initialising —
        the tab renders its boxes before TinyMCE has claimed them, and setting
        one mid-initialisation is overwritten by the editor's own empty content.
        """
        deadline = time.monotonic() + wait_ms / 1000
        out: dict = {"state": "no-editor"}
        while True:
            try:
                out = self._eval_on(block, self._TINY_SET_JS, str(value)) or {}
            except PWError as exc:
                out = {"state": "error", "text": "", "id": "",
                       "error": str(exc)[:160]}
            if out.get("state") not in ("not-ready", "no-editor"):
                return out
            if time.monotonic() >= deadline:
                return out
            self.page.wait_for_timeout(400)

    def rich_text(self, label: str, value: str) -> Entry:
        """
        Put text into a TinyMCE editor, and prove it went in.

        The obligor's narrative tabs — BBFS Details' nineteen boxes, Corporate
        Governance's Governance box — are TinyMCE, not textareas. TinyMCE hides
        the original <textarea> and edits inside an <iframe>, so filling the
        textarea sets a value the editor immediately overwrites with its own
        empty content.

        Three routes, tried in order, and NONE of them is allowed to report
        success on trust:

          1. TinyMCE's own API on this block's editor instance. Deterministic,
             immune to a box being below the fold or mid-initialisation, and it
             hands back both the text the editor now holds and the editor's id.
          2. Typing into the iframe body, for a build where the API is absent.
          3. The plain <textarea>, for the tabs where the same label is not an
             editor at all.

        Every route reads the value back afterwards and raises rather than
        record a value the control does not hold. Route 1 also returns the
        editor id, which the flow uses to catch two labels resolving to one
        editor — the failure that had nineteen BBFS boxes all reporting success
        with the text only ever landing in the first.
        """
        assert_writable()
        block = self._block(label)
        value = str(value)

        # ---- 1. TinyMCE's own API -------------------------------------
        got = self._tiny_set(block, value)
        if got.get("state") == "set":
            if _text_arrived(value, got.get("text", "")):
                self.page.wait_for_timeout(120)
                return self._record(label, value, "rich-text",
                                    target=got.get("id") or "")
            # The API accepted it and the editor does not hold it. Worth
            # falling through rather than failing: a read-only editor answers
            # exactly like this, and route 3 will say so plainly.

        # ---- 2. type into the iframe ----------------------------------
        frame_el = block.locator("iframe").first
        if frame_el.count():
            try:
                # A property, not a method — calling it raises
                # "'FrameLocator' object is not callable".
                frame = frame_el.content_frame
                if frame is not None:
                    body = frame.locator("body").first
                    body.click(timeout=6000)
                    # TinyMCE keeps a bogus <br> in an empty body; select-all
                    # then type replaces it instead of appending to it.
                    self.page.keyboard.press("Control+A")
                    body.type(value, delay=8)
                    self.page.wait_for_timeout(300)
                    if _text_arrived(value, body.inner_text() or ""):
                        return self._record(
                            label, value, "rich-text",
                            target=got.get("id") or self._control_key(block))
            except PWError:
                pass

        # ---- 3. contenteditable, then the plain textarea ---------------
        ce = block.locator('[contenteditable="true"]').first
        if ce.count():
            try:
                ce.click(timeout=6000)
                self.page.keyboard.press("Control+A")
                ce.type(value, delay=8)
                if _text_arrived(value, ce.inner_text() or ""):
                    return self._record(label, value, "rich-text",
                                        target=self._control_key(block))
            except PWError:
                pass

        ta = block.locator("textarea").first
        if ta.count():
            try:
                ta.fill(value)
                if _text_arrived(value, ta.input_value() or ""):
                    return self._record(label, value, "textarea",
                                        target=self._control_key(block))
            except PWError as exc:
                raise FillError(
                    f"{label!r} is a rich-text box that would not accept text: "
                    f"{str(exc)[:120]}")

        raise FillError(
            f"{label!r} is a rich-text box that did not end up holding the "
            f"text. TinyMCE reported {got.get('state', 'nothing')!r}"
            + (f" and the box reads {got.get('text', '')[:60]!r}"
               if got.get("text") else "")
            + ". The value was NOT recorded as entered, because it is not "
              "there.")

    def add_row(self, grid: int = 0, heading: str = "") -> bool:
        """
        Click the '+' that opens a linkage table's add-row dialog.

        Prefer `heading` — the caption above the grid, e.g. "MAJOR BUYER(S)".
        Additional Information carries THIRTEEN grids on one tab, and picking
        the '+' by position there is exactly the kind of positional targeting
        this module exists to avoid: the app renders a grid's '+' only while
        that section is expanded, so an index that meant "Major Buyers"
        yesterday can mean "Major Brands" today. `grid` remains as the fallback
        for a tab with a single unnamed table.

        The '+' is a bare <i class="fa fa-plus">, not a button, so it is found
        by class rather than by label; and 'fa-user-plus' is excluded because
        that is the page's own "create" icon in the shell, not a grid control.
        """
        assert_writable()
        opened = self.page.evaluate(
            """([which, heading]) => {
                const vis = (el) => el.offsetParent !== null || el.getClientRects().length;
                const root = document.querySelector('[data-crawl-root]') || document.body;
                const norm = (s) => (s || '').toLowerCase().replace(/&/g, ' and ')
                    .replace(/[^a-z0-9]+/g, ' ').trim();
                const plus = [...root.querySelectorAll('i, span, a, button')]
                    .filter(el => {
                        const c = typeof el.className === 'string' ? el.className : '';
                        return /fa-plus(?!-)/.test(c) && !/user-plus/.test(c) && vis(el);
                    });
                if (!plus.length) return false;
                let el = null;
                if (heading) {
                    const want = norm(heading);
                    for (const p of plus) {
                        let n = p.parentElement, head = '';
                        for (let k = 0; k < 6 && n && !head; k++, n = n.parentElement) {
                            const h = n.querySelector(
                                'h1,h2,h3,h4,h5,h6,.card-title,.panel-title,legend');
                            if (h) head = norm(h.textContent);
                        }
                        if (head && (head === want || head.includes(want)
                                     || want.includes(head))) { el = p; break; }
                    }
                    if (!el) return false;
                } else {
                    if (which >= plus.length) return false;
                    el = plus[which];
                }
                (el.closest('button, a') || el).click();
                return true;
            }""", [grid, heading])
        if not opened:
            return False
        try:
            self.page.wait_for_selector(".modal.show", timeout=10000)
        except PWError:
            return False
        self.page.wait_for_timeout(700)
        return True

    def toggle(self, label: str, on: bool = True) -> Entry:
        """
        Set a <ui-switch> Yes/No toggle.

        There is no input to set — the control is a button[role=switch] whose
        aria-checked carries the state — so the state is read, clicked only if
        it needs changing, and then confirmed.
        """
        assert_writable()
        block = self._block(label)
        btn = block.locator('button[role="switch"]').first
        if not btn.count():
            raise FillError(f"{label!r} is not a Yes/No switch.")
        now = (btn.get_attribute("aria-checked") or "").lower() == "true"
        if now != on:
            btn.click(timeout=6000)
            self.page.wait_for_timeout(300)
        after = (btn.get_attribute("aria-checked") or "").lower() == "true"
        if after != on:
            raise FillError(
                f"{label!r} would not switch to {'Yes' if on else 'No'} — it "
                f"still reads {'Yes' if after else 'No'}.")
        return self._record(label, "Yes" if on else "No", "switch")

    def check(self, label: str, on: bool = True) -> Entry:
        """
        Tick a checkbox.

        Bootstrap's custom-control markup hides the real <input> and shows a
        styled <label> in its place, so clicking the input times out waiting
        for something that is never visible. The label is the control as far as
        a user is concerned, and clicking it toggles the input.
        """
        assert_writable()
        block = self._block(label)
        box = block.locator("input[type=checkbox]").first
        if not box.count():
            raise FillError(f"{label!r} has no checkbox.")
        if box.is_checked() == on:
            return self._record(label, "true" if on else "false", "checkbox")

        for target in (block.locator("label.custom-control-label").first,
                       block.locator("label").first,
                       box):
            if not target.count():
                continue
            try:
                target.click(timeout=4000)
            except PWError:
                continue
            if box.is_checked() == on:
                return self._record(label, "true" if on else "false", "checkbox")

        # Last resort: the input itself, forced past the visibility check.
        try:
            box.set_checked(on, force=True)
        except PWError as exc:
            raise FillError(
                f"{label!r} could not be ticked: {str(exc)[:120]}")
        return self._record(label, "true" if on else "false", "checkbox")

    # ---- dropdowns ------------------------------------------------------
    # How long to give an ng-select to fill its panel, and the step between
    # looks. A fixed 700ms wait was not enough on this deployment: several of
    # these lists are fetched when the panel opens, and reading them too early
    # reported "opened no options to choose from" for Collateral Priority —
    # static reference data that is never actually empty.
    _PANEL_TIMEOUT_MS = 5000
    _PANEL_STEP_MS = 250

    def _panel_texts(self) -> list[str]:
        """The option rows the open dropdown panel is showing, right now."""
        try:
            return self.page.evaluate(
                """() => [...document.querySelectorAll(
                            '.ng-dropdown-panel .ng-option')]
                        .map(o => (o.textContent || '').trim())
                        .filter(Boolean)""") or []
        except PWError:
            return []

    def _panel_open(self) -> bool:
        try:
            return bool(self.page.locator(
                ".ng-dropdown-panel").first.is_visible(timeout=200))
        except PWError:
            return False

    def _open_panel(self, box) -> list[str]:
        """
        Open an ng-select and wait for its options, returning what it offers.

        Two clicks at most. The panel is opened with a REAL click — a scripted
        one does not open it — and then polled rather than read once, because a
        list this app fetches on open is empty for the first few hundred
        milliseconds and an empty list here is indistinguishable from a field
        with genuinely nothing left to offer.

        A second click is tried only when the panel never opened at all, which
        is the case a single click really does lose: the first click landed
        while the dialog was still settling. It is not tried when the panel is
        open and empty, because that is an answer.

        Rows are returned as the panel renders them, "No items found" included.
        A list with nothing left to offer and a list that has not loaded yet
        both have zero rows for a moment, and only ng-select's own empty row
        tells them apart — so that row is passed up rather than swallowed, and
        the caller decides what it means.

        An empty list is returned, never raised on. Whether "nothing offered"
        is a defect depends on what the caller asked for, and only the caller
        knows.
        """
        for attempt in (1, 2):
            try:
                box.click(timeout=6000)
            except PWError:
                if attempt == 2:
                    return []
                self.page.wait_for_timeout(400)
                continue
            waited = 0
            while waited < self._PANEL_TIMEOUT_MS:
                self.page.wait_for_timeout(self._PANEL_STEP_MS)
                waited += self._PANEL_STEP_MS
                texts = self._panel_texts()
                if texts:
                    return texts
            # A panel that is open and has rendered no row at all — not even an
            # empty one — is as good an answer as this can get. Clicking again
            # would only close it.
            if self._panel_open():
                return []
        return []

    def options(self, label: str) -> list[str]:
        """
        What a dropdown offers. Reads the panel open and closes it again, so it
        is safe to call before deciding a value.

        The panel is dismissed with a neutral click rather than Escape. Escape
        inside a dialog closes the DIALOG — see _dismiss_overlay — so asking a
        dropdown what it offers used to destroy the form the answer was for.
        """
        block = self._block(label)
        box = block.locator("ng-select").first
        if not box.count():
            raise FillError(f"{label!r} is not a dropdown.")
        opts = self._open_panel(box)
        self._dismiss_overlay()
        return opts

    def choose(self, label: str, value: Optional[str] = None) -> Entry:
        """
        Pick from an ng-select. `value=None` takes the first real option, which
        is what the create flow does for fields where any valid value will do.
        """
        assert_writable()
        block = self._block(label)
        box = block.locator("ng-select").first
        if not box.count():
            raise FillError(f"{label!r} is not a dropdown.")

        # Every failure below dismisses the panel with a neutral click rather
        # than Escape. A dropdown that cannot be answered is one failed field;
        # an Escape that closes the dialog underneath it is every remaining
        # field on the form reported missing, which is what used to happen.
        texts = self._open_panel(box)
        if not texts:
            self._dismiss_overlay()
            raise FillError(f"{label!r} opened no options to choose from.")

        panel = self.page.locator(".ng-dropdown-panel .ng-option")
        chosen = None
        if value is None:
            for i, txt in enumerate(texts):
                # Skip the placeholder / "no items" rows.
                if txt and not re.match(r"^-?\s*select\s*-?$|^no items", txt, re.I):
                    panel.nth(i).click(timeout=6000)
                    chosen = txt
                    break
        else:
            want = value.strip().lower()
            idx = next((i for i, t in enumerate(texts) if t.lower() == want), None)
            if idx is None:
                idx = next((i for i, t in enumerate(texts)
                            if want in t.lower()), None)
            if idx is None:
                self._dismiss_overlay()
                raise FillError(
                    f"{label!r} has no option matching {value!r}. "
                    f"Offered: {', '.join(texts[:12])}")
            panel.nth(idx).click(timeout=6000)
            chosen = texts[idx]

        if chosen is None:
            self._dismiss_overlay()
            raise FillError(f"{label!r} offered only placeholder options.")
        self.page.wait_for_timeout(400)
        return self._record(label, chosen, "dropdown")

    def choose_in_dialog(self, value: Optional[str] = None,
                         label: str = "") -> Entry:
        """
        Pick from the dropdown in the dialog that is currently open.

        Needed because not every dropdown has a label[title] to anchor on. The
        Request Type dialog's is labelled "Select RequestType :" in plain text,
        so there is nothing to match on — but a dialog only ever holds one
        dropdown, which makes "the one in this dialog" unambiguous and safer
        than guessing at its label.
        """
        assert_writable()
        dialog = self.page.locator(".modal.show").first
        if not dialog.count():
            raise FillError("No dialog is open to choose in.")
        boxes = dialog.locator("ng-select")
        if not boxes.count():
            raise FillError("The open dialog has no dropdown.")
        if boxes.count() > 1:
            raise FillError(
                f"The open dialog has {boxes.count()} dropdowns, so 'the one in "
                f"this dialog' is ambiguous. Anchor on a label instead.")
        box = boxes.first
        texts = self._open_panel(box)
        if not texts:
            self._dismiss_overlay()
            raise FillError("The dialog's dropdown opened no options.")
        panel = self.page.locator(".ng-dropdown-panel .ng-option")

        if value is None:
            idx = next((i for i, t in enumerate(texts)
                        if t and not re.match(r"^-*\s*select\s*-*$|^no items",
                                              t, re.I)), None)
        else:
            want = value.strip().lower()
            idx = next((i for i, t in enumerate(texts) if t.lower() == want), None)
            if idx is None:
                idx = next((i for i, t in enumerate(texts)
                            if want in t.lower()), None)
        if idx is None:
            self._dismiss_overlay()
            raise FillError(
                f"The dialog's dropdown has no option matching {value!r}. "
                f"Offered: {', '.join(t for t in texts if t)[:200]}")

        panel.nth(idx).click(timeout=6000)
        self.page.wait_for_timeout(400)
        return self._record(label or "Request Type", texts[idx], "dropdown")

    # Every candidate for the closed toggle of an inline treeview dropdown.
    # Ordered most specific first, because `.dropdown-toggle` alone would also
    # match the page's own menus.
    _TREE_TOGGLES = (
        "ngx-dropdown-treeview button.dropdown-toggle",
        "ngx-dropdown-treeview .dropdown-toggle",
        "app-dropdown-tree-dynamic-single-select button.dropdown-toggle",
        "app-dropdown-tree-dynamic-single-select .dropdown-toggle",
        "[class*='treeview'] button.dropdown-toggle",
        "button.dropdown-toggle",
        ".dropdown-toggle",
    )

    def choose_in_tree(self, label: str = "", value: Optional[str] = None,
                       prefer: Optional[list] = None,
                       within: str = "") -> Entry:
        """
        Pick from an inline treeview dropdown — a "Select option" toggle that
        opens a panel with a Search box and a tree of choices.

        This is a THIRD shape of chooser, and it is not either of the other two.
        `choose` drives an <ng-select>; `lookup` clicks a magnifier that opens
        the tree in its own modal. This one has no magnifier and opens no modal:
        the tree drops down inside the dialog that is already there. Requesting
        a facility uses it, and treating it as one of the others finds nothing.

        The tree's top level is a GROUP — "Facility" — not a choice. Clicking a
        group either selects nothing or selects everything under it, so only
        leaves are considered: an item with no ngx-treeview-item inside it.

        `prefer` names products to look for in order, falling back to the first
        selectable leaf. Naming them matters here in a way it does not for a
        reference-data dropdown: which product is requested decides which tabs
        the facility then has.

        `within` confines the search for the TOGGLE to one field's container,
        and a dialog with two of these needs it. The toggle was otherwise
        found as "the first visible one in scope", which is unambiguous only
        while a dialog has a single tree: the Obligor Collateral dialog has
        two — Collateral Classification and Collateral Name — so asking for
        the name re-opened the classification's tree and picked from it. The
        run recorded 'Pledge' as the collateral NAME, a classification, and
        left the panel covering Proceed.

        Only the toggle is confined. The panel it opens is still looked for in
        the dialog as a whole, because only one tree can be open at a time and
        some builds render the panel outside the field's own container.
        """
        assert_writable()
        scope = self._scope()
        toggle_scope = within or scope

        toggle = None
        for sel in self._TREE_TOGGLES:
            loc = self.page.locator(f"{toggle_scope} {sel}")
            try:
                n = min(loc.count(), 6)
            except PWError:
                continue
            for i in range(n):
                cand = loc.nth(i)
                try:
                    if cand.is_visible(timeout=300):
                        toggle = cand
                        break
                except PWError:
                    continue
            if toggle is not None:
                break
        if toggle is None:
            raise FillError(
                "There is no treeview dropdown here to choose in — nothing "
                "matching a 'Select option' toggle is visible"
                + (f" inside {within}." if within else "."))

        try:
            toggle.click(timeout=6000)
        except PWError as exc:
            raise FillError(f"The treeview dropdown would not open: "
                            f"{str(exc).splitlines()[0][:110]}")
        self.page.wait_for_timeout(700)

        items = self.page.locator(
            f"{scope} ngx-treeview-item label.form-check-label"
            f":not(.tree-item-disabled)")
        try:
            items.first.wait_for(state="visible", timeout=10000)
        except PWError:
            pass
        if not items.count():
            raise FillError("The treeview dropdown opened no options.")

        search = self.page.locator(f'{scope} input[placeholder="Search"]').first
        wanted = ([value] if value else list(prefer or [])) or [None]

        for want in wanted:
            if want and search.count():
                try:
                    search.fill(str(want))
                    self.page.wait_for_timeout(900)
                except PWError:
                    pass
            leaf, text = self._first_leaf(items, want)
            if leaf is None:
                continue
            try:
                leaf.click(timeout=8000)
            except PWError:
                continue
            self.page.wait_for_timeout(600)
            self._close_tree(toggle)
            self._settle(8000)
            return self._record(label or "Requested Facility", text, "tree")

        # Nothing preferred was offered. Any valid option will do rather than
        # failing the whole facility over a product name.
        if search.count():
            try:
                search.fill("")
                self.page.wait_for_timeout(900)
            except PWError:
                pass
        leaf, text = self._first_leaf(items, None)
        if leaf is None:
            raise FillError(
                "The treeview dropdown offers no selectable entry — every one "
                "of its items is a group heading or is disabled.")
        try:
            leaf.click(timeout=8000)
        except PWError as exc:
            raise FillError(f"The treeview entry {text!r} could not be "
                            f"clicked: {str(exc).splitlines()[0][:100]}")
        self.page.wait_for_timeout(600)
        self._close_tree(toggle)
        self._settle(8000)
        return self._record(label or "Requested Facility", text, "tree")

    def _first_leaf(self, items, want: Optional[str]):
        """
        The first visible tree entry that is a real choice, not a group.

        A group node is what "Facility" is in the requested-facility tree: it
        has children, and clicking it does not select a product.
        """
        try:
            n = min(items.count(), 200)
        except PWError:
            return None, ""
        needle = (want or "").strip().lower()
        for i in range(n):
            cand = items.nth(i)
            try:
                if not cand.is_visible(timeout=200):
                    continue
                text = (cand.inner_text() or "").strip().replace("\n", " ")
            except PWError:
                continue
            if not text:
                continue
            if needle and needle not in text.lower():
                continue
            try:
                is_group = self.page.evaluate(
                    """(el) => {
                        const item = el.closest('ngx-treeview-item');
                        return !!(item && item.querySelector('ngx-treeview-item'));
                    }""", cand.element_handle())
            except PWError:
                is_group = False
            if is_group:
                continue
            return cand, text[:120]
        return None, ""

    def _close_tree(self, toggle) -> None:
        """
        Shut the panel again. Not cosmetic: an open treeview covers the dialog's
        Proceed button, and a click on a covered button goes to the panel.
        """
        for _ in range(2):
            try:
                if not self.page.locator(
                        "ngx-treeview-item:visible").count():
                    return
                toggle.click(timeout=3000)
            except PWError:
                try:
                    self.page.keyboard.press("Escape")
                except PWError:
                    return
            self.page.wait_for_timeout(400)

    def pick_row_in_dialog(self, value: Optional[str] = None,
                           label: str = "Selection") -> Entry:
        """
        Choose an item from a LIST inside the dialog that is currently open.

        The counterpart to choose_in_dialog, for the dialogs that offer their
        choices as a grid of rows with a checkbox or radio rather than as a
        dropdown. Selecting the requested facility works this way: the app opens
        the facility product list and expects a row to be ticked before Proceed
        becomes meaningful.

        `value=None` takes the first selectable row, which is the same rule the
        rest of this module applies to a dropdown: any valid option will do, and
        hard-coding one would break when the environment's product master is
        re-seeded. The row's own text is recorded, so what was actually chosen
        is in the report and can be re-found afterwards.
        """
        assert_writable()
        dialog = self.page.locator(".modal.show").last
        if not dialog.count():
            raise FillError("No dialog is open to choose in.")
        rows = dialog.locator("table tbody tr")
        try:
            n = rows.count()
        except PWError:
            n = 0
        if not n:
            raise FillError("The open dialog has no list of rows to choose from.")

        want = (value or "").strip().lower()
        seen: list[str] = []
        for i in range(min(n, 80)):
            row = rows.nth(i)
            try:
                if not row.is_visible(timeout=200):
                    continue
                text = (row.inner_text() or "").strip().replace("\n", " ")
            except PWError:
                continue
            if not text or re.search(r"no (data|record|result)", text, re.I):
                continue
            seen.append(text[:60])
            if want and want not in text.lower():
                continue
            box = row.locator("input[type=checkbox], input[type=radio]").first
            if not box.count():
                continue
            # The real input is hidden behind a styled label in this app's
            # markup, exactly as it is on the obligor form, so the label is what
            # a click has to land on.
            for target in (row.locator("label.custom-control-label").first,
                           row.locator("label").first, box):
                if not target.count():
                    continue
                try:
                    target.click(timeout=4000)
                except PWError:
                    continue
                try:
                    if box.is_checked():
                        self.page.wait_for_timeout(400)
                        return self._record(label, text[:120], "row-selection")
                except PWError:
                    pass
            try:
                box.set_checked(True, force=True)
                self.page.wait_for_timeout(400)
                return self._record(label, text[:120], "row-selection")
            except PWError:
                continue

        raise FillError(
            f"No selectable row in the open dialog"
            + (f" matches {value!r}." if value else " could be ticked.")
            + (f" Rows offered: {'; '.join(seen[:6])}" if seen else ""))

    # ---- lookups (the tree modal) ---------------------------------------
    def lookup(self, label: str, value: Optional[str] = None) -> Entry:
        """
        Set a lookup field through its magnifier modal.

        The field's own input is disabled by the app, so this is the only way to
        set it. `value=None` takes the first item in the tree; a string picks the
        item containing it, after typing it into the modal's Search box so long
        trees do not have to be paged through.
        """
        assert_writable()
        block = self._block(label)
        mag = block.locator("i.fa-search").first
        if not mag.count():
            raise FillError(
                f"{label!r} has no lookup magnifier, so it is not a lookup "
                f"field. Try text() or choose() instead.")

        # A previous lookup's backdrop still fading out swallows this click, so
        # the modal never opens and the field is reported unsettable. But only
        # clear it when this field is on the PAGE: a lookup inside an add-row
        # dialog would otherwise dismiss the very dialog being filled, which is
        # what made every field of Sector And Industry look missing.
        already_open = self._scope() != "body"
        if not already_open:
            self._close_modal()
        before = self.page.locator(".modal.show").count()

        # Two attempts with an explicit wait, rather than one click and a fixed
        # sleep: these modals fetch their tree over the network and how long
        # that takes varies with the size of the reference data.
        modal = None
        for attempt in (1, 2):
            try:
                mag.click(timeout=8000)
            except PWError as exc:
                if attempt == 2:
                    raise FillError(
                        f"The lookup magnifier for {label!r} could not be "
                        f"clicked: {str(exc)[:120]}")
                continue
            try:
                # The tree stacks ON TOP of any dialog already open, so wait for
                # one MORE modal and then take the last — .first would keep
                # pointing at the add-row dialog underneath.
                self.page.wait_for_function(
                    "n => document.querySelectorAll('.modal.show').length > n",
                    arg=before, timeout=12000)
                modal = self.page.locator(".modal.show").last
                break
            except PWError:
                if attempt == 2:
                    raise FillError(
                        f"The lookup for {label!r} did not open after two "
                        f"attempts.")
                self.page.wait_for_timeout(1200)
        if modal is None:
            raise FillError(f"The lookup for {label!r} did not open.")

        if value:
            box = modal.locator('input[placeholder="Search"]').first
            if box.count():
                box.fill(str(value))
                self.page.wait_for_timeout(900)

        # The tree arrives with the modal's own request, so wait for an item
        # rather than reading an empty list and calling the lookup empty.
        #
        # Disabled entries are excluded: these trees include unselectable
        # grouping nodes (District offered 'Kashmir' with
        # class="tree-item-disabled" first), and clicking one just times out.
        items = modal.locator(
            "ngx-treeview-item label.form-check-label:not(.tree-item-disabled)")
        try:
            items.first.wait_for(state="visible", timeout=10000)
        except PWError:
            pass
        n = items.count()
        if not n:
            total = modal.locator(
                "ngx-treeview-item label.form-check-label").count()
            self._close_down_to(before)
            if total:
                raise FillError(
                    f"The lookup for {label!r} offers {total} entr(ies) but "
                    f"every one is unselectable — it probably depends on "
                    f"another field being set first.")
            raise FillError(
                f"The lookup for {label!r} offered nothing"
                + (f" matching {value!r}." if value else "."))

        chosen, idx = None, None
        if value:
            want = str(value).strip().lower()
            texts = [(items.nth(i).inner_text() or "").strip() for i in range(n)]
            idx = next((i for i, t in enumerate(texts) if t.lower() == want), None)
            if idx is None:
                idx = next((i for i, t in enumerate(texts)
                            if want in t.lower()), None)
            if idx is None:
                self._close_down_to(before)
                raise FillError(
                    f"The lookup for {label!r} has no entry matching {value!r}. "
                    f"Offered: {'; '.join(texts[:8])}")
            chosen = texts[idx]
        else:
            idx = 0
            chosen = (items.nth(0).inner_text() or "").strip()

        try:
            items.nth(idx).click(timeout=8000)
        except PWError as exc:
            # Raised as a FillError so the caller records it and carries on.
            # A raw Playwright timeout escaping from here aborted an entire
            # run over one unselectable tree entry.
            self._close_down_to(before)
            raise FillError(
                f"The lookup entry {chosen!r} for {label!r} could not be "
                f"clicked: {str(exc).splitlines()[0][:100]}")
        self.page.wait_for_timeout(700)

        # Single-select trees usually close themselves. When one does not, it is
        # waiting for its own confirm button — which must still clear the
        # committable allowlist.
        if self.page.locator(".modal.show").count() > before:
            for name in ("Select", "OK", "Done", "Apply", "Save"):
                btn = self.page.locator(
                    f'.modal.show >> nth=-1 >> button:has-text("{name}")').first
                if btn.count() and _committable(name):
                    try:
                        btn.click(timeout=4000)
                        break
                    except PWError:
                        continue
            self.page.wait_for_timeout(400)

        # Close ONLY the tree, never the dialog underneath it. _close_modal
        # presses Escape until nothing is open, which would take the add-row
        # dialog with it and lose everything already typed into it.
        for _ in range(3):
            if self.page.locator(".modal.show").count() <= before:
                break
            try:
                self.page.keyboard.press("Escape")
            except PWError:
                break
            self.page.wait_for_timeout(400)
        self._settle(8000)
        return self._record(label, chosen, "lookup")

    def _close_modal(self) -> None:
        """Close every open overlay. Only safe when nothing underneath matters."""
        try:
            cr._dismiss_overlays(self.page)
        except PWError:
            pass

    def _close_down_to(self, count: int) -> None:
        """
        Close overlays until only `count` remain.

        The distinction from _close_modal matters a great deal. A lookup tree
        opens ON TOP of an add-row dialog, so dismissing everything when the
        tree misbehaves takes the dialog with it — along with every field
        already typed into it. That turned one unselectable District entry into
        'Country is not on this screen', a failed save, and a lost row.
        """
        for _ in range(4):
            try:
                if self.page.locator(".modal.show").count() <= count:
                    return
                self.page.keyboard.press("Escape")
            except PWError:
                return
            self.page.wait_for_timeout(400)

    # ---- files ----------------------------------------------------------
    def upload(self, path: str, label: str = "",
               strict: bool = False) -> Entry:
        """
        Attach a file to a named field.

        Scoped to that field's own block where a label is given, and only
        falling back to "the first file input on the screen" when there is no
        label to go on. The Documents screen is why: its Document Action panel
        carries more than one file input, so the first one in the DOM is not
        necessarily the field that was asked for, and attaching to the wrong
        one is the same class of mistake as typing into the wrong box.

        `strict` refuses that fallback outright. A caller wants it when the
        wrong file input would not merely be the wrong box but the wrong
        FEATURE: the Conditions screen carries a bulk importer in its sidebar,
        and quietly handing it a generated PNG would report an import failure
        that is nothing but this run feeding it the wrong kind of file.

        set_input_files rather than clicking a paperclip and driving a native
        dialog: the input needs no be visible for it, which matters because
        this app hides every one of them behind an icon, and nothing can block
        an unattended run on a file chooser that never opens.
        """
        assert_writable()
        if not os.path.exists(path):
            raise FillError(f"There is no file at {path!r} to attach.")

        inp = None
        if label:
            try:
                candidate = self._block(label).locator(
                    'input[type=file]').first
                if candidate.count():
                    inp = candidate
            except FillError:
                inp = None          # named field not here; fall back below

        if inp is None and not strict:
            # Prefer an open dialog's input over the page's, for the same
            # reason _scope prefers a dialog's fields.
            for sel in (f'{self._scope()} input[type=file]',
                        'input[type=file]'):
                loc = self.page.locator(sel)
                if loc.count():
                    inp = loc.first
                    break
        if inp is None and strict:
            raise FillError(
                f"There is no file input inside the {label or 'named'!r} "
                f"field, and nothing was attached: falling back to the first "
                f"file input on the screen was refused, because on this "
                f"screen that one belongs to another form.")
        if inp is None:
            raise FillError(
                f"There is no file input on this screen to attach "
                f"{os.path.basename(path)!r} to"
                + (f" — and no field labelled {label!r} either." if label
                   else "."))

        inp.set_input_files(path)
        self.page.wait_for_timeout(900)
        return self._record(label or "Attachment", os.path.basename(path),
                            "file")

    def file_inputs(self) -> int:
        """How many file inputs the current scope is offering. Read only."""
        try:
            return self.page.locator(
                f'{self._scope()} input[type=file]').count()
        except PWError:
            return 0

    # ---- committing -----------------------------------------------------
    def commit(self, label: str = "Save", expect_settle: bool = True) -> str:
        """
        Click a named commit button. This is the one place data is written.

        Refuses anything outside COMMITTABLE, and anything in NEVER, even when a
        flow asks for it — a mistake in a flow must not be able to approve a
        case or delete a record.
        """
        assert_writable()
        if not _committable(label):
            raise WriteRefused(
                f"{label!r} is not a permitted commit control. Allowed: Save, "
                f"Save & Next, Proceed, Add, OK, Done, Select, Apply, Update, "
                f"Submit — and never Approve / Reject / Delete / Forward.")

        scope = self._scope()
        btn = None
        # Every match is considered, not just the first. Taking .first was a
        # real bug: a dismissed add-row dialog stays in the DOM with its own
        # hidden Save, and being earlier in document order it shadowed the
        # tab's real one — so four tabs reported "no Save button" on a page
        # that plainly had one.
        for sel in (f'{scope} button:has-text("{label}")',
                    f'{scope} a.btn:has-text("{label}")',
                    f'button:has-text("{label}")',
                    f'a.btn:has-text("{label}")'):
            loc = self.page.locator(sel)
            try:
                n = loc.count()
            except PWError:
                continue
            for i in range(min(n, 12)):
                cand = loc.nth(i)
                try:
                    if not cand.is_visible(timeout=400):
                        continue
                    if cand.is_disabled(timeout=400):
                        continue
                    # Exact-ish: "Save" must not match "Save and Approve".
                    text = (cand.inner_text() or "").strip().lower()
                    if text and not _committable(text):
                        continue
                except PWError:
                    continue
                btn = cand
                break
            if btn is not None:
                break
        if btn is None:
            raise FillError(
                f"No enabled {label!r} button is visible on this screen.")

        self.session.recorder.clear()
        try:
            btn.scroll_into_view_if_needed(timeout=4000)
        except PWError:
            pass
        btn.click(timeout=10000)
        if expect_settle:
            self._settle(25000)
            try:
                cr.stamp_content_root(self.page)
            except PWError:
                pass
        self.page.wait_for_timeout(800)
        return f"clicked {label}"

    # ---- reading back ---------------------------------------------------
    def value_of(self, label: str) -> str:
        """
        What the app is showing for a field right now.

        Used to confirm a save took and by the verification leg, so it has to
        cover every control kind this app uses — not just inputs. Reading only
        inputs and ng-selects made <ui-switch> toggles and TinyMCE boxes look
        EMPTY, and the round trip then reported their values as lost when they
        were plainly on screen.

        Returns "" only when there is genuinely nothing to read.
        """
        try:
            block = self._block(label)
        except FillError:
            return ""

        ng = block.locator("ng-select .ng-value-label").first
        if ng.count():
            try:
                got = (ng.inner_text() or "").strip()
                if got:
                    return got
            except PWError:
                pass

        # A <ui-switch> keeps its state in aria-checked, not in any input.
        sw = block.locator('button[role="switch"]').first
        if sw.count():
            try:
                on = (sw.get_attribute("aria-checked") or "").lower() == "true"
                return "Yes" if on else "No"
            except PWError:
                pass

        # TinyMCE: the visible text lives in the iframe, and the hidden
        # textarea it replaced is not kept in step with it. Ask the editor
        # itself first — reading through the iframe needs the iframe to be
        # laid out, and on a tab of nineteen editors several are not.
        try:
            got = self._eval_on(block, self._TINY_GET_JS, timeout=2000)
            if got:
                return got
        except PWError:
            pass

        frame_el = block.locator("iframe").first
        if frame_el.count():
            try:
                frame = frame_el.content_frame
                if frame is not None:
                    body = frame.locator("body").first
                    if body.count():
                        got = (body.inner_text() or "").strip()
                        if got:
                            return got
            except PWError:
                pass

        ce = block.locator('[contenteditable="true"]').first
        if ce.count():
            try:
                got = (ce.inner_text() or "").strip()
                if got:
                    return got
            except PWError:
                pass

        box = block.locator("input[type=checkbox]").first
        if box.count():
            try:
                return "true" if box.is_checked() else "false"
            except PWError:
                pass

        ctl = block.locator("input:not([type=hidden]), textarea").first
        if ctl.count():
            try:
                got = (ctl.input_value() or "").strip()
                if got:
                    return got
            except PWError:
                pass
        return ""

    def messages(self) -> dict:
        """
        Whatever the app said: toasts, banners, and inline field errors.

        Field errors are read from the open dialog when there is one. A linkage
        table's add-row form sits on top of a page that has its own stale
        validation text, and mixing the two makes a row that saved perfectly
        look like it failed.

        Toasts are always read from the whole document: they render outside the
        dialog, at the page level.
        """
        try:
            return self.page.evaluate(
                """(scope) => {
                    const clean = (s) => (s||'').trim().replace(/\\s+/g,' ');
                    const vis = (el) => el.offsetParent !== null ||
                                        el.getClientRects().length > 0;
                    const grab = (root, sels) => {
                        const out = [];
                        if (!root) return out;
                        for (const s of sels)
                            for (const el of root.querySelectorAll(s)) {
                                if (!vis(el)) continue;
                                const t = clean(el.textContent);
                                if (t && t.length < 300 && !out.includes(t)) out.push(t);
                            }
                        return out.slice(0, 10);
                    };
                    const dlg = document.querySelector(scope) || document;
                    return {
                        ok: grab(document, ['.toast-success', '.alert-success',
                                            '.swal2-success', '.swal2-html-container']),
                        bad: grab(document, ['.toast-error', '.alert-danger',
                                             '.toast-danger', '.swal2-error']),
                        fields: grab(dlg, ['.invalid-feedback', '.text-danger',
                                           '.error-msg', 'mat-error']),
                    };
                }""", self._scope())
        except PWError:
            return {"ok": [], "bad": [], "fields": []}

    def unsatisfied(self) -> list[str]:
        """
        Inline messages that read like an unmet rule, in the scope being filled.

        Filters to actual validation wording so a red hint or a stray styled
        span does not get reported as a blocker.
        """
        looks = re.compile(
            r"required|invalid|must be|cannot|should be|not valid|mandatory|"
            r"minimum|maximum|at least|digits?|characters?", re.I)
        return [m for m in self.messages()["fields"] if looks.search(m)]

