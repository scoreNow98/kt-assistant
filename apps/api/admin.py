"""Operator commands, run on the server:

    python admin.py set-plan <any email in the company> <plan>
    python admin.py usage <any email in the company>
"""
import json
import sys

from db import admin_db
from main import PLANS, usage


def org_of(conn, email: str) -> int:
    row = conn.execute("SELECT org_id FROM users WHERE email = %s", (email.lower(),)).fetchone()
    if not row:
        sys.exit(f"No account with the email {email}")
    return row[0]


def set_plan(email: str, plan: str) -> dict:
    if plan not in PLANS:
        sys.exit(f"Unknown plan {plan}. Plans: {', '.join(PLANS)}")
    with admin_db() as conn:
        org_id = org_of(conn, email)
        conn.execute("UPDATE organisations SET plan = %s WHERE id = %s", (plan, org_id))
        return usage(conn, org_id)


if __name__ == "__main__":
    command, *args = sys.argv[1:] or ["help"]
    if command == "set-plan" and len(args) == 2:
        print(json.dumps(set_plan(*args), indent=2))
    elif command == "usage" and len(args) == 1:
        with admin_db() as conn:
            print(json.dumps(usage(conn, org_of(conn, args[0])), indent=2))
    else:
        sys.exit(__doc__)
