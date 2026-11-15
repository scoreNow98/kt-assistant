import json
import logging
import os
import time
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from typing import Annotated, Literal

import httpx
import psycopg
from fastapi import Cookie, Depends, FastAPI, HTTPException, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

import auth
import rag
from db import admin_db, init_schema, user_db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
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


# ---------- alerts ----------

# Any chat webhook that accepts JSON: Slack and Google Chat read "text", Discord reads "content".
ALERT_WEBHOOK_URL = os.getenv("ALERT_WEBHOOK_URL", "")
ALERT_EVERY_SECONDS = 60
_last_alert = -ALERT_EVERY_SECONDS


def alert(text: str) -> None:
    """Posts to the webhook, at most once a minute so an outage does not flood the channel."""
    global _last_alert
    if not ALERT_WEBHOOK_URL or time.monotonic() - _last_alert < ALERT_EVERY_SECONDS:
        return
    _last_alert = time.monotonic()
    try:
        httpx.post(ALERT_WEBHOOK_URL, json={"text": text, "content": text}, timeout=5)
    except httpx.HTTPError as e:
        log.warning("could not send alert: %r", e)


@app.exception_handler(Exception)
def unexpected_error(request, exc):
    log.error("unhandled error on %s %s", request.method, request.url.path, exc_info=exc)
    alert(f"KT Assistant: {type(exc).__name__} on {request.method} {request.url.path}. See the API logs.")
    return JSONResponse(status_code=500, content={"detail": "Something went wrong on the server. It has been logged."})


def model_down_message() -> str:
    # The model server's address stays in the logs; users see what to check.
    return (
        "The AI model server did not answer. If you run Ollama, check that it is open and the models are pulled; "
        "otherwise check the model API settings."
    )


@app.exception_handler(httpx.HTTPError)
def model_server_error(request, exc):
    log.error("model server call failed: %r", exc)
    alert(f"KT Assistant cannot reach the model server at {rag.LLM_BASE_URL}.")
    return JSONResponse(status_code=502, content={"detail": model_down_message()})


# ---------- plans and usage ----------

# shortcut: placeholder numbers; set real ones with your pricing. Plans are changed by hand (admin.py) until billing exists.
PLANS = {
    "trial": {"questions": 500, "documents": 500, "people": 10},
    "team": {"questions": 10_000, "documents": 10_000, "people": 100},
}
LIMIT_REACHED = {
    "questions": "Your company has used the {limit} questions in its plan for this month. An admin can upgrade the plan.",
    "documents": "Your company has reached the {limit} documents in its plan. An admin can upgrade the plan.",
    "people": "Your company has reached the {limit} people in its plan. An admin can upgrade the plan.",
}


def usage(conn, org_id: int) -> dict:
    """Questions this calendar month, documents and people now. conn must be admin_db()."""
    plan, questions, documents, people = conn.execute(
        "SELECT o.plan, "
        "(SELECT count(*) FROM messages m JOIN workspaces w ON w.id = m.workspace_id "
        " WHERE w.org_id = o.id AND m.created_at >= date_trunc('month', now())), "
        "(SELECT count(*) FROM documents d JOIN workspaces w ON w.id = d.workspace_id WHERE w.org_id = o.id), "
        "(SELECT count(*) FROM users u WHERE u.org_id = o.id) "
        "FROM organisations o WHERE o.id = %s",
        (org_id,),
    ).fetchone()
    return {"plan": plan, "used": {"questions": questions, "documents": documents, "people": people}, "limits": PLANS[plan]}


def require_room(conn, org_id: int, what: str) -> None:
    # shortcut: check then act, so two requests at the same moment can pass one over the limit; fine for soft limits.
    u = usage(conn, org_id)
    if u["used"][what] >= u["limits"][what]:
        raise HTTPException(403, LIMIT_REACHED[what].format(limit=u["limits"][what]))


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
            return dict(zip(("id", "email", "role", "org_id", "org_name"), row)) | {"session": kt_session}
    raise HTTPException(401, "Sign in first")


def admin_user(user: Annotated[dict, Depends(current_user)]) -> dict:
    if user["role"] != "admin":
        raise HTTPException(403, "Only an admin can do this")
    return user


User = Annotated[dict, Depends(current_user)]
Admin = Annotated[dict, Depends(admin_user)]


def open_workspace(conn, workspace_id: int) -> None:
    """404 unless this person may open the workspace. conn must come from user_db()."""
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

# shortcut: counted in memory by this one API process; move to Redis or the proxy if you run several.
_attempts: dict[str, list[float]] = {}


