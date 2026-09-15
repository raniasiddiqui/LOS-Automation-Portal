"""
Scrapling parser: takes the raw HTML captured per page by crawler.py and
extracts a structured inventory of UI elements — forms, inputs, buttons,
links, tables. This is the "understanding the layout" layer, separate
from crawling so it can be re-run/tuned without re-crawling the app.

Requires: pip install scrapling
Tested against scrapling 0.4.x, which exposes `Selector` (older releases
called this class `Adaptor` — we fall back to that name if needed).
"""
import json
from typing import Any

try:
    from scrapling import Selector as _ScraplingParser
except ImportError:
    from scrapling import Adaptor as _ScraplingParser  # older scrapling versions

import config


def _attrs(el) -> dict:
    """Normalize an element's attributes to a plain dict."""
    try:
        return dict(el.attrib)
    except Exception:
        return {}


def _text(el) -> str:
    """
    Full concatenated text of an element, including text that sits after
    a nested child tag — e.g. <button><i class="ft-unlock"></i> Login
    </button>. Plain `.text` only returns the first text node (empty
    string here), so we use get_all_text() and fall back gracefully.
    """
    try:
        t = el.get_all_text(strip=True)
        if t:
            return t
    except Exception:
        pass
    return (el.text or "").strip()


def _label_for(field_el, form_el) -> str:
    """
    Best-effort human label for a form field: <label for="id">, a wrapping
    <label>, a placeholder, or aria-label — in that priority order. Angular
    Material / ngx-style forms often skip <label for> and rely on
    placeholder text instead (see the login form we captured: no <label>,
    just placeholder="User Name").
    """
    attrs = _attrs(field_el)
    field_id = attrs.get("id")
    if field_id:
        labels = form_el.css(f'label[for="{field_id}"]')
        if labels:
            text = _text(labels[0])
            if text:
                return text
    if attrs.get("aria-label"):
        return attrs["aria-label"].strip()
    if attrs.get("placeholder"):
        return attrs["placeholder"].strip()
    if attrs.get("name"):
        return attrs["name"]
    return ""


def parse_form(form_el) -> dict:
    attrs = _attrs(form_el)
    fields = []

    for tag in ("input", "select", "textarea"):
        for el in form_el.css(tag):
            fattrs = _attrs(el)
            field = {
                "tag": tag,
                "type": fattrs.get("type", "text" if tag == "input" else tag),
                "name": fattrs.get("name") or fattrs.get("formcontrolname"),
                "id": fattrs.get("id"),
                "label": _label_for(el, form_el),
                "placeholder": fattrs.get("placeholder"),
                "required": ("required" in fattrs) or (fattrs.get("aria-required") == "true"),
                "maxlength": fattrs.get("maxlength"),
                "pattern": fattrs.get("pattern"),
                "attrs": fattrs,
            }
            if tag == "select":
                field["options"] = [
                    {"value": _attrs(o).get("value"), "text": _text(o)}
                    for o in el.css("option")
                ]
            fields.append(field)

    buttons = []
    for tag in ("button", "input"):
        selector = tag if tag == "button" else 'input[type="submit"], input[type="button"], input[type="reset"]'
        for el in form_el.css(selector):
            battrs = _attrs(el)
            btype = battrs.get("type", "submit" if tag == "button" else battrs.get("type"))
            buttons.append({
                "tag": tag,
                "type": btype,
                "text": _text(el) or battrs.get("value", ""),
                "id": battrs.get("id"),
                "attrs": battrs,
            })

    return {
        "id": attrs.get("id"),
        "name": attrs.get("name"),
        "action": attrs.get("action"),
        "method": (attrs.get("method") or "get").lower(),
        "fields": fields,
        "buttons": buttons,
    }


