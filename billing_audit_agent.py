"""Billing data audit AGENT: Google Sheets + ArchiMate MCP + local Ollama -> PDF report.

The LLM is an agent with tools: it lists the billing issues and decides which invoices to fix.
Fixes are written to the Google Sheet (Billing.customer_name) using the name from Customers.
The sheet must be shared with the service account as EDITOR.

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
import httpx2
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
# Write-back to Google Sheets (called only by the agent's fix tool)
# --------------------------------------------------------------------------- #
def apply_name_corrections(audit: dict, only_invoice: str | None = None) -> list[dict]:
    """Write the Customers name into Billing.customer_name, ONLY for PROPOSE_CORRECTION issues
    (customer_id matches exactly one Customers row). Everything else is left for a human."""
    from gspread.utils import rowcol_to_a1

    creds_path = Path(os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "service-account.json"))
    if not creds_path.is_absolute():
        creds_path = BASE_DIR / creds_path
    client = gspread.service_account(filename=str(creds_path))
    ws = client.open_by_key(os.getenv("GOOGLE_SHEET_ID", "").strip()).worksheet(
        os.getenv("BILLING_TAB", "Billing"))

    values = ws.get_all_values()  # one read, used to re-check each row before writing
    header = [h.strip() for h in values[0]]
    inv_col = header.index("invoice_id")
    name_col = header.index("customer_name")

    updates, changes = [], []
    for issue in audit["issues"]:
        if issue.get("action") != "PROPOSE_CORRECTION":
            continue
        if only_invoice and norm_id(issue.get("invoice_id")) != norm_id(only_invoice):
            continue
        row = issue["row"]  # 1-based sheet row
        new_name = issue["customer_name"]
        if row > len(values):
            continue
        current = values[row - 1]
        # Safety: the row must still be the same invoice with the same old name
        if norm(current[inv_col]) != (issue.get("invoice_id") or "") \
                or norm(current[name_col]) != issue["billing_name"]:
            continue
        updates.append({"range": rowcol_to_a1(row, name_col + 1), "values": [[new_name]]})
        changes.append({"invoice_id": issue.get("invoice_id"), "customer_id": issue["customer_id"],
                        "row": row, "old_name": issue["billing_name"], "new_name": new_name})

    if updates:
        ws.batch_update(updates, value_input_option="RAW")
    return changes


# --------------------------------------------------------------------------- #
# Deterministic audit (detection only; it never writes)
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

    async with httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {token}"},
        timeout=httpx2.Timeout(30.0, read=300.0),
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
# Ollama agent
# --------------------------------------------------------------------------- #
AGENT_TOOLS = [
    {"type": "function", "function": {
        "name": "list_billing_issues",
        "description": "Return the billing data issues found in Google Sheets (type, invoice_id, customer_id, row, action).",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {
        "name": "fix_billing_customer_name",
        "description": ("Correct the customer_name of ONE invoice in the Billing sheet using the name from the "
                        "Customers sheet. Works only for CUSTOMER_NAME_MISMATCH issues; refuses anything else."),
        "parameters": {"type": "object",
                       "properties": {"invoice_id": {"type": "string", "description": "e.g. INV001"}},
                       "required": ["invoice_id"]}}},
]


TOOL_NAMES = {t["function"]["name"] for t in AGENT_TOOLS}


def extract_text_tool_calls(text: str) -> list[dict]:
    """Small models often PRINT a tool call as JSON text instead of using the tool-call API.
    Recover those calls so the agent still acts: {"name": ..., "parameters": {...}}."""
    calls, decoder, i = [], json.JSONDecoder(), 0
    while True:
        j = text.find("{", i)
        if j < 0:
            break
        try:
            obj, end = decoder.raw_decode(text[j:])
        except ValueError:
            i = j + 1
            continue
        i = j + end
        if isinstance(obj, dict) and obj.get("name") in TOOL_NAMES:
            args = obj.get("parameters", obj.get("arguments", {}))
            calls.append({"function": {"name": obj["name"], "arguments": args if isinstance(args, dict) else {}}})
    return calls


def unfixed_mismatches(audit: dict, attempted: set) -> list[str]:
    done = {norm_id(c["invoice_id"]) for c in audit["corrections_applied"]}
    return [i["invoice_id"] for i in audit["issues"]
            if i.get("action") == "PROPOSE_CORRECTION" and i.get("invoice_id")
            and norm_id(i["invoice_id"]) not in done and norm_id(i["invoice_id"]) not in attempted]


def run_tool(name: str, args: dict, audit: dict) -> str:
    """Executes a tool the LLM asked for. All safety checks live here, not in the prompt."""
    if name == "list_billing_issues":
        return json.dumps(audit["issues"], ensure_ascii=False)
    if name == "fix_billing_customer_name":
        inv = norm(args.get("invoice_id"))
        if not inv:
            return "Error: invoice_id is required."
        if any(norm_id(c["invoice_id"]) == norm_id(inv) for c in audit["corrections_applied"]):
            return f"{inv} was already corrected."
        try:
            changes = apply_name_corrections(audit, only_invoice=inv)
        except Exception as exc:
            return f"Error while writing to Google Sheets: {exc}"
        if not changes:
            return (f"Refused: {inv} has no safe automatic correction (not a name mismatch with a unique "
                    "customer ID, or the sheet changed). Leave it for human review.")
        audit["corrections_applied"].extend(changes)
        return "Corrected in Google Sheets: " + json.dumps(changes, ensure_ascii=False)
    return f"Unknown tool: {name}"


def run_agent(audit: dict, architecture: dict) -> str:
    url = os.getenv("OLLAMA_URL", "http://localhost:11434/api/chat").strip()
    model = os.getenv("OLLAMA_MODEL", "llama3.1:8b").strip()
    system_prompt = """You are an agent that audits and repairs billing data for an enterprise architecture project.
