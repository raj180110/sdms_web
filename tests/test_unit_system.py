"""Milestone 4: unit tests (single functions) and system tests (whole user journeys).
Run:  python -m unittest discover -s tests -v
"""
import os
import sqlite3
import sys
import tempfile
import unittest

from werkzeug.datastructures import MultiDict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as sdms  # noqa: E402


def form(**over):
    d = {"id": "S-9", "name": "Asha Verma", "email": "asha@mits.edu", "phone": "9876543210",
         "department": "CSE", "semester": "1", "courses": ["CS101", "CS102"]}
    d.update(over)
    m = MultiDict()
    for k, v in d.items():
        for item in (v if isinstance(v, list) else [v]):
            m.add(k, item)
    return m


class UnitTests(unittest.TestCase):
    """Functions tested on their own, without going through HTTP."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        path = os.path.join(self.tmp.name, "u.db")
        sdms.init_db(path)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    # ---- validate_student
    def test_valid_student_has_no_errors(self):
        errors, clean = sdms.validate_student(self.db, form())
        self.assertEqual(errors, [])
        self.assertEqual(clean["semester"], 1)
        self.assertEqual(clean["courses"], ["CS101", "CS102"])

    def test_each_validation_rule(self):
        cases = {"id": [("S-1", "already exists"), ("has space", "Student ID"), ("x" * 11, "Student ID"), ("", "Student ID")],
                 "name": [("", "Name"), ("x" * 21, "Name")],
                 "email": [("bad", "valid email"), ("a@b", "valid email"), ("student-1@gmail.com", "email already")],
                 "phone": [("12", "Phone"), ("12ab5678", "Phone"), ("016111111111", "phone number already")],
                 "semester": [("0", "Semester"), ("9", "Semester"), ("x", "Semester")],
                 "courses": [([], "valid courses"), (["NOPE"], "valid courses"),
                             (["CS101", "CS102", "CS103", "MA101", "CS101X"], "valid courses")]}
        for field, items in cases.items():
            for value, msg in items:
                errors, _ = sdms.validate_student(self.db, form(**{field: value}))
                self.assertTrue(any(msg in e for e in errors), (field, value, errors))

    def test_boundary_values_accepted(self):
        for over in ({"id": "x" * 10}, {"name": "x" * 20}, {"semester": "8"}, {"phone": "+1234567"},
                     {"courses": ["CS101", "CS102", "CS103", "MA101"]}):
            errors, _ = sdms.validate_student(self.db, form(**over))
            self.assertEqual(errors, [], over)

    def test_duplicate_courses_in_form_are_collapsed(self):
        _, clean = sdms.validate_student(self.db, form(courses=["CS101", "CS101"]))
        self.assertEqual(clean["courses"], ["CS101"])

    def test_editing_own_email_is_not_a_duplicate(self):
        errors, _ = sdms.validate_student(self.db, form(email="student-1@gmail.com", phone="016111111111"), editing="S-1")
        self.assertEqual(errors, [])

    # ---- analytics
    def test_seed_course_stats(self):
        rows = {r["code"]: r for r in sdms.course_stats(self.db)}
        self.assertEqual((rows["CS101"]["enrolled"], rows["CS101"]["avg_marks"], rows["CS101"]["pass_pct"]), (3, 61.7, 100.0))
        self.assertEqual(rows["CS101"]["attendance_pct"], 75.0)
        self.assertIsNone(rows["MA101"]["avg_marks"])           # no data -> None, not 0 or an error

    def test_summary_values(self):
        s = sdms.summary(self.db)
        self.assertEqual((s["students"], s["courses"]), (3, 4))
        self.assertEqual((s["fee_total"], s["fee_paid"], s["fee_due"]), (150000, 80000, 70000))

    def test_summary_on_empty_marks_and_attendance(self):
        self.db.execute("DELETE FROM marks")
        self.db.execute("DELETE FROM attendance")
        s = sdms.summary(self.db)
        self.assertIsNone(s["avg_marks"])
        self.assertIsNone(s["pass_pct"])
        self.assertIsNone(s["attendance_pct"])

    def test_pass_mark_is_40_percent(self):
        self.db.execute("DELETE FROM marks")
        for exam, score in (("Mid-1", 40), ("Mid-2", 39.9)):
            self.db.execute("INSERT INTO marks(student_id,course_code,exam,score,max_score) VALUES('S-1','CS101',?,?,100)",
                            (exam, score))
        self.assertEqual(sdms.summary(self.db)["pass_pct"], 50.0)

    def test_student_detail_unknown_id(self):
        self.assertIsNone(sdms.student_detail(self.db, "S-404"))

    # ---- database rules
    def test_database_rejects_bad_rows(self):
        bad = ["INSERT INTO fees VALUES('S-1', 100, 200)",                                             # paid > total
               "INSERT INTO marks(student_id,course_code,exam,score,max_score) VALUES('S-1','CS101','X',150,100)",
               "INSERT INTO attendance(student_id,course_code,date,status) VALUES('S-1','CS101','2026-01-01','Z')",
               "INSERT INTO students VALUES('S-1','Dup','d@x.com','9000000000','CSE',1)",             # duplicate id
               "INSERT INTO enrollments(student_id,course_code) VALUES('NOPE','CS101')"]               # missing parent
        for sql in bad:
            with self.assertRaises(sqlite3.IntegrityError, msg=sql):
                self.db.execute(sql)

    # ---- helpers
    def test_page_args(self):
        self.assertEqual(sdms.page_args({}), (1, sdms.DEFAULT_PER_PAGE))
        self.assertEqual(sdms.page_args({"page": "3", "per_page": "10"}), (3, 10))
        self.assertEqual(sdms.page_args({"page": "-2", "per_page": "abc"}), (1, sdms.DEFAULT_PER_PAGE))
        self.assertEqual(sdms.page_args({"per_page": "100000"})[1], sdms.MAX_PER_PAGE)


class SystemTests(unittest.TestCase):
    """Whole journeys through the running application (browser-level requests)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.path = os.path.join(self.tmp.name, "s.db")
        self.app = sdms.create_app(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def login(self, user, pw):
        c = self.app.test_client()
        c.get("/login")
        r = self.post(c, "/login", {"username": user, "password": pw})
        return c, r

    def post(self, c, url, data=None):
        with c.session_transaction() as s:
            token = s.get("csrf") or ""
        if not token:
            c.get("/login")
            with c.session_transaction() as s:
                token = s["csrf"]
        return c.post(url, data=dict(data or {}, csrf_token=token))

    def test_student_lifecycle(self):
        admin, r = self.login("admin", "admin123")
        self.assertEqual(r.status_code, 302)

        # 1. admin registers a student with two courses and sets up the fee
        self.post(admin, "/students/new", {"id": "S-9", "name": "Asha Verma", "email": "asha@mits.edu",
                                           "phone": "9876543210", "department": "CSE", "semester": "1",
                                           "courses": ["CS101", "CS102"]})
        self.post(admin, "/fees/S-9/total", {"total": "60000"})
        self.post(admin, "/fees/S-9/pay", {"amount": "25000"})

        # 2. admin enters marks and attendance
        self.post(admin, "/marks", {"course": "CS101", "exam": "Mid-2", "max_score": "50", "score_S-9": "44"})
        self.post(admin, "/attendance", {"course": "CS101", "date": "2026-10-09", "status_S-9": "P"})

        # 3. the student logs in with the default password and sees only own data
        stu, r = self.login("S-9", "student123")
        self.assertEqual(r.status_code, 302)
        me = stu.get("/api/me").get_json()
        self.assertEqual(me["student"]["name"], "Asha Verma")
        self.assertEqual((me["fee"]["total"], me["fee"]["paid"], me["fee"]["due"]), (60000, 25000, 35000))
        self.assertEqual(me["overall"]["avg_marks"], 88.0)
        self.assertEqual(me["overall"]["attendance_pct"], 100.0)
        self.assertEqual(stu.get("/students").status_code, 403)
        self.assertEqual(stu.get("/students/S-1").status_code, 403)

        # 4. admin analytics and summary include the new data
        courses = {r["code"]: r for r in admin.get("/api/analytics/courses").get_json()}
        self.assertEqual(courses["CS101"]["enrolled"], 4)
        summary = admin.get("/api/summary").get_json()
        self.assertEqual((summary["students"], summary["fee_paid"]), (4, 105000))

        # 5. admin deletes the student: data and login are gone
        self.post(admin, "/students/S-9/delete")
        self.assertEqual(admin.get("/api/students/S-9").status_code, 404)
        _, r = self.login("S-9", "student123")
        self.assertEqual(r.status_code, 401)
        db = sqlite3.connect(self.path)
        try:
            for table in ("students", "users", "fees", "enrollments", "marks", "attendance"):
                col = "id" if table == "students" else "student_id"
                self.assertEqual(db.execute(f"SELECT COUNT(*) FROM {table} WHERE {col}='S-9'").fetchone()[0], 0, table)
        finally:
            db.close()                      # Windows cannot delete a database file that is still open

    def test_data_survives_restart(self):
        admin, _ = self.login("admin", "admin123")
        self.post(admin, "/courses", {"code": "ZZ1", "name": "Persistent", "credits": "3"})
        again = sdms.create_app(self.path)                     # "restart": new app on the same database file
        c = again.test_client()
        c.get("/login")
        with c.session_transaction() as s:
            tok = s["csrf"]
        c.post("/login", data={"username": "admin", "password": "admin123", "csrf_token": tok})
        self.assertIn(b"ZZ1", c.get("/courses").data)

    def test_password_change_and_admin_reset_journey(self):
        stu, _ = self.login("S-2", "student123")
        self.post(stu, "/password", {"old": "student123", "new": "fresh123", "again": "fresh123"})
        self.assertEqual(self.login("S-2", "fresh123")[1].status_code, 302)
        self.assertEqual(self.login("S-2", "student123")[1].status_code, 401)
        admin, _ = self.login("admin", "admin123")
        self.post(admin, "/students/S-2/reset-password")
        self.assertEqual(self.login("S-2", "student123")[1].status_code, 302)

    def test_logout_ends_session(self):
        admin, _ = self.login("admin", "admin123")
        self.assertEqual(admin.get("/dashboard").status_code, 200)
        self.post(admin, "/logout")
        self.assertEqual(admin.get("/dashboard").status_code, 302)
        self.assertEqual(admin.get("/api/summary").status_code, 401)

    def test_unknown_page_gives_404(self):
        admin, _ = self.login("admin", "admin123")
        self.assertEqual(admin.get("/no-such-page").status_code, 404)
        self.assertEqual(admin.get("/students/S-404").status_code, 404)


if __name__ == "__main__":
    unittest.main()
