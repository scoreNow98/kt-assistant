"""Retrieval check: is the expected document among the chunks found for each question?

Run after loading the sample data:  python eval.py
It checks search only. Read a few answers yourself to judge answer quality.
"""
import json
from pathlib import Path

import rag

questions = json.loads(Path(__file__).with_name("eval_questions.json").read_text())
hits = 0
for q in questions:
    found = [s["document"] for s in rag.search(q["workspace"], q["question"])]
    ok = any(q["expect"] in name for name in found)
    hits += ok
    print(f"{'ok  ' if ok else 'MISS'} [{q['workspace']}] {q['question']}" + ("" if ok else f"\n       wanted {q['expect']}, got {found[:3]}"))
print(f"\n{hits}/{len(questions)} questions found the right document")
