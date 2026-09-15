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


class FrozenField(FillError):
    """
    The control exists but the application has deliberately locked it.

    Its own class because it is NOT a defect and must not be reported as one.
    The PR checklist freezes some factor values on purpose — Margin
    Requirement is the confirmed example — so a flow catches this, records
    "skipped, frozen field" and carries on, while an ordinary FillError still
    means something is wrong.
    """


def assert_writable(url: str = "") -> str:
    """
    Gate 1. Called by every entry point here, and again before each commit.

    `url` checks a SPECIFIC page rather than the configured base. That matters
    because not every screen in this application lives on the configured host:
    PR Checklist's 'Perform PR' opens a different app on a different machine
    (an .aspx page on 10.11.12.10) in a new tab. Checking only
    crawler_config.BASE_URL would have said "approved" while the run typed
    into a host nobody had approved — the allowlist would have been a
    formality. Callers acting on a page other than the main one pass its URL.
    """
    target = url or crawler_config.BASE_URL
    allowed, reason = settings.write_allowed(target)
    if not allowed:
        raise WriteRefused(
            f"Data entry is not permitted against {target}. {reason}")
    return reason


# Controls a flow may commit with. Anything else is refused even here, so a
# typo in a flow cannot reach Approve, Reject, Delete or a workflow transition
# that was never intended.
COMMITTABLE = [
    r"^save$", r"^save\s*&?\s*next$", r"^proceed$", r"^add$", r"^ok$",
    r"^done$", r"^select$", r"^apply$", r"^update$", r"^submit$",
]

# Never committed by this module regardless of what a flow asks for. These end a
# case or a session rather than saving a record.
NEVER = [
    r"\bapprove\b", r"\breject\b", r"\bdelete\b", r"\bremove\b",
    r"\bauthorize\b", r"\bsign ?out\b", r"\blog ?out\b", r"\bwithdraw\b",
    r"\brelease\b", r"\bbulk\b", r"\bforward\b", r"\breturn\b",
]


# The application keeps an ngx-ui-loader overlay in the DOM permanently. While
# it is "up" it is opacity:0 but full-screen, z-index 99998 and
# pointer-events:auto, so it swallows clicks without any visible sign. This is
# the condition for "no overlay is currently eating pointer events" — opacity
# is deliberately NOT part of it, because an invisible overlay blocks just as
# effectively as a visible one.
_NO_BLOCKING_OVERLAY = """() => {
    const o = [...document.querySelectorAll('.ngx-overlay, .loading-foreground')];
    return !o.some(el => {
        const s = getComputedStyle(el);
        return s.display !== 'none' && s.visibility !== 'hidden'
               && s.pointerEvents !== 'none'
               && el.getBoundingClientRect().width > 0;
    });
}"""


