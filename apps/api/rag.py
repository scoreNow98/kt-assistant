"""Parse, chunk, embed, search and answer. No web code in this file."""
import hashlib
import io
import os
import re
from pathlib import Path

import httpx
import psycopg

# Any OpenAI-compatible server works: Ollama locally now, a paid API later.
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "http://host.docker.internal:11434/v1")
LLM_API_KEY = os.getenv("LLM_API_KEY", "ollama")
EMBED_MODEL = os.getenv("EMBED_MODEL", "bge-m3")
CHAT_MODEL = os.getenv("CHAT_MODEL", "gemma3:4b")

SUFFIXES = {".md", ".mdx", ".txt", ".pdf", ".docx"}
NOT_FOUND = "I could not find this in the documents."
HEADING = re.compile(r"#{1,6}\s+(.+)")


def connect():
    return psycopg.connect(os.environ["DATABASE_URL"])


def init_schema():
    with connect() as conn:
        conn.execute(Path(__file__).with_name("schema.sql").read_text())


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


def _post(path: str, body: dict) -> dict:
    r = httpx.post(
        f"{LLM_BASE_URL}{path}",
        json=body,
        headers={"Authorization": f"Bearer {LLM_API_KEY}"},
        timeout=300,
    )
    r.raise_for_status()
    return r.json()


def embed(texts: list[str]) -> list[list[float]]:
    vectors = []
    for i in range(0, len(texts), 32):
        data = _post("/embeddings", {"model": EMBED_MODEL, "input": texts[i : i + 32]})["data"]
        vectors += [d["embedding"] for d in sorted(data, key=lambda d: d["index"])]
    return vectors


def chat(system: str, user: str) -> str:
    body = {
        "model": CHAT_MODEL,
        "temperature": 0,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
    }
    return _post("/chat/completions", body)["choices"][0]["message"]["content"].strip()


def _vector(v: list[float]) -> str:
    return "[" + ",".join(map(str, v)) + "]"


def ingest(workspace: str, name: str, data: bytes) -> dict:
    """Stores one file as chunks. The same file twice in a workspace is skipped."""
    pieces = [
        (page, heading, text)
        for page, part in extract(name, data)
        for heading, text in chunk(part)
    ]
    if not pieces:
        raise ValueError("no readable text in this file (scanned PDFs are not supported yet)")
    with connect() as conn:  # one transaction: a failed embedding leaves nothing behind
        row = conn.execute(
            "INSERT INTO documents (workspace, name, sha256) VALUES (%s, %s, %s) "
            "ON CONFLICT DO NOTHING RETURNING id",
            (workspace, name, hashlib.sha256(data).hexdigest()),
        ).fetchone()
        if row is None:
            return {"name": name, "status": "duplicate", "chunks": 0}
        vectors = embed([f"{name} > {heading}\n{text}" for _, heading, text in pieces])
        conn.cursor().executemany(
            "INSERT INTO chunks (document_id, workspace, heading, page, text, embed_model, embedding) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s::vector)",
            [
                (row[0], workspace, heading, page, text, EMBED_MODEL, _vector(v))
                for (page, heading, text), v in zip(pieces, vectors, strict=True)
            ],
        )
    return {"name": name, "status": "ready", "chunks": len(pieces)}


def search(workspace: str, question: str, k: int = 8) -> list[dict]:
    """The k closest chunks. The workspace filter is the tenant boundary: never remove it."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT d.name, c.heading, c.page, c.text FROM chunks c "
            "JOIN documents d ON d.id = c.document_id "
            "WHERE c.workspace = %s AND c.embed_model = %s "
            "ORDER BY c.embedding <=> %s::vector LIMIT %s",
            (workspace, EMBED_MODEL, _vector(embed([question])[0]), k),
        ).fetchall()
    return [
        {"n": n, "document": name, "heading": heading, "page": page, "text": text}
        for n, (name, heading, page, text) in enumerate(rows, 1)
    ]


def ask(workspace: str, question: str) -> dict:
    sources = search(workspace, question)
    if not sources:
        return {"answer": NOT_FOUND, "sources": []}
    system = (
        "You answer questions for a software team using only the numbered sources below. "
        "Cite every claim with its source number, like [1]. "
        f"If the sources do not contain the answer, reply exactly: {NOT_FOUND}"
    )
    context = "\n\n".join(f"[{s['n']}] {s['document']} > {s['heading']}\n{s['text']}" for s in sources)
    return {"answer": chat(system, f"{context}\n\nQuestion: {question}"), "sources": sources}
