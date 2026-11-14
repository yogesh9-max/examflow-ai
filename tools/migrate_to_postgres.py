"""Copy an ExamFlow SQLite database into the managed Postgres instance.

Usage (from the repository root):

    DATABASE_URL=postgresql://... python tools/migrate_to_postgres.py [sqlite_path]

The SQLite path defaults to ``data/examflow.db`` (the restored snapshot file) or
``$EXAMFLOW_DB``. Rows are copied table by table in foreign-key order and the
statements are conflict-tolerant, so re-running the migration never duplicates a
record.
"""
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))

import db  # noqa: E402
import pgdb  # noqa: E402


def table_order(conn):
    """Every business table, parents before children."""
    tables = [r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()]
    parents = {t: set() for t in tables}
    for table in tables:
        for fk in conn.execute("PRAGMA foreign_key_list(%s)" % table).fetchall():
            target = fk[2]
            if target in parents and target != table:
                parents[table].add(target)
    ordered, seen = [], set()

    def visit(table, stack=()):
        if table in seen or table in stack:
            return
        for parent in sorted(parents[table]):
            visit(parent, stack + (table,))
        if table not in seen:
            seen.add(table)
            ordered.append(table)

    for table in tables:
        visit(table)
    return ordered


def main():
    if not pgdb.enabled():
        raise SystemExit("DATABASE_URL is not set to a postgres:// URL")

    source_path = sys.argv[1] if len(sys.argv) > 1 else (
        os.environ.get("EXAMFLOW_DB") or os.path.join(ROOT, "data", "examflow.db"))
    if not os.path.exists(source_path):
        raise SystemExit("SQLite database not found: %s" % source_path)

    source = sqlite3.connect(source_path)
    source.row_factory = sqlite3.Row

    target = db.connect()
    try:
        target.executescript(db.SCHEMA)
        target.commit()
        db._ensure_user_columns()
        total = 0
        for table in table_order(source):
            target_columns = {r["name"] for r in target.execute("PRAGMA table_info(%s)" % table).fetchall()}
            if not target_columns:
                print("  %-26s skipped (not part of the current schema)" % table)
                continue
            rows = source.execute("SELECT * FROM %s" % table).fetchall()
            if not rows:
                continue
            columns = [column for column in rows[0].keys() if column in target_columns]
            placeholders = ",".join("?" for _ in columns)
            sql = "INSERT OR IGNORE INTO %s (%s) VALUES (%s)" % (
                table, ",".join(columns), placeholders)
            copied = 0
            for row in rows:
                target.execute(sql, tuple(row[column] for column in columns))
                copied += 1
            target.commit()
            total += copied
            print("  %-26s %5d rows" % (table, copied))
        print("Migrated %d rows from %s" % (total, source_path))
        for table in ("schools", "users", "examinations", "rooms", "invigilators"):
            count = target.execute("SELECT COUNT(*) AS total FROM %s" % table).fetchone()["total"]
            print("  verify %-14s %d" % (table, count))
    finally:
        target.close()
        source.close()


if __name__ == "__main__":
    main()
