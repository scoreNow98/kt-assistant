"""End-to-end tests through the HTTP API. Need Postgres with pgvector; the models are faked."""
import json
import os
import uuid

import httpx

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


@pytest.fixture
def world(monkeypatch):
    """Two companies. Acme has an admin, a member, and workspaces 'bank' and 'shop'. Rival has one admin."""
    monkeypatch.setattr(rag, "embed", fake_embed)
    monkeypatch.setattr(rag, "chat", lambda system, user: "unused")
    monkeypatch.setattr(rag, "chat_stream", lambda system, user: iter(["Use make ", "restart [1]"]))
    tag = uuid.uuid4().hex[:8]

    def client():
        return TestClient(main.app)

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
    with admin_db() as conn:
        rival_id = conn.execute("SELECT id FROM users WHERE email = %s", (f"boss-{world['tag']}@rival.test",)).fetchone()[0]
        lead_id = conn.execute("SELECT id FROM users WHERE email = %s", (f"lead-{world['tag']}@acme.test",)).fetchone()[0]
    with user_db(rival_id) as conn:
        assert conn.execute("SELECT count(*) FROM chunks").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM documents").fetchone()[0] == 0
        assert conn.execute("UPDATE chunks SET text = 'x'").rowcount == 0
        with pytest.raises(Exception):  # cannot plant a document in someone else's workspace
            conn.execute("INSERT INTO documents (workspace_id, name, sha256) VALUES (%s, 'x', 'x')", (world["bank"],))
    with user_db(lead_id) as conn:
        assert conn.execute("SELECT count(*) FROM chunks").fetchone()[0] == 1
        with pytest.raises(Exception):  # the app role can never read password hashes
            conn.execute("SELECT password_hash FROM users")


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
    with admin_db() as conn:
        rival_id = conn.execute("SELECT id FROM users WHERE email = %s", (f"boss-{world['tag']}@rival.test",)).fetchone()[0]
    with user_db(rival_id) as conn:
        assert conn.execute("SELECT count(*) FROM guides").fetchone()[0] == 0
