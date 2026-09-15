"""
Knowledge store: persists the parsed site understanding so crawler.py and
parser.py don't have to run every time. Backed by plain SQLite (no server
to stand up) but modeled as a graph — pages are nodes, discovered links
and form-submit actions are edges, and every form/button/input/table is
an element attached to its page. That's enough structure for both
"what's on this page" queries and "how do I get from page A to page B"
queries, which the test-case generator needs for multi-step flows
(e.g. login -> dashboard -> raise a query).

Requires nothing beyond the standard library.
"""
import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterable, Optional
from urllib.parse import urljoin

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS pages (
    url TEXT PRIMARY KEY,
    title TEXT,
    html_hash TEXT,
    first_crawled TEXT,
    last_crawled TEXT
);

CREATE TABLE IF NOT EXISTS elements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    page_url TEXT NOT NULL,
    element_type TEXT NOT NULL,      -- form | field | button | link | table
    parent_id INTEGER,               -- e.g. a field's parent form
    tag TEXT,
    name TEXT,
    label TEXT,
    detail_json TEXT NOT NULL,       -- full parsed dict for this element
    FOREIGN KEY (page_url) REFERENCES pages(url) ON DELETE CASCADE,
    FOREIGN KEY (parent_id) REFERENCES elements(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_page TEXT NOT NULL,
    target_page TEXT NOT NULL,
    relation TEXT NOT NULL DEFAULT 'navigates_to',
    via_element_id INTEGER,
    FOREIGN KEY (source_page) REFERENCES pages(url) ON DELETE CASCADE,
    FOREIGN KEY (via_element_id) REFERENCES elements(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS api_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    page_url TEXT NOT NULL,
    method TEXT,
    url TEXT,
    status INTEGER,
    -- Response bodies from this app's config endpoints (getDynamicFields,
    -- menuButtons, workflowWithColumns) define the real form fields,
    -- required flags and dropdown enums. Keeping them makes the generator
    -- able to write cases about actual business data instead of guesses.
    response_body TEXT,
    FOREIGN KEY (page_url) REFERENCES pages(url) ON DELETE CASCADE
);

-- ---------------------------------------------------------------------
-- FSD side of the graph. The crawler contributes structure (what exists);
-- the FSD contributes intent (what it is for, who does it, what the rules
-- are). Keeping them as separate node types in ONE graph is what lets us
-- join them and report gaps in both directions.
-- ---------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS fsd_processes (
    process_id TEXT PRIMARY KEY,
    name TEXT,
    module TEXT,
    actors TEXT,            -- JSON array
    preconditions TEXT,     -- JSON array
    outcomes TEXT,          -- JSON array
    alternate_flows TEXT,   -- JSON array
    source_section TEXT
);

CREATE TABLE IF NOT EXISTS fsd_steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    process_id TEXT NOT NULL,
    seq INTEGER,
    actor TEXT,
    action TEXT,
    screen_hint TEXT,
    expected TEXT,
    data_json TEXT,
    FOREIGN KEY (process_id) REFERENCES fsd_processes(process_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS fsd_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    process_id TEXT NOT NULL,
    kind TEXT NOT NULL,     -- business_rule | validation
    field TEXT,
    rule TEXT,
    applies_to TEXT,
    valid_examples TEXT,    -- JSON array
    invalid_examples TEXT,  -- JSON array
    FOREIGN KEY (process_id) REFERENCES fsd_processes(process_id) ON DELETE CASCADE
);

-- Field specifications lifted verbatim from the FSD's data-dictionary tables
-- ("Field Name | Type | Mandatory | Values / Format"). These carry the
-- mandatory flags, formats and enumerated values that make a test case
-- concrete, and they are parsed deterministically rather than by the LLM.
CREATE TABLE IF NOT EXISTS fsd_field_specs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    section TEXT NOT NULL,
    screen_hint TEXT,
    sub_menu TEXT,          -- the tab within the screen, when the FSD names one
    name TEXT NOT NULL,
    type TEXT,
    mandatory TEXT,
    values_format TEXT,
    allowed_values TEXT     -- JSON array
);

