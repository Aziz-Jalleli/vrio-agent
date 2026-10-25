"""Billing data audit agent: Google Sheets + ArchiMate MCP + local Ollama -> PDF report.

Expected Google Sheets tabs:
  Customers: customer_id, name, email
  Billing: invoice_id, customer_id, customer_name, amount

Extra dependency for the PDF report:  pip install reportlab

Configure environment variables in a .env file beside this script.
Optional .env keys for the report: REPORT_TITLE, REPORT_PDF_NAME
"""

import asyncio
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

import gspread
import requests
import httpx  # NOTE: was "httpx2" in the original, which is not a real package
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    HRFlowable, KeepTogether, PageBreak, Paragraph, SimpleDocTemplate,
    Spacer, Table, TableStyle,
)

BASE_DIR = Path(__file__).resolve().parent


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def load_env(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def norm(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def norm_id(value: Any) -> str:
    return norm(value).casefold()


def read_google_sheets() -> tuple[list[dict], list[dict]]:
    creds_file = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "service-account.json")
    creds_path = Path(creds_file)
    if not creds_path.is_absolute():
        creds_path = BASE_DIR / creds_path
    sheet_id = os.getenv("GOOGLE_SHEET_ID", "").strip()
    if not sheet_id:
        raise RuntimeError("Set GOOGLE_SHEET_ID in your .env file.")
    if not creds_path.exists():
        raise RuntimeError(
            f"Google service-account JSON not found: {creds_path}. "
            "Download it from Google Cloud and place it beside this script."
        )

    client = gspread.service_account(filename=str(creds_path))
    spreadsheet = client.open_by_key(sheet_id)
    customers = spreadsheet.worksheet(os.getenv("CUSTOMERS_TAB", "Customers")).get_all_records()
    billing = spreadsheet.worksheet(os.getenv("BILLING_TAB", "Billing")).get_all_records()
    return customers, billing


# --------------------------------------------------------------------------- #
# Deterministic audit (unchanged)
# --------------------------------------------------------------------------- #
def audit_data(customers: list[dict], billing: list[dict]) -> dict:
    """Deterministic audit. Does not change source Google Sheets."""
    issues: list[dict] = []
    customers_by_id: dict[str, list[dict]] = defaultdict(list)
    invoice_by_id: dict[str, list[dict]] = defaultdict(list)

    for row_number, row in enumerate(customers, start=2):
        cid = norm_id(row.get("customer_id"))
        if cid:
            customers_by_id[cid].append({**row, "_row_number": row_number})
        else:
            issues.append({
                "type": "MISSING_CUSTOMER_ID", "source": "Customers",
                "row": row_number, "action": "REVIEW_REQUIRED",
                "details": "Customer row has no customer_id."
            })

    for cid, rows in customers_by_id.items():
        if len(rows) > 1:
            distinct_names = sorted({norm(r.get("name")) for r in rows if norm(r.get("name"))})
            issues.append({
                "type": "DUPLICATE_CUSTOMER_ID", "source": "Customers",
                "customer_id": cid,
                "rows": [r["_row_number"] for r in rows],
                "names": distinct_names,
                "action": "REVIEW_REQUIRED",
                "details": "Multiple customer records share this ID; do not choose a canonical row automatically."
            })

    for row_number, row in enumerate(billing, start=2):
        invoice_id = norm(row.get("invoice_id"))
        if invoice_id:
            invoice_by_id[norm_id(invoice_id)].append({**row, "_row_number": row_number})
        else:
            issues.append({
                "type": "MISSING_INVOICE_ID", "source": "Billing",
                "row": row_number, "action": "REVIEW_REQUIRED",
                "details": "Billing row has no invoice_id."
            })

        cid = norm_id(row.get("customer_id"))
        if not cid:
            issues.append({
                "type": "MISSING_BILLING_CUSTOMER_ID", "source": "Billing",
                "invoice_id": invoice_id or None, "row": row_number,
                "action": "REVIEW_REQUIRED", "details": "Billing row has no customer_id."
            })
            continue

        matched = customers_by_id.get(cid, [])
        if not matched:
            issues.append({
                "type": "MISSING_CUSTOMER_REFERENCE", "source": "Billing",
                "invoice_id": invoice_id or None, "customer_id": norm(row.get("customer_id")),
                "row": row_number, "action": "REVIEW_REQUIRED",
                "details": "Billing record references a customer_id absent from Customers."
            })
        elif len(matched) == 1:
            canonical_name = norm(matched[0].get("name"))
            billing_name = norm(row.get("customer_name"))
            if billing_name and canonical_name and billing_name.casefold() != canonical_name.casefold():
                issues.append({
                    "type": "CUSTOMER_NAME_MISMATCH", "source": "Billing",
                    "invoice_id": invoice_id or None, "customer_id": norm(row.get("customer_id")),
                    "row": row_number, "billing_name": billing_name,
                    "customer_name": canonical_name,
                    "proposed_action": f"Use '{canonical_name}' in the billing record if Customers is the approved source of truth.",
                    "action": "PROPOSE_CORRECTION",
                    "details": "Customer ID matches exactly, but the names differ."
                })
        else:
            issues.append({
                "type": "AMBIGUOUS_CUSTOMER_REFERENCE", "source": "Billing",
                "invoice_id": invoice_id or None, "customer_id": norm(row.get("customer_id")),
                "row": row_number, "action": "REVIEW_REQUIRED",
                "details": "Several Customers rows share this ID, so a canonical name cannot be selected safely."
            })

    for iid, rows in invoice_by_id.items():
        if len(rows) > 1:
            issues.append({
                "type": "DUPLICATE_INVOICE_ID", "source": "Billing",
                "invoice_id": norm(rows[0].get("invoice_id")),
                "rows": [r["_row_number"] for r in rows],
                "action": "REVIEW_REQUIRED",
                "details": "Multiple billing rows share this invoice ID."
            })

    return {
        "summary": {
            "customers_rows": len(customers),
            "billing_rows": len(billing),
            "issues_found": len(issues),
            "issues_requiring_review": sum(i.get("action") == "REVIEW_REQUIRED" for i in issues),
            "proposed_corrections": sum(i.get("action") == "PROPOSE_CORRECTION" for i in issues),
        },
        "issues": issues,
        "source_data_modified": False,
    }


# --------------------------------------------------------------------------- #
# ArchiMate MCP
# --------------------------------------------------------------------------- #
def get_schema(tool: Any) -> dict:
    schema = getattr(tool, "inputSchema", None)
    if schema is None:
        schema = getattr(tool, "input_schema", None)
    if schema is None and isinstance(tool, dict):
        schema = tool.get("inputSchema") or tool.get("input_schema")
    if hasattr(schema, "model_dump"):
        schema = schema.model_dump(by_alias=True)
    return schema if isinstance(schema, dict) else {"type": "object", "properties": {}}


def result_to_text(result: Any) -> str:
    structured = getattr(result, "structuredContent", None)
    if structured is None:
        structured = getattr(result, "structured_content", None)
    if structured is not None:
        return json.dumps(structured, ensure_ascii=False, default=str)
    content = getattr(result, "content", None) or []
    parts = [item.text for item in content if getattr(item, "text", None)]
    if parts:
        return "\n".join(parts)
    try:
        if hasattr(result, "model_dump"):
            return json.dumps(result.model_dump(by_alias=True), ensure_ascii=False, default=str)
    except Exception:
        pass
    return repr(result)


async def get_architecture_context() -> dict:
    mcp_url = os.getenv("ARCHI_MCP_URL", "http://localhost:8001/mcp").strip()
    token = os.getenv("ARCHI_TOKEN", "").strip()
    if not token:
        raise RuntimeError("Set ARCHI_TOKEN in .env to your ArchiMate MCP token.")

    async with httpx.AsyncClient(
        headers={"Authorization": f"Bearer {token}"},
        timeout=httpx.Timeout(30.0, read=300.0),
    ) as http_client:
        async with streamable_http_client(mcp_url, http_client=http_client) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                listed = await session.list_tools()
                available = {tool.name: tool for tool in listed.tools}
                context: dict[str, Any] = {"available_tools": sorted(available), "results": {}, "errors": []}

                async def call_tool(name: str, args: dict | None = None) -> str:
                    if name not in available:
                        return f"Tool unavailable: {name}"
                    try:
                        result = await session.call_tool(name, arguments=args or {})
                        return result_to_text(result)
                    except Exception as exc:
                        context["errors"].append(f"{name}: {exc}")
                        return f"Tool call failed: {name}: {exc}"

                if "archimate_list_views" in available:
                    context["results"]["views"] = await call_tool("archimate_list_views")

                if "archimate_find_elements" in available:
                    schema = get_schema(available["archimate_find_elements"])
                    props = schema.get("properties", {}) or {}
                    query_key = next((k for k in ("query", "search", "keyword", "name", "term") if k in props), None)
                    if query_key is None:
                        required = schema.get("required", []) or []
                        query_key = next((k for k in required if k in props), None)
                    for term in ("facturation", "client", "donnée"):
                        args = {query_key: term} if query_key else {}
                        context["results"][f"find_{term}"] = await call_tool("archimate_find_elements", args)

                return context


# --------------------------------------------------------------------------- #
# Ollama
# --------------------------------------------------------------------------- #
def ask_ollama(audit: dict, architecture: dict) -> str:
    url = os.getenv("OLLAMA_URL", "http://localhost:11434/api/chat").strip()
    model = os.getenv("OLLAMA_MODEL", "llama3.1:8b").strip()
    system_prompt = """You are an assistant auditing billing data for an enterprise architecture project.
Use ONLY the audit JSON and ArchiMate MCP results supplied by the user. Explain each detected data issue and its likely architectural relevance. Use actual ArchiMate element names only when they appear in the MCP results. Do not invent model elements or relationships. Distinguish detected facts from hypotheses. A customer-name mismatch can be proposed for correction only if the customer ID uniquely matches one Customers row and that sheet is the configured source of truth. Missing or ambiguous references require human review. The audit code does not modify source data.

FORMAT: Reply in clean Markdown using exactly these section headings, in this order:
## Executive Summary
## Issue-by-Issue Actions
## Architecture Impact
## Recommended Next Steps
Use short paragraphs and '-' bullet lists. Bold key terms with **double asterisks**. Do not use tables or code blocks."""
    user_payload = {
        "task": "Explain this billing audit and relate it to the actual ArchiMate model where the returned evidence supports the link. Give a concise summary, issue-by-issue actions, and architecture impact.",
        "audit": audit,
        "archimate_mcp_context": architecture,
    }
    response = requests.post(
        url,
        json={
            "model": model,
            "stream": False,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
            ],
            "options": {"temperature": 0.1},
        },
        timeout=300,
    )
    response.raise_for_status()
    body = response.json()
    return body.get("message", {}).get("content", "No response content from Ollama.")


