"""
SDMS - Student Lifecycle Management System (web version)
Flask + SQLite.  Modules: Auth/RBAC, Students, Courses, Fees,
Examination (marks), Attendance, Analytics, JSON APIs.
Run:  python app.py        (then open http://127.0.0.1:5000)
"""
import os
import re
import secrets
import sqlite3
import time
from datetime import date, timedelta
from functools import wraps

from flask import (Flask, abort, flash, g, jsonify, redirect, render_template,
                   request, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

BASE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_STUDENT_PASS = "student123"
MAX_COURSES_PER_STUDENT = 4
PASS_PERCENT = 40
EXAMS = ["Mid-1", "Mid-2", "Assignment", "End-Sem"]
MAX_LOGIN_FAILS = 3          # wrong passwords allowed per username
LOCK_SECONDS = 60            # lock time after MAX_LOGIN_FAILS
SESSION_MINUTES = 30         # idle session lifetime
DEFAULT_PER_PAGE, MAX_PER_PAGE = 25, 200
DEFAULT_SECRET = "dev-secret-change-me"
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PHONE_RE = re.compile(r"^\+?\d{7,19}$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS students(
  id TEXT PRIMARY KEY, name TEXT NOT NULL,
  email TEXT NOT NULL UNIQUE, phone TEXT NOT NULL UNIQUE,
  department TEXT NOT NULL DEFAULT 'CSE', semester INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL UNIQUE,
  pass_hash TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('admin','student')),
  student_id TEXT REFERENCES students(id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS courses(
  code TEXT PRIMARY KEY, name TEXT NOT NULL, credits INTEGER NOT NULL DEFAULT 3);
CREATE TABLE IF NOT EXISTS enrollments(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  student_id TEXT NOT NULL REFERENCES students(id) ON DELETE CASCADE,
  course_code TEXT NOT NULL REFERENCES courses(code) ON DELETE CASCADE,
  UNIQUE(student_id, course_code));
CREATE TABLE IF NOT EXISTS fees(
  student_id TEXT PRIMARY KEY REFERENCES students(id) ON DELETE CASCADE,
  total REAL NOT NULL DEFAULT 0, paid REAL NOT NULL DEFAULT 0,
  CHECK(paid >= 0 AND paid <= total));
CREATE TABLE IF NOT EXISTS marks(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  student_id TEXT NOT NULL REFERENCES students(id) ON DELETE CASCADE,
  course_code TEXT NOT NULL REFERENCES courses(code) ON DELETE CASCADE,
  exam TEXT NOT NULL, score REAL NOT NULL, max_score REAL NOT NULL DEFAULT 100,
  UNIQUE(student_id, course_code, exam),
  CHECK(max_score > 0 AND score >= 0 AND score <= max_score));
CREATE TABLE IF NOT EXISTS attendance(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  student_id TEXT NOT NULL REFERENCES students(id) ON DELETE CASCADE,
  course_code TEXT NOT NULL REFERENCES courses(code) ON DELETE CASCADE,
  date TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('P','A')),
  UNIQUE(student_id, course_code, date));
CREATE TABLE IF NOT EXISTS login_attempts(
  username TEXT PRIMARY KEY, fails INTEGER NOT NULL DEFAULT 0,
  lock_until REAL NOT NULL DEFAULT 0, last_try REAL NOT NULL DEFAULT 0);

-- Milestone 4: indexes for the course-wise analytics and per-course screens.
-- (the UNIQUE constraints already index student_id lookups)
CREATE INDEX IF NOT EXISTS idx_enr_course   ON enrollments(course_code);
CREATE INDEX IF NOT EXISTS idx_marks_course ON marks(course_code, exam, score, max_score);
CREATE INDEX IF NOT EXISTS idx_att_course   ON attendance(course_code, date, status);
CREATE INDEX IF NOT EXISTS idx_users_student ON users(student_id);
"""

PCT = "ROUND(AVG(score*100.0/max_score),1)"
PASS = f"ROUND(100.0*SUM(CASE WHEN score*100.0/max_score>={PASS_PERCENT} THEN 1 ELSE 0 END)/COUNT(*),1)"
ATT = "ROUND(100.0*SUM(CASE WHEN status='P' THEN 1 ELSE 0 END)/COUNT(*),1)"


# ----------------------------------------------------------------- database
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(g.app_db)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
        g.db.execute("PRAGMA busy_timeout = 5000")
    return g.db


def init_db(path, seed=True):
    db = sqlite3.connect(path)
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("PRAGMA journal_mode = WAL")      # readers do not block the writer
    db.execute("PRAGMA synchronous = NORMAL")
    db.executescript(SCHEMA)
    if seed and db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
        seed_data(db)
    db.commit()
    db.close()


def seed_data(db):
    db.execute("INSERT INTO users(username,pass_hash,role) VALUES('admin',?, 'admin')",
               (generate_password_hash(os.environ.get("SDMS_ADMIN_PASSWORD", "admin123")),))
    db.executemany("INSERT INTO courses VALUES(?,?,?)", [
        ("CS101", "Database Management Systems", 4), ("CS102", "Operating Systems", 4),
        ("CS103", "Data Structures", 4), ("MA101", "Engineering Mathematics", 3)])
    people = [("S-1", "Student 1", "student-1@gmail.com", "016111111111", ["CS101"], 50000, 50000),
              ("S-2", "Student 2", "student-2@gmail.com", "016111111112", ["CS101", "CS102"], 50000, 30000),
              ("S-3", "Student 3", "student-3@gmail.com", "016111111113", ["CS101", "CS102", "CS103"], 50000, 0)]
    for sid, name, email, phone, courses, total, paid in people:
        db.execute("INSERT INTO students VALUES(?,?,?,?,?,?)", (sid, name, email, phone, "CSE", 1))
        db.execute("INSERT INTO users(username,pass_hash,role,student_id) VALUES(?,?, 'student',?)",
                   (sid, generate_password_hash(DEFAULT_STUDENT_PASS), sid))
        db.execute("INSERT INTO fees VALUES(?,?,?)", (sid, total, paid))
        for c in courses:
            db.execute("INSERT INTO enrollments(student_id,course_code) VALUES(?,?)", (sid, c))
    for sid, c, score in [("S-1", "CS101", 78), ("S-2", "CS101", 62), ("S-2", "CS102", 35),
                          ("S-3", "CS101", 45), ("S-3", "CS102", 55), ("S-3", "CS103", 28)]:
        db.execute("INSERT INTO marks(student_id,course_code,exam,score,max_score) VALUES(?,?,?,?,100)",
                   (sid, c, "Mid-1", score))
    days = ["2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08"]
    pattern = {"S-1": "PPPP", "S-2": "PPPA", "S-3": "PPAA"}
    for sid, p in pattern.items():
        for d, s in zip(days, p):
            db.execute("INSERT INTO attendance(student_id,course_code,date,status) VALUES(?,?,?,?)",
                       (sid, "CS101", d, s))


# --------------------------------------------------------------- validation
def validate_student(db, f, editing=None):
    """Same rules as the C prototype. Returns (errors, cleaned)."""
    e = []
    sid = editing if editing else f.get("id", "").strip()
    name, email = f.get("name", "").strip(), f.get("email", "").strip()
    phone, dept = f.get("phone", "").strip(), f.get("department", "").strip() or "CSE"
    sem, courses = f.get("semester", "1").strip(), f.getlist("courses")
    if not editing:
        if not sid or len(sid) > 10 or " " in sid:
            e.append("Student ID is required, max 10 characters, no spaces.")
        elif (db.execute("SELECT 1 FROM students WHERE id=?", (sid,)).fetchone()
              or db.execute("SELECT 1 FROM users WHERE username=?", (sid,)).fetchone()):
            e.append("This ID already exists.")
    if not name or len(name) > 20:
        e.append("Name is required (max 20 characters).")
    if not email or len(email) > 30 or not EMAIL_RE.match(email):
        e.append("Enter a valid email (max 30 characters).")
    elif db.execute("SELECT 1 FROM students WHERE email=? AND id<>?", (email, sid)).fetchone():
        e.append("This email already exists.")
    if not PHONE_RE.match(phone):
        e.append("Phone must be digits only (7 to 19 digits, optional leading +).")
    elif db.execute("SELECT 1 FROM students WHERE phone=? AND id<>?", (phone, sid)).fetchone():
        e.append("This phone number already exists.")
    if not sem.isdigit() or not 1 <= int(sem) <= 8:
        e.append("Semester must be between 1 and 8.")
    known = {r["code"] for r in db.execute("SELECT code FROM courses")}
    courses = list(dict.fromkeys(courses))
    if not 1 <= len(courses) <= MAX_COURSES_PER_STUDENT or not set(courses) <= known:
        e.append(f"Select 1 to {MAX_COURSES_PER_STUDENT} valid courses.")
    clean = dict(id=sid, name=name, email=email, phone=phone, department=dept[:20],
                 semester=int(sem) if sem.isdigit() else 1, courses=courses)
    return e, clean


def student_detail(db, sid):
    s = db.execute("SELECT * FROM students WHERE id=?", (sid,)).fetchone()
    if not s:
        return None
    courses = [dict(r) for r in db.execute(f"""
        SELECT c.code,c.name,c.credits,
          (SELECT {PCT} FROM marks m WHERE m.student_id=e.student_id AND m.course_code=c.code) AS avg_marks,
          (SELECT {ATT} FROM attendance a WHERE a.student_id=e.student_id AND a.course_code=c.code) AS attendance_pct
        FROM enrollments e JOIN courses c ON c.code=e.course_code
        WHERE e.student_id=? ORDER BY c.code""", (sid,))]
    marks = [dict(r) for r in db.execute(
        "SELECT course_code,exam,score,max_score FROM marks WHERE student_id=? ORDER BY course_code,exam", (sid,))]
    f = db.execute("SELECT total,paid FROM fees WHERE student_id=?", (sid,)).fetchone()
    fee = {"total": f["total"], "paid": f["paid"], "due": f["total"] - f["paid"]} if f else {"total": 0, "paid": 0, "due": 0}
    overall = {
        "avg_marks": db.execute(f"SELECT {PCT} FROM marks WHERE student_id=?", (sid,)).fetchone()[0],
        "attendance_pct": db.execute(f"SELECT {ATT} FROM attendance WHERE student_id=?", (sid,)).fetchone()[0]}
    return {"student": dict(s), "courses": courses, "marks": marks, "fee": fee, "overall": overall}


def course_stats(db):
    return [dict(r) for r in db.execute(f"""
        SELECT c.code, c.name,
          (SELECT COUNT(*) FROM enrollments e WHERE e.course_code=c.code) AS enrolled,
          (SELECT {PCT} FROM marks m WHERE m.course_code=c.code) AS avg_marks,
          (SELECT {PASS} FROM marks m WHERE m.course_code=c.code) AS pass_pct,
          (SELECT {ATT} FROM attendance a WHERE a.course_code=c.code) AS attendance_pct
        FROM courses c ORDER BY c.code""")]


def summary(db):
    one = lambda q: db.execute(q).fetchone()[0]
    total, paid = db.execute("SELECT COALESCE(SUM(total),0), COALESCE(SUM(paid),0) FROM fees").fetchone()
    return {"students": one("SELECT COUNT(*) FROM students"), "courses": one("SELECT COUNT(*) FROM courses"),
            "fee_total": total, "fee_paid": paid, "fee_due": total - paid,
            "avg_marks": one(f"SELECT {PCT} FROM marks"), "pass_pct": one(f"SELECT {PASS} FROM marks"),
            "attendance_pct": one(f"SELECT {ATT} FROM attendance")}


# ------------------------------------------------------------------- paging
def page_args(args):
    """Read ?page= and ?per_page= safely. Returns (page, per_page)."""
    def num(key, default):
        v = args.get(key, "")
        return int(v) if v.isdigit() and int(v) > 0 else default
    return num("page", 1), min(num("per_page", DEFAULT_PER_PAGE), MAX_PER_PAGE)


def escape_like(q):
    return q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# ---------------------------------------------------------------- app factory
def create_app(db_path=None, seed=True):
    app = Flask(__name__)
    production = os.environ.get("SDMS_ENV") == "production"
    secret = os.environ.get("SECRET_KEY", DEFAULT_SECRET)
    if production and secret == DEFAULT_SECRET:
        raise RuntimeError("Set a long random SECRET_KEY before running in production.")
    app.config.update(SECRET_KEY=secret,
                      SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                      SESSION_COOKIE_SECURE=production,      # cookie only over HTTPS in production
                      PERMANENT_SESSION_LIFETIME=timedelta(minutes=SESSION_MINUTES),
                      MAX_CONTENT_LENGTH=1024 * 1024)        # reject request bodies over 1 MB
    path = db_path or os.environ.get("SDMS_DB") or os.path.join(BASE, "sdms.db")   # SDMS_DB = path on a persistent disk
    init_db(path, seed)

    @app.before_request
    def before():
        g.app_db = path
        g.user = None
        if "uid" in session:
            g.user = get_db().execute("SELECT * FROM users WHERE id=?", (session["uid"],)).fetchone()
            if g.user is None:
                session.clear()
        if request.method == "POST":
            token = request.form.get("csrf_token", "")
            if not token or not secrets.compare_digest(token, session.get("csrf", "")):
                abort(400, "Invalid or missing CSRF token.")

    @app.teardown_appcontext
    def close(_):
        db = g.pop("db", None)
        if db:
            db.close()

    @app.context_processor
    def inject():
        if "csrf" not in session:
            session["csrf"] = secrets.token_hex(16)
        return {"user": g.get("user"), "csrf_token": session["csrf"], "EXAMS": EXAMS,
                "today": date.today().isoformat()}

    @app.after_request
    def headers(r):
        r.headers["X-Content-Type-Options"] = "nosniff"
        r.headers["X-Frame-Options"] = "DENY"
        r.headers["Cache-Control"] = "no-store"
        r.headers["Referrer-Policy"] = "same-origin"
        r.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        if production:
            r.headers["Strict-Transport-Security"] = "max-age=31536000"
        return r

    # ------------------------------------------------------------ decorators
    def guard(role=None, api=False):
        def deco(fn):
            @wraps(fn)
            def inner(*a, **k):
                if g.user is None:
                    if api:
                        return jsonify(error="Login required"), 401
                    return redirect(url_for("login"))
                if role and g.user["role"] != role:
                    if api:
                        return jsonify(error="Forbidden"), 403
                    abort(403)
                return fn(*a, **k)
            return inner
        return deco

    def can_see(sid):
        return g.user["role"] == "admin" or g.user["student_id"] == sid

    @app.errorhandler(403)
    def e403(e):
        return render_template("error.html", code=403, msg="You do not have permission to open this page."), 403

    @app.errorhandler(404)
    def e404(e):
        return render_template("error.html", code=404, msg="Page not found."), 404

    @app.errorhandler(400)
    def e400(e):
        return render_template("error.html", code=400, msg=getattr(e, "description", "Bad request")), 400

    # ------------------------------------------------------------------ auth
    @app.route("/")
    def home():
        return redirect(url_for("dashboard" if g.user else "login"))

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            db = get_db()
            name = request.form.get("username", "").strip()[:50]
            now = time.time()
            # the lock is stored in the database per username, so opening a new
            # browser or clearing cookies does not reset the attempt counter
            row = db.execute("SELECT fails, lock_until FROM login_attempts WHERE username=?",
                             (name,)).fetchone()
            if row and row["lock_until"] > now:
                flash("Too many failed attempts. Try again in a minute.", "err")
                return render_template("login.html"), 429
            u = db.execute("SELECT * FROM users WHERE username=?", (name,)).fetchone()
            if u and check_password_hash(u["pass_hash"], request.form.get("password", "")):
                db.execute("DELETE FROM login_attempts WHERE username=?", (name,))
                db.commit()
                keep = session.get("csrf")
                session.clear()
                session.permanent = True
                session["uid"], session["csrf"] = u["id"], keep or secrets.token_hex(16)
                return redirect(url_for("dashboard"))
            fails = (row["fails"] if row else 0) + 1
            if fails >= MAX_LOGIN_FAILS:
                db.execute("""INSERT INTO login_attempts(username,fails,lock_until,last_try) VALUES(?,0,?,?)
                              ON CONFLICT(username) DO UPDATE SET fails=0, lock_until=excluded.lock_until,
                              last_try=excluded.last_try""", (name, now + LOCK_SECONDS, now))
                flash("Too many failed attempts. Login locked for 1 minute.", "err")
            else:
                db.execute("""INSERT INTO login_attempts(username,fails,last_try) VALUES(?,?,?)
                              ON CONFLICT(username) DO UPDATE SET fails=excluded.fails,
                              last_try=excluded.last_try""", (name, fails, now))
                flash(f"Invalid username or password. ({fails}/{MAX_LOGIN_FAILS})", "err")
            db.execute("DELETE FROM login_attempts WHERE last_try < ? AND lock_until < ?",
                       (now - 3600, now))                 # forget old attempts
            db.commit()
            return render_template("login.html"), 401
        return render_template("login.html")

    @app.route("/logout", methods=["POST"])
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.route("/password", methods=["GET", "POST"])
    @guard()
    def password():
        if request.method == "POST":
            old, new, again = (request.form.get(k, "") for k in ("old", "new", "again"))
            if not check_password_hash(g.user["pass_hash"], old):
                flash("Current password is wrong.", "err")
            elif len(new) < 6 or len(new) > 20:
                flash("New password must be 6 to 20 characters.", "err")
            elif new != again:
                flash("Passwords do not match.", "err")
            else:
                db = get_db()
                db.execute("UPDATE users SET pass_hash=? WHERE id=?", (generate_password_hash(new), g.user["id"]))
                db.commit()
                flash("Password changed successfully.", "ok")
                return redirect(url_for("dashboard"))
        return render_template("password.html")

    @app.route("/dashboard")
    @guard()
    def dashboard():
        return render_template("dashboard.html")

    # -------------------------------------------------------------- students
    @app.route("/students")
    @guard("admin")
    def students():
        db = get_db()
        q = request.args.get("q", "").strip()
        like = f"%{escape_like(q)}%"
        where = "WHERE s.id LIKE ? ESCAPE '\\' OR s.name LIKE ? ESCAPE '\\' OR s.email LIKE ? ESCAPE '\\'"
        total = db.execute(f"SELECT COUNT(*) FROM students s {where}", (like, like, like)).fetchone()[0]
        page, per_page = page_args(request.args)
        pages = max(1, -(-total // per_page))
        page = min(page, pages)
        rows = db.execute(f"""
            SELECT s.*, (SELECT COUNT(*) FROM enrollments e WHERE e.student_id=s.id) AS ncourses,
                   f.total, f.paid FROM students s LEFT JOIN fees f ON f.student_id=s.id
            {where} ORDER BY s.id LIMIT ? OFFSET ?""",
            (like, like, like, per_page, (page - 1) * per_page)).fetchall()
        return render_template("students.html", rows=rows, q=q, total=total,
                               page=page, pages=pages, per_page=per_page)

    @app.route("/students/new", methods=["GET", "POST"])
    @guard("admin")
    def student_new():
        db = get_db()
        all_courses = db.execute("SELECT * FROM courses ORDER BY code").fetchall()
        form = {"courses": [], "department": "CSE", "semester": 1}
        if request.method == "POST":
            errs, form = validate_student(db, request.form)
            if not errs:
                db.execute("INSERT INTO students VALUES(?,?,?,?,?,?)", (form["id"], form["name"], form["email"],
                           form["phone"], form["department"], form["semester"]))
                db.execute("INSERT INTO users(username,pass_hash,role,student_id) VALUES(?,?, 'student',?)",
                           (form["id"], generate_password_hash(DEFAULT_STUDENT_PASS), form["id"]))
                db.execute("INSERT INTO fees VALUES(?,0,0)", (form["id"],))
                db.executemany("INSERT INTO enrollments(student_id,course_code) VALUES(?,?)",
                               [(form["id"], c) for c in form["courses"]])
                db.commit()
                flash(f"Student added. Login: {form['id']} / {DEFAULT_STUDENT_PASS}", "ok")
                return redirect(url_for("student_view", sid=form["id"]))
            for m in errs:
                flash(m, "err")
        return render_template("student_form.html", form=form, courses=all_courses, editing=False)

    @app.route("/students/<sid>")
    @guard()
    def student_view(sid):
        if not can_see(sid):
            abort(403)
        d = student_detail(get_db(), sid)
        if not d:
            abort(404)
        return render_template("student_view.html", d=d)

    @app.route("/students/<sid>/edit", methods=["GET", "POST"])
    @guard("admin")
    def student_edit(sid):
        db = get_db()
        cur = db.execute("SELECT * FROM students WHERE id=?", (sid,)).fetchone()
        if not cur:
            abort(404)
        all_courses = db.execute("SELECT * FROM courses ORDER BY code").fetchall()
        form = dict(cur)
        form["courses"] = [r["course_code"] for r in db.execute("SELECT course_code FROM enrollments WHERE student_id=?", (sid,))]
        if request.method == "POST":
            errs, form = validate_student(db, request.form, editing=sid)
            if not errs:
                db.execute("UPDATE students SET name=?,email=?,phone=?,department=?,semester=? WHERE id=?",
                           (form["name"], form["email"], form["phone"], form["department"], form["semester"], sid))
                db.execute("DELETE FROM enrollments WHERE student_id=?", (sid,))
                db.executemany("INSERT INTO enrollments(student_id,course_code) VALUES(?,?)",
                               [(sid, c) for c in form["courses"]])
                db.commit()
                flash("Student updated.", "ok")
                return redirect(url_for("student_view", sid=sid))
            for m in errs:
                flash(m, "err")
        return render_template("student_form.html", form=form, courses=all_courses, editing=True)

    @app.route("/students/<sid>/delete", methods=["POST"])
    @guard("admin")
    def student_delete(sid):
        db = get_db()
        db.execute("DELETE FROM users WHERE student_id=?", (sid,))
        db.execute("DELETE FROM students WHERE id=?", (sid,))   # cascades to all linked records
        db.commit()
        flash("Student deleted with courses, fee, marks, attendance and login.", "ok")
        return redirect(url_for("students"))

    @app.route("/students/<sid>/reset-password", methods=["POST"])
    @guard("admin")
    def student_reset(sid):
        db = get_db()
        db.execute("UPDATE users SET pass_hash=? WHERE student_id=?", (generate_password_hash(DEFAULT_STUDENT_PASS), sid))
        db.commit()
        flash(f"Password reset to {DEFAULT_STUDENT_PASS}.", "ok")
        return redirect(url_for("student_view", sid=sid))

    # --------------------------------------------------------------- courses
    @app.route("/courses", methods=["GET", "POST"])
    @guard("admin")
    def courses():
        db = get_db()
        if request.method == "POST":
            code, name = request.form.get("code", "").strip().upper(), request.form.get("name", "").strip()
            cr = request.form.get("credits", "3").strip()
            if not code or len(code) > 10 or " " in code or not name or len(name) > 40 or not cr.isdigit() or not 1 <= int(cr) <= 10:
                flash("Enter code (max 10, no spaces), name (max 40) and credits (1-10).", "err")
            elif db.execute("SELECT 1 FROM courses WHERE code=?", (code,)).fetchone():
                flash("This course code already exists.", "err")
            else:
                db.execute("INSERT INTO courses VALUES(?,?,?)", (code, name, int(cr)))
                db.commit()
                flash("Course added.", "ok")
            return redirect(url_for("courses"))
        rows = db.execute("""SELECT c.*, (SELECT COUNT(*) FROM enrollments e WHERE e.course_code=c.code) AS enrolled
                             FROM courses c ORDER BY code""").fetchall()
        return render_template("courses.html", rows=rows)

    @app.route("/courses/<code>/delete", methods=["POST"])
    @guard("admin")
    def course_delete(code):
        db = get_db()
        db.execute("DELETE FROM courses WHERE code=?", (code,))
        db.commit()
        flash("Course deleted.", "ok")
        return redirect(url_for("courses"))

    # ------------------------------------------------------------------ fees
    @app.route("/fees")
    @guard("admin")
    def fees():
        rows = get_db().execute("""SELECT s.id, s.name, f.total, f.paid FROM students s
                                   JOIN fees f ON f.student_id=s.id ORDER BY s.id""").fetchall()
        return render_template("fees.html", rows=rows)

    def money(v, lo, hi):
        try:
            x = float(v)
        except (TypeError, ValueError):
            return None
        return round(x, 2) if lo <= x <= hi else None

    @app.route("/fees/<sid>/total", methods=["POST"])
    @guard("admin")
    def fee_total(sid):
        db = get_db()
        f = db.execute("SELECT * FROM fees WHERE student_id=?", (sid,)).fetchone()
        if not f:
            abort(404)
        t = money(request.form.get("total"), f["paid"], 10_000_000)
        if t is None:
            flash(f"Total fee must be a number from {f['paid']:.2f} (already paid) to 10000000.", "err")
        else:
            db.execute("UPDATE fees SET total=? WHERE student_id=?", (t, sid))
            db.commit()
            flash("Total fee updated.", "ok")
        return redirect(url_for("fees"))

    @app.route("/fees/<sid>/pay", methods=["POST"])
    @guard("admin")
    def fee_pay(sid):
        db = get_db()
        f = db.execute("SELECT * FROM fees WHERE student_id=?", (sid,)).fetchone()
        if not f:
            abort(404)
        due = f["total"] - f["paid"]
        a = money(request.form.get("amount"), 0.01, due) if due > 0 else None
        if a is None:
            flash(f"Payment must be between 0.01 and the due amount ({due:.2f}).", "err")
        else:
            db.execute("UPDATE fees SET paid=paid+? WHERE student_id=?", (a, sid))
            db.commit()
            flash(f"Payment of {a:.2f} recorded.", "ok")
        return redirect(url_for("fees"))

    # ----------------------------------------------------------------- marks
    def enrolled(db, code):
        return db.execute("""SELECT s.id, s.name FROM enrollments e JOIN students s ON s.id=e.student_id
                             WHERE e.course_code=? ORDER BY s.id""", (code,)).fetchall()

    @app.route("/marks", methods=["GET", "POST"])
    @guard("admin")
    def marks():
        db = get_db()
        code = request.values.get("course", "")
        exam = request.values.get("exam", EXAMS[0])
        courses_ = db.execute("SELECT * FROM courses ORDER BY code").fetchall()
        rows, mx = [], request.values.get("max_score", "100")
        if code and db.execute("SELECT 1 FROM courses WHERE code=?", (code,)).fetchone() and exam in EXAMS:
            rows = enrolled(db, code)
            if request.method == "POST":
                m = money(mx, 1, 1000)
                parsed, bad = [], []
                for r in rows:
                    v = request.form.get(f"score_{r['id']}", "").strip()
                    if v == "":
                        continue
                    sc = money(v, 0, m if m else 0)
                    (parsed if sc is not None else bad).append((r["id"], sc) if sc is not None else r["id"])
                if m is None:
                    flash("Maximum score must be between 1 and 1000.", "err")
                elif bad:
                    flash(f"Invalid score (must be 0 to {m:g}) for: {', '.join(bad)}. Nothing was saved.", "err")
                else:
                    for sid, sc in parsed:
                        db.execute("""INSERT INTO marks(student_id,course_code,exam,score,max_score) VALUES(?,?,?,?,?)
                                      ON CONFLICT(student_id,course_code,exam)
                                      DO UPDATE SET score=excluded.score, max_score=excluded.max_score""",
                                   (sid, code, exam, sc, m))
                    db.commit()
                    flash(f"Saved {len(parsed)} mark(s).", "ok")
                    return redirect(url_for("marks", course=code, exam=exam))
            existing = {r["student_id"]: r for r in db.execute(
                "SELECT * FROM marks WHERE course_code=? AND exam=?", (code, exam))}
            if existing and request.method == "GET" and "max_score" not in request.args:
                mx = f"{next(iter(existing.values()))['max_score']:g}"
        else:
            existing = {}
        return render_template("marks.html", courses=courses_, code=code, exam=exam, rows=rows,
                               existing=existing, mx=mx)

    # ------------------------------------------------------------ attendance
    @app.route("/attendance", methods=["GET", "POST"])
    @guard("admin")
    def attendance():
        db = get_db()
        code = request.values.get("course", "")
        day = request.values.get("date", date.today().isoformat())
        courses_ = db.execute("SELECT * FROM courses ORDER BY code").fetchall()
        rows, existing = [], {}
        valid_day = bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", day))
        if valid_day:
            try:
                date.fromisoformat(day)
            except ValueError:
                valid_day = False
        if code and valid_day and db.execute("SELECT 1 FROM courses WHERE code=?", (code,)).fetchone():
            rows = enrolled(db, code)
            if request.method == "POST":
                for r in rows:
                    st = request.form.get(f"status_{r['id']}", "")
                    if st in ("P", "A"):
                        db.execute("""INSERT INTO attendance(student_id,course_code,date,status) VALUES(?,?,?,?)
                                      ON CONFLICT(student_id,course_code,date) DO UPDATE SET status=excluded.status""",
                                   (r["id"], code, day, st))
                db.commit()
                flash("Attendance saved.", "ok")
                return redirect(url_for("attendance", course=code, date=day))
            existing = {r["student_id"]: r["status"] for r in db.execute(
                "SELECT student_id,status FROM attendance WHERE course_code=? AND date=?", (code, day))}
        elif code:
            flash("Choose a valid course and date (YYYY-MM-DD).", "err")
        return render_template("attendance.html", courses=courses_, code=code, day=day, rows=rows, existing=existing)

    # ------------------------------------------------------------- analytics
    @app.route("/analytics")
    @guard("admin")
    def analytics():
        return render_template("analytics.html")

    # ------------------------------------------------------------------ APIs
    @app.route("/api/summary")
    @guard("admin", api=True)
    def api_summary():
        return jsonify(summary(get_db()))

    @app.route("/api/analytics/courses")
    @guard("admin", api=True)
    def api_courses():
        return jsonify(course_stats(get_db()))

    @app.route("/api/students")
    @guard("admin", api=True)
    def api_students():
        db = get_db()
        if "page" not in request.args and "per_page" not in request.args:
            return jsonify([dict(r) for r in db.execute("SELECT * FROM students ORDER BY id")])
        page, per_page = page_args(request.args)
        total = db.execute("SELECT COUNT(*) FROM students").fetchone()[0]
        items = [dict(r) for r in db.execute("SELECT * FROM students ORDER BY id LIMIT ? OFFSET ?",
                                             (per_page, (page - 1) * per_page))]
        return jsonify(items=items, page=page, per_page=per_page, total=total,
                       pages=max(1, -(-total // per_page)))

    @app.route("/api/students/<sid>")
    @guard(api=True)
    def api_student(sid):
        if not can_see(sid):
            return jsonify(error="Forbidden"), 403
        d = student_detail(get_db(), sid)
        return (jsonify(d), 200) if d else (jsonify(error="Not found"), 404)

    @app.route("/api/me")
    @guard("student", api=True)
    def api_me():
        return jsonify(student_detail(get_db(), g.user["student_id"]))

    return app


if __name__ == "__main__":
    create_app().run(debug=os.environ.get("FLASK_DEBUG") == "1")