def parse_standalone_buttons(page, form_elements) -> list[dict]:
    """Buttons that live outside any <form> — common for Angular apps that
    submit via (click) handlers calling a component method instead of a
    native form POST."""
    form_button_ids = set()
    for f in form_elements:
        for b in f.css("button"):
            bid = _attrs(b).get("id")
            if bid:
                form_button_ids.add(bid)

    out = []
    for el in page.css("button"):
        attrs = _attrs(el)
        if attrs.get("id") in form_button_ids:
            continue
        out.append({
            "tag": "button",
            "type": attrs.get("type", "button"),
            "text": _text(el),
            "id": attrs.get("id"),
            "attrs": attrs,
        })
    return out


def parse_links(page) -> list[dict]:
    out = []
    for el in page.css("a"):
        attrs = _attrs(el)
        href = attrs.get("href")
        if not href:
            continue
        out.append({
            "href": href,
            "text": _text(el),
            "id": attrs.get("id"),
            "router_link": attrs.get("routerlink") or attrs.get("routerLink"),
        })
    # Angular routerLink-only nav items (no href at all)
    for el in page.css("[routerLink]"):
        attrs = _attrs(el)
        if attrs.get("href"):
            continue  # already captured above
        out.append({
            "href": None,
            "text": _text(el),
            "id": attrs.get("id"),
            "router_link": attrs.get("routerlink") or attrs.get("routerLink"),
        })
    return out


def parse_tables(page) -> list[dict]:
    out = []
    for t in page.css("table"):
        headers = [_text(h) for h in t.css("th") if _text(h)]
        row_count = len(t.css("tbody tr")) or max(len(t.css("tr")) - 1, 0)
        out.append({
            "id": _attrs(t).get("id"),
            "headers": headers,
            "row_count": row_count,
        })
    return out


def parse_unformed_fields(page, form_elements) -> list[dict]:
    """
    Inputs that sit outside any <form>. Angular reactive forms very often
    bind to a <div [formGroup]> rather than a real <form>, so restricting
    field extraction to <form> descendants silently loses most of the real
    inputs on a screen. Anything with formControlName is a genuine field.
    """
    in_form_ids = set()
    for f in form_elements:
        for tag in ("input", "select", "textarea"):
            for el in f.css(tag):
                a = _attrs(el)
                in_form_ids.add(a.get("id") or a.get("name") or a.get("formcontrolname") or id(el))

    out = []
    for tag in ("input", "select", "textarea"):
        for el in page.css(tag):
            a = _attrs(el)
            key = a.get("id") or a.get("name") or a.get("formcontrolname") or id(el)
            if key in in_form_ids:
                continue
            ftype = a.get("type", "text" if tag == "input" else tag)
            if ftype in ("hidden",):
                continue
            field = {
                "tag": tag,
                "type": ftype,
                "name": a.get("name") or a.get("formcontrolname"),
                "id": a.get("id"),
                "label": _label_for(el, page),
                "placeholder": a.get("placeholder"),
                "required": ("required" in a) or (a.get("aria-required") == "true"),
                "maxlength": a.get("maxlength"),
                "pattern": a.get("pattern"),
                "form_group": a.get("formgroupname") or a.get("ng-reflect-form-group-name"),
                "attrs": a,
            }
            if tag == "select":
                field["options"] = [
                    {"value": _attrs(o).get("value"), "text": _text(o)} for o in el.css("option")
                ]
            out.append(field)
    return out


def parse_state(state: dict) -> dict:
    """
    Parse one captured action-state (a modal or inline panel opened by
    clicking a button). These carry the app's actual business forms — the
    "Raise Query" / "Add Obligor" dialogs — which never appear in a passive
    page capture, so without parsing them the generator can only ever
    describe search boxes and Cancel buttons.
    """
    html = _strip_shell(state.get("html") or "")
    if not html:
        return {}
    page = _ScraplingParser(html)
    form_elements = page.css("form")
    return {
        "trigger_label": state.get("trigger_label"),
        "kind": state.get("kind"),
        "title": _text(page.css(".modal-title")[0]) if page.css(".modal-title") else "",
        "forms": [parse_form(f) for f in form_elements],
        "fields_outside_forms": parse_unformed_fields(page, form_elements),
        "buttons": parse_standalone_buttons(page, form_elements),
        "tables": parse_tables(page),
        "api_calls": state.get("api_calls", []),
        "api_responses": state.get("api_responses", []),
    }


