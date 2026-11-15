import rag


def test_chunk_splits_on_headings_but_not_on_code_comments():
    text = "intro\n\n# Setup { #setup }\ninstall it\n```python\n# not a heading\nx = 1\n```\n## Run\nstart it\n"
    assert rag.chunk(text) == [
        ("", "intro"),
        ("Setup", "install it\n```python\n# not a heading\nx = 1\n```"),
        ("Run", "start it"),
    ]


def test_chunk_caps_long_text():
    pieces = rag.chunk("word " * 2000, max_chars=500)  # one 10,000-character line
    assert len(pieces) > 5 and all(len(text) <= 1000 for _, text in pieces)
    pieces = rag.chunk("\n\n".join(["line " * 60] * 20), max_chars=500)
    assert len(pieces) > 5 and all(len(text) < 1000 for _, text in pieces)


def test_unsupported_file_type_is_rejected():
    import pytest

    with pytest.raises(ValueError):
        rag.extract("photo.png", b"x")


def test_chat_stream_reads_server_sent_events(monkeypatch):
    from contextlib import contextmanager

    events = [
        'data: {"choices":[{"delta":{"role":"assistant"}}]}',
        "",
        'data: {"choices":[{"delta":{"content":"Use make "}}]}',
        ": keep-alive comment",
        'data: {"choices":[{"delta":{"content":"restart [1]"}}]}',
        'data: {"choices":[]}',
        "data: [DONE]",
    ]

    class Response:
        def raise_for_status(self):
            pass

        def iter_lines(self):
            return iter(events)

    @contextmanager
    def fake_stream(method, url, **kwargs):
        assert kwargs["json"]["stream"] is True
        yield Response()

    monkeypatch.setattr(rag.httpx, "stream", fake_stream)
    assert "".join(rag.chat_stream("system", "user")) == "Use make restart [1]"


def test_keywords_keep_only_word_characters():
    assert rag._keywords("Why does E1234 & allow_origins :* fail?") == "Why | does | E1234 | allow_origins | fail"
    assert rag._keywords("कैसे चलाएं?") == "कैसे | चलाएं"


def test_unreadable_files_raise_value_error_not_a_crash():
    import pytest

    for name, data in [("broken.pdf", b"%PDF-1.4 not really"), ("broken.docx", b"PK\x03\x04 nope")]:
        with pytest.raises(ValueError):
            rag.extract(name, data)


def test_utf16_and_nul_bytes_are_read_as_text():
    assert rag.extract("notes.txt", "Restart with make restart".encode("utf-16")) == [(None, "Restart with make restart")]
    assert rag.extract("notes.txt", b"a\x00b") == [(None, "ab")]


def test_docx_tables_are_kept():
    import io

    from docx import Document

    doc = Document()
    doc.add_heading("Owners", level=1)
    table = doc.add_table(rows=1, cols=2)
    table.rows[0].cells[0].text, table.rows[0].cells[1].text = "billing", "Priya"
    buf = io.BytesIO()
    doc.save(buf)
    text = rag.extract("owners.docx", buf.getvalue())[0][1]
    assert "# Owners" in text and "billing | Priya" in text


def test_fit_keeps_best_sources_within_budget(monkeypatch):
    monkeypatch.setattr(rag, "MAX_CONTEXT_CHARS", 1000)
    sources = [{"n": i, "document": "d.md", "heading": "h", "text": "x" * 400} for i in range(1, 9)]
    assert [s["n"] for s in rag.fit(sources)] == [1, 2]
    assert [s["n"] for s in rag.fit([{"n": 1, "document": "d", "heading": "h", "text": "x" * 5000}])] == [1]


def test_cited_order_follows_the_text():
    assert rag.cited_order("First [3]. Then [1, 3]. Also [Source 2].") == [3, 1, 2]
