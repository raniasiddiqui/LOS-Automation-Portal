"""
Playwright crawler: logs into the app and explores it the way a user does —
by clicking through the in-app menu, then clicking the action controls on
each screen — capturing per screen:

  - the ROUTED PAGE COMPONENT only (not the navbar/sidebar shell)
  - a content signature used to *prove* the route actually changed
  - "action states": the modals, side panels, and sub-routes that open when
    you click a screen's own buttons ("Create Obligor", "Raise Query", ...),
    each captured as its own record with its own forms
  - dropdown option sets
  - API requests AND response bodies for /api/ calls, because this app is
    config-driven (getDynamicFields, menuButtons, workflowWithColumns) and
    the real field definitions live in those responses, not in the HTML

Finding the routed content is the crux. Angular renders the active page
component as the element FOLLOWING the innermost <router-outlet>:

    <router-outlet></router-outlet><app-master>
        <app-navbar>...</app-navbar>          <- shell
        <app-sidebar>...</app-sidebar>        <- shell
        <router-outlet></router-outlet><app-bucket>   <- the actual screen
            <div class="content-body">...</div>
        </app-bucket>
    </app-master>

Guessing at ".content-body" / ".content-wrapper" instead does not work here:
several nodes match ".content-body" and the first one is not the routed one
(it yielded a 33KB region containing zero buttons), while ".content-wrapper"
is WIDER than the screen and drags <app-navbar> in — which is how "Preferences"
and "Sign out" ended up looking like page actions. So we walk from the
innermost router-outlet and stamp that element with data-crawl-root.

SAFETY: this can point at a production banking app. Every click is checked
against DESTRUCTIVE_ACTION_PATTERNS first, and only non-mutating
open/expand/view controls are clicked. Native dialogs are auto-dismissed and
file pickers are swallowed so nothing can block or commit. Set
config.ALLOW_INTERACTION = False for a passive, zero-click crawl.
"""
import json
import re
from collections import deque
from typing import Any, Optional
from urllib.parse import urljoin, urlparse

from playwright.sync_api import sync_playwright, Page, Request, Response, Error as PWError

import config


# Overlay containers this app actually uses (bootstrap-style .modal +
# SweetAlert2 — confirmed present; no mat-dialog/ngb-modal/cdk-overlay).
MODAL_SELECTORS = [
    ".modal.show",
    ".modal.in",
    ".modal[style*='display: block']",
    ".swal2-container",
]

LOADER_SELECTORS = ["ngx-ui-loader", ".ngx-background-spinner", ".sk-ball-spin-clockwise"]

# Shell regions. Never treated as page content and never clicked.
SHELL_SELECTOR = ("app-navbar, app-sidebar, app-footer, nav#stackNav, "
                  ".main-menu, .navbar, .header-navbar")

# Controls worth opening: they reveal a form/panel without committing anything.
SAFE_ACTION_PATTERNS = [
    r"\badd\b", r"\bnew\b", r"\bcreate\b", r"\braise\b", r"\brequest\b",
    r"\bfilter\b", r"\bsearch\b", r"\bview\b", r"\bdetail", r"\bopen\b",
    r"\bselect options\b", r"\bexpand\b", r"\badvanced\b", r"\bmore\b",
    r"\bcolumns?\b", r"\bconfigure\b", r"\bassign\b", r"\bmap\b",
    r"\bicon:(plus|add|create|new|search|filter|eye|view|expand)\b",
    r"\braise transaction\b", r"\bworkflow\b", r"\bpreview\b", r"\bshow\b",
]

# Never clicked, regardless of the allowlist. Anything that could write to a
# real banking record, end the session, fire a workflow transition, or open a
# native file/print dialog.
DESTRUCTIVE_ACTION_PATTERNS = [
    r"\bsave\b", r"\bsubmit\b", r"\bapprove\b", r"\breject\b", r"\bdelete\b",
    r"\bremove\b", r"\bpost\b", r"\bconfirm\b", r"\bproceed\b", r"\byes\b",
    r"\bsign ?out\b", r"\blog ?out\b", r"\bsend\b", r"\bauthorize\b",
    r"\bverify\b", r"\brelease\b", r"\bwithdraw\b", r"\bupdate\b", r"\bedit\b",
    r"\bmodify\b", r"\bbulk action\b", r"\bexecute\b", r"\bprint\b",
    r"\bexport\b", r"\bdownload\b", r"\bupload\b", r"\battach\b",
    r"\bpreferences\b", r"\brefresh\b", r"\breset\b",
]


def is_excluded(url: str) -> bool:
    return any(pat in url.lower() for pat in config.EXCLUDE_PATTERNS)


def same_origin(url: str, base: str) -> bool:
    return urlparse(url).netloc == urlparse(base).netloc


def _matches_any(text: str, patterns: list[str]) -> bool:
    low = (text or "").lower()
    return any(re.search(p, low) for p in patterns)


def is_safe_to_click(label: str) -> bool:
    """Denylist wins over allowlist, always."""
    if not label or not label.strip():
        return False
    if _matches_any(label, DESTRUCTIVE_ACTION_PATTERNS):
        return False
    return _matches_any(label, SAFE_ACTION_PATTERNS)


# --------------------------------------------------------------------------
# Network capture
# --------------------------------------------------------------------------

class NetworkRecorder:
    """
    Records /api/ traffic per page-visit. Response bodies matter here: this
    app builds its forms from config endpoints, so getDynamicFields /
    menuButtons / workflowWithColumns responses carry the real field names,
    required flags, dropdown enums and workflow states that the DOM never
    spells out. That is the richest signal available for test generation.
    """

    def __init__(self, max_body_chars: int = 20000):
        self.max_body_chars = max_body_chars
        self.requests: list[dict] = []
        self.responses: list[dict] = []
        self._inflight = 0

    def clear(self):
        self.requests.clear()
        self.responses.clear()

    @property
    def inflight(self) -> int:
        return self._inflight

    def on_request(self, req: Request):
        if req.resource_type not in ("xhr", "fetch"):
            return
        self._inflight += 1
        entry: dict[str, Any] = {"method": req.method, "url": req.url}
        if req.method in ("POST", "PUT", "PATCH"):
            try:
                body = req.post_data
                if body:
                    entry["request_body"] = body[: self.max_body_chars]
            except PWError:
                pass
        self.requests.append(entry)

    def on_response(self, resp: Response):
        req = resp.request
        if req.resource_type not in ("xhr", "fetch"):
            return
        self._inflight = max(0, self._inflight - 1)
        if "/api/" not in resp.url:
            return
        entry: dict[str, Any] = {"method": req.method, "url": resp.url, "status": resp.status}
        ctype = (resp.headers or {}).get("content-type", "")
        if "json" in ctype.lower():
            try:
                text = resp.text()
                entry["response_body"] = text[: self.max_body_chars]
                entry["truncated"] = len(text) > self.max_body_chars
            except (PWError, UnicodeDecodeError):
                pass
        self.responses.append(entry)


# --------------------------------------------------------------------------
# Locating the routed page component
# --------------------------------------------------------------------------

_STAMP_ROOT_JS = """(shellSel) => {
    document.querySelectorAll('[data-crawl-root]').forEach(e => e.removeAttribute('data-crawl-root'));

    // app-master is the OUTER routed component (it holds the shell), so it is
    // not itself a screen — keep descending past it to the inner outlet.
    const NOT_A_SCREEN = /^(app-master|app-navbar|app-sidebar|app-footer|app-root)$/;
    let best = null, bestDepth = -1;

    for (const outlet of document.querySelectorAll('router-outlet')) {
        let sib = outlet.nextElementSibling;
        while (sib) {
            const tag = sib.tagName.toLowerCase();
            if (tag.includes('-') && !NOT_A_SCREEN.test(tag)) {
                let d = 0, n = sib;
                while (n) { d++; n = n.parentElement; }
                if (d > bestDepth) { bestDepth = d; best = sib; }
                break;
            }
            sib = sib.nextElementSibling;
        }
    }

    // Fallback: no usable outlet sibling. Pick the candidate container with
    // the most form controls rather than the first one in document order —
    // several nodes match .content-body and the first is often an empty one.
    if (!best) {
        let score = -1;
        for (const c of document.querySelectorAll('.content-body, .content-wrapper, main, [role="main"]')) {
            if (c.closest(shellSel)) continue;
            const s = c.querySelectorAll('input,select,textarea,button,table,a.btn').length;
            if (s > score) { score = s; best = c; }
        }
    }
    if (!best) best = document.body;

    best.setAttribute('data-crawl-root', '1');
    return {
        tag: best.tagName.toLowerCase(),
        classes: String(best.className || '').slice(0, 80),
        via: bestDepth >= 0 ? 'router-outlet' : 'fallback-scored',
    };
}"""

