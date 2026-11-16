"""Student pages must show one batch only, and stay cheap as the data grows."""
import json
import os
import sys
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))

_tmp = tempfile.mkdtemp(prefix="examflow-scope-")
os.environ.pop("DATABASE_URL", None)
os.environ["EXAMFLOW_DB"] = os.path.join(_tmp, "scope.db")

import db  # noqa: E402
import api  # noqa: E402


def _build(tmp_path):
    conn = db.connect()
    conn.executescript(db.SCHEMA)
    existing = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
    for column in ("student_group_id", "roll_number"):
        if column not in existing:
            conn.execute("ALTER TABLE users ADD COLUMN %s TEXT" % column)
    # the fixture is idempotent so it can share a process with the other test modules
    for table, ids in (("schedule_assignments", ("a0", "a1", "a2", "a3")),
                       ("invigilator_assignments", ()),
                       ("examinations", ("e1", "e2", "e3", "e4")),
                       ("scheduling_runs", ("run1",)), ("rooms", ("r1",)),
                       ("users", ("u3", "u5")),
                       ("student_groups", ("g3", "g5")), ("schools", ("sch1",))):
        for rid in ids:
            conn.execute("DELETE FROM %s WHERE id = ?" % table, (rid,))
    now = db.now_iso()
    conn.execute("INSERT INTO schools (id, school_name, school_code, created_at) VALUES (?,?,?,?)",
                 ("sch1", "LPU", "LPU", now))
    for gid, name, sem in (("g3", "B.Tech CSE Sem 3", 3), ("g5", "B.Tech CSE Sem 5", 5)):
        conn.execute("""INSERT INTO student_groups
            (id, school_id, group_name, semester, section, student_count, academic_year, created_at)
            VALUES (?,?,?,?,?,?,?,?)""", (gid, "sch1", name, sem, "A", 60, "2026-27", now))
    exams = [("e1", "CS301", "Data Structures", ["g3"]),
             ("e2", "MA201", "Discrete Mathematics", ["g3"]),
             ("e3", "CS302", "Operating Systems", ["g5"]),
             ("e4", "CS303", "Computer Networks", ["g5", "extra"])]
    for eid, code, name, groups in exams:
        conn.execute("""INSERT INTO examinations
            (id, school_id, subject_name, subject_code, student_group_ids, duration_minutes,
             priority, preferred_dates, required_seating_capacity, required_facilities, status,
             created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (eid, "sch1", name, code, json.dumps(groups), 120, "High", "[]", 60, "[]",
             "Scheduled", now))
    conn.execute("""INSERT INTO scheduling_runs
        (id, run_name, algorithm_name, start_time, status, solver_status, total_examinations,
         scheduled_examinations, hard_constraint_violations, soft_constraint_penalty,
         execution_time, configuration, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        ("run1", "Published run", "CSP", now, "Published", "FEASIBLE_BUT_NOT_PROVEN_OPTIMAL",
         4, 4, 0, 0.0, 0.1, "{}", now))
    conn.execute("INSERT INTO rooms (id, school_id, room_number, capacity, floor, is_active, facilities, created_at)"
                 " VALUES (?,?,?,?,?,?,?,?)", ("r1", "sch1", "L-101", 120, 0, 1, "[]", now))
    for i, eid in enumerate(("e1", "e2", "e3", "e4")):
        conn.execute("""INSERT INTO schedule_assignments
            (id, scheduling_run_id, examination_id, room_id, examination_date, start_time,
             end_time, assignment_status, priority, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            ("a%d" % i, "run1", eid, "r1", "2026-11-1%d" % (i + 6), "09:30", "12:30",
             "Scheduled", "High", now))
    for uid, email, group in (("u3", "three@lpu.co.in", "g3"), ("u5", "five@lpu.co.in", "g5")):
        conn.execute("""INSERT INTO users
            (id, full_name, email, password_hash, role, school_id, department_id, is_active,
             student_group_id, roll_number, created_at) VALUES (?,?,?,?,?,?,NULL,1,?,?,?)""",
            (uid, email.split("@")[0].title(), email, "x", "Student", "sch1", group, "10", now))
    conn.commit()
    db.ensure_group_index()
    db.sync_examination_groups()   # rebuild the paper -> batch index for this fixture
    return conn


@pytest.fixture(scope="module")
def conn():
    return _build(None)


def student(conn, uid):
    return dict(conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone())


def test_links_match_the_examination_field(conn):
    rows = conn.execute("SELECT id, student_group_ids FROM examinations").fetchall()
    for row in rows:
        expected = set(json.loads(row["student_group_ids"]))
        actual = {r["group_id"] for r in conn.execute(
            "SELECT group_id FROM examination_groups WHERE examination_id = ?", (row["id"],))}
        assert actual == expected


def test_each_student_sees_only_their_own_batch(conn):
    three = api.my_exams(conn, student(conn, "u3"))
    five = api.my_exams(conn, student(conn, "u5"))
    assert [e["subject_code"] for e in three["exams"]] == ["CS301", "MA201"]
    assert [e["subject_code"] for e in five["exams"]] == ["CS302", "CS303"]
    assert three["course"]["semester"] == 3 and five["course"]["semester"] == 5
    assert three["student"]["roll_number"] == "10"
    # the room is filled from the live run, so only its presence is asserted here
    assert "room" in three["exams"][0] and "examination_date" in three["exams"][0]


def test_moving_a_student_changes_what_they_see(conn):
    moved = student(conn, "u3")
    moved["student_group_id"] = "g5"
    codes = [e["subject_code"] for e in api.my_exams(conn, moved)["exams"]]
    assert codes == ["CS302", "CS303"]


def test_editing_a_paper_updates_the_index(conn):
    admin = {"id": "adm", "role": "Super Admin", "school_id": None, "email": "a@lpu.co.in"}
    api.update_resource(conn, admin, "examinations", "e1",
                        {"student_group_ids": ["g5"]})
    three = [e["subject_code"] for e in api.my_exams(conn, student(conn, "u3"))["exams"]]
    five = [e["subject_code"] for e in api.my_exams(conn, student(conn, "u5"))["exams"]]
    assert three == ["MA201"]
    assert five == ["CS301", "CS302", "CS303"]


def test_unlinked_student_gets_an_empty_page(conn):
    user = student(conn, "u3")
    user["student_group_id"] = None
    page = api.my_exams(conn, user)
    assert page["enrolled"] is False and page["exams"] == []
