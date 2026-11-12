"""Documentos: Atenea crea PDF, Word (.docx) y Excel (.xlsx) bien maquetados a partir de Markdown (o filas JSON).
Se guardan en ~/.config/atenea/files/AAAA-MM/ y se muestran en el chat como archivo para abrir o descargar."""
from __future__ import annotations

import glob
import html
import json
import os
import re
import subprocess
import tempfile
import time

from . import config as C


def _out(title: str, ext: str) -> str:
    d = os.path.join(os.path.dirname(C.PATH), "files", time.strftime("%Y-%m"))
    os.makedirs(d, exist_ok=True)
    safe = re.sub(r"\s+", "-", re.sub(r"[^\w\- áéíóúñÁÉÍÓÚÑ]+", "", title).strip())[:80] or "documento"
    p = os.path.join(d, f"{safe}.{ext}")
    n = 2
    while os.path.exists(p):
        p = os.path.join(d, f"{safe} ({n}).{ext}")
        n += 1
    return p


# ---------------- PDF (Markdown -> HTML -> headless Chromium) ----------------
CSS = """@page{size:A4;margin:22mm 20mm 20mm}
body{font-family:'Geist','Inter','Helvetica Neue',Arial,sans-serif;color:#111315;font-size:10.5pt;line-height:1.55}
header{display:flex;justify-content:space-between;align-items:flex-end;border-bottom:2px solid #2F6B4F;padding-bottom:8px;margin-bottom:18px}
header h1{font-size:20pt;margin:0;letter-spacing:-.02em}header .d{color:#5B616B;font-size:9pt}
h1{font-size:16pt}h2{font-size:13pt;margin:18px 0 6px;color:#1F4A36}h3{font-size:11pt;margin:14px 0 4px}
table{border-collapse:collapse;width:100%;margin:8px 0 12px;font-size:9.5pt}th,td{border:1px solid #E2DED4;padding:5px 8px;text-align:left;vertical-align:top}
th{background:#EFECE5}tr:nth-child(even) td{background:#FAF9F6}
code{font-family:'Geist Mono',Menlo,monospace;font-size:9pt;background:#EFECE5;padding:1px 4px;border-radius:3px}
pre{background:#F6F4EF;border:1px solid #E2DED4;border-radius:6px;padding:8px 10px;white-space:pre-wrap}pre code{background:none}
blockquote{border-left:3px solid #2F6B4F;margin:8px 0;padding:2px 12px;color:#33373D}
img{max-width:100%}a{color:#2F6B4F}"""


def _chrome() -> str | None:
    c = glob.glob(os.path.expanduser("~/.cache/ms-playwright/chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell"))
    return c[0] if c else None


def make_pdf(title: str, content: str) -> str:
    import markdown
    body = markdown.markdown(content, extensions=["tables", "fenced_code", "sane_lists"])
    page = (f"<!doctype html><html><head><meta charset='utf-8'><title>{html.escape(title)}</title><style>{CSS}</style>"
            f"</head><body><header><h1>{html.escape(title)}</h1><span class='d'>{time.strftime('%d/%m/%Y')}</span></header>"
            f"{body}</body></html>")
    out = _out(title, "pdf")
    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, "doc.html")
        open(src, "w", encoding="utf-8").write(page)
        ch = _chrome()
        if ch:
            subprocess.run([ch, "--no-sandbox", "--disable-gpu", "--no-pdf-header-footer", f"--print-to-pdf={out}",
                            "file://" + src], capture_output=True, timeout=120)
        if not os.path.exists(out):                        # fallback: LibreOffice
            subprocess.run(["soffice", "--headless", "--convert-to", "pdf", "--outdir", td, src],
                           capture_output=True, timeout=180)
            if os.path.exists(os.path.join(td, "doc.pdf")):
                os.replace(os.path.join(td, "doc.pdf"), out)
    return out if os.path.exists(out) else "Error: could not create the PDF"


# ---------------- Word ----------------
def _runs(par, text: str):
    for part in re.split(r"(\*\*[^*]+\*\*|`[^`]+`|\*[^*]+\*)", text):
        if not part:
            continue
        if part.startswith("**") and part.endswith("**"):
            par.add_run(part[2:-2]).bold = True
        elif part.startswith("`") and part.endswith("`"):
            r = par.add_run(part[1:-1])
            r.font.name = "Consolas"
        elif part.startswith("*") and part.endswith("*") and len(part) > 2:
            par.add_run(part[1:-1]).italic = True
        else:
            par.add_run(part)


