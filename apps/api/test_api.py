"""End-to-end tests through the HTTP API. Need Postgres with pgvector; the models are faked."""
import json
import os
import uuid

import httpx
import psycopg

import pytest
from fastapi.testclient import TestClient

import auth
import main
import rag
from db import admin_db, user_db

pytestmark = pytest.mark.skipif("DATABASE_URL" not in os.environ, reason="needs Postgres with pgvector")


def fake_embed(texts):
    """Word-count vectors: texts sharing words land close together. No model needed."""
    vectors = []
    for t in texts:
        v = [0.0] * 64
        for word in t.lower().split():
            v[sum(map(ord, word)) % 64] += 1.0
        vectors.append(v)
    return vectors


def session_of(email):
    """A fresh session token for an account, as signing in would create."""
    token, hashed = auth.new_token()
    with admin_db() as conn:
        conn.execute(
            "INSERT INTO sessions (token_hash, user_id, expires_at) "
            "SELECT %s, id, now() + interval '1 hour' FROM users WHERE email = %s",
            (hashed, email),
        )
    return token


@pytest.fixture
def world(monkeypatch):
    """Two companies. Acme has an admin, a member, and workspaces 'bank' and 'shop'. Rival has one admin."""
    monkeypatch.setattr(rag, "embed", fake_embed)
    main._attempts.clear()  # every test client shares one address; rate limits count per test
    monkeypatch.setattr(rag, "chat", lambda system, user: "unused")
    monkeypatch.setattr(rag, "chat_stream", lambda system, user: iter(["Use make ", "restart [1]"]))
    tag = uuid.uuid4().hex[:8]

    def client():
        return TestClient(main.app)

    try:
        with client() as acme_admin, client() as acme_member, client() as rival, client() as anonymous:
            assert acme_admin.post("/auth/signup", json={"email": f"lead-{tag}@acme.test", "password": "correct horse", "org_name": f"Acme {tag}"}).status_code == 200
            assert rival.post("/auth/signup", json={"email": f"boss-{tag}@rival.test", "password": "correct horse", "org_name": f"Rival {tag}"}).status_code == 200
            bank = acme_admin.post("/workspaces", json={"name": "bank"}).json()["id"]
            shop = acme_admin.post("/workspaces", json={"name": "shop"}).json()["id"]
            token = acme_admin.post("/invites", json={}).json()["token"]
            assert acme_member.post("/auth/signup", json={"email": f"dev-{tag}@acme.test", "password": "correct horse", "invite": token}).status_code == 200
            r = acme_admin.post(f"/workspaces/{bank}/documents", files={"file": ("runbook.md", b"# Deploy\nRestart the payment service with make restart.\n")})
            assert r.json() == {"name": "runbook.md", "status": "ready", "chunks": 1}
            yield {"admin": acme_admin, "member": acme_member, "rival": rival, "anonymous": anonymous, "bank": bank, "shop": shop, "tag": tag, "invite": token}
    finally:  # also when setup failed half way
        with admin_db() as conn:
            conn.execute("DELETE FROM organisations WHERE name IN (%s, %s)", (f"Acme {tag}", f"Rival {tag}"))


def ask(client, workspace_id, question, history=()):
    """Posts a question and returns (status code, list of streamed JSON lines)."""
    r = client.post(f"/workspaces/{workspace_id}/ask", json={"question": question, "history": list(history)})
    return r.status_code, [json.loads(line) for line in r.text.splitlines()] if r.status_code == 200 else r.json()


def test_signed_out_requests_are_refused(world):
    assert world["anonymous"].get("/me").status_code == 401
    assert world["anonymous"].post(f"/workspaces/{world['bank']}/ask", json={"question": "restart?"}).status_code == 401


def test_admin_asks_and_gets_cited_sources(world):
    status, lines = ask(world["admin"], world["bank"], "how do I restart the service?")
    assert status == 200
    assert lines[0]["searched_for"] == "how do I restart the service?"
    assert [(s["n"], s["document"], s["heading"]) for s in lines[0]["sources"]] == [(1, "runbook.md", "Deploy")]
    assert "".join(line.get("delta", "") for line in lines) == "Use make restart [1]"
    assert lines[-1]["done"] is True and isinstance(lines[-1]["message_id"], int)
    again = world["admin"].post(f"/workspaces/{world['bank']}/documents", files={"file": ("copy.md", b"# Deploy\nRestart the payment service with make restart.\n")})
    assert again.json()["status"] == "duplicate"


