"""Milestone 4 tests: per-account login lock, session settings, indexes, paging, headers.
Run:  python -m unittest discover -s tests -v
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as sdms  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.app = sdms.create_app(self.path)
        self.app.testing = True

    def tearDown(self):
        for ext in ("", "-wal", "-shm"):
            try:
                os.remove(self.path + ext)
            except OSError:
                pass

    def client(self):
        c = self.app.test_client()
        c.get("/login")                       # creates the CSRF token in the session
        return c

    def post_login(self, c, user, pw):
        with c.session_transaction() as s:
            token = s["csrf"]
        return c.post("/login", data={"username": user, "password": pw, "csrf_token": token})

    def login_ok(self, user="admin", pw="admin123"):
        c = self.client()
        self.assertEqual(self.post_login(c, user, pw).status_code, 302)
        return c


class LoginLockTests(Base):
    def test_lock_after_three_wrong_attempts(self):
        c = self.client()
        self.assertEqual(self.post_login(c, "admin", "x").status_code, 401)
        self.assertEqual(self.post_login(c, "admin", "x").status_code, 401)
        self.assertEqual(self.post_login(c, "admin", "x").status_code, 401)   # 3rd: now locked
        self.assertEqual(self.post_login(c, "admin", "admin123").status_code, 429)

    def test_lock_survives_new_browser_session(self):
        """Milestone 3 weakness: a fresh session used to reset the counter."""
        for _ in range(3):
            self.post_login(self.client(), "admin", "wrong")                  # new client every time
        self.assertEqual(self.post_login(self.client(), "admin", "admin123").status_code, 429)

    def test_other_accounts_not_locked(self):
        for _ in range(3):
            self.post_login(self.client(), "admin", "wrong")
        self.assertEqual(self.post_login(self.client(), "S-1", "student123").status_code, 302)

    def test_unknown_username_is_counted_too(self):
        for _ in range(3):
            self.post_login(self.client(), "nobody", "wrong")
        self.assertEqual(self.post_login(self.client(), "nobody", "wrong").status_code, 429)

    def test_lock_expires(self):
        for _ in range(3):
            self.post_login(self.client(), "S-2", "wrong")
        later = sdms.time.time() + sdms.LOCK_SECONDS + 1
        with mock.patch.object(sdms.time, "time", return_value=later):
            self.assertEqual(self.post_login(self.client(), "S-2", "student123").status_code, 302)

    def test_success_resets_counter(self):
        c = self.client()
        self.post_login(c, "S-1", "wrong")
        self.post_login(c, "S-1", "wrong")
        self.assertEqual(self.post_login(c, "S-1", "student123").status_code, 302)
        c2 = self.client()
        self.post_login(c2, "S-1", "wrong")
        self.post_login(c2, "S-1", "wrong")
        self.assertEqual(self.post_login(c2, "S-1", "student123").status_code, 302)   # still allowed


class SessionAndHeaderTests(Base):
    def test_session_cookie_flags_and_expiry(self):
        c = self.client()
        r = self.post_login(c, "admin", "admin123")
        cookie = r.headers.get("Set-Cookie", "")
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Lax", cookie)
        self.assertIn("Expires=", cookie)          # permanent session with a fixed lifetime
        self.assertEqual(self.app.config["PERMANENT_SESSION_LIFETIME"].total_seconds(),
                         sdms.SESSION_MINUTES * 60)

    def test_security_headers(self):
        h = self.login_ok().get("/api/summary").headers
        self.assertEqual(h["X-Frame-Options"], "DENY")
        self.assertEqual(h["X-Content-Type-Options"], "nosniff")
        self.assertEqual(h["Referrer-Policy"], "same-origin")
        self.assertIn("Cache-Control", h)

    def test_production_needs_real_secret_key(self):
        with mock.patch.dict(os.environ, {"SDMS_ENV": "production"}, clear=False):
            os.environ.pop("SECRET_KEY", None)
            with self.assertRaises(RuntimeError):
                sdms.create_app(self.path)
            os.environ["SECRET_KEY"] = "x" * 40
            app = sdms.create_app(self.path)
            self.assertTrue(app.config["SESSION_COOKIE_SECURE"])

    def test_csrf_still_required(self):
        c = self.login_ok()
        self.assertEqual(c.post("/students/S-1/delete").status_code, 400)

    def test_huge_request_rejected(self):
        c = self.client()
        with c.session_transaction() as s:
            token = s["csrf"]
        r = c.post("/login", data={"username": "a" * 2_000_000, "password": "x", "csrf_token": token})
        self.assertEqual(r.status_code, 413)


class IndexTests(Base):
    def test_indexes_exist(self):
        import sqlite3
        db = sqlite3.connect(self.path)
        names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        for n in ("idx_enr_course", "idx_marks_course", "idx_att_course", "idx_users_student"):
            self.assertIn(n, names)

    def test_course_queries_use_indexes(self):
        import sqlite3
        db = sqlite3.connect(self.path)
        for sql in ("SELECT AVG(score) FROM marks WHERE course_code='CS101'",
                    "SELECT COUNT(*) FROM attendance WHERE course_code='CS101'",
                    "SELECT COUNT(*) FROM enrollments WHERE course_code='CS101'"):
            plan = " ".join(r[3] for r in db.execute("EXPLAIN QUERY PLAN " + sql))
            self.assertIn("USING", plan, plan)
            self.assertNotIn("SCAN marks", plan)
            self.assertNotIn("SCAN attendance", plan)

    def test_wal_mode(self):
        import sqlite3
        self.assertEqual(sqlite3.connect(self.path).execute("PRAGMA journal_mode").fetchone()[0], "wal")


class PagingTests(Base):
    def add_students(self, n):
        import sqlite3
        db = sqlite3.connect(self.path)
        for i in range(n):
            db.execute("INSERT INTO students VALUES(?,?,?,?,?,?)",
                       (f"P-{i:03d}", f"Pager {i}", f"p{i}@x.com", f"9{i:09d}", "CSE", 1))
        db.commit()

    def test_plain_list_unchanged(self):
        r = self.login_ok().get("/api/students")
        self.assertIsInstance(r.get_json(), list)
        self.assertEqual(len(r.get_json()), 3)

    def test_paged_api(self):
        self.add_students(60)
        c = self.login_ok()
        d = c.get("/api/students?page=2&per_page=25").get_json()
        self.assertEqual((d["page"], d["per_page"], d["total"], d["pages"]), (2, 25, 63, 3))
        self.assertEqual(len(d["items"]), 25)
        last = c.get("/api/students?page=3&per_page=25").get_json()
        self.assertEqual(len(last["items"]), 13)

    def test_bad_paging_values_are_safe(self):
        c = self.login_ok()
        for q in ("page=abc", "page=-1", "per_page=0", "per_page=99999", "page=0&per_page=x"):
            self.assertEqual(c.get("/api/students?" + q).status_code, 200, q)
        self.assertEqual(c.get("/api/students?per_page=99999").get_json()["per_page"], sdms.MAX_PER_PAGE)

    def test_paged_api_still_admin_only(self):
        c = self.login_ok("S-1", "student123")
        self.assertEqual(c.get("/api/students?page=1").status_code, 403)

    def test_students_page_with_paging_and_search(self):
        self.add_students(60)
        c = self.login_ok()
        for q in ("", "?page=2", "?page=999", "?q=Pager&page=2", "?q=%25", "?q=a_b", "?page=x"):
            self.assertEqual(c.get("/students" + q).status_code, 200, q)

    def test_like_wildcards_are_escaped(self):
        self.assertEqual(sdms.escape_like("50%_x"), "50\\%\\_x")


if __name__ == "__main__":
    unittest.main()