def make_docx(title: str, content: str) -> str:
    from docx import Document
    from docx.shared import Pt, RGBColor
    d = Document()
    st = d.styles["Normal"]
    st.font.name, st.font.size = "Calibri", Pt(11)
    h = d.add_heading(title, 0)
    for r in h.runs:
        r.font.color.rgb = RGBColor(0x11, 0x13, 0x15)
    lines, i, code = content.splitlines(), 0, None
    while i < len(lines):
        ln = lines[i]
        if ln.strip().startswith("```"):
            if code is None:
                code = []
            else:
                p = d.add_paragraph()
                r = p.add_run("\n".join(code))
                r.font.name, r.font.size = "Consolas", Pt(9.5)
                code = None
            i += 1
            continue
        if code is not None:
            code.append(ln)
            i += 1
            continue
        if re.match(r"^\s*\|.*\|\s*$", ln) and i + 1 < len(lines) and re.match(r"^\s*\|[\s:|-]+\|\s*$", lines[i + 1]):
            rows = [ln]
            i += 2
            while i < len(lines) and re.match(r"^\s*\|.*\|\s*$", lines[i]):
                rows.append(lines[i])
                i += 1
            cells = [[c.strip() for c in r.strip().strip("|").split("|")] for r in rows]
            t = d.add_table(rows=len(cells), cols=max(len(r) for r in cells))
            t.style = "Light Grid Accent 1"
            for ri, row in enumerate(cells):
                for ci, val in enumerate(row):
                    cell = t.cell(ri, ci)
                    cell.text = ""
                    _runs(cell.paragraphs[0], val)
                    if ri == 0:
                        for r in cell.paragraphs[0].runs:
                            r.bold = True
            continue
        m = re.match(r"^(#{1,4})\s+(.*)", ln)
        if m:
            d.add_heading(m.group(2), min(len(m.group(1)), 3))
        elif re.match(r"^\s*[-*]\s+", ln):
            _runs(d.add_paragraph(style="List Bullet"), re.sub(r"^\s*[-*]\s+", "", ln))
        elif re.match(r"^\s*\d+[.)]\s+", ln):
            _runs(d.add_paragraph(style="List Number"), re.sub(r"^\s*\d+[.)]\s+", "", ln))
        elif ln.strip().startswith(">"):
            p = d.add_paragraph(style="Intense Quote")
            _runs(p, ln.strip().lstrip("> "))
        elif ln.strip():
            _runs(d.add_paragraph(), ln)
        i += 1
    out = _out(title, "docx")
    d.save(out)
    return out


# ---------------- Excel ----------------
def _tables_from_markdown(content: str) -> dict:
    sheets, lines, i, n = {}, content.splitlines(), 0, 1
    title = None
    while i < len(lines):
        m = re.match(r"^#{1,4}\s+(.*)", lines[i])
        if m:
            title = m.group(1)[:31]
        if re.match(r"^\s*\|.*\|\s*$", lines[i]) and i + 1 < len(lines) and re.match(r"^\s*\|[\s:|-]+\|\s*$", lines[i + 1]):
            rows = [lines[i]]
            i += 2
            while i < len(lines) and re.match(r"^\s*\|.*\|\s*$", lines[i]):
                rows.append(lines[i])
                i += 1
            name = re.sub(r"[\[\]:*?/\\]", "", title or f"Hoja {n}")[:31] or f"Hoja {n}"
            sheets[name] = [[c.strip() for c in r.strip().strip("|").split("|")] for r in rows]
            n, title = n + 1, None
            continue
        i += 1
    return sheets


def _num(v):
    if isinstance(v, (int, float)) or v is None:
        return v
    s = str(v).strip().replace(" ", "")
    t = s.replace("€", "").replace("$", "").replace("%", "").strip()
    if re.fullmatch(r"-?\d{1,3}(\.\d{3})+(,\d+)?", t):           # 1.234,56
        t = t.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"-?\d+,\d+", t):
        t = t.replace(",", ".")
    try:
        x = float(t)
        return x / 100 if s.endswith("%") else (int(x) if x.is_integer() and "." not in t else x)
    except ValueError:
        return s


def make_xlsx(title: str, content: str) -> str:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    try:
        data = json.loads(content)
        sheets = data.get("sheets", data) if isinstance(data, dict) else {"Hoja 1": data}
    except (json.JSONDecodeError, AttributeError):
        sheets = _tables_from_markdown(content)
    if not sheets:
        return "Error: no tables found (give Markdown tables or JSON {\"sheets\": {\"Name\": [[header...], [row...]]}})"
    wb = Workbook()
    wb.remove(wb.active)
    for name, rows in sheets.items():
        ws = wb.create_sheet(str(name)[:31])
        for r in rows:
            ws.append([_num(v) for v in r])
        for c in ws[1]:
            c.font = Font(bold=True, color="FFFFFF")
            c.fill = PatternFill("solid", fgColor="2F6B4F")
            c.alignment = Alignment(vertical="center")
        ws.freeze_panes = "A2"
        for i, col in enumerate(ws.columns, 1):
            ws.column_dimensions[get_column_letter(i)].width = min(60, max(10, *(len(str(c.value or "")) + 2 for c in col)))
        if ws.max_row > 1:
            ws.auto_filter.ref = ws.dimensions
    out = _out(title, "xlsx")
    wb.save(out)
    return out


def create_document(title: str, content: str, format: str = "pdf") -> str:
    fmt = (format or "pdf").lower().strip(". ")
    try:
        out = {"pdf": make_pdf, "docx": make_docx, "word": make_docx, "xlsx": make_xlsx, "excel": make_xlsx}[fmt](title, content)
    except KeyError:
        return "Error: format must be pdf, docx or xlsx"
    except Exception as e:
        return f"Error creating the document: {type(e).__name__}: {e}"
    if out.startswith("Error"):
        return out
    return f"Document created ({os.path.getsize(out) // 1024} KB). Show it to the user with this markdown line:\n[📄 {os.path.basename(out)}]({out})"


INSTRUCTIONS = """- DOCUMENTS: create_document(title, content, format) makes a real file: "pdf" (nicely laid out report), "docx"
  (Word) or "xlsx" (Excel). content is Markdown (headings, lists, **bold**, tables); for xlsx give Markdown tables
  (each table becomes a sheet; a heading right above names it) or JSON {"sheets": {"Name": [[headers], [row], …]}}.
  Use it when the user asks for a document, report, invoice, CV, table or spreadsheet to keep or send."""