def throttle(key: str, limit: int, seconds: int, count: bool = True) -> None:
    """Refuses the request if `key` was counted `limit` times in the last `seconds`; then counts this one
    unless count=False (the caller counts it later, for example only when it fails)."""
    if len(_attempts) > 10_000:  # keeps memory bounded under a flood
        _attempts.clear()
    now = time.monotonic()
    recent = [t for t in _attempts.get(key, []) if now - t < seconds]
    _attempts[key] = recent
    if len(recent) >= limit:
        raise HTTPException(429, "Too many attempts. Wait a few minutes and try again.")
    if count:
        recent.append(now)


class Credentials(BaseModel):
    email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", max_length=200)
    password: str = Field(min_length=8, max_length=200)


class Signup(Credentials):
    org_name: str | None = Field(default=None, min_length=1, max_length=100)
    invite: str | None = Field(default=None, max_length=200)


@app.post("/auth/signup")
def signup(body: Signup, request: Request, response: Response):
    """With an invite: join that organisation. Without: create a new organisation and become its admin."""
    throttle(f"signup:{request.client.host}", limit=10, seconds=3600)
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
            require_room(conn, org_id, "people")  # refusing rolls back, so the invite stays usable
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
def login(body: Credentials, request: Request, response: Response):
    throttle(f"login-ip:{request.client.host}", limit=30, seconds=300)
    # Failed passwords per address and email: someone guessing elsewhere cannot lock the real owner out.
    guesses = f"login-fail:{request.client.host}:{body.email.lower()}"
    throttle(guesses, limit=10, seconds=300, count=False)
    with admin_db() as conn:
        row = conn.execute("SELECT id, password_hash FROM users WHERE email = %s", (body.email.lower(),)).fetchone()
        if not auth.check_password(body.password, row[1] if row else auth.DUMMY_HASH) or not row:
            _attempts[guesses].append(time.monotonic())
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
    with user_db(user["session"]) as conn:
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
        require_room(conn, user["org_id"], "people")
        conn.execute(
            "INSERT INTO invites (token_hash, org_id, role, expires_at) VALUES (%s, %s, %s, now() + make_interval(days => %s))",
            (hashed, user["org_id"], body.role, auth.INVITE_DAYS),
        )
    return {"token": token, "valid_days": auth.INVITE_DAYS}


@app.get("/usage")
def get_usage(user: Admin):
    with admin_db() as conn:
        return usage(conn, user["org_id"])


@app.get("/team")
def team(user: Admin):
    with admin_db() as conn:
        members = conn.execute(
            "SELECT u.id, u.email, u.role, coalesce(array_agg(m.workspace_id) FILTER (WHERE m.workspace_id IS NOT NULL), '{}') "
            "FROM users u LEFT JOIN workspace_members m ON m.user_id = u.id "
            "WHERE u.org_id = %s GROUP BY u.id ORDER BY u.email",
            (user["org_id"],),
        ).fetchall()
    return [{"id": i, "email": e, "role": r, "workspace_ids": w, "you": i == user["id"]} for i, e, r, w in members]


@app.delete("/team/{member_id}")
def remove_person(member_id: int, user: Admin):
    """Removes someone from the company: their account, sessions, access and questions go; documents stay."""
    if member_id == user["id"]:
        raise HTTPException(400, "You cannot remove yourself. Ask another admin.")
    with admin_db() as conn:
        removed = conn.execute(
            "DELETE FROM users WHERE id = %s AND org_id = %s", (member_id, user["org_id"])
        ).rowcount
    if not removed:
        raise HTTPException(404, "Person not found")
    return {"ok": True}


class NewWorkspace(BaseModel):
    name: str = Field(min_length=1, max_length=60)


@app.post("/workspaces")
def create_workspace(body: NewWorkspace, user: Admin):
    try:
        with user_db(user["session"]) as conn:
            row = conn.execute(
                "INSERT INTO workspaces (org_id, name) VALUES (%s, %s) RETURNING id, name",
                (user["org_id"], body.name.strip()),
            ).fetchone()
    except psycopg.errors.UniqueViolation:
        raise HTTPException(409, "A workspace with this name already exists")
    return {"id": row[0], "name": row[1]}


@app.put("/workspaces/{workspace_id}/members/{member_id}")
def add_member(workspace_id: int, member_id: int, user: Admin):
    with admin_db() as conn:
        same_org = conn.execute(
            "SELECT 1 FROM users WHERE id = %s AND org_id = %s", (member_id, user["org_id"])
        ).fetchone()
    if not same_org:
        raise HTTPException(404, "Person not found")
    with user_db(user["session"]) as conn:
        open_workspace(conn, workspace_id)
        conn.execute(
            "INSERT INTO workspace_members (workspace_id, user_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            (workspace_id, member_id),
        )
    return {"ok": True}