ROOT_SEL = "[data-crawl-root]"


def stamp_content_root(page: Page) -> dict:
    """Mark the routed page component so every later query can scope to it."""
    try:
        return page.evaluate(_STAMP_ROOT_JS, SHELL_SELECTOR)
    except PWError:
        return {"tag": "", "classes": "", "via": "error"}


_FINGERPRINT_JS = """() => {
    const root = document.querySelector('[data-crawl-root]') || document.body;

    // Ancestors matter as much as descendants: the screen's own component tag
    // (app-bucket, app-query-listing) is the root itself or above the inner
    // container, so a descendants-only scan returns [] and every screen looks
    // identical. That produced false "SAME SCREEN" skips on 8 real screens.
    const chain = [];
    for (let n = root; n && n !== document.documentElement; n = n.parentElement) {
        const t = n.tagName.toLowerCase();
        if (t.includes('-')) chain.push(t);
    }
    const inner = [...root.querySelectorAll('*')]
        .map(e => e.tagName.toLowerCase()).filter(t => t.includes('-'));

    const txt = (e) => (e.textContent || '').trim().replace(/\\s+/g, ' ');
    return {
        components: [...new Set(chain.concat(inner))].sort(),
        headings: [...root.querySelectorAll(
            'h1,h2,h3,h4,h5,.card-title,.content-header-title,.page-title,legend,.card-header')]
            .map(txt).filter(Boolean).slice(0, 8),
        table_headers: [...root.querySelectorAll('th')].map(txt).filter(Boolean).slice(0, 20),
        field_names: [...root.querySelectorAll('input,select,textarea')]
            .map(e => e.getAttribute('formcontrolname') || e.getAttribute('name')
                   || e.getAttribute('placeholder') || '')
            .filter(Boolean).slice(0, 25),
        size: root.innerHTML.length,
        container: root.tagName.toLowerCase()
            + (root.className ? '.' + String(root.className).split(' ')[0] : ''),
    };
}"""


def route_fingerprint(page: Page) -> dict:
    try:
        return page.evaluate(_FINGERPRINT_JS)
    except PWError:
        return {"components": [], "headings": [], "table_headers": [],
                "field_names": [], "size": 0, "container": ""}


def fingerprint_signature(fp: dict) -> str:
    """Flatten a fingerprint into a comparable string."""
    return "||".join([
        ",".join(fp.get("components", [])),
        "|".join(fp.get("headings", [])),
        "|".join(fp.get("table_headers", [])),
        "|".join(fp.get("field_names", [])),
    ])


def _signature_is_substantive(sig: str) -> bool:
    """
    Only trust a signature enough to declare two screens identical if it
    actually carries information. An empty/near-empty signature means we
    failed to read the screen, not that it duplicates another one — treating
    those as duplicates is exactly what silently dropped 8 screens.
    """
    return len(re.sub(r"[|,\s]", "", sig)) >= 25


def content_html(page: Page) -> str:
    """The routed page component's markup only."""
    try:
        return page.evaluate(
            """() => {
                const el = document.querySelector('[data-crawl-root]');
                return el ? el.outerHTML : (document.body ? document.body.outerHTML : '');
            }"""
        )
    except PWError:
        return ""


def shell_html(page: Page) -> str:
    """Navbar + sidebar, captured ONCE for the whole crawl rather than per page."""
    try:
        return page.evaluate(
            """() => {
                const parts = [];
                for (const s of ['app-navbar', 'app-sidebar', 'nav#stackNav']) {
                    const el = document.querySelector(s);
                    if (el) parts.push(el.outerHTML);
                }
                return parts.join('\\n');
            }"""
        )
    except PWError:
        return ""


def count_form_controls(page: Page) -> int:
    try:
        return page.evaluate(
            """() => {
                const r = document.querySelector('[data-crawl-root]') || document.body;
                return r.querySelectorAll('input,select,textarea').length;
            }"""
        )
    except PWError:
        return 0


# --------------------------------------------------------------------------
# Login
# --------------------------------------------------------------------------

def login(page: Page) -> bool:
    """Log in and verify it actually succeeded. Returns True on success."""
    if not config.USERNAME or not config.PASSWORD:
        print("No credentials set — skipping login (assuming public app).")
        return True

    page.goto(config.LOGIN_URL, timeout=config.NAV_TIMEOUT_MS)
    page.wait_for_selector(config.LOGIN_SELECTORS["username_input"], timeout=config.NAV_TIMEOUT_MS)

    page.fill(config.LOGIN_SELECTORS["username_input"], config.USERNAME)
    page.fill(config.LOGIN_SELECTORS["password_input"], config.PASSWORD)

    pre_login_url = page.url
    page.click(config.LOGIN_SELECTORS["submit_button"])

    try:
        page.wait_for_function(
            """([preUrl, usernameSelector]) => {
                const formGone = !document.querySelector(usernameSelector);
                return window.location.href !== preUrl || formGone;
            }""",
            arg=[pre_login_url, config.LOGIN_SELECTORS["username_input"]],
            timeout=config.NAV_TIMEOUT_MS,
        )
    except PWError:
        pass  # fall through to explicit check below

    wait_until_settled(page)

    still_on_login = page.locator(config.LOGIN_SELECTORS["username_input"]).count() > 0
    if still_on_login:
        try:
            page.screenshot(path=f"{config.OUTPUT_DIR}/login_failure.png", full_page=True)
        except PWError:
            pass
        print("  ! Login appears to have failed — still seeing the login form.")
        print(f"  ! Current URL: {page.url}")
        return False

    print(f"  Login succeeded. Landed on: {page.url}")
    return True


# --------------------------------------------------------------------------
# Waiting: "settled" means spinner gone, XHRs drained, DOM stable
# --------------------------------------------------------------------------

def _loader_visible(page: Page) -> bool:
    for sel in LOADER_SELECTORS:
        try:
            if page.locator(sel).first.is_visible(timeout=250):
                return True
        except PWError:
            continue
    return False


def wait_until_settled(page: Page, recorder: Optional[NetworkRecorder] = None,
                       timeout_ms: int = 25000, stable_polls: int = 3) -> bool:
    """
    `networkidle` is unreliable on this app (polling + long-lived requests),
    and it returns before the data grid renders. Instead: poll until the
    content DOM size stops changing, no loader is visible, and no XHR is
    in flight. Returns True if it settled, False if it timed out.
    """
    interval = 400
    last_size = -1
    stable = 0
    elapsed = 0

    while elapsed < timeout_ms:
        try:
            size = page.evaluate(
                """() => {
                    const r = document.querySelector('[data-crawl-root]');
                    const el = r || document.body;
                    return el ? el.innerHTML.length : 0;
                }"""
            )
        except PWError:
            size = -1

        busy = _loader_visible(page) or (recorder is not None and recorder.inflight > 0)
        if size == last_size and size > 0 and not busy:
            stable += 1
            if stable >= stable_polls:
                return True
        else:
            stable = 0
        last_size = size
        page.wait_for_timeout(interval)
        elapsed += interval

    return False


# --------------------------------------------------------------------------
# Menu inventory + in-app navigation
# --------------------------------------------------------------------------

