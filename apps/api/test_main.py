from fastapi.testclient import TestClient

from main import app


def test_health_answers_even_without_a_database(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    r = TestClient(app).get("/health")
    assert r.status_code == 200
    assert r.json() == {"api": "ok", "db": "down", "pgvector": None}


def test_answer_slot_is_released_when_a_stream_is_dropped_unread():
    import gc
    import threading

    import main

    slots = threading.BoundedSemaphore(1)
    slots.acquire()
    response = main.holding_slot(iter(['{"delta": "x"}\n']), slots)
    assert not slots.acquire(blocking=False)  # still held while the response exists
    del response
    gc.collect()
    assert slots.acquire(blocking=False)
