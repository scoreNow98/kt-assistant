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
    # The heading path keeps the notes' title as parent; no full email address in it.
    heading = hits[0]["heading"]
    assert hits[0]["document"] == name and heading.startswith(f"Handover notes from lead-{tag}, ")
    assert heading.endswith(" > Who owns the billing database?") and "@" not in heading
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


# ---------- hardening round ----------

def member_id_of(world):
    return next(m["id"] for m in world["admin"].get("/team").json() if m["role"] == "member")


def give_member(world, workspace_id):
    assert world["admin"].put(f"/workspaces/{workspace_id}/members/{member_id_of(world)}").status_code == 200


def upload(client, workspace_id, name, data, replace=False):
    return client.post(f"/workspaces/{workspace_id}/documents" + ("?replace=true" if replace else ""), files={"file": (name, data)})


def test_new_endpoints_refuse_other_companies_and_members(world):
    admin, member, rival, bank = world["admin"], world["member"], world["rival"], world["bank"]
    give_member(world, bank)
    _, lines = ask(admin, bank, "how do I restart the service?")
    message_id = lines[-1]["message_id"]
    doc_id = admin.get(f"/workspaces/{bank}/documents").json()[0]["id"]
    invite_id = admin.post("/invites", json={}).json()["id"]
    admin_id = next(p["id"] for p in admin.get("/team").json() if p["you"])

    # Another company's admin: the workspace, message, document, invite and person do not exist for them.
    assert rival.patch(f"/workspaces/{bank}", json={"name": "taken over"}).status_code == 404
    assert rival.delete(f"/workspaces/{bank}").status_code == 404
    assert rival.post(f"/workspaces/{bank}/gaps/dismiss", json={"question": "x"}).status_code == 404
    assert rival.put(f"/team/{admin_id}/role", json={"role": "member"}).status_code == 404
    assert rival.get("/invites").json() == []
    assert rival.delete(f"/invites/{invite_id}").status_code == 404
    assert rival.delete(f"/messages/{message_id}").status_code == 404
    assert rival.delete(f"/workspaces/{bank}/documents/{doc_id}").status_code == 404
    assert rival.delete(f"/workspaces/{bank}/messages").status_code == 404
    assert rival.get(f"/workspaces/{bank}/guide").status_code == 404
    # A plain member of the workspace: admin actions are refused, and other people's things are not theirs.
    assert member.patch(f"/workspaces/{bank}", json={"name": "taken over"}).status_code == 403
    assert member.delete(f"/workspaces/{bank}").status_code == 403
    assert member.post(f"/workspaces/{bank}/gaps/dismiss", json={"question": "x"}).status_code == 403
    assert member.put(f"/team/{admin_id}/role", json={"role": "member"}).status_code == 403
    assert member.get("/invites").status_code == 403
    assert member.delete(f"/invites/{invite_id}").status_code == 403
    assert member.delete(f"/messages/{message_id}").status_code == 404
    assert member.delete(f"/workspaces/{bank}/documents/{doc_id}").status_code == 403
    assert member.delete(f"/workspaces/{bank}/messages").json() == {"ok": True, "deleted": 0}  # not the admin's

    # Nothing changed.
    assert [w["name"] for w in admin.get("/me").json()["workspaces"]] == ["bank", "shop"]
    assert [m["id"] for m in admin.get(f"/workspaces/{bank}/messages").json()] == [message_id]
    assert [i["id"] for i in admin.get("/invites").json()] == [invite_id]
    assert admin.get("/me").json()["role"] == "admin"
    assert len(admin.get(f"/workspaces/{bank}/documents").json()) == 1