def collect_menu_targets(page: Page, base: str) -> list[dict]:
    """
    Read the sidebar/navbar menu once and return {label, href, path} for each
    nav target. We navigate by clicking these, not by goto-ing their href.
    """
    items = page.evaluate(
        """() => {
            const out = [];
            const scopes = ['app-sidebar', '.main-menu', 'ul.navigation', 'app-navbar', 'nav'];
            const seen = new Set();
            for (const scope of scopes) {
                const root = document.querySelector(scope);
                if (!root) continue;
                for (const a of root.querySelectorAll('a[href], a[routerLink], [routerLink]')) {
                    const href = a.getAttribute('href');
                    const rl = a.getAttribute('routerLink') || a.getAttribute('ng-reflect-router-link');
                    const label = (a.textContent || '').trim().replace(/\\s+/g, ' ').slice(0, 80);
                    const key = (href || '') + '|' + (rl || '') + '|' + label;
                    if (seen.has(key)) continue;
                    seen.add(key);
                    out.push({label, href, router_link: rl});
                }
            }
            return out;
        }"""
    )

    targets = []
    seen_paths = set()
    for it in items:
        raw = it.get("href") or it.get("router_link")
        if not raw or raw.startswith("javascript:") or raw.strip() == "#":
            continue
        full = urljoin(base, raw).split("#")[0]
        if not same_origin(full, base) or is_excluded(full):
            continue
        path = urlparse(full).path.rstrip("/") or "/"
        if path in seen_paths:
            continue
        seen_paths.add(path)
        targets.append({"label": it.get("label") or path, "href": raw, "url": full, "path": path})
    return targets


def navigate_in_app(page: Page, target: dict, recorder: NetworkRecorder) -> dict:
    """
    Click the menu entry so Angular routes client-side, keeping the app's
    bootstrapped state alive. Falls back to goto() only if no clickable
    element exists. Reports how we got there and whether the SPA survived,
    so a silent full-reload-and-redirect can't masquerade as a real visit.
    """
    before = route_fingerprint(page)
    href, path = target.get("href"), target["path"]

    try:
        page.evaluate("() => { window.__crawlMarker = true; }")
    except PWError:
        pass

    candidates = []
    if href:
        candidates += [f'a[href="{href}"]', f'[routerLink="{href}"]']
    candidates += [f'a[href="{path}"]', f'a[href$="{path}"]', f'[routerLink="{path}"]']

    method = None
    for sel in candidates:
        try:
            loc = page.locator(sel).first
            if loc.count() == 0:
                continue
            # Menu items often sit in collapsed submenus, so a normal click
            # fails the visibility check — dispatch the DOM click instead,
            # which still triggers Angular's routerLink handler.
            loc.evaluate("e => e.click()")
            method = f"in_app_click[{sel}]"
            break
        except PWError:
            continue

    if method is None:
        try:
            page.goto(target["url"], timeout=config.NAV_TIMEOUT_MS)
            method = "goto_fallback"
        except PWError as e:
            return {"ok": False, "method": "goto_fallback", "error": str(e)}

    settled = wait_until_settled(page, recorder)
    root_info = stamp_content_root(page)
    after = route_fingerprint(page)

    try:
        spa_alive = bool(page.evaluate("() => window.__crawlMarker === true"))
    except PWError:
        spa_alive = False

    return {
        "ok": True,
        "method": method,
        "settled": settled,
        "spa_preserved": spa_alive,
        "route_changed": fingerprint_signature(after) != fingerprint_signature(before),
        "final_url": page.url,
        "fingerprint": after,
        "root": root_info,
    }


# --------------------------------------------------------------------------
# Action discovery + exploration: where the real business forms live
# --------------------------------------------------------------------------

_STAMP_ACTIONS_JS = """(shellSel) => {
    document.querySelectorAll('[data-crawl-action]').forEach(e => e.removeAttribute('data-crawl-action'));

    const root = document.querySelector('[data-crawl-root]');
    const roots = [];
    if (root) roots.push(root);
    // A screen's toolbar often lives in app-page-header / .content-header,
    // which can sit OUTSIDE the routed component. Include those explicitly —
    // "Create Obligor"-style buttons are usually there.
    for (const e of document.querySelectorAll('app-page-header, .content-header, .card-header')) {
        if (!e.closest(shellSel) && !(root && root.contains(e))) roots.push(e);
    }
    if (!roots.length) roots.push(document.body);

    const ICON_HINT = /(plus|add|create|new|search|filter|eye|view|expand|detail)/i;
    const out = [];
    const seen = new Set();
    let i = 0;

    for (const scope of roots) {
        const els = scope.querySelectorAll(
            'button, a.btn, .btn, [role="button"], i[class*="ft-"], i[class*="fa-"], ' +
            'a[href="#"], a:not([href]), [class*="cursor-pointer"]');
        for (const el of els) {
            if (el.closest(shellSel)) continue;
            if (el.closest('.modal, .swal2-container')) continue;   // already-open overlay

            let label = (el.textContent || '').trim().replace(/\\s+/g, ' ');
            if (!label) {
                label = el.getAttribute('title') || el.getAttribute('aria-label')
                     || el.getAttribute('data-original-title') || '';
            }
            if (!label) {
                // Icon-only controls are extremely common in this template's
                // grid toolbars, so derive a label from the icon class.
                const cls = String(el.className || '') + ' ' +
                    [...el.querySelectorAll('i')].map(x => String(x.className || '')).join(' ');
                const m = ICON_HINT.exec(cls);
                if (m) label = 'icon:' + m[1].toLowerCase();
            }
            label = String(label).trim().slice(0, 80);
            if (!label) continue;

            const key = label.toLowerCase();
            if (seen.has(key)) continue;

            const visible = el.offsetParent !== null || el.getClientRects().length > 0;
            if (!visible) continue;

            seen.add(key);
            el.setAttribute('data-crawl-action', String(i));
            out.push({
                index: i,
                label,
                id: el.id || null,
                tag: el.tagName.toLowerCase(),
                classes: String(el.className || '').slice(0, 120),
                disabled: el.disabled === true || el.classList.contains('disabled')
                          || el.getAttribute('aria-disabled') === 'true',
                scope: scope.tagName.toLowerCase(),
            });
            i++;
        }
    }
    return out;
}"""


def collect_action_buttons(page: Page) -> list[dict]:
    """
    Discover the screen's own action controls and stamp each with
    data-crawl-action so we can click it later by a stable selector rather
    than re-matching fragile text locators against a mutated DOM.
    """
    try:
        raw = page.evaluate(_STAMP_ACTIONS_JS, SHELL_SELECTOR)
    except PWError:
        return []
    for b in raw:
        b["safe_to_click"] = is_safe_to_click(b["label"])
    return raw


def _overlay_open(page: Page) -> bool:
    for sel in MODAL_SELECTORS:
        try:
            if page.locator(sel).first.is_visible(timeout=200):
                return True
        except PWError:
            continue
    return False


def _overlay_html(page: Page) -> str:
    try:
        return page.evaluate(
            """(sels) => {
                for (const s of sels) {
                    const el = document.querySelector(s);
                    if (el && (el.offsetParent !== null || el.getClientRects().length)) return el.outerHTML;
                }
                return '';
            }""",
            MODAL_SELECTORS,
        )
    except PWError:
        return ""


def _dismiss_overlays(page: Page) -> bool:
    """Close any open modal without committing anything. Escape first, then an
    explicit close/cancel control — never a Proceed/OK/Yes/Save button."""
    for _ in range(3):
        if not _overlay_open(page):
            return True
        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(300)
        except PWError:
            pass
        if not _overlay_open(page):
            return True
        for sel in ['.modal.show .close', '.modal.show [aria-label="Close"]',
                    '.modal.show button:has-text("Cancel")', '.modal.show .btn-secondary',
                    ".swal2-cancel", ".swal2-close"]:
            try:
                loc = page.locator(sel).first
                if loc.count() > 0:
                    loc.click(timeout=1500)
                    page.wait_for_timeout(300)
                    break
            except PWError:
                continue
    return not _overlay_open(page)


def _restore_screen(page: Page, recorder: NetworkRecorder, target: dict) -> bool:
    """Get back to the screen under exploration after a state pulled us away."""
    _dismiss_overlays(page)
    if urlparse(page.url).path.rstrip("/") != target["path"]:
        try:
            page.go_back(timeout=config.NAV_TIMEOUT_MS)
            wait_until_settled(page, recorder)
        except PWError:
            pass
    if urlparse(page.url).path.rstrip("/") != target["path"]:
        nav = navigate_in_app(page, target, recorder)
        return bool(nav.get("ok"))
    stamp_content_root(page)
    return True


