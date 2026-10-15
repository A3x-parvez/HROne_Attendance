# Employee Attendance & Analytics API

This repository is my submission for the HROne Software Engineer Trainee assignment. It implements the attendance API
and analytics described in `openapi.yaml`, using FastAPI and MongoDB. The goal is to keep attendance records consistent,
apply the specified business rules, preserve a correction audit trail, and return the required reports from MongoDB.

## What the API solves

- Creates and lists employees using the customer-supplied `emp_code`.
- Records punch-in and punch-out, including supported statuses, duplicate protection, IST attendance dates, overnight
  shifts, and server-computed late, overtime, work-hour, and half-day values.
- Lists and filters attendance records, and supports manual corrections with an append-only history of changed fields.
- Provides employee monthly summaries, department monthly summaries, a late-arrival leaderboard, and a gap-filled daily
  department trend.
- Provides execution statistics for the supported endpoint queries through `/admin/explain/{endpoint}`.

Timestamps at the API boundary use epoch milliseconds. MongoDB stores instants as BSON dates and calendar dates as
`YYYY-MM-DD` strings. Analytics read stored derived attendance values, include employees with no logs in headcount, and
apply the assignment's weekday, rounding, tie-ranking, and date-range rules.

## Run locally

Use Python 3.11 or newer and MongoDB 6.0 or newer. Install dependencies and set `MONGO_URI` and `MONGO_DB` in the
environment before starting the service. Do not commit `.env` files or database credentials.

PowerShell example (replace the URI with your own local or Atlas connection string):

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
$env:MONGO_URI = "mongodb://localhost:27017"
$env:MONGO_DB = "attendance_db"
python sample_seed.py
uvicorn app.main:app --port 8000
```

For Bash, activate with `source .venv/bin/activate` and export the same two environment variables. The sample seed
replaces documents in the configured collections; use it only with a disposable development database.

Run the regression tests with:

```powershell
python -m unittest discover -s tests -v
```

## Files

- `app/main.py` — API routes, validation, attendance rules, MongoDB pipelines, indexes, and explain support.
- `openapi.yaml` — HTTP contract and rules R1–R10.
- `DATA_MODEL.md` and `sample_data/` — MongoDB schema and sample records.
- `REVIEW.md` — defects identified in the starter and the corresponding fixes.
- `DECISIONS.md` — implementation choices and their trade-offs.

The implementation was tested with the local regression suite. A live MongoDB integration run is still needed to verify
the pipelines and explain plans against a MongoDB 6+ server and a production-sized dataset.