def test_uploader_removes_own_document_but_not_others(world):
    admin, member, bank = world["admin"], world["member"], world["bank"]
    give_member(world, bank)
    assert upload(member, bank, "mine.md", b"# Mine\nmy notes about the payment service\n").json()["status"] == "ready"
    docs = {d["name"]: d for d in member.get(f"/workspaces/{bank}/documents").json()}
    assert docs["mine.md"]["yours"] is True and docs["runbook.md"]["yours"] is False
    assert docs["mine.md"]["created_at"].startswith("20")
    assert admin.get(f"/workspaces/{bank}/documents").json()[0]["yours"] is False  # mine.md, not the admin's

    assert member.delete(f"/workspaces/{bank}/documents/{docs['runbook.md']['id']}").status_code == 403
    assert member.delete(f"/workspaces/{bank}/documents/{docs['mine.md']['id']}").status_code == 200
    assert [d["name"] for d in admin.get(f"/workspaces/{bank}/documents").json()] == ["runbook.md"]
    with admin_db() as conn:
        assert conn.execute("SELECT count(*) FROM chunks WHERE document_id = %s", (docs["mine.md"]["id"],)).fetchone()[0] == 0


def test_database_lets_uploaders_delete_only_their_own_documents(world):
    admin, member, bank = world["admin"], world["member"], world["bank"]
    give_member(world, bank)
    upload(member, bank, "mine.md", b"# Mine\nmy notes\n")
    with admin_db() as conn:
        member_id = member_id_of(world)
        runbook = conn.execute("SELECT id FROM documents WHERE workspace_id = %s AND name = 'runbook.md'", (bank,)).fetchone()[0]
    with user_db(session_of(f"dev-{world['tag']}@acme.test")) as conn:  # the member, with raw SQL
        assert conn.execute("DELETE FROM chunks").rowcount == 0
        assert conn.execute("UPDATE documents SET uploaded_by = %s", (member_id,)).rowcount == 0  # cannot claim one
        assert conn.execute("UPDATE chunks SET document_id = document_id").rowcount == 0
        with pytest.raises(psycopg.errors.InsufficientPrivilege):  # cannot record a document as someone else's
            conn.execute("INSERT INTO documents (workspace_id, name, sha256, uploaded_by) VALUES (%s, 'x', 'x', NULL)", (bank,))
    with user_db(session_of(f"dev-{world['tag']}@acme.test")) as conn:
        assert conn.execute("DELETE FROM documents").rowcount == 1  # only mine.md
    with admin_db() as conn:
        assert conn.execute("SELECT id FROM documents WHERE workspace_id = %s", (bank,)).fetchall() == [(runbook,)]
    with user_db(session_of(f"dev-{world['tag']}@acme.test")) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):  # whether an answer was found is the API's to say
            conn.execute("UPDATE messages SET not_found = false, dismissed = true")


def test_replace_needs_admin_or_uploader(world):
    admin, member, bank = world["admin"], world["member"], world["bank"]
    give_member(world, bank)
    new_runbook = b"# Deploy\nRestart the payment service with make restart-all.\n"

    r = upload(member, bank, "runbook.md", new_runbook)
    assert r.status_code == 409 and r.json()["can_replace"] is False and "runbook.md" in r.json()["detail"]
    assert upload(member, bank, "runbook.md", new_runbook, replace=True).status_code == 403
    r = upload(admin, bank, "runbook.md", new_runbook)
    assert r.status_code == 409 and r.json()["can_replace"] is True

    assert upload(member, bank, "notes.md", b"# Notes\nfirst\n").json()["status"] == "ready"
    r = upload(member, bank, "notes.md", b"# Notes\nsecond\n")
    assert r.status_code == 409 and r.json()["can_replace"] is True
    assert upload(member, bank, "notes.md", b"# Notes\nsecond\n", replace=True).json() == {"name": "notes.md", "status": "replaced", "chunks": 1}
    assert upload(member, bank, "notes.md", b"# Notes\nsecond\n").json()["status"] == "duplicate"  # same content again

    assert upload(admin, bank, "runbook.md", new_runbook, replace=True).json()["status"] == "replaced"
    r = upload(admin, bank, "runbook.md", b"", replace=True)
    assert r.status_code == 400 and r.json()["detail"] == "This file is empty."
    docs = admin.get(f"/workspaces/{bank}/documents").json()
    assert sorted(d["name"] for d in docs) == ["notes.md", "runbook.md"]
    _, lines = ask(admin, bank, "how do I restart the payment service?")
    assert any("restart-all" in s["text"] for s in lines[0]["sources"])
    assert not any("make restart." in s["text"] for s in lines[0]["sources"])  # the old one is gone


