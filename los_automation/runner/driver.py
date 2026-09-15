"""
Browser session and path-based navigation.

Everything hard about driving this app is already solved in crawler.py and is
imported rather than rewritten: login that verifies it worked, in-app navigation
that survives Angular's router, wait_until_settled (networkidle is unreliable
here), routed-content detection via the innermost router-outlet, and discovery of
case menus / tabs / grid rows.

This module is READ-ONLY. The only text ever typed is the login form inside
crawler.login. Writing arrives in Phase 2 via widgets.py, gated on the host
allowlist in settings.write_allowed.
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright, Error as PWError

import config as crawler_config
import crawler as cr

from .. import settings
from . import results as R
from .targets import (ACTION, CONTEXT_MENU, MENU, ROW, ROW_BY_ID, TAB,
                      NavStep, SubScreen, Target)


def new_run_id(prefix: str = "run") -> str:
    return f"{prefix}-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"


# Live read of the visible form state. Also the mechanism Phase 2 needs for
# round-trip verification, which is why it returns values as well as labels.
_READ_FIELDS_JS = r"""(shellSel) => {
    let scope = null;
    for (const s of ['.modal.show', '.modal.in']) {
        const el = document.querySelector(s);
        if (el && (el.offsetParent !== null || el.getClientRects().length)) { scope = el; break; }
    }
    if (!scope) scope = document.querySelector('[data-crawl-root]') || document.body;

    const visible = (el) => el.offsetParent !== null || el.getClientRects().length > 0;
    const clean = (s) => (s || '').trim().replace(/\s+/g, ' ');

    // Angular sometimes names a control 'noName', and readonly LOV inputs carry
    // their own value in title/aria-label. Both produced labels like
    // "CIBG - Corporate & Investment Banking Group" for a field actually called
    // "Business Segment", so they are rejected outright.
    const JUNK = /^(noname|undefined|null|nan|\d+|-?\s*select\s*-?|choose a date|n\/?a)$/i;
    const usable = (t) => {
        t = clean(t);
        return t && t.length <= 90 && !JUNK.test(t);
    };
    const LABEL_SEL = 'label, .control-label, .field-label, .form-label';

    // Every label on the page, in document order, so a field can find the one
    // that immediately precedes it.
    const allLabels = [...document.querySelectorAll(LABEL_SEL)]
        .filter(l => visible(l) && usable(l.textContent));

    const labelFor = (el) => {
        const own = clean(el.value);

        // 1. A wrapping <label> is unambiguous.
        const wrap = el.closest('label');
        if (wrap && usable(wrap.textContent)) return clean(wrap.textContent);

        // 2. Climb to the SMALLEST ancestor that holds this control and a label
        //    but no sibling controls. This app wraps every field in its own
        //    <fieldset class="form-group"> carrying that field's <label>, so this
        //    is the structurally correct answer. Stopping as soon as a second
        //    control appears is essential: the enclosing div.row holds 49 inputs,
        //    and querying it would hand "Customer ID" to all of them.
        let n = el.parentElement, hops = 0;
        while (n && hops < 8) {
            if (n.querySelectorAll('input, select, textarea').length > 1) break;
            const l = n.querySelector(LABEL_SEL);
            if (l && usable(l.textContent)) return clean(l.textContent);
            n = n.parentElement;
            hops++;
        }

        // 3. label[for=id] — deliberately AFTER the structural climb, because ids
        //    are not unique here: several controls carry id="title", so a
        //    document-wide lookup returns whichever label happens to come first.
        //    That is what labelled Obligor Name as "Father/Guardian ID Expiry
        //    Date". Only trusted when the id occurs exactly once.
        if (el.id && document.querySelectorAll('[id="' + CSS.escape(el.id) + '"]').length === 1) {
            const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
            if (l && usable(l.textContent)) return clean(l.textContent);
        }

        // 4. Otherwise the nearest label that precedes this control on the page.
        let best = null;
        for (const l of allLabels) {
            const pos = l.compareDocumentPosition(el);
            if (pos & Node.DOCUMENT_POSITION_FOLLOWING) best = l;  // label before el
            else break;
        }
        if (best) return clean(best.textContent);

        // 5. Attributes last, and never the control's own value.
        for (const a of ['aria-label', 'placeholder', 'formcontrolname', 'name', 'title']) {
            const v = clean(el.getAttribute(a));
            if (usable(v) && v !== own) return v;
        }
        return '';
    };

    const out = [];
    for (const el of scope.querySelectorAll('input, select, textarea')) {
        if (el.closest(shellSel)) continue;
        const type = (el.getAttribute('type') || el.tagName).toLowerCase();
        if (type === 'hidden') continue;
        if (!visible(el)) continue;
        const label = labelFor(el);
        if (!label) continue;

        let value = '';
        if (type === 'checkbox' || type === 'radio') value = el.checked ? 'true' : 'false';
        else if (el.tagName.toLowerCase() === 'select') {
            const o = el.selectedOptions && el.selectedOptions[0];
            value = o ? (o.textContent || '').trim() : '';
        } else value = el.value || '';

        const item = {
            label: label.replace(/\s+/g, ' ').slice(0, 80),
            type: type,
            value: String(value).slice(0, 200),
            required: el.required === true || el.getAttribute('aria-required') === 'true',
            maxlength: el.getAttribute('maxlength') || '',
            disabled: el.disabled === true || el.readOnly === true,
        };
        if (el.tagName.toLowerCase() === 'select') {
            item.options = [...el.options].map(o => (o.textContent || '').trim()).slice(0, 40);
        }
        out.push(item);
    }

    // Rich-text editors are not form controls at all. The narrative screens —
    // Credit Memorandum, CRMD Note, Observations — are built from CKEditor-style
    // contenteditable regions under their own headings, so a reader that looks
    // only at input/select/textarea reported those screens as EMPTY: a screen
    // full of content failing the "shows its content" check.
    //
    // Their label is a heading rather than a <label>, hence the separate lookup.
    const RTE_SEL = '[contenteditable="true"], .ck-editor__editable, .ql-editor,'
                  + ' .note-editable, .cke_editable, .fr-element';
    const HEAD_SEL = 'label, .control-label, .card-title, .panel-title, legend,'
                   + ' h1, h2, h3, h4, h5, h6';
    const headingFor = (el) => {
        let n = el.parentElement;
        for (let i = 0; i < 6 && n; i++, n = n.parentElement) {
            const h = n.querySelector(HEAD_SEL);
            if (h && usable(h.textContent)) return clean(h.textContent);
        }
        return '';
    };
    for (const el of scope.querySelectorAll(RTE_SEL)) {
        if (el.closest(shellSel)) continue;
        if (!visible(el)) continue;
        // CKEditor nests one editable inside another; counting both would list
        // the same field twice.
        if (el.parentElement && el.parentElement.closest(RTE_SEL)) continue;
        const label = headingFor(el);
        if (!label) continue;
        out.push({
            label: label.replace(/\s+/g, ' ').slice(0, 80),
            type: 'rich-text',
            value: clean(el.textContent).slice(0, 200),
            required: el.getAttribute('aria-required') === 'true',
            maxlength: '',
            disabled: el.getAttribute('contenteditable') === 'false',
        });
    }

    // Custom widgets render their chosen value as text, not in an <input>.
    for (const el of scope.querySelectorAll('ng-select, [class*="ng-select"]')) {
        if (el.closest(shellSel)) continue;
        if (!visible(el)) continue;
        const label = el.getAttribute('formcontrolname') || el.getAttribute('name')
                   || el.getAttribute('placeholder') || '';
        if (!label) continue;
        const chosen = el.querySelector('.ng-value-label, .ng-value');
        out.push({
            label: String(label).replace(/\s+/g, ' ').slice(0, 80),
            type: 'ng-select',
            value: chosen ? (chosen.textContent || '').trim().slice(0, 200) : '',
            required: false, maxlength: '', disabled: false,
        });
    }
    return out;
}"""


# Two different things get styled red, and conflating them is misleading.
#
#   an ERROR   — a toast or banner: the screen or a request actually failed
#   a VALIDATION HINT — "Dealing Branch is Required" next to an empty field:
#                       the record is incomplete, which on a view screen is
#                       information about the DATA, not a malfunction
_ERROR_BANNER_JS = r"""() => {
    const out = [];
    const sels = ['.alert-danger', '.alert-error', '.toast-error', '.toast-danger',
                  '.swal2-container .swal2-title', '.swal2-html-container'];
    for (const s of sels) {
        for (const el of document.querySelectorAll(s)) {
            if (!(el.offsetParent !== null || el.getClientRects().length)) continue;
            const t = (el.textContent || '').trim().replace(/\s+/g, ' ');
            if (t.length > 3 && t.length < 300) out.push(t);
        }
    }
    return [...new Set(out)].slice(0, 10);
}"""


_VALIDATION_JS = r"""() => {
    const out = [];
    const sels = ['.invalid-feedback', '.text-danger', '.field-error', 'mat-error',
                  '.error-msg', '[class*="validation"]'];
    // Only messages that read like a validation rule — this also excludes the
    // red asterisk that marks a mandatory label.
    const looks = /required|invalid|must be|cannot|should be|not valid|mandatory/i;
    for (const s of sels) {
        for (const el of document.querySelectorAll(s)) {
            if (!(el.offsetParent !== null || el.getClientRects().length)) continue;
            const t = (el.textContent || '').trim().replace(/\s+/g, ' ');
            if (t.length > 3 && t.length < 200 && looks.test(t)) out.push(t);
        }
    }
    return [...new Set(out)].slice(0, 15);
}"""


class NavigationError(Exception):
    """Raised when a hop cannot be made. `blocked` separates an environment
    problem (no test data) from an app regression (menu entry gone)."""

    def __init__(self, message: str, blocked: bool = False):
        super().__init__(message)
        self.blocked = blocked


class Session:
    """A logged-in browser positioned somewhere in the app."""

    def __init__(self, run_id: str, mode: str = settings.VERIFY,
                 headless: Optional[bool] = None, emit=None):
        self.run_id = run_id
        self.mode = mode
        self.headless = settings.HEADLESS if headless is None else headless
        # Structured progress sink. The UI uses it to stream steps and
        # screenshots while a run is still going, instead of showing nothing
        # until the end.
        self.emit = emit or (lambda event: None)
        self.artifacts_dir = os.path.join(settings.ARTIFACTS_DIR, run_id)
        self.steps: list[R.StepLog] = []
        self._pw = None
        self._browser = None
        self._page = None
        self.recorder: Optional[cr.NetworkRecorder] = None
        self.console_errors: list[str] = []
        self.opened_record: str = ""
        # Captured ONCE from the landing page — see _step_context_menu.
        self.top_level_paths: set[str] = set()
        self.active_target: Optional[Target] = None

    # ---- lifecycle -----------------------------------------------------
    def __enter__(self) -> "Session":
        os.makedirs(self.artifacts_dir, exist_ok=True)
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=self.headless)
        ctx = self._browser.new_context(viewport={"width": 1600, "height": 1000})
        self._page = ctx.new_page()
        self._page.set_default_timeout(settings.STEP_TIMEOUT_MS)

        # Never let a native dialog or file picker stall an unattended run.
        self._page.on("dialog", lambda d: d.dismiss())
        self._page.on("filechooser", lambda fc: None)
        self._page.on("pageerror", lambda e: self.console_errors.append(str(e)[:300]))

        self.recorder = cr.NetworkRecorder()
        self._page.on("request", self.recorder.on_request)
        self._page.on("response", self.recorder.on_response)
        return self

    def __exit__(self, *exc):
        try:
            if self._browser:
                self._browser.close()
        finally:
            if self._pw:
                self._pw.stop()

    @property
    def page(self):
        if self._page is None:
            raise RuntimeError("Session used outside its context manager.")
        return self._page

    # ---- login ---------------------------------------------------------
    def login(self) -> None:
        if not cr.login(self.page):
            raise NavigationError(
                "Could not sign in to the application. Check the credentials and "
                "login selectors in .env, and that the environment is up.",
                blocked=True)
        cr.wait_until_settled(self.page, self.recorder)
        cr.stamp_content_root(self.page)

        # Record the main menu NOW, while the landing page is showing it. Once a
        # case is open this app replaces the sidebar with the case's own menu, so
        # reading it later would classify the case menu as "already known" and
        # then exclude the very entries we came to click.
        try:
            self.top_level_paths = {
                m["path"] for m in cr.collect_menu_targets(self.page,
                                                           crawler_config.BASE_URL)}
        except PWError:
            self.top_level_paths = set()

    # ---- navigation ----------------------------------------------------
    def navigate(self, target: Target) -> None:
        """Walk every hop of a target's path, recording where it got to."""
        # Remembered so a failing step can give route-specific advice.
        self.active_target = target
        total = len(target.steps)
        for i, step in enumerate(target.steps, start=1):
            self.emit({"kind": "step_start", "index": i, "total": total,
                       "text": step.describe()})
            try:
                note = self._do_step(step)
                self.steps.append(R.StepLog(i, step.kind, step.label or step.path,
                                            R.PASS, note))
                # A screenshot per hop is what makes the run watchable.
                shot = self.screenshot(f"step{i}-{step.kind}")
                self.emit({"kind": "step_done", "index": i, "total": total,
                           "status": R.PASS, "text": step.describe(),
                           "note": note, "shot": shot})
            except NavigationError as e:
                status = R.BLOCKED if e.blocked else R.FAIL
                self.steps.append(R.StepLog(
                    i, step.kind, step.label or step.path, status, str(e)))
                shot = self.screenshot(f"step{i}-{step.kind}-failed")
                self.emit({"kind": "step_done", "index": i, "total": total,
                           "status": status, "text": step.describe(),
                           "note": str(e), "shot": shot})
                raise

    def _do_step(self, step: NavStep) -> str:
        if step.kind == MENU:
            return self._step_menu(step)
        if step.kind == ROW:
            return self._step_row(step)
        if step.kind == ROW_BY_ID:
            return self._step_row_by_id(step)
        if step.kind == CONTEXT_MENU:
            return self._step_context_menu(step)
        if step.kind == TAB:
            return self._step_tab(step)
        if step.kind == ACTION:
            return self._step_action(step)
        raise NavigationError(f"Unsupported navigation step {step.kind!r}")

    def _step_menu(self, step: NavStep) -> str:
        self.recorder.clear()
        nav = cr.navigate_in_app(
            self.page,
            {"label": step.label or step.path, "href": step.path,
             "path": step.path,
             "url": crawler_config.BASE_URL.rstrip("/") + step.path
             if step.path.startswith("/") else step.path},
            self.recorder)
        if not nav.get("ok"):
            raise NavigationError(
                f"Could not open {step.label or step.path}: "
                f"{str(nav.get('error', ''))[:120]}", blocked=True)
        landed = urlparse(self.page.url).path.rstrip("/") or "/"
        if step.path and landed != step.path.rstrip("/"):
            raise NavigationError(
                f"Opening {step.label!r} landed on {landed} instead of {step.path}. "
                f"The route may have been removed or redirected.")
        return f"landed on {landed}"

    def _step_row(self, step: NavStep) -> str:
        openers = cr.collect_row_openers(self.page, max_rows=1)
        if not openers:
            raise NavigationError(
                "There are no records in this grid to open, so the screens below "
                "it cannot be reached. This usually means the environment has no "
                "test data rather than a defect.",
                blocked=step.missing_is_blocked)
        row = openers[0]
        before = urlparse(self.page.url).path.rstrip("/")
        self.recorder.clear()
        if not cr._click_stamped(self.page, "data-crawl-row", row["index"], timeout=6000):
            raise NavigationError(f"The record {row['label']!r} could not be opened.")
        cr.wait_until_settled(self.page, self.recorder, timeout_ms=15000, stable_polls=2)
        cr.stamp_content_root(self.page)
        after = urlparse(self.page.url).path.rstrip("/")
        if after == before and not cr._overlay_open(self.page):
            raise NavigationError(
                f"Clicking the record {row['label']!r} did not open anything.")
        self.opened_record = row["label"]
        return f"opened record {row['label']}"

    def _step_row_by_id(self, step: NavStep) -> str:
        """
        Open one SPECIFIC record, so a run always verifies the same data.

        Deliberately paginates rather than typing into the grid's search box:
        filling a search field would break the guarantee that this tool never
        types into the application at all, and that guarantee is worth more than
        the few seconds saved. Bounded by MAX_GRID_PAGES.
        """
        wanted = (step.value or step.label or "").strip()
        if not wanted:
            raise NavigationError("No record id was given to look for.", blocked=True)

        max_pages = getattr(settings, "MAX_GRID_PAGES", 12)
        scanned = 0
        for page_no in range(1, max_pages + 1):
            # A big cap so every row on the page is considered, not just the
            # first couple the crawler would take.
            openers = cr.collect_row_openers(self.page, max_rows=400)
            scanned += len(openers)
            match = next((r for r in openers
                          if wanted.lower() in (r.get("row_preview") or "").lower()), None)
            if match is not None:
                before = urlparse(self.page.url).path.rstrip("/")
                self.recorder.clear()
                if not cr._click_stamped(self.page, "data-crawl-row",
                                         match["index"], timeout=6000):
                    raise NavigationError(f"Record {wanted} was found but could not "
                                          f"be opened.")
                cr.wait_until_settled(self.page, self.recorder,
                                      timeout_ms=20000, stable_polls=2)
                cr.stamp_content_root(self.page)
                after = urlparse(self.page.url).path.rstrip("/")
                if after == before and not cr._overlay_open(self.page):
                    raise NavigationError(f"Clicking record {wanted} did not open "
                                          f"anything.")
                self.opened_record = wanted
                return (f"opened record {wanted}"
                        + (f" (page {page_no})" if page_no > 1 else ""))

            if not self._next_grid_page():
                break

        # Point at the right route rather than leaving the operator to work out
        # that the two grids hold different id formats.
        from .targets import route_advice
        advice = route_advice(wanted,
                             self.active_target.key if self.active_target else "")
        grid = ""
        if self.active_target and self.active_target.id_description:
            grid = (f" This grid holds {self.active_target.id_description} "
                    f"(e.g. {self.active_target.id_example}).")
        tail = (f" {advice}" if advice else
                " Check the id exists in this environment, or raise "
                "LOS_MAX_GRID_PAGES if the grid is longer.")
        raise NavigationError(
            f"Record {wanted} was not found here. Looked at {scanned} row(s) "
            f"across {page_no} page(s).{grid}{tail}",
            blocked=step.missing_is_blocked)

    def _next_grid_page(self) -> bool:
        """
        Advance the grid one page. Pagination is navigation, so it only has to
        clear the destructive denylist — it is not in the action allowlist and
        should not be, since "Next" is not an action a test would assert on.
        """
        try:
            return bool(self.page.evaluate(
                """() => {
                    const root = document.querySelector('[data-crawl-root]') || document.body;
                    const vis = (el) => el.offsetParent !== null || el.getClientRects().length;
                    const dead = (el) =>
                        el.classList.contains('disabled') ||
                        (el.parentElement && el.parentElement.classList.contains('disabled')) ||
                        el.getAttribute('aria-disabled') === 'true';
                    const cands = root.querySelectorAll(
                        '.pagination a, .pagination button, li.next a, ' +
                        '[aria-label="Next"], [title="Next"], a, button');
                    for (const el of cands) {
                        const t = (el.textContent || '').trim().toLowerCase();
                        const aria = (el.getAttribute('aria-label') || '').toLowerCase();
                        const isNext = t === 'next' || t === '›' || t === '>' ||
                                       aria === 'next';
                        if (!isNext || !vis(el) || dead(el)) continue;
                        el.click();
                        return true;
                    }
                    return false;
                }"""))
        except PWError:
            return False
        finally:
            try:
                cr.wait_until_settled(self.page, self.recorder,
                                      timeout_ms=12000, stable_polls=2)
                cr.stamp_content_root(self.page)
            except PWError:
                pass

    def open_sub_screen(self, sub: SubScreen) -> str:
        """
        Switch to one sub-screen of the opened record.

        Two mechanisms, and they are not interchangeable. The obligor screens
        are TABS of the screen already showing, so a tab that is already active
        counts as opened rather than missing. The case's own screens —
        Facilities, Collaterals, Queries and the rest — are each their own
        ROUTE, reached by clicking the case sidebar; the sidebar survives every
        one of those navigations, which is what lets a single run walk all
        nineteen of them without going back to the grid in between.
        """
        if sub.kind == CONTEXT_MENU:
            return self._step_context_menu(NavStep(kind=CONTEXT_MENU,
                                                   label=sub.label))
        return self._step_tab(NavStep(kind=TAB, label=sub.label,
                                      satisfied_if_active=sub.satisfied_if_active))

    # ---- drilling into a grid row --------------------------------------
    def row_opener_count(self, cap: int = 40) -> int:
        """How many grid rows on this screen can be opened."""
        return len(cr.collect_row_openers(self.page, max_rows=cap))

    def open_row_detail(self, index: int = 0) -> str:
        """
        Open one row's detail view on this screen.

        Why this is needed: several case screens are a SUMMARY grid. Facilities
        lists facility type and amount; the eighty-odd fields the specification
        defines for a facility are in the row's detail view. Checking only the
        summary reports every one of them missing on a screen that in fact
        holds the data — a false failure, which is worse than no check.

        `index` is a position in the screen's row openers, in document order,
        so consecutive indices walk down the page and across its grids. A screen
        like Additional Information holds thirteen tables, and reading only the
        first one's first row would report every value entered into the other
        twelve as lost.

        Still read-only. collect_row_openers never returns a checkbox or a
        control whose label fails the destructive denylist, nothing is typed,
        and the detail view is left via Escape / Cancel / Back only.

        Returns a note when a row was opened, or "" when there was nothing to
        open — an empty grid is ordinary, not an error.
        """
        # A generous cap rather than index+1: collect_row_openers stamps rows
        # and THEN drops any whose label fails the denylist, so asking for
        # exactly index+1 rows can come back short of `index` while the screen
        # plainly has more.
        openers = cr.collect_row_openers(self.page, max_rows=60)
        if index >= len(openers):
            return ""
        row = openers[index]
        before = urlparse(self.page.url).path.rstrip("/")
        self.recorder.clear()
        if not cr._click_stamped(self.page, "data-crawl-row", row["index"],
                                 timeout=6000):
            return ""
        try:
            cr.wait_until_settled(self.page, self.recorder, timeout_ms=15000,
                                  stable_polls=2)
            cr.stamp_content_root(self.page)
        except PWError:
            return ""
        after = urlparse(self.page.url).path.rstrip("/")
        if after == before and not cr._overlay_open(self.page):
            return ""          # the click revealed nothing; not a defect
        return f"opened record {row['label']}"

    def leave_row_detail(self) -> None:
        """
        Come back out of a row's detail so the next sidebar entry can be
        clicked. Best-effort by design: the case sidebar stays on screen for
        these routes, so the next hop re-establishes position anyway — and if it
        cannot, that hop reports it rather than this hiding it.
        """
        try:
            cr._dismiss_overlays(self.page)
        except PWError:
            pass

    def tab_strip(self, limit: int = 12) -> list[dict]:
        """
        The tab strip on screen right now, as [{"label", "active"}].

        Discovered live rather than declared, because these strips vary with the
        record: a syndicated facility does not show the same tabs as a running
        finance one, and hard-coding either list would report the other's tabs
        as missing. The active tab is included — it is a screen that needs
        checking too, and it is the one already showing.
        """
        out: list[dict] = []
        seen: set[str] = set()
        for t in cr.collect_tabs(self.page):
            if not t.get("safe") or t.get("disabled"):
                continue
            label = (t.get("label") or "").strip()
            if not label or label.lower() in seen:
                continue
            seen.add(label.lower())
            out.append({"label": label, "active": bool(t.get("active"))})
        return out[:limit]

    def open_tab(self, label: str) -> str:
        return self._step_tab(NavStep(kind=TAB, label=label,
                                      satisfied_if_active=True))

    def _step_context_menu(self, step: NavStep) -> str:
        items = cr.collect_context_menu(self.page, crawler_config.BASE_URL,
                                        self.top_level_paths)
        match = self._match_label(items, step.label, key="label")
        if match is None:
            available = ", ".join(i["label"] for i in items[:10]) or "none"
            raise NavigationError(
                f"'{step.label}' is not in the case menu. Available: {available}.")
        self.recorder.clear()
        if not cr._click_stamped(self.page, "data-crawl-menu", match["index"], timeout=6000):
            raise NavigationError(f"'{step.label}' could not be clicked.")
        cr.wait_until_settled(self.page, self.recorder, timeout_ms=15000, stable_polls=2)
        cr.stamp_content_root(self.page)
        return f"opened {match['label']} ({urlparse(self.page.url).path})"

    def _step_tab(self, step: NavStep) -> str:
        tabs = cr.collect_tabs(self.page)
        match = self._match_label(tabs, step.label, key="label")

        if match is None:
            # collect_tabs omits the active tab, so absence can simply mean we
            # are already on it. Confirm against the live DOM before failing —
            # Basic Information is the default tab on Obligor Details (BIR).
            if step.satisfied_if_active and self._tab_is_active(step.label):
                return f"'{step.label}' was already the open tab"
            available = ", ".join(t["label"] for t in tabs[:10]) or "none"
            raise NavigationError(
                f"The '{step.label}' tab was not found. Available: {available}.")

        if match.get("active"):
            return f"'{match['label']}' was already the open tab"
        self.recorder.clear()
        if not cr._click_stamped(self.page, "data-crawl-tab", match["index"], timeout=6000):
            raise NavigationError(f"The '{step.label}' tab could not be clicked.")
        cr.wait_until_settled(self.page, self.recorder, timeout_ms=12000, stable_polls=2)
        cr.stamp_content_root(self.page)
        return f"opened tab {match['label']}"

    def _step_action(self, step: NavStep) -> str:
        buttons = cr.collect_action_buttons(self.page)
        match = self._match_label(buttons, step.label, key="label")
        if match is None:
            available = ", ".join(b["label"] for b in buttons[:10]) or "none"
            raise NavigationError(
                f"The '{step.label}' button was not found. Available: {available}.")
        if match.get("disabled"):
            raise NavigationError(f"The '{step.label}' button is disabled.")
        self.recorder.clear()
        if not cr._click_stamped(self.page, "data-crawl-action", match["index"], timeout=6000):
            raise NavigationError(f"'{step.label}' could not be clicked.")
        cr.wait_until_settled(self.page, self.recorder, timeout_ms=15000, stable_polls=2)
        cr.stamp_content_root(self.page)
        return f"clicked {match['label']}"

    # ---- helpers -------------------------------------------------------
    @staticmethod
    def _norm(s: Optional[str]) -> str:
        s = (s or "").strip().lower().replace("&", " and ")
        s = re.sub(r"[^a-z0-9]+", " ", s)
        return re.sub(r"\s+", " ", s).strip()

    def _match_label(self, items: list[dict], label: str, key: str = "label"):
        """Exact-ish, then prefix, then containment. The app abbreviates menu
        text ('Relationship with Ot...'), and the FSD spells things differently
        from the DOM ('Sector & Industry' vs 'Sector And Industry')."""
        want = self._norm(label)
        if not want:
            return None
        norm = [(self._norm(i.get(key)), i) for i in items]
        for n, i in norm:
            if n == want:
                return i
        for n, i in norm:
            if n.startswith(want) or want.startswith(n):
                return i
        for n, i in norm:
            if want in n or n in want:
                return i
        return None

    def _tab_is_active(self, label: str) -> bool:
        try:
            return bool(self.page.evaluate(
                """(want) => {
                    const norm = (s) => (s||'').toLowerCase().replace(/&/g,' and ')
                        .replace(/[^a-z0-9]+/g,' ').trim();
                    const sels = '.nav-tabs a, .nav-pills a, [role="tab"], '
                               + 'a[data-toggle="tab"], .nav-link';
                    for (const el of document.querySelectorAll(sels)) {
                        const active = el.classList.contains('active')
                            || el.getAttribute('aria-selected') === 'true'
                            || (el.parentElement && el.parentElement.classList.contains('active'));
                        if (active && norm(el.textContent) === norm(want)) return true;
                    }
                    return false;
                }""", label))
        except PWError:
            return False

    # ---- observation ---------------------------------------------------
    def read_fields(self) -> list[dict]:
        try:
            return self.page.evaluate(_READ_FIELDS_JS, cr.SHELL_SELECTOR)
        except PWError:
            return []

    def read_table_headers(self) -> list[list[str]]:
        try:
            return self.page.evaluate(
                """() => {
                    const r = document.querySelector('[data-crawl-root]') || document.body;
                    return [...r.querySelectorAll('table')].map(
                        t => [...t.querySelectorAll('th')]
                              .map(h => (h.textContent||'').trim()).filter(Boolean)
                    ).filter(h => h.length).slice(0, 8);
                }""")
        except PWError:
            return []

    def read_grids(self) -> list[dict]:
        """
        Grids on the screen, with their column headers and row counts.

        Several of these screens are not forms at all. Sector And Industry, for
        instance, renders as a read-only table of Sector / Industry with a "+"
        to add rows — the FSD calls these Linkage Tables and there are 13 of
        them. Their specified fields appear as COLUMN HEADINGS, so a check that
        only looks at <input> elements sees an empty screen and reports every
        field missing.

        Scoped like read_fields: an open dialog wins over the page behind it,
        because a grid row's detail often opens in a modal and the summary grid
        underneath is not what is being checked at that point.
        """
        try:
            return self.page.evaluate(
                """() => {
                    let root = null;
                    for (const s of ['.modal.show', '.modal.in']) {
                        const el = document.querySelector(s);
                        if (el && (el.offsetParent !== null || el.getClientRects().length)) {
                            root = el; break;
                        }
                    }
                    if (!root) root = document.querySelector('[data-crawl-root]') || document.body;
                    const vis = (el) => el.offsetParent !== null || el.getClientRects().length;
                    const txt = (el) => (el.textContent || '').trim().replace(/\\s+/g, ' ');
                    const out = [];
                    for (const t of root.querySelectorAll('table')) {
                        if (!vis(t)) continue;
                        const headers = [...t.querySelectorAll('th')]
                            .map(txt).filter(Boolean);
                        const rows = [...t.querySelectorAll('tbody tr')].filter(vis);
                        const dataRows = rows.filter(
                            r => txt(r) && !/no (data|record|result)/i.test(txt(r)));
                        if (!headers.length && !dataRows.length) continue;
                        // A caption or preceding heading names the grid.
                        let title = '';
                        const cap = t.querySelector('caption');
                        if (cap) title = txt(cap);
                        if (!title) {
                            let n = t.parentElement;
                            for (let i = 0; i < 4 && n && !title; i++, n = n.parentElement) {
                                const h = n.querySelector('h1,h2,h3,h4,h5,h6,.card-title');
                                if (h) title = txt(h);
                            }
                        }
                        out.push({title: title.slice(0, 80), headers: headers.slice(0, 25),
                                  rows: dataRows.length});
                    }
                    return out.slice(0, 8);
                }""")
        except PWError:
            return []

    def error_banners(self) -> list[str]:
        try:
            return self.page.evaluate(_ERROR_BANNER_JS)
        except PWError:
            return []

    def validation_messages(self) -> list[str]:
        """Inline "X is Required" style messages — a statement about the record's
        completeness rather than a malfunction."""
        try:
            return self.page.evaluate(_VALIDATION_JS)
        except PWError:
            return []

    def failed_api_calls(self) -> list[dict]:
        """4xx/5xx responses seen since the last recorder.clear(). Free, and the
        strongest single signal that a screen is broken."""
        out = []
        for r in (self.recorder.responses if self.recorder else []):
            status = r.get("status") or 0
            if status >= 400:
                out.append({"method": r.get("method"), "url": r.get("url"),
                            "status": status})
        return out

    def screenshot(self, name: str) -> str:
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", name)[:80]
        path = os.path.join(self.artifacts_dir, f"{safe}.png")
        try:
            self.page.screenshot(path=path, full_page=True)
            return path
        except PWError:
            return ""
