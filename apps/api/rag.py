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
CITED = re.compile(r"\[(?:Sources? )?(\d+(?:\s*,\s*\d+)*)\]")  # [1], [1, 3], [Source 1]
MAX_CHUNKS_PER_DOCUMENT = 2000
# What the model reads per question. Ollama's default context on most machines is about 4k tokens;
# ~10k characters of sources plus the prompt fits in 8k (set OLLAMA_CONTEXT_LENGTH=8192, see README).
MAX_CONTEXT_CHARS = int(os.getenv("MAX_CONTEXT_CHARS", "10000"))
HEADING = re.compile(r"(#{1,6})\s+(.+?)(?:\s*\{\s*#[^}]*\})?\s*$")  # drops a trailing {#anchor}
FRONT_MATTER = re.compile(r"\A\ufeff?---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.S)
FRONT_MATTER_TITLE = re.compile(r"^title:[ \t]*(.+?)[ \t]*$", re.M)
MIN_SECTION_CHARS = 300  # shorter sections are merged into the next one, so tiny chunks do not fill the results
# Optional relevance gate (cosine distance). Unset: every question gets the best passages. Tune with eval.py.
MAX_DISTANCE = float(os.environ["MAX_DISTANCE"]) if os.getenv("MAX_DISTANCE") else None


def extract(name: str, data: bytes) -> list[tuple[int | None, str]]:
    """Returns (page number or None, text) parts of a file. Any file it cannot read raises ValueError."""
    suffix = Path(name).suffix.lower()
    if suffix not in SUFFIXES:
        raise ValueError(f"unsupported file type: {suffix or name}")
    try:
        parts = _extract(suffix, data)
    except Exception as e:  # corrupt, encrypted or mislabelled files: the parsers raise many kinds of errors
        raise ValueError("could not read this file. Scanned or password-protected PDFs are not supported yet.") from e
    return [(page, text.replace("\x00", "")) for page, text in parts]  # Postgres text cannot hold NUL


def _extract(suffix: str, data: bytes) -> list[tuple[int | None, str]]:
    if suffix == ".pdf":
        from pypdf import PdfReader

        pages = PdfReader(io.BytesIO(data)).pages
        return [(i, page.extract_text() or "") for i, page in enumerate(pages, 1)]
    if suffix == ".docx":
        from docx import Document

        doc = Document(io.BytesIO(data))
        lines = []
        for p in doc.paragraphs:
            is_heading = p.style.name.startswith("Heading")
            lines.append(f"# {p.text}" if is_heading and p.text.strip() else p.text)
        # shortcut: tables come after all paragraphs, not in their place in the document.
        for table in doc.tables:
            lines += [" | ".join(cell.text.strip() for cell in row.cells) for row in table.rows]
        return [(None, "\n\n".join(lines))]
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):  # UTF-16, as Windows Notepad sometimes saves
        return [(None, data.decode("utf-16", errors="replace"))]
    return [(None, data.decode("utf-8", errors="replace"))]


def chunk(text: str, max_chars: int = 1500, min_chars: int = MIN_SECTION_CHARS) -> list[tuple[str, str]]:
    """Splits text into (heading path, text) pieces of roughly max_chars, breaking at headings and blank lines.

    The heading path is "Parent > Child" from the Markdown heading levels; a YAML front matter title is the
    root. A piece shorter than min_chars is merged into the next one, keeping its heading inside the text.
    """
    out, path, buf, in_code = [], [], "", False
    if m := FRONT_MATTER.match(text):
        text = text[m.end() :]
        if title := FRONT_MATTER_TITLE.search(m.group(1)):
            path = [(0, title.group(1).strip("\"'"))]

    def flush():
        nonlocal buf
        if buf.strip():
            out.append((" > ".join(h for _, h in path), buf.strip()))
        buf = ""

    lines = [raw[i : i + max_chars] for raw in text.splitlines() for i in range(0, len(raw) or 1, max_chars)]
    for line in lines:  # a line longer than max_chars was cut into several above
        if line.lstrip().startswith("```"):
            in_code = not in_code
        m = None if in_code else HEADING.match(line)  # "# comment" inside code is not a heading
        if m:
            flush()
            level, heading = len(m.group(1)), m.group(2).strip()
            path = [(lv, h) for lv, h in path if lv < level and h != heading]  # a "# Title" repeating the front matter title once
            path.append((level, heading))
            continue
        if not line.strip() and len(buf) >= max_chars:
            flush()
            continue
        buf += line + "\n"
        if len(buf) >= max_chars * 2:  # text with no blank lines
            flush()
    flush()

    merged, carry = [], ""
    for i, (heading, body) in enumerate(out):
        joined = f"{carry}\n\n{body}" if carry else body
        if len(joined) < min_chars and i + 1 < len(out):
            short_heading = heading.rsplit(" > ", 1)[-1]
            carry = f"{carry}\n\n{short_heading}\n{body}".strip() if short_heading else joined
            continue
        merged.append((heading, joined))
        carry = ""
    return merged


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