def test_failed_replace_keeps_the_old_document(world):
    admin, bank = world["admin"], world["bank"]
    before = admin.get(f"/workspaces/{bank}/documents").json()
    r = upload(admin, bank, "runbook.md", b"\x00\x00", replace=True)  # only NUL bytes: no text to store
    assert r.status_code == 400
    assert admin.get(f"/workspaces/{bank}/documents").json() == before  # same document, same id


def test_upload_file_names_are_cleaned(world):
    admin, bank = world["admin"], world["bank"]
    assert main.clean_file_name("a\x07b\u202e\x00.md") == "ab.md"  # control and invisible direction characters
    assert main.clean_file_name(" /\x01 ") == main.clean_file_name(None) == "upload"
    assert upload(admin, bank, "..\\..\\secret/dir/ab.md", b"# A\nfirst text\n").json()["name"] == "ab.md"
    long = "n" * 300 + ".md"
    assert upload(admin, bank, long, b"# Long\nsecond text\n").json()["name"] == "n" * 197 + ".md"
    with admin_db() as conn:
        names = [r[0] for r in conn.execute("SELECT name FROM documents WHERE workspace_id = %s", (bank,))]
    assert "ab.md" in names and all(len(n) <= 200 for n in names)


def test_ask_trims_and_flags_not_found(world, monkeypatch):
    admin, bank = world["admin"], world["bank"]
    assert ask(admin, bank, "   ")[0] == 422
    status, lines = ask(admin, bank, "  how do I restart the service?  ")
    assert status == 200 and lines[0]["searched_for"] == "how do I restart the service?"
    assert lines[-1]["not_found"] is False
    assert isinstance(lines[0]["sources"][0]["distance"], float)

    for reply in ("Use make restart.", "Use make restart [7]."):  # no citation; a citation of no source shown
        monkeypatch.setattr(rag, "chat_stream", lambda s, u, reply=reply: iter([reply]))
        assert ask(admin, bank, "how do I restart the service?")[1][-1]["not_found"] is True
    history = admin.get(f"/workspaces/{bank}/messages").json()
    assert [m["not_found"] for m in history] == [False, True, True]
    assert history[0]["question"] == "how do I restart the service?" and history[0]["created_at"].startswith("20")


def test_relevance_gate_answers_not_found_without_the_model(world, monkeypatch):
    admin, bank = world["admin"], world["bank"]
    calls = []
    monkeypatch.setattr(rag, "chat_stream", lambda s, u: calls.append(u) or iter(["Use make restart [1]"]))
    monkeypatch.setattr(rag, "MAX_DISTANCE", 0.0001)
    _, lines = ask(admin, bank, "restart the payment database")  # shares keywords, but is not close enough
    assert lines[0]["sources"] == [] and lines[-1]["not_found"] is True and calls == []
    assert "".join(line.get("delta", "") for line in lines) == rag.NOT_FOUND
    monkeypatch.setattr(rag, "MAX_DISTANCE", 2.0)
    _, lines = ask(admin, bank, "restart the payment database")
    assert lines[0]["sources"] and lines[-1]["not_found"] is False and len(calls) == 1


def test_busy_assistant_answers_429(world, monkeypatch):
    import threading

    admin, bank = world["admin"], world["bank"]
    slots = threading.BoundedSemaphore(1)
    monkeypatch.setattr(main, "_answer_slots", slots)
    slots.acquire()  # someone else's answer is being written
    status, body = ask(admin, bank, "restart?")
    assert status == 429 and body["detail"] == "The assistant is busy. Try again in a moment."
    assert admin.post(f"/workspaces/{bank}/guide").status_code == 429
    slots.release()
    for _ in range(2):  # each finished answer gives its slot back
        assert ask(admin, bank, "restart?")[0] == 200
    assert admin.post(f"/workspaces/{bank}/guide").status_code == 200

    def down(texts):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(rag, "embed", down)
    assert ask(admin, bank, "restart?")[0] == 502  # failing before the stream also gives it back
    monkeypatch.setattr(rag, "embed", fake_embed)
    assert ask(admin, bank, "restart?")[0] == 200


