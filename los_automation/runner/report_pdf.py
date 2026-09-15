"""
Turn a finished run into a PDF a person can send to somebody.

Why this exists: everything a run produces already lives in the artifacts
directory — result.json and a few dozen PNGs — but that is a folder, not a
document. What gets forwarded to a colleague, attached to a defect, or filed as
evidence that a release was exercised is one file with the screenshots and the
findings in the order they happened. This builds that file.

How, and why this way:

  - The report is authored as HTML and printed by the SAME headless Chromium
    Playwright already installs for the runs themselves. That is the whole
    reason there is no PDF library in requirements.txt: adding reportlab or
    weasyprint to draw boxes by hand, when a browser that lays out documents
    beautifully is already a hard dependency, would be work in exchange for a
    worse-looking report.
  - Screenshots are inlined as data URIs rather than linked, so the PDF and its
    intermediate HTML are both self-contained. They are downscaled first where
    Pillow is available: a run leaves fifty full-page 1600px PNGs behind, and
    embedding them untouched makes a 40MB file nobody can email.
  - It runs in the RUNNER's process, never in Streamlit's. Playwright's sync
    API refuses to start inside a thread with a running asyncio event loop, and
    Streamlit's runtime is built on one — the same reason runs are subprocesses.
    The web page asks for a PDF by starting a subprocess, exactly as it starts a
    run.

Nothing here touches the application under test. It reads files off disk and
renders them.
"""
from __future__ import annotations

import base64
import html
import io
import json
import os
from datetime import datetime
from typing import Optional

from . import results as R

PDF_NAME = "report.pdf"
HTML_NAME = "report.html"

# Wide enough to stay readable printed on A4, small enough that fifty of them
# do not make a file too big to send. A full-page capture of this app is around
# 1600x2400; at 1000px wide the field labels are still legible.
_MAX_WIDTH = 1000
_JPEG_QUALITY = 72

_WORD = {R.PASS: "Passed", R.FAIL: "Failed", R.ERROR: "Could not run"}
_ORDER = {R.FAIL: 0, R.PASS: 1}


# --------------------------------------------------------------------------
# Images
# --------------------------------------------------------------------------

def _data_uri(path: str) -> str:
    """A screenshot as an inline image, shrunk if that is possible here."""
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError:
        return ""
    try:
        from PIL import Image           # optional; see the module docstring
        img = Image.open(io.BytesIO(raw))
        if img.width > _MAX_WIDTH:
            height = round(img.height * _MAX_WIDTH / img.width)
            img = img.resize((_MAX_WIDTH, height), Image.LANCZOS)
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="JPEG", quality=_JPEG_QUALITY,
                                optimize=True)
        return ("data:image/jpeg;base64,"
                + base64.b64encode(buf.getvalue()).decode("ascii"))
    except Exception:  # noqa: BLE001 - no Pillow, or an image it cannot read
        return ("data:image/png;base64,"
                + base64.b64encode(raw).decode("ascii"))


def _shots(artifacts_dir: str) -> list[str]:
    """Every screenshot in the run's folder, in the order it was taken.

    Sorted by modification time rather than by name: the names are descriptive
    (`tab-BBFS-Details-filled`), which reads well in the report but sorts into
    an order that has nothing to do with what happened when.
    """
    try:
        names = [n for n in os.listdir(artifacts_dir)
                 if n.lower().endswith((".png", ".jpg", ".jpeg"))]
    except OSError:
        return []
    paths = [os.path.join(artifacts_dir, n) for n in names]
    paths.sort(key=lambda p: (os.path.getmtime(p), p))
    return paths


def _caption(path: str) -> str:
    """The file's stem, read as a sentence rather than as a filename."""
    stem = os.path.splitext(os.path.basename(path))[0]
    stem = stem.replace("_", " ").replace("-", " ")
    return stem[:1].upper() + stem[1:]


# --------------------------------------------------------------------------
# HTML
# --------------------------------------------------------------------------

