"""Milestone 5: deployment settings read from environment variables."""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as sdms  # noqa: E402


def login(app, user, pw):
    c = app.test_client()
    c.get("/login")
    with c.session_transaction() as s:
        token = s["csrf"]
    return c.post("/login", data={"username": user, "password": pw, "csrf_token": token}).status_code


class DeploySettings(unittest.TestCase):
    def test_database_path_from_environment(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            path = os.path.join(d, "persistent.db")
            with mock.patch.dict(os.environ, {"SDMS_DB": path}):
                sdms.create_app()
            self.assertTrue(os.path.exists(path))

    def test_admin_password_from_environment(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            with mock.patch.dict(os.environ, {"SDMS_ADMIN_PASSWORD": "Str0ng-Admin-Pass"}):
                app = sdms.create_app(os.path.join(d, "a.db"))
            self.assertEqual(login(app, "admin", "admin123"), 401)
            self.assertEqual(login(app, "admin", "Str0ng-Admin-Pass"), 302)

    def test_defaults_unchanged_without_environment(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            env = {k: v for k, v in os.environ.items() if k != "SDMS_ADMIN_PASSWORD"}
            with mock.patch.dict(os.environ, env, clear=True):
                app = sdms.create_app(os.path.join(d, "b.db"))
            self.assertEqual(login(app, "admin", "admin123"), 302)

    def test_wsgi_entry_point_exposes_app(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            with mock.patch.dict(os.environ, {"SDMS_DB": os.path.join(d, "w.db")}):
                import importlib
                import wsgi
                importlib.reload(wsgi)
                self.assertTrue(callable(wsgi.app))


if __name__ == "__main__":
    unittest.main()
