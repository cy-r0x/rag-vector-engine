import json

import pytest

from workers.chunker import chunk_markdown, to_jsonl


def _assert_spans_match(markdown: str, chunks) -> None:
    for index, chunk in enumerate(chunks):
        assert chunk.index == index
        assert chunk.text == markdown[chunk.start : chunk.end]


def test_short_document_is_one_chunk():
    markdown = "# Notes\n\nRevenue grew 12%.\n"
    chunks = chunk_markdown(markdown, size=1000, overlap=150)
    assert [(c.text, c.start, c.end) for c in chunks] == [("# Notes\n\nRevenue grew 12%.", 0, 26)]


def test_empty_document_has_no_chunks():
    assert chunk_markdown(" \n\n \n", size=1000, overlap=150) == []


def test_paragraphs_are_packed_without_being_split():
    paragraphs = [f"Paragraph {n} " + "word " * 20 for n in range(10)]
    markdown = "\n\n".join(p.strip() for p in paragraphs)

    chunks = chunk_markdown(markdown, size=300, overlap=0)

    _assert_spans_match(markdown, chunks)
    assert len(chunks) > 1
    assert all(len(c.text) <= 300 for c in chunks)
    joined = "\n\n".join(c.text for c in chunks)
    assert joined == markdown
    for chunk in chunks:
        assert chunk.text.startswith("Paragraph") and chunk.text.endswith("word")


def test_consecutive_chunks_overlap_on_word_boundaries():
    markdown = "\n\n".join(f"Section {n}: " + " ".join(f"w{n}_{i}" for i in range(30)) for n in range(6))

    chunks = chunk_markdown(markdown, size=400, overlap=60)

    _assert_spans_match(markdown, chunks)
    assert all(len(c.text) <= 400 for c in chunks)
    for previous, current in zip(chunks, chunks[1:]):
        assert current.start < previous.end, "expected an overlap"
        assert previous.end - current.start <= 60
        assert current.start == 0 or markdown[current.start - 1].isspace()


def test_long_paragraph_is_split_at_whitespace():
    markdown = " ".join(f"token{i}" for i in range(400))

    chunks = chunk_markdown(markdown, size=200, overlap=0)

    _assert_spans_match(markdown, chunks)
    assert all(len(c.text) <= 200 for c in chunks)
    assert " ".join(c.text for c in chunks) == markdown


def test_unbroken_text_is_hard_split():
    markdown = "x" * 450

    chunks = chunk_markdown(markdown, size=200, overlap=50)

    assert [len(c.text) for c in chunks] == [200, 200, 50]
    assert "".join(c.text for c in chunks) == markdown


@pytest.mark.parametrize(("size", "overlap"), [(0, 0), (100, 100), (100, -1)])
def test_rejects_invalid_settings(size: int, overlap: int):
    with pytest.raises(ValueError):
        chunk_markdown("text", size=size, overlap=overlap)


def test_jsonl_has_one_object_per_line():
    chunks = chunk_markdown("Ünïcode paragraph one.\n\nParagraph two.", size=25, overlap=0)

    lines = to_jsonl(chunks).decode("utf-8").splitlines()

    assert [json.loads(line) for line in lines] == [
        {"index": 0, "text": "Ünïcode paragraph one.", "start": 0, "end": 22},
        {"index": 1, "text": "Paragraph two.", "start": 24, "end": 38},
    ]
