from fastapi.testclient import TestClient

from main import app


def test_health_answers_even_without_a_database(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    r = TestClient(app).get("/health")
    assert r.status_code == 200
    assert r.json() == {"api": "ok", "db": "down", "pgvector": None}