def explore_action_states(page: Page, recorder: NetworkRecorder, buttons: list[dict],
                          max_states: int, target: dict) -> list[dict]:
    """
    Click each safe action control and capture whatever it opens as its own
    state record: a modal, an inline panel, an expanded form, or a sub-route
    (a "Create Obligor" button often ROUTES to a create screen rather than
    opening a dialog — that case was previously invisible, which is why zero
    states were captured). Then restore the screen and continue.
    """
    states: list[dict] = []
    candidates = [b for b in buttons if b["safe_to_click"] and not b["disabled"]]
    if not candidates:
        labels = ", ".join(b["label"] for b in buttons[:6]) or "none found"
        print(f"      - no safe action controls to click (saw: {labels})")
        return states

    for cand in candidates:
        if len(states) >= max_states:
            break
        label = cand["label"]

        # Re-stamp: the DOM has probably changed since discovery, so indices
        # from the original pass may no longer point at the same element.
        fresh = collect_action_buttons(page)
        match = next((f for f in fresh if f["label"].lower() == label.lower()), None)
        if match is None:
            print(f"      - {label!r}: no longer present after previous action")
            continue

        before_sig = fingerprint_signature(route_fingerprint(page))
        before_url = page.url
        before_controls = count_form_controls(page)
        recorder.clear()

        sel = f'[data-crawl-action="{match["index"]}"]'
        try:
            page.locator(sel).first.click(timeout=4000)
        except PWError:
            # Some controls are overlapped or zero-sized; a DOM click still
            # fires the Angular (click) handler.
            try:
                page.eval_on_selector(sel, "e => e.click()")
            except PWError as e:
                print(f"      - {label!r}: unclickable ({str(e)[:60]})")
                continue

        wait_until_settled(page, recorder, timeout_ms=12000, stable_polls=2)

        overlay = _overlay_html(page)
        after_url = page.url
        stamp_content_root(page)
        after_fp = route_fingerprint(page)
        after_sig = fingerprint_signature(after_fp)
        after_controls = count_form_controls(page)

        if overlay:
            kind, html = "modal", overlay
        elif urlparse(after_url).path.rstrip("/") != urlparse(before_url).path.rstrip("/"):
            kind, html = "sub_route", content_html(page)
        elif after_sig != before_sig:
            kind, html = "inline_panel", content_html(page)
        elif after_controls > before_controls:
            kind, html = "expanded_form", content_html(page)
        else:
            kind, html = None, ""

        if kind:
            states.append({
                "trigger_label": label,
                "trigger_id": match.get("id"),
                "trigger_scope": match.get("scope"),
                "kind": kind,
                "html": html,
                "url_at_state": after_url,
                "fingerprint": after_fp,
                "api_calls": list(recorder.requests),
                "api_responses": list(recorder.responses),
            })
            print(f"      + {kind:14s} via {label!r} "
                  f"[{len(html)//1024}KB, {after_controls} controls, "
                  f"{len(recorder.responses)} api]")
        else:
            print(f"      . {label!r}: click produced no observable state change")

        if not _restore_screen(page, recorder, target):
            print(f"      ! could not return to {target['path']}; stopping exploration here")
            break

    return states


def collect_dropdown_options(page: Page) -> list[dict]:
    """Native <select> option sets, plus custom ng-select widgets, from the
    routed content. Enumerated values are directly testable."""
    try:
        return page.evaluate(
            """() => {
                const root = document.querySelector('[data-crawl-root]') || document.body;
                const out = [];
                for (const s of root.querySelectorAll('select')) {
                    out.push({
                        kind: 'select',
                        name: s.getAttribute('name') || s.getAttribute('formcontrolname') || s.id || null,
                        options: [...s.options].map(o => ({value: o.value, text: (o.textContent||'').trim()})),
                    });
                }
                for (const s of root.querySelectorAll('ng-select, [class*="ng-select"]')) {
                    out.push({
                        kind: 'ng-select',
                        name: s.getAttribute('formcontrolname') || s.getAttribute('name') || null,
                        placeholder: s.getAttribute('placeholder') || null,
                        options: [...s.querySelectorAll('.ng-option, .ng-value')]
                            .map(o => ({value: null, text: (o.textContent||'').trim()})),
                    });
                }
                return out;
            }"""
        )
    except PWError:
        return []


# --------------------------------------------------------------------------
# Recursive exploration: tabs, and records opened from a list screen
#
# A list screen ("All Obligors", "My Bucket") shows almost nothing testable on
# its own — the real surface is one level down: open a case, then walk its
# sub-screens (Basic Information, Sector & Industry, Facilities, Collaterals,
# Documents...). This layer drives that descent.
#
# It is deliberately READ-ONLY. Tabs and record links do not mutate anything,
# and every click still passes the same DESTRUCTIVE_ACTION_PATTERNS denylist
# used everywhere else. Nothing is ever typed into a field and no Save /
# Submit / Approve / Delete control is reachable from here.
#
# This layer is ADDITIVE: explore_action_states() is untouched and still runs
# first. Whatever it finds is deduped against what the recursion finds.
# --------------------------------------------------------------------------

# Tab strips / wizard steps / sub-menus used by this admin template.
TAB_SELECTOR = (
    '.nav-tabs a, .nav-pills a, [role="tab"], a[data-toggle="tab"], '
    'a[data-bs-toggle="tab"], .bs-stepper .step-trigger, .wizard .nav-link, '
    'ul.nav.nav-tabs li a, .card-header .nav-link, .list-group-item-action'
)

# Affordances that open a record from a grid row, tried in this order.
#
# In this app the click handler is on the ROW ITSELF — rows render as
# <tr class="clickable"> (My Bucket) or <tr class="editable"> (All Approval
# Requests) with Angular's (click) binding compiled away, and the cells contain
# nothing but <span> text. An earlier version looked only for anchors and
# buttons inside the row and therefore found no opener on any screen: the sole
# <a> in each row lives inside a <td style="display:none"> calendar cell.
ROW_CLICKABLE_ROW_SELECTOR = (
    'tr.clickable, tr.editable, tr[class*="pointer"], tr[style*="cursor"], '
    'tr[class*="clickable"], tr[class*="selectable"]'
)
ROW_OPEN_SELECTOR = (
    'a[href]:not([href="#"]), a[routerLink], [role="button"], '
    'i[class*="eye"], i[class*="ft-eye"], i[class*="fa-eye"], '
    'i[class*="search"], i[class*="folder"], .btn'
)


# Once a case is open, this app REPLACES the sidebar with a case-scoped menu
# (Credit Approval Memo, Obligor Details (BIR), Queries, Request Details,
# Facilities, Collaterals, Facility Coverage, eCIB Details, ...), and each entry
# is its own route. Those are the sub-pages that make a case worth crawling, but
# they live inside app-sidebar / .main-menu — which SHELL_SELECTOR excludes
# everywhere else, and which collect_menu_targets only ever read once at login.
# So they need their own discovery pass, run again at every level.
CONTEXT_MENU_SELECTOR = (
    'app-sidebar a, .main-menu a, ul.navigation a, .menu-content a, '
    '.sidebar a, aside a, [routerLink]'
)

# Never followed while descending: these leave the case we are exploring.
CONTEXT_MENU_SKIP = re.compile(
    r"^\s*(back to menu|back|home|dashboard|menu)\s*$|\bsign ?out\b|\blog ?out\b", re.I)


def is_safe_tab(label: str) -> bool:
    """
    Tabs and record links are navigation, not actions, so they do not need to
    appear in SAFE_ACTION_PATTERNS — a tab is legitimately called
    "Sector & Industry" or "Shareholders and Directors Details", which no
    action allowlist would ever match. The destructive denylist still applies
    in full, so a control labelled Save/Submit/Approve/Delete can never be
    reached through this path either.
    """
    if not label or not label.strip():
        return False
    if len(label) > 60:                      # a whole paragraph is not a tab
        return False
    return not _matches_any(label, DESTRUCTIVE_ACTION_PATTERNS)


