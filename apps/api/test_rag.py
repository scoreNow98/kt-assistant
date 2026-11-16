import rag


def test_chunk_splits_on_headings_but_not_on_code_comments():
    # min_chars=0 turns merging off, to see the splits. Headings are now a path: "Run" sits under "Setup".
    text = "intro\n\n# Setup { #setup }\ninstall it\n```python\n# not a heading\nx = 1\n```\n## Run\nstart it\n"
    assert rag.chunk(text, min_chars=0) == [
        ("", "intro"),
        ("Setup", "install it\n```python\n# not a heading\nx = 1\n```"),
        ("Setup > Run", "start it"),
    ]


def test_chunk_heading_path_follows_levels():
    text = "# A\n" + "a" * 400 + "\n## B\n" + "b" * 400 + "\n### C\n" + "c" * 400 + "\n## D\n" + "d" * 400 + "\n# E\n" + "e" * 400
    assert [h for h, _ in rag.chunk(text)] == ["A", "A > B", "A > B > C", "A > D", "E"]


def test_chunk_merges_short_sections_into_the_next_keeping_their_heading():
    text = "# Ports\nUse 8080.\n# Env\nSet DEBUG=1.\n# Deploy\n" + "Run make deploy. " * 30 + "\n# Last\nshort"
    assert rag.chunk(text) == [
        ("Deploy", "Ports\nUse 8080.\n\nEnv\nSet DEBUG=1.\n\n" + ("Run make deploy. " * 30).strip()),
        ("Last", "short"),  # nothing follows, so it stays
    ]


def test_chunk_uses_front_matter_title_and_drops_the_block():
    text = '---\ntitle: "Quick start"\nsidebarTitle: Start\n---\n' + "x" * 400 + "\n## Install\n" + "y" * 400
    assert rag.chunk(text) == [("Quick start", "x" * 400), ("Quick start > Install", "y" * 400)]
    # A "# Title" repeating the front matter title is not doubled.
    assert [h for h, _ in rag.chunk("---\ntitle: Quick start\n---\n# Quick start\n" + "x" * 400)] == ["Quick start"]
    # Without a closing line it is not front matter.
    assert rag.chunk("---\ntitle: x\n" + "z" * 400)[0][1].startswith("---")


def test_not_found_needs_a_valid_citation():
    sources = [{"n": 1, "document": "a.md"}, {"n": 2, "document": "b.md"}]
    assert not rag.is_not_found("Restart it [2].", sources)
    assert rag.is_not_found("Restart it.", sources)  # no citation
    assert rag.is_not_found("Restart it [3].", sources)  # cites a source that was not shown
    assert rag.is_not_found("Sorry, I could not find this in the documents [1].", sources)
    assert rag.is_not_found(rag.NOT_FOUND, [])


def test_eval_answers_scoring():
    from eval_answers import score

    sources = [{"n": 1, "document": "tutorial/cors.md"}, {"n": 2, "document": "tutorial/testing.md"}]
    cors = {"workspace": "client-a", "question": "q", "expect": "tutorial/cors.md", "must_contain": ["CORSMiddleware"]}
    assert score(cors, "Add corsmiddleware [1].", sources) == (True, "ok")
    assert score(cors, "Add a middleware [1].", sources)[0] is False  # missing the word
    assert score(cors, "Add CORSMiddleware [2].", sources)[0] is False  # wrong document
    assert score(cors, "Add CORSMiddleware [1][5].", sources)[0] is False  # [5] was not a source
    assert score(cors, rag.NOT_FOUND, sources)[0] is False
    several = {"workspace": "client-a", "question": "q", "expect": ["a.md", "tutorial/testing.md"]}  # no must_contain
    assert score(several, "Use TestClient [2].", sources) == (True, "ok")
    unanswerable = {"workspace": "client-a", "question": "q", "expect": None}
    assert score(unanswerable, rag.NOT_FOUND, sources)[0] is True
    assert score(unanswerable, "Probably 42.", sources)[0] is True  # no citation counts as not found
    assert score(unanswerable, "It is 42 [1].", sources)[0] is False


def test_eval_suggests_max_distance_only_without_overlap():
    from eval import expected, suggest_max_distance

    assert suggest_max_distance([0.2, 0.3], [0.5, 0.6]) == 0.4
    assert suggest_max_distance([0.2, 0.55], [0.5, 0.6]) is None
    assert suggest_max_distance([0.2], []) is None
    assert expected({"expect": None}) == [] and expected({}) == [] and expected({"expect": "a.md"}) == ["a.md"]


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
