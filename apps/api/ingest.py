"""Bulk-load a folder into a workspace of an admin's organisation (creates the workspace if needed):

    python ingest.py <admin email> <workspace name> <folder>
"""
import sys
from pathlib import Path

import rag
from db import admin_db, init_schema


def workspace_id_for(conn, email: str, name: str) -> int:
    row = conn.execute("SELECT org_id FROM users WHERE email = %s AND role = 'admin'", (email.lower(),)).fetchone()
    if not row:
        sys.exit(f"No admin account with the email {email}. Sign up in the web app first.")
    conn.execute(
        "INSERT INTO workspaces (org_id, name) VALUES (%s, %s) ON CONFLICT DO NOTHING", (row[0], name)
    )
    return conn.execute("SELECT id FROM workspaces WHERE org_id = %s AND name = %s", (row[0], name)).fetchone()[0]


if __name__ == "__main__":
    email, name, folder = sys.argv[1], sys.argv[2], Path(sys.argv[3])
    init_schema()
    with admin_db() as conn:
        workspace_id = workspace_id_for(conn, email, name)
    files = sorted(p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() in rag.SUFFIXES)
    total = 0
    for i, path in enumerate(files, 1):
        try:
            with admin_db() as conn:  # one transaction per file
                result = rag.ingest(conn, workspace_id, str(path.relative_to(folder)), path.read_bytes())
        except ValueError as e:  # one unreadable file must not stop the batch
            result = {"status": f"skipped ({e})", "chunks": 0}
        total += result["chunks"]
        print(f"[{i}/{len(files)}] {path.relative_to(folder)}: {result['status']}, {result['chunks']} chunks", flush=True)
    print(f"done: {total} new chunks in workspace {name}")
