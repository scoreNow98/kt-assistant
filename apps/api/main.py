import logging
import os
from contextlib import asynccontextmanager
from typing import Annotated

import httpx
import psycopg
from fastapi import FastAPI, HTTPException, Path, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

import rag

log = logging.getLogger("kt")
MAX_UPLOAD_BYTES = 20 * 1024 * 1024


@asynccontextmanager
async def lifespan(app: FastAPI):
    rag.init_schema()
    yield


app = FastAPI(title="KT Assistant API", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[os.getenv("WEB_ORIGIN", "http://localhost:3000")],
    allow_methods=["*"],
    allow_headers=["*"],
)

Workspace = Annotated[str, Path(pattern=r"^[a-z0-9-]{1,50}$")]


class Question(BaseModel):
    question: str = Field(min_length=1, max_length=2000)


@app.exception_handler(httpx.HTTPError)
def model_server_error(request, exc):
    log.error("model server call failed: %r", exc)
    return JSONResponse(
        status_code=502,
        content={"detail": f"Cannot use the model server at {rag.LLM_BASE_URL}. Is Ollama running, and are the models pulled?"},
    )


@app.get("/health")
def health():
    """Says whether the API can reach Postgres and whether pgvector is installed."""
    try:
        with psycopg.connect(os.environ["DATABASE_URL"], connect_timeout=2) as conn:
            row = conn.execute(
                "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
            ).fetchone()
        return {"api": "ok", "db": "ok", "pgvector": row[0] if row else None}
    except Exception:
        log.exception("database check failed")  # details stay in the logs, not the response
        return {"api": "ok", "db": "down", "pgvector": None}


@app.post("/workspaces/{workspace}/documents")
def upload(workspace: Workspace, file: UploadFile):
    data = file.file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "file is larger than 20 MB")
    try:
        return rag.ingest(workspace, file.filename or "upload", data)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/workspaces/{workspace}/documents")
def documents(workspace: Workspace):
    with rag.connect() as conn:
        rows = conn.execute(
            "SELECT d.name, count(c.id) FROM documents d LEFT JOIN chunks c ON c.document_id = d.id "
            "WHERE d.workspace = %s GROUP BY d.id ORDER BY d.name",
            (workspace,),
        ).fetchall()
    return [{"name": name, "chunks": chunks} for name, chunks in rows]


@app.post("/workspaces/{workspace}/ask")
def ask(workspace: Workspace, body: Question):
    return rag.ask(workspace, body.question)
