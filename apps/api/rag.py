"""Parse, chunk, embed, search and answer. No web code in this file."""
import hashlib
import io
import json
import os
import re
import unicodedata
from pathlib import Path

import httpx

# Any OpenAI-compatible server works: Ollama locally now, a paid API later.
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "http://host.docker.internal:11434/v1")
LLM_API_KEY = os.getenv("LLM_API_KEY", "ollama")
EMBED_MODEL = os.getenv("EMBED_MODEL", "bge-m3")
CHAT_MODEL = os.getenv("CHAT_MODEL", "gemma3:4b")

SUFFIXES = {".md", ".mdx", ".txt", ".pdf", ".docx"}
NOT_FOUND = "I could not find this in the documents."
HEADING = re.compile(r"#{1,6}\s+(.+?)(?:\s*\{\s*#[^}]*\})?\s*$")  # drops a trailing {#anchor}


def extract(name: str, data: bytes) -> list[tuple[int | None, str]]:
    """Returns (page number or None, text) parts of a file."""
    suffix = Path(name).suffix.lower()
    if suffix not in SUFFIXES:
        raise ValueError(f"unsupported file type: {suffix or name}")
    if suffix == ".pdf":
        from pypdf import PdfReader

        pages = PdfReader(io.BytesIO(data)).pages
        return [(i, page.extract_text() or "") for i, page in enumerate(pages, 1)]
    if suffix == ".docx":
        from docx import Document

        lines = []
        for p in Document(io.BytesIO(data)).paragraphs:
            is_heading = p.style.name.startswith("Heading")
            lines.append(f"# {p.text}" if is_heading and p.text.strip() else p.text)
        return [(None, "\n\n".join(lines))]
    return [(None, data.decode("utf-8", errors="replace"))]


def chunk(text: str, max_chars: int = 1500) -> list[tuple[str, str]]:
    """Splits text into (nearest heading, text) pieces of roughly max_chars, breaking at headings and blank lines."""
    out, heading, buf, in_code = [], "", "", False

    def flush():
        nonlocal buf
        if buf.strip():
            out.append((heading, buf.strip()))
        buf = ""

    lines = [raw[i : i + max_chars] for raw in text.splitlines() for i in range(0, len(raw) or 1, max_chars)]
    for line in lines:  # a line longer than max_chars was cut into several above
        if line.lstrip().startswith("```"):
            in_code = not in_code
        m = None if in_code else HEADING.match(line)  # "# comment" inside code is not a heading
        if m:
            flush()
            heading = m.group(1).strip()
            continue
        if not line.strip() and len(buf) >= max_chars:
            flush()
            continue
        buf += line + "\n"
        if len(buf) >= max_chars * 2:  # text with no blank lines
            flush()
    flush()
    return out


def _headers() -> dict:
    return {"Authorization": f"Bearer {LLM_API_KEY}"}


def _post(path: str, body: dict) -> dict:
    r = httpx.post(f"{LLM_BASE_URL}{path}", json=body, headers=_headers(), timeout=300)
    r.raise_for_status()
    return r.json()


def embed(texts: list[str]) -> list[list[float]]:
    vectors = []
    for i in range(0, len(texts), 32):
        data = _post("/embeddings", {"model": EMBED_MODEL, "input": texts[i : i + 32]})["data"]
        vectors += [d["embedding"] for d in sorted(data, key=lambda d: d["index"])]
    return vectors


def _messages(system: str, user: str) -> dict:
    return {
        "model": CHAT_MODEL,
        "temperature": 0,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
    }


def chat(system: str, user: str) -> str:
    return _post("/chat/completions", _messages(system, user))["choices"][0]["message"]["content"].strip()


def chat_stream(system: str, user: str):
    """Yields the answer in pieces as the model writes it."""
    body = _messages(system, user) | {"stream": True}
    with httpx.stream("POST", f"{LLM_BASE_URL}/chat/completions", json=body, headers=_headers(), timeout=300) as r:
        r.raise_for_status()
        for line in r.iter_lines():
            if not line.startswith("data: ") or line == "data: [DONE]":
                continue
            for choice in json.loads(line[6:]).get("choices") or []:
                if piece := (choice.get("delta") or {}).get("content"):
                    yield piece


def _vector(v: list[float]) -> str:
    return "[" + ",".join(map(str, v)) + "]"


