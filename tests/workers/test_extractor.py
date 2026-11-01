import pytest

from api.validators.file_type import CSV, DOCX, PDF, PNG, TXT, XLSX
from workers.extractor import ExtractionError, NeedsOcrError, extract_markdown
from tests.workers.sample_documents import blank_pdf, pdf_with_text, sample_docx, sample_xlsx


def test_pdf_text_layer_becomes_page_sections():
    markdown = extract_markdown(pdf_with_text("Quarterly revenue grew", "Outlook is stable"), PDF)
    assert markdown == "## Page 1\n\nQuarterly revenue grew\n\n## Page 2\n\nOutlook is stable\n"


def test_pdf_without_text_layer_needs_ocr():
    with pytest.raises(NeedsOcrError, match="no text layer"):
        extract_markdown(blank_pdf(), PDF)


def test_password_protected_pdf_is_rejected():
    with pytest.raises(ExtractionError, match="password-protected"):
        extract_markdown(blank_pdf(password="secret"), PDF)


def test_damaged_pdf_is_rejected():
    with pytest.raises(ExtractionError, match="damaged or unreadable"):
        extract_markdown(b"%PDF-1.7\nthis is not really a pdf", PDF)


def test_docx_headings_lists_and_tables():
    markdown = extract_markdown(sample_docx(), DOCX)
    assert markdown == (
        "# Quarterly Report\n\n"
        "# Revenue\n\n"
        "Revenue grew 12% year over year.\n\n"
        "- North region\n\n"
        "1. Hire two engineers\n\n"
        "| Region | Amount |\n"
        "| --- | --- |\n"
        "| North \\| East | 100 |\n"
    )


def test_xlsx_sheets_become_tables_and_empty_sheets_are_skipped():
    markdown = extract_markdown(sample_xlsx(), XLSX)
    assert markdown == (
        "## Sheet: Q1\n\n"
        "| Region | Amount | Closed |\n"
        "| --- | --- | --- |\n"
        "| North | 100 | 2026-03-31T00:00:00 |\n"
        "| South | 82.5 |  |\n"
    )


def test_csv_with_sniffed_delimiter():
    data = "\ufeffname;amount\nacme;100\nglobex;200\n".encode()
    assert extract_markdown(data, CSV) == "| name | amount |\n| --- | --- |\n| acme | 100 |\n| globex | 200 |\n"


def test_txt_is_kept_as_is():
    assert extract_markdown(b"Quarterly notes\n\nRevenue grew 12%.\n", TXT) == "Quarterly notes\n\nRevenue grew 12%.\n"


def test_whitespace_only_text_has_nothing_to_extract():
    with pytest.raises(ExtractionError, match="No extractable text"):
        extract_markdown(b" \n\t\n", TXT)


def test_images_need_ocr():
    with pytest.raises(NeedsOcrError):
        extract_markdown(b"\x89PNG\r\n\x1a\n", PNG)


def test_unknown_type_is_rejected():
    with pytest.raises(ExtractionError, match="Unsupported document type"):
        extract_markdown(b"PK\x03\x04", "application/zip")
