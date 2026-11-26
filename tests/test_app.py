"""Run:  python -m unittest discover -s tests -v   (also works with pytest)"""
import os, sqlite3, sys, tempfile, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import create_app


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "t.db")
        self.app = create_app(self.path)
        self.c = self.app.test_client()

    def tearDown(self):
        self.dir.cleanup()

    def token(self, c=None):
        c = c or self.c
        with c.session_transaction() as s:
            if "csrf" not in s:
                pass
        c.get("/login")
        with c.session_transaction() as s:
            return s["csrf"]

    def post(self, url, data=None, c=None, follow=False):
        c = c or self.c
        d = dict(data or {})
        d["csrf_token"] = self.token(c)
        return c.post(url, data=d, follow_redirects=follow)

    def login(self, u, p, c=None):
        return self.post("/login", {"username": u, "password": p}, c=c)

    def as_admin(self):
        self.login("admin", "admin123")
        return self.c

    def as_student(self, sid="S-3"):
        c = self.app.test_client()
        self.login(sid, "student123", c=c)
        return c

    def q(self, sql, *a):
        db = sqlite3.connect(self.path)
        try:
            return db.execute(sql, a).fetchall()
        finally:
            db.close()

    def new_student(self, **over):
        d = {"id": "S-9", "name": "Asha Verma", "email": "asha@mits.edu", "phone": "9876543210",
             "department": "CSE", "semester": "1", "courses": ["CS101", "CS102"]}
        d.update(over)
        return self.post("/students/new", d, follow=True)


class AuthAndRBAC(Base):
    def test_admin_login_ok(self):
        r = self.login("admin", "admin123")
        self.assertEqual(r.status_code, 302)
        self.assertIn(b"Admin Dashboard", self.c.get("/dashboard").data)

    def test_wrong_password_rejected(self):
        self.assertEqual(self.login("admin", "bad").status_code, 401)
        self.assertEqual(self.c.get("/dashboard").status_code, 302)

    def test_lock_after_three_failures(self):
        for _ in range(3):
            self.login("admin", "bad")
        self.assertEqual(self.login("admin", "admin123").status_code, 429)

    def test_post_without_csrf_rejected(self):
        self.assertEqual(self.c.post("/login", data={"username": "admin", "password": "admin123"}).status_code, 400)

    def test_passwords_are_hashed(self):
        for (h,) in self.q("SELECT pass_hash FROM users"):
            self.assertNotIn("admin123", h)
            self.assertNotIn("student123", h)

    def test_unauthenticated_access(self):
        self.assertEqual(self.c.get("/students").status_code, 302)
        self.assertEqual(self.c.get("/api/summary").status_code, 401)

    def test_student_blocked_from_admin_pages(self):
        s = self.as_student()
        for url in ["/students", "/students/new", "/courses", "/fees", "/marks", "/attendance", "/analytics",
                    "/students/S-1/edit"]:
            self.assertEqual(s.get(url).status_code, 403, url)

    def test_student_blocked_from_admin_actions(self):
        s = self.as_student()
        self.assertEqual(self.post("/students/S-1/delete", c=s).status_code, 403)
        self.assertEqual(self.post("/fees/S-3/pay", {"amount": "10"}, c=s).status_code, 403)
        self.assertEqual(len(self.q("SELECT 1 FROM students")), 3)

    def test_student_blocked_from_admin_apis(self):
        s = self.as_student()
        for url in ["/api/summary", "/api/analytics/courses", "/api/students"]:
            self.assertEqual(s.get(url).status_code, 403, url)

    def test_student_sees_only_own_data(self):
        s = self.as_student("S-3")
        self.assertEqual(s.get("/students/S-3").status_code, 200)
        self.assertEqual(s.get("/students/S-2").status_code, 403)
        self.assertEqual(s.get("/api/students/S-3").status_code, 200)
        self.assertEqual(s.get("/api/students/S-2").status_code, 403)
        self.assertEqual(s.get("/api/me").get_json()["student"]["id"], "S-3")

    def test_admin_cannot_use_student_only_api(self):
        self.assertEqual(self.as_admin().get("/api/me").status_code, 403)

    def test_change_password(self):
        s = self.as_student("S-1")
        self.post("/password", {"old": "wrong", "new": "newpass1", "again": "newpass1"}, c=s)
        self.assertEqual(self.login("S-1", "newpass1", c=self.app.test_client()).status_code, 401)
        self.post("/password", {"old": "student123", "new": "newpass1", "again": "newpass1"}, c=s)
        self.assertEqual(self.login("S-1", "newpass1", c=self.app.test_client()).status_code, 302)