_CSS = """
@page { size: A4; margin: 14mm 12mm; }
* { box-sizing: border-box; }
body { font: 11px/1.5 "Segoe UI", system-ui, sans-serif; color: #1a1d21;
       margin: 0; }
h1 { font-size: 20px; margin: 0 0 2px; }
h2 { font-size: 15px; margin: 22px 0 8px; padding-bottom: 4px;
     border-bottom: 2px solid #e3e6ea; page-break-after: avoid; }
h3 { font-size: 12px; margin: 16px 0 6px; page-break-after: avoid; }
.sub { color: #5b6470; margin: 0 0 14px; }
.tiles { display: flex; gap: 8px; margin: 12px 0 4px; }
.tile { flex: 1; border: 1px solid #e3e6ea; border-radius: 6px; padding: 8px 10px; }
.tile .n { font-size: 20px; font-weight: 600; display: block; }
.tile .l { color: #5b6470; font-size: 10px; text-transform: uppercase;
           letter-spacing: .04em; }
.banner { border-radius: 6px; padding: 10px 12px; margin: 10px 0 6px;
          font-weight: 600; }
.PASS { background: #e8f5ec; color: #14612c; }
.FAIL { background: #fdeceb; color: #8f1d16; }
.ERROR { background: #fdf3e5; color: #8a4b06; }
.NOTE { background: #eef2f7; color: #33415c; }
table { width: 100%; border-collapse: collapse; margin: 6px 0 12px; }
th, td { border: 1px solid #e3e6ea; padding: 4px 6px; text-align: left;
         vertical-align: top; font-size: 10px; word-break: break-word; }
th { background: #f5f7f9; font-weight: 600; }
.check { border-left: 3px solid #d7dbe0; padding: 6px 0 6px 10px;
         margin: 0 0 10px; page-break-inside: avoid; }
.check.FAIL { border-left-color: #b42318; }
.check.PASS { border-left-color: #1a7f37; }
.check.ERROR { border-left-color: #b54708; }
.check.NOTE { border-left-color: #7a8699; }
.check .name { font-weight: 600; }
.check .row { color: #333a42; margin: 2px 0; }
.check .note { color: #5b6470; font-style: italic; margin-top: 3px; }
.k { color: #5b6470; }
figure { margin: 0 0 14px; page-break-inside: avoid; }
figure img { width: 100%; border: 1px solid #d7dbe0; border-radius: 4px; }
figcaption { color: #5b6470; font-size: 10px; margin-top: 3px; }
.foot { margin-top: 24px; padding-top: 8px; border-top: 1px solid #e3e6ea;
        color: #5b6470; font-size: 10px; }
.empty { color: #5b6470; font-style: italic; }
"""


def _esc(text) -> str:
    return html.escape(str(text or ""))


def _tiles(counts: dict, notes: int = 0) -> str:
    cells = [("Passed", counts.get(R.PASS, 0)),
             ("Failed", counts.get(R.FAIL, 0))]
    if notes:
        cells.append(("Observations", notes))
    return ('<div class="tiles">' + "".join(
        f'<div class="tile"><span class="n">{n}</span>'
        f'<span class="l">{_esc(label)}</span></div>'
        for label, n in cells) + "</div>")


def _table(headers: list[str], rows: list[list]) -> str:
    if not rows:
        return '<p class="empty">Nothing recorded.</p>'
    head = "".join(f"<th>{_esc(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{_esc(c)}</td>" for c in r) + "</tr>"
                   for r in rows)
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def _figure(path: str, uri: str, caption: str = "") -> str:
    if not uri:
        return ""
    return (f'<figure><img src="{uri}">'
            f'<figcaption>{_esc(caption or _caption(path))}</figcaption>'
            f'</figure>')


