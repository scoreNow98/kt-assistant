"""Bulk-load a folder into a workspace of an admin's organisation (creates the workspace if needed):

    python ingest.py [--replace] <admin email> <workspace name> <folder>

--replace first removes documents with the same name as a file, so a folder can be loaded again
(for example after a change to chunking). A file that cannot be read keeps its old document.
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


def load_file(workspace_id: int, name: str, data: bytes, replace: bool = False) -> dict:
    with admin_db() as conn:  # one transaction per file
        if replace:
            conn.execute("DELETE FROM documents WHERE workspace_id = %s AND name = %s", (workspace_id, name))
        return rag.ingest(conn, workspace_id, name, data)


if __name__ == "__main__":
    replace = "--replace" in sys.argv[1:]
    args = [a for a in sys.argv[1:] if a != "--replace"]
    if len(args) != 3:
        sys.exit(__doc__)
    email, name, folder = args[0], args[1], Path(args[2])
    init_schema()
    with admin_db() as conn:
        workspace_id = workspace_id_for(conn, email, name)
    files = sorted(p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() in rag.SUFFIXES)
    total = 0
    for i, path in enumerate(files, 1):
        try:
            result = load_file(workspace_id, str(path.relative_to(folder)), path.read_bytes(), replace)
        except ValueError as e:  # one unreadable file must not stop the batch
            result = {"status": f"skipped ({e})", "chunks": 0}
        total += result["chunks"]
        print(f"[{i}/{len(files)}] {path.relative_to(folder)}: {result['status']}, {result['chunks']} chunks", flush=True)
    print(f"done: {total} new chunks in workspace {name}")