# --------------------------------------------------------------------------- #
# PDF report
# --------------------------------------------------------------------------- #
NAVY = colors.HexColor("#1F2A44")
GREY = colors.HexColor("#6B7280")
LIGHT = colors.HexColor("#F3F4F6")
RULE = colors.HexColor("#D1D5DB")
AMBER = colors.HexColor("#B45309")
AMBER_BG = colors.HexColor("#FEF3C7")
BLUE = colors.HexColor("#1D4ED8")
BLUE_BG = colors.HexColor("#DBEAFE")
GREEN = colors.HexColor("#047857")
GREEN_BG = colors.HexColor("#D1FAE5")


def register_fonts() -> tuple[str, str, str]:
    """Use DejaVu Sans if available (full Unicode); otherwise built-in Helvetica.

    Helvetica covers French accents (é, è, à, ç...) but not every Unicode symbol.
    """
    candidates = [
        ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
         "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
         "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"),
        ("C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/arialbd.ttf", "C:/Windows/Fonts/cour.ttf"),
        ("/Library/Fonts/Arial.ttf", "/Library/Fonts/Arial Bold.ttf", "/Library/Fonts/Courier New.ttf"),
    ]
    for regular, bold, mono in candidates:
        if all(Path(p).exists() for p in (regular, bold, mono)):
            try:
                pdfmetrics.registerFont(TTFont("Report", regular))
                pdfmetrics.registerFont(TTFont("Report-Bold", bold))
                pdfmetrics.registerFont(TTFont("Report-Mono", mono))
                pdfmetrics.registerFontFamily("Report", normal="Report", bold="Report-Bold",
                                              italic="Report", boldItalic="Report-Bold")
                return "Report", "Report-Bold", "Report-Mono"
            except Exception:
                pass
    return "Helvetica", "Helvetica-Bold", "Courier"


