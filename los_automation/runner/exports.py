"""
Turning a finished run into something you can send to somebody.

The portal already shows a run on screen. This module makes the same run
downloadable, and it exists as its own file for one reason: there must be ONE
description of what a result contains. The checks table, the entered-values
table, the CSVs and the PDF are all built from the row builders here, so a
column added for the screen is a column the exports gain too — rather than
three renderings of the same run that drift apart.

Read-only throughout. Nothing here touches a browser or the application; it
reads the `result.json` a run has already written.

    checks_csv(result)      what was checked, one row per check
    entries_csv(result)     what was entered, one row per field
    build_pdf(result)       the whole report as a PDF

The PDF needs reportlab. It is imported lazily and its absence is reported as
a message rather than an exception, so a portal on a machine without it keeps
working and says why the button is unavailable — the same way the rest of the
portal explains a thing it cannot do instead of failing.
"""
from __future__ import annotations

import html
import io
import os
import re
from typing import Optional

import pandas as pd

from . import results as R

WORD = {R.PASS: "Passed", R.FAIL: "Failed", R.BLOCKED: "Could not check"}

# The kinds widgets.py records for an editor rather than a plain box. Their
# values need unpicking before they read properly in a cell — see plain_text.
RICH_KINDS = ("rich-text", "textarea")


# --------------------------------------------------------------------------
# Rich text
#
# Proceeding Details on the Litigation screen is a TinyMCE editor, and the
# obligor's narrative tabs are nineteen more. What comes back from an editor is
# not a plain string: read from the page it arrives as text broken by newlines,
# and read from an API — or pasted into one by a person — it can carry markup.
# Neither belongs in a CSV cell, where a raw newline splits the row in two and
# a <p> reads as noise.
#
# So: markup is unwound rather than deleted. A block tag becomes a paragraph
# break and <br> a line break, which is the structure a person put there, and
# that structure is what the PDF then lays out.
# --------------------------------------------------------------------------

_BLOCK = re.compile(
    r"</\s*(p|div|li|tr|h[1-6]|blockquote|pre|section|article)\s*>|"
    r"<\s*(p|div|li|tr|h[1-6]|blockquote|pre|section|article)\b[^>]*>",
    re.I)
_BREAK = re.compile(r"<\s*br\s*/?\s*>", re.I)
_TAG = re.compile(r"<[^>]+>")


def rich_paragraphs(value: str) -> list[str]:
    """A rich-text value as the paragraphs it was written in."""
    if value is None:
        return []
    text = str(value)
    if "<" in text and ">" in text:
        text = _BREAK.sub("\n", text)
        text = _BLOCK.sub("\n\n", text)
        text = _TAG.sub("", text)
    text = html.unescape(text)
    # A non-breaking space is what &nbsp; unescapes to, and it is not a space
    # to strip() — an editor's empty paragraph would survive as content.
    text = (text.replace("\xa0", " ")
                .replace("\r\n", "\n").replace("\r", "\n"))
    parts = re.split(r"\n{2,}", text)
    out = []
    for part in parts:
        # Single newlines are kept as line breaks inside one paragraph; only
        # the blank line between them starts a new one.
        cleaned = "\n".join(ln.strip() for ln in part.split("\n"))
        cleaned = re.sub(r"[ \t]{2,}", " ", cleaned).strip()
        if cleaned:
            out.append(cleaned)
    return out


def plain_text(value: str) -> str:
    """
    One line of readable text for a value, whatever kind of box it came from.

    A CSV cell cannot hold the structure, so paragraphs are joined with a
    marker a reader can see rather than being run together into one sentence
    that says something the author did not write.
    """
    paras = rich_paragraphs(value)
    return " ¶ ".join(p.replace("\n", " ") for p in paras)


# --------------------------------------------------------------------------
# Rows — the one description of what a result contains
# --------------------------------------------------------------------------

def sorted_checks(result: dict) -> list[dict]:
    """
    Failures first, then what could not be checked — R.ORDER, the same order
    the portal sorts by on screen, so a printed report and the page it came
    from read the same way round.
    """
    return sorted((result or {}).get("checks", []),
                  key=lambda c: (R.ORDER.get(c.get("status"), 9),
                                 c.get("name", "")))


def check_rows(result: dict) -> list[dict]:
    """One row per check. The shape the portal has always exported."""
    return [{"Screen": c.get("screen", ""),
             "Check": c.get("name", ""),
             "Result": c.get("status", ""),
             "Should be": c.get("expected", ""),
             "Actually": c.get("actual", ""),
             "Notes": c.get("detail", "")}
            for c in sorted_checks(result)]


