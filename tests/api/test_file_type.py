import io
import zipfile

import pytest

from api.validators.file_type import (
    CSV,
    DOCX,
    JPEG,
    PDF,
    PNG,
    TIFF,
    TXT,
    XLSX,
    FileTooLargeError,
    FileTypeMismatchError,
    UnsupportedFileTypeError,
    detect_file_type,
)

PDF_BYTES = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n1 0 obj\n<<>>\nendobj\n"
PNG_BYTES = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + b"\x00" * 17
JPEG_BYTES = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00" + b"\x00" * 32
TIFF_LE_BYTES = b"II*\x00\x08\x00\x00\x00" + b"\x00" * 32
TIFF_BE_BYTES = b"MM\x00*\x00\x00\x00\x08" + b"\x00" * 32
EXE_BYTES = b"MZ\x90\x00\x03\x00\x00\x00\x04\x00\x00\x00\xff\xff" + b"\x00" * 64


def _zip(entries: dict[str, bytes | str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return buf.getvalue()


def _ooxml(main_part: str, **extra: bytes | str) -> bytes:
    return _zip({"[Content_Types].xml": "<Types/>", "_rels/.rels": "<Relationships/>", main_part: "<x/>", **extra})


DOCX_BYTES = _ooxml("word/document.xml")
XLSX_BYTES = _ooxml("xl/workbook.xml")


@pytest.mark.parametrize(
    ("data", "mime", "extension"),
    [
        (PDF_BYTES, PDF, "pdf"),
        (PNG_BYTES, PNG, "png"),
        (JPEG_BYTES, JPEG, "jpg"),
        (TIFF_LE_BYTES, TIFF, "tiff"),
        (TIFF_BE_BYTES, TIFF, "tiff"),
        (DOCX_BYTES, DOCX, "docx"),
        (XLSX_BYTES, XLSX, "xlsx"),
        (b"name,amount\nacme,100\nglobex,200\n", CSV, "csv"),
    ],
)
def test_detects_allowed_types(data: bytes, mime: str, extension: str):
    meta = detect_file_type(data)
    assert meta.mime == mime
    assert meta.extension == extension
    assert meta.size == len(data)


def test_is_image():
    assert detect_file_type(PNG_BYTES).is_image
    assert not detect_file_type(PDF_BYTES).is_image


def test_declared_content_type_is_irrelevant_exe_renamed_to_pdf_is_rejected():
    with pytest.raises(UnsupportedFileTypeError) as exc_info:
        detect_file_type(EXE_BYTES, filename="invoice.pdf")
    assert exc_info.value.detected_mime == "application/x-msdownload"


def test_plain_zip_is_rejected():
    with pytest.raises(UnsupportedFileTypeError) as exc_info:
        detect_file_type(_zip({"hello.txt": "hi"}))
    assert exc_info.value.detected_mime == "application/zip"


def test_zip_with_office_paths_but_no_content_types_is_rejected():
    with pytest.raises(UnsupportedFileTypeError):
        detect_file_type(_zip({"word/document.xml": "<x/>"}))


def test_macro_enabled_document_is_rejected():
    data = _ooxml("word/document.xml", **{"word/vbaProject.bin": b"\x00" * 10})
    with pytest.raises(UnsupportedFileTypeError, match="Macro"):
        detect_file_type(data)


def test_zip_bomb_is_rejected():
    data = _ooxml("word/document.xml", **{"word/media/big.bin": b"\x00" * 50_000})
    with pytest.raises(UnsupportedFileTypeError, match="expands"):
        detect_file_type(data, max_uncompressed_size=10_000)


def test_corrupt_zip_is_rejected():
    with pytest.raises(UnsupportedFileTypeError):
        detect_file_type(DOCX_BYTES[:40])


def test_too_large_is_rejected_before_sniffing():
    with pytest.raises(FileTooLargeError):
        detect_file_type(PDF_BYTES, max_size=10)


def test_empty_file_is_rejected():
    with pytest.raises(UnsupportedFileTypeError):
        detect_file_type(b"")


@pytest.mark.parametrize(
    "data",
    [
        b"\xef\xbb\xbfname;amount\nacme;100\nglobex;200\n",
        b"a\tb\tc\n1\t2\t3\n",
        b'name,note\n"acme","hello, world"\n',
    ],
)
def test_csv_variants_are_accepted(data: bytes):
    assert detect_file_type(data).mime == CSV


@pytest.mark.parametrize(
    "data",
    [
        b"just some plain text without structure",
        b"\xef\xbb\xbfMeeting notes\n\n- revenue up\t12%\r\n- caf\xc3\xa9 opened\n\x0cpage two\n",
    ],
)
def test_plain_utf8_text_is_txt(data: bytes):
    meta = detect_file_type(data, filename="notes.txt")
    assert meta.mime == TXT
    assert meta.extension == "txt"


def test_delimited_text_named_txt_is_txt():
    assert detect_file_type(b"name,amount\nacme,100\n", filename="export.txt").mime == TXT


def test_prose_named_csv_is_rejected():
    with pytest.raises(FileTypeMismatchError):
        detect_file_type(b"just some plain text without structure", filename="data.csv")


@pytest.mark.parametrize(
    "data",
    [
        b"name,amount\nacme,100\x00\n",
        b"name,amount\n\xff\xfe\xfa,100\n",
        b"\x01\x02\x03\x04\x05\x06\x07\x08",
        b"looks like text\x1b[31m but has escapes",
    ],
)
def test_binary_and_non_utf8_text_are_rejected(data: bytes):
    with pytest.raises(UnsupportedFileTypeError):
        detect_file_type(data)


def test_allowed_narrows_the_allowlist():
    assert detect_file_type(PDF_BYTES, allowed={PDF, TXT}).mime == PDF
    with pytest.raises(UnsupportedFileTypeError) as exc_info:
        detect_file_type(PNG_BYTES, allowed={PDF, TXT})
    assert exc_info.value.detected_mime == PNG


def test_large_csv_is_sampled():
    data = b"id,value\n" + b"".join(f"{i},{i * 2}\n".encode() for i in range(20_000))
    assert detect_file_type(data).mime == CSV


@pytest.mark.parametrize(
    ("data", "filename"),
    [
        (PDF_BYTES, "report.pdf"),
        (PDF_BYTES, "REPORT.PDF"),
        (JPEG_BYTES, "photo.jpeg"),
        (JPEG_BYTES, "photo.JPG"),
        (TIFF_LE_BYTES, "scan.tif"),
        (PDF_BYTES, "no_extension"),
    ],
)
def test_matching_or_missing_extension_is_accepted(data: bytes, filename: str):
    detect_file_type(data, filename=filename)


@pytest.mark.parametrize(
    ("data", "filename"),
    [
        (PNG_BYTES, "report.pdf"),
        (XLSX_BYTES, "letter.docx"),
        (PDF_BYTES, "data.csv"),
    ],
)
def test_extension_contradicting_bytes_is_rejected(data: bytes, filename: str):
    with pytest.raises(FileTypeMismatchError):
        detect_file_type(data, filename=filename)