def test_another_company_cannot_reach_the_workspace(world):
    rival, bank = world["rival"], world["bank"]
    assert rival.get("/me").json()["workspaces"] == []
    assert ask(rival, bank, "restart?")[0] == 404
    assert rival.get(f"/workspaces/{bank}/messages").status_code == 404
    assert rival.get(f"/workspaces/{bank}/documents").status_code == 404
    assert rival.post(f"/workspaces/{bank}/documents", files={"file": ("x.md", b"# x\ny\n")}).status_code == 404
    member_id = world["admin"].get("/team").json()[0]["id"]
    assert rival.put(f"/workspaces/{bank}/members/{member_id}").status_code == 404


def test_member_sees_only_assigned_workspaces(world):
    admin, member, bank, shop = world["admin"], world["member"], world["bank"], world["shop"]
    assert member.get("/me").json()["workspaces"] == []
    assert ask(member, bank, "restart?")[0] == 404
    assert member.post("/workspaces", json={"name": "mine"}).status_code == 403
    assert member.get("/team").status_code == 403

    member_id = next(m["id"] for m in admin.get("/team").json() if m["role"] == "member")
    assert admin.put(f"/workspaces/{bank}/members/{member_id}").status_code == 200
    assert [w["name"] for w in member.get("/me").json()["workspaces"]] == ["bank"]
    assert ask(member, bank, "restart?")[0] == 200
    assert member.get(f"/workspaces/{shop}/documents").status_code == 404

    assert admin.delete(f"/workspaces/{bank}/members/{member_id}").status_code == 200
    assert member.get(f"/workspaces/{bank}/documents").status_code == 404


def test_database_itself_hides_other_companies_rows(world):
    """Even a query with no workspace filter returns nothing: this is row-level security, not API code."""
    with user_db(session_of(f"boss-{world['tag']}@rival.test")) as conn:
        assert conn.execute("SELECT count(*) FROM chunks").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM documents").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM workspace_members").fetchone()[0] == 0
        assert conn.execute("UPDATE chunks SET text = 'x'").rowcount == 0
        with pytest.raises(Exception):  # cannot plant a document in someone else's workspace
            conn.execute("INSERT INTO documents (workspace_id, name, sha256) VALUES (%s, 'x', 'x')", (world["bank"],))
    with user_db(session_of(f"lead-{world['tag']}@acme.test")) as conn:
        assert conn.execute("SELECT count(*) FROM chunks").fetchone()[0] == 1


def test_app_login_cannot_impersonate_or_read_accounts(world):
    """Council finding S1: kt_app used to be able to set any user id itself. Now it needs a session token."""
    with admin_db() as conn:
        lead_id = conn.execute("SELECT id FROM users WHERE email = %s", (f"lead-{world['tag']}@acme.test",)).fetchone()[0]
    with user_db(session_of(f"boss-{world['tag']}@rival.test")) as conn:
        for name, value in (("app.user_id", str(lead_id)), ("app.session", "guessed-token"), ("app.session", "")):
            conn.execute("SELECT set_config(%s, %s, true)", (name, value))
            assert conn.execute("SELECT count(*) FROM chunks").fetchone()[0] == 0
            assert conn.execute("SELECT count(*) FROM workspaces").fetchone()[0] == 0
    for table in ("users", "sessions", "invites", "organisations"):
        with user_db(session_of(f"lead-{world['tag']}@acme.test")) as conn:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(f"SELECT * FROM {table} LIMIT 1")
    with admin_db() as conn:  # an expired session opens nothing
        conn.execute("UPDATE sessions SET expires_at = now() - interval '1 second' WHERE user_id = %s", (lead_id,))
    with TestClient(main.app) as c:
        c.cookies = world["admin"].cookies
        assert c.get("/me").status_code == 401