def entry_rows(result: dict) -> list[dict]:
    """
    One row per field this run entered, in the order it entered them.

    Order is deliberate: it is the order the form asks for the values, so a
    Litigation row reads Type of Suit, Date Of Suit Filing, Relevant Court and
    on down the form — which is how somebody checking the export against the
    screen will read it.
    """
    rows = []
    for e in (result or {}).get("entries", []) or []:
        kind = e.get("kind", "")
        value = e.get("value", "")
        rows.append({
            "Screen": e.get("screen", ""),
            "Section": e.get("group") or e.get("screen", ""),
            "Field": e.get("label", ""),
            "Value": plain_text(value) if kind in RICH_KINDS else str(value),
            "Kind": kind,
        })
    return rows


def screen_rows(result: dict) -> list[dict]:
    """Per-screen totals, for the summary at the top of the report."""
    by_screen: dict[str, dict] = {}
    for c in (result or {}).get("checks", []):
        name = c.get("screen") or "(record)"
        cnt = by_screen.setdefault(
            name, {R.PASS: 0, R.FAIL: 0, R.BLOCKED: 0})
        cnt[c.get("status")] = cnt.get(c.get("status"), 0) + 1
    out = []
    for name, cnt in by_screen.items():
        worst = (R.FAIL if cnt[R.FAIL] else
                 (R.PASS if cnt[R.PASS] else R.BLOCKED))
        out.append({"Screen": name, "Passed": cnt[R.PASS],
                    "Failed": cnt[R.FAIL],
                    "Could not check": cnt[R.BLOCKED], "Result": worst})
    return out


def counts(result: dict) -> dict:
    out = {R.PASS: 0, R.FAIL: 0, R.BLOCKED: 0}
    for c in (result or {}).get("checks", []):
        out[c.get("status")] = out.get(c.get("status"), 0) + 1
    return out


# --------------------------------------------------------------------------
# CSV
# --------------------------------------------------------------------------

def to_csv(rows: list[dict]) -> str:
    """pandas, as the portal has always used, so the output is unchanged."""
    return pd.DataFrame(rows).to_csv(index=False)


def checks_csv(result: dict) -> str:
    return to_csv(check_rows(result))


def entries_csv(result: dict) -> str:
    return to_csv(entry_rows(result))


def base_name(result: dict) -> str:
    return (result or {}).get("run_id") or "run"


# --------------------------------------------------------------------------
# PDF
# --------------------------------------------------------------------------

PDF_HINT = ("PDF export needs the 'reportlab' package. Install it with "
            "`pip install reportlab` (it is in los_automation/"
            "requirements.txt) and reload this page.")


def pdf_available() -> bool:
    try:
        import reportlab  # noqa: F401
    except Exception:  # noqa: BLE001 - any import problem means no PDF
        return False
    return True


