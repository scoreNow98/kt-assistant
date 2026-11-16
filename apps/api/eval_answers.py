"""Answer check: runs the real pipeline (search, fit, answer) for every question in eval_questions.json
and scores the answers. Needs the model server, so it is slow; run after loading the sample data:

    python eval_answers.py <admin email>

An answerable question passes when the answer contains one of its "must_contain" words (if given, any
case), cites a source from an expected document, and every number it cites was among the sources.
An unanswerable one ("expect": null) passes when the answer is "not found", as the app would mark it.
"""
import json
import sys
from pathlib import Path

import rag
from db import admin_db
from eval import expected, workspace_ids


def score(entry: dict, answer: str, sources: list[dict]) -> tuple[bool, str]:
    """(passed, reason). Pure, so it is tested without a model."""
    expects = expected(entry)
    if not expects:
        return (True, "not found, as expected") if rag.is_not_found(answer, sources) else (False, "answered, but should say not found")
    if rag.is_not_found(answer, sources):
        return False, "not found"
    cited = rag.cited_order(answer)
    by_number = {s["n"]: s["document"] for s in sources}
    if missing := [n for n in cited if n not in by_number]:
        return False, f"cites [{', '.join(map(str, missing))}], which was not a source"
    if not any(e in by_number[n] for n in cited for e in expects):
        return False, f"cites {sorted({by_number[n] for n in cited})}, wanted {' or '.join(expects)}"
    words = entry.get("must_contain") or []
    if words and not any(w.lower() in answer.lower() for w in words):
        return False, f"missing all of {words}"
    return True, "ok"


def main(email: str) -> None:
    questions = json.loads(Path(__file__).with_name("eval_questions.json").read_text())
    passed = {"answerable": [0, 0], "unanswerable": [0, 0]}
    with admin_db() as conn:
        ids = workspace_ids(conn, email)
        for q in questions:
            if q["workspace"] not in ids:
                sys.exit(f"Workspace {q['workspace']} not found for {email}. Load the sample data first.")
            sources = rag.fit(rag.search(conn, ids[q["workspace"]], q["question"]))
            answer = "".join(rag.answer_stream(sources, q["question"]))
            ok, reason = score(q, answer, sources)
            kind = "answerable" if expected(q) else "unanswerable"
            passed[kind][0] += ok
            passed[kind][1] += 1
            print(f"{'ok  ' if ok else 'FAIL'} [{q['workspace']}] {q['question']}" + ("" if ok else f"\n       {reason}"), flush=True)
    print()
    for kind, (good, total) in passed.items():
        print(f"{kind:>12}: {good}/{total} passed")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
