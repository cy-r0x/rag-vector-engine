"""Direct (non-OCR) extraction of uploaded documents to Markdown, routed by the MIME type detected at upload."""

import csv
import io
from collections.abc import Callable, Iterable
from datetime import date, datetime, time

import docx
import openpyxl
from docx.document import Document as DocxDocument
from docx.table import Table
from docx.text.paragraph import Paragraph
from pypdf import PdfReader
from pypdf.errors import PdfReadError

from api.validators.file_type import CSV, DOCX, IMAGE_MIMES, PDF, TXT, XLSX

_CSV_DELIMITERS = ",;\t|"
_CSV_SNIFF_BYTES = 64 * 1024


class ExtractionError(Exception):
    """The document can't be turned into text; `str(exc)` is safe to show to the tenant."""


class NeedsOcrError(ExtractionError):
    """The document has no text layer (an image or a scanned PDF)."""


def extract_markdown(data: bytes, mime: str) -> str:
    """Convert a document to Markdown. Raises ExtractionError (or NeedsOcrError) when there is no usable text."""
    if mime in IMAGE_MIMES:
        raise NeedsOcrError("Images need OCR, which is not available yet")
    extractor = _EXTRACTORS.get(mime)
    if extractor is None:
        raise ExtractionError(f"Unsupported document type: {mime}")

    markdown = extractor(data).strip()
    if not markdown:
        raise ExtractionError("No extractable text found in the document")
    return markdown + "\n"


def _pdf(data: bytes) -> str:
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(""):
            raise ExtractionError("The PDF is password-protected")
        pages = [(number, (page.extract_text() or "").strip()) for number, page in enumerate(reader.pages, start=1)]
    except PdfReadError as exc:
        raise ExtractionError("The PDF is damaged or unreadable") from exc

    if not any(text for _, text in pages):
        raise NeedsOcrError("The PDF has no text layer (scanned?); OCR is not available yet")
    return "\n\n".join(f"## Page {number}\n\n{text}" for number, text in pages if text)


def _docx(data: bytes) -> str:
    document: DocxDocument = docx.Document(io.BytesIO(data))
    blocks: list[str] = []
    for item in document.iter_inner_content():
        block = _docx_table(item) if isinstance(item, Table) else _docx_paragraph(item)
        if block:
            blocks.append(block)
    return "\n\n".join(blocks)


def _docx_paragraph(paragraph: Paragraph) -> str:
    text = paragraph.text.strip()
    if not text:
        return ""
    style = (paragraph.style.name if paragraph.style is not None else "") or ""
    if style == "Title":
        return f"# {text}"
    if style.startswith("Heading"):
        level = style.removeprefix("Heading").strip()
        depth = min(int(level), 6) if level.isdigit() else 1
        return f"{'#' * depth} {text}"
    if style.startswith("List Number"):
        return f"1. {text}"
    if style.startswith("List") or paragraph._p.pPr is not None and paragraph._p.pPr.numPr is not None:
        return f"- {text}"
    return text


def _docx_table(table: Table) -> str:
    return _markdown_table([cell.text for cell in row.cells] for row in table.rows)


def _xlsx(data: bytes) -> str:
    workbook = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    try:
        sections = []
        for sheet in workbook.worksheets:
            table = _markdown_table([_cell_text(value) for value in row] for row in sheet.iter_rows(values_only=True))
            if table:
                sections.append(f"## Sheet: {sheet.title}\n\n{table}")
        return "\n\n".join(sections)
    finally:
        workbook.close()


def _csv(data: bytes) -> str:
    text = _decode(data)
    try:
        dialect = csv.Sniffer().sniff(text[:_CSV_SNIFF_BYTES], delimiters=_CSV_DELIMITERS)
    except csv.Error:
        dialect = csv.excel
    return _markdown_table(csv.reader(io.StringIO(text), dialect))


def _txt(data: bytes) -> str:
    return _decode(data)


def _decode(data: bytes) -> str:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ExtractionError("The text is not valid UTF-8") from exc


def _cell_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, datetime | date | time):
        return value.isoformat()
    return str(value)


def _markdown_table(rows: Iterable[Iterable[str]]) -> str:
    """Markdown table with the first non-empty row as header; empty rows and trailing empty columns are dropped."""
    cleaned = [[_cell(value) for value in row] for row in rows]
    cleaned = [row for row in cleaned if any(row)]
    if not cleaned:
        return ""

    width = max(len(row) for row in cleaned)
    while width > 0 and not any(len(row) >= width and row[width - 1] for row in cleaned):
        width -= 1
    cleaned = [(row + [""] * width)[:width] for row in cleaned]

    header, *body = cleaned
    lines = [_markdown_row(header), _markdown_row(["---"] * width), *(_markdown_row(row) for row in body)]
    return "\n".join(lines)


def _cell(value: str) -> str:
    return " ".join(str(value).split()).replace("|", "\\|")


def _markdown_row(cells: list[str]) -> str:
    return "| " + " | ".join(cells) + " |"


_EXTRACTORS: dict[str, Callable[[bytes], str]] = {
    PDF: _pdf,
    DOCX: _docx,
    XLSX: _xlsx,
    CSV: _csv,
    TXT: _txt,
}
