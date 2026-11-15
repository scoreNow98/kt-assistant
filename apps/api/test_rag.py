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
