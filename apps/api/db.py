"""Database connections. Two kinds, on purpose:

admin_db()  full access; for accounts, sessions, invites and command-line tools.
            Every query through it must filter by organisation in the code.
user_db()   client data (workspaces, documents, chunks) as one user; Postgres row-level
            security hides everything that user may not open.
"""
import os
from contextlib import contextmanager
from pathlib import Path

import psycopg


def admin_db():
    return psycopg.connect(os.environ["DATABASE_URL"])


@contextmanager
def user_db(user_id: int):
    with admin_db() as conn:
        # shortcut: one login that switches role per transaction; give kt_app its own login
        # before production so a SQL injection cannot switch back.
        conn.execute("SET LOCAL ROLE kt_app")
        conn.execute("SELECT set_config('app.user_id', %s, true)", (str(user_id),))
        yield conn


def init_schema():
    with admin_db() as conn:
        conn.execute(Path(__file__).with_name("schema.sql").read_text())
