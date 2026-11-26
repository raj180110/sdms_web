"""
SDMS load test (Milestone 4).  Builds a large temporary database, then measures the
JSON APIs one request at a time (latency) and with several users at once (throughput).

  python tools/load_test.py                          # 2000 students, with indexes
  python tools/load_test.py --no-indexes             # same data, M4 indexes dropped (the "before" run)
  python tools/load_test.py --students 5000 --threads 16 --out results.md

No network or running server is needed: requests go straight into the Flask app.
"""
import argparse
import os
import random
import sqlite3
import statistics
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as sdms  # noqa: E402

INDEXES = ["idx_enr_course", "idx_marks_course", "idx_att_course", "idx_users_student"]
EXAMS = sdms.EXAMS


def build_db(path, n_students, n_courses, days):
    rnd = random.Random(1)
    db = sqlite3.connect(path)
    db.execute("PRAGMA foreign_keys = ON")
    codes = [f"C{i:03d}" for i in range(n_courses)]
    db.executemany("INSERT INTO courses VALUES(?,?,?)", [(c, f"Course {c}", 4) for c in codes])
    shared_hash = sdms.generate_password_hash("student123")      # one hash: hashing 1000s of passwords is slow
    stu, usr, fee, enr, mk, att = [], [], [], [], [], []
    dates = [f"2026-{9 + d // 30:02d}-{d % 28 + 1:02d}" for d in range(days)]
    dates = list(dict.fromkeys(dates))[:days]
    for i in range(n_students):
        sid = f"P-{i:05d}"
        stu.append((sid, f"Student {i}", f"s{i}@mits.edu", f"9{i:09d}", "CSE", 1))
        usr.append((sid, shared_hash, "student", sid))
        fee.append((sid, 60000, rnd.choice([0, 30000, 60000])))
        for c in rnd.sample(codes, 3):
            enr.append((sid, c))
            for e in EXAMS:
                mk.append((sid, c, e, rnd.randint(10, 100), 100))
            for d in dates:
                att.append((sid, c, d, "P" if rnd.random() < 0.8 else "A"))
    db.executemany("INSERT INTO students VALUES(?,?,?,?,?,?)", stu)
    db.executemany("INSERT INTO users(username,pass_hash,role,student_id) VALUES(?,?,?,?)", usr)
    db.executemany("INSERT INTO fees VALUES(?,?,?)", fee)
    db.executemany("INSERT INTO enrollments(student_id,course_code) VALUES(?,?)", enr)
    db.executemany("INSERT INTO marks(student_id,course_code,exam,score,max_score) VALUES(?,?,?,?,?)", mk)
    db.executemany("INSERT INTO attendance(student_id,course_code,date,status) VALUES(?,?,?,?)", att)
    db.commit()
    counts = dict(students=len(stu), enrollments=len(enr), marks=len(mk), attendance=len(att))
    db.close()
    return counts


def admin_client(app):
    c = app.test_client()
    c.get("/login")
    with c.session_transaction() as s:
        token = s["csrf"]
    r = c.post("/login", data={"username": "admin", "password": "admin123", "csrf_token": token})
    assert r.status_code == 302, "admin login failed"
    return c


def pct(values, p):
    values = sorted(values)
    return values[min(len(values) - 1, int(len(values) * p))]


def timed(fn):
    t = time.perf_counter()
    r = fn()
    return (time.perf_counter() - t) * 1000, r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--students", type=int, default=2000)
    ap.add_argument("--courses", type=int, default=20)
    ap.add_argument("--days", type=int, default=30, help="attendance days per enrolment")
    ap.add_argument("--repeat", type=int, default=30, help="requests per endpoint (latency test)")
    ap.add_argument("--threads", type=int, default=8)
    ap.add_argument("--per-thread", type=int, default=40, help="requests per thread (throughput test)")
    ap.add_argument("--no-indexes", action="store_true", help="drop the Milestone 4 indexes first")
    ap.add_argument("--out", help="also write the result table to this file")
    a = ap.parse_args()

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        app = sdms.create_app(path)
        counts = build_db(path, a.students, a.courses, a.days)
        db = sqlite3.connect(path)
        if a.no_indexes:
            for n in INDEXES:
                db.execute(f"DROP INDEX IF EXISTS {n}")
        db.execute("ANALYZE")
        db.commit()
        db.close()
        mode = "WITHOUT M4 indexes" if a.no_indexes else "WITH M4 indexes"

        c = admin_client(app)
        endpoints = [
            ("GET /api/summary", "/api/summary"),
            ("GET /api/analytics/courses", "/api/analytics/courses"),
            ("GET /api/students (all)", "/api/students"),
            ("GET /api/students?page=1&per_page=25", "/api/students?page=1&per_page=25"),
            ("GET /api/students/<id>", "/api/students/P-00100"),
        ]
        lines = [f"### SDMS load test - {mode}",
                 f"Data: {counts['students']} students, {counts['enrollments']} enrolments, "
                 f"{counts['marks']} marks, {counts['attendance']} attendance rows",
                 "", "**Latency (one request at a time, ms)**", "",
                 "| Endpoint | median | p95 | max |", "|---|---:|---:|---:|"]
        for label, url in endpoints:
            c.get(url)                                        # warm-up
            ms = []
            for _ in range(a.repeat):
                t, r = timed(lambda: c.get(url))
                assert r.status_code == 200, (url, r.status_code)
                ms.append(t)
            lines.append(f"| {label} | {statistics.median(ms):.1f} | {pct(ms, 0.95):.1f} | {max(ms):.1f} |")

        # login cost (password hashing is deliberately slow)
        ms = []
        for _ in range(5):
            cc = app.test_client()
            cc.get("/login")
            with cc.session_transaction() as s:
                tok = s["csrf"]
            t, r = timed(lambda: cc.post("/login", data={"username": "admin", "password": "admin123",
                                                         "csrf_token": tok}))
            ms.append(t)
        lines.append(f"| POST /login (hash check) | {statistics.median(ms):.1f} | {max(ms):.1f} | {max(ms):.1f} |")

        # concurrent users: every thread is a logged-in admin sending a mix of requests
        urls = [u for _, u in endpoints]
        all_ms, errors = [], []
        lock = threading.Lock()

        def worker(seed):
            rnd = random.Random(seed)
            cl = admin_client(app)
            local = []
            for _ in range(a.per_thread):
                url = rnd.choice(urls)
                t, r = timed(lambda: cl.get(url))
                local.append(t)
                if r.status_code != 200:
                    with lock:
                        errors.append((url, r.status_code))
            with lock:
                all_ms.extend(local)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(a.threads)]
        t0 = time.perf_counter()
        [t.start() for t in threads]
        [t.join() for t in threads]
        wall = time.perf_counter() - t0
        lines += ["", f"**Concurrent test: {a.threads} users x {a.per_thread} requests (mixed endpoints)**", "",
                  "| Requests | Errors | Throughput (req/s) | median ms | p95 ms |", "|---:|---:|---:|---:|---:|",
                  f"| {len(all_ms)} | {len(errors)} | {len(all_ms) / wall:.0f} | "
                  f"{statistics.median(all_ms):.1f} | {pct(all_ms, 0.95):.1f} |"]
        out = "\n".join(lines)
        print(out)
        if a.out:
            with open(a.out, "w", encoding="utf-8") as f:
                f.write(out + "\n")
    finally:
        for ext in ("", "-wal", "-shm"):
            try:
                os.remove(path + ext)
            except OSError:
                pass


if __name__ == "__main__":
    main()