def test_login_logout_and_invite_rules(world):
    tag = world["tag"]
    with TestClient(main.app) as c:
        assert c.post("/auth/login", json={"email": f"lead-{tag}@acme.test", "password": "wrong password"}).status_code == 401
        assert c.post("/auth/login", json={"email": f"nobody-{tag}@acme.test", "password": "correct horse"}).status_code == 401
        assert c.post("/auth/login", json={"email": f"LEAD-{tag}@acme.test", "password": "correct horse"}).status_code == 200
        assert c.get("/me").json()["role"] == "admin"
        assert c.post("/auth/logout").status_code == 200
        assert c.get("/me").status_code == 401
        # an invite works once; a short password and a taken email are refused
        assert c.post("/auth/signup", json={"email": f"late-{tag}@acme.test", "password": "correct horse", "invite": world["invite"]}).status_code == 400
        assert c.post("/auth/signup", json={"email": f"x-{tag}@acme.test", "password": "short", "org_name": "X"}).status_code == 422
        assert c.post("/auth/signup", json={"email": f"lead-{tag}@acme.test", "password": "correct horse", "org_name": "X"}).status_code == 409


def test_password_hash_roundtrip():
    stored = auth.hash_password("correct horse")
    assert "correct horse" not in stored
    assert auth.check_password("correct horse", stored) and not auth.check_password("wrong", stored)


def test_keyword_search_finds_exact_terms(world):
    """Twelve look-alike pages and one that holds the exact setting name: hybrid search ranks that one first.
    With keyword=False this fails (checked), so it really tests the keyword half."""
    admin, shop = world["admin"], world["shop"]
    for i in range(12):
        admin.post(f"/workspaces/{shop}/documents", files={"file": (f"filler-{i}.md", f"# Notes {i}\nWhat does this do and what does that do, page {i}.\n".encode())})
    admin.post(f"/workspaces/{shop}/documents", files={"file": ("settings.md", b"# Memory\nSet OLLAMA_KEEP_ALIVE to 10m to unload sooner.\n")})
    with admin_db() as conn:
        hits = rag.search(conn, shop, "what does OLLAMA_KEEP_ALIVE do?", k=3)
        assert hits[0]["document"] == "settings.md"
        assert rag.search(conn, shop, "error: code !& | :* ' ) (", k=3) is not None  # odd input cannot break the query


def test_follow_up_question_is_rewritten_before_search(world, monkeypatch):
    seen = []
    monkeypatch.setattr(rag, "chat", lambda system, user: seen.append(user) or "How do I restart the payment service?")
    history = [{"question": "What does the payment service do?", "answer": "It takes payments [1]"}]
    status, lines = ask(world["admin"], world["bank"], "and how do I restart it?", history)
    assert status == 200 and lines[0]["searched_for"] == "How do I restart the payment service?"
    assert "What does the payment service do?" in seen[0] and "and how do I restart it?" in seen[0]
    _, lines = ask(world["admin"], world["bank"], "first question")
    assert lines[0]["searched_for"] == "first question"  # no history, no rewrite call
    assert len(seen) == 1


def test_history_and_feedback_are_private(world):
    admin, member, rival, bank = world["admin"], world["member"], world["rival"], world["bank"]
    _, lines = ask(admin, bank, "how do I restart the service?")
    message_id = lines[-1]["message_id"]

    history = admin.get(f"/workspaces/{bank}/messages").json()
    assert [(m["id"], m["question"], m["answer"], m["feedback"]) for m in history] == [
        (message_id, "how do I restart the service?", "Use make restart [1]", None)
    ]
    assert history[0]["sources"][0]["document"] == "runbook.md"

    assert admin.put(f"/messages/{message_id}/feedback", json={"value": 1}).status_code == 200
    assert admin.get(f"/workspaces/{bank}/messages").json()[0]["feedback"] == 1
    assert admin.put(f"/messages/{message_id}/feedback", json={"value": 0}).status_code == 200
    assert admin.get(f"/workspaces/{bank}/messages").json()[0]["feedback"] is None
    assert admin.put(f"/messages/{message_id}/feedback", json={"value": 5}).status_code == 422

    # Another company, and a colleague with access to the same workspace, cannot see or rate it.
    assert rival.put(f"/messages/{message_id}/feedback", json={"value": -1}).status_code == 404
    member_id = next(m["id"] for m in admin.get("/team").json() if m["role"] == "member")
    admin.put(f"/workspaces/{bank}/members/{member_id}")
    assert member.get(f"/workspaces/{bank}/messages").json() == []
    assert member.put(f"/messages/{message_id}/feedback", json={"value": -1}).status_code == 404