def build_styles(font: str, bold: str) -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()["Normal"]
    return {
        "body": ParagraphStyle("body", parent=base, fontName=font, fontSize=10, leading=14.5,
                               textColor=colors.HexColor("#111827"), alignment=TA_LEFT, spaceAfter=6),
        "bullet": ParagraphStyle("bullet", parent=base, fontName=font, fontSize=10, leading=14.5,
                                 leftIndent=14, bulletIndent=3, spaceAfter=3),
        "h1": ParagraphStyle("h1", parent=base, fontName=bold, fontSize=15, leading=19,
                             textColor=NAVY, spaceBefore=14, spaceAfter=6),
        "h2": ParagraphStyle("h2", parent=base, fontName=bold, fontSize=12, leading=16,
                             textColor=NAVY, spaceBefore=10, spaceAfter=4),
        "h3": ParagraphStyle("h3", parent=base, fontName=bold, fontSize=10.5, leading=14,
                             textColor=NAVY, spaceBefore=8, spaceAfter=3),
        "title": ParagraphStyle("title", parent=base, fontName=bold, fontSize=24, leading=29,
                                textColor=NAVY, spaceAfter=4),
        "subtitle": ParagraphStyle("subtitle", parent=base, fontName=font, fontSize=11, leading=15,
                                   textColor=GREY, spaceAfter=2),
        "cell": ParagraphStyle("cell", parent=base, fontName=font, fontSize=8.5, leading=11),
        "cellb": ParagraphStyle("cellb", parent=base, fontName=bold, fontSize=8.5, leading=11,
                                textColor=colors.white),
        "kpi_value": ParagraphStyle("kpi_value", parent=base, fontName=bold, fontSize=20, leading=24,
                                    textColor=NAVY, alignment=1),
        "kpi_label": ParagraphStyle("kpi_label", parent=base, fontName=font, fontSize=8, leading=10,
                                    textColor=GREY, alignment=1),
        "note": ParagraphStyle("note", parent=base, fontName=font, fontSize=8.5, leading=12, textColor=GREY),
    }