# This app nests <app-navbar> inside each routed page component, so even a
# correctly-scoped capture (app-bucket, app-condition, ...) still contains the
# navbar. Left in, the parser attributes "Preferences", "Sign out" and the
# navbar's "Explore Stack..." search box to every screen — which is how shell
# controls ended up being offered as test targets.
SHELL_TAGS = ("app-navbar", "app-sidebar", "app-footer")
SHELL_CSS = ("nav#stackNav", ".main-menu", ".navbar", ".header-navbar", ".menu-content")


def _strip_shell(html: str) -> str:
    """Drop navbar/sidebar subtrees before parsing. Uses lxml (already a
    scrapling dependency) so nesting is handled properly — a regex over HTML
    would mangle it."""
    try:
        from lxml import html as lxml_html
    except ImportError:
        return html
    if not html or not html.strip():
        return html
    try:
        root = lxml_html.fromstring(html)
    except Exception:
        return html

    removed = 0
    for tag in SHELL_TAGS:
        for el in root.xpath(f"//{tag}"):
            parent = el.getparent()
            if parent is not None:
                parent.remove(el)
                removed += 1
    for css in SHELL_CSS:
        try:
            for el in root.cssselect(css):
                parent = el.getparent()
                if parent is not None:
                    parent.remove(el)
                    removed += 1
        except Exception:
            continue
    if not removed:
        return html
    return lxml_html.tostring(root, encoding="unicode")


def parse_page(page_record: dict) -> dict:
    """
    page_record: one entry from crawler.py's site_map.json. "html" is the
    route content region (not the whole document), plus the crawler now
    supplies "states", "dropdowns", "action_buttons" and "api_responses".
    """
    html = _strip_shell(page_record["html"])
    page = _ScraplingParser(html)

    form_elements = page.css("form")
    forms = [parse_form(f) for f in form_elements]

    states = [s for s in (parse_state(s) for s in page_record.get("states", [])) if s]

    return {
        "url": page_record["url"],
        "title": page_record.get("title", ""),
        "menu_label": page_record.get("menu_label"),
        "fingerprint": page_record.get("fingerprint", {}),
        "forms": forms,
        "fields_outside_forms": parse_unformed_fields(page, form_elements),
        "standalone_buttons": parse_standalone_buttons(page, form_elements),
        "action_buttons": page_record.get("action_buttons", []),
        "dropdowns": page_record.get("dropdowns", []),
        "links": parse_links(page),
        "tables": parse_tables(page),
        "states": states,
        "api_calls": page_record.get("api_calls", []),
        "api_responses": page_record.get("api_responses", []),
        "discovered_links": page_record.get("discovered_links", []),
    }


def parse_site_map(site_map_path: str) -> list[dict]:
    with open(site_map_path, "r", encoding="utf-8") as f:
        raw_pages = json.load(f)
    return [parse_page(p) for p in raw_pages]


if __name__ == "__main__":
    import os
    parsed = parse_site_map(config.CRAWL_MAP_FILE)
    os.makedirs(config.OUTPUT_DIR, exist_ok=True)
    out_path = os.path.join(config.OUTPUT_DIR, "parsed_pages.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(parsed, f, indent=2)

    total_forms = sum(len(p["forms"]) for p in parsed)
    total_fields = sum(len(form["fields"]) for p in parsed for form in p["forms"])
    print(f"Parsed {len(parsed)} pages: {total_forms} forms, {total_fields} fields.")
    print(f"Saved structured inventory to {out_path}")