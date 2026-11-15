import json
import logging
import os
from contextlib import asynccontextmanager
from typing import Annotated, Literal

import httpx
import psycopg
from fastapi import Cookie, Depends, FastAPI, HTTPException, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

import auth
import rag
from db import admin_db, init_schema, user_db

log = logging.getLogger("kt")
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
COOKIE = "kt_session"
# Set COOKIE_SECURE=1 when the site is served over https.
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "") == "1"


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_schema()
    yield


app = FastAPI(title="KT Assistant API", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[os.getenv("WEB_ORIGIN", "http://localhost:3000")],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def model_down_message() -> str:
    return f"Cannot use the model server at {rag.LLM_BASE_URL}. Is Ollama running, and are the models pulled?"


@app.exception_handler(httpx.HTTPError)
def model_server_error(request, exc):
    log.error("model server call failed: %r", exc)
    return JSONResponse(status_code=502, content={"detail": model_down_message()})


# ---------- who is asking ----------

def current_user(kt_session: Annotated[str | None, Cookie()] = None) -> dict:
    if kt_session:
        with admin_db() as conn:
            row = conn.execute(
                "SELECT u.id, u.email, u.role, u.org_id, o.name FROM sessions s "
                "JOIN users u ON u.id = s.user_id JOIN organisations o ON o.id = u.org_id "
                "WHERE s.token_hash = %s AND s.expires_at > now()",
                (auth.token_hash(kt_session),),
            ).fetchone()
        if row:
            return dict(zip(("id", "email", "role", "org_id", "org_name"), row))
    raise HTTPException(401, "Sign in first")


def admin_user(user: Annotated[dict, Depends(current_user)]) -> dict:
    if user["role"] != "admin":
        raise HTTPException(403, "Only an admin can do this")
    return user


User = Annotated[dict, Depends(current_user)]
Admin = Annotated[dict, Depends(admin_user)]


def open_workspace(conn, workspace_id: int) -> None:
    """404 unless this user may open the workspace. conn must come from user_db()."""
    if not conn.execute("SELECT 1 FROM workspaces WHERE id = %s", (workspace_id,)).fetchone():
        raise HTTPException(404, "Workspace not found")


def start_session(conn, response: Response, user_id: int) -> None:
    token, hashed = auth.new_token()
    conn.execute(
        "INSERT INTO sessions (token_hash, user_id, expires_at) VALUES (%s, %s, now() + make_interval(days => %s))",
        (hashed, user_id, auth.SESSION_DAYS),
    )
    response.set_cookie(
        COOKIE, token, max_age=auth.SESSION_DAYS * 86400, httponly=True, samesite="lax", secure=COOKIE_SECURE
    )


# ---------- accounts ----------

class Credentials(BaseModel):
    email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", max_length=200)
    password: str = Field(min_length=8, max_length=200)


class Signup(Credentials):
    org_name: str | None = Field(default=None, min_length=1, max_length=100)
    invite: str | None = Field(default=None, max_length=200)


@app.post("/auth/signup")
def signup(body: Signup, response: Response):
    """With an invite: join that organisation. Without: create a new organisation and become its admin."""
    with admin_db() as conn:
        if body.invite:
            row = conn.execute(
                "UPDATE invites SET used_at = now() "
                "WHERE token_hash = %s AND used_at IS NULL AND expires_at > now() RETURNING org_id, role",
                (auth.token_hash(body.invite),),
            ).fetchone()
            if not row:
                raise HTTPException(400, "This invite link is not valid any more. Ask your admin for a new one.")
            org_id, role = row
        elif body.org_name and body.org_name.strip():
            org_id = conn.execute(
                "INSERT INTO organisations (name) VALUES (%s) RETURNING id", (body.org_name.strip(),)
            ).fetchone()[0]
            role = "admin"
        else:
            raise HTTPException(400, "Enter your company name")
        try:
            user_id = conn.execute(
                "INSERT INTO users (email, password_hash, org_id, role) VALUES (%s, %s, %s, %s) RETURNING id",
                (body.email.lower(), auth.hash_password(body.password), org_id, role),
            ).fetchone()[0]
        except psycopg.errors.UniqueViolation:
            raise HTTPException(409, "An account with this email already exists. Sign in instead.")
        start_session(conn, response, user_id)
    return {"ok": True}


@app.post("/auth/login")
def login(body: Credentials, response: Response):
    with admin_db() as conn:
        row = conn.execute("SELECT id, password_hash FROM users WHERE email = %s", (body.email.lower(),)).fetchone()
        if not auth.check_password(body.password, row[1] if row else auth.DUMMY_HASH) or not row:
            raise HTTPException(401, "Wrong email or password")
        start_session(conn, response, row[0])
    return {"ok": True}


@app.post("/auth/logout")
def logout(response: Response, kt_session: Annotated[str | None, Cookie()] = None):
    if kt_session:
        with admin_db() as conn:
            conn.execute("DELETE FROM sessions WHERE token_hash = %s", (auth.token_hash(kt_session),))
    response.delete_cookie(COOKIE)
    return {"ok": True}


@app.get("/me")
def me(user: User):
    with user_db(user["id"]) as conn:
        workspaces = conn.execute("SELECT id, name FROM workspaces ORDER BY name").fetchall()
    return {
        "email": user["email"],
        "role": user["role"],
        "org": user["org_name"],
        "workspaces": [{"id": i, "name": n} for i, n in workspaces],
    }


# ---------- team (admins) ----------

class InviteRequest(BaseModel):
    role: Literal["admin", "member"] = "member"


@app.post("/invites")
def invite(body: InviteRequest, user: Admin):
    """Returns a one-time token. The web app turns it into a link for the admin to send."""
    token, hashed = auth.new_token()
    with admin_db() as conn:
        conn.execute(
            "INSERT INTO invites (token_hash, org_id, role, expires_at) VALUES (%s, %s, %s, now() + make_interval(days => %s))",
            (hashed, user["org_id"], body.role, auth.INVITE_DAYS),
        )
    return {"token": token, "valid_days": auth.INVITE_DAYS}


@app.get("/team")
def team(user: Admin):
    with admin_db() as conn:
        members = conn.execute(
            "SELECT u.id, u.email, u.role, coalesce(array_agg(m.workspace_id) FILTER (WHERE m.workspace_id IS NOT NULL), '{}') "
            "FROM users u LEFT JOIN workspace_members m ON m.user_id = u.id "
            "WHERE u.org_id = %s GROUP BY u.id ORDER BY u.email",
            (user["org_id"],),
        ).fetchall()
    return [{"id": i, "email": e, "role": r, "workspace_ids": w} for i, e, r, w in members]


class NewWorkspace(BaseModel):
    name: str = Field(min_length=1, max_length=60)


@app.post("/workspaces")
def create_workspace(body: NewWorkspace, user: Admin):
    try:
        with user_db(user["id"]) as conn:
            row = conn.execute(
                "INSERT INTO workspaces (org_id, name) VALUES (%s, %s) RETURNING id, name",
                (user["org_id"], body.name.strip()),
            ).fetchone()
    except psycopg.errors.UniqueViolation:
        raise HTTPException(409, "A workspace with this name already exists")
    return {"id": row[0], "name": row[1]}


@app.put("/workspaces/{workspace_id}/members/{member_id}")
def add_member(workspace_id: int, member_id: int, user: Admin):
    with user_db(user["id"]) as conn:
        open_workspace(conn, workspace_id)
        same_org = conn.execute(
            "SELECT 1 FROM users WHERE id = %s AND org_id = %s", (member_id, user["org_id"])
        ).fetchone()
        if not same_org:
            raise HTTPException(404, "Person not found")
        conn.execute(
            "INSERT INTO workspace_members (workspace_id, user_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            (workspace_id, member_id),
        )
    return {"ok": True}


@app.delete("/workspaces/{workspace_id}/members/{member_id}")
def remove_member(workspace_id: int, member_id: int, user: Admin):
    with user_db(user["id"]) as conn:
        open_workspace(conn, workspace_id)
        conn.execute(
            "DELETE FROM workspace_members WHERE workspace_id = %s AND user_id = %s", (workspace_id, member_id)
        )
    return {"ok": True}


# ---------- documents and questions ----------

class Turn(BaseModel):
    question: str = Field(max_length=2000)
    answer: str = Field(max_length=8000)


class Question(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    history: list[Turn] = Field(default=[], max_length=2)


class Feedback(BaseModel):
    value: Literal[-1, 0, 1]  # 0 clears it


@app.post("/workspaces/{workspace_id}/documents")
def upload(workspace_id: int, file: UploadFile, user: User):
    data = file.file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "This file is larger than 20 MB")
    with user_db(user["id"]) as conn:
        open_workspace(conn, workspace_id)
        try:
            return rag.ingest(conn, workspace_id, file.filename or "upload", data)
        except ValueError as e:
            raise HTTPException(400, str(e))


@app.get("/workspaces/{workspace_id}/documents")
def documents(workspace_id: int, user: User):
    with user_db(user["id"]) as conn:
        open_workspace(conn, workspace_id)
        rows = conn.execute(
            "SELECT d.name, count(c.id) FROM documents d LEFT JOIN chunks c ON c.document_id = d.id "
            "WHERE d.workspace_id = %s GROUP BY d.id ORDER BY d.name",
            (workspace_id,),
        ).fetchall()
    return [{"name": name, "chunks": chunks} for name, chunks in rows]


@app.post("/workspaces/{workspace_id}/ask")
def ask(workspace_id: int, body: Question, user: User):
    """Streams newline-separated JSON: {searched_for, sources}, then {delta} pieces, then {done, message_id}.

    Model errors before the first line become a normal 502; after it, an {error} line.
    """
    with user_db(user["id"]) as conn:
        open_workspace(conn, workspace_id)
        standalone = rag.rewrite(body.question, [t.model_dump() for t in body.history])
        sources = rag.search(conn, workspace_id, standalone)

    def lines():
        yield json.dumps({"searched_for": standalone, "sources": sources}) + "\n"
        answer = ""
        try:
            for piece in rag.answer_stream(sources, standalone):
                answer += piece
                yield json.dumps({"delta": piece}) + "\n"
        except httpx.HTTPError as e:
            log.error("model server call failed: %r", e)
            yield json.dumps({"error": model_down_message()}) + "\n"
            return
        with user_db(user["id"]) as conn:
            message_id = conn.execute(
                "INSERT INTO messages (workspace_id, user_id, question, searched_for, answer, sources) "
                "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
                (workspace_id, user["id"], body.question, standalone, answer, Jsonb(sources)),
            ).fetchone()[0]
        yield json.dumps({"done": True, "message_id": message_id}) + "\n"

    return StreamingResponse(lines(), media_type="application/x-ndjson")


@app.get("/workspaces/{workspace_id}/messages")
def messages(workspace_id: int, user: User):
    """This user's last 20 questions in the workspace, oldest first."""
    with user_db(user["id"]) as conn:
        open_workspace(conn, workspace_id)
        rows = conn.execute(
            "SELECT * FROM (SELECT id, question, searched_for, answer, sources, feedback FROM messages "
            "WHERE workspace_id = %s ORDER BY id DESC LIMIT 20) recent ORDER BY id",
            (workspace_id,),
        ).fetchall()
    keys = ("id", "question", "searched_for", "answer", "sources", "feedback")
    return [dict(zip(keys, row)) for row in rows]


@app.put("/messages/{message_id}/feedback")
def feedback(message_id: int, body: Feedback, user: User):
    with user_db(user["id"]) as conn:
        updated = conn.execute(
            "UPDATE messages SET feedback = NULLIF(%s, 0) WHERE id = %s", (body.value, message_id)
        ).rowcount
    if not updated:
        raise HTTPException(404, "Message not found")
    return {"ok": True}


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