def test_model_failure_mid_answer_is_reported_and_not_saved(world, monkeypatch):
    def broken(system, user):
        yield "Use make "
        raise httpx.ReadTimeout("model stopped")

    monkeypatch.setattr(rag, "chat_stream", broken)
    status, lines = ask(world["admin"], world["bank"], "how do I restart the service?")
    assert status == 200 and "Ollama" in lines[-1]["error"]
    assert world["admin"].get(f"/workspaces/{world['bank']}/messages").json() == []


def test_model_down_before_answer_is_a_502(world, monkeypatch):
    def down(texts):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(rag, "embed", down)
    status, body = ask(world["admin"], world["bank"], "anything")
    assert status == 502 and "Ollama" in body["detail"]


def test_gap_report_lists_unanswered_and_unhelpful_questions_without_names(world, monkeypatch):
    admin, member, rival, bank, tag = world["admin"], world["member"], world["rival"], world["bank"], world["tag"]
    member_id = next(m["id"] for m in admin.get("/team").json() if m["role"] == "member")
    admin.put(f"/workspaces/{bank}/members/{member_id}")

    monkeypatch.setattr(rag, "chat_stream", lambda s, u: iter([rag.NOT_FOUND]))
    ask(admin, bank, "Who owns the billing database?")
    ask(member, bank, "who owns the billing database?")  # same question, other person, other case
    monkeypatch.setattr(rag, "chat_stream", lambda s, u: iter(["Use make restart [1]"]))
    _, lines = ask(member, bank, "how do I restart the service?")
    member.put(f"/messages/{lines[-1]['message_id']}/feedback", json={"value": -1})
    ask(admin, bank, "a question that was answered fine")

    gaps = member.get(f"/workspaces/{bank}/gaps").json()
    assert [(g["question"].lower(), g["asked"], g["not_found"], g["not_helpful"]) for g in gaps] == [
        ("who owns the billing database?", 2, True, False),
        ("how do i restart the service?", 1, False, True),
    ]
    assert tag not in json.dumps(gaps)  # no email addresses
    assert rival.get(f"/workspaces/{bank}/gaps").status_code == 404


def test_guide_is_built_by_admins_and_read_by_members(world):
    admin, member, rival, bank = world["admin"], world["member"], world["rival"], world["bank"]
    member_id = next(m["id"] for m in admin.get("/team").json() if m["role"] == "member")
    admin.put(f"/workspaces/{bank}/members/{member_id}")

    assert admin.get(f"/workspaces/{bank}/guide").json() is None
    assert member.post(f"/workspaces/{bank}/guide").status_code == 403
    assert rival.post(f"/workspaces/{bank}/guide").status_code == 404  # an admin, but of another company
    assert rival.get(f"/workspaces/{bank}/guide").status_code == 404

    r = admin.post(f"/workspaces/{bank}/guide")
    lines = [json.loads(line) for line in r.text.splitlines()]
    assert [line["section"]["title"] for line in lines[:-1]] == [t for t, _ in main.GUIDE_SECTIONS]
    assert lines[-1] == {"done": True, "reading_order": ["runbook.md"]}

    saved = member.get(f"/workspaces/{bank}/guide").json()
    assert saved["reading_order"] == ["runbook.md"] and saved["sections"][0]["answer"] == "Use make restart [1]"
    admin.post(f"/workspaces/{bank}/guide")  # building again replaces it
    assert len(member.get(f"/workspaces/{bank}/guide").json()["sections"]) == len(main.GUIDE_SECTIONS)


def test_guide_is_not_saved_when_the_model_fails(world, monkeypatch):
    def broken(system, user):
        raise httpx.ReadTimeout("model stopped")
        yield

    monkeypatch.setattr(rag, "chat_stream", broken)
    lines = [json.loads(line) for line in world["admin"].post(f"/workspaces/{world['bank']}/guide").text.splitlines()]
    assert "Ollama" in lines[-1]["error"]
    assert world["admin"].get(f"/workspaces/{world['bank']}/guide").json() is None