class _Budget:
    """
    Two independent caps on a single screen's descent.

    `remaining` bounds how many states are KEPT. `clicks` bounds how many are
    ATTEMPTED — and that second one is the load-bearing limit. Deduplicated
    states cost no budget but are still recursed into, so a subtree where
    everything looks familiar would otherwise walk forever without ever
    decrementing anything.
    """

    def __init__(self, max_states: int, max_clicks: Optional[int] = None):
        self.remaining = max_states
        self.clicks = max_clicks if max_clicks is not None else max_states * 4

    def take(self) -> bool:
        if self.remaining <= 0:
            return False
        self.remaining -= 1
        return True

    def visit(self) -> bool:
        if self.clicks <= 0:
            return False
        self.clicks -= 1
        return True

    @property
    def exhausted(self) -> bool:
        return self.remaining <= 0 or self.clicks <= 0


_STAMP_TABS_JS = """(args) => {
    const [tabSel, shellSel] = args;
    document.querySelectorAll('[data-crawl-tab]').forEach(e => e.removeAttribute('data-crawl-tab'));

    // Prefer whatever overlay is open: inside a "Create Obligor" dialog the
    // tabs that matter are the dialog's, not the page's behind it.
    let scope = null;
    for (const s of ['.modal.show', '.modal.in', '.swal2-container']) {
        const el = document.querySelector(s);
        if (el && (el.offsetParent !== null || el.getClientRects().length)) { scope = el; break; }
    }
    if (!scope) scope = document.querySelector('[data-crawl-root]') || document.body;

    const out = [];
    const seen = new Set();
    let i = 0;
    for (const el of scope.querySelectorAll(tabSel)) {
        if (el.closest(shellSel)) continue;
        const label = (el.textContent || '').trim().replace(/\\s+/g, ' ').slice(0, 60);
        if (!label) continue;
        const key = label.toLowerCase();
        if (seen.has(key)) continue;
        const visible = el.offsetParent !== null || el.getClientRects().length > 0;
        if (!visible) continue;
        seen.add(key);
        el.setAttribute('data-crawl-tab', String(i));
        out.push({
            index: i,
            label,
            active: el.classList.contains('active') || el.getAttribute('aria-selected') === 'true',
            disabled: el.classList.contains('disabled') || el.getAttribute('aria-disabled') === 'true',
        });
        i++;
    }
    return out;
}"""


def collect_tabs(page: Page) -> list[dict]:
    """Tab strip / wizard steps of the current view (or of the open dialog)."""
    try:
        raw = page.evaluate(_STAMP_TABS_JS, [TAB_SELECTOR, SHELL_SELECTOR])
    except PWError:
        return []
    for t in raw:
        t["safe"] = is_safe_tab(t["label"])
    return raw


_STAMP_ROWS_JS = """(args) => {
    const [rowSel, clickableRowSel, shellSel, maxRows] = args;
    document.querySelectorAll('[data-crawl-row]').forEach(e => e.removeAttribute('data-crawl-row'));

    let scope = null;
    for (const s of ['.modal.show', '.modal.in']) {
        const el = document.querySelector(s);
        if (el && (el.offsetParent !== null || el.getClientRects().length)) { scope = el; break; }
    }
    if (!scope) scope = document.querySelector('[data-crawl-root]') || document.body;

    const visible = (el) => el.offsetParent !== null || el.getClientRects().length > 0;
    const out = [];
    let i = 0;

    for (const tbody of scope.querySelectorAll('table tbody')) {
        if (tbody.closest(shellSel)) continue;
        for (const tr of tbody.querySelectorAll('tr')) {
            if (out.length >= maxRows) break;
            if (!visible(tr)) continue;

            // Skip "no data" placeholders and header-ish rows with no cells.
            const rowText = (tr.textContent || '').trim();
            if (!rowText || /no (data|record|result)/i.test(rowText)) continue;
            const cells = [...tr.querySelectorAll('td')].filter(visible);
            if (!cells.length) continue;

            let opener = null, how = '';

            // 1. The row itself carries the handler — the normal case here.
            if (tr.matches(clickableRowSel)) {
                opener = tr; how = 'row';
            }

            // 2. Otherwise an explicit affordance inside the row, but only one
            //    that is actually visible: the calendar <a> in these grids sits
            //    in a <td style="display:none"> and clicking it does nothing.
            if (!opener) {
                for (const cand of tr.querySelectorAll(rowSel)) {
                    if (cand.matches('input, label')) continue;
                    if (cand.querySelector('input[type=checkbox], input[type=radio]')) continue;
                    if (!visible(cand)) continue;
                    opener = cand; how = 'affordance';
                    break;
                }
            }

            // 3. Last resort: a data cell with text. Skip the first cell when it
            //    holds the Bulk Action checkbox — never tick a selection box.
            if (!opener) {
                for (const td of cells) {
                    if (td.querySelector('input[type=checkbox], input[type=radio]')) continue;
                    if (!(td.textContent || '').trim()) continue;
                    opener = td; how = 'cell';
                    break;
                }
            }
            if (!opener) continue;

            // Label from the row's first meaningful cell (the Request ID),
            // which identifies the record far better than the opener's text.
            let label = '';
            for (const td of cells) {
                if (td.querySelector('input[type=checkbox], input[type=radio]')) continue;
                const t = (td.textContent || '').trim().replace(/\\s+/g, ' ');
                if (t) { label = t; break; }
            }
            if (!label) label = (opener.getAttribute('title') || '').trim();

            opener.setAttribute('data-crawl-row', String(i));
            out.push({
                index: i,
                label: String(label).slice(0, 60),
                row_preview: rowText.replace(/\\s+/g, ' ').slice(0, 140),
                tag: opener.tagName.toLowerCase(),
                how: how,
            });
            i++;
        }
    }
    return out;
}"""


def collect_row_openers(page: Page, max_rows: int) -> list[dict]:
    """
    One opener per grid row — the link or view icon that opens that record.
    Checkboxes are deliberately skipped: ticking a Bulk Action box mutates
    selection state and leads toward bulk operations we must never touch.
    """
    try:
        raw = page.evaluate(_STAMP_ROWS_JS, [ROW_OPEN_SELECTOR, ROW_CLICKABLE_ROW_SELECTOR,
                                             SHELL_SELECTOR, max_rows])
    except PWError:
        return []
    out = []
    for r in raw:
        # A row opener is read-only navigation, but the label still has to
        # clear the destructive denylist — a row's action column can contain
        # a Delete icon, and that must never be the thing we click.
        if is_safe_tab(r["label"]):
            out.append(r)
    return out


_STAMP_CONTEXT_MENU_JS = """(menuSel) => {
    document.querySelectorAll('[data-crawl-menu]').forEach(e => e.removeAttribute('data-crawl-menu'));
    const out = [];
    const seen = new Set();
    let i = 0;
    for (const a of document.querySelectorAll(menuSel)) {
        const href = a.getAttribute('href');
        const rl = a.getAttribute('routerLink') || a.getAttribute('ng-reflect-router-link');
        if (!href && !rl) continue;
        const label = (a.textContent || '').trim().replace(/\\s+/g, ' ').slice(0, 60);
        if (!label) continue;
        const key = label.toLowerCase() + '|' + (href || rl);
        if (seen.has(key)) continue;
        seen.add(key);
        a.setAttribute('data-crawl-menu', String(i));
        out.push({index: i, label, href, router_link: rl,
                  active: a.classList.contains('active')
                          || (a.parentElement && a.parentElement.classList.contains('active'))});
        i++;
    }
    return out;
}"""


def collect_context_menu(page: Page, base: str, known_paths: set) -> list[dict]:
    """
    Menu entries visible right now whose route is NOT one of the top-level
    screens already queued by the main crawl. Inside an open case that is
    exactly the case menu; on a top-level screen it is empty, because every
    sidebar entry there is already known — which is what stops this from
    walking the crawler back out to the main menu.
    """
    try:
        raw = page.evaluate(_STAMP_CONTEXT_MENU_JS, CONTEXT_MENU_SELECTOR)
    except PWError:
        return []

    out, seen_paths = [], set()
    for item in raw:
        label = item["label"]
        if CONTEXT_MENU_SKIP.search(label):
            continue
        # Same denylist as everywhere else: a menu entry called Delete or
        # Approve is never followed.
        if not is_safe_tab(label):
            continue
        rawhref = item.get("href") or item.get("router_link")
        if not rawhref or rawhref.startswith("javascript:") or rawhref.strip() == "#":
            continue
        full = urljoin(base, rawhref).split("#")[0]
        if not same_origin(full, base) or is_excluded(full):
            continue
        path = urlparse(full).path.rstrip("/") or "/"
        if path in known_paths or path in seen_paths:
            continue
        seen_paths.add(path)
        out.append({**item, "url": full, "path": path})
    return out