def _norm_label(text: str) -> str:
    """Compare two row or field names without punctuation or case."""
    s = (text or "").strip().lower().replace("&", " and ")
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", s)).strip()


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

    def as_dict(self) -> dict:
        return {"label": self.label, "value": self.value, "kind": self.kind,
                "screen": self.screen, "group": self.group or self.screen}


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
    entries: list[Entry] = field(default_factory=list)
    _gate_reason: str = ""

    def __post_init__(self) -> None:
        self._gate_reason = assert_writable()

    # ---- locating -------------------------------------------------------
    @property
    def page(self):
        return self.session.page

    def _scope(self) -> str:
        """Prefer an open dialog: an add-row form's fields shadow the page's."""
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

    # Does this candidate hold exactly ONE field's label?
    #
    # A container that spans several fields hands the wrong control to
    # whichever is asked for first, and it does so silently. Group Review is
    # what made that concrete: seven TinyMCE editors and their seven labels are
    # all direct children of ONE <fieldset class="form-group">, so
    # `fieldset:has(> label[title="Group Background"])` matched that fieldset —
    # and so did the same selector for the other six. Every read and every
    # write went to the first editor, and six sections' text landed on top of
    # each other in one box.
    _ONE_FIELD_JS = r"""(el) => {
        const vis = (x) => x.offsetParent !== null || x.getClientRects().length > 0;
        // Labels that are part of a control, not the name of a field.
        const own = (l) => {
            const c = typeof l.className === 'string' ? l.className : '';
            return !/custom-control-label|form-check-label/.test(c)
                   && (l.textContent || '').trim().length > 0;
        };
        return [...el.querySelectorAll('label, .control-label, .form-label')]
            .filter(l => vis(l) && own(l)).length <= 1;
    }"""

    # The control that FOLLOWS this label, for a form laid out FLAT: one
    # container holding label, control, label, control … Every label's
    # smallest control-bearing ancestor is then that same container, so both
    # anchors above give the first control every time. Reading forward from
    # the label to the next label is how a person reads such a form, and it is
    # still anchored on the label — never on an index among like controls.
    _STAMP_AFTER_LABEL_JS = r"""([scope, wanted, ctrlSel]) => {
        const root = document.querySelector(scope) || document.body;
        const vis = (el) => el.offsetParent !== null || el.getClientRects().length > 0;
        const norm = (s) => (s || '').toLowerCase().replace(/&/g, ' and ')
            .replace(/[^a-z0-9]+/g, ' ').trim();
        const want = norm(wanted);
        if (!want) return -1;
        const LABEL_SEL = 'label, .control-label, .form-label';
        const own = (l) => {
            const c = typeof l.className === 'string' ? l.className : '';
            return !/custom-control-label|form-check-label/.test(c)
                   && (l.textContent || '').trim().length > 0;
        };
        const labels = [...root.querySelectorAll(LABEL_SEL)]
            .filter(l => vis(l) && own(l));
        const i = labels.findIndex(
            l => norm(l.getAttribute('title') || l.textContent) === want);
        if (i < 0) return -1;
        const L = labels[i];
        const N = labels[i + 1] || null;      // the next field's label
        const follows = (a, b) =>
            !!(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING);

        // The first visible control after this label and before the next.
        let C = null;
        for (const c of root.querySelectorAll(ctrlSel)) {
            if (!vis(c)) continue;
            if (!follows(L, c)) continue;
            if (N && follows(N, c)) continue;
            C = c;
            break;
        }
        if (!C) return -1;

        // Widen to the largest wrapper that still names no field and still
        // stops short of the next one — for TinyMCE that is .tox-tinymce,
        // which is what holds the <iframe> the text actually goes into.
        let node = C;
        while (node.parentElement && node.parentElement !== root) {
            const p = node.parentElement;
            if ([...p.querySelectorAll(LABEL_SEL)].some(l => vis(l) && own(l)))
                break;
            if (N && p.contains(N)) break;
            node = p;
        }
        document.querySelectorAll('[data-fill-field]').forEach(
            e => e.removeAttribute('data-fill-field'));
        node.setAttribute('data-fill-field', '0');
        return 0;
    }"""

    def _block(self, label: str):
        """
        The element that holds one field: its label plus its control.

        Three anchors, tried in that order, and each is refused if it turns out
        to span more than one field:

          1. `label[title="..."]` — the strongest where it exists. The obligor
             form emits it on every field and it carries the clean name
             without the mandatory asterisk.
          2. the label's own TEXT, climbing to the smallest ancestor holding
             that label and a control. Most case screens emit a bare <label>.
          3. the control that FOLLOWS the label, for a form laid out flat —
             one container, label/control/label/control. Neither anchor above
             can separate those, because the smallest ancestor holding any one
             label and a control is the container holding them all.

        Scoped to an open dialog when there is one, because this app reuses the
        same field names inside add-row forms.
        """
        scope = self._scope()
        esc = label.replace('"', '\\"')
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
                if not self.page.evaluate(self._ONE_FIELD_JS,
                                          loc.element_handle()):
                    continue          # spans other fields — try a tighter one
                return loc
            except PWError:
                continue

        # Fall back to the label's own text, then to the control after it.
        for js in (self._STAMP_FIELD_JS, self._STAMP_AFTER_LABEL_JS):
            try:
                found = self.page.evaluate(js, [scope, label, self._CTRL_SEL])
            except PWError:
                continue
            if found < 0:
                continue
            loc = self.page.locator('[data-fill-field]').first
            try:
                if loc.count() and loc.is_visible(timeout=400):
                    return loc
            except PWError:
                continue

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
        """
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
                }""", [self._scope(), self._CTRL_SEL])[:limit]
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

    def _record(self, label: str, value: str, kind: str) -> Entry:
        e = Entry(label=label, value=str(value), kind=kind, screen=self.screen,
                  group=self.group or self.screen)
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
        self._close_calendar(ctl)
        return True

    def _calendar_open(self) -> bool:
        try:
            return self.page.locator(
                "bs-datepicker-container, bs-daterangepicker-container, "
                ".bs-datepicker").first.is_visible(timeout=300)
        except PWError:
            return False

    def _close_calendar(self, ctl) -> None:
        """
        Dismiss the calendar WITHOUT dismissing what is behind it.

        Escape is what closes this picker, and it used to be pressed
        unconditionally. Inside a modal that is a trap: Escape closes the
        DIALOG as well, and on the Litigation form it threw the whole entry
        form away — along with the field that had already been answered, which
        then read back empty and looked like the application losing a value.

        So: nothing is pressed unless a calendar is actually open. Clicking a
        day closes it already, which is the usual case and now costs nothing.
        When one IS open and a modal is up, the trigger is clicked again to
        toggle it shut, because that cannot reach the dialog. Escape stays as
        the last resort, and on a screen with no modal it is what it always
        was.
        """
        if not self._calendar_open():
            return
        if self._scope() != "body":
            try:
                ctl.click(timeout=3000)
                self.page.wait_for_timeout(300)
            except PWError:
                pass
            if not self._calendar_open():
                return
        try:
            ctl.press("Escape")
        except PWError:
            pass

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

        attempts: list[str] = []

        # Calendar first — it cannot be misparsed.
        if self._calendar_pick(ctl, when):
            got = (ctl.input_value() or "").strip()
            attempts.append(f"calendar -> {got or '(empty)'}")
            if self._date_taken(when, got):
                return self._record(label, got, "date")

        for fmt in self._DATE_FORMATS:
            wanted = when.strftime(fmt)
            try:
                ctl.click(timeout=6000)
                ctl.fill("")                       # clear any partial value
                ctl.fill(wanted)                   # one operation, not per-key
                self._close_calendar(ctl)          # never with Escape in a modal
                ctl.press("Tab")                   # blur so Angular parses it
            except PWError as exc:
                attempts.append(f"{wanted} -> {str(exc)[:40]}")
                continue
            # Long enough for the picker to normalise the text. Without this the
            # raw string is read back and a misparse looks like a success.
            self.page.wait_for_timeout(1200)
            got = (ctl.input_value() or "").strip()
            attempts.append(f"{wanted} -> {got or '(empty)'}")
            if self._date_taken(when, got):
                return self._record(label, got, "date")

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

    def rich_text(self, label: str, value: str) -> Entry:
        """
        Type into a TinyMCE editor.

        The obligor's narrative tabs — BBFS Details' nineteen boxes, Corporate
        Governance's Governance box — are TinyMCE, not textareas. TinyMCE hides
        the original <textarea> and edits inside an <iframe>, so filling the
        textarea sets a value the editor immediately overwrites with its own
        empty content. The iframe's body is the real target.

        Falls back through contenteditable and then the plain textarea, because
        the same label is a plain textarea on some tabs and an editor on others.
        """
        assert_writable()
        block = self._block(label)

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
                    body.type(str(value), delay=8)
                    self.page.wait_for_timeout(300)
                    return self._record(label, value, "rich-text")
            except PWError:
                pass

        ce = block.locator('[contenteditable="true"]').first
        if ce.count():
            try:
                ce.click(timeout=6000)
                self.page.keyboard.press("Control+A")
                ce.type(str(value), delay=8)
                return self._record(label, value, "rich-text")
            except PWError:
                pass

        ta = block.locator("textarea").first
        if ta.count():
            try:
                ta.fill(str(value))
                return self._record(label, value, "textarea")
            except PWError as exc:
                raise FillError(
                    f"{label!r} is a rich-text box that would not accept text: "
                    f"{str(exc)[:120]}")
        raise FillError(f"{label!r} has no editor or textarea to type into.")

    # ---- grids ----------------------------------------------------------
    #
    # One visible input on My Bucket, and it is unambiguous:
    # <input type="search" class="form-control form-control-sm"> inside
    # [data-crawl-root]. The placeholder variants are for a deployment that
    # renders a plain text box instead.
    _SEARCH_SEL = (
        '[data-crawl-root] input[type="search"]',
        '[data-crawl-root] input[placeholder*="Search"]',
        '[data-crawl-root] input[placeholder*="search"]',
        'input[type="search"]',
    )

    def search_grid(self, value: str, settle_ms: int = 20000) -> bool:
        """
        Type into a grid's own search box so the grid filters, and wait for it.

        This is the one thing this module types that is NOT a value being
        entered into a record, and it is deliberately **not recorded** as an
        Entry. A search term is navigation, not data: recording it would put
        it into the round-trip comparison, which exists to compare what was
        entered against what the record came back holding.

        Everything else about it is unchanged — the host allowlist is checked
        first, and nothing but a search field is touched.

        Returns False when the screen has no search box, so a caller can fall
        back to paging rather than fail. A box that is present and refuses the
        value raises, because that is a finding rather than an absence.
        """
        assert_writable()
        box = None
        for sel in self._SEARCH_SEL:
            loc = self.page.locator(sel).first
            try:
                if (loc.count() and loc.is_visible(timeout=800)
                        and not loc.is_disabled(timeout=500)):
                    box = loc
                    break
            except PWError:
                continue
        if box is None:
            return False

        try:
            box.click(timeout=6000)
            box.fill("")
            box.fill(str(value))
            # Enter for a box that searches on submit; the settle below covers
            # the filter-as-you-type kind, which needs no key at all.
            box.press("Enter")
        except PWError as exc:
            raise FillError(
                f"The grid's search box would not accept {value!r}: "
                f"{str(exc)[:120]}")

        self._settle(settle_ms)
        try:
            cr.stamp_content_root(self.page)
        except PWError:
            pass
        self.page.wait_for_timeout(900)
        return True

    # ---- a grid whose row labels are in a DIFFERENT TABLE ---------------
    #
    # Business Performance is built this way, and it defeats every anchor in
    # _block. The ten metric names are one <table>; the twenty numeric boxes
    # are another; and the boxes carry no label, no title, no aria-label, no
    # formcontrolname, the same name="id" on all of them and a duplicate
    # id="false" or id="true" on every one. There is nothing on an input that
    # says which metric it belongs to. field_labels() finds nothing here, and
    # correctly: there are no labelled fields on this screen.
    #
    # The join can therefore only be the row's POSITION in its table, which is
    # the one thing this module refuses to target by everywhere else. What
    # makes it safe HERE, and what the guards below enforce:
    #
    #   * the caller never supplies a position. It names a metric, and the
    #     name is looked up in the label table.
    #   * the two tables must have exactly as many rows as each other. A
    #     layout change that broke the correspondence changes a row count, and
    #     is refused rather than typed into.
    #   * the metric must name exactly ONE row. Two matches is ambiguous and
    #     is refused.
    #   * exactly one label table and one value table may pair up. On a screen
    #     with two such grids visible at once, which one was meant cannot be
    #     told, so nothing is typed.
    #
    # Every refusal raises rather than falling back to a guess, because a
    # wrong guess here writes a number into another metric's box — the same
    # class of fault as the shared-fieldset bug on Group Review, and silent in
    # exactly the same way.
    _CELL_SEL = 'input:not([type=hidden]), textarea'

    _GRID_JS = r"""(payload) => {
        const [want, column, doStamp] = payload;
        const INP = 'input:not([type=hidden]), textarea';
        const norm = (s) => (s || '').toLowerCase().replace(/&/g, ' and ')
            .replace(/[^a-z0-9]+/g, ' ').trim();
        const vis = (e) => e.offsetParent !== null
                        || e.getClientRects().length > 0;
        const root = document.querySelector('[data-crawl-root]')
                  || document.body;
        root.querySelectorAll('[data-crawl-cell]').forEach(
            (e) => e.removeAttribute('data-crawl-cell'));

        const shape = [...root.querySelectorAll('table')].filter(vis)
            .map((t) => {
                const rows = [...t.querySelectorAll('tr')].filter(vis);
                return {t: t, rows: rows,
                        valueRows: rows.filter(
                            (r) => r.querySelectorAll(INP).length)};
            });
        const labels = shape.filter(
            (s) => s.valueRows.length === 0 && s.rows.length > 0);
        const values = shape.filter((s) => s.valueRows.length > 0);
        if (!labels.length)
            return {ok: false, why: 'this screen has no table of row labels'};
        if (!values.length)
            return {ok: false, why: 'this screen has no table of input boxes'};

        const pairs = [];
        for (const L of labels)
            for (const V of values)
                if (V.valueRows.length === L.rows.length) pairs.push([L, V]);
        if (!pairs.length)
            return {ok: false, why: 'no label table has as many rows as a '
                    + 'table of inputs (labels: '
                    + labels.map((l) => l.rows.length).join('/')
                    + ', inputs: '
                    + values.map((v) => v.valueRows.length).join('/') + ')'};
        if (pairs.length > 1)
            return {ok: false, why: pairs.length + ' label/input table '
                    + 'pairings are possible, so which grid is meant cannot '
                    + 'be told'};

        const L = pairs[0][0], V = pairs[0][1];
        const names = L.rows.map(
            (r) => (r.innerText || '').replace(/\s+/g, ' ').trim());
        const cells = V.valueRows.map(
            (r) => [...r.querySelectorAll(INP)].map((i) => ({
                value: (i.value || '').trim(),
                readonly: i.disabled === true || i.readOnly === true})));

        // The column headers sit outside both tables, in the panel's own
        // header block, and the inputs carry no text of their own — so the
        // LAST leaf texts on the panel are the column names. Validated
        // before use: a candidate that repeats a metric name, or that reads
        // like the period above it, means the layout is not what this
        // expects, and the caller is given plain column numbers instead of a
        // name that might be wrong.
        const wide = cells.length ? cells[0].length : 0;
        let cols = [];
        if (wide) {
            const leaves = [];
            for (const e of root.querySelectorAll('span, th, td, h6, div, p')) {
                if (e.children.length || !vis(e)) continue;
                const t = (e.textContent || '').trim();
                if (t && t.length < 60) leaves.push(t);
            }
            const tail = leaves.slice(-wide);
            const known = names.map(norm);
            const ok = tail.length === wide && tail.every(
                (t) => t && !known.includes(norm(t))
                       && !/\d{4}/.test(t));
            if (ok) cols = tail;
        }
        const info = {ok: true, names: names, cells: cells, columns: cols,
                      wide: wide};
        if (!want) return info;

        const hits = [];
        names.forEach((n, i) => { if (norm(n) === norm(want)) hits.push(i); });
        if (!hits.length)
            return Object.assign(info, {ok: false,
                why: 'no row is named ' + JSON.stringify(want)});
        if (hits.length > 1)
            return Object.assign(info, {ok: false, why: hits.length
                + ' rows are named ' + JSON.stringify(want)});

        const ri = hits[0];
        const inputs = [...V.valueRows[ri].querySelectorAll(INP)];
        if (column < 0 || column >= inputs.length)
            return Object.assign(info, {ok: false, why: 'row '
                + JSON.stringify(want) + ' has ' + inputs.length
                + ' column(s), so there is no column ' + (column + 1)});
        const target = inputs[column];
        if (doStamp) target.setAttribute('data-crawl-cell', '1');
        return Object.assign(info, {ok: true, row: ri, rowName: names[ri],
            value: (target.value || '').trim(),
            readonly: target.disabled === true || target.readOnly === true});
    }"""

    def paired_grid(self) -> dict:
        """
        The shape of a two-table grid: its row names, column names and cells.

        Read-only, and the basis of everything else here — a caller uses it to
        learn what the screen is actually showing rather than assuming the
        specification's field list is what got rendered.
        """
        try:
            got = self.page.evaluate(self._GRID_JS, ["", 0, False])
        except PWError as exc:
            return {"ok": False, "why": f"the grid could not be read: "
                                        f"{str(exc)[:120]}"}
        return got or {"ok": False, "why": "the grid could not be read"}

    def paired_cell(self, label: str, column: int = 0) -> dict:
        """One cell of a two-table grid: its value and whether it is writable."""
        try:
            got = self.page.evaluate(self._GRID_JS, [label, column, False])
        except PWError as exc:
            return {"ok": False, "why": str(exc)[:120]}
        return got or {"ok": False, "why": "the grid could not be read"}

    def paired_cell_value(self, label: str, column: int = 0) -> str:
        """What the grid is showing for one metric, in one column."""
        got = self.paired_cell(label, column)
        return str(got.get("value") or "") if got.get("ok") else ""

    def grid_cell(self, label: str, value: str, column: int = 0,
                  column_name: str = "") -> Entry:
        """
        Type a number into one cell of a two-table grid, found by its row name.

        A read-only cell raises rather than being skipped quietly: on this
        screen read-only means the application computes the figure, which is
        something a report should say out loud rather than pass over.
        """
        assert_writable()
        got = self.page.evaluate(self._GRID_JS, [label, column, True])
        if not got or not got.get("ok"):
            why = (got or {}).get("why", "the grid could not be read")
            raise FillError(f"No grid row labelled {label!r} could be written "
                            f"to: {why}.")
        if got.get("readonly"):
            raise FillError(
                f"{label!r} column {column + 1} is read-only — the "
                f"application computes it, so there is nothing to enter. It "
                f"currently shows {got.get('value')!r}.")

        ctl = self.page.locator('[data-crawl-cell="1"]').first
        if not ctl.count():
            raise FillError(f"{label!r} column {column + 1} could not be "
                            f"located after being found in the grid.")
        try:
            ctl.click(timeout=6000)
            ctl.fill("")
            ctl.fill(str(value))
            # Angular recomputes the totals on blur, and leaves the box
            # untouched — so still invalid to Save — without it.
            ctl.press("Tab")
        except PWError as exc:
            raise FillError(f"{label!r} column {column + 1} would not accept "
                            f"{value!r}: {str(exc)[:120]}")
        name = f"{label} — {column_name}" if column_name else \
               f"{label} — column {column + 1}"
        return self._record(name, value, "number")

    # ---- Business Performance: one label table, many period blocks -------
    #
    # paired_grid above deliberately refuses this screen once a second period
    # exists — it finds two input tables with the same row count as the label
    # table and cannot tell which is meant, which is the right answer for a
    # method whose job is "the grid". But Business Performance's '+ Add' makes
    # exactly that shape on purpose: each period is its OWN table, sitting to
    # the right of the last, sharing the one table of row names.
    #
    # Measured on the live screen after adding a period:
    #     table 0   10 rows, no inputs          x=499   the row names
    #     table 1   10 value rows, 2 inputs     x=667   the existing period
    #     table 2   10 value rows, 2 inputs     x=963   the one just added
    #
    # So a block is addressed by INDEX in document order, which is also
    # left-to-right on screen, and the period just added is the last one.
    _BLOCK_JS = r"""([want, column, block, doStamp]) => {
        const INP = 'input:not([type=hidden]), textarea';
        const norm = (s) => (s || '').toLowerCase().replace(/&/g, ' and ')
            .replace(/[^a-z0-9]+/g, ' ').trim();
        const vis = (e) => e.offsetParent !== null
                        || e.getClientRects().length > 0;
        const root = document.querySelector('[data-crawl-root]')
                  || document.body;
        root.querySelectorAll('[data-crawl-cell]').forEach(
            (e) => e.removeAttribute('data-crawl-cell'));

        const shape = [...root.querySelectorAll('table')].filter(vis)
            .map((t) => {
                const rows = [...t.querySelectorAll('tr')].filter(vis);
                return {t: t, rows: rows,
                        valueRows: rows.filter(
                            (r) => r.querySelectorAll(INP).length)};
            });
        const labels = shape.filter(
            (s) => s.valueRows.length === 0 && s.rows.length > 0);
        if (!labels.length)
            return {ok: false, why: 'this screen has no table of row labels'};
        // The widest label table, in case the panel carries a stray one.
        const L = labels.reduce((a, b) => b.rows.length > a.rows.length ? b : a);
        const names = L.rows.map(
            (r) => (r.innerText || '').replace(/\s+/g, ' ').trim());

        const blocks = shape.filter((s) => s.valueRows.length === L.rows.length);
        if (!blocks.length)
            return {ok: false, why: 'no table of inputs has as many rows ('
                    + L.rows.length + ') as the table of row names'};

        const described = blocks.map((B, bi) => {
            const r = B.t.getBoundingClientRect();
            return {
                index: bi, x: Math.round(r.x),
                wide: B.valueRows.length
                    ? B.valueRows[0].querySelectorAll(INP).length : 0,
                cells: B.valueRows.map((row) =>
                    [...row.querySelectorAll(INP)].map((i) => ({
                        value: (i.value || '').trim(),
                        placeholder: i.getAttribute('placeholder') || '',
                        readonly: i.disabled === true || i.readOnly === true}))),
            };
        });
        const info = {ok: true, names: names, blocks: described,
                      count: blocks.length};
        if (!want) return info;

        if (block < 0 || block >= blocks.length)
            return Object.assign(info, {ok: false, why: 'there is no period '
                + (block + 1) + '; the screen shows ' + blocks.length});
        const hits = [];
        names.forEach((n, i) => { if (norm(n) === norm(want)) hits.push(i); });
        if (!hits.length)
            return Object.assign(info, {ok: false,
                why: 'no row is named ' + JSON.stringify(want)});
        if (hits.length > 1)
            return Object.assign(info, {ok: false, why: hits.length
                + ' rows are named ' + JSON.stringify(want)});

        const ri = hits[0];
        const inputs = [...blocks[block].valueRows[ri]
                        .querySelectorAll(INP)];
        if (column < 0 || column >= inputs.length)
            return Object.assign(info, {ok: false, why: 'row '
                + JSON.stringify(want) + ' has ' + inputs.length
                + ' column(s) in this period, so there is no column '
                + (column + 1)});
        const target = inputs[column];
        if (doStamp) target.setAttribute('data-crawl-cell', '1');
        return Object.assign(info, {ok: true, row: ri, rowName: names[ri],
            value: (target.value || '').trim(),
            readonly: target.disabled === true || target.readOnly === true});
    }"""

    def period_blocks(self) -> dict:
        """
        The row names, and one entry per period block on the screen.

        Read-only. A caller uses it to learn how many periods the screen is
        showing and which of them is the one just added.
        """
        try:
            got = self.page.evaluate(self._BLOCK_JS, ["", 0, 0, False])
        except PWError as exc:
            return {"ok": False, "why": f"the grid could not be read: "
                                        f"{str(exc)[:120]}"}
        return got or {"ok": False, "why": "the grid could not be read"}

    def block_cell(self, label: str, column: int = 0, block: int = 0) -> dict:
        try:
            got = self.page.evaluate(self._BLOCK_JS,
                                     [label, column, block, False])
        except PWError as exc:
            return {"ok": False, "why": str(exc)[:120]}
        return got or {"ok": False, "why": "the grid could not be read"}

    def block_cell_value(self, label: str, column: int = 0,
                         block: int = 0) -> str:
        got = self.block_cell(label, column, block)
        return str(got.get("value") or "") if got.get("ok") else ""

    def set_block_cell(self, label: str, value: str, column: int = 0,
                       block: int = 0, column_name: str = "") -> Entry:
        """
        Type a figure into one cell of one PERIOD of the grid.

        The same contract as grid_cell, with the period made explicit: a
        read-only cell raises rather than being skipped quietly, because
        read-only here means the application computes the figure.
        """
        assert_writable(self.page.url)
        got = self.page.evaluate(self._BLOCK_JS, [label, column, block, True])
        if not got or not got.get("ok"):
            why = (got or {}).get("why", "the grid could not be read")
            raise FillError(f"No cell for {label!r} in period {block + 1}, "
                            f"column {column + 1}: {why}.")
        if got.get("readonly"):
            raise FillError(
                f"{label!r} column {column + 1} is read-only — the "
                f"application computes it. It shows {got.get('value')!r}.")

        ctl = self.page.locator('[data-crawl-cell="1"]').first
        if not ctl.count():
            raise FillError(f"{label!r} could not be located after being "
                            f"found in the grid.")
        try:
            ctl.click(timeout=6000)
            ctl.fill("")
            ctl.fill(str(value))
            ctl.press("Tab")       # the app recomputes its totals on blur
        except PWError as exc:
            raise FillError(f"{label!r} would not accept {value!r}: "
                            f"{str(exc)[:120]}")
        name = f"{label} — {column_name}" if column_name else \
               f"{label} — column {column + 1}"
        return self._record(name, value, "number")

    # ---- long grids whose rows are named in their own first cell ---------
    #
    # Financials Input is 450-odd rows in ONE table: a first cell carrying a
    # numbered name ('2.Cash & Marketable Securities'), then one input per
    # statement column. Neither paired_grid nor period_blocks fits — the names
    # are not a separate table, and the columns are not separate tables — so
    # this addresses a cell as (row name, input index within that row).
    #
    # Rows with no input at all are section headings ('BALANCE SHEET') and are
    # reported as such rather than skipped silently.
    _ROWS_JS = r"""([wanted, index, doStamp]) => {
        const norm = (s) => (s || '').toLowerCase().replace(/&/g, ' and ')
            .replace(/[^a-z0-9]+/g, ' ').trim();
        const txt = (e) => (e ? (e.textContent || '') : '').trim()
            .replace(/\s+/g, ' ');
        const vis = (e) => e.offsetParent !== null
                        || e.getClientRects().length > 0;
        const root = document.querySelector('[data-crawl-root]')
                  || document.body;
        root.querySelectorAll('[data-crawl-cell]').forEach(
            (e) => e.removeAttribute('data-crawl-cell'));

        const INP = 'input:not([type=hidden]):not([type=checkbox]), textarea';
        const rows = [];
        for (const tr of root.querySelectorAll('table tr')) {
            if (!vis(tr)) continue;
            const tds = [...tr.querySelectorAll('td')];
            if (!tds.length) continue;
            const name = txt(tds[0]);
            if (!name) continue;
            const inputs = [...tr.querySelectorAll(INP)].filter(vis);
            rows.push({
                name: name,
                heading: inputs.length === 0,
                cells: inputs.map((i) => ({
                    value: (i.value || '').trim(),
                    readonly: i.disabled === true || i.readOnly === true
                              || i.hasAttribute('disabled')
                              || i.hasAttribute('readonly'),
                })),
                _el: null,
            });
            if (doStamp && wanted && norm(name) === norm(wanted)) {
                let k = index;
                if (k < 0) {
                    // -1 means "the last ENABLED one", which on this screen is
                    // the statement just created: the columns already saved
                    // come back read-only.
                    for (let j = inputs.length - 1; j >= 0; j--) {
                        const i = inputs[j];
                        const ro = i.disabled === true || i.readOnly === true
                            || i.hasAttribute('disabled')
                            || i.hasAttribute('readonly');
                        if (!ro) { k = j; break; }
                    }
                }
                if (k >= 0 && k < inputs.length) {
                    inputs[k].setAttribute('data-crawl-cell', '1');
                    rows[rows.length - 1].stamped = k;
                }
            }
        }
        return {ok: true, rows: rows.map((r) => ({
            name: r.name, heading: r.heading, cells: r.cells,
            stamped: r.stamped}))};
    }"""

    def labelled_rows(self) -> list[dict]:
        """
        Every named row of the long grid, with each of its cells' state.

        Read-only. A caller uses it to learn what the screen is showing rather
        than working from a list of row names that would go stale the first
        time the chart of accounts changed.
        """
        try:
            got = self.page.evaluate(self._ROWS_JS, ["", -1, False])
        except PWError:
            return []
        return (got or {}).get("rows") or []

    def row_cell_values(self, row_name: str) -> list[str]:
        """What one named row is showing, across all its columns."""
        for r in self.labelled_rows():
            if _norm_label(r.get("name")) == _norm_label(row_name):
                return [c.get("value", "") for c in (r.get("cells") or [])]
        return []

    def set_row_cell(self, row_name: str, value: str,
                     index: int = -1) -> Entry:
        """
        Type into one cell of a named row. `index=-1` takes the last ENABLED
        cell, which is the column this run just created — the ones already
        saved come back read-only.
        """
        assert_writable(self.page.url)
        try:
            got = self.page.evaluate(self._ROWS_JS, [row_name, index, True])
        except PWError as exc:
            raise FillError(f"The grid could not be read: {str(exc)[:120]}")
        hit = next((r for r in (got or {}).get("rows", [])
                    if r.get("stamped") is not None), None)
        if hit is None:
            raise FillError(f"No writable cell in a row named {row_name!r}.")

        ctl = self.page.locator('[data-crawl-cell="1"]').first
        if not ctl.count():
            raise FillError(f"{row_name!r} could not be located after being "
                            f"found in the grid.")
        try:
            ctl.scroll_into_view_if_needed(timeout=4000)
            ctl.click(timeout=6000)
            ctl.fill("")
            ctl.fill(str(value))
            ctl.press("Tab")       # the app recomputes its totals on blur
        except PWError as exc:
            raise FillError(f"{row_name!r} would not accept {value!r}: "
                            f"{str(exc)[:120]}")
        return self._record(row_name, value, "number")

    def commit_block(self, block: int = 0, label: str = "Save") -> str:
        """
        Press the Save that belongs to ONE period block.

        Each period on Business Performance carries its own Save, side by side
        under its own columns. commit() takes the first enabled match in
        document order, which is the LEFTMOST — the period that was already
        there — so saving a period this run added needs the right one by
        position, not the first one that matches the word.

        Saves are ordered by their x position and matched to blocks
        left-to-right, which is the order period_blocks reports them in.
        """
        assert_writable(self.page.url)
        if not _committable(label):
            raise WriteRefused(
                f"{label!r} is not a permitted commit control.")

        try:
            found = self.page.evaluate(
                """(want) => {
                    const txt = e => (e.value || e.textContent || '').trim();
                    const vis = e => e.offsetParent !== null
                                  || e.getClientRects().length;
                    const norm = s => s.toLowerCase().replace(/\\s+/g, ' ').trim();
                    const all = [...document.querySelectorAll(
                        'button, a.btn, input[type=button], input[type=submit]')]
                        .filter(vis)
                        .filter(b => norm(txt(b)) === norm(want)
                                     && !b.disabled);
                    all.sort((a, b) => a.getBoundingClientRect().x
                                     - b.getBoundingClientRect().x);
                    all.forEach((b, i) => b.setAttribute('data-crawl-save',
                                                         String(i)));
                    return all.map((b, i) => ({i,
                        x: Math.round(b.getBoundingClientRect().x)}));
                }""", label) or []
        except PWError as exc:
            raise FillError(f"The {label!r} buttons could not be read: "
                            f"{str(exc)[:120]}")

        if not found:
            raise FillError(f"No enabled {label!r} button is on this screen.")
        which = block if 0 <= block < len(found) else len(found) - 1
        btn = self.page.locator(f'[data-crawl-save="{which}"]').first
        if not btn.count():
            raise FillError(f"The {label!r} for period {block + 1} could not "
                            f"be located after being found.")

        self.session.recorder.clear()
        try:
            btn.scroll_into_view_if_needed(timeout=4000)
        except PWError:
            pass
        try:
            btn.click(timeout=10000)
        except PWError as exc:
            raise FillError(f"{label!r} would not click: {str(exc)[:140]}")
        self._settle(25000)
        self.page.wait_for_timeout(800)
        return (f"clicked the {label} for period {which + 1} of "
                f"{len(found)}")

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

    def radio(self, wanted: str) -> Entry:
        """
        Select a radio button by the text beside it.

        Matched on a FRAGMENT, case-insensitively, because these labels are
        not always spelled the way a specification spells them — the Add Peer
        dialog offers 'Exisitng Customer', and quoting the app's typo in a
        flow file is a trap for whoever tidies it next.

        Clicked rather than asserted. The obvious reading of 'Existing
        Customer is selected by default' is that it needs no click; on this
        dialog NEITHER radio starts checked, and the field the choice reveals
        never appears until one is.
        """
        assert_writable(self.page.url)
        scope = self._scope()
        got = self.page.evaluate(
            """([scope, want]) => {
                const root = document.querySelector(scope) || document.body;
                const norm = s => (s || '').toLowerCase()
                    .replace(/\\s+/g, ' ').trim();
                const w = norm(want);
                const rs = [...root.querySelectorAll('input[type=radio]')];
                const lab = r => norm(((r.closest('label')
                    || (r.id && root.querySelector('label[for="' + r.id + '"]'))
                    || r.parentElement || {}).textContent) || '');
                const hit = rs.find(r => lab(r).includes(w));
                if (!hit) return {ok: false,
                                  offered: rs.map(lab).slice(0, 6)};
                hit.click();
                hit.dispatchEvent(new Event('change', {bubbles: true}));
                return {ok: true, label: lab(hit), checked: hit.checked};
            }""", [scope, wanted])
        if not got or not got.get("ok"):
            raise FillError(
                f"No radio button reads {wanted!r}. Offered: "
                + (", ".join(repr(x) for x in (got or {}).get("offered", []))
                   or "none"))
        self.page.wait_for_timeout(600)
        self._settle(15000)
        if not got.get("checked"):
            raise FillError(f"{wanted!r} was clicked but did not become "
                            f"selected.")
        return self._record(got.get("label") or wanted, "selected", "radio")

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
    def _close_dropdown(self, box) -> None:
        """
        Close an ng-select's option panel, and nothing else.

        The same trap as the calendar's, in the same place: Escape closes the
        panel, and inside a modal it closes the DIALOG too. A dropdown that
        offers nothing is a field-level problem — it must not take the form
        that field is on down with it, or one empty reference list reads as
        every later field having vanished.
        """
        if self._scope() != "body":
            try:
                box.click(timeout=3000)      # the trigger toggles it shut
                self.page.wait_for_timeout(200)
                return
            except PWError:
                pass
        try:
            self.page.keyboard.press("Escape")
        except PWError:
            pass

    def options(self, label: str) -> list[str]:
        """
        What a dropdown offers. Reads the panel open and closes it again, so it
        is safe to call before deciding a value.
        """
        block = self._block(label)
        box = block.locator("ng-select").first
        if not box.count():
            raise FillError(f"{label!r} is not a dropdown.")
        box.click(timeout=6000)          # a real click; JS click will not open it
        self.page.wait_for_timeout(700)
        opts = self.page.evaluate(
            """() => [...document.querySelectorAll('.ng-dropdown-panel .ng-option')]
                    .map(o => (o.textContent || '').trim()).filter(Boolean)""")
        self._close_dropdown(box)
        self.page.wait_for_timeout(200)
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
        box.click(timeout=6000)
        self.page.wait_for_timeout(700)

        panel = self.page.locator(".ng-dropdown-panel .ng-option")
        n = panel.count()
        if not n:
            self._close_dropdown(box)
            raise FillError(f"{label!r} opened no options to choose from.")

        chosen = None
        if value is None:
            for i in range(n):
                txt = (panel.nth(i).inner_text() or "").strip()
                # Skip the placeholder / "no items" rows.
                if txt and not re.match(r"^-?\s*select\s*-?$|^no items", txt, re.I):
                    panel.nth(i).click(timeout=6000)
                    chosen = txt
                    break
        else:
            # Runs of whitespace are collapsed on BOTH sides before matching.
            # The bank list on Relationship with Other Banks offers 'Islamic
            # Bank' with two spaces in it, so asking for the name as anyone
            # would write it found nothing at all — and the alternative was
            # copying the app's own typo into a spec file, where the next
            # reader would quite reasonably tidy it away again.
            #
            # Strictly more forgiving than before: an option that matched
            # under the old comparison still matches, and still matches first.
            def _flat(t: str) -> str:
                return re.sub(r"\s+", " ", (t or "").strip().lower())

            want = _flat(value)
            texts = [(panel.nth(i).inner_text() or "").strip() for i in range(n)]
            flat = [_flat(t) for t in texts]
            idx = next((i for i, t in enumerate(flat) if t == want), None)
            if idx is None:
                idx = next((i for i, t in enumerate(flat) if want in t), None)
            if idx is None:
                self._close_dropdown(box)
                raise FillError(
                    f"{label!r} has no option matching {value!r}. "
                    f"Offered: {', '.join(texts[:12])}")
            panel.nth(idx).click(timeout=6000)
            chosen = texts[idx]

        if chosen is None:
            self._close_dropdown(box)
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
        box.click(timeout=6000)
        self.page.wait_for_timeout(800)

        panel = self.page.locator(".ng-dropdown-panel .ng-option")
        n = panel.count()
        if not n:
            raise FillError("The dialog's dropdown opened no options.")
        texts = [(panel.nth(i).inner_text() or "").strip() for i in range(n)]

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
            self.page.keyboard.press("Escape")
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
                       prefer: Optional[list] = None) -> Entry:
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
        """
        assert_writable()
        scope = self._scope()

        toggle = None
        for sel in self._TREE_TOGGLES:
            loc = self.page.locator(f"{scope} {sel}")
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
                "matching a 'Select option' toggle is visible.")

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

    # A dialog's own dismissal controls. Deliberately a closed list rather
    # than "the button that is not Save": these are clicked without the write
    # gate, so nothing that could commit may ever be reachable through here.
    _DISMISS = ("Cancel", "Close", "Back", "No")

    def cancel_modal(self, label: str = "") -> str:
        """
        Dismiss the open dialog with its OWN control, discarding what it holds.

        Escape is what _close_modal reaches for first, and Escape is not
        always only a dialog's problem — on Litigation it closed the whole
        form being filled. A dialog that offers Cancel or Close should
        therefore be dismissed by pressing it.

        Not gated by the write allowlist, because it commits nothing: every
        label it will press is on the closed list above, and a label that is
        committable at all is refused outright rather than trusted. Returns
        what it pressed, or "" if no dialog was open.

        Kept public and unused by the happy path on purpose — a cancel-flow
        test needs it, and a helper written when the flow is added is a helper
        written against a screen nobody is looking at.
        """
        wants = [label] if label else list(self._DISMISS)
        for want in wants:
            if _committable(want):
                raise WriteRefused(
                    f"{want!r} can commit, so it is not a way to cancel.")
        try:
            if not self.page.locator(".modal.show, .modal.in").count():
                return ""
        except PWError:
            return ""

        for want in wants:
            for sel in (f'.modal.show button:text-is("{want}")',
                        f'.modal.show .btn:text-is("{want}")',
                        f'.modal.in button:text-is("{want}")'):
                try:
                    loc = self.page.locator(sel).first
                    if loc.count() and loc.is_visible(timeout=500):
                        loc.click(timeout=4000)
                        self._settle(6000)
                        self.page.wait_for_timeout(500)
                        return want
                except PWError:
                    continue

        # No named control: fall back to the generic dismissal, which is what
        # every other screen here has always used.
        self._close_modal()
        return "the dialog's close control"

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
    def upload(self, path: str, label: str = "") -> Entry:
        """
        Attach a file. Uses Playwright's file chooser rather than clicking a
        native dialog, so nothing can block an unattended run.
        """
        assert_writable()
        if not os.path.exists(path):
            raise FillError(f"There is no file at {path!r} to attach.")
        inputs = self.page.locator('input[type=file]')
        if not inputs.count():
            raise FillError("This screen has no file input.")
        inputs.first.set_input_files(path)
        self.page.wait_for_timeout(600)
        return self._record(label or "Attachment", os.path.basename(path),
                            "file")

    def upload_in_scope(self, path: str, label: str = "") -> Entry:
        """
        Attach a file to the file input in the SCOPE being filled.

        The same job as upload(), scoped the way everything else here is: an
        open dialog wins over the page behind it. upload() takes the first
        file input in the whole DOCUMENT, which is right on a page that has
        one and a coin toss for a dialog sitting over a screen with its own —
        the eCIB list is exactly that, so its Upload dialog needs this.

        The input is normally hidden behind a paperclip button, and that is
        the case set_input_files exists for: clicking the paperclip opens a
        native file picker, and an unattended run cannot answer one.

        Settles afterwards, unlike upload(): attaching a file here starts a
        server-side parse, and the fields that follow cannot be read until it
        has come back.
        """
        assert_writable()
        if not os.path.exists(path):
            raise FillError(f"There is no file at {path!r} to attach.")

        scope = self._scope()
        box = self.page.locator(f"{scope} input[type=file]")
        try:
            scoped = box.count()
        except PWError:
            scoped = 0
        if not scoped:
            # A dialog that renders its input outside itself is not worth
            # failing over. Falling back to the page is what upload() would
            # have done anyway.
            box = self.page.locator("input[type=file]")
            try:
                if not box.count():
                    raise FillError(
                        "There is no file input in this dialog or on the page "
                        "behind it.")
            except PWError as exc:
                raise FillError(f"The file input could not be read: {exc}")

        box.first.set_input_files(path)
        self.page.wait_for_timeout(800)
        self._settle(30000)
        return self._record(label or "Attachment", os.path.basename(path),
                            "file")

    # ---- committing -----------------------------------------------------
    def commit(self, label: str = "Save", expect_settle: bool = True) -> str:
        """
        Click a named commit button. This is the one place data is written.

        Refuses anything outside COMMITTABLE, and anything in NEVER, even when a
        flow asks for it — a mistake in a flow must not be able to approve a
        case or delete a record.
        """
        # The live page, for the same reason as press_action: Save on the PR
        # checklist commits to a host the base URL knows nothing about.
        assert_writable(self.page.url)
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

    # ---- non-committing action buttons ----------------------------------
    def press_action(self, label: str, settle_ms: int = 30000) -> str:
        """
        Click a button that ACTS without saving — 'Perform PR', 'Generate',
        'Generate Pdf', 'Edit'.

        Separate from commit() because those labels are deliberately not in
        COMMITTABLE and must stay out of it: COMMITTABLE is the list a flow
        may SAVE with, and widening it to fit a button that merely opens a
        form would weaken the guarantee for every screen in the suite.

        The NEVER list still applies, and that is the point of routing this
        through widgets at all rather than clicking from a flow. Approve,
        Reject, Delete, Forward and the rest end a case or a session, and no
        flow may reach them by any door.

        THE DISPATCH FALLBACK, and why it is not cheating: this application
        keeps an ngx-ui-loader overlay in the DOM at opacity 0 with
        pointer-events:auto and z-index 99998. It is invisible to a person and
        a person's click lands, because by the time they aim at anything the
        overlay has usually gone; an automated click arrives sooner and is
        silently swallowed — Playwright reports a successful click, the
        handler never runs, and nothing at all happens. Measured on the PR
        Checklist screen: a real .click() produced zero network requests,
        while dispatching the same event on the same element produced two. So
        the real click is tried FIRST and honestly — the fallback only runs
        when the click provably did nothing.
        """
        # NEVER is checked FIRST, before the host gate and before anything
        # touches the page. A forbidden label is refused on its name alone —
        # it needs no browser, no session and no live URL to be wrong — and
        # ordering it this way means the refusal cannot be skipped by a
        # failure to read the page.
        low = (label or "").strip().lower()
        for pat in NEVER:
            if re.search(pat, low):
                raise WriteRefused(
                    f"{label!r} is never pressed by this suite: it matches "
                    f"{pat!r}. These end a case or a session rather than "
                    f"acting on a record.")

        # The LIVE page, not the configured base: press_action is the method
        # that drives the PR form, and that form is served by a different host.
        assert_writable(self.page.url)

        scope = self._scope()
        btn = None
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
                    if cand.is_visible(timeout=400) and not cand.is_disabled(
                            timeout=400):
                        btn = cand
                        break
                except PWError:
                    continue
            if btn is not None:
                break
        if btn is None:
            raise FillError(
                f"No enabled {label!r} button is visible on this screen.")

        # Wait the overlay out first — that alone fixes most of it.
        try:
            self.page.wait_for_function(_NO_BLOCKING_OVERLAY, timeout=20000)
        except PWError:
            pass

        # Instrument the button so "did the click land?" is MEASURED on the
        # element, not guessed from the page.
        #
        # An earlier version inferred it from a page fingerprint, and that was
        # wrong in the dangerous direction: a click that landed but changed
        # nothing visible within the settle window looked identical to a click
        # that was swallowed, so the fallback fired and the button was pressed
        # TWICE. On a control that toggles, two presses are the same as none.
        # A capture-phase listener on the element itself cannot be fooled by
        # either.
        try:
            btn.evaluate("""el => {
                el.__pressCount = 0;
                if (!el.__pressHooked) {
                    el.__pressHooked = true;
                    el.addEventListener(
                        'click', () => { el.__pressCount++; }, true);
                }
            }""")
            instrumented = True
        except PWError:
            instrumented = False

        self.session.recorder.clear()
        try:
            btn.scroll_into_view_if_needed(timeout=4000)
        except PWError:
            pass
        try:
            btn.click(timeout=10000)
        except PWError as exc:
            raise FillError(f"{label!r} would not click: {str(exc)[:140]}")
        self._settle(settle_ms)

        if not instrumented:
            return f"clicked {label}"
        try:
            landed = int(btn.evaluate("el => el.__pressCount || 0"))
        except PWError:
            # Cannot tell. Assume it landed: a missed form is recoverable, a
            # double press may not be.
            landed = 1
        if landed:
            return f"clicked {label}"

        # The click provably never reached the element — the loader overlay
        # ate it. Dispatching is then the FIRST press this button has had, not
        # a second one.
        try:
            fired = self.page.evaluate(
                """(want) => {
                    const norm = s => (s || '').trim().toLowerCase();
                    const b = [...document.querySelectorAll('button, a.btn')]
                        .find(x => norm(x.textContent).includes(norm(want)));
                    if (!b) return false;
                    b.dispatchEvent(new MouseEvent('click',
                        {bubbles: true, cancelable: true}));
                    return true;
                }""", label)
        except PWError:
            fired = False
        if fired:
            self._settle(settle_ms)
            return (f"clicked {label} (the real click never reached the "
                    f"button — the loader overlay ate it — so the event was "
                    f"dispatched instead; the button was pressed once)")
        return f"clicked {label}"

    # ---- PR checklist factor tables -------------------------------------
    #
    # Not the paired grid above. These are ordinary tables whose columns are
    # 'PR Factor' (a read-only regulatory label), 'Factor Value' (the only
    # writable cell), and then 'Regulatory Requirement', 'Actual Value' and
    # 'Compliant Status', which the application fills in for itself when
    # Generate is pressed.
    #
    # Found by their HEADERS rather than by position or by section name. The
    # screen repeats this table under every section heading and the sections
    # are configuration, so a flow that hard-coded 'the third table' or a list
    # of section names would break the first time one was added — and the
    # brief asks for every section to be walked, not a named few.
    _FACTOR_JS = r"""([wanted, stamp]) => {
        const norm = s => (s || '').toLowerCase().replace(/&/g, ' and ')
            .replace(/[^a-z0-9]+/g, ' ').trim();
        const txt = e => (e ? (e.textContent || '') : '').trim()
            .replace(/\s+/g, ' ');
        const vis = e => e && (e.offsetParent !== null || e.getClientRects().length);
        const root = document.querySelector('[data-crawl-root]') || document.body;
        document.querySelectorAll('[data-crawl-cell]').forEach(
            e => e.removeAttribute('data-crawl-cell'));

        const rows = [];
        for (const t of root.querySelectorAll('table')) {
            if (!vis(t)) continue;
            const heads = [...t.querySelectorAll('th')].map(h => norm(txt(h)));
            const fi = heads.findIndex(h => h.includes('pr factor'));
            const vi = heads.findIndex(h => h.includes('factor value'));
            if (fi < 0 || vi < 0) continue;          // not a factor table

            // The section name. Headings are tried first, but on this screen
            // they are NOT headings — 'SECURITY REQUIREMENT' and the rest are
            // plain text sitting above each table, so an ancestor search for
            // h1-h6 finds nothing and every factor would be reported under
            // '(no section)'. So: fall back to walking backwards from the
            // table for the nearest short standalone line of text that is not
            // itself part of a table.
            let section = '';
            let n = t.parentElement;
            for (let k = 0; k < 6 && n && !section; k++, n = n.parentElement) {
                const h = n.querySelector(
                    'h1,h2,h3,h4,h5,h6,.card-title,.panel-title,legend');
                if (h) section = txt(h);
            }
            if (!section) {
                let node = t;
                outer:
                for (let up = 0; up < 4 && node; up++, node = node.parentElement) {
                    let prev = node.previousElementSibling;
                    for (let back = 0; back < 6 && prev;
                         back++, prev = prev.previousElementSibling) {
                        if (prev.querySelector && prev.querySelector('table')) continue;
                        const t2 = txt(prev);
                        if (t2 && t2.length <= 80 && !/^\s*$/.test(t2)) {
                            section = t2;
                            break outer;
                        }
                    }
                }
            }

            // EVERY column is examined, not just 'Factor Value'.
            //
            // That was the first version and it was wrong: on the live form
            // two of the nine settable factors — both 'Linkage between
            // borrower`s equity ...' rows — carry their writable input in the
            // ACTUAL VALUE column instead, while other rows have a DISABLED
            // input in that same column. So "which column is writable" is a
            // property of the row, not of the table, and the only way to get
            // it right is to look.
            //
            // The rule the screen actually follows, and the one the brief
            // gives: fill whatever is enabled, leave whatever is grey.
            for (const tr of t.querySelectorAll('tr')) {
                if (!vis(tr)) continue;
                const tds = [...tr.querySelectorAll('td')];
                if (tds.length <= fi) continue;
                const factor = txt(tds[fi]);
                if (!factor) continue;

                // Cells are keyed by the header at the same index, which is
                // only meaningful while the two line up. On the read-only
                // view some rows carry a different number of cells from the
                // header row, and mapping them anyway shifted every value one
                // column along — which surfaced as 'Yes' appearing under
                // Regulatory Requirement on one page and Compliant Status on
                // the other, reported as three failures that were really one
                // parsing bug. The flag is carried so a comparison can refuse
                // to compare rather than invent a mismatch.
                const rec = {section, factor, columns: {}, controls: [],
                             aligned: tds.length === heads.length,
                             cells: tds.map(td => txt(td).slice(0, 60))};
                let writable = null;

                tds.forEach((td, i) => {
                    const name = heads[i] || ('column ' + (i + 1));
                    const ctl = td.querySelector('select, ng-select, input, textarea');
                    if (!ctl) {
                        rec.columns[name] = txt(td);
                        return;
                    }
                    const tag = ctl.tagName.toLowerCase();
                    const off = ctl.disabled === true || ctl.readOnly === true
                        || ctl.hasAttribute('disabled')
                        || ctl.hasAttribute('readonly')
                        || ctl.getAttribute('aria-disabled') === 'true'
                        || /disabled/.test(String(ctl.className || ''));
                    const info = {col: i, colName: name, kind: tag,
                                  disabled: off};
                    if (tag === 'select') {
                        info.options = [...ctl.querySelectorAll('option')]
                            .map(o => txt(o)).filter(Boolean);
                        info.value = txt(ctl.querySelector('option:checked'));
                    } else if (tag === 'ng-select') {
                        info.value = txt(ctl.querySelector('.ng-value'));
                    } else {
                        info.value = ctl.value || '';
                        info.type = ctl.getAttribute('type') || 'text';
                    }
                    rec.controls.push(info);
                    rec.columns[name] = info.value;
                    if (!off && !writable) {
                        writable = info;
                        if (stamp && wanted && norm(factor) === norm(wanted)) {
                            ctl.setAttribute('data-crawl-cell', '1');
                            rec.stamped = true;
                        }
                    }
                });

                // The flat fields the rest of the suite reads.
                rec.disabled = !writable;
                rec.kind = writable ? writable.kind
                    : (rec.controls[0] || {}).kind || 'text';
                rec.value = writable ? writable.value
                    : (rec.controls[0] || {}).value || '';
                rec.options = writable ? (writable.options || []) : [];
                rec.writableColumn = writable ? writable.colName : '';
                rows.push(rec);
            }
        }
        return rows;
    }"""

    def factor_rows(self) -> list[dict]:
        """
        Every PR factor on the screen, with its control's kind and state.

        Read-only, and the basis of the whole flow: the sections are
        configuration, so what is on the page is discovered rather than
        declared.
        """
        try:
            return self.page.evaluate(self._FACTOR_JS, ["", False]) or []
        except PWError:
            return []

    def set_factor(self, factor: str, value: Optional[str] = None) -> Entry:
        """
        Set one factor's writable cell, found by the factor's own text.

        Whichever column that cell is in — 'Factor Value' for most rows,
        'Actual Value' for the two Linkage ones. `value=None` takes the first
        real option of a dropdown, which is what the brief asks for on factors
        it does not name; a text input with no value given is refused rather
        than guessed at, because a number typed into a compliance calculation
        is not a neutral default.

        A row with nothing enabled raises FrozenField — not a failure, and the
        caller records it as skipped.
        """
        assert_writable(self.page.url)
        try:
            rows = self.page.evaluate(self._FACTOR_JS, [factor, True]) or []
        except PWError as exc:
            raise FillError(f"The factor tables could not be read: "
                            f"{str(exc)[:120]}")
        match = next((r for r in rows if r.get("stamped")), None)
        if match is None:
            known = [r["factor"][:40] for r in rows][:6]
            raise FillError(
                f"No writable factor matching {factor!r}. "
                + (f"On screen: {'; '.join(known)}" if known
                   else "No factor tables are on this screen at all."))
        if match.get("disabled"):
            raise FrozenField(
                f"{factor!r} is locked by the application — every control on "
                f"that row is disabled, and it shows "
                f"{match.get('value') or '(empty)'!r}.")

        ctl = self.page.locator('[data-crawl-cell="1"]').first
        if not ctl.count():
            raise FillError(f"{factor!r} could not be located after being "
                            f"found in the table.")

        kind = match.get("kind")
        column = match.get("writableColumn") or "its writable column"
        if kind == "select":
            real = [o for o in (match.get("options") or [])
                    if o and not re.match(r"^-?\s*select\s*-?$|^--", o, re.I)]
            if not real:
                raise FillError(f"{factor!r} offers no selectable options.")
            want = value if value in real else real[0]
            try:
                ctl.select_option(label=want)
            except PWError as exc:
                raise FillError(f"{factor!r} would not take {want!r}: "
                                f"{str(exc)[:120]}")
            kind_out = "dropdown"
        elif kind == "ng-select":
            want = self._choose_stamped(ctl, factor, value)
            kind_out = "dropdown"
        elif kind in ("input", "textarea"):
            if value is None:
                raise FillError(
                    f"{factor!r} is a free-text {match.get('writableColumn')} "
                    f"box, not a dropdown, and no value was given for it. "
                    f"This one is not guessed at: the figure feeds the "
                    f"application's own compliance arithmetic, so a made-up "
                    f"default would produce a Compliant Status that means "
                    f"nothing. Pin it in PR_FACTOR_VALUES.")
            try:
                ctl.click(timeout=6000)
                ctl.fill("")
                ctl.fill(str(value))
                ctl.press("Tab")        # ASP.NET recalculates on blur
            except PWError as exc:
                raise FillError(f"{factor!r} would not take {value!r}: "
                                f"{str(exc)[:120]}")
            want = str(value)
            kind_out = "text"
        else:
            raise FillError(
                f"{factor!r} has a {kind!r} control in {column}, which this "
                f"flow does not know how to set.")

        self.page.wait_for_timeout(400)
        return self._record(factor, want, kind_out)

    def _choose_stamped(self, box, factor: str,
                        value: Optional[str]) -> str:
        """Open the stamped ng-select and pick from its panel."""
        try:
            box.click(timeout=6000)      # must be real; JS click will not open it
        except PWError as exc:
            raise FillError(f"{factor!r} would not open: {str(exc)[:120]}")
        self.page.wait_for_timeout(700)
        panel = self.page.locator(".ng-dropdown-panel .ng-option")
        n = panel.count()
        if not n:
            self._close_dropdown(box)
            raise FillError(f"{factor!r} opened no options to choose from.")

        texts = [(panel.nth(i).inner_text() or "").strip() for i in range(n)]

        def _flat(t):
            return re.sub(r"\s+", " ", (t or "").strip().lower())

        idx = None
        if value is not None:
            want = _flat(value)
            idx = next((i for i, t in enumerate(texts) if _flat(t) == want), None)
            if idx is None:
                idx = next((i for i, t in enumerate(texts)
                            if want in _flat(t)), None)
            if idx is None:
                self._close_dropdown(box)
                raise FillError(
                    f"{factor!r} has no option matching {value!r}. Offered: "
                    f"{', '.join(texts[:10])}")
        else:
            idx = next((i for i, t in enumerate(texts)
                        if t and not re.match(r"^-?\s*select\s*-?$|^no items",
                                              t, re.I)), None)
            if idx is None:
                self._close_dropdown(box)
                raise FillError(f"{factor!r} offered only placeholder options.")
        panel.nth(idx).click(timeout=6000)
        return texts[idx]

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
        # textarea it replaced is not kept in step with it.
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