def ingest(conn, workspace_id: int, name: str, data: bytes) -> dict:
    """Stores one file as chunks. The same file twice in a workspace is skipped.

    conn decides who is asking: pass a db.user_db() connection for a signed-in user.
    """
    pieces = [
        (page, heading, text)
        for page, part in extract(name, data)
        for heading, text in chunk(part)
    ]
    if not pieces:
        raise ValueError("no readable text in this file (scanned PDFs are not supported yet)")
    # The caller's transaction covers all of this: a failed embedding leaves nothing behind.
    row = conn.execute(
        "INSERT INTO documents (workspace_id, name, sha256) VALUES (%s, %s, %s) "
        "ON CONFLICT DO NOTHING RETURNING id",
        (workspace_id, name, hashlib.sha256(data).hexdigest()),
    ).fetchone()
    if row is None:
        return {"name": name, "status": "duplicate", "chunks": 0}
    vectors = embed([f"{name} > {heading}\n{text}" for _, heading, text in pieces])
    conn.cursor().executemany(
        "INSERT INTO chunks (document_id, workspace_id, heading, page, text, embed_model, embedding) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s::vector)",
        [
            (row[0], workspace_id, heading, page, text, EMBED_MODEL, _vector(v))
            for (page, heading, text), v in zip(pieces, vectors, strict=True)
        ],
    )
    return {"name": name, "status": "ready", "chunks": len(pieces)}


def _keywords(question: str) -> str:
    """Any word may match. Only letters, digits, _ and accent or vowel marks reach to_tsquery, so its
    syntax cannot break. Marks are kept because Python's \\w would split Hindi words at their vowel signs."""
    kept = "".join(c if c.isalnum() or c == "_" or unicodedata.category(c).startswith("M") else " " for c in question)
    return " | ".join(kept.split()[:50])


def search(conn, workspace_id: int, question: str, k: int = 8, keyword: bool = True) -> list[dict]:
    """The k best chunks: vector and keyword results merged by reciprocal rank fusion.

    The workspace filter is the tenant boundary: never remove it.
    """
    rows = conn.execute(
        """
        WITH vec AS (
          SELECT id, row_number() OVER (ORDER BY embedding <=> %(v)s::vector) AS r FROM chunks
          WHERE workspace_id = %(w)s AND embed_model = %(m)s
          ORDER BY embedding <=> %(v)s::vector LIMIT 30
        ), kw AS (
          SELECT id, row_number() OVER (ORDER BY ts_rank_cd(tsv, q) DESC) AS r
          FROM chunks, to_tsquery('english', %(q)s) q
          WHERE %(use_kw)s AND workspace_id = %(w)s AND embed_model = %(m)s AND tsv @@ q
          ORDER BY ts_rank_cd(tsv, q) DESC LIMIT 30
        ), fused AS (
          SELECT id, sum(1.0 / (60 + r)) AS score FROM (SELECT * FROM vec UNION ALL SELECT * FROM kw) hits GROUP BY id
        )
        SELECT d.name, c.heading, c.page, c.text FROM fused f
        JOIN chunks c ON c.id = f.id JOIN documents d ON d.id = c.document_id
        ORDER BY f.score DESC, c.id LIMIT %(k)s
        """,
        {
            "v": _vector(embed([question])[0]),
            "w": workspace_id,
            "m": EMBED_MODEL,
            "q": _keywords(question),
            "use_kw": keyword,
            "k": k,
        },
    ).fetchall()
    return [
        {"n": n, "document": name, "heading": heading, "page": page, "text": text}
        for n, (name, heading, page, text) in enumerate(rows, 1)
    ]


def rewrite(question: str, history: list[dict]) -> str:
    """Turns a follow-up ("and how do I restart it?") into a question that stands on its own."""
    if not history:
        return question
    convo = "\n\n".join(f"Q: {h['question']}\nA: {h['answer'][:600]}" for h in history[-2:])
    out = chat(
        "You rewrite the user's last question so it can be understood without the conversation. "
        "Keep every name and technical term. If it already stands on its own, return it unchanged. "
        "Reply with the question only.",
        f"{convo}\n\nLast question: {question}",
    ).strip().strip('"')
    return out if 0 < len(out) <= 500 else question


def answer_stream(sources: list[dict], question: str):
    """Yields the answer in pieces. With no sources, says so without calling the model."""
    if not sources:
        yield NOT_FOUND
        return
    system = (
        "You answer questions for a software team using only the numbered sources below. "
        "Put the source number right after each sentence it supports, like this [1]. "
        "Never list source numbers at the start of the answer. "
        f"If the sources do not contain the answer, reply exactly: {NOT_FOUND}"
    )
    context = "\n\n".join(f"[{s['n']}] {s['document']} > {s['heading']}\n{s['text']}" for s in sources)
    yield from chat_stream(system, f"{context}\n\nQuestion: {question}")