def inline_md(text: str, mono: str) -> str:
    """Escape for ReportLab, then convert **bold**, *italic*, `code` to its markup."""
    text = escape(text)
    text = re.sub(r"`([^`]+)`", rf'<font face="{mono}" color="#9A3412">\1</font>', text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"(?<![\*\w])\*(?!\s)(.+?)(?<!\s)\*(?!\w)", r"<i>\1</i>", text)
    return text


def markdown_to_flowables(md: str, styles: dict, mono: str) -> list:
    """Small Markdown renderer: headings, bullets, numbered lists, paragraphs."""
    flow: list = []
    paragraph_lines: list[str] = []

    def flush() -> None:
        if paragraph_lines:
            flow.append(Paragraph(inline_md(" ".join(paragraph_lines), mono), styles["body"]))
            paragraph_lines.clear()

    for raw in md.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped or set(stripped) <= {"-", "*", "_", "="} and len(stripped) >= 3:
            flush()
            continue
        heading = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        bullet = re.match(r"^[-*•]\s+(.*)$", stripped)
        numbered = re.match(r"^(\d+)[.)]\s+(.*)$", stripped)
        if heading:
            flush()
            level = min(len(heading.group(1)), 3)
            flow.append(Paragraph(inline_md(heading.group(2), mono), styles[f"h{level}"]))
            if level == 1:
                flow.append(HRFlowable(width="100%", thickness=0.6, color=RULE, spaceAfter=4))
        elif bullet:
            flush()
            flow.append(Paragraph(inline_md(bullet.group(1), mono), styles["bullet"], bulletText="•"))
        elif numbered:
            flush()
            flow.append(Paragraph(inline_md(numbered.group(2), mono), styles["bullet"],
                                  bulletText=f"{numbered.group(1)}."))
        else:
            paragraph_lines.append(stripped)
    flush()
    return flow


