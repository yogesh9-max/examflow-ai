"""Vercel entry point.

With ``DATABASE_URL`` set the whole app runs on the managed Postgres instance and
the schema/migration pass runs on every cold start so additive changes land.
Without it the SQLite file lives in /tmp and is seeded on a cold start.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))

os.environ.setdefault("EXAMFLOW_DB", "/tmp/examflow.db")
os.environ.setdefault("EXAMFLOW_QUIET", "1")

import db  # noqa: E402

if db.using_postgres() or not os.path.exists(db.DB_PATH):
    db.init_db(seed=os.environ.get("EXAMFLOW_SEED", "1") != "0")

from wsgi import application  # noqa: E402,F401
