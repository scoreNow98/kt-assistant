"""Retrieval check: is the expected document among the chunks found for each question?

"expect" is one page, or a list when several pages answer the question.
Run after loading the sample data:  python eval.py <admin email>
Prints the score for vector search alone and for hybrid (vector + keyword) search, which the app uses.
It checks search only. Read a few answers yourself to judge answer quality.
"""
import json
import sys
from pathlib import Path

import rag
from db import admin_db

questions = json.loads(Path(__file__).with_name("eval_questions.json").read_text())
scores = {"vector only": 0, "hybrid": 0}
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
        expects = q["expect"] if isinstance(q["expect"], list) else [q["expect"]]  # any of these pages is right
        found = {}
        for mode, keyword in (("vector only", False), ("hybrid", True)):
            found[mode] = [s["document"] for s in rag.search(conn, ids[q["workspace"]], q["question"], keyword=keyword)]
            scores[mode] += any(e in name for e in expects for name in found[mode])
        ok = any(e in name for e in expects for name in found["hybrid"])
        print(f"{'ok  ' if ok else 'MISS'} [{q['workspace']}] {q['question']}" + ("" if ok else f"\n       wanted {' or '.join(expects)}, got {found['hybrid'][:3]}"))
print()
for mode, score in scores.items():
    print(f"{mode:>12}: {score}/{len(questions)} questions found the right document")
