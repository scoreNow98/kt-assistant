"""Retrieval check: is the expected document among the chunks found for each question?

"expect" is one page, or a list when several pages answer the question, or null for a question the
documents cannot answer (skipped in the score, used for the distance ranges below).
Run after loading the sample data:  python eval.py <admin email>
Prints the score for vector search alone and for hybrid (vector + keyword) search, which the app uses,
the best vector distance per question, and the distance ranges of answerable and unanswerable questions
with a suggested MAX_DISTANCE when they do not overlap.
It checks search only; eval_answers.py checks the answers.
"""
import json
import sys
from pathlib import Path

import rag
from db import admin_db


def expected(q: dict) -> list[str]:
    """The pages that answer the question; empty for an unanswerable one."""
    expect = q.get("expect")
    return [] if expect is None else expect if isinstance(expect, list) else [expect]


def suggest_max_distance(answerable: list[float], unanswerable: list[float]) -> float | None:
    """Halfway between the farthest answerable and the nearest unanswerable question, when they do not overlap."""
    if not answerable or not unanswerable or max(answerable) >= min(unanswerable):
        return None
    return round((max(answerable) + min(unanswerable)) / 2, 3)


def workspace_ids(conn, email: str) -> dict:
    return dict(
        conn.execute(
            "SELECT w.name, w.id FROM workspaces w JOIN users u ON u.org_id = w.org_id WHERE u.email = %s",
            (email.lower(),),
        ).fetchall()
    )


def main(email: str) -> None:
    rag.MAX_DISTANCE = None  # measure every match, also the ones a gate would drop
    questions = json.loads(Path(__file__).with_name("eval_questions.json").read_text())
    scores = {"vector only": 0, "hybrid": 0}
    distances = {"answerable": [], "unanswerable": []}
    with admin_db() as conn:
        ids = workspace_ids(conn, email)
        for q in questions:
            if q["workspace"] not in ids:
                sys.exit(f"Workspace {q['workspace']} not found for {email}. Load the sample data first.")
            expects = expected(q)  # any of these pages is right
            found = {}
            for mode, keyword in (("vector only", False), ("hybrid", True)):
                found[mode] = rag.search(conn, ids[q["workspace"]], q["question"], keyword=keyword)
            best = min((s["distance"] for s in found["vector only"]), default=None)
            if best is not None:
                distances["answerable" if expects else "unanswerable"].append(best)
            shown = "-" if best is None else f"{best:.3f}"
            if not expects:
                print(f"n/a  [{q['workspace']}] {q['question']}  (unanswerable, distance {shown})")
                continue
            for mode in scores:
                scores[mode] += any(e in s["document"] for e in expects for s in found[mode])
            got = [s["document"] for s in found["hybrid"]]
            ok = any(e in name for e in expects for name in got)
            print(f"{'ok  ' if ok else 'MISS'} [{q['workspace']}] {q['question']}  (distance {shown})"
                  + ("" if ok else f"\n       wanted {' or '.join(expects)}, got {got[:3]}"))
    answerable = sum(1 for q in questions if expected(q))
    print()
    for mode, score in scores.items():
        print(f"{mode:>12}: {score}/{answerable} questions found the right document")
    for kind, values in distances.items():
        if values:
            print(f"{kind:>12}: best distance {min(values):.3f} to {max(values):.3f} ({len(values)} questions)")
    suggestion = suggest_max_distance(distances["answerable"], distances["unanswerable"])
    if suggestion is not None:
        print(f"The ranges do not overlap: try MAX_DISTANCE={suggestion}")
    elif distances["answerable"] and distances["unanswerable"]:
        print("The ranges overlap: a MAX_DISTANCE would turn away answerable questions too. Leave it unset.")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