def _restore_to(page: Page, recorder: NetworkRecorder, anchor: dict) -> bool:
    """
    Return to a specific view rather than always to the top-level screen.
    Inside a case, the thing to come back to is the case page, not the list
    that led to it — going back to the list would drop us out of the case
    entirely and lose the rest of its menu.
    """
    _dismiss_overlays(page)
    want = anchor["path"]
    for _ in range(3):
        if urlparse(page.url).path.rstrip("/") == want:
            stamp_content_root(page)
            return True
        try:
            page.go_back(timeout=config.NAV_TIMEOUT_MS)
            wait_until_settled(page, recorder)
        except PWError:
            break
    ok = urlparse(page.url).path.rstrip("/") == want
    if ok:
        stamp_content_root(page)
    return ok


def grid_digest(page: Page) -> str:
    """
    A cheap signature of what the grid is currently SHOWING.

    The route fingerprint deliberately ignores row data, which is right for
    deciding whether a route changed but wrong for filter tabs: switching
    All -> In Progress -> Approved refilters the same grid, so components,
    headings, table headers and field names are all identical. Those tabs were
    being discarded as "nothing new" — and worse, never recursed into. Adding
    row count plus the first row's text makes each filter a distinct state.
    """
    try:
        return page.evaluate(
            """() => {
                const root = document.querySelector('[data-crawl-root]') || document.body;
                const rows = [...root.querySelectorAll('table tbody tr')]
                    .filter(r => r.offsetParent !== null || r.getClientRects().length);
                const first = rows.length
                    ? (rows[0].textContent || '').trim().replace(/\\s+/g, ' ').slice(0, 120) : '';
                const last = rows.length > 1
                    ? (rows[rows.length - 1].textContent || '').trim().replace(/\\s+/g, ' ').slice(0, 60) : '';
                return rows.length + '|' + first + '|' + last;
            }"""
        )
    except PWError:
        return ""


def _click_stamped(page: Page, attr: str, index: int, timeout: int = 4000) -> bool:
    sel = f'[{attr}="{index}"]'
    try:
        page.locator(sel).first.click(timeout=timeout)
        return True
    except PWError:
        try:
            page.eval_on_selector(sel, "e => e.click()")
            return True
        except PWError:
            return False


def _snapshot(page: Page, recorder: NetworkRecorder, kind: str, label: str,
              trail: tuple, depth: int) -> dict:
    """Build a state record for whatever is currently on screen."""
    overlay = _overlay_html(page)
    html = overlay or content_html(page)
    fp = route_fingerprint(page)
    return {
        # Same shape explore_action_states produces, so parser.py and the
        # knowledge graph need no changes to consume these.
        "trigger_label": label,
        "trigger_id": None,
        "trigger_scope": "recursive",
        "kind": kind,
        "html": html,
        "url_at_state": page.url,
        "fingerprint": fp,
        "api_calls": list(recorder.requests),
        "api_responses": list(recorder.responses),
        # Extra context the recursion knows and the flat pass cannot.
        "depth": depth,
        "path_trail": list(trail),
    }