def build_html(result: dict, artifacts_dir: str) -> str:
    """The report as one self-contained HTML document."""
    checks = sorted(result.get("checks") or [],
                    key=lambda c: (_ORDER.get(c.get("status"), 9),
                                   c.get("name", "")))
    counts = {R.PASS: 0, R.FAIL: 0}
    for c in checks:
        counts[c.get("status")] = counts.get(c.get("status"), 0) + 1
    notes = result.get("notes") or []
    overall = result.get("overall", R.ERROR)

    # Every screenshot in the folder, encoded once. Evidence images are shown
    # against the check that cites them AND left out of the appendix, so the
    # same picture is never printed twice.
    all_shots = _shots(artifacts_dir)
    uris = {p: _data_uri(p) for p in all_shots}
    cited: set[str] = set()
    for c in checks:
        for p in c.get("evidence") or []:
            real = os.path.abspath(p) if p else ""
            for known in all_shots:
                if os.path.abspath(known) == real:
                    cited.add(known)

    parts: list[str] = []
    title = result.get("target_title") or result.get("target_key") or "Run"
    parts.append(f"<h1>{_esc(title)}</h1>")

    when = result.get("finished_at") or result.get("started_at") or ""
    bits = [f"Run <b>{_esc(result.get('run_id', ''))}</b>"]
    if result.get("case_id"):
        bits.append(f"record <b>{_esc(result['case_id'])}</b>")
    if result.get("base_url"):
        bits.append(_esc(result["base_url"]))
    if when:
        bits.append(_esc(when))
    parts.append('<p class="sub">' + " &middot; ".join(bits) + "</p>")

    parts.append(f'<div class="banner {_esc(overall)}">'
                 f'{_esc(_WORD.get(overall, overall))} — '
                 f'{_esc(result.get("headline", ""))}</div>')
    if result.get("dry_run"):
        parts.append('<div class="banner NOTE">Dry run — every field was '
                     'filled and then abandoned. Nothing was written.</div>')
    parts.append(_tiles(counts, len(notes)))
    if result.get("error_reason"):
        parts.append(f'<p class="check ERROR">'
                     f'<span class="name">Could not run</span><br>'
                     f'{_esc(result["error_reason"])}</p>')

    # ---- what the run produced ----------------------------------------
    made = [(k, result.get(v)) for k, v in
            (("Obligor", "obligor_name"), ("Customer ID", "customer_id"),
             ("Request / case ID", "request_id"), ("Run marker", "marker"),
             ("Facility requested", "facility_ref"))
            if result.get(v)]
    if made:
        parts.append("<h2>What this run produced</h2>")
        parts.append(_table(["", ""], [[k, v] for k, v in made]))

    # ---- per screen ----------------------------------------------------
    by_screen: dict[str, list[dict]] = {}
    for c in checks:
        by_screen.setdefault(c.get("screen") or "(record)", []).append(c)
    if len(by_screen) > 1:
        parts.append("<h2>Screens checked</h2>")
        rows = []
        for screen, items in by_screen.items():
            cnt = {R.PASS: 0, R.FAIL: 0}
            for c in items:
                cnt[c.get("status")] = cnt.get(c.get("status"), 0) + 1
            worst = R.FAIL if cnt[R.FAIL] else R.PASS
            rows.append([screen, _WORD.get(worst, worst), cnt[R.PASS],
                         cnt[R.FAIL]])
        parts.append(_table(["Screen", "Result", "Passed", "Failed"],
                            rows))

    # ---- how it got there ----------------------------------------------
    steps = result.get("steps") or []
    if steps:
        parts.append("<h2>What it did, in order</h2>")
        parts.append(_table(
            ["#", "Step", "Result", "Note"],
            [[s.get("index", ""), s.get("label") or s.get("text", ""),
              _WORD.get(s.get("status"), s.get("status", "")),
              str(s.get("note", ""))[:220]] for s in steps]))

    # ---- what it entered -------------------------------------------------
    entries = result.get("entries") or []
    if entries:
        parts.append(f"<h2>What it entered ({len(entries)} fields)</h2>")
        parts.append(_table(
            ["Screen", "Field", "Value", "Control"],
            [[e.get("group") or e.get("screen", ""), e.get("label", ""),
              str(e.get("value", ""))[:300], e.get("kind", "")]
             for e in entries]))

    # ---- the checks themselves ------------------------------------------
    parts.append("<h2>What was checked</h2>")
    for screen, items in by_screen.items():
        failed = sum(1 for c in items if c.get("status") == R.FAIL)
        parts.append(f"<h3>{_esc(screen)} — {len(items)} checks, "
                     f"{failed} failed</h3>")
        for c in items:
            block = [f'<div class="check {_esc(c.get("status"))}">',
                     f'<div class="name">{_esc(_WORD.get(c.get("status"), ""))}'
                     f' &middot; {_esc(c.get("name"))}</div>']
            if c.get("expected"):
                block.append(f'<div class="row"><span class="k">Should be:'
                             f'</span> {_esc(c["expected"])}</div>')
            if c.get("actual"):
                block.append(f'<div class="row"><span class="k">Actually:'
                             f'</span> {_esc(c["actual"])}</div>')
            if c.get("detail"):
                block.append(f'<div class="note">{_esc(c["detail"])}</div>')
            for p in c.get("evidence") or []:
                match = next((k for k in all_shots
                              if os.path.abspath(k) == os.path.abspath(p or "")),
                             "")
                if match:
                    block.append(_figure(match, uris.get(match, ""),
                                         f"Evidence — {_caption(match)}"))
            block.append("</div>")
            parts.append("".join(block))

    # ---- observations ----------------------------------------------------
    #
    # Deliberately NOT checks and deliberately not counted. These are the
    # things the run could not make an assertion about — a field the screen
    # does not have under the name this suite knows it by, a pass left blank
    # on purpose, a dry run. They used to be a third "could not check" status,
    # which let a report be entirely amber while saying nothing was wrong.
    if notes:
        parts.append(f"<h2>Observations ({len(notes)})</h2>")
        parts.append('<p class="sub">Things worth knowing that are not '
                     'verdicts on the application. Nothing here counts '
                     'towards passed or failed.</p>')
        for n in notes:
            block = ['<div class="check NOTE">']
            if n.get("screen"):
                block.append(f'<div class="name">{_esc(n["screen"])}</div>')
            block.append(f'<div class="row">{_esc(n.get("text", ""))}</div>')
            for path in n.get("evidence") or []:
                match = next((k for k in all_shots
                              if os.path.abspath(k) == os.path.abspath(path or "")),
                             "")
                if match:
                    cited.add(match)
                    block.append(_figure(match, uris.get(match, ""),
                                         f"Observed — {_caption(match)}"))
            block.append("</div>")
            parts.append("".join(block))

    # ---- every other screenshot ------------------------------------------
    rest = [p for p in all_shots if p not in cited]
    if rest:
        parts.append(f"<h2>Screenshots ({len(rest)})</h2>")
        parts.append('<p class="sub">Every other frame the run captured, in '
                     'the order it was taken.</p>')
        for p in rest:
            parts.append(_figure(p, uris.get(p, "")))

    parts.append(f'<div class="foot">Generated '
                 f'{datetime.now().strftime("%d %b %Y %H:%M")} from '
                 f'{_esc(artifacts_dir)} by the LOS Automation Portal.</div>')

    return ("<!doctype html><html><head><meta charset=\"utf-8\">"
            f"<title>{_esc(title)}</title><style>{_CSS}</style></head>"
            f"<body>{''.join(parts)}</body></html>")