def ingest(conn, workspace_id: int, name: str, data: bytes, uploaded_by: int | None = None) -> dict:
    """Stores one file as chunks. The same file twice in a workspace is skipped.

    conn decides who is asking: pass a db.user_db() connection for a signed-in user, and their id as
    uploaded_by (the database refuses anything else). Command-line loads leave it empty.
    """
    pieces = [
        (page, heading, text)
        for page, part in extract(name, data)
        for heading, text in chunk(part)
    ]
    if not pieces:
        raise ValueError("no readable text in this file (scanned PDFs are not supported yet)")
    if len(pieces) > MAX_CHUNKS_PER_DOCUMENT:
        raise ValueError(f"this file is too long ({len(pieces)} sections). Split it into smaller files.")
    # The caller's transaction covers all of this: a failed embedding leaves nothing behind.
    sha256 = hashlib.sha256(data).hexdigest()
    # The same file again is a duplicate, unless it was embedded with another model: then embed it afresh.
    # (Removing the old copy needs an admin or its uploader; for anyone else it stays a duplicate.)
    conn.execute(
        "DELETE FROM documents d WHERE workspace_id = %s AND sha256 = %s "
        "AND NOT EXISTS (SELECT 1 FROM chunks c WHERE c.document_id = d.id AND c.embed_model = %s)",
        (workspace_id, sha256, EMBED_MODEL),
    )
    row = conn.execute(
        "INSERT INTO documents (workspace_id, name, sha256, uploaded_by) VALUES (%s, %s, %s, %s) "
        "ON CONFLICT DO NOTHING RETURNING id",
        (workspace_id, name, sha256, uploaded_by),
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
    """The k best chunks: vector and keyword results merged by reciprocal rank fusion. Each has the cosine
    distance of its vector match, or None when only the keywords found it.

    With MAX_DISTANCE set, vector matches farther than it are dropped first, and when none is left the
    result is empty (so the answer is "not found" without asking the model), even if keywords matched.
    The workspace filter is the tenant boundary: never remove it.
    """
    rows = conn.execute(
        """
        WITH vec AS (
          SELECT id, row_number() OVER (ORDER BY embedding <=> %(v)s::vector) AS r, embedding <=> %(v)s::vector AS distance
          FROM chunks
          WHERE workspace_id = %(w)s AND embed_model = %(m)s
            AND (%(max_d)s::float8 IS NULL OR embedding <=> %(v)s::vector <= %(max_d)s::float8)
          ORDER BY embedding <=> %(v)s::vector LIMIT 30
        ), kw AS (
          SELECT id, row_number() OVER (ORDER BY ts_rank_cd(tsv, q) DESC) AS r
          FROM chunks, to_tsquery('english', %(q)s) q
          WHERE %(use_kw)s AND workspace_id = %(w)s AND embed_model = %(m)s AND tsv @@ q
            AND (%(max_d)s::float8 IS NULL OR EXISTS (SELECT 1 FROM vec))
          ORDER BY ts_rank_cd(tsv, q) DESC LIMIT 30
        ), fused AS (
          SELECT id, sum(1.0 / (60 + r)) AS score
          FROM (SELECT id, r FROM vec UNION ALL SELECT id, r FROM kw) hits GROUP BY id
        )
        SELECT d.name, c.heading, c.page, c.text, v.distance FROM fused f
        JOIN chunks c ON c.id = f.id JOIN documents d ON d.id = c.document_id LEFT JOIN vec v ON v.id = f.id
        ORDER BY f.score DESC, c.id LIMIT %(k)s
        """,
        {
            "v": _vector(embed([question])[0]),
            "w": workspace_id,
            "m": EMBED_MODEL,
            "q": _keywords(question),
            "use_kw": keyword,
            "k": k,
            "max_d": MAX_DISTANCE,
        },
    ).fetchall()
    return [
        {"n": n, "document": name, "heading": heading, "page": page, "text": text,
         "distance": None if distance is None else round(float(distance), 4)}
        for n, (name, heading, page, text, distance) in enumerate(rows, 1)
    ]


def fit(sources: list[dict]) -> list[dict]:
    """The best-ranked sources that fit in MAX_CONTEXT_CHARS (always at least one), so the model reads
    every source it is shown and the page lists only those."""
    kept, used = [], 0
    for s in sources:
        used += len(s["text"]) + len(s["heading"]) + len(s["document"]) + 10
        if kept and used > MAX_CONTEXT_CHARS:
            break
        kept.append(s)
    return kept


def cited_order(answer: str) -> list[int]:
    """Source numbers in the order the answer first cites them."""
    order = []
    for group in CITED.findall(answer):
        for n in group.split(","):
            if int(n) not in order:
                order.append(int(n))
    return order


def is_not_found(answer: str, sources: list[dict]) -> bool:
    """True when the answer says it found nothing, or cites none of the sources it was given."""
    if NOT_FOUND.lower().removeprefix("i ").rstrip(".") in answer.lower():  # small models add words around it
        return True
    shown = {s["n"] for s in sources}
    return not any(n in shown for n in cited_order(answer))


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
        "Put the source number right after each sentence it supports, like this [1], or [1, 2] for two. "
        "Never list source numbers at the start of the answer. "
        f"If the sources do not contain the answer, reply exactly: {NOT_FOUND}"
    )
    context = "\n\n".join(f"[{s['n']}] {s['document']} > {s['heading']}\n{s['text']}" for s in sources)
    yield from chat_stream(system, f"{context}\n\nQuestion: {question}")