def test_people_delete_only_their_own_questions(world):
    admin, member, bank = world["admin"], world["member"], world["bank"]
    give_member(world, bank)
    mine = [ask(member, bank, f"question {i}?")[1][-1]["message_id"] for i in range(3)]
    theirs = ask(admin, bank, "admin question?")[1][-1]["message_id"]

    assert member.delete(f"/messages/{theirs}").status_code == 404
    assert member.delete(f"/messages/{mine[0]}").json() == {"ok": True}
    assert member.delete(f"/messages/{mine[0]}").status_code == 404
    assert [m["id"] for m in member.get(f"/workspaces/{bank}/messages").json()] == mine[1:]
    assert member.delete(f"/workspaces/{bank}/messages").json() == {"ok": True, "deleted": 2}
    assert member.get(f"/workspaces/{bank}/messages").json() == []
    assert [m["id"] for m in admin.get(f"/workspaces/{bank}/messages").json()] == [theirs]


def test_gap_closes_after_a_found_answer_and_after_dismiss(world, monkeypatch):
    admin, member, bank = world["admin"], world["member"], world["bank"]
    give_member(world, bank)
    monkeypatch.setattr(rag, "chat_stream", lambda s, u: iter([rag.NOT_FOUND]))
    ask(member, bank, "Who owns billing?")
    ask(member, bank, "asdf qwerty")
    gap_questions = lambda: [g["question"] for g in member.get(f"/workspaces/{bank}/gaps").json()]
    assert sorted(gap_questions()) == ["Who owns billing?", "asdf qwerty"]

    # Someone hands the answer over; the question is asked again and answered: the gap closes by itself.
    monkeypatch.setattr(rag, "chat_stream", lambda s, u: iter(["Priya owns it [1]."]))
    ask(admin, bank, "who owns billing?")
    assert gap_questions() == ["asdf qwerty"]
    # ...but an answer marked not helpful keeps it open.
    _, lines = ask(member, bank, "Who owns billing?")
    member.put(f"/messages/{lines[-1]['message_id']}/feedback", json={"value": -1})
    assert sorted(gap_questions()) == ["Who owns billing?", "asdf qwerty"]

    assert member.post(f"/workspaces/{bank}/gaps/dismiss", json={"question": "ASDF QWERTY"}).status_code == 403
    assert admin.post(f"/workspaces/{bank}/gaps/dismiss", json={"question": "ASDF QWERTY"}).json() == {"ok": True, "dismissed": 1}
    assert gap_questions() == ["Who owns billing?"]
    assert admin.post(f"/workspaces/{bank}/gaps/dismiss", json={"question": "asdf qwerty"}).json()["dismissed"] == 0


def test_gap_window_is_30_days(world, monkeypatch):
    admin, bank = world["admin"], world["bank"]
    monkeypatch.setattr(rag, "chat_stream", lambda s, u: iter([rag.NOT_FOUND]))
    ask(admin, bank, "old question?")
    ask(admin, bank, "recent question?")
    with admin_db() as conn:
        conn.execute("UPDATE messages SET created_at = now() - interval '31 days' WHERE workspace_id = %s AND question = 'old question?'", (bank,))
        conn.execute("UPDATE messages SET created_at = now() - interval '29 days' WHERE workspace_id = %s AND question = 'recent question?'", (bank,))
    assert [g["question"] for g in admin.get(f"/workspaces/{bank}/gaps").json()] == ["recent question?"]


def test_workspace_rename_delete_and_duplicate_names(world):
    admin, member, bank, shop = world["admin"], world["member"], world["bank"], world["shop"]
    give_member(world, bank)
    assert admin.post("/workspaces", json={"name": "  BANK "}).status_code == 409
    assert admin.post("/workspaces", json={"name": "   "}).status_code == 422
    assert admin.post("/workspaces", json={"name": "x" * 61}).status_code == 422
    r = admin.post("/workspaces", json={"name": "  Client \t  C  "})
    assert r.status_code == 200 and r.json()["name"] == "Client C"

    assert admin.patch(f"/workspaces/{shop}", json={"name": "Bank"}).status_code == 409
    assert admin.patch(f"/workspaces/{bank}", json={"name": "Bank"}).json() == {"id": bank, "name": "Bank"}  # own name, other case
    assert admin.patch(f"/workspaces/{bank}", json={"name": " Bank  Ltd "}).json() == {"id": bank, "name": "Bank Ltd"}
    assert member.get("/me").json()["workspaces"] == [{"id": bank, "name": "Bank Ltd"}]

    ask(member, bank, "restart?")
    admin.post(f"/workspaces/{bank}/guide")
    assert admin.delete(f"/workspaces/{bank}").json() == {"ok": True}
    assert member.get(f"/workspaces/{bank}/documents").status_code == 404
    assert admin.get(f"/workspaces/{bank}/messages").status_code == 404
    with admin_db() as conn:
        for table in ("documents", "chunks", "messages", "guides", "workspace_members"):
            assert conn.execute(f"SELECT count(*) FROM {table} WHERE workspace_id = %s", (bank,)).fetchone()[0] == 0
    assert [w["name"] for w in admin.get("/me").json()["workspaces"]] == ["Client C", "shop"]
    assert admin.delete(f"/workspaces/{bank}").status_code == 404