@app.delete("/workspaces/{workspace_id}/members/{member_id}")
def remove_member(workspace_id: int, member_id: int, user: Admin):
    with user_db(user["session"]) as conn:
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
    with admin_db() as conn:
        require_room(conn, user["org_id"], "documents")
    with user_db(user["session"]) as conn:
        open_workspace(conn, workspace_id)
        try:
            return rag.ingest(conn, workspace_id, file.filename or "upload", data)
        except ValueError as e:
            raise HTTPException(400, str(e))


@app.get("/workspaces/{workspace_id}/documents")
def documents(workspace_id: int, user: User):
    with user_db(user["session"]) as conn:
        open_workspace(conn, workspace_id)
        rows = conn.execute(
            "SELECT d.id, d.name, count(c.id) FROM documents d LEFT JOIN chunks c ON c.document_id = d.id "
            "WHERE d.workspace_id = %s GROUP BY d.id ORDER BY d.name, d.id",
            (workspace_id,),
        ).fetchall()
    return [{"id": i, "name": name, "chunks": chunks} for i, name, chunks in rows]


@app.delete("/workspaces/{workspace_id}/documents/{document_id}")
def delete_document(workspace_id: int, document_id: int, user: Admin):
    """Removes a document and its chunks, for example one uploaded to the wrong client or out of date."""
    with user_db(user["session"]) as conn:
        open_workspace(conn, workspace_id)
        deleted = conn.execute(
            "DELETE FROM documents WHERE id = %s AND workspace_id = %s", (document_id, workspace_id)
        ).rowcount
    if not deleted:
        raise HTTPException(404, "Document not found")
    return {"ok": True}


@app.post("/workspaces/{workspace_id}/ask")
def ask(workspace_id: int, body: Question, user: User):
    """Streams newline-separated JSON: {searched_for, sources}, then {delta} pieces, then {done, message_id}.

    Model errors before the first line become a normal 502; after it, an {error} line.
    """
    with user_db(user["session"]) as conn:
        open_workspace(conn, workspace_id)
    with admin_db() as conn:
        require_room(conn, user["org_id"], "questions")
    with user_db(user["session"]) as conn:
        standalone = rag.rewrite(body.question, [t.model_dump() for t in body.history])
        sources = rag.fit(rag.search(conn, workspace_id, standalone))

    def lines():
        yield json.dumps({"searched_for": standalone, "sources": sources}) + "\n"
        answer = ""
        try:
            for piece in rag.answer_stream(sources, standalone):
                answer += piece
                yield json.dumps({"delta": piece}) + "\n"
        except httpx.HTTPError as e:
            log.error("model server call failed: %r", e)
            alert(f"KT Assistant cannot reach the model server at {rag.LLM_BASE_URL}.")
            yield json.dumps({"error": model_down_message()}) + "\n"
            return
        except Exception:  # the response has started, so the error handler cannot turn this into a 500
            log.exception("answer stream failed")
            alert("KT Assistant: an answer failed while streaming. See the API logs.")
            yield json.dumps({"error": "The answer failed part way. Try again."}) + "\n"
            return
        with user_db(user["session"]) as conn:
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
    with user_db(user["session"]) as conn:
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
    with user_db(user["session"]) as conn:
        updated = conn.execute(
            "UPDATE messages SET feedback = NULLIF(%s, 0) WHERE id = %s", (body.value, message_id)
        ).rowcount
    if not updated:
        raise HTTPException(404, "Message not found")
    return {"ok": True}


# ---------- knowledge transfer ----------

GAP_DAYS = 30
# The answer prompt asks for this sentence when the documents lack the answer; small models sometimes add to it.
NOT_FOUND_PATTERN = "%could not find this in the documents%"

GUIDE_SECTIONS = [
    ("What this is", "What is this project or system, and what does it do?"),
    ("Main parts", "What are the main parts or components, and how do they fit together?"),
    ("Set up and run it", "How do I install, set up and run it?"),
    ("Common problems", "What are common problems or errors, and how are they fixed?"),
    ("Key terms", "What are the most important terms a newcomer should know? Explain each in one sentence."),
]


