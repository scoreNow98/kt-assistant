"""Database connections. Two kinds, on purpose:

admin_db()  the database owner; for accounts, sessions, invites, usage and command-line tools.
            Every query through it must filter by organisation in the code.
user_db()   client data (workspaces, documents, chunks, messages, guides) as one signed-in person,
            through the separate kt_app login. Postgres row-level security hides everything that person
            may not open. The person is identified by their secret session token (see schema.sql), which
            kt_app cannot read or forge, and kt_app cannot switch to another role.
"""
import hashlib
import os
from contextlib import contextmanager
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo


def admin_db():
    return psycopg.connect(os.environ["DATABASE_URL"])


@contextmanager
def user_db(session_token: str):
    app_login = make_conninfo(os.environ["DATABASE_URL"], user="kt_app", password=os.environ["APP_DB_PASSWORD"])
    with psycopg.connect(app_login) as conn:
        conn.execute("SELECT set_config('app.session', %s, true)", (session_token,))
        yield conn


def init_schema():
    """Applies schema.sql when it changed since the last run. Re-running unchanged DDL on every start would
    take exclusive table locks while other programs (the API, ingest.py) are using the tables."""
    schema = Path(__file__).with_name("schema.sql").read_text()
    digest = hashlib.sha256(schema.encode()).hexdigest()
    with admin_db() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(7270001)")  # one schema run at a time
        conn.execute("CREATE TABLE IF NOT EXISTS schema_state (id int PRIMARY KEY, digest text NOT NULL)")
        applied = conn.execute("SELECT digest FROM schema_state WHERE id = 1").fetchone()
        if applied is None or applied[0] != digest:
            conn.execute(schema)
            conn.execute(
                "INSERT INTO schema_state VALUES (1, %s) ON CONFLICT (id) DO UPDATE SET digest = excluded.digest",
                (digest,),
            )
        conn.execute(
            sql.SQL("ALTER ROLE kt_app WITH LOGIN PASSWORD {}").format(sql.Literal(os.environ["APP_DB_PASSWORD"]))
        )
