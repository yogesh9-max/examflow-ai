"""ExamFlow AI HTTP server (Python standard library only).

Run:  python3 server/app.py  ->  http://localhost:8000
"""
import json
import os
import re
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, quote

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db
import auth
import api
import persistence
import google_auth
import integrations
import mailer
import otp
import solver_service

AUTH_MODE = (os.environ.get("EXAMFLOW_AUTH_MODE") or "both").strip().lower()

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PORTAL_ROLES = {"admin": ("Super Admin", "School Admin"),
                "user": ("Invigilator",),
                "student": ("Student",)}


PORTAL_GOOGLE_ERRORS = {
    "admin": "This is the administrator portal. Invigilators and students sign in from their own portal.",
    "student": "This is the student portal. Staff sign in from the administrator or invigilator portal.",
    "user": "This is the invigilator portal. Administrators and students sign in from their own portal.",
}


def _password_sign_in_available():
    if AUTH_MODE == "google" and google_auth.ENABLED:
        return False
    if AUTH_MODE == "otp" and mailer.ENABLED:
        return False
    return True
DB_STATUS_HIDDEN = {"login_codes"}


def _table_names(conn):
    """Every user table in whichever engine is behind the connection."""
    try:
        rows = conn.execute(
            "SELECT table_name AS name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_type = 'BASE TABLE' "
            "ORDER BY table_name").fetchall()
    except Exception:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()
    return [row["name"] for row in rows]


def _database_status():
    """Live database health: row counts for every table, plus where it lives."""
    info = {"tables": {}, "records": 0}
    try:
        conn = db.connect()
        try:
            for table in _table_names(conn):
                if table in DB_STATUS_HIDDEN:
                    continue
                try:
                    count = conn.execute('SELECT COUNT(*) AS n FROM "%s"' % table).fetchone()["n"]
                except Exception:
                    continue
                info["tables"][table] = count
                info["records"] += count
        finally:
            conn.close()
        info["reachable"] = True
    except Exception as exc:  # pragma: no cover - diagnostics only
        info["reachable"] = False
        info["error"] = str(exc)[:200]
    if db.using_postgres():
        info["engine"] = "postgres"
        info["durability"] = "managed Postgres (DATABASE_URL); no snapshot commits needed"
        info["snapshot"] = {"enabled": False}
    else:
        info["engine"] = "sqlite"
        info.update(persistence.status())
    return info


STATIC = os.path.join(ROOT, "static")
CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
                 ".js": "application/javascript; charset=utf-8", ".svg": "image/svg+xml",
                 ".png": "image/png", ".jpg": "image/jpeg", ".ico": "image/x-icon",
                 ".json": "application/json"}