-- Grounding: which crawled screen (and optionally which action-state on it)
-- implements a given FSD step. Rows with a low score are still recorded so
-- the coverage report can show near-misses rather than silently dropping them.
CREATE TABLE IF NOT EXISTS fsd_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    step_id INTEGER NOT NULL,
    process_id TEXT NOT NULL,
    page_url TEXT,
    state_trigger TEXT,
    score REAL,
    evidence TEXT,
    FOREIGN KEY (step_id) REFERENCES fsd_steps(id) ON DELETE CASCADE
);

-- Which crawled screen a whole field-spec section describes. Separate from
-- fsd_links because a spec section is not a step — it is a data dictionary
-- for a screen, and it grounds by field-name overlap rather than by action.
CREATE TABLE IF NOT EXISTS fsd_spec_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    section TEXT NOT NULL,
    page_url TEXT,
    score REAL,
    evidence TEXT
);

CREATE INDEX IF NOT EXISTS idx_elements_page ON elements(page_url);
CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source_page);
CREATE INDEX IF NOT EXISTS idx_apicalls_page ON api_calls(page_url);
CREATE INDEX IF NOT EXISTS idx_fsdsteps_proc ON fsd_steps(process_id);
CREATE INDEX IF NOT EXISTS idx_fsdrules_proc ON fsd_rules(process_id);
CREATE INDEX IF NOT EXISTS idx_fsdlinks_step ON fsd_links(step_id);
"""


@contextmanager
def connect(db_path: Optional[str] = None):
    path = db_path or config.DB_FILE
    # SQLite will not create missing intermediate directories, so a fresh
    # checkout (or a cleared output/ folder) failed with a bare
    # "unable to open database file" on the very first command.
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(db_path: Optional[str] = None):
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)
        # CREATE TABLE IF NOT EXISTS won't add columns to a DB created by an
        # earlier version, so bring old api_calls tables forward in place.
        existing = {r["name"] for r in conn.execute("PRAGMA table_info(api_calls)")}
        for col, decl in (("status", "INTEGER"), ("response_body", "TEXT")):
            if col not in existing:
                conn.execute(f"ALTER TABLE api_calls ADD COLUMN {col} {decl}")


def has_pages(db_path: Optional[str] = None) -> bool:
    with connect(db_path) as conn:
        row = conn.execute("SELECT COUNT(*) AS c FROM pages").fetchone()
        return row["c"] > 0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def upsert_page(conn: sqlite3.Connection, url: str, title: str, html: str):
    html_hash = hashlib.sha256(html.encode("utf-8")).hexdigest()
    now = _now()
    existing = conn.execute("SELECT html_hash FROM pages WHERE url = ?", (url,)).fetchone()
    if existing is None:
        conn.execute(
            "INSERT INTO pages (url, title, html_hash, first_crawled, last_crawled) VALUES (?, ?, ?, ?, ?)",
            (url, title, html_hash, now, now),
        )
        return True  # newly seen page (or content changed vs before — see below)
    changed = existing["html_hash"] != html_hash
    conn.execute(
        "UPDATE pages SET title = ?, html_hash = ?, last_crawled = ? WHERE url = ?",
        (title, html_hash, now, url),
    )
    if changed:
        # structure may have changed — wipe old elements/edges/api_calls for
        # this page so re-parsing doesn't leave stale entries alongside new ones
        conn.execute("DELETE FROM elements WHERE page_url = ?", (url,))
        conn.execute("DELETE FROM edges WHERE source_page = ?", (url,))
        conn.execute("DELETE FROM api_calls WHERE page_url = ?", (url,))
    return changed


def add_element(
    conn: sqlite3.Connection,
    page_url: str,
    element_type: str,
    detail: dict,
    parent_id: Optional[int] = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO elements (page_url, element_type, parent_id, tag, name, label, detail_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            page_url,
            element_type,
            parent_id,
            detail.get("tag"),
            detail.get("name"),
            detail.get("label") or detail.get("text"),
            json.dumps(detail),
        ),
    )
    return cur.lastrowid


def add_edge(
    conn: sqlite3.Connection,
    source_page: str,
    target_page: str,
    relation: str = "navigates_to",
    via_element_id: Optional[int] = None,
):
    conn.execute(
        "INSERT INTO edges (source_page, target_page, relation, via_element_id) VALUES (?, ?, ?, ?)",
        (source_page, target_page, relation, via_element_id),
    )


def add_api_calls(conn: sqlite3.Connection, page_url: str, api_calls: Iterable[dict]):
    conn.executemany(
        "INSERT INTO api_calls (page_url, method, url, status, response_body) VALUES (?, ?, ?, ?, ?)",
        [(page_url, c.get("method"), c.get("url"), c.get("status"), c.get("response_body"))
         for c in api_calls],
    )


def store_parsed_page(conn: sqlite3.Connection, parsed: dict):
    """
    Persist one page's output from parser.parse_page() into the graph:
    page node, its form/field/button/link/table elements, outgoing
    navigation edges, and captured API calls.
    """
    page_url = parsed["url"]

    for form in parsed.get("forms", []):
        form_id = add_element(conn, page_url, "form", form)
        for field in form.get("fields", []):
            add_element(conn, page_url, "field", field, parent_id=form_id)
        for button in form.get("buttons", []):
            add_element(conn, page_url, "button", button, parent_id=form_id)

    # Angular reactive forms bind to [formGroup] divs, not <form>, so these
    # page-level fields are usually the majority of a screen's real inputs.
    for field in parsed.get("fields_outside_forms", []):
        add_element(conn, page_url, "field", field)

    for button in parsed.get("standalone_buttons", []):
        add_element(conn, page_url, "button", button)

    for table in parsed.get("tables", []):
        add_element(conn, page_url, "table", table)

    # Each action-state (modal/panel) is stored whole, with its own nested
    # forms, so the generator can reason about "click Raise Query -> this
    # form appears" as a single unit.
    for state in parsed.get("states", []):
        add_element(conn, page_url, "state", state)

    # Navigation edges. The hrefs the parser extracts are RELATIVE
    # ("/riskNucleus/master/bucket") while discovered_links are ABSOLUTE
    # ("http://host/riskNucleus/master/bucket"), so comparing them directly
    # never matched and the graph ended up with zero edges — which in turn
    # meant zero workflow test cases. Resolve both sides to a normalized
    # absolute URL before comparing.
    def _norm(u: str) -> str:
        if not u:
            return ""
        absolute = urljoin(page_url, u).split("#")[0]
        return absolute.rstrip("/")

    known_targets = {_norm(u): urljoin(page_url, u).split("#")[0]
                     for u in parsed.get("discovered_links", [])}

    for link in parsed.get("links", []):
        link_id = add_element(conn, page_url, "link", link)
        raw = link.get("href") or link.get("router_link")
        if not raw or raw.startswith("javascript:") or raw.strip() == "#":
            continue
        key = _norm(raw)
        target = known_targets.get(key)
        if target and target.rstrip("/") != page_url.rstrip("/"):
            add_edge(conn, page_url, target, "navigates_to", via_element_id=link_id)

    add_api_calls(conn, page_url, parsed.get("api_calls", []))


def get_pages(db_path: Optional[str] = None) -> list[str]:
    with connect(db_path) as conn:
        return [r["url"] for r in conn.execute("SELECT url FROM pages ORDER BY url")]


def get_page_bundle(conn: sqlite3.Connection, url: str) -> dict:
    """
    Everything an LLM needs to reason about one page: its identity, its
    forms (with nested fields/buttons), any standalone buttons/tables/
    links, outgoing navigation edges, and the API calls it fired.
    """
    page = conn.execute("SELECT * FROM pages WHERE url = ?", (url,)).fetchone()
    if page is None:
        raise KeyError(f"No such page in knowledge store: {url}")

    elements = conn.execute(
        "SELECT * FROM elements WHERE page_url = ? ORDER BY id", (url,)
    ).fetchall()

    forms, standalone_buttons, tables, links = [], [], [], []
    page_fields, states = [], []
    fields_by_parent: dict[int, list[dict]] = {}
    buttons_by_parent: dict[int, list[dict]] = {}

    for el in elements:
        detail = json.loads(el["detail_json"])
        if el["element_type"] == "field" and el["parent_id"]:
            fields_by_parent.setdefault(el["parent_id"], []).append(detail)
        elif el["element_type"] == "button" and el["parent_id"]:
            buttons_by_parent.setdefault(el["parent_id"], []).append(detail)

    for el in elements:
        detail = json.loads(el["detail_json"])
        if el["element_type"] == "form":
            detail["fields"] = fields_by_parent.get(el["id"], [])
            detail["buttons"] = buttons_by_parent.get(el["id"], [])
            forms.append(detail)
        elif el["element_type"] == "button" and not el["parent_id"]:
            standalone_buttons.append(detail)
        elif el["element_type"] == "field" and not el["parent_id"]:
            page_fields.append(detail)
        elif el["element_type"] == "table":
            tables.append(detail)
        elif el["element_type"] == "link":
            links.append(detail)
        elif el["element_type"] == "state":
            states.append(detail)

    edges = conn.execute(
        "SELECT target_page, relation FROM edges WHERE source_page = ?", (url,)
    ).fetchall()
    api_calls = conn.execute(
        "SELECT DISTINCT method, url, status, response_body FROM api_calls WHERE page_url = ?", (url,)
    ).fetchall()

    return {
        "url": page["url"],
        "title": page["title"],
        "forms": forms,
        "fields_outside_forms": page_fields,
        "standalone_buttons": standalone_buttons,
        "tables": tables,
        "links": links,
        "states": states,
        "outgoing_edges": [dict(e) for e in edges],
        "api_calls": [dict(a) for a in api_calls],
    }


def get_all_page_bundles(db_path: Optional[str] = None) -> list[dict]:
    with connect(db_path) as conn:
        urls = [r["url"] for r in conn.execute("SELECT url FROM pages ORDER BY url")]
        return [get_page_bundle(conn, u) for u in urls]


# --------------------------------------------------------------------------
# FSD side
# --------------------------------------------------------------------------

def _dumps(v) -> str:
    return json.dumps(v or [])


def clear_fsd(conn: sqlite3.Connection):
    """Re-ingesting an FSD replaces it wholesale — partial merges across runs
    would leave orphaned steps whose numbering no longer matches."""
    for table in ("fsd_links", "fsd_rules", "fsd_steps", "fsd_processes",
                  "fsd_field_specs"):
        conn.execute(f"DELETE FROM {table}")


def clear_field_specs(conn: sqlite3.Connection):
    """Only the data dictionary — used by --specs-only, which must not destroy
    processes extracted by an earlier full run."""
    conn.execute("DELETE FROM fsd_field_specs")
    conn.execute("DELETE FROM fsd_spec_links")


def store_field_spec_group(conn: sqlite3.Connection, group: dict):
    conn.executemany(
        "INSERT INTO fsd_field_specs (section, screen_hint, sub_menu, name, type, mandatory, "
        "values_format, allowed_values) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [(group["section"], group.get("screen_hint"), f.get("sub_menu"), f["name"],
          f.get("type"), f.get("mandatory"), f.get("values_format"),
          _dumps(f.get("allowed_values")))
         for f in group.get("fields", [])],
    )


def get_field_spec_sections(conn: sqlite3.Connection) -> list[dict]:
    """One row per FSD section that defines fields, with its field list."""
    rows = conn.execute(
        "SELECT section, screen_hint, sub_menu, name, type, mandatory, values_format, allowed_values "
        "FROM fsd_field_specs ORDER BY section, id").fetchall()
    grouped: dict[str, dict] = {}
    for r in rows:
        g = grouped.setdefault(r["section"], {
            "section": r["section"], "screen_hint": r["screen_hint"], "fields": []})
        field = {
            "name": r["name"], "type": r["type"], "mandatory": r["mandatory"],
            "values_format": r["values_format"],
            "allowed_values": json.loads(r["allowed_values"] or "[]"),
        }
        if r["sub_menu"]:
            field["sub_menu"] = r["sub_menu"]
        g["fields"].append(field)
    return list(grouped.values())


def field_specs_for_sections(conn: sqlite3.Connection, sections: Iterable[str]) -> list[dict]:
    sections = [s for s in sections if s]
    if not sections:
        return []
    marks = ",".join("?" * len(sections))
    rows = conn.execute(
        f"SELECT section, screen_hint, sub_menu, name, type, mandatory, values_format, "
        f"allowed_values FROM fsd_field_specs WHERE section IN ({marks}) ORDER BY section, id",
        sections
    ).fetchall()
    grouped: dict[str, dict] = {}
    for r in rows:
        g = grouped.setdefault(r["section"], {
            "section": r["section"], "screen_hint": r["screen_hint"], "fields": []})
        field = {
            "name": r["name"], "type": r["type"], "mandatory": r["mandatory"],
            "values_format": r["values_format"],
            "allowed_values": json.loads(r["allowed_values"] or "[]"),
        }
        if r["sub_menu"]:
            field["sub_menu"] = r["sub_menu"]
        g["fields"].append(field)
    return list(grouped.values())


def store_fsd_process(conn: sqlite3.Connection, p: dict):
    conn.execute(
        "INSERT OR REPLACE INTO fsd_processes "
        "(process_id, name, module, actors, preconditions, outcomes, alternate_flows, source_section) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (p["process_id"], p.get("name"), p.get("module"), _dumps(p.get("actors")),
         _dumps(p.get("preconditions")), _dumps(p.get("outcomes")),
         _dumps(p.get("alternate_flows")), p.get("source_section")),
    )
    for i, step in enumerate(p.get("steps") or [], start=1):
        conn.execute(
            "INSERT INTO fsd_steps (process_id, seq, actor, action, screen_hint, expected, data_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (p["process_id"], step.get("seq", i), step.get("actor"), step.get("action"),
             step.get("screen_hint"), step.get("expected"),
             json.dumps(step.get("data") or {})),
        )
    for rule in p.get("business_rules") or []:
        conn.execute(
            "INSERT INTO fsd_rules (process_id, kind, field, rule, applies_to, "
            "valid_examples, invalid_examples) VALUES (?, 'business_rule', NULL, ?, ?, '[]', '[]')",
            (p["process_id"], rule.get("rule"), rule.get("applies_to")),
        )
    for val in p.get("validations") or []:
        conn.execute(
            "INSERT INTO fsd_rules (process_id, kind, field, rule, applies_to, "
            "valid_examples, invalid_examples) VALUES (?, 'validation', ?, ?, NULL, ?, ?)",
            (p["process_id"], val.get("field"), val.get("rule"),
             _dumps(val.get("valid_examples")), _dumps(val.get("invalid_examples"))),
        )


def has_fsd(db_path: Optional[str] = None) -> bool:
    with connect(db_path) as conn:
        try:
            return conn.execute("SELECT COUNT(*) c FROM fsd_processes").fetchone()["c"] > 0
        except sqlite3.OperationalError:
            return False


def get_process_ids(db_path: Optional[str] = None) -> list[str]:
    with connect(db_path) as conn:
        return [r["process_id"] for r in
                conn.execute("SELECT process_id FROM fsd_processes ORDER BY module, name")]


def get_process_bundle(conn: sqlite3.Connection, process_id: str) -> dict:
    """One process with its steps, rules, and whatever screens its steps were
    grounded to — the unit the workflow generator works from."""
    p = conn.execute("SELECT * FROM fsd_processes WHERE process_id = ?", (process_id,)).fetchone()
    if p is None:
        raise KeyError(f"No such process: {process_id}")

    steps = []
    for s in conn.execute("SELECT * FROM fsd_steps WHERE process_id = ? ORDER BY seq, id",
                          (process_id,)):
        links = [dict(l) for l in conn.execute(
            "SELECT page_url, state_trigger, score, evidence FROM fsd_links "
            "WHERE step_id = ? ORDER BY score DESC", (s["id"],))]
        steps.append({
            "id": s["id"], "seq": s["seq"], "actor": s["actor"], "action": s["action"],
            "screen_hint": s["screen_hint"], "expected": s["expected"],
            "data": json.loads(s["data_json"] or "{}"),
            "grounded_to": links,
        })

    rules, validations = [], []
    for r in conn.execute("SELECT * FROM fsd_rules WHERE process_id = ?", (process_id,)):
        if r["kind"] == "business_rule":
            rules.append({"rule": r["rule"], "applies_to": r["applies_to"]})
        else:
            validations.append({
                "field": r["field"], "rule": r["rule"],
                "valid_examples": json.loads(r["valid_examples"] or "[]"),
                "invalid_examples": json.loads(r["invalid_examples"] or "[]"),
            })

    return {
        "process_id": p["process_id"], "name": p["name"], "module": p["module"],
        "actors": json.loads(p["actors"] or "[]"),
        "preconditions": json.loads(p["preconditions"] or "[]"),
        "outcomes": json.loads(p["outcomes"] or "[]"),
        "alternate_flows": json.loads(p["alternate_flows"] or "[]"),
        "source_section": p["source_section"],
        "steps": steps, "business_rules": rules, "validations": validations,
    }


def get_all_steps(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT s.*, p.name AS process_name, p.module AS module "
        "FROM fsd_steps s JOIN fsd_processes p ON p.process_id = s.process_id "
        "ORDER BY s.process_id, s.seq, s.id")]


def clear_links(conn: sqlite3.Connection):
    conn.execute("DELETE FROM fsd_links")
    conn.execute("DELETE FROM fsd_spec_links")


def add_spec_link(conn: sqlite3.Connection, section: str, page_url: Optional[str],
                  score: float, evidence: str):
    conn.execute(
        "INSERT INTO fsd_spec_links (section, page_url, score, evidence) VALUES (?, ?, ?, ?)",
        (section, page_url, score, evidence),
    )


def add_fsd_link(conn: sqlite3.Connection, step_id: int, process_id: str,
                 page_url: Optional[str], state_trigger: Optional[str],
                 score: float, evidence: str):
    conn.execute(
        "INSERT INTO fsd_links (step_id, process_id, page_url, state_trigger, score, evidence) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (step_id, process_id, page_url, state_trigger, score, evidence),
    )


def rules_for_page(conn: sqlite3.Connection, page_url: str,
                   threshold: float = 0.25) -> dict:
    """
    Everything the FSD says about this screen: the business rules and
    validations of processes grounded here, plus the data-dictionary fields
    for it. This is what turns a page-level case from "enter text in the
    search field" into "reject an obligor code longer than 12 characters"
    and "Nature of Account accepts only Current, Savings or Fixed Deposit".
    """
    spec_sections = [r["section"] for r in conn.execute(
        "SELECT DISTINCT section FROM fsd_spec_links WHERE page_url = ? AND score >= ?",
        (page_url, threshold))]
    field_specs = field_specs_for_sections(conn, spec_sections)

    proc_ids = [r["process_id"] for r in conn.execute(
        "SELECT DISTINCT process_id FROM fsd_links WHERE page_url = ? AND score >= ?",
        (page_url, threshold))]
    if not proc_ids:
        return {"processes": [], "business_rules": [], "validations": [],
                "field_specs": field_specs}

    marks = ",".join("?" * len(proc_ids))
    names = [dict(r) for r in conn.execute(
        f"SELECT process_id, name, module FROM fsd_processes WHERE process_id IN ({marks})",
        proc_ids)]
    rules, validations = [], []
    for r in conn.execute(f"SELECT * FROM fsd_rules WHERE process_id IN ({marks})", proc_ids):
        if r["kind"] == "business_rule":
            rules.append({"rule": r["rule"], "applies_to": r["applies_to"]})
        else:
            validations.append({
                "field": r["field"], "rule": r["rule"],
                "valid_examples": json.loads(r["valid_examples"] or "[]"),
                "invalid_examples": json.loads(r["invalid_examples"] or "[]"),
            })
    return {"processes": names, "business_rules": rules, "validations": validations,
            "field_specs": field_specs}


def coverage_report(conn: sqlite3.Connection, threshold: float) -> dict:
    """Gaps in both directions — the most actionable output of the join."""
    ungrounded = [dict(r) for r in conn.execute(
        "SELECT s.id, s.process_id, p.name AS process_name, s.seq, s.actor, s.action, "
        "       s.screen_hint, "
        "       (SELECT MAX(score) FROM fsd_links l WHERE l.step_id = s.id) AS best_score "
        "FROM fsd_steps s JOIN fsd_processes p ON p.process_id = s.process_id "
        "WHERE COALESCE(best_score, 0) < ? ORDER BY s.process_id, s.seq", (threshold,))]

    unspecified = [dict(r) for r in conn.execute(
        "SELECT url FROM pages WHERE url NOT IN "
        "(SELECT DISTINCT page_url FROM fsd_links WHERE page_url IS NOT NULL AND score >= ?) "
        "ORDER BY url", (threshold,))]

    return {"ungrounded_steps": ungrounded, "screens_without_fsd": unspecified}