You have two tools. Work in this order:
1. Call list_billing_issues.
2. For EVERY issue of type CUSTOMER_NAME_MISMATCH, call fix_billing_customer_name with its invoice_id.
3. Never try to fix other issue types: they need human review.
4. When all fixes are done, write the final report. Only state that something was corrected if a tool result confirmed it.
Use ArchiMate element names only if they appear in the provided ArchiMate context. Do not invent elements.

FORMAT of the final report, clean Markdown with exactly these headings, in this order:
## Executive Summary
## Issue-by-Issue Actions
## Architecture Impact
## Recommended Next Steps
Use short paragraphs and '-' bullets. Bold key terms with **double asterisks**. No tables or code blocks."""
    arch_text = json.dumps(architecture.get("results", {}), ensure_ascii=False)[:6000]
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": "Audit summary: " + json.dumps(audit["summary"]) +
                                    "\n\nArchiMate MCP context:\n" + arch_text +
                                    "\n\nStart now: audit, fix what is safe, then write the report."},
    ]
    listed_issues, attempted, nudges = False, set(), 0
    for _ in range(12):
        resp = requests.post(url, json={"model": model, "stream": False, "messages": messages,
                                        "tools": AGENT_TOOLS,
                                        "options": {"temperature": 0.1, "num_ctx": 8192}}, timeout=300)
        resp.raise_for_status()
        msg = resp.json()["message"]
        messages.append(msg)
        calls = msg.get("tool_calls") or extract_text_tool_calls(msg.get("content") or "")
        if not calls:
            # The model wants to stop. If it skipped its job, push it back (max 2 times).
            pending = unfixed_mismatches(audit, attempted)
            if nudges < 2 and (not listed_issues or pending):
                nudges += 1
                hint = ("You have not called any tool yet. Call list_billing_issues now."
                        if not listed_issues else
                        "These invoices still have unfixed name mismatches: " + ", ".join(pending) +
                        ". Call fix_billing_customer_name for each one.")
                print("  [agent] nudging the model to use its tools")
                messages.append({"role": "user", "content": hint + " Use the tool-calling interface."})
                continue
            return msg.get("content") or "No response content from the model."
        for call in calls:
            fn = call.get("function", {})
            args = fn.get("arguments") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    args = {}
            name = fn.get("name", "")
            print(f"  [agent] tool call: {name}({args})")
            if name == "list_billing_issues":
                listed_issues = True
            if name == "fix_billing_customer_name":
                attempted.add(norm_id(args.get("invoice_id")))
            messages.append({"role": "tool", "tool_name": name, "content": run_tool(name, args, audit)})
    return "The agent stopped after 10 steps without a final report."


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
                   f"{summary['issues_found']} issue(s) detected. " +
                   (f"<b>{len(audit.get('corrections_applied', []))} billing name(s) were corrected</b> "
                    "by the agent from the Customers sheet; all other issues need human review."
                    if audit.get("corrections_applied") else
                    "No correction was applied; every issue needs human review."))
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
        "Generated by the local Ollama agent from the audit tools and the ArchiMate MCP context. "
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

    # ---- Corrections applied by the agent ----------------------------------
    applied = audit.get("corrections_applied", [])
    story.append(Spacer(1, 12))
    story.append(Paragraph("Corrections applied by the agent", styles["h2"]))
    if not applied:
        story.append(Paragraph("None.", styles["body"]))
    else:
        rows = [[Paragraph(h, styles["cellb"]) for h in ("Invoice", "Customer ID", "Sheet row", "Old name", "New name")]]
        for c in applied:
            rows.append([Paragraph(escape(str(c[k])), styles["cell"])
                         for k in ("invoice_id", "customer_id", "row", "old_name", "new_name")])
        ctable = Table(rows, colWidths=[usable_width * w for w in (0.15, 0.17, 0.13, 0.275, 0.275)], repeatRows=1)
        ctable.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), NAVY),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, LIGHT]),
            ("GRID", (0, 0), (-1, -1), 0.4, RULE),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ]))
        story.append(ctable)

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
        audit["corrections_applied"] = []
        report_path = BASE_DIR / "billing_audit_report.json"
        report_path.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Audit complete: {audit['summary']['issues_found']} issue(s) found.")
        print(f"Raw report saved to: {report_path}")

        print("Inspecting the ArchiMate model through MCP...")
        architecture = asyncio.run(get_architecture_context())
        arch_path = BASE_DIR / "archimate_audit_context.json"
        arch_path.write_text(json.dumps(architecture, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Architecture context saved to: {arch_path}")

        print("Starting the agent (it decides which fixes to apply)...\n")
        explanation = run_agent(audit, architecture)
        audit["source_data_modified"] = bool(audit["corrections_applied"])
        (BASE_DIR / "billing_corrections_log.json").write_text(
            json.dumps(audit["corrections_applied"], ensure_ascii=False, indent=2), encoding="utf-8")
        print(explanation)
        explanation_path = BASE_DIR / "billing_audit_explanation.md"
        explanation_path.write_text(explanation + "\n", encoding="utf-8")
        print(f"\nExplanation saved to: {explanation_path}")

        pdf_path = BASE_DIR / os.getenv("REPORT_PDF_NAME", "billing_audit_report.pdf")
        build_pdf_report(audit, architecture, explanation, pdf_path)
        print(f"PDF report saved to: {pdf_path}")

        n = len(audit["corrections_applied"])
        print(f"\n{n} Google Sheets cell(s) corrected by the agent (see billing_corrections_log.json)."
              if n else "\nNo Google Sheets cells were changed.")
        return 0
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        print("Check the setup steps, .env values, Google Sheet permissions, and that Ollama/MCP are running.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())