class Handler(BaseHTTPRequestHandler):
    server_version = "ExamFlowAI/1.0"
    protocol_version = "HTTP/1.1"

    # ------------------------------------------------------------- plumbing
    def log_message(self, fmt, *args):
        if os.environ.get("EXAMFLOW_QUIET") != "1":
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _redirect(self, location):
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _origin(self):
        proto = self.headers.get("X-Forwarded-Proto") or "http"
        host = self.headers.get("Host") or "localhost:8000"
        return "%s://%s" % (proto, host)

    def _google_account(self, conn, profile, portal):
        """Resolve a verified Google identity to an ExamFlow account for this portal."""
        row = conn.execute("SELECT * FROM users WHERE LOWER(email) = ?",
                           (profile["email"],)).fetchone()
        if row is None:
            if portal != "student":
                return None, ("No ExamFlow account uses that Google address. Ask the examination "
                              "office to create one, or sign in with your email and password.")
            conn.execute(
                """INSERT INTO users (id, full_name, email, password_hash, role,
                                      school_id, department_id, student_group_id,
                                      is_active, created_at)
                   VALUES (?,?,?,?,?,NULL,NULL,NULL,1,?)""",
                (db.new_id(), profile["name"], profile["email"],
                 auth.hash_password(db.new_id() + db.new_id()), "Student", db.now_iso()))
            conn.commit()
            row = conn.execute("SELECT * FROM users WHERE LOWER(email) = ?",
                               (profile["email"],)).fetchone()

        user = db.decode_row("users", row)
        if not user.get("is_active", 1):
            return None, "This account is deactivated. Contact the examination office."
        allowed = {"admin": ("Super Admin", "School Admin"),
                   "user": ("Invigilator",),
                   "student": ("Student",)}.get(portal)
        if allowed and user["role"] not in allowed:
            # Google has already proved who this is, and the shell sends people to the
            # home page for their own role, so a portal mismatch is not a failure: note
            # it and let them in rather than bouncing a valid account with an error.
            db.audit(conn, user, "Signed in via Google from another portal", "user", user["id"],
                     "Opened the %s portal as %s; routed to their own portal" % (portal, user["role"]))
            conn.commit()
        return user, None

    def _redirect(self, location):
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _send(self, status, payload=None, content_type="application/json", raw=None):
        path = self.path or ""
        if (status < 400 and self.command in ("POST", "PUT", "PATCH", "DELETE")
                and path.startswith("/api")):
            # every accepted write has to survive the next cold start
            persistence.save_now()
        body = raw if raw is not None else (
            json.dumps(payload).encode("utf-8") if payload is not None else b"")
        if not isinstance(body, bytes):
            body = str(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except json.JSONDecodeError:
            raise api.auth.ValidationError("Request body must be valid JSON")

    def _user(self, conn):
        header = self.headers.get("Authorization") or ""
        token = header[7:].strip() if header.lower().startswith("bearer ") else None
        if not token:
            raise api.auth.Unauthorized("Sign in to continue")
        user = auth.current_user(token)
        if user is None:
            # An expired, tampered or deleted-session token is a sign-in problem,
            # not a server fault: answer 401 so the client clears it and shows
            # the login page instead of a 500 in the console and the logs.
            raise api.auth.Unauthorized("Your session has expired. Please sign in again.")
        return user

    def _dispatch(self, method):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}

        if not path.startswith("/api"):
            return self._static(path)

        if path == "/api/health":
            return self._send(200, {"status": "ok", "app": "ExamFlow AI"})

        if path == "/api/status":
            return self._send(200, {"status": "ok", "app": "ExamFlow AI",
                                    "database": _database_status()})

        if method == "POST" and path == "/api/admin/migrate":
            expected = (os.environ.get("EXAMFLOW_MIGRATE_TOKEN") or "").strip()
            supplied = (self.headers.get("X-Migrate-Token") or "").strip()
            if not expected or supplied != expected:
                return self._send(401, {"error": "unauthorized"})
            if not db.using_postgres():
                return self._send(400, {"error": "Postgres is not configured (DATABASE_URL missing)"})
            import migrate_pg
            reset = (query.get("reset") or "").strip().lower() in ("1", "true", "yes")
            try:
                summary = migrate_pg.migrate_snapshot(reset=reset)
            except Exception as exc:
                return self._send(500, {"error": str(exc)[:300]})
            return self._send(200, {"status": "ok", "migrated": summary})

        if path == "/api/ping":
            import time as _t
            started = _t.time()
            conn = db.connect()
            connected = _t.time()
            conn.execute("SELECT 1 AS x").fetchone()
            queried = _t.time()
            conn.close()
            return self._send(200, {"ok": True,
                                    "connect_ms": round((connected - started) * 1000, 1),
                                    "first_query_ms": round((queried - connected) * 1000, 1)})

        if path == "/api/integrations":
            return self._send(200, {"status": "ok", "services": integrations.status()})

        conn = db.connect()
        try:
            if method == "GET" and path == "/api/auth/providers":
                return self._send(200, {"password": _password_sign_in_available(),
                                        "google": google_auth.ENABLED or google_auth.ID_MODE,
                                        "google_mode": ("redirect" if google_auth.ENABLED
                                                        else ("idtoken" if google_auth.ID_MODE else None)),
                                        "google_client_id": google_auth.CLIENT_ID or None,
                                        "otp": mailer.ENABLED,
                                        "mode": AUTH_MODE})

            if method == "POST" and path == "/api/auth/google/idtoken":
                body = self._json_body()
                portal = (body.get("portal") or "").strip().lower()
                try:
                    profile = google_auth.verify_id_token(body.get("credential"))
                except google_auth.GoogleAuthError as exc:
                    raise auth.Unauthorized(str(exc))
                user, error = self._google_account(conn, profile, portal)
                if error:
                    raise auth.Unauthorized(error)
                db.audit(conn, user, "User login (Google)", "user", user["id"], "Portal: %s" % (portal or "default"))
                conn.commit()
                return self._send(200, {"token": auth.make_token(user), "user": user})

            if method == "GET" and path == "/api/auth/google/start":
                if not google_auth.ENABLED:
                    return self._redirect("/?google_error=not_configured#/login")
                portal = (query.get("portal") or "user").strip().lower()
                redirect_uri = self._origin() + "/api/auth/google/callback"
                state = google_auth.sign_state(redirect_uri + "|" + portal)
                return self._redirect(google_auth.authorize_url(redirect_uri, state))

            if method == "GET" and path == "/api/auth/google/callback":
                if query.get("error"):
                    return self._redirect("/?google_error=cancelled#/login")
                try:
                    redirect_uri, portal = (google_auth.read_state(query.get("state") or "")
                                            or ("", "")).split("|")
                except ValueError:
                    redirect_uri, portal = "", ""
                if not redirect_uri:
                    return self._redirect("/?google_error=expired#/login")
                try:
                    profile = google_auth.exchange_code(query.get("code") or "", redirect_uri)
                except google_auth.GoogleAuthError:
                    return self._redirect("/?google_error=google#/login")

                row = conn.execute("SELECT * FROM users WHERE LOWER(email) = ?",
                                   (profile["email"],)).fetchone()
                if row is None:
                    if portal != "student":
                        return self._redirect("/?google_error=no_account#/login/student")
                    conn.execute(
                        """INSERT INTO users (id, full_name, email, password_hash, role,
                                              school_id, department_id, student_group_id,
                                              is_active, created_at)
                           VALUES (?,?,?,?,?,NULL,NULL,NULL,1,?)""",
                        (db.new_id(), profile["name"], profile["email"],
                         auth.hash_password(db.new_id() + db.new_id()), "Student", db.now_iso()))
                    conn.commit()
                    row = conn.execute("SELECT * FROM users WHERE LOWER(email) = ?",
                                       (profile["email"],)).fetchone()

                user = db.decode_row("users", row)
                if not user.get("is_active", 1):
                    return self._redirect("/?google_error=inactive#/login")
                allowed = {"admin": ("Super Admin", "School Admin"),
                           "user": ("Invigilator",),
                           "student": ("Student",)}.get(portal, ())
                if allowed and user["role"] not in allowed:
                    return self._redirect("/?google_error=wrong_portal#/login/%s" % portal)

                db.audit(conn, user, "User login (Google)", "user", user["id"])
                conn.commit()
                return self._redirect("/#google_token=" + quote(auth.make_token(user)))

            if method == "GET" and path == "/api/public/enrolment-options":
                return self._send(200, api.public_enrolment_options(conn))

            if method == "POST" and path == "/api/auth/request-code":
                body = self._json_body()
                portal = (body.get("portal") or "").strip().lower()
                email = (body.get("email") or "").strip().lower()
                if "@" not in email or len(email) < 5:
                    raise auth.ValidationError("Enter the email address on your account.")
                user = auth.find_user(email)
                allowed_roles = PORTAL_ROLES.get(portal)
                known = bool(user) and (not allowed_roles or user["role"] in allowed_roles)
                if known and otp.recent_request_blocked(conn, email):
                    raise auth.ValidationError(
                        "A code was sent a moment ago. Wait a minute before asking for another.")
                if known:
                    code = otp.issue(conn, email, portal)
                    conn.commit()
                    sent = False
                    if mailer.ENABLED:
                        try:
                            mailer.send(
                                email,
                                "Your ExamFlow AI sign-in code",
                                "Your ExamFlow AI sign-in code is %s. It expires in %d minutes."
                                % (code, otp.CODE_TTL_MINUTES),
                                "<p>Your ExamFlow AI sign-in code is <strong>%s</strong>.</p>"
                                "<p>It expires in %d minutes. If you did not try to sign in, ignore this "
                                "email.</p>" % (code, otp.CODE_TTL_MINUTES),
                            )
                            sent = True
                            db.audit(conn, None, "Sign-in code emailed", "user", user["id"], email)
                            conn.commit()
                        except Exception:  # noqa: BLE001
                            traceback.print_exc()
                # identical response either way: never reveal whether an address exists
                return self._send(200, {"sent": True, "delivered": bool(mailer.ENABLED), "email": email,
                                        "expires_in": otp.CODE_TTL_MINUTES * 60})

            if method == "POST" and path == "/api/auth/verify-code":
                body = self._json_body()
                portal = (body.get("portal") or "").strip().lower()
                email = (body.get("email") or "").strip().lower()
                ok, reason = otp.confirm(conn, email, portal, body.get("code"))
                conn.commit()
                if not ok:
                    raise auth.Unauthorized(reason)
                user = auth.find_user(email)
                if not user:
                    raise auth.Unauthorized("No active account is linked to that address.")
                allowed_roles = PORTAL_ROLES.get(portal)
                if allowed_roles and user["role"] not in allowed_roles:
                    db.audit(conn, user, "Blocked sign-in (wrong portal)", "user", user["id"],
                             "Tried the %s portal as %s" % (portal, user["role"]))
                    conn.commit()
                    if portal == "admin":
                        raise auth.Unauthorized(
                            "This is the administrator portal. Invigilators and students sign in from "
                            "their own portal.")
                    if portal == "student":
                        raise auth.Unauthorized(
                            "This is the student portal. Staff sign in from the administrator or "
                            "invigilator portal.")
                    raise auth.Unauthorized(
                        "This is the invigilator portal. Administrators and students sign in from "
                        "their own portal.")
                db.audit(conn, user, "User login (email code)", "user", user["id"],
                         "Portal: %s" % (portal or "default"))
                conn.commit()
                return self._send(200, {"token": auth.make_token(user), "user": user})

            if method == "POST" and path == "/api/auth/gmail":
                body = self._json_body()
                portal = (body.get("portal") or "student").strip().lower()
                email = (body.get("email") or "").strip().lower()
                password = body.get("password") or ""
                if "@" not in email or "." not in email.split("@")[-1]:
                    raise auth.ValidationError("Enter your email address.")
                if len(password) < 8:
                    raise auth.ValidationError("Choose a password of at least 8 characters.")
                row = conn.execute("SELECT * FROM users WHERE lower(email) = ?", (email,)).fetchone()
                if row and not row["is_active"]:
                    raise auth.Unauthorized("This account has been deactivated. Contact your administrator.")
                if row:
                    user = auth.authenticate(email, password)
                    if not user:
                        raise auth.Unauthorized(
                            "This email already has an account. Enter its password, or sign up with a "
                            "different address.")
                    allowed_roles = PORTAL_ROLES.get(portal)
                    if allowed_roles and user["role"] not in allowed_roles:
                        raise auth.Unauthorized(
                            "That account belongs to a different portal. Use the right sign-in page "
                            "for its role.")
                    db.audit(conn, user, "User login (email account)", "user", user["id"],
                             "Portal: %s" % (portal or "default"))
                    conn.commit()
                    return self._send(200, {"token": auth.make_token(user), "user": user,
                                            "created": False})
                phone = (body.get("phone") or "").strip()
                if phone:
                    mobile = auth.normalise_mobile(phone)
                    if not mobile:
                        raise auth.ValidationError(
                            "Enter a 10-digit Indian mobile number, for example 98765 43210.")
                    taken = conn.execute("SELECT id FROM users WHERE phone = ?", (mobile,)).fetchone()
                    if taken:
                        raise auth.ValidationError(
                            "That mobile number is already linked to another account.")
                group_id = body.get("student_group_id") or None
                if not group_id and portal == "student" and body.get("semester"):
                    # The student picked school + programme + semester themselves:
                    # match that batch, creating it when the office has not yet.
                    school_id = (body.get("school_id") or "").strip()
                    if school_id and conn.execute("SELECT id FROM schools WHERE id=?",
                                                  (school_id,)).fetchone():
                        dept_id = (body.get("department_id") or "").strip() or None
                        if dept_id:
                            dept = conn.execute("SELECT school_id FROM departments WHERE id=?",
                                                (dept_id,)).fetchone()
                            if not dept or dept["school_id"] != school_id:
                                dept_id = None
                        try:
                            semester = int(body.get("semester"))
                        except (TypeError, ValueError):
                            semester = 0
                        if 1 <= semester <= 12:
                            group_id, _created = api.ensure_student_batch(
                                conn, school_id, dept_id, semester, body.get("section"),
                                body.get("academic_year"))
                user = auth.provision_gmail_account(
                    conn, email, password, portal, body.get("full_name"),
                    phone=phone or None,
                    student_group_id=group_id,
                    roll_number=body.get("roll_number") or None)
                if not user:
                    raise auth.ValidationError("Could not create that account. Try another address.")
                db.audit(conn, user, "Self-service account created", "user", user["id"],
                         "Portal: %s" % (portal or "default"))
                db.notify(conn, user["id"], "Welcome to ExamFlow AI",
                          "Your %s access is ready. Explore your dashboard to get started."
                          % user["role"])
                conn.commit()
                # a new account must survive the next cold start, so snapshot it now
                persistence.save_now()
                return self._send(201, {"token": auth.make_token(user), "user": user, "created": True})

            if method == "POST" and path == "/api/auth/login":
                body = self._json_body()
                portal = (body.get("portal") or "").strip().lower()
                if not _password_sign_in_available():
                    raise auth.Unauthorized(
                        "Password sign-in is switched off on this deployment.")
                identifier = (body.get("identifier") or body.get("email")
                              or body.get("phone") or "").strip()
                if not identifier:
                    raise auth.ValidationError("Enter your email address or mobile number.")
                allowed, _remaining = integrations.rate_limit(
                    "login:%s:%s" % (self.client_address[0], identifier.lower()), 10, 600)
                if not allowed:
                    raise auth.Unauthorized("Too many sign-in attempts. Try again in a few minutes.")
                user = auth.authenticate(identifier, body.get("password") or "")
                if not user:
                    raise auth.Unauthorized("That email/mobile and password do not match an account.")
                allowed_roles = {"admin": ("Super Admin", "School Admin"),
                                 "user": ("Invigilator",),
                                 "student": ("Student",)}.get(portal)
                if allowed_roles and user["role"] not in allowed_roles:
                    db.audit(conn, user, "Blocked sign-in (wrong portal)", "user", user["id"],
                             "Tried the %s portal as %s" % (portal, user["role"]))
                    conn.commit()
                    if portal == "admin":
                        raise auth.Unauthorized(
                            "This is the administrator portal. Invigilators and students sign in from their own portal.")
                    if portal == "student":
                        raise auth.Unauthorized(
                            "This is the student portal. Staff sign in from the administrator or invigilator portal.")
                    raise auth.Unauthorized(
                        "This is the invigilator portal. Administrators and students sign in from their own portal.")
                db.audit(conn, user, "User login", "user", user["id"],
                         "Portal: %s" % (portal or "default"))
                conn.commit()
                return self._send(200, {"token": auth.make_token(user), "user": user})

            user = self._user(conn)
            if user is None:
                raise api.auth.Unauthorized("Your session has expired. Please sign in again.")

            if path == "/api/auth/me":
                return self._send(200, {"user": user})

            if method in ("POST", "PUT") and path == "/api/auth/mobile":
                body = self._json_body()
                mobile = auth.normalise_mobile(body.get("phone") or "")
                if not mobile:
                    raise auth.ValidationError(
                        "Enter a 10-digit Indian mobile number, for example 98765 43210.")
                taken = conn.execute(
                    "SELECT id FROM users WHERE phone = ? AND id <> ?", (mobile, user["id"])).fetchone()
                if taken:
                    raise auth.ValidationError("That mobile number is already linked to another account.")
                conn.execute("UPDATE users SET phone = ? WHERE id = ?", (mobile, user["id"]))
                db.audit(conn, user, "Mobile number linked for sign-in", "user", user["id"], mobile)
                conn.commit()
                user = dict(user, phone=mobile)
                return self._send(200, {"user": user})

            if method == "POST" and path == "/api/auth/logout":
                db.audit(conn, user, "User logout", "user", user["id"])
                conn.commit()
                return self._send(200, {"ok": True})
            if path == "/api/meta":
                return self._send(200, api.meta(conn, user))
            if path == "/api/dashboard":
                return self._send(200, api.dashboard_cached(conn, user))
            if path == "/api/my-duties":
                return self._send(200, api.my_duties(conn, user))
            if path == "/api/my-exams":
                return self._send(200, api.my_exams(conn, user))

            if path == "/api/student/enrolment-options":
                return self._send(200, api.enrolment_options(conn, user))

            if method == "POST" and path == "/api/student/profile":
                return self._send(200, api.save_student_profile(conn, user, self._json_body()))

            if method == "POST" and path == "/api/users/bulk-students":
                return self._send(201, api.bulk_students(conn, user, self._json_body()))

            if method == "POST" and path == "/api/student/enrol":
                return self._send(200, api.enrol_student(conn, user, self._json_body()))
            if method == "POST" and path == "/api/my-duties/report-issue":
                return self._send(200, api.report_availability_issue(conn, user, self._json_body()))

            if method == "POST" and path == "/api/scheduling/run":
                body = self._json_body()
                if user["role"] == "Invigilator":
                    raise auth.Forbidden("Invigilators cannot generate timetables")
                if user["role"] == "School Admin" and not body.get("school_ids"):
                    body["school_ids"] = [user["school_id"]]
                return self._send(201, solver_service.run_schedule(conn, user, body))

            m = re.match(r"^/api/runs/([0-9a-zA-Z]+)/(validate|fairness|assignments|approve|publish|repair)$",
                         path)
            if m:
                run_id, action = m.groups()
                if method == "GET" and action == "validate":
                    return self._send(200, solver_service.validate_run(conn, run_id))
                if method == "GET" and action == "fairness":
                    return self._send(200, solver_service.fairness_for_run(conn, run_id))
                if method == "GET" and action == "assignments":
                    return self._send(200, {"assignments": solver_service.assignments_for_run(conn, run_id)})
                if method == "POST" and action == "approve":
                    return self._send(200, solver_service.approve_run(conn, user, run_id))
                if method == "POST" and action == "publish":
                    return self._send(200, solver_service.publish_run(conn, user, run_id))
                if method == "POST" and action == "repair":
                    return self._send(200, solver_service.repair_run(conn, user, run_id))

            m = re.match(r"^/api/runs/([0-9a-zA-Z]+)$", path)
            if m and method == "GET":
                return self._send(200, solver_service.get_run(conn, m.group(1)))

            if path == "/api/comparison":
                a, b = query.get("a"), query.get("b")
                if not a or not b:
                    csp = conn.execute("SELECT id FROM scheduling_runs WHERE algorithm_name LIKE 'CSP%' "
                                       "ORDER BY created_at DESC LIMIT 1").fetchone()
                    ff = conn.execute("SELECT id FROM scheduling_runs WHERE algorithm_name LIKE 'First-Fit%' "
                                      "ORDER BY created_at DESC LIMIT 1").fetchone()
                    if not (csp and ff):
                        raise auth.ValidationError(
                            "Generate one AI run and one First-Fit run to compare them.")
                    a, b = ff["id"], csp["id"]
                return self._send(200, solver_service.compare_runs(conn, a, b))

            if method == "PUT" and re.match(r"^/api/assignments/[0-9a-zA-Z]+/reschedule$", path):
                assignment_id = path.split("/")[3]
                body = self._json_body()
                if user["role"] == "Invigilator":
                    raise auth.Forbidden("Invigilators cannot reschedule examinations")
                return self._send(200, solver_service.reschedule_assignment(
                    conn, user, assignment_id, body.get("time_slot_id"), body.get("room_id")))

            m = re.match(r"^/api/notifications/([0-9a-zA-Z]+)/read$", path)
            if m and method == "POST":
                conn.execute("UPDATE notifications SET is_read=1 WHERE id=? AND user_id=?",
                             (m.group(1), user["id"]))
                conn.commit()
                return self._send(200, {"ok": True})
            if path == "/api/notifications/read-all" and method == "POST":
                conn.execute("UPDATE notifications SET is_read=1 WHERE user_id=?", (user["id"],))
                conn.commit()
                return self._send(200, {"ok": True})

            m = re.match(r"^/api/import/([a-z-]+)$", path)
            if m and method == "POST":
                body = self._json_body()
                return self._send(200, api.import_csv(conn, user, m.group(1), body.get("csv") or ""))

            m = re.match(r"^/api/reports/([a-z-]+)(\.csv)?$", path)
            if m and method == "GET":
                kind, csv_flag = m.group(1), m.group(2)
                if csv_flag:
                    text, filename = api.report_csv(conn, user, kind, query)
                    self.send_response(200)
                    self.send_header("Content-Type", "text/csv; charset=utf-8")
                    self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
                    data = text.encode("utf-8")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                return self._send(200, api.report(conn, user, kind, query))

            m = re.match(r"^/api/([a-z-]+)/([0-9a-zA-Z_]+)$", path)
            if m:
                resource, record_id = m.groups()
                if method == "GET":
                    return self._send(200, api.get_resource(conn, user, resource, record_id))
                if method in ("PUT", "PATCH"):
                    return self._send(200, api.update_resource(
                        conn, user, resource, record_id, self._json_body()))
                if method == "DELETE":
                    return self._send(200, api.delete_resource(conn, user, resource, record_id))

            m = re.match(r"^/api/([a-z-]+)$", path)
            if m:
                resource = m.group(1)
                if method == "GET":
                    return self._send(200, api.list_resource(conn, user, resource, query))
                if method == "POST":
                    return self._send(201, api.create_resource(
                        conn, user, resource, self._json_body()))

            raise auth.NotFound("Endpoint not found")
        except auth.Unauthorized as exc:
            self._send(401, {"error": str(exc)})
        except auth.Forbidden as exc:
            self._send(403, {"error": str(exc)})
        except auth.NotFound as exc:
            self._send(404, {"error": str(exc)})
        except auth.ValidationError as exc:
            self._send(422, {"error": str(exc)})
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            self._send(500, {"error": "Unexpected server error. Check the server console for details."})
        finally:
            conn.close()

    def _static(self, path):
        if path in ("/", "/index.html"):
            target = os.path.join(STATIC, "index.html")
        else:
            target = os.path.join(ROOT, path.lstrip("/"))
            if not os.path.abspath(target).startswith(ROOT):
                return self._send(403, {"error": "Forbidden"})
        if not os.path.isfile(target):
            return self._send(404, {"error": "Not found"})
        ext = os.path.splitext(target)[1].lower()
        with open(target, "rb") as handle:
            data = handle.read()
        self._send(200, None, CONTENT_TYPES.get(ext, "application/octet-stream"), raw=data)

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def do_HEAD(self):
        self._dispatch("GET")


def main():
    port = int(os.environ.get("PORT") or 8000)
    db.init_db(seed=os.environ.get("EXAMFLOW_SEED", "1") != "0")
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"ExamFlow AI running on http://localhost:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping ExamFlow AI.")


if __name__ == "__main__":
    main()