def test_handover_notes_become_a_searchable_document(world):
    admin, rival, bank, tag = world["admin"], world["rival"], world["bank"], world["tag"]
    answers = [
        {"question": "Who owns the billing database?", "answer": "Priya on the client side; escalate to the DBA rota."},
        {"question": "What breaks most often?", "answer": "   "},  # empty answers are left out
    ]
    r = admin.post(f"/workspaces/{bank}/handover", json={"answers": answers})
    assert r.status_code == 200 and r.json()["status"] == "ready"
    name = r.json()["name"]
    assert name.startswith(f"handover/lead-{tag}-") and "@" not in name

    assert name in [d["name"] for d in admin.get(f"/workspaces/{bank}/documents").json()]
    with admin_db() as conn:
        hits = rag.search(conn, bank, "who owns the billing database", k=1)
    assert hits[0]["document"] == name and hits[0]["heading"] == "Who owns the billing database?"
    assert "breaks" not in hits[0]["text"]

    assert admin.post(f"/workspaces/{bank}/handover", json={"answers": answers[1:]}).status_code == 400
    assert rival.post(f"/workspaces/{bank}/handover", json={"answers": answers}).status_code == 404


def test_guides_are_hidden_by_the_database_too(world):
    world["admin"].post(f"/workspaces/{world['bank']}/guide")
    with user_db(session_of(f"boss-{world['tag']}@rival.test")) as conn:
        assert conn.execute("SELECT count(*) FROM guides").fetchone()[0] == 0


def test_plan_limits_stop_questions_uploads_and_new_people(world, monkeypatch):
    admin, rival, bank = world["admin"], world["rival"], world["bank"]
    monkeypatch.setitem(main.PLANS, "trial", {"questions": 1, "documents": 2, "people": 2})  # Acme has 2 people, 1 document

    assert ask(admin, bank, "restart?")[0] == 200
    status, body = ask(admin, bank, "restart again?")
    assert status == 403 and "questions" in body["detail"]

    assert admin.post(f"/workspaces/{bank}/documents", files={"file": ("two.md", b"# Two\nsecond document\n")}).status_code == 200
    r = admin.post(f"/workspaces/{bank}/documents", files={"file": ("three.md", b"# Three\nthird document\n")})
    assert r.status_code == 403 and "documents" in r.json()["detail"]

    assert admin.post("/invites", json={}).status_code == 403

    usage = admin.get("/usage").json()
    assert usage == {"plan": "trial", "used": {"questions": 1, "documents": 2, "people": 2}, "limits": main.PLANS["trial"]}
    assert world["member"].get("/usage").status_code == 403
    assert rival.get("/usage").json()["used"] == {"questions": 0, "documents": 0, "people": 1}  # counted per company


def test_invite_is_kept_when_the_company_is_full(world, monkeypatch):
    admin, tag = world["admin"], world["tag"]
    token = admin.post("/invites", json={}).json()["token"]
    monkeypatch.setitem(main.PLANS, "trial", {"questions": 9, "documents": 9, "people": 2})
    with TestClient(main.app) as c:
        r = c.post("/auth/signup", json={"email": f"new-{tag}@acme.test", "password": "correct horse", "invite": token})
        assert r.status_code == 403
        monkeypatch.setitem(main.PLANS, "trial", {"questions": 9, "documents": 9, "people": 3})
        assert c.post("/auth/signup", json={"email": f"new-{tag}@acme.test", "password": "correct horse", "invite": token}).status_code == 200


def test_set_plan_command(world):
    import admin as admin_cli

    result = admin_cli.set_plan(f"LEAD-{world['tag']}@acme.test", "team")
    assert result["plan"] == "team" and result["limits"] == main.PLANS["team"]
    with pytest.raises(SystemExit):
        admin_cli.set_plan(f"lead-{world['tag']}@acme.test", "platinum")


