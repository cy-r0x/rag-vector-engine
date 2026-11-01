"""Simple chunker: packs whole paragraphs of the extracted Markdown into size-limited, overlapping chunks.

Paragraphs are blank-line separated; a paragraph longer than the chunk size is split at whitespace.
Headers and tables are not treated specially yet (a table may be split across chunks).
"""

import json
import re
from collections.abc import Iterator
from dataclasses import asdict, dataclass

_PARAGRAPH_BREAK = re.compile(r"\n[ \t]*\n\s*")
_WHITESPACE = re.compile(r"\s")
_NON_WHITESPACE = re.compile(r"\S")


@dataclass(frozen=True)
class Chunk:
    """`text` is `markdown[start:end]` of the document's extracted Markdown."""

    index: int
    text: str
    start: int
    end: int


def chunk_markdown(markdown: str, *, size: int, overlap: int) -> list[Chunk]:
    if size <= 0:
        raise ValueError("size must be positive")
    if not 0 <= overlap < size:
        raise ValueError("overlap must be at least 0 and smaller than size")

    units = [piece for span in _paragraphs(markdown) for piece in _split_long(markdown, *span, size=size)]
    chunks: list[Chunk] = []
    next_start: int | None = None
    i = 0
    while i < len(units):
        start = units[i][0] if next_start is None or units[i][1] - next_start > size else next_start
        end = units[i][1]
        i += 1
        while i < len(units) and units[i][1] - start <= size:
            end = units[i][1]
            i += 1
        chunks.append(Chunk(index=len(chunks), text=markdown[start:end], start=start, end=end))
        next_start = _overlap_start(markdown, start, end, overlap)
    return chunks


def to_jsonl(chunks: list[Chunk]) -> bytes:
    return "".join(json.dumps(asdict(chunk), ensure_ascii=False) + "\n" for chunk in chunks).encode("utf-8")


def _paragraphs(text: str) -> Iterator[tuple[int, int]]:
    start = 0
    for match in _PARAGRAPH_BREAK.finditer(text):
        yield from _trimmed(text, start, match.start())
        start = match.end()
    yield from _trimmed(text, start, len(text))


def _trimmed(text: str, start: int, end: int) -> Iterator[tuple[int, int]]:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    if start < end:
        yield start, end


def _split_long(text: str, start: int, end: int, *, size: int) -> Iterator[tuple[int, int]]:
    """Split one paragraph into pieces of at most `size`, preferring to cut at whitespace."""
    while end - start > size:
        window = text[start : start + size + 1]
        cut = max((m.start() for m in _WHITESPACE.finditer(window)), default=-1)
        cut = start + cut if cut > size // 2 else start + size
        yield from _trimmed(text, start, cut)
        match = _NON_WHITESPACE.search(text, cut, end)
        if match is None:
            return
        start = match.start()
    yield from _trimmed(text, start, end)


def _overlap_start(text: str, start: int, end: int, overlap: int) -> int | None:
    """Where the next chunk starts so it repeats up to `overlap` characters, snapped to the start of a word."""
    if overlap == 0:
        return None
    position = max(start, end - overlap)
    if position > start and not text[position - 1].isspace():
        boundary = _WHITESPACE.search(text, position, end)
        if boundary is None:
            return None
        position = boundary.start()
    word = _NON_WHITESPACE.search(text, position, end)
    return word.start() if word and word.start() > start else None