def explore_recursively(page: Page, recorder: NetworkRecorder, target: dict,
                        budget: _Budget, seen_sigs: set, depth: int = 0,
                        trail: tuple = (), max_depth: int = 3,
                        max_rows: int = 2,
                        blocked_tabs: frozenset = frozenset(),
                        blocked_menu_paths: frozenset = frozenset(),
                        known_paths: Optional[set] = None,
                        anchor: Optional[dict] = None) -> list[dict]:
    """
    Descend through the current view: walk its tabs, open its records, and
    repeat inside whatever those reveal.

    Ordering matters. Tabs are walked before rows because a tab click keeps
    us on the same route (cheap, always recoverable), whereas opening a record
    navigates away and needs a restore afterwards.

    `blocked_tabs` carries the labels of the tab strip we descended THROUGH.
    Clicking a tab does not remove its strip from the page, so without this the
    child level re-walks the same strip and clicks the siblings — producing
    nonsense paths like "In Progress > All > Approved", where all three are
    peers of one filter bar. Sibling tabs are alternatives, not children.

    `anchor` is the view to come back to at THIS level. Inside an open case that
    is the case page, not the list screen that led here — restoring to the list
    would drop out of the case and abandon the rest of its menu.
    """
    states: list[dict] = []
    if depth >= max_depth or budget.exhausted:
        return states

    if known_paths is None:
        known_paths = set()
    if anchor is None:
        anchor = {"path": target["path"], "url": target.get("url") or page.url}

    indent = "      " + "  " * depth

    # ---- 1. Tabs / wizard steps / sub-menus at this level ------------------
    current_strip = frozenset(t["label"].lower() for t in collect_tabs(page))
    for tab in collect_tabs(page):
        if budget.exhausted:
            break
        if not tab["safe"] or tab["disabled"] or tab["active"]:
            continue
        if tab["label"].lower() in blocked_tabs:
            continue  # a sibling of a strip we already came through
        if tab["label"].lower() in {t.lower() for t in trail}:
            continue  # already inside this tab further up the stack

        # Re-stamp: a previous click re-rendered the strip, so old indices rot.
        fresh = collect_tabs(page)
        match = next((t for t in fresh if t["label"].lower() == tab["label"].lower()), None)
        if match is None:
            continue

        if not budget.visit():
            break
        before_sig = fingerprint_signature(route_fingerprint(page)) + "#" + grid_digest(page)
        recorder.clear()
        if not _click_stamped(page, "data-crawl-tab", match["index"]):
            continue
        wait_until_settled(page, recorder, timeout_ms=10000, stable_polls=2)
        stamp_content_root(page)

        # Signature INCLUDES the grid contents, so a status filter counts as a
        # distinct state rather than a duplicate of the tab beside it.
        sig = fingerprint_signature(route_fingerprint(page)) + "#" + grid_digest(page)
        is_new = sig != before_sig and sig not in seen_sigs

        if is_new and budget.take():
            seen_sigs.add(sig)
            state = _snapshot(page, recorder, "tab", tab["label"], trail + (tab["label"],), depth)
            states.append(state)
            print(f"{indent}+ tab            {tab['label'][:40]!r} "
                  f"[{len(state['html'])//1024}KB, {count_form_controls(page)} controls]")
        else:
            print(f"{indent}. tab            {tab['label'][:40]!r} (same content — not recorded)")

        # Recurse REGARDLESS of whether the tab itself was worth recording.
        # "In Progress" may render an identical-looking grid, but the records
        # inside it are the whole point of coming here — gating recursion on
        # novelty is what stopped any case from being opened.
        if not budget.exhausted:
            states.extend(explore_recursively(
                page, recorder, target, budget, seen_sigs, depth + 1,
                trail + (tab["label"],), max_depth, max_rows,
                blocked_tabs=blocked_tabs | current_strip,
                blocked_menu_paths=blocked_menu_paths,
                known_paths=known_paths, anchor=anchor))

    # ---- 2. Context menu: the case-scoped sidebar --------------------------
    # Inside an open case the sidebar becomes the case's own menu, each entry a
    # separate route (Obligor Details (BIR), Queries, Facilities, Collaterals,
    # eCIB Details...). These siblings share that sidebar, so we walk them
    # in sequence exactly as a user would — no going back between them.
    if not budget.exhausted:
        menu_items = [m for m in collect_context_menu(page, config.BASE_URL, known_paths)
                      if m["path"] not in blocked_menu_paths]
        # Every sub-page keeps showing the same case sidebar, so the whole strip
        # must be blocked for deeper levels. Without this, Obligor Details would
        # "contain" Queries, which would "contain" Facilities — a fake hierarchy
        # that expands combinatorially and never terminates.
        current_menu = frozenset(m["path"] for m in menu_items)
        if menu_items:
            print(f"{indent}> case menu: {len(menu_items)} sub-page(s): "
                  f"{', '.join(m['label'][:18] for m in menu_items[:6])}"
                  f"{'...' if len(menu_items) > 6 else ''}")
        visited_here: set = set()

        for item in menu_items:
            if budget.exhausted:
                break
            if item["path"] in visited_here:
                continue
            visited_here.add(item["path"])

            fresh = collect_context_menu(page, config.BASE_URL, known_paths)
            match = next((m for m in fresh if m["path"] == item["path"]), None)
            if match is None:
                continue

            if not budget.visit():
                break
            recorder.clear()
            if not _click_stamped(page, "data-crawl-menu", match["index"], timeout=6000):
                print(f"{indent}. sub_screen     {item['label'][:34]!r}: unclickable")
                continue
            wait_until_settled(page, recorder, timeout_ms=15000, stable_polls=2)
            stamp_content_root(page)

            here = urlparse(page.url).path.rstrip("/") or "/"
            sig = fingerprint_signature(route_fingerprint(page))
            if here != item["path"] and sig in seen_sigs:
                print(f"{indent}. sub_screen     {item['label'][:34]!r}: did not navigate")
                continue

            if sig not in seen_sigs and budget.take():
                seen_sigs.add(sig)
                state = _snapshot(page, recorder, "sub_screen", item["label"],
                                  trail + (item["label"],), depth)
                state["sub_page_url"] = page.url
                states.append(state)
                print(f"{indent}+ sub_screen     {item['label'][:34]!r} -> {here[-42:]} "
                      f"[{len(state['html'])//1024}KB, {count_form_controls(page)} controls]")

            # Everything under this sub-page: its tabs, its grids, and any
            # further menu it exposes. The anchor becomes this sub-page so a
            # record opened here returns here, not to the case root.
            if not budget.exhausted:
                states.extend(explore_recursively(
                    page, recorder, target, budget, seen_sigs, depth + 1,
                    trail + (item["label"],), max_depth, max_rows,
                    blocked_tabs=frozenset(),
                    blocked_menu_paths=blocked_menu_paths | current_menu,
                    known_paths=known_paths,
                    anchor={"path": here, "url": page.url}))

        # Sibling sub-pages share the sidebar, so we only need to come back to
        # this level's anchor once the whole menu has been walked.
        if menu_items:
            _restore_to(page, recorder, anchor)

    # ---- 3. Records opened from a grid at this level -----------------------
    if depth < max_depth and not budget.exhausted:
        openers = collect_row_openers(page, max_rows)
        if not openers:
            # Say so out loud. A silent zero here is what made an earlier run
            # look like "recursion did nothing" when in fact no row opener was
            # ever recognised.
            try:
                nrows = page.evaluate(
                    """() => {
                        const r = document.querySelector('[data-crawl-root]') || document.body;
                        return r.querySelectorAll('table tbody tr').length;
                    }""")
            except PWError:
                nrows = -1
            if nrows > 0:
                print(f"{indent}. no openable row found among {nrows} grid row(s) — "
                      f"rows may not be clickable on this screen")

        for row in openers:
            if budget.exhausted:
                break

            fresh = collect_row_openers(page, max_rows)
            match = next((r for r in fresh if r["label"].lower() == row["label"].lower()), None)
            if match is None:
                continue

            if not budget.visit():
                break
            before_sig = fingerprint_signature(route_fingerprint(page))
            before_url = page.url
            recorder.clear()
            if not _click_stamped(page, "data-crawl-row", match["index"]):
                continue
            wait_until_settled(page, recorder, timeout_ms=15000, stable_polls=2)
            stamp_content_root(page)

            sig = fingerprint_signature(route_fingerprint(page))
            url_changed = (urlparse(page.url).path.rstrip("/")
                           != urlparse(before_url).path.rstrip("/"))
            opened = _overlay_open(page) or url_changed or sig != before_sig

            if opened and sig not in seen_sigs and budget.take():
                seen_sigs.add(sig)
                label = f"open record: {row['label'][:40]}"
                state = _snapshot(page, recorder, "record_detail", label,
                                  trail + (label,), depth)
                state["row_preview"] = row.get("row_preview", "")
                states.append(state)
                print(f"{indent}+ record_detail  {row['label'][:36]!r} "
                      f"[{len(state['html'])//1024}KB, {count_form_controls(page)} controls]")

                # The case detail is where the sub-screens live — this is the
                # descent that makes list screens worth crawling at all.
                # blocked_tabs resets: a case detail carries its own strip
                # (Basic Information / Sector & Industry / Collaterals), which
                # has nothing to do with the status filter we came in through.
                # The anchor becomes the case page, so its sub-pages return
                # here instead of back out to the list.
                states.extend(explore_recursively(
                    page, recorder, target, budget, seen_sigs, depth + 1,
                    trail + (label,), max_depth, max_rows,
                    blocked_tabs=frozenset(),
                    blocked_menu_paths=blocked_menu_paths,
                    known_paths=known_paths,
                    anchor={"path": urlparse(page.url).path.rstrip("/") or "/",
                            "url": page.url}))
            elif not opened:
                print(f"{indent}. row {row['label'][:30]!r} (how={match['how']}): "
                      f"click opened nothing")
            else:
                print(f"{indent}. row {row['label'][:30]!r}: same layout as a record "
                      f"already captured")

            if not _restore_to(page, recorder, anchor):
                print(f"{indent}! lost {anchor['path']} after opening a record; stopping descent")
                break
            # Returning re-rendered the grid, so any tab context is gone too.
            if depth > 0:
                break

    return states


def load_fsd_tab_hints() -> dict:
    """
    Expected sub-screen names from the FSD's data dictionary, used only to
    report what the crawl did NOT reach. Purely advisory — the crawl works
    identically without an FSD, and any failure here is non-fatal.
    """
    try:
        import knowledge_graph as ks
        with ks.connect() as conn:
            groups = ks.get_field_spec_sections(conn)
    except Exception:  # noqa: BLE001 - FSD is optional; never break the crawl
        return {}

    hints: dict[str, set] = {}
    for g in groups:
        section = re.sub(r"^\s*[\d.]+\s*", "", g.get("section") or "").strip()
        for f in g.get("fields", []):
            sub = (f.get("sub_menu") or "").strip()
            if sub:
                hints.setdefault(section, set()).add(sub)
    return {k: sorted(v) for k, v in hints.items() if v}


# --------------------------------------------------------------------------
# Crawl
# --------------------------------------------------------------------------