def pretty_type(issue_type: str) -> str:
    return issue_type.replace("_", " ").title()


def location_of(issue: dict) -> str:
    if "rows" in issue:
        return "Rows " + ", ".join(str(r) for r in issue["rows"])
    if "row" in issue:
        return f"Row {issue['row']}"
    return "-"


def reference_of(issue: dict) -> str:
    parts = []
    if issue.get("invoice_id"):
        parts.append(f"Invoice: {issue['invoice_id']}")
    if issue.get("customer_id"):
        parts.append(f"Customer: {issue['customer_id']}")
    return "\n".join(parts) or "-"


def details_of(issue: dict) -> str:
    text = issue.get("details", "")
    if issue.get("billing_name") or issue.get("customer_name"):
        text += f"\nBilling: '{issue.get('billing_name')}' vs Customers: '{issue.get('customer_name')}'"
    if issue.get("names"):
        text += "\nNames: " + ", ".join(issue["names"])
    if issue.get("proposed_action"):
        text += "\nProposal: " + issue["proposed_action"]
    return text


def action_cell(action: str, styles: dict) -> Paragraph:
    color = BLUE if action == "PROPOSE_CORRECTION" else AMBER
    label = "Propose correction" if action == "PROPOSE_CORRECTION" else "Review required"
    style = ParagraphStyle("act", parent=styles["cell"], textColor=color, fontName=styles["h1"].fontName)
    return Paragraph(label, style)


def make_page_decorator(title: str, font: str):
    generated = datetime.now().strftime("%Y-%m-%d %H:%M")

    def decorate(canvas, doc):
        canvas.saveState()
        width, height = A4
        canvas.setStrokeColor(RULE)
        canvas.setLineWidth(0.5)
        canvas.line(18 * mm, 15 * mm, width - 18 * mm, 15 * mm)
        canvas.setFont(font, 8)
        canvas.setFillColor(GREY)
        canvas.drawString(18 * mm, 10 * mm, f"{title}  |  Generated {generated}")
        canvas.drawRightString(width - 18 * mm, 10 * mm, f"Page {doc.page}")
        if doc.page > 1:
            canvas.drawString(18 * mm, height - 12 * mm, title)
        canvas.restoreState()

    return decorate


