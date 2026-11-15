"""Retrieval check: is the expected document among the chunks found for each question?

Run after loading the sample data:  python eval.py <admin email>
It checks search only. Read a few answers yourself to judge answer quality.
"""
import json
import sys
from pathlib import Path

import rag
from db import admin_db

questions = json.loads(Path(__file__).with_name("eval_questions.json").read_text())
hits = 0
with admin_db() as conn:
    ids = dict(
        conn.execute(
            "SELECT w.name, w.id FROM workspaces w JOIN users u ON u.org_id = w.org_id WHERE u.email = %s",
            (sys.argv[1].lower(),),
        ).fetchall()
    )
    for q in questions:
        if q["workspace"] not in ids:
            sys.exit(f"Workspace {q['workspace']} not found for {sys.argv[1]}. Load the sample data first.")
        found = [s["document"] for s in rag.search(conn, ids[q["workspace"]], q["question"])]
        ok = any(q["expect"] in name for name in found)
        hits += ok
        print(f"{'ok  ' if ok else 'MISS'} [{q['workspace']}] {q['question']}" + ("" if ok else f"\n       wanted {q['expect']}, got {found[:3]}"))
print(f"\n{hits}/{len(questions)} questions found the right document")
