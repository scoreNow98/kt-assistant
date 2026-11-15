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