def build_pdf(result: dict) -> bytes:
    """
    The whole run as a PDF: what it ran against, what it entered, what it
    checked, and what it found.

    Everything a cell holds goes in as a Paragraph so it WRAPS. A table of raw
    strings in reportlab does not wrap — it overflows the column and the text
    disappears off the page, which on a report about missing values would be
    its own small joke.
    """
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import (KeepTogether, PageBreak, Paragraph,
                                    SimpleDocTemplate, Spacer, Table,
                                    TableStyle)

    brand = colors.HexColor("#0f5132")       # a bank's dark green
    rule = colors.HexColor("#c9ccd1")
    band = colors.HexColor("#f2f4f6")
    ink = colors.HexColor("#1f2328")
    quiet = colors.HexColor("#5b6472")
    tone = {R.PASS: colors.HexColor("#1a7f37"),
            R.FAIL: colors.HexColor("#b42318"),
            R.BLOCKED: colors.HexColor("#b54708")}

    base = ParagraphStyle("base", fontName="Helvetica", fontSize=8.5,
                          leading=11, textColor=ink, alignment=TA_LEFT)
    small = ParagraphStyle("small", parent=base, fontSize=7.5, leading=9.5,
                           textColor=quiet)
    head = ParagraphStyle("head", parent=base, fontName="Helvetica-Bold",
                          fontSize=8, leading=10,
                          textColor=colors.white)
    bold = ParagraphStyle("bold", parent=base, fontName="Helvetica-Bold")
    title = ParagraphStyle("title", parent=base, fontName="Helvetica-Bold",
                           fontSize=16, leading=20, textColor=brand)
    subtitle = ParagraphStyle("subtitle", parent=base, fontSize=10,
                              leading=13, textColor=quiet)
    section = ParagraphStyle("section", parent=base,
                             fontName="Helvetica-Bold", fontSize=11,
                             leading=14, textColor=brand, spaceBefore=6,
                             spaceAfter=4)

    def cell(text, style=base) -> Paragraph:
        return Paragraph(html.escape(str(text or "")).replace("\n", "<br/>"),
                         style)

    def status_cell(status: str) -> Paragraph:
        st = ParagraphStyle(f"s{status}", parent=base,
                            fontName="Helvetica-Bold",
                            textColor=tone.get(status, ink))
        return Paragraph(html.escape(WORD.get(status, status or "—")), st)

    def grid(data, widths, align_top=True) -> Table:
        t = Table(data, colWidths=widths, repeatRows=1, hAlign="LEFT")
        style = [
            ("BACKGROUND", (0, 0), (-1, 0), brand),
            ("GRID", (0, 0), (-1, -1), 0.4, rule),
            ("VALIGN", (0, 0), (-1, -1), "TOP" if align_top else "MIDDLE"),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]
        for i in range(2, len(data), 2):
            style.append(("BACKGROUND", (0, i), (-1, i), band))
        t.setStyle(TableStyle(style))
        return t

    # ---- what this run was ---------------------------------------------
    overall = result.get("overall", R.BLOCKED)
    dry = bool(result.get("dry_run"))
    cnt = counts(result)
    total = 182 * mm

    story = [
        Paragraph("NATIONAL BANK OF PAKISTAN", small),
        Paragraph("Loan Origination System — Automation Report", title),
        Paragraph(html.escape(str(result.get("target_title", "")
                                  or result.get("target_key", ""))),
                  subtitle),
        Spacer(1, 4 * mm),
    ]

    meta = [
        ("Result", WORD.get(overall, overall)),
        ("Summary", result.get("headline", "")),
        ("Mode", "Dry run — every field was filled and then abandoned, "
                 "nothing was written" if dry else
                 "Live — values were saved and read back"),
        ("Record", result.get("case_id", "") or "—"),
        ("Screens", ", ".join(result.get("screens") or []) or "—"),
        ("Run marker", result.get("marker", "") or "—"),
        ("Run", result.get("run_id", "") or "—"),
        ("Environment", result.get("base_url", "") or "—"),
        ("Started", result.get("started_at", "") or "—"),
        ("Finished", result.get("finished_at", "") or "—"),
    ]
    if result.get("facility_ref"):
        meta.insert(5, ("Facility requested", result["facility_ref"]))
    if result.get("blocked_reason"):
        meta.append(("Could not check", result["blocked_reason"]))

    info = Table([[cell(k, bold), cell(v)] for k, v in meta],
                 colWidths=[38 * mm, total - 38 * mm], hAlign="LEFT")
    info.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LINEBELOW", (0, 0), (-1, -2), 0.3, rule),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING", (0, 0), (0, -1), 0),
    ]))
    story += [info, Spacer(1, 5 * mm)]

    tally = grid(
        [[cell("Passed", head), cell("Failed", head),
          cell("Could not check", head)],
         [cell(cnt[R.PASS]), cell(cnt[R.FAIL]), cell(cnt[R.BLOCKED])]],
        [total / 3.0] * 3, align_top=False)
    story += [tally, Spacer(1, 5 * mm)]

    per_screen = screen_rows(result)
    if len(per_screen) > 1:
        story.append(Paragraph("Screens covered", section))
        data = [[cell("Screen", head), cell("Result", head),
                 cell("Passed", head), cell("Failed", head),
                 cell("Could not check", head)]]
        for row in per_screen:
            data.append([cell(row["Screen"]), status_cell(row["Result"]),
                         cell(row["Passed"]), cell(row["Failed"]),
                         cell(row["Could not check"])])
        story += [grid(data, [total - 108 * mm, 30 * mm, 22 * mm, 22 * mm,
                              34 * mm]),
                  Spacer(1, 4 * mm)]

    # ---- what it entered ------------------------------------------------
    #
    # First, and in full. On a Litigation run this IS the record — Type of
    # Suit through to Date Of Decree — and it is what somebody reading the
    # report has come for. The rich-text box keeps its paragraphs here, which
    # is the whole reason each cell is a Paragraph rather than a string.
    entries = result.get("entries") or []
    if entries:
        story.append(Paragraph(f"What was entered ({len(entries)} field"
                               f"{'s' if len(entries) != 1 else ''})",
                               section))
        if dry:
            story.append(Paragraph(
                "This was a dry run: the values below were entered on the "
                "form and deliberately not saved.", small))
            story.append(Spacer(1, 2 * mm))

        groups: dict[str, list[dict]] = {}
        for e in entries:
            groups.setdefault(e.get("group") or e.get("screen", "") or
                              "(record)", []).append(e)

        for group, items in groups.items():
            data = [[cell("Field", head), cell("Value", head),
                     cell("Type", head)]]
            for e in items:
                kind = e.get("kind", "")
                value = e.get("value", "")
                if kind in RICH_KINDS:
                    paras = rich_paragraphs(value)
                    body = "<br/><br/>".join(
                        html.escape(p).replace("\n", "<br/>") for p in paras)
                    shown = Paragraph(body or "—", base)
                else:
                    shown = cell(value)
                data.append([cell(e.get("label", "")), shown, cell(kind, small)])
            story += [KeepTogether([Paragraph(group, bold), Spacer(1, 1.5 * mm),
                                    grid(data, [50 * mm, total - 76 * mm,
                                                26 * mm])]),
                      Spacer(1, 4 * mm)]

    # ---- how it got there ----------------------------------------------
    steps = result.get("steps") or []
    if steps:
        story.append(Paragraph("How it got there", section))
        data = [[cell("#", head), cell("Step", head), cell("Result", head),
                 cell("Note", head)]]
        for st in steps:
            data.append([cell(st.get("index", "")),
                         cell(st.get("label") or st.get("text", "")),
                         status_cell(st.get("status", "")),
                         cell(st.get("note", ""), small)])
        story += [grid(data, [10 * mm, 62 * mm, 30 * mm, total - 102 * mm]),
                  Spacer(1, 4 * mm)]

    # ---- what it checked ------------------------------------------------
    checks = sorted_checks(result)
    if checks:
        story.append(PageBreak())
        story.append(Paragraph(f"What was checked ({len(checks)})", section))
        by_screen: dict[str, list[dict]] = {}
        for c in checks:
            by_screen.setdefault(c.get("screen") or "(record)", []).append(c)
        for screen, items in by_screen.items():
            failed = sum(1 for c in items if c.get("status") == R.FAIL)
            data = [[cell("Result", head), cell("Check", head),
                     cell("Should be", head), cell("Actually", head),
                     cell("Notes", head)]]
            for c in items:
                data.append([status_cell(c.get("status", "")),
                             cell(c.get("name", "")),
                             cell(c.get("expected", ""), small),
                             cell(c.get("actual", ""), small),
                             cell(c.get("detail", ""), small)])
            story += [Paragraph(
                f"{screen} — {len(items)} check"
                f"{'s' if len(items) != 1 else ''}, {failed} failed", bold),
                Spacer(1, 1.5 * mm),
                grid(data, [22 * mm, 44 * mm, 36 * mm, 36 * mm,
                            total - 138 * mm]),
                Spacer(1, 4 * mm)]

    if result.get("artifacts_dir"):
        story += [Spacer(1, 2 * mm),
                  Paragraph("Screenshots and the machine-readable report for "
                            f"this run are in {result['artifacts_dir']}",
                            small)]

    # ---- page furniture -------------------------------------------------
    run_id = base_name(result)

    def furniture(canvas, doc) -> None:
        canvas.saveState()
        canvas.setStrokeColor(rule)
        canvas.setLineWidth(0.4)
        canvas.line(14 * mm, 14 * mm, A4[0] - 14 * mm, 14 * mm)
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(quiet)
        canvas.drawString(14 * mm, 9.5 * mm,
                          f"NBP Loan Origination System — automation report "
                          f"· {run_id}")
        canvas.drawRightString(A4[0] - 14 * mm, 9.5 * mm,
                               f"Page {canvas.getPageNumber()}")
        canvas.restoreState()

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=14 * mm, rightMargin=14 * mm,
        topMargin=14 * mm, bottomMargin=20 * mm,
        title=f"{result.get('target_title', 'Automation report')} — {run_id}",
        author="NBP LOS Automation Portal",
        subject=f"Automation report for record "
                f"{result.get('case_id', '') or 'unspecified'}")
    doc.build(story, onFirstPage=furniture, onLaterPages=furniture)
    return buf.getvalue()


def write_pdf(result: dict, directory: str = "") -> Optional[str]:
    """
    Save the PDF beside the run's other artifacts and return the path.

    For the CLI and for anything scripted; the portal serves the bytes
    straight to the browser instead.
    """
    if not pdf_available():
        return None
    target_dir = directory or result.get("artifacts_dir") or "."
    os.makedirs(target_dir, exist_ok=True)
    path = os.path.join(target_dir, f"{base_name(result)}.pdf")
    with open(path, "wb") as fh:
        fh.write(build_pdf(result))
    return path
