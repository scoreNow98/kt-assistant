import logging
import os

import psycopg
from fastapi import FastAPI

app = FastAPI(title="KT Assistant API")
log = logging.getLogger("kt")


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