def test_role_change(world):
    admin, member = world["admin"], world["member"]
    admin_id = next(p["id"] for p in admin.get("/team").json() if p["you"])
    assert admin.put(f"/team/{admin_id}/role", json={"role": "member"}).status_code == 400
    assert admin.put(f"/team/{member_id_of(world)}/role", json={"role": "owner"}).status_code == 422
    member_id = member_id_of(world)
    assert admin.put(f"/team/{member_id}/role", json={"role": "admin"}).json() == {"ok": True}
    assert member.get("/me").json()["role"] == "admin" and member.get("/team").status_code == 200
    assert len(member.get("/me").json()["workspaces"]) == 2  # admins see every workspace
    assert member.put(f"/team/{admin_id}/role", json={"role": "member"}).status_code == 200
    assert admin.get("/team").status_code == 403


def test_invites_are_listed_and_revoked(world):
    admin, rival, tag = world["admin"], world["rival"], world["tag"]
    first = admin.post("/invites", json={}).json()
    second = admin.post("/invites", json={"role": "admin"}).json()
    rival.post("/invites", json={})
    listed = admin.get("/invites").json()
    assert [(i["id"], i["role"]) for i in listed] == [(second["id"], "admin"), (first["id"], "member")]  # the used one is not listed
    assert listed[0]["expires_at"].startswith("20") and "token" not in listed[0]

    assert rival.delete(f"/invites/{first['id']}").status_code == 404
    assert admin.delete(f"/invites/{first['id']}").json() == {"ok": True}
    assert admin.delete(f"/invites/{first['id']}").status_code == 404
    assert [i["id"] for i in admin.get("/invites").json()] == [second["id"]]
    with TestClient(main.app) as c:
        r = c.post("/auth/signup", json={"email": f"late-{tag}@acme.test", "password": "correct horse", "invite": first["token"]})
        assert r.status_code == 400
    with admin_db() as conn:
        conn.execute("UPDATE invites SET expires_at = now() - interval '1 second' WHERE id = %s", (second["id"],))
    assert admin.get("/invites").json() == []


def test_guide_goes_stale_when_documents_change(world):
    admin, member, bank = world["admin"], world["member"], world["bank"]
    give_member(world, bank)
    admin.post(f"/workspaces/{bank}/guide")
    assert member.get(f"/workspaces/{bank}/guide").json()["stale"] is False
    upload(member, bank, "extra.md", b"# Extra\nmore about the payment service\n")
    assert member.get(f"/workspaces/{bank}/guide").json()["stale"] is True
    admin.post(f"/workspaces/{bank}/guide")
    assert member.get(f"/workspaces/{bank}/guide").json()["stale"] is False
    extra = next(d["id"] for d in member.get(f"/workspaces/{bank}/documents").json() if d["name"] == "extra.md")
    member.delete(f"/workspaces/{bank}/documents/{extra}")
    assert member.get(f"/workspaces/{bank}/guide").json()["stale"] is True  # removed counts too


def test_handover_author_can_remove_their_notes(world):
    admin, member, bank = world["admin"], world["member"], world["bank"]
    give_member(world, bank)
    name = member.post(f"/workspaces/{bank}/handover", json={"answers": [{"question": "q", "answer": "a"}]}).json()["name"]
    doc = next(d for d in member.get(f"/workspaces/{bank}/documents").json() if d["name"] == name)
    assert doc["yours"] is True
    assert member.delete(f"/workspaces/{bank}/documents/{doc['id']}").status_code == 200


