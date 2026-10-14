"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
import os
from datetime import datetime, timedelta, timezone
from typing import Optional, Literal
from datetime import timedelta
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError
from contextlib import asynccontextmanager
from decimal import Decimal, ROUND_HALF_UP


load_dotenv()

client = MongoClient(os.getenv("MONGO_URI"))
db = client[os.getenv("MONGO_DB")]

IST = timezone(timedelta(hours=5, minutes=30))


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Create database indexes before accepting requests.

    # 1. Ensure employee codes are unique
    db.employees.create_index(
        [("emp_code", 1)],
        unique=True,
    )

    # 2. Ensure one attendance record per employee per date
    db.attendance_logs.create_index(
        [("emp_code", 1), ("date", 1)],
        unique=True,
    )

    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #



def serialize_mongo_datetimes(value):
    """Convert MongoDB datetimes to epoch milliseconds recursively."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return int(value.timestamp() * 1000)

    if isinstance(value, dict):
        return {
            key: serialize_mongo_datetimes(item)
            for key, item in value.items()
        }

    if isinstance(value, list):
        return [serialize_mongo_datetimes(item) for item in value]

    return value



def compute_late_minutes(
    punch_in: datetime,
    shift_start: str,
    attendance_date: str,
) -> int:
    # Build the scheduled start time for this attendance date.
    start_time = datetime.strptime(shift_start, "%H:%M").time()
    shift_date = datetime.fromisoformat(attendance_date).date()

    scheduled_start = datetime.combine(
        shift_date,
        start_time,
        tzinfo=punch_in.tzinfo,
    )

    # Calculate seconds late, including seconds within a minute.
    elapsed_seconds = (punch_in - scheduled_start).total_seconds()

    # Grace period: exactly 10 minutes late is not considered late.
    if elapsed_seconds <= 600:
        return 0

    # Return whole minutes since scheduled shift start.
    return int(elapsed_seconds // 60)



def compute_work_hours(
    punch_in: datetime,
    punch_out: datetime,
) -> float:
    seconds = Decimal(
        str((punch_out - punch_in).total_seconds())
    )

    hours = (seconds / Decimal("3600")).quantize(
        Decimal("0.01"),
        rounding=ROUND_HALF_UP,
    )

    return float(hours)



def compute_overtime(
    punch_out: datetime,
    shift_start: str,
    shift_end: str,
    date_str: str,
) -> int:
    start_time = datetime.strptime(shift_start, "%H:%M").time()
    end_time = datetime.strptime(shift_end, "%H:%M").time()

    shift_date = datetime.fromisoformat(date_str).date()
    end_date = shift_date

    # An overnight shift ends on the following calendar day.
    if end_time <= start_time:
        end_date += timedelta(days=1)

    scheduled_end = datetime.combine(end_date, end_time, tzinfo=IST)
    minutes = int((punch_out - scheduled_end).total_seconds() // 60)

    # Overtime is counted only when it reaches 30 minutes.
    return minutes if minutes >= 30 else 0


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None


# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #

@app.get("/health")
def health():
    try:
        db.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(
            status_code=503,
            detail="MongoDB unavailable"
        )


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(timezone.utc)

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409,
            detail="emp_code already exists",
        )

    doc.pop("_id", None)
    # doc["_id"] = str(doc["_id"])

    # The API contract requires epoch milliseconds.
    doc["created_at"] = int(
        doc["created_at"].timestamp() * 1000
    )

    return doc




@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    q = {}

    if department is not None:
        q["department"] = department

    skip = (page - 1) * page_size

    total = db.employees.count_documents(q)

    items = list(
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", 1)
        .skip(skip)
        .limit(page_size)
    )

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }



@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    # 1. Validate that the employee exists
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="Employee not found")

    # 2. Convert the timestamp to IST
    if body.punched_at is not None:
        if (
            isinstance(body.punched_at, bool)
            or not isinstance(body.punched_at, int)
            or body.punched_at < 100_000_000_000
        ):
            raise HTTPException(
                status_code=422,
                detail="punched_at must be an epoch-millisecond integer",
            )

        try:
            ts = datetime.fromtimestamp(
                body.punched_at / 1000,
                tz=timezone.utc,
            ).astimezone(IST)
        except (ValueError, OverflowError, OSError):
            raise HTTPException(
                status_code=422,
                detail="Invalid punched_at timestamp",
            )
    else:
        ts = datetime.now(IST)

    # 3. Truncate to whole seconds
    ts = ts.replace(microsecond=0)
    attendance_day = ts.date()

    shift_start = datetime.strptime(
        emp["shift_start"], "%H:%M"
    ).time()

    shift_end = datetime.strptime(
        emp["shift_end"], "%H:%M"
    ).time()

    # If this is an overnight shift and punch-in is
    # before the shift's end time, use the previous date.
    if shift_end <= shift_start and ts.time() < shift_end:
        attendance_day -= timedelta(days=1)

    attendance_date = attendance_day.isoformat()

    # 4. Build the attendance record
    doc = {
        "emp_code": body.emp_code,
        "date": attendance_date,
        "status": body.status,
        "punch_in": ts.astimezone(timezone.utc).replace(tzinfo=None),
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp["shift_start"],attendance_date,),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    # 5. Insert atomically; the unique index prevents duplicate records
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409,
            detail="Already punched in for this date",
        )

    # 6. Prepare the API response
    doc.pop("_id", None)
    doc["punch_in"] = int(ts.timestamp() * 1000)
    doc["punch_out"] = None

    return doc


@app.post("/attendance/punch-out", status_code=200)
def punch_out(body: PunchOutIn):
    # 1. Verify the employee exists.
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="Employee not found")

    # 2. Convert the requested punch-out time to IST.
    if body.punched_at is not None:
        if (
            isinstance(body.punched_at, bool)
            or not isinstance(body.punched_at, int)
            or body.punched_at < 100_000_000_000
        ):
            raise HTTPException(
                status_code=422,
                detail="punched_at must be an epoch-millisecond integer",
            )

        try:
            ts = datetime.fromtimestamp(
                body.punched_at / 1000,
                tz=timezone.utc,
            ).astimezone(IST)
        except (ValueError, OverflowError, OSError):
            raise HTTPException(
                status_code=422,
                detail="Invalid punched_at timestamp",
            )
    else:
        ts = datetime.now(IST)

    # 3. Truncate to whole seconds.
    ts = ts.replace(microsecond=0)

    # MongoDB stores attendance datetimes as naive UTC.
    punch_out_utc = ts.astimezone(timezone.utc).replace(tzinfo=None)

    # 4. Find the most recent punch-in at or before this time.
    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$lte": punch_out_utc},
        },
        sort=[("punch_in", -1)],
    )

    if record is None:
        raise HTTPException(
            status_code=404,
            detail="No punch-in found before this time",
        )

    # 5. A closed record cannot be punched out again.
    if record.get("punch_out") is not None:
        raise HTTPException(
            status_code=409,
            detail="Attendance already punched out",
        )

    punch_in_utc = record["punch_in"].replace(tzinfo=timezone.utc)
    punch_in_ist = punch_in_utc.astimezone(IST)

    # 6. Validate the punch-out interval.
    elapsed = punch_out_utc - record["punch_in"]

    if elapsed.total_seconds() <= 0:
        raise HTTPException(
            status_code=422,
            detail="Punch-out must be after punch-in",
        )

    if elapsed > timedelta(hours=24):
        raise HTTPException(
            status_code=422,
            detail="Punch-out cannot be more than 24 hours after punch-in",
        )

    # 7. Compute the derived fields.
    work_hours = compute_work_hours(punch_in_ist, ts)

    overtime_minutes = compute_overtime(
        ts,
        emp["shift_start"],
        emp["shift_end"],
        record["date"],
    )

    half_day = work_hours < 4.50

    # 8. Atomically close the record.
    # Only one concurrent request can update this open record.
    result = db.attendance_logs.update_one(
        {
            "_id": record["_id"],
            "punch_out": None,
        },
        {
            "$set": {
                "punch_out": punch_out_utc,
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day,
            }
        },
    )

    if result.modified_count != 1:
        raise HTTPException(
            status_code=409,
            detail="Attendance was already punched out",
        )

    # 9. Return the updated record without MongoDB's internal _id.
    updated = db.attendance_logs.find_one({"_id": record["_id"]})
    updated.pop("_id", None)

    updated["punch_in"] = int(
        punch_in_utc.timestamp() * 1000
    )
    updated["punch_out"] = int(
        punch_out_utc.replace(tzinfo=timezone.utc).timestamp() * 1000
    )

    return updated


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[
        Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]
    ] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    # Validate dates and require YYYY-MM-DD format.
    parsed_from = None
    parsed_to = None

    try:
        if date_from is not None:
            parsed_from = datetime.strptime(date_from, "%Y-%m-%d").date()
            if parsed_from.isoformat() != date_from:
                raise ValueError("Invalid date format")

        if date_to is not None:
            parsed_to = datetime.strptime(date_to, "%Y-%m-%d").date()
            if parsed_to.isoformat() != date_to:
                raise ValueError("Invalid date format")
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="Dates must use YYYY-MM-DD format",
        )

    if parsed_from and parsed_to and parsed_from > parsed_to:
        raise HTTPException(
            status_code=422,
            detail="date_from must be on or before date_to",
        )

    # Build MongoDB filters.
    query = {}

    if emp_code is not None:
        query["emp_code"] = emp_code

    if parsed_from or parsed_to:
        date_filter = {}

        if parsed_from:
            date_filter["$gte"] = parsed_from.isoformat()

        if parsed_to:
            date_filter["$lte"] = parsed_to.isoformat()

        query["date"] = date_filter

    if status is not None:
        query["status"] = status

    # Count and paginate in MongoDB, not in Python.
    total = db.attendance_logs.count_documents(query)

    cursor = (
        db.attendance_logs.find(query, {"_id": 0})
        .sort([("date", -1), ("emp_code", 1)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )

    items = [
        serialize_mongo_datetimes(record)
        for record in cursor
    ]

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }

# --------------------------------------------------------------------------- #
# TODO - the rest of the contract (see openapi.yaml):
#   POST  /attendance/punch-out
#   PATCH /attendance/{emp_code}/{date}
#   GET   /analytics/employees/{emp_code}/monthly
#   GET   /analytics/departments/summary
#   GET   /analytics/leaderboard/late
#   GET   /analytics/departments/{department}/trend
#   GET   /admin/explain/{endpoint}
# --------------------------------------------------------------------------- #
