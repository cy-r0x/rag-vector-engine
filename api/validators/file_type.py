import csv
import io
import zipfile
from dataclasses import dataclass
from pathlib import PurePath

import filetype

from common.config import get_settings

PDF = "application/pdf"
PNG = "image/png"
JPEG = "image/jpeg"
TIFF = "image/tiff"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
CSV = "text/csv"
ZIP = "application/zip"

CANONICAL_EXTENSION = {PDF: "pdf", PNG: "png", JPEG: "jpg", TIFF: "tiff", DOCX: "docx", XLSX: "xlsx", CSV: "csv"}
ACCEPTED_EXTENSIONS = {
    PDF: {"pdf"},
    PNG: {"png"},
    JPEG: {"jpg", "jpeg"},
    TIFF: {"tif", "tiff"},
    DOCX: {"docx"},
    XLSX: {"xlsx"},
    CSV: {"csv"},
}
IMAGE_MIMES = frozenset({PNG, JPEG, TIFF})

_SIMPLE_TYPES = frozenset({PDF, PNG, JPEG, TIFF})
_ZIP_LIKE = frozenset({ZIP, DOCX, XLSX})
_MAX_ZIP_ENTRIES = 10_000
_CSV_SAMPLE_BYTES = 64 * 1024
_CSV_SAMPLE_ROWS = 20
_CSV_DELIMITERS = ",;\t|"


class FileValidationError(Exception):
    def __init__(self, detail: str, detected_mime: str | None = None) -> None:
        super().__init__(detail)
        self.detail = detail
        self.detected_mime = detected_mime


class FileTooLargeError(FileValidationError):
    pass


class UnsupportedFileTypeError(FileValidationError):
    pass


class FileTypeMismatchError(FileValidationError):
    pass


@dataclass(frozen=True)
class FileMetadata:
    mime: str
    extension: str
    size: int

    @property
    def is_image(self) -> bool:
        return self.mime in IMAGE_MIMES


def detect_file_type(
    data: bytes,
    *,
    filename: str | None = None,
    max_size: int | None = None,
    max_uncompressed_size: int | None = None,
) -> FileMetadata:
    """Identify an upload from its bytes and check it against the allowlist.

    The client's Content-Type is never consulted. `filename` is only used to reject
    uploads whose extension contradicts the detected type.
    """
    settings = get_settings()
    max_size = settings.max_upload_bytes if max_size is None else max_size
    max_uncompressed_size = (
        settings.max_uncompressed_bytes if max_uncompressed_size is None else max_uncompressed_size
    )

    if len(data) > max_size:
        raise FileTooLargeError(f"File exceeds the maximum size of {max_size} bytes")
    if not data:
        raise UnsupportedFileTypeError("Empty file")

    mime = _sniff(data, max_uncompressed_size)
    _check_extension(filename, mime)
    return FileMetadata(mime=mime, extension=CANONICAL_EXTENSION[mime], size=len(data))


def _sniff(data: bytes, max_uncompressed_size: int) -> str:
    kind = filetype.guess(data)
    if kind is None:
        if _looks_like_csv(data):
            return CSV
        raise UnsupportedFileTypeError("Unsupported or mismatched file type")
    if kind.mime in _SIMPLE_TYPES:
        return kind.mime
    if kind.mime in _ZIP_LIKE:
        return _inspect_ooxml(data, max_uncompressed_size)
    raise UnsupportedFileTypeError("Unsupported or mismatched file type", detected_mime=kind.mime)


def _inspect_ooxml(data: bytes, max_uncompressed_size: int) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
    except (zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise UnsupportedFileTypeError("Corrupt ZIP archive", detected_mime=ZIP) from exc

    if len(entries) > _MAX_ZIP_ENTRIES:
        raise UnsupportedFileTypeError("Archive has too many entries", detected_mime=ZIP)
    if sum(entry.file_size for entry in entries) > max_uncompressed_size:
        raise UnsupportedFileTypeError("Archive expands beyond the allowed size", detected_mime=ZIP)

    names = {entry.filename for entry in entries}
    if any(PurePath(name).name.lower() == "vbaproject.bin" for name in names):
        raise UnsupportedFileTypeError("Macro-enabled documents are not allowed", detected_mime=ZIP)
    if "[Content_Types].xml" in names:
        if "word/document.xml" in names:
            return DOCX
        if "xl/workbook.xml" in names:
            return XLSX
    raise UnsupportedFileTypeError("Unsupported or mismatched file type", detected_mime=ZIP)


def _looks_like_csv(data: bytes) -> bool:
    sample = data[:_CSV_SAMPLE_BYTES]
    if b"\x00" in sample:
        return False
    if len(data) > _CSV_SAMPLE_BYTES:
        sample = sample[: sample.rfind(b"\n") + 1]
    try:
        text = sample.decode("utf-8-sig")
    except UnicodeDecodeError:
        return False

    try:
        dialect = csv.Sniffer().sniff(text, delimiters=_CSV_DELIMITERS)
    except csv.Error:
        return False

    rows = [row for row in csv.reader(io.StringIO(text), dialect) if row][:_CSV_SAMPLE_ROWS]
    if not rows:
        return False
    widths = {len(row) for row in rows}
    return len(widths) == 1 and widths.pop() >= 2


def _check_extension(filename: str | None, mime: str) -> None:
    if not filename:
        return
    suffix = PurePath(filename).suffix.lower().lstrip(".")
    if suffix and suffix not in ACCEPTED_EXTENSIONS[mime]:
        raise FileTypeMismatchError(
            f"File extension '.{suffix}' does not match the detected type", detected_mime=mime
        )