def test_unexpected_errors_are_logged_alerted_and_hidden(world, monkeypatch):
    sent = []
    monkeypatch.setattr(main, "ALERT_WEBHOOK_URL", "https://hooks.example/alert")
    monkeypatch.setattr(main, "_last_alert", -main.ALERT_EVERY_SECONDS)
    monkeypatch.setattr(main.httpx, "post", lambda url, json, timeout: sent.append(json))

    def broken(*a, **k):
        raise RuntimeError("secret detail")

    monkeypatch.setattr(rag, "search", broken)
    with TestClient(main.app, raise_server_exceptions=False) as c:
        c.cookies = world["admin"].cookies
        for _ in range(2):
            r = c.post(f"/workspaces/{world['bank']}/ask", json={"question": "restart?"})
            assert r.status_code == 500 and "secret" not in r.text
    assert len(sent) == 1  # the second error within a minute is not sent again
    assert "RuntimeError" in sent[0]["text"] and "secret" not in sent[0]["text"]


def test_app_login_cannot_switch_role(world):
    with admin_db() as conn:
        owner = conn.execute("SELECT current_user").fetchone()[0]
    with user_db(session_of(f"lead-{world['tag']}@acme.test")) as conn:
        assert conn.execute("SELECT current_user").fetchone()[0] == "kt_app"
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(f'SET ROLE "{owner}"')


def test_admin_can_delete_a_document_members_cannot(world):
    admin, member, rival, bank = world["admin"], world["member"], world["rival"], world["bank"]
    member_id = next(m["id"] for m in admin.get("/team").json() if m["role"] == "member")
    admin.put(f"/workspaces/{bank}/members/{member_id}")
    doc_id = admin.get(f"/workspaces/{bank}/documents").json()[0]["id"]

    assert member.delete(f"/workspaces/{bank}/documents/{doc_id}").status_code == 403
    assert rival.delete(f"/workspaces/{bank}/documents/{doc_id}").status_code == 404
    assert admin.delete(f"/workspaces/{world['shop']}/documents/{doc_id}").status_code == 404  # wrong workspace
    assert admin.delete(f"/workspaces/{bank}/documents/{doc_id}").status_code == 200
    assert admin.get(f"/workspaces/{bank}/documents").json() == []
    _, lines = ask(admin, bank, "how do I restart the service?")
    assert lines[0]["sources"] == []  # its chunks went with it


def test_changing_the_embedding_model_lets_documents_load_again(world, monkeypatch):
    admin, bank = world["admin"], world["bank"]
    runbook = b"# Deploy\nRestart the payment service with make restart.\n"
    monkeypatch.setattr(rag, "EMBED_MODEL", "another-model")
    r = admin.post(f"/workspaces/{bank}/documents", files={"file": ("runbook.md", runbook)})
    assert r.json()["status"] == "ready"  # not "duplicate": the old chunks were for the old model
    assert [d["name"] for d in admin.get(f"/workspaces/{bank}/documents").json()] == ["runbook.md"]
    _, lines = ask(admin, bank, "how do I restart the service?")
    assert lines[0]["sources"][0]["document"] == "runbook.md"


def test_admin_removes_a_person_who_is_then_signed_out(world):
    admin, member, rival, tag = world["admin"], world["member"], world["rival"], world["tag"]
    people = admin.get("/team").json()
    me = next(p for p in people if p["you"])
    member_id = next(p["id"] for p in people if p["role"] == "member")

    assert member.delete(f"/team/{me['id']}").status_code == 403
    assert rival.delete(f"/team/{member_id}").status_code == 404
    assert admin.delete(f"/team/{me['id']}").status_code == 400
    assert admin.delete(f"/team/{member_id}").status_code == 200
    assert member.get("/me").status_code == 401
    assert [p["email"] for p in admin.get("/team").json()] == [f"lead-{tag}@acme.test"]


def test_bad_uploads_get_a_clear_400(world):
    admin, bank = world["admin"], world["bank"]
    r = admin.post(f"/workspaces/{bank}/documents", files={"file": ("broken.pdf", b"%PDF-1.4 not really")})
    assert r.status_code == 400 and "could not read" in r.json()["detail"]


def test_mid_stream_crash_ends_with_an_error_line(world, monkeypatch):
    def broken(system, user):
        yield "Use make "
        raise KeyError("choices")

    monkeypatch.setattr(rag, "chat_stream", broken)
    status, lines = ask(world["admin"], world["bank"], "how do I restart the service?")
    assert status == 200 and "failed part way" in lines[-1]["error"]


