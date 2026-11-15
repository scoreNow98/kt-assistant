"""End-to-end tests through the HTTP API. Need Postgres with pgvector; the models are faked."""
import os
import uuid

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
    monkeypatch.setattr(rag, "chat", lambda system, user: "Use make restart [1]")
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


def test_signed_out_requests_are_refused(world):
    assert world["anonymous"].get("/me").status_code == 401
    assert world["anonymous"].post(f"/workspaces/{world['bank']}/ask", json={"question": "restart?"}).status_code == 401


def test_admin_asks_and_gets_cited_sources(world):
    r = world["admin"].post(f"/workspaces/{world['bank']}/ask", json={"question": "how do I restart the service?"}).json()
    assert r["answer"] == "Use make restart [1]"
    assert [(s["n"], s["document"], s["heading"]) for s in r["sources"]] == [(1, "runbook.md", "Deploy")]
    again = world["admin"].post(f"/workspaces/{world['bank']}/documents", files={"file": ("copy.md", b"# Deploy\nRestart the payment service with make restart.\n")})
    assert again.json()["status"] == "duplicate"


def test_another_company_cannot_reach_the_workspace(world):
    rival, bank = world["rival"], world["bank"]
    assert rival.get("/me").json()["workspaces"] == []
    assert rival.post(f"/workspaces/{bank}/ask", json={"question": "restart?"}).status_code == 404
    assert rival.get(f"/workspaces/{bank}/documents").status_code == 404
    assert rival.post(f"/workspaces/{bank}/documents", files={"file": ("x.md", b"# x\ny\n")}).status_code == 404
    member_id = world["admin"].get("/team").json()[0]["id"]
    assert rival.put(f"/workspaces/{bank}/members/{member_id}").status_code == 404


def test_member_sees_only_assigned_workspaces(world):
    admin, member, bank, shop = world["admin"], world["member"], world["bank"], world["shop"]
    assert member.get("/me").json()["workspaces"] == []
    assert member.post(f"/workspaces/{bank}/ask", json={"question": "restart?"}).status_code == 404
    assert member.post("/workspaces", json={"name": "mine"}).status_code == 403
    assert member.get("/team").status_code == 403

    member_id = next(m["id"] for m in admin.get("/team").json() if m["role"] == "member")
    assert admin.put(f"/workspaces/{bank}/members/{member_id}").status_code == 200
    assert [w["name"] for w in member.get("/me").json()["workspaces"]] == ["bank"]
    assert member.post(f"/workspaces/{bank}/ask", json={"question": "restart?"}).status_code == 200
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
