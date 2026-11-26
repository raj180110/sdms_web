# SDMS - Student Lifecycle Management System (web version)

Flask + SQLite web app. Milestone 3 build: frontend integrated with backend APIs,
Examination (marks) and Academic Records (attendance) modules, role-based access control (RBAC).

## Run on your computer
```
pip install -r requirements.txt
python app.py
```
Open http://127.0.0.1:5000

| Role    | Username            | Password   |
|---------|---------------------|------------|
| Admin   | admin               | admin123   |
| Student | S-1, S-2, S-3       | student123 |

The database file `sdms.db` is created automatically with demo data on the first run
(delete the file to reset). Change `SECRET_KEY` before real deployment:
`set SECRET_KEY=some-long-random-text` (Windows) / `export SECRET_KEY=...` (Linux).

## Modules
| Module | Admin | Student |
|---|---|---|
| Students (add / search / edit / delete, 1-4 courses, validation) | full | own profile only |
| Courses | add / delete | view own |
| Fee management (total, payment, due, report) | full | view own |
| Marks (exam-wise entry, pass mark 40%) | enter / update | view own |
| Attendance (date-wise Present/Absent) | enter / update | view own % |
| Analytics (avg marks, pass %, attendance %) | course-wise | own summary |
| Login, change password, reset password | yes | change own |

## JSON APIs used by the frontend
| Endpoint | Who | Returns |
|---|---|---|
| GET /api/summary | admin | totals, fee, avg marks, pass %, attendance % |
| GET /api/analytics/courses | admin | per-course analytics |
| GET /api/students | admin | all students |
| GET /api/students/<id> | admin or that student | profile, courses, marks, fee, attendance |
| GET /api/me | student | own full record |

Not logged in -> 401. Wrong role / someone else's record -> 403.

## Tests (RBAC, validation, data flow, security)
```
python -m unittest discover -s tests -v
```
(30 tests; pytest also works if installed.)

## Deploy (free)
**Render / Railway:** push this folder to GitHub, create a Web Service, build command
`pip install -r requirements.txt`, start command `gunicorn wsgi:app`, add env var `SECRET_KEY`.
**PythonAnywhere:** upload the folder, create a Flask web app, point WSGI file to `from app import create_app; application = create_app()`.
Note: free hosts may reset the SQLite file on restart - fine for demo; use a persistent disk or PostgreSQL later.

## Project files
- `app.py` backend (routes, validation, SQL, APIs)   - `wsgi.py` production entry
- `templates/` pages   - `static/` CSS + JavaScript (fetches the APIs)
- `tests/test_app.py` automated tests

## Milestone 4 additions
- **Login lock per account:** 3 wrong passwords lock that username for 60 s, stored in the `login_attempts` table (survives new browsers/cookies).
- **Sessions:** 30-minute lifetime, `HttpOnly`, `SameSite=Lax`; with `SDMS_ENV=production` the cookie is HTTPS-only, HSTS is sent and the app refuses to start with the default `SECRET_KEY`.
- **Speed:** indexes on course-wise columns, SQLite WAL mode, paging (`/students?page=2`, `/api/students?page=2&per_page=25`; plain `/api/students` still returns the full list).
- **Tests:** `python -m unittest discover -s tests -v` (30 from Milestone 3 + 20 in `tests/test_m4.py` + 17 in `tests/test_unit_system.py` = 67, all passing).
- **Load test:** `python tools/load_test.py` (add `--no-indexes` for the "before" run).

## Milestone 5: deployment settings (environment variables)
| Variable | Purpose |
|---|---|
| `SECRET_KEY` | signs session cookies (required in production) |
| `SDMS_ENV=production` | HTTPS-only cookies + HSTS; refuses to start with the default key (use on HTTPS hosts only) |
| `SDMS_DB` | database file path, e.g. a persistent disk `/data/sdms.db` |
| `SDMS_ADMIN_PASSWORD` | admin password when the database is first created (instead of `admin123`) |

Windows local server: `python -m pip install waitress` then `python -m waitress --listen=127.0.0.1:8000 wsgi:app`.
Linux / cloud: `gunicorn wsgi:app --workers 2`.
Tests: 71 (`python -m unittest discover -s tests -v`).