class StudentModule(Base):
    def test_add_student_creates_all_linked_records(self):
        self.as_admin()
        r = self.new_student()
        self.assertIn(b"Student added", r.data)
        self.assertEqual(self.q("SELECT name FROM students WHERE id='S-9'")[0][0], "Asha Verma")
        self.assertEqual(len(self.q("SELECT 1 FROM enrollments WHERE student_id='S-9'")), 2)
        self.assertEqual(self.q("SELECT total,paid FROM fees WHERE student_id='S-9'")[0], (0, 0))
        self.assertEqual(self.login("S-9", "student123", c=self.app.test_client()).status_code, 302)

    def test_validation_rules(self):
        self.as_admin()
        cases = [({"id": "S-1"}, b"already exists"), ({"email": "bademail"}, b"valid email"),
                 ({"phone": "123"}, b"Phone must be digits"), ({"name": "x" * 21}, b"Name is required"),
                 ({"email": "student-1@gmail.com"}, b"email already exists"),
                 ({"phone": "016111111111"}, b"phone number already exists"),
                 ({"id": "admin"}, b"already exists"), ({"id": "has space"}, b"Student ID"),
                 ({"semester": "9"}, b"Semester"), ({"courses": []}, b"1 to 4"),
                 ({"courses": ["CS101", "CS102", "CS103", "MA101", "XX"]}, b"1 to 4")]
        for over, msg in cases:
            self.assertIn(msg, self.new_student(**over).data, over)
        self.assertEqual(len(self.q("SELECT 1 FROM students")), 3)

    def test_four_courses_allowed_five_rejected(self):
        self.as_admin()
        self.post("/courses", {"code": "XX1", "name": "Extra", "credits": "3"})
        self.assertIn(b"Student added", self.new_student(courses=["CS101", "CS102", "CS103", "MA101"]).data)
        self.assertIn(b"1 to 4", self.new_student(id="S-10", email="b@b.com", phone="9999999999",
                                                  courses=["CS101", "CS102", "CS103", "MA101", "XX1"]).data)

    def test_edit_keeps_id_and_replaces_courses(self):
        self.as_admin()
        self.post("/students/S-2/edit", {"name": "New Name", "email": "n@mits.edu", "phone": "9000000001",
                                         "department": "ECE", "semester": "2", "courses": ["MA101"]})
        self.assertEqual(self.q("SELECT name,department FROM students WHERE id='S-2'")[0], ("New Name", "ECE"))
        self.assertEqual(self.q("SELECT course_code FROM enrollments WHERE student_id='S-2'"), [("MA101",)])

    def test_delete_cascades_everywhere(self):
        self.as_admin()
        self.post("/students/S-3/delete")
        for t, col in [("students", "id"), ("users", "student_id"), ("fees", "student_id"), ("enrollments", "student_id"),
                       ("marks", "student_id"), ("attendance", "student_id")]:
            self.assertEqual(self.q(f"SELECT 1 FROM {t} WHERE {col}='S-3'"), [], t)

    def test_search(self):
        r = self.as_admin().get("/students?q=Student 2")
        self.assertIn(b"S-2", r.data)
        self.assertNotIn(b"student-1@gmail.com", r.data)


class FeeModule(Base):
    def test_payment_rules(self):
        self.as_admin()
        self.post("/fees/S-2/pay", {"amount": "99999"})
        self.assertEqual(self.q("SELECT paid FROM fees WHERE student_id='S-2'")[0][0], 30000)
        self.post("/fees/S-2/pay", {"amount": "20000"})
        self.assertEqual(self.q("SELECT paid FROM fees WHERE student_id='S-2'")[0][0], 50000)
        self.post("/fees/S-2/pay", {"amount": "1"})
        self.assertEqual(self.q("SELECT paid FROM fees WHERE student_id='S-2'")[0][0], 50000)

    def test_total_cannot_go_below_paid(self):
        self.as_admin()
        self.post("/fees/S-2/total", {"total": "100"})
        self.assertEqual(self.q("SELECT total FROM fees WHERE student_id='S-2'")[0][0], 50000)
        self.post("/fees/S-2/total", {"total": "60000"})
        self.assertEqual(self.q("SELECT total FROM fees WHERE student_id='S-2'")[0][0], 60000)

    def test_summary_api_matches_fee_table(self):
        s = self.as_admin().get("/api/summary").get_json()
        self.assertEqual((s["fee_total"], s["fee_paid"], s["fee_due"]), (150000, 80000, 70000))


