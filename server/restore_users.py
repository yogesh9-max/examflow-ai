"""One-off recovery helper: put the users table back from the committed snapshot.

The production users table was emptied by mistake during a QA sweep. The SQLite
snapshot committed at ``data/examflow.db.gz`` still holds the original accounts
including their password hashes, so this module copies just that table back.
It never touches any other table.
"""

import sqlite3

import db
import migrate_pg

COLUMNS = ("id", "full_name", "email", "password_hash", "role", "school_id", "department_id",
           "is_active", "phone", "created_at", "student_group_id", "roll_number")


def restore_users():
    path = migrate_pg.download_snapshot()
    source = sqlite3.connect(path)
    source.row_factory = sqlite3.Row
    available = [row[1] for row in source.execute("PRAGMA table_info(users)")]
    columns = [c for c in COLUMNS if c in available]
    rows = source.execute("SELECT %s FROM users" % ", ".join(columns)).fetchall()
    source.close()

    conn = db.connect()
    existing = {r[0] for r in conn.execute("SELECT id FROM users").fetchall()}
    inserted, skipped = [], []
    for row in rows:
        record = {c: row[c] for c in columns}
        if record["id"] in existing:
            skipped.append(record["email"])
            continue
        placeholders = ", ".join("?" for _ in columns)
        conn.execute("INSERT OR IGNORE INTO users (%s) VALUES (%s)"
                     % (", ".join(columns), placeholders),
                     tuple(record[c] for c in columns))
        inserted.append(record["email"])
    conn.commit()
    total = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    conn.close()
    return {"inserted": inserted, "skipped": skipped, "users_now": total}
