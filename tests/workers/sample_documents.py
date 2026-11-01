"""Builders for small, real documents used by the extractor and worker tests."""

import io
from datetime import date

import docx
import openpyxl
from pypdf import PdfWriter


def pdf_with_text(*pages: str) -> bytes:
    """A PDF whose pages each draw one line of text (Helvetica), i.e. with a real text layer."""
    page_ids = [4 + 2 * index for index in range(len(pages))]
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{' '.join(f'{p} 0 R' for p in page_ids)}] /Count {len(pages)} >>".encode(),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    for page_id, text in zip(page_ids, pages, strict=True):
        content = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {page_id + 1} 0 R >>".encode()
        )
        objects.append(b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream")

    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(f"{number} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets:
        out.write(f"{offset:010d} 00000 n \n".encode())
    out.write(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return out.getvalue()


def blank_pdf(*, password: str | None = None) -> bytes:
    """A one-page PDF without any text layer, like a scan; optionally encrypted with a user password."""
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    if password is not None:
        writer.encrypt(password)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def sample_docx() -> bytes:
    document = docx.Document()
    document.add_heading("Quarterly Report", level=0)
    document.add_heading("Revenue", level=1)
    document.add_paragraph("Revenue grew 12% year over year.")
    document.add_paragraph("North region", style="List Bullet")
    document.add_paragraph("Hire two engineers", style="List Number")
    table = document.add_table(rows=2, cols=2)
    for cell, text in zip(table.rows[0].cells, ["Region", "Amount"], strict=True):
        cell.text = text
    for cell, text in zip(table.rows[1].cells, ["North | East", "100"], strict=True):
        cell.text = text
    out = io.BytesIO()
    document.save(out)
    return out.getvalue()


def sample_xlsx() -> bytes:
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Q1"
    sheet.append(["Region", "Amount", "Closed", None])
    sheet.append(["North", 100.0, date(2026, 3, 31), None])
    sheet.append([None, None, None, None])
    sheet.append(["South", 82.5, None, None])
    workbook.create_sheet("Empty")
    out = io.BytesIO()
    workbook.save(out)
    return out.getvalue()