def build_pdf_report(audit: dict, architecture: dict, explanation: str, pdf_path: Path) -> None:
    font, bold, mono = register_fonts()
    styles = build_styles(font, bold)
    title = os.getenv("REPORT_TITLE", "Billing Data Audit Report").strip()
    summary = audit["summary"]
    issues = audit["issues"]
    usable_width = A4[0] - 36 * mm

    doc = SimpleDocTemplate(
        str(pdf_path), pagesize=A4,
        leftMargin=18 * mm, rightMargin=18 * mm, topMargin=20 * mm, bottomMargin=22 * mm,
        title=title, author="Billing Data Audit Agent",
    )
    story: list = []

    # ---- Cover block --------------------------------------------------------
    story.append(Paragraph(escape(title), styles["title"]))
    story.append(Paragraph("Google Sheets billing data checked against the ArchiMate model", styles["subtitle"]))
    story.append(Paragraph(datetime.now().strftime("%d %B %Y, %H:%M"), styles["subtitle"]))
    story.append(Spacer(1, 4))
    story.append(HRFlowable(width="100%", thickness=1.5, color=NAVY, spaceAfter=10))

    # ---- KPI tiles ----------------------------------------------------------
    def tile(value: Any, label: str) -> list:
        return [Paragraph(str(value), styles["kpi_value"]), Paragraph(label, styles["kpi_label"])]

    kpis = [
        tile(summary["customers_rows"], "Customer rows"),
        tile(summary["billing_rows"], "Billing rows"),
        tile(summary["issues_found"], "Issues found"),
        tile(summary["issues_requiring_review"], "Need human review"),
        tile(summary["proposed_corrections"], "Proposed corrections"),
    ]
    kpi_table = Table([[k for k in kpis]], colWidths=[usable_width / 5] * 5)
    kpi_table.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.5, RULE),
        ("INNERGRID", (0, 0), (-1, -1), 0.5, RULE),
        ("BACKGROUND", (0, 0), (-1, -1), LIGHT),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 9),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 9),
    ]))
    story.append(kpi_table)
    story.append(Spacer(1, 8))

    status_ok = summary["issues_found"] == 0
    status_text = ("No data issues detected." if status_ok else
                   f"{summary['issues_found']} issue(s) detected. "
                   "Source data was <b>not modified</b>; every correction needs approval.")
    status_style = ParagraphStyle("status", parent=styles["body"], textColor=GREEN if status_ok else AMBER,
                                  backColor=GREEN_BG if status_ok else AMBER_BG,
                                  borderPadding=(6, 8, 6, 8), spaceBefore=4, spaceAfter=10)
    story.append(Paragraph(status_text, status_style))

    # ---- Issues by type -----------------------------------------------------
    if issues:
        counts = Counter(i["type"] for i in issues)
        rows = [[Paragraph("Issue type", styles["cellb"]), Paragraph("Source", styles["cellb"]),
                 Paragraph("Count", styles["cellb"])]]
        source_by_type = {i["type"]: i["source"] for i in issues}
        for issue_type, count in counts.most_common():
            rows.append([Paragraph(pretty_type(issue_type), styles["cell"]),
                         Paragraph(source_by_type[issue_type], styles["cell"]),
                         Paragraph(str(count), styles["cell"])])
        by_type = Table(rows, colWidths=[usable_width * 0.6, usable_width * 0.25, usable_width * 0.15],
                        repeatRows=1)
        by_type.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), NAVY),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, LIGHT]),
            ("GRID", (0, 0), (-1, -1), 0.4, RULE),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]))
        story.append(KeepTogether([Paragraph("Issues by type", styles["h2"]), by_type]))

    # ---- AI analysis --------------------------------------------------------
    story.append(PageBreak())
    story.append(Paragraph("AI Analysis", styles["h1"]))
    story.append(HRFlowable(width="100%", thickness=0.6, color=RULE, spaceAfter=4))
    story.append(Paragraph(
        "Generated by the local Ollama model from the audit results and the ArchiMate MCP context. "
        "Treat it as an explanation to verify, not as ground truth.", styles["note"]))
    story.append(Spacer(1, 4))
    story.extend(markdown_to_flowables(explanation, styles, mono))

    # ---- Detailed findings --------------------------------------------------
    story.append(PageBreak())
    story.append(Paragraph("Detailed Findings", styles["h1"]))
    story.append(HRFlowable(width="100%", thickness=0.6, color=RULE, spaceAfter=6))
    if not issues:
        story.append(Paragraph("No issues to list.", styles["body"]))
    else:
        header = [Paragraph(h, styles["cellb"]) for h in ("#", "Issue", "Location", "References", "Details", "Action")]
        data = [header]
        for n, issue in enumerate(issues, start=1):
            data.append([
                Paragraph(str(n), styles["cell"]),
                Paragraph(f"<b>{escape(pretty_type(issue['type']))}</b><br/>{escape(issue['source'])}", styles["cell"]),
                Paragraph(escape(location_of(issue)), styles["cell"]),
                Paragraph(escape(reference_of(issue)).replace("\n", "<br/>"), styles["cell"]),
                Paragraph(escape(details_of(issue)).replace("\n", "<br/>"), styles["cell"]),
                action_cell(issue.get("action", ""), styles),
            ])
        widths = [0.05, 0.19, 0.11, 0.17, 0.33, 0.15]
        findings = Table(data, colWidths=[usable_width * w for w in widths], repeatRows=1)
        style_cmds = [
            ("BACKGROUND", (0, 0), (-1, 0), NAVY),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, LIGHT]),
            ("GRID", (0, 0), (-1, -1), 0.4, RULE),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]
        findings.setStyle(TableStyle(style_cmds))
        story.append(findings)

    # ---- Appendix: ArchiMate context ---------------------------------------
    story.append(Spacer(1, 12))
    tools = ", ".join(architecture.get("available_tools", [])) or "none"
    errors = architecture.get("errors", [])
    appendix = [
        Paragraph("Appendix: ArchiMate MCP session", styles["h2"]),
        Paragraph(f"<b>Tools available:</b> {escape(tools)}", styles["body"]),
        Paragraph(f"<b>Queries run:</b> {escape(', '.join(architecture.get('results', {}).keys()) or 'none')}",
                  styles["body"]),
        Paragraph("<b>MCP errors:</b> " + (escape("; ".join(errors)) if errors else "none"), styles["body"]),
        Paragraph("Full raw results are saved in archimate_audit_context.json.", styles["note"]),
    ]
    story.append(KeepTogether(appendix))

    decorate = make_page_decorator(title, font)
    doc.build(story, onFirstPage=decorate, onLaterPages=decorate)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> int:
    load_env(BASE_DIR / ".env")
    print("Billing Data Audit Agent")
    print("Reading Google Sheets...")
    try:
        customers, billing = read_google_sheets()
        audit = audit_data(customers, billing)
        report_path = BASE_DIR / "billing_audit_report.json"
        report_path.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Audit complete: {audit['summary']['issues_found']} issue(s) found.")
        print(f"Raw report saved to: {report_path}")

        print("Inspecting the ArchiMate model through MCP...")
        architecture = asyncio.run(get_architecture_context())
        arch_path = BASE_DIR / "archimate_audit_context.json"
        arch_path.write_text(json.dumps(architecture, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Architecture context saved to: {arch_path}")

        print("Asking local Ollama model to explain the results...\n")
        explanation = ask_ollama(audit, architecture)
        print(explanation)
        explanation_path = BASE_DIR / "billing_audit_explanation.md"
        explanation_path.write_text(explanation + "\n", encoding="utf-8")
        print(f"\nExplanation saved to: {explanation_path}")

        pdf_path = BASE_DIR / os.getenv("REPORT_PDF_NAME", "billing_audit_report.pdf")
        build_pdf_report(audit, architecture, explanation, pdf_path)
        print(f"PDF report saved to: {pdf_path}")

        print("\nNo Google Sheets cells were changed. Review the report before applying any corrections.")
        return 0
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        print("Check the setup steps, .env values, Google Sheet permissions, and that Ollama/MCP are running.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())