@app.get("/workspaces/{workspace_id}/gaps")
def gaps(workspace_id: int, user: User):
    """Questions from the last 30 days that the documents could not answer or that someone marked
    not helpful. Shared with everyone who can open the workspace, without saying who asked."""
    with user_db(user["session"]) as conn:
        open_workspace(conn, workspace_id)
    with admin_db() as conn:  # reads every member's questions; access was checked just above
        rows = conn.execute(
            "SELECT min(searched_for), count(*), max(created_at), "
            "coalesce(bool_or(answer ILIKE %(nf)s), false), coalesce(bool_or(feedback = -1), false) "
            "FROM messages WHERE workspace_id = %(w)s AND created_at > now() - make_interval(days => %(days)s) "
            "AND (answer ILIKE %(nf)s OR feedback = -1) "
            "GROUP BY lower(searched_for) ORDER BY count(*) DESC, max(created_at) DESC LIMIT 50",
            {"nf": NOT_FOUND_PATTERN, "w": workspace_id, "days": GAP_DAYS},
        ).fetchall()
    return [
        {"question": q, "asked": n, "last_asked": last.isoformat(), "not_found": nf, "not_helpful": nh}
        for q, n, last, nf, nh in rows
    ]


@app.get("/workspaces/{workspace_id}/guide")
def guide(workspace_id: int, user: User):
    with user_db(user["session"]) as conn:
        open_workspace(conn, workspace_id)
        row = conn.execute(
            "SELECT sections, reading_order, created_at FROM guides WHERE workspace_id = %s", (workspace_id,)
        ).fetchone()
    return {"sections": row[0], "reading_order": row[1], "created_at": row[2].isoformat()} if row else None


@app.post("/workspaces/{workspace_id}/guide")
def create_guide(workspace_id: int, user: Admin):
    """Streams one {section} line per finished section, then {done}. Saves only a complete guide."""
    with user_db(user["session"]) as conn:
        open_workspace(conn, workspace_id)
        if not conn.execute("SELECT 1 FROM documents WHERE workspace_id = %s LIMIT 1", (workspace_id,)).fetchone():
            raise HTTPException(400, "Add documents to this workspace first.")

    def lines():
        sections = []
        try:
            for title, question in GUIDE_SECTIONS:
                with user_db(user["session"]) as conn:
                    sources = rag.fit(rag.search(conn, workspace_id, question))
                section = {"title": title, "answer": "".join(rag.answer_stream(sources, question)), "sources": sources}
                sections.append(section)
                yield json.dumps({"section": section}) + "\n"
        except httpx.HTTPError as e:
            log.error("model server call failed: %r", e)
            alert(f"KT Assistant cannot reach the model server at {rag.LLM_BASE_URL}.")
            yield json.dumps({"error": model_down_message()}) + "\n"
            return
        except Exception:
            log.exception("guide stream failed")
            alert("KT Assistant: building a guide failed. See the API logs.")
            yield json.dumps({"error": "Building the guide failed part way. Try again."}) + "\n"
            return
        # Reading order: documents in the order the guide first cites them.
        reading_order = []
        for section in sections:
            by_number = {s["n"]: s["document"] for s in section["sources"]}
            for n in rag.cited_order(section["answer"]):
                if n in by_number and by_number[n] not in reading_order:
                    reading_order.append(by_number[n])
        with user_db(user["session"]) as conn:
            conn.execute(
                "INSERT INTO guides (workspace_id, sections, reading_order) VALUES (%s, %s, %s) "
                "ON CONFLICT (workspace_id) DO UPDATE SET sections = excluded.sections, "
                "reading_order = excluded.reading_order, created_at = now()",
                (workspace_id, Jsonb(sections), Jsonb(reading_order)),
            )
        yield json.dumps({"done": True, "reading_order": reading_order}) + "\n"

    return StreamingResponse(lines(), media_type="application/x-ndjson")


class HandoverAnswer(BaseModel):
    question: str = Field(min_length=1, max_length=500)
    answer: str = Field(max_length=10_000)


class Handover(BaseModel):
    answers: list[HandoverAnswer] = Field(min_length=1, max_length=30)


@app.post("/workspaces/{workspace_id}/handover")
def handover(workspace_id: int, body: Handover, user: User):
    """Saves a leaving person's answers as a document, one section per question, so they can be cited."""
    filled = [a for a in body.answers if a.answer.strip()]
    if not filled:
        raise HTTPException(400, "Write at least one answer before saving")
    now = datetime.now(timezone.utc)
    text = f"# Handover notes from {user['email']}, {now:%d %b %Y}\n\n" + "\n\n".join(
        f"## {' '.join(a.question.split())}\n\n{a.answer.strip()}" for a in filled
    )
    with admin_db() as conn:
        require_room(conn, user["org_id"], "documents")
    with user_db(user["session"]) as conn:
        open_workspace(conn, workspace_id)
        return rag.ingest(conn, workspace_id, f"handover/{user['email'].split('@')[0]}-{now:%Y-%m-%d-%H%M}.md", text.encode())


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
