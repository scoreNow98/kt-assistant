import os
import uuid

import pytest

import rag


def test_chunk_splits_on_headings_but_not_on_code_comments():
    text = "intro\n\n# Setup\ninstall it\n```python\n# not a heading\nx = 1\n```\n## Run\nstart it\n"
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
    with pytest.raises(ValueError):
        rag.extract("photo.png", b"x")


def fake_embed(texts):
    """Word-count vectors: texts sharing words land close together. No model needed."""
    vectors = []
    for t in texts:
        v = [0.0] * 64
        for word in t.lower().split():
            v[sum(map(ord, word)) % 64] += 1.0
        vectors.append(v)
    return vectors


@pytest.mark.skipif("DATABASE_URL" not in os.environ, reason="needs Postgres with pgvector")
def test_ingest_search_ask_and_workspace_isolation(monkeypatch):
    monkeypatch.setattr(rag, "embed", fake_embed)
    monkeypatch.setattr(rag, "chat", lambda system, user: "Restart with make restart [1]")
    rag.init_schema()
    a, b = f"test-a-{uuid.uuid4().hex[:8]}", f"test-b-{uuid.uuid4().hex[:8]}"
    try:
        doc_a = b"# Deploy\nRestart the payment service with make restart.\n"
        assert rag.ingest(a, "runbook.md", doc_a) == {"name": "runbook.md", "status": "ready", "chunks": 1}
        assert rag.ingest(a, "copy.md", doc_a)["status"] == "duplicate"
        rag.ingest(b, "secret.md", b"# Deploy\nRestart the billing service with the secret script.\n")

        hits = rag.search(a, "how do I restart the service?")
        assert [h["document"] for h in hits] == ["runbook.md"]  # nothing from workspace b
        assert hits[0]["heading"] == "Deploy"

        answer = rag.ask(a, "how do I restart the service?")
        assert answer["answer"].endswith("[1]") and answer["sources"][0]["n"] == 1
        assert rag.ask(f"empty-{a}", "anything") == {"answer": rag.NOT_FOUND, "sources": []}
    finally:
        with rag.connect() as conn:
            conn.execute("DELETE FROM documents WHERE workspace IN (%s, %s)", (a, b))
