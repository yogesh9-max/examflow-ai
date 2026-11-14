"""Copy an ExamFlow SQLite database into the managed Postgres instance.

Usage (from the repository root):

    DATABASE_URL=postgresql://... python tools/migrate_to_postgres.py [sqlite_path]

The logic lives in ``server/migrate_pg.py`` because the deployed application
exposes the same migration through ``POST /api/admin/migrate``.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))

import db  # noqa: E402
import migrate_pg  # noqa: E402
import pgdb  # noqa: E402


def main():
    if not pgdb.enabled():
        raise SystemExit("DATABASE_URL is not set to a postgres:// URL")
    source_path = sys.argv[1] if len(sys.argv) > 1 else (
        os.environ.get("EXAMFLOW_DB") or os.path.join(ROOT, "data", "examflow.db"))
    if not os.path.exists(source_path):
        raise SystemExit("SQLite database not found: %s" % source_path)
    summary = migrate_pg.migrate_from_sqlite(source_path)
    for table, count in sorted(summary["tables"].items()):
        print("  %-26s %5d rows" % (table, count))
    for table in summary["skipped"]:
        print("  %-26s skipped (not part of the current schema)" % table)
    print("Migrated %d rows from %s" % (summary["records"], source_path))
    target = db.connect()
    try:
        for table in ("schools", "users", "examinations", "rooms", "invigilators"):
            count = target.execute("SELECT COUNT(*) AS total FROM %s" % table).fetchone()["total"]
            print("  verify %-14s %d" % (table, count))
    finally:
        target.close()


if __name__ == "__main__":
    main()