# --------------------------------------------------------------------------
# PDF
# --------------------------------------------------------------------------

def build(artifacts_dir: str, result: Optional[dict] = None) -> str:
    """
    Write report.html and report.pdf into the run's folder, and return the PDF
    path — or "" if a browser could not be started, in which case the HTML is
    still there and is still a complete report.
    """
    artifacts_dir = os.path.abspath(artifacts_dir)
    if result is None:
        with open(os.path.join(artifacts_dir, "result.json"), "r",
                  encoding="utf-8") as fh:
            result = json.load(fh)

    html_path = os.path.join(artifacts_dir, HTML_NAME)
    with open(html_path, "w", encoding="utf-8") as fh:
        fh.write(build_html(result, artifacts_dir))

    pdf_path = os.path.join(artifacts_dir, PDF_NAME)
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.goto("file:///" + html_path.replace("\\", "/"),
                          wait_until="load", timeout=120000)
                page.emulate_media(media="print")
                page.pdf(path=pdf_path, format="A4", print_background=True,
                         margin={"top": "14mm", "bottom": "14mm",
                                 "left": "12mm", "right": "12mm"})
            finally:
                browser.close()
    except Exception:  # noqa: BLE001 - no browser is a missing extra, not a
        # failed run. The HTML above is the same report and is already written.
        return ""
    return pdf_path