def test_guide_needs_documents_and_orders_reading_by_citation(world, monkeypatch):
    admin, bank, shop = world["admin"], world["bank"], world["shop"]
    assert admin.post(f"/workspaces/{shop}/guide").status_code == 400  # empty workspace
    admin.post(f"/workspaces/{bank}/documents", files={"file": ("config.md", b"# Config\nRestart settings live in config.\n")})
    monkeypatch.setattr(rag, "chat_stream", lambda s, u: iter(["Settings [2]. Restart [1]."]))
    lines = [json.loads(line) for line in admin.post(f"/workspaces/{bank}/guide").text.splitlines()]
    sources = {s["n"]: s["document"] for s in lines[0]["section"]["sources"]}
    assert lines[-1]["reading_order"] == [sources[2], sources[1]]


def test_handover_counts_against_the_document_limit(world, monkeypatch):
    monkeypatch.setitem(main.PLANS, "trial", {"questions": 9, "documents": 1, "people": 9})  # Acme has 1 document
    r = world["admin"].post(f"/workspaces/{world['bank']}/handover", json={"answers": [{"question": "q", "answer": "a"}]})
    assert r.status_code == 403


def test_questions_from_last_month_do_not_count(world):
    ask(world["admin"], world["bank"], "restart?")
    with admin_db() as conn:
        conn.execute("UPDATE messages SET created_at = date_trunc('month', now()) - interval '1 second' "
                     "WHERE workspace_id = %s", (world["bank"],))
    assert world["admin"].get("/usage").json()["used"]["questions"] == 0


def test_password_guessing_is_slowed_down_without_locking_out_the_owner(world):
    email = f"lead-{world['tag']}@acme.test"
    with TestClient(main.app, client=("203.0.113.9", 4000)) as attacker:
        codes = [attacker.post("/auth/login", json={"email": email, "password": "wrong password"}).status_code for _ in range(11)]
        assert codes == [401] * 10 + [429]
        assert attacker.post("/auth/login", json={"email": email, "password": "correct horse"}).status_code == 429
    with TestClient(main.app, client=("198.51.100.7", 4000)) as owner:  # the real owner, elsewhere
        assert owner.post("/auth/login", json={"email": email, "password": "correct horse"}).status_code == 200
        for _ in range(12):  # successful sign-ins are not counted as guesses
            assert owner.post("/auth/login", json={"email": email, "password": "correct horse"}).status_code == 200


def test_temporary_tables_cannot_stand_in_for_sessions_or_users(world):
    """Council re-check: kt_app made temp tables named sessions and users so the lookup functions read them."""
    with admin_db() as conn:
        acme_org = conn.execute("SELECT org_id FROM users WHERE email = %s", (f"lead-{world['tag']}@acme.test",)).fetchone()[0]
    with user_db(session_of(f"boss-{world['tag']}@rival.test")) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("CREATE TEMP TABLE sessions (token_hash text, user_id bigint, expires_at timestamptz)")
    with admin_db() as conn:  # even if a temp table did exist, the functions read the real ones
        conn.execute("CREATE TEMP TABLE users (id bigint, org_id bigint, role text)")
        conn.execute("INSERT INTO users VALUES (1, %s, 'admin')", (acme_org,))
        conn.execute("SELECT set_config('app.session', 'nothing', true)")
        assert conn.execute("SELECT kt_org_id(), kt_is_admin()").fetchone() == (None, False)


def test_database_lets_only_admins_delete(world):
    admin, bank = world["admin"], world["bank"]
    member_id = next(m["id"] for m in admin.get("/team").json() if m["role"] == "member")
    admin.put(f"/workspaces/{bank}/members/{member_id}")
    with user_db(session_of(f"dev-{world['tag']}@acme.test")) as conn:  # a member, with raw SQL
        assert conn.execute("SELECT count(*) FROM documents").fetchone()[0] == 1  # can read
        assert conn.execute("DELETE FROM chunks").rowcount == 0
        assert conn.execute("DELETE FROM documents").rowcount == 0
        assert conn.execute("DELETE FROM workspaces").rowcount == 0
        assert conn.execute("DELETE FROM workspace_members").rowcount == 0
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("INSERT INTO guides (workspace_id, sections, reading_order) VALUES (%s, '[]', '[]')", (bank,))
    assert len(admin.get(f"/workspaces/{bank}/documents").json()) == 1