def crawl() -> list[dict]:
    pages_data: list[dict] = []
    max_states = getattr(config, "MAX_ACTION_STATES_PER_PAGE", 8)
    allow_interaction = getattr(config, "ALLOW_INTERACTION", True)
    recursive = allow_interaction and getattr(config, "RECURSIVE_EXPLORATION", True)
    max_depth = getattr(config, "MAX_RECURSION_DEPTH", 3)
    max_rows = getattr(config, "MAX_ROWS_PER_SCREEN", 2)
    max_deep = getattr(config, "MAX_DEEP_STATES_PER_PAGE", 40)
    fsd_hints = load_fsd_tab_hints() if recursive else {}
    if fsd_hints:
        total = sum(len(v) for v in fsd_hints.values())
        print(f"  FSD hints: {total} expected sub-screens across {len(fsd_hints)} sections.")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=getattr(config, "HEADLESS", True))
        context = browser.new_context(viewport={"width": 1600, "height": 1000})
        page = context.new_page()
        page.set_default_timeout(20000)

        # A native confirm() or a file picker would block the crawl forever,
        # and dismissing is always the non-committing choice.
        page.on("dialog", lambda d: d.dismiss())
        page.on("filechooser", lambda fc: None)

        recorder = NetworkRecorder()
        page.on("request", recorder.on_request)
        page.on("response", recorder.on_response)

        if not login(page):
            print("Aborting crawl: login failed. Check LOGIN_SELECTORS/credentials in config.py,")
            print("and inspect output/login_failure.png if it was saved.")
            browser.close()
            return []

        base = config.BASE_URL
        wait_until_settled(page, recorder)
        stamp_content_root(page)

        shell = shell_html(page)
        menu = collect_menu_targets(page, base)
        print(f"  Menu inventory: {len(menu)} nav targets discovered from the shell.")

        queue = deque([{"label": "(landing)", "href": None, "url": page.url,
                        "path": urlparse(page.url).path.rstrip("/") or "/"}] + menu)

        # Routes the main loop will visit anyway. The recursion treats any menu
        # entry OUTSIDE this set as a context (case-scoped) sub-page, which is
        # how "Obligor Details (BIR)" is told apart from "All Obligors".
        top_level_paths = {m["path"] for m in menu}
        top_level_paths.add(urlparse(page.url).path.rstrip("/") or "/")

        visited_paths: set[str] = set()
        signatures: dict[str, str] = {}
        skipped_duplicates: list[tuple[str, str]] = []

        while queue and len(pages_data) < config.MAX_PAGES:
            target = queue.popleft()
            if target["path"] in visited_paths:
                continue
            visited_paths.add(target["path"])

            recorder.clear()
            nav = navigate_in_app(page, target, recorder)
            if not nav.get("ok"):
                print(f"  ! failed to reach {target['path']}: {nav.get('error', '')[:90]}")
                continue

            fp = nav["fingerprint"]
            sig = fingerprint_signature(fp)

            # Only treat two screens as the same when the signature carries
            # real information. A blank signature means we failed to read the
            # screen, not that it duplicates one we've already seen.
            if _signature_is_substantive(sig):
                dup_of = signatures.get(sig)
                if dup_of and dup_of != target["path"]:
                    print(f"  ~ SAME SCREEN as {dup_of} — skipping {target['path']}")
                    skipped_duplicates.append((target["path"], dup_of))
                    continue
                signatures.setdefault(sig, target["path"])
            else:
                print(f"  ? weak signature for {target['path']} "
                      f"(root={nav['root'].get('tag')} via {nav['root'].get('via')}) — keeping anyway")

            final_path = urlparse(nav["final_url"]).path.rstrip("/") or "/"
            if final_path != target["path"]:
                print(f"  ~ redirected: {target['path']} -> {final_path}")
            visited_paths.add(final_path)
            target = {**target, "path": final_path}

            body = content_html(page)
            buttons = collect_action_buttons(page)
            dropdowns = collect_dropdown_options(page)
            nav_api_calls = list(recorder.requests)
            nav_api_responses = list(recorder.responses)

            print(f"  crawled ({len(pages_data)+1}/{config.MAX_PAGES}): {final_path}"
                  f"  [root={nav['root'].get('tag')}, {len(body)//1024}KB, "
                  f"{len(buttons)} controls, {len(nav_api_responses)} api]")

            states = []
            if allow_interaction:
                states = explore_action_states(page, recorder, buttons, max_states, target)

            # Additive second pass: descend into tabs and into records opened
            # from this screen's grid. Runs after the flat pass and dedupes
            # against it by fingerprint, so nothing is captured twice.
            if recursive:
                _restore_screen(page, recorder, target)
                seen_sigs = {fingerprint_signature(s["fingerprint"])
                             for s in states if s.get("fingerprint")}
                seen_sigs.add(fingerprint_signature(fp))
                budget = _Budget(max_deep)
                deep = explore_recursively(
                    page, recorder, target, budget, seen_sigs,
                    depth=0, trail=(), max_depth=max_depth, max_rows=max_rows,
                    # The top-level menu paths are already queued by the main
                    # loop; excluding them is what keeps the case-menu walk
                    # from navigating back out to the main menu.
                    known_paths=top_level_paths,
                    anchor={"path": target["path"], "url": nav["final_url"]})
                if deep:
                    by_kind: dict[str, int] = {}
                    for s in deep:
                        by_kind[s["kind"]] = by_kind.get(s["kind"], 0) + 1
                    print(f"      = recursion added {len(deep)} states {by_kind} "
                          f"(depth<={max_depth}, budget left {budget.remaining})")
                states.extend(deep)
                _restore_screen(page, recorder, target)

            pages_data.append({
                "url": nav["final_url"],
                "requested_url": target["url"],
                "menu_label": target["label"],
                "title": page.title(),
                "html": body,
                "content_root": nav["root"],
                "fingerprint": fp,
                "nav_method": nav["method"],
                "spa_preserved": nav["spa_preserved"],
                "route_changed": nav["route_changed"],
                "settled": nav["settled"],
                "action_buttons": buttons,
                "dropdowns": dropdowns,
                "states": states,
                "api_calls": nav_api_calls,
                "api_responses": nav_api_responses,
                "discovered_links": [m["url"] for m in menu],
            })

        browser.close()

    _report(pages_data, shell, skipped_duplicates, fsd_hints)
    return pages_data


def _report(pages_data: list[dict], shell: str, skipped: list[tuple[str, str]],
            fsd_hints: Optional[dict] = None):
    """Diagnostics that make a bad crawl obvious instead of plausible."""
    if not pages_data:
        return
    unique = len({fingerprint_signature(p["fingerprint"]) for p in pages_data})
    weak = sum(1 for p in pages_data
               if not _signature_is_substantive(fingerprint_signature(p["fingerprint"])))
    fallback_roots = sum(1 for p in pages_data if p["content_root"].get("via") != "router-outlet")
    states = sum(len(p["states"]) for p in pages_data)
    by_kind: dict[str, int] = {}
    for p in pages_data:
        for s in p["states"]:
            by_kind[s["kind"]] = by_kind.get(s["kind"], 0) + 1
    clickable = sum(1 for p in pages_data for b in p["action_buttons"]
                    if b["safe_to_click"] and not b["disabled"])

    print(f"\n  Screens captured:                   {len(pages_data)}")
    print(f"  Distinct signatures:                {unique}/{len(pages_data)}")
    print(f"  Weak/unreadable signatures:         {weak}")
    print(f"  Content root via fallback (not outlet): {fallback_roots}")
    print(f"  Safe action controls found:         {clickable}")
    print(f"  Action states captured:             {states} {by_kind if by_kind else ''}")
    print(f"  Shell captured once:                {len(shell)//1024}KB")

    deep = [s for p in pages_data for s in p["states"] if s.get("depth") is not None]
    if deep:
        by_depth: dict[int, int] = {}
        for s in deep:
            by_depth[s["depth"]] = by_depth.get(s["depth"], 0) + 1
        print(f"  Reached by recursion:               {len(deep)} "
              f"(by depth: {dict(sorted(by_depth.items()))})")
        deepest = max(deep, key=lambda s: len(s.get("path_trail") or []))
        if deepest.get("path_trail"):
            print(f"  Deepest path:                       "
                  f"{' > '.join(deepest['path_trail'])[:100]}")

    if skipped:
        print(f"  Skipped as duplicates:              {len(skipped)}")
        for path, of in skipped:
            print(f"     {path}  ==  {of}")

    # Which sub-screens the FSD documents but the crawl never opened. This is
    # the coverage question that matters: a tab we never reached produces no
    # test cases no matter how well the FSD describes it.
    if fsd_hints:
        reached = {(s.get("trigger_label") or "").strip().lower()
                   for p in pages_data for s in p["states"]}
        missing = []
        for section, subs in sorted(fsd_hints.items()):
            for sub in subs:
                if sub.strip().lower() not in reached:
                    missing.append(f"{section} > {sub}")
        found = sum(len(v) for v in fsd_hints.values()) - len(missing)
        print(f"  FSD sub-screens reached:            "
              f"{found}/{sum(len(v) for v in fsd_hints.values())}")
        for m in missing[:12]:
            print(f"     not reached: {m[:88]}")
        if len(missing) > 12:
            print(f"     ... and {len(missing) - 12} more")

    if states == 0 and clickable > 0:
        print("  ! Controls were found but no states captured — check the '.' lines above.")


if __name__ == "__main__":
    import os
    os.makedirs(config.OUTPUT_DIR, exist_ok=True)
    data = crawl()
    with open(config.CRAWL_MAP_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    print(f"\nCrawled {len(data)} pages. Saved raw map to {config.CRAWL_MAP_FILE}")
