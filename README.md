# Employee Attendance & Analytics API

FastAPI service for employee attendance, manual corrections, and MongoDB-backed reporting. It follows the endpoints,
response shapes, and attendance rules in [`openapi.yaml`](openapi.yaml).

## Features

- Create and list employees.
- Punch in and out; calculate attendance dates in IST and store derived late, overtime, work-hour, and half-day values.
- List attendance and regularize records with an audit history of changed fields.
- Report monthly employee and department attendance, rank late arrivals, and return a gap-filled daily department trend.
- Inspect MongoDB execution statistics for the supported endpoint queries.

Instants cross the API boundary as epoch milliseconds and are stored in MongoDB as BSON dates. Analytics use the stored
derived attendance values and MongoDB aggregation pipelines.

## Implementation changes

The main implementation is in `app/main.py`, as required by the assignment. It includes:

- The contract routes:
  - System and employees: `GET /health`, `POST /employees`, `GET /employees`.
  - Attendance: `POST /attendance/punch-in`, `POST /attendance/punch-out`, `GET /attendance`, and
    `PATCH /attendance/{emp_code}/{date}`.
  - Analytics: `GET /analytics/employees/{emp_code}/monthly`, `GET /analytics/departments/summary`,
    `GET /analytics/leaderboard/late`, and `GET /analytics/departments/{department}/trend`.
  - Query plans: `GET /admin/explain/{endpoint}`.
- Pydantic request models for employee creation, punch-in, punch-out, and regularization. Employee codes, departments,
  names, email, shift times, dates, and epoch-millisecond timestamps are validated at the request boundary.
- IST attendance-date handling, including assigning early-morning punches on overnight shifts to the preceding shift
  date. Punch instants are truncated to whole seconds before storage and response.
- Server calculations for the strict 10-minute lateness grace period, the 30-minute overtime threshold, work-hour
  half-up rounding, and half-day status based on rounded hours.
- Duplicate protection through unique MongoDB indexes. Punch-out updates only an open record, while regularization
  validates the corrected status and times, recomputes derived values, and appends an audit entry for fields that
  changed.
- API serialization that hides MongoDB `_id`, converts stored BSON datetimes to epoch milliseconds, and supplies
  contract defaults for optional legacy attendance fields.
- MongoDB aggregation pipelines for employee monthly metrics, department summaries, the late leaderboard, and the
  gap-filled department trend. They include join-date working-day/headcount rules, stored derived metrics, competition
  ranking, daily rows without logs, and the required rounding.
- Startup index creation for unique employee codes, unique employee/date attendance records, attendance date ordering,
  and employee join-date/department filters. The explain endpoint uses these endpoint query or pipeline builders and
  requests `executionStats`.

## Requirements and setup

The stack is Python, FastAPI, Pydantic, PyMongo, and MongoDB. Python 3.11 or newer and MongoDB 6.0 or newer are
required. From PowerShell in the `candidate_kit` directory:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
$env:MONGO_URI = "mongodb://localhost:27017"
$env:MONGO_DB = "attendance_db"
```

Set these environment variables to your own MongoDB connection and database. Alternatively, the app loads them from a
local `.env`; do not include `.env` or credentials in the submission.

To load the provided sample records and start the API:

```powershell
python sample_seed.py
uvicorn app.main:app --port 8000
```

**Warning:** `sample_seed.py` deletes and replaces documents in both configured collections. Use only a disposable
development database. When the API is running, its interactive documentation is at <http://localhost:8000/docs>.

## Tests

Run the regression tests with:

```powershell
python -m unittest discover -s tests -v
```

Latest run in this workspace: 22 tests passed and one failed in
`test_regularization_handles_legacy_history_and_tracks_only_changes`. Its test double keeps the persisted history time
as a Python `datetime`, while the API response serializer returns the contract-required epoch-millisecond integer; the
assertion compares those two representations directly.

## Project files

- `app/main.py` — API routes, validation, attendance calculations, aggregation pipelines, and startup indexes.
- `requirements.txt` — Python dependencies.
- `openapi.yaml` — API contract and business rules R1–R10.
- `DATA_MODEL.md`, `sample_data/`, and `sample_seed.py` — MongoDB schema, examples, and sample loader.
- `REVIEW.md` — review of defects in the original starter and the implemented corrections.
- `DECISIONS.md` — answers to the five design questions in the assignment.

The regression tests exercise application logic without a live MongoDB server. I have not independently verified the
aggregation pipelines or explain plans against MongoDB in this workspace.
