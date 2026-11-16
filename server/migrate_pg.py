"""Copy the ExamFlow SQLite dataset into the managed Postgres instance.

The sandbox that builds this project can only reach the internet over HTTPS, so
the actual migration runs inside the deployed Vercel function (which has full
network access) and is triggered through ``POST /api/admin/migrate``. The same
module powers ``tools/migrate_to_postgres.py`` for local runs.
"""
import base64
import gzip
import os
import sqlite3
import tempfile
import urllib.request

import db

SNAPSHOT_REPO = (os.environ.get("EXAMFLOW_SNAPSHOT_REPO") or "yogesh9-max/examflow-ai").strip()
SNAPSHOT_TOKEN = (os.environ.get("EXAMFLOW_GITHUB_TOKEN") or "").strip()
SNAPSHOT_PATH = (os.environ.get("EXAMFLOW_SNAPSHOT_PATH") or "data/examflow.db.gz").strip()
SNAPSHOT_BRANCH = (os.environ.get("EXAMFLOW_SNAPSHOT_BRANCH") or "main").strip()


def download_snapshot(dest=None):
    """Fetch the committed SQLite snapshot and write the raw database file."""
    dest = dest or os.path.join(tempfile.gettempdir(), "examflow_snapshot.db")
    url = "https://api.github.com/repos/%s/contents/%s?ref=%s" % (
        SNAPSHOT_REPO, SNAPSHOT_PATH, SNAPSHOT_BRANCH)
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "ExamFlowAI"}
    if SNAPSHOT_TOKEN:
        headers["Authorization"] = "Bearer " + SNAPSHOT_TOKEN
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=30) as response:
        blob = json_load(response.read())
    payload = base64.b64decode(blob.get("content") or "")
    raw = gzip.decompress(payload) if payload[:2] == b"\x1f\x8b" else payload
    with open(dest, "wb") as handle:
        handle.write(raw)
    return dest


def json_load(data):
    import json
    return json.loads(data or b"{}")


def table_order(source):
    """Every source table, parents before children."""
    tables = [row["name"] for row in source.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()]
    parents = {table: set() for table in tables}
    for table in tables:
        for fk in source.execute("PRAGMA foreign_key_list(%s)" % table).fetchall():
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


def migrate_from_sqlite(source_path, reset=False):
    """Create the schema and copy every row into Postgres. Safe to re-run.

    With ``reset`` the target tables are emptied first so demo/seed rows that the
    empty database created on first boot cannot survive next to the real dataset.
    """
    source = sqlite3.connect(source_path)
    source.row_factory = sqlite3.Row
    target = db.connect()
    summary = {"tables": {}, "records": 0, "skipped": []}
    try:
        target.executescript(db.SCHEMA)
        target.commit()
        db._ensure_user_columns()
        db.ensure_group_index()
        if reset:
            target_tables = [row["name"] for row in target.execute(
                "SELECT table_name AS name FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'").fetchall()]
            if target_tables:
                target.execute("TRUNCATE TABLE %s RESTART IDENTITY CASCADE" % ",".join(target_tables))
                target.commit()
            summary["reset"] = True
        for table in table_order(source):
            target_columns = {row["name"] for row in
                              target.execute("PRAGMA table_info(%s)" % table).fetchall()}
            if not target_columns:
                summary["skipped"].append(table)
                continue
            rows = source.execute("SELECT * FROM %s" % table).fetchall()
            if not rows:
                continue
            columns = [column for column in rows[0].keys() if column in target_columns]
            sql = "INSERT OR IGNORE INTO %s (%s) VALUES (%s)" % (
                table, ",".join(columns), ",".join("?" for _ in columns))
            copied = 0
            for row in rows:
                target.execute(sql, tuple(row[column] for column in columns))
                copied += 1
            target.commit()
            summary["tables"][table] = copied
            summary["records"] += copied
    finally:
        target.close()
        source.close()
    return summary


def migrate_snapshot(reset=False):
    path = download_snapshot()
    try:
        return migrate_from_sqlite(path, reset=reset)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