def test_ingest_replace_loads_a_folder_again(world, tmp_path, monkeypatch):
    import ingest

    bank = world["bank"]
    (tmp_path / "runbook.md").write_bytes(b"# Deploy\nRestart with make restart-v2.\n")
    assert ingest.load_file(bank, "runbook.md", (tmp_path / "runbook.md").read_bytes())["status"] == "ready"  # a second runbook.md
    assert ingest.load_file(bank, "runbook.md", b"# Deploy\nRestart with make restart-v3.\n", replace=True)["status"] == "ready"
    with admin_db() as conn:
        texts = [r[0] for r in conn.execute(
            "SELECT c.text FROM chunks c JOIN documents d ON d.id = c.document_id WHERE d.workspace_id = %s AND d.name = 'runbook.md'", (bank,))]
    assert texts == ["Restart with make restart-v3."]
    with pytest.raises(ValueError):  # unreadable: the old one stays
        ingest.load_file(bank, "runbook.md", b"\x00", replace=True)
    with admin_db() as conn:
        assert conn.execute("SELECT count(*) FROM documents WHERE workspace_id = %s AND name = 'runbook.md'", (bank,)).fetchone()[0] == 1


def test_removed_questions_are_wiped_but_still_counted(world, monkeypatch):
    admin, member, bank = world["admin"], world["member"], world["bank"]
    give_member(world, bank)
    monkeypatch.setattr(rag, "chat_stream", lambda s, u: iter([rag.NOT_FOUND]))
    secret = ask(member, bank, "what is the billing password?")[1][-1]["message_id"]
    assert [g["question"] for g in admin.get(f"/workspaces/{bank}/gaps").json()] == ["what is the billing password?"]
    assert admin.get("/usage").json()["used"]["questions"] == 1

    assert admin.delete(f"/messages/{secret}").status_code == 404  # another person, even an admin
    assert member.delete(f"/messages/{secret}").json() == {"ok": True}
    assert member.delete(f"/messages/{secret}").status_code == 404  # already removed
    assert member.put(f"/messages/{secret}/feedback", json={"value": -1}).status_code == 404
    assert member.get(f"/workspaces/{bank}/messages").json() == []
    assert admin.get(f"/workspaces/{bank}/gaps").json() == []
    assert admin.get("/usage").json()["used"]["questions"] == 1  # removing does not give questions back
    with admin_db() as conn:
        row = conn.execute("SELECT removed, question, searched_for, answer, sources, feedback FROM messages WHERE id = %s", (secret,)).fetchone()
    assert row == (True, "", "", "", [], None)

    ask(member, bank, "another?")
    assert member.delete(f"/workspaces/{bank}/messages").json() == {"ok": True, "deleted": 1}  # the removed one is not counted again
    assert admin.get("/usage").json()["used"]["questions"] == 2


def test_app_login_cannot_hard_delete_messages(world):
    ask(world["admin"], world["bank"], "restart?")
    with user_db(session_of(f"lead-{world['tag']}@acme.test")) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("DELETE FROM messages")
    assert len(world["admin"].get(f"/workspaces/{world['bank']}/messages").json()) == 1


def test_replace_is_allowed_at_the_document_limit(world, monkeypatch):
    admin, bank = world["admin"], world["bank"]
    monkeypatch.setitem(main.PLANS, "trial", {"questions": 9, "documents": 1, "people": 9})  # Acme has 1 document
    r = upload(admin, bank, "new.md", b"# New\nanother document\n")
    assert r.status_code == 403 and "documents" in r.json()["detail"]
    r = upload(admin, bank, "runbook.md", b"# Deploy\nRestart with make restart-v2.\n", replace=True)
    assert r.status_code == 200 and r.json()["status"] == "replaced"
    assert upload(admin, bank, "copy.md", b"# Deploy\nRestart with make restart-v2.\n").json()["status"] == "duplicate"
    assert [d["name"] for d in admin.get(f"/workspaces/{bank}/documents").json()] == ["runbook.md"]