class ExamAndAttendance(Base):
    def test_seed_analytics(self):
        rows = {r["code"]: r for r in self.as_admin().get("/api/analytics/courses").get_json()}
        self.assertEqual((rows["CS101"]["avg_marks"], rows["CS101"]["pass_pct"], rows["CS101"]["attendance_pct"]), (61.7, 100.0, 75.0))
        self.assertEqual((rows["CS102"]["avg_marks"], rows["CS102"]["pass_pct"]), (45.0, 50.0))
        self.assertEqual(rows["CS103"]["pass_pct"], 0.0)
        self.assertIsNone(rows["MA101"]["avg_marks"])

    def test_marks_validation_saves_nothing_when_one_is_bad(self):
        self.as_admin()
        self.post("/marks", {"course": "CS101", "exam": "Mid-2", "max_score": "50",
                             "score_S-1": "40", "score_S-2": "70", "score_S-3": "10"})
        self.assertEqual(self.q("SELECT 1 FROM marks WHERE exam='Mid-2'"), [])

    def test_marks_saved_then_updated_and_flow_to_analytics_and_student_view(self):
        self.as_admin()
        self.post("/marks", {"course": "CS101", "exam": "Mid-2", "max_score": "50",
                             "score_S-1": "40", "score_S-2": "20", "score_S-3": "10"})
        self.assertEqual(len(self.q("SELECT 1 FROM marks WHERE exam='Mid-2'")), 3)
        self.post("/marks", {"course": "CS101", "exam": "Mid-2", "max_score": "50", "score_S-1": "45"})
        self.assertEqual(self.q("SELECT score FROM marks WHERE exam='Mid-2' AND student_id='S-1'")[0][0], 45)
        api = self.as_student("S-1").get("/api/me").get_json()
        self.assertEqual(len(api["marks"]), 2)
        self.assertEqual(api["overall"]["avg_marks"], 84.0)     # (78 + 45/50*100) / 2

    def test_marks_for_non_enrolled_student_ignored(self):
        self.as_admin()
        self.post("/marks", {"course": "CS103", "exam": "Mid-1", "max_score": "100", "score_S-1": "90"})
        self.assertEqual(self.q("SELECT 1 FROM marks WHERE student_id='S-1' AND course_code='CS103'"), [])

    def test_attendance_save_updates_percentage(self):
        self.as_admin()
        self.post("/attendance", {"course": "CS101", "date": "2026-10-09",
                                  "status_S-1": "P", "status_S-2": "P", "status_S-3": "A"})
        rows = {r["code"]: r for r in self.c.get("/api/analytics/courses").get_json()}
        self.assertEqual(rows["CS101"]["attendance_pct"], round(100 * 11 / 15, 1))
        self.post("/attendance", {"course": "CS101", "date": "2026-10-09", "status_S-3": "P"})
        self.assertEqual(self.q("SELECT status FROM attendance WHERE student_id='S-3' AND date='2026-10-09'")[0][0], "P")

    def test_attendance_rejects_bad_date(self):
        self.as_admin()
        r = self.post("/attendance", {"course": "CS101", "date": "2026-13-45", "status_S-1": "P"}, follow=True)
        self.assertEqual(self.q("SELECT 1 FROM attendance WHERE date='2026-13-45'"), [])


class Security(Base):
    def test_sql_injection_in_search_is_harmless(self):
        r = self.as_admin().get("/students?q=' OR '1'='1")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn(b"S-1</b>", r.data)

    def test_xss_is_escaped(self):
        self.as_admin()
        self.new_student(name="<script>alert(1)</script>")
        self.assertEqual(self.q("SELECT 1 FROM students WHERE id='S-9'"), [])   # rejected (length / content)
        self.new_student(name="<b>x</b>")
        self.assertNotIn(b"<b>x</b>", self.c.get("/students").data)

    def test_security_headers(self):
        r = self.c.get("/login")
        self.assertEqual(r.headers["X-Frame-Options"], "DENY")
        self.assertEqual(r.headers["X-Content-Type-Options"], "nosniff")


if __name__ == "__main__":
    unittest.main()
