"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
import os
import json
from datetime import date, datetime, timedelta, timezone
from typing import Optional, Literal
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError
from contextlib import asynccontextmanager
from decimal import Decimal, ROUND_HALF_UP
from bson import json_util
from bson.decimal128 import Decimal128


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

    # Support date-filtered attendance queries and their requested sort.
    db.attendance_logs.create_index(
        [("date", -1), ("emp_code", 1)],
    )

    # Support department headcount and joined-date analytics.
    db.employees.create_index([("joined_on", 1)])
    db.employees.create_index([("department", 1), ("joined_on", 1)])

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


def parse_month(month: str):
    try:
        parsed = datetime.strptime(month, "%Y-%m")
        if parsed.strftime("%Y-%m") != month:
            raise ValueError
    except ValueError:
        raise HTTPException(422, "month must use YYYY-MM format")

    try:
        next_month = (
            parsed.replace(year=parsed.year + 1, month=1)
            if parsed.month == 12
            else parsed.replace(month=parsed.month + 1)
        )
    except ValueError:
        raise HTTPException(422, "month is out of range")
    return (
        parsed,
        month + "-01",
        next_month.strftime("%Y-%m") + "-01",
        (next_month - timedelta(days=1)).strftime("%Y-%m-%d"),
    )


def parse_calendar_date(value: str, field_name: str) -> date:
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d").date()
        if parsed.isoformat() != value:
            raise ValueError
        return parsed
    except ValueError:
        raise HTTPException(
            422, f"{field_name} must use YYYY-MM-DD format"
        )


def build_attendance_query(
    emp_code: Optional[str],
    date_from: Optional[str],
    date_to: Optional[str],
    status: Optional[str],
):
    # Validate dates and require YYYY-MM-DD format.
    parsed_from = (
        parse_calendar_date(date_from, "date_from")
        if date_from is not None else None
    )
    parsed_to = (
        parse_calendar_date(date_to, "date_to")
        if date_to is not None else None
    )
    if parsed_from and parsed_to and parsed_from > parsed_to:
        raise HTTPException(
            422, "date_from must be on or before date_to"
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
    return query


def round_half_up(value, places: int) -> float:
    quantum = Decimal(1).scaleb(-places)
    return float(
        Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP)
    )


def build_employee_monthly_pipeline(
    emp_code: str,
    month_start: str,
    next_month_start: str,
):
    return [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": month_start, "$lt": next_month_start},
            }
        },
        {
            "$set": {
                "_attendance_day": {
                    "$dateFromString": {"dateString": "$date"}
                }
            }
        },
        {
            "$set": {
                "_is_weekday": {
                    "$and": [
                        {
                            "$gte": [
                                {"$dayOfWeek": "$_attendance_day"},
                                2,
                            ]
                        },
                        {
                            "$lte": [
                                {"$dayOfWeek": "$_attendance_day"},
                                6,
                            ]
                        },
                    ]
                }
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {
                                        "$in": [
                                            "$status",
                                            ["PRESENT", "WFH", "ON_DUTY"],
                                        ]
                                    },
                                    "$_is_weekday",
                                ]
                            },
                            {"$cond": ["$half_day", 0.5, 1]},
                            0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {
                        "$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]
                    }
                },
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"},
            }
        },
    ]


def build_department_summary_pipeline(
    month_start: str,
    next_month_start: str,
    month_end: str,
    department: Optional[str],
):
    employee_match = {"joined_on": {"$lte": month_end}}
    if department is not None:
        employee_match["department"] = department

    zero_stats = {
        "present_days": 0,
        "work_hours_sum": 0,
        "work_hours_count": 0,
        "late_count": 0,
        "total_late_minutes": 0,
        "leave_count": 0,
        "on_duty_count": 0,
    }
    return [
        {"$match": employee_match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"employee_code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$employee_code"]},
                                    {"$gte": ["$date", month_start]},
                                    {"$lt": ["$date", next_month_start]},
                                ]
                            }
                        }
                    },
                    {
                        "$set": {
                            "_attendance_day": {
                                "$dateFromString": {"dateString": "$date"}
                            }
                        }
                    },
                    {
                        "$set": {
                            "_is_weekday": {
                                "$and": [
                                    {
                                        "$gte": [
                                            {"$dayOfWeek": "$_attendance_day"},
                                            2,
                                        ]
                                    },
                                    {
                                        "$lte": [
                                            {"$dayOfWeek": "$_attendance_day"},
                                            6,
                                        ]
                                    },
                                ]
                            }
                        }
                    },
                    {
                        "$group": {
                            "_id": None,
                            "present_days": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {
                                                    "$in": [
                                                        "$status",
                                                        [
                                                            "PRESENT",
                                                            "WFH",
                                                            "ON_DUTY",
                                                        ],
                                                    ]
                                                },
                                                "$_is_weekday",
                                            ]
                                        },
                                        {"$cond": ["$half_day", 0.5, 1]},
                                        0,
                                    ]
                                }
                            },
                            "work_hours_sum": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {
                                                    "$in": [
                                                        "$status",
                                                        [
                                                            "PRESENT",
                                                            "WFH",
                                                            "ON_DUTY",
                                                        ],
                                                    ]
                                                },
                                                {"$ne": ["$work_hours", None]},
                                            ]
                                        },
                                        "$work_hours",
                                        0,
                                    ]
                                }
                            },
                            "work_hours_count": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {
                                                    "$in": [
                                                        "$status",
                                                        [
                                                            "PRESENT",
                                                            "WFH",
                                                            "ON_DUTY",
                                                        ],
                                                    ]
                                                },
                                                {"$ne": ["$work_hours", None]},
                                            ]
                                        },
                                        1,
                                        0,
                                    ]
                                }
                            },
                            "late_count": {
                                "$sum": {
                                    "$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]
                                }
                            },
                            "total_late_minutes": {"$sum": "$late_minutes"},
                            "leave_count": {
                                "$sum": {
                                    "$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]
                                }
                            },
                            "on_duty_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$eq": ["$status", "ON_DUTY"]},
                                        1,
                                        0,
                                    ]
                                }
                            },
                        }
                    },
                ],
                "as": "monthly_stats",
            }
        },
        {
            "$set": {
                "_stats": {
                    "$ifNull": [
                        {"$arrayElemAt": ["$monthly_stats", 0]},
                        zero_stats,
                    ]
                }
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": "$_stats.present_days"},
                "work_hours_sum": {"$sum": "$_stats.work_hours_sum"},
                "work_hours_count": {"$sum": "$_stats.work_hours_count"},
                "late_count": {"$sum": "$_stats.late_count"},
                "total_late_minutes": {"$sum": "$_stats.total_late_minutes"},
                "leave_count": {"$sum": "$_stats.leave_count"},
                "on_duty_count": {"$sum": "$_stats.on_duty_count"},
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "present_days": 1,
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": ["$work_hours_count", 0]},
                        {
                            "$divide": [
                                "$work_hours_sum",
                                "$work_hours_count",
                            ]
                        },
                        None,
                    ]
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
        {"$sort": {"department": 1}},
    ]


def build_late_leaderboard_pipeline(
    month_start: str,
    next_month_start: str,
    department: Optional[str],
    limit: int,
):
    stages = [
        {
            "$match": {
                "date": {"$gte": month_start, "$lt": next_month_start}
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {
                    "$sum": {
                        "$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]
                    }
                },
            }
        },
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "employee",
            }
        },
        {"$unwind": "$employee"},
    ]
    if department is not None:
        stages.append({"$match": {"employee.department": department}})
    stages.extend(
        [
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": -1},
                    "output": {"rank": {"$rank": {}}},
                }
            },
            {"$match": {"rank": {"$lte": limit}}},
            {"$sort": {"total_late_minutes": -1, "employee.emp_code": 1}},
            {
                "$project": {
                    "_id": 0,
                    "rank": 1,
                    "emp_code": "$employee.emp_code",
                    "name": "$employee.name",
                    "department": "$employee.department",
                    "total_late_minutes": 1,
                    "late_count": 1,
                }
            },
        ]
    )
    return stages


def build_department_trend_pipeline(
    department: str,
    from_date: date,
    to_date: date,
):
    start = datetime.combine(from_date, datetime.min.time())
    try:
        end_exclusive = datetime.combine(
            to_date + timedelta(days=1), datetime.min.time()
        )
    except OverflowError:
        raise HTTPException(422, "to date is out of range")
    from_string = from_date.isoformat()
    to_string = to_date.isoformat()
    return [
        {"$match": {"department": department}},
        {"$limit": 1},
        {
            "$project": {
                "_id": 0,
                "date": {"$dateFromString": {"dateString": from_string}},
                "present_count": {"$literal": 0},
                "late_count": {"$literal": 0},
            }
        },
        {
            "$unionWith": {
                "coll": "attendance_logs",
                "pipeline": [
                    {
                        "$match": {
                            "date": {"$gte": from_string, "$lte": to_string}
                        }
                    },
                    {
                        "$lookup": {
                            "from": "employees",
                            "localField": "emp_code",
                            "foreignField": "emp_code",
                            "as": "employee",
                        }
                    },
                    {"$match": {"employee.department": department}},
                    {
                        "$group": {
                            "_id": {
                                "$dateFromString": {"dateString": "$date"}
                            },
                            "present_count": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$in": [
                                                "$status",
                                                ["PRESENT", "WFH", "ON_DUTY"],
                                            ]
                                        },
                                        {"$cond": ["$half_day", 0.5, 1]},
                                        0,
                                    ]
                                }
                            },
                            "late_count": {
                                "$sum": {
                                    "$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]
                                }
                            },
                        }
                    },
                    {
                        "$project": {
                            "_id": 0,
                            "date": "$_id",
                            "present_count": 1,
                            "late_count": 1,
                        }
                    },
                ],
            }
        },
        {
            "$group": {
                "_id": "$date",
                "present_count": {"$sum": "$present_count"},
                "late_count": {"$sum": "$late_count"},
            }
        },
        {"$project": {"_id": 0, "date": "$_id", "present_count": 1, "late_count": 1}},
        {
            "$densify": {
                "field": "date",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [start, end_exclusive],
                },
            }
        },
        {
            "$fill": {
                "sortBy": {"date": 1},
                "output": {
                    "present_count": {"value": 0},
                    "late_count": {"value": 0},
                },
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {
                    "attendance_day": {
                        "$dateToString": {"format": "%Y-%m-%d", "date": "$date"}
                    }
                },
                "pipeline": [
                    {"$match": {"department": department}},
                    {
                        "$match": {
                            "$expr": {
                                "$lte": ["$joined_on", "$$attendance_day"]
                            }
                        }
                    },
                    {"$count": "headcount"},
                ],
                "as": "_headcount",
            }
        },
        {
            "$set": {
                "headcount": {
                    "$ifNull": [
                        {"$arrayElemAt": ["$_headcount.headcount", 0]},
                        0,
                    ]
                },
                "is_working_day": {
                    "$and": [
                        {"$gte": [{"$dayOfWeek": "$date"}, 2]},
                        {"$lte": [{"$dayOfWeek": "$date"}, 6]},
                    ]
                },
            }
        },
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_working_day",
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {
                            "$divide": [
                                {
                                    "$floor": {
                                        "$add": [
                                            {
                                                "$multiply": [
                                                    {
                                                        "$divide": [
                                                            {
                                                                "$toDecimal": "$present_count"
                                                            },
                                                            {
                                                                "$toDecimal": "$headcount"
                                                            },
                                                        ]
                                                    },
                                                    Decimal128("10000"),
                                                ]
                                            },
                                            Decimal128("0.5"),
                                        ]
                                    }
                                },
                                Decimal128("10000"),
                            ]
                        },
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, "current"]},
                    }
                },
            }
        },
        {"$sort": {"date": 1}},
        {
            "$project": {
                "_id": 0,
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date"}},
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1,
            }
        },
    ]


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


class RegularizeIn(BaseModel):
    status: Optional[Literal[
        "PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"
    ]] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str
    regularized_by: str

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
    query = build_attendance_query(emp_code, date_from, date_to, status)

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



@app.patch("/attendance/{emp_code}/{date}", status_code=200)
def regularize_attendance(
    emp_code: str,
    date: str,
    body: RegularizeIn,
):
    # Validate the attendance date.
    try:
        parsed_date = datetime.strptime(date, "%Y-%m-%d").date()
        if parsed_date.isoformat() != date:
            raise ValueError
    except ValueError:
        raise HTTPException(422, "date must use YYYY-MM-DD format")

    # Validate the audit information.
    if not 5 <= len(body.reason) <= 200:
        raise HTTPException(422, "reason must contain 5 to 200 characters")
    if not 1 <= len(body.regularized_by) <= 50:
        raise HTTPException(422, "regularized_by must contain 1 to 50 characters")

    # The employee and attendance record must both exist.
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "Employee not found")

    record = db.attendance_logs.find_one(
        {"emp_code": emp_code, "date": date}
    )
    if record is None:
        raise HTTPException(404, "Attendance record not found")

    # Convert epoch milliseconds to IST, truncating to whole seconds.
    def parse_instant(value, field_name):
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int):
            raise HTTPException(
                422, f"{field_name} must be an epoch-millisecond integer"
            )
        if value < 100_000_000_000:
            raise HTTPException(
                422, f"{field_name} must be an epoch-millisecond integer"
            )
        try:
            instant = datetime.fromtimestamp(
                value / 1000, tz=timezone.utc
            ).astimezone(IST)
        except (ValueError, OverflowError, OSError):
            raise HTTPException(422, f"Invalid {field_name} timestamp")
        return instant.replace(microsecond=0)

    old_in = record.get("punch_in")
    old_out = record.get("punch_out")

    # MongoDB stores naive UTC datetimes.
    old_in_ist = (
        old_in.replace(tzinfo=timezone.utc).astimezone(IST)
        if old_in is not None else None
    )
    old_out_ist = (
        old_out.replace(tzinfo=timezone.utc).astimezone(IST)
        if old_out is not None else None
    )

    new_status = body.status if body.status is not None else record["status"]
    new_in = (
        parse_instant(body.punch_in, "punch_in")
        if body.punch_in is not None else old_in_ist
    )
    new_out = (
        parse_instant(body.punch_out, "punch_out")
        if body.punch_out is not None else old_out_ist
    )

    # ABSENT and LEAVE cannot have punch times.
    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(
                422, "ABSENT and LEAVE cannot include punch times"
            )
        new_in = None
        new_out = None

    # Presence statuses require a punch-in.
    elif new_in is None:
        raise HTTPException(
            422, "Presence statuses require a punch_in"
        )

    # A corrected punch-in must belong to this attendance date.
    if new_in is not None:
        start_time = datetime.strptime(
            emp["shift_start"], "%H:%M"
        ).time()
        end_time = datetime.strptime(
            emp["shift_end"], "%H:%M"
        ).time()

        attendance_day = new_in.date()
        if end_time <= start_time and new_in.time() < end_time:
            attendance_day -= timedelta(days=1)

        if attendance_day.isoformat() != date:
            raise HTTPException(
                422, "punch_in does not belong to this attendance date"
            )

    # A punch-out must be after punch-in and no more than 24 hours later.
    if new_out is not None:
        if new_in is None:
            raise HTTPException(422, "punch_out requires punch_in")
        if new_out <= new_in:
            raise HTTPException(422, "punch_out must be after punch_in")
        if new_out - new_in > timedelta(hours=24):
            raise HTTPException(
                422, "punch_out cannot be more than 24 hours after punch_in"
            )

    # Convert instants to MongoDB's naive UTC format.
    new_in_utc = (
        new_in.astimezone(timezone.utc).replace(tzinfo=None)
        if new_in is not None else None
    )
    new_out_utc = (
        new_out.astimezone(timezone.utc).replace(tzinfo=None)
        if new_out is not None else None
    )

    # Recompute all derived fields from the final status and times.
    if new_status in ("ABSENT", "LEAVE"):
        new_late = 0
        new_work = None
        new_overtime = 0
        new_half_day = False
    else:
        new_late = compute_late_minutes(
            new_in, emp["shift_start"], date
        )
        if new_out is None:
            new_work = None
            new_overtime = 0
            new_half_day = False
        else:
            new_work = compute_work_hours(new_in, new_out)
            new_overtime = compute_overtime(
                new_out,
                emp["shift_start"],
                emp["shift_end"],
                date,
            )
            new_half_day = new_work < 4.50

    # Build audit changes only for fields whose values actually changed.
    def epoch_ms(value):
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return int(value.timestamp() * 1000)

    old_values = {
        "status": record["status"],
        "punch_in": epoch_ms(old_in),
        "punch_out": epoch_ms(old_out),
        "work_hours": record.get("work_hours"),
        "late_minutes": record.get("late_minutes", 0),
        "overtime_minutes": record.get("overtime_minutes", 0),
        "half_day": record.get("half_day", False),
    }
    new_values = {
        "status": new_status,
        "punch_in": epoch_ms(new_in_utc),
        "punch_out": epoch_ms(new_out_utc),
        "work_hours": new_work,
        "late_minutes": new_late,
        "overtime_minutes": new_overtime,
        "half_day": new_half_day,
    }

    changes = {
        key: {"from": old_values[key], "to": new_values[key]}
        for key in old_values
        if old_values[key] != new_values[key]
    }

    if not changes:
        raise HTTPException(422, "The request does not change the record")

    history_entry = {
        "at": int(datetime.now(timezone.utc).timestamp() * 1000),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # Compare-and-update prevents concurrent edits from losing history.
    result = db.attendance_logs.update_one(
        {
            "_id": record["_id"],
            "history": record.get("history", []),
            "status": record["status"],
            "punch_in": old_in,
            "punch_out": old_out,
        },
        {
            "$set": {
                "status": new_status,
                "punch_in": new_in_utc,
                "punch_out": new_out_utc,
                "work_hours": new_work,
                "late_minutes": new_late,
                "overtime_minutes": new_overtime,
                "half_day": new_half_day,
            },
            "$push": {"history": history_entry},
        },
    )

    if result.modified_count != 1:
        raise HTTPException(
            409, "Attendance changed concurrently; reload and retry"
        )

    updated = db.attendance_logs.find_one(
        {"_id": record["_id"]}, {"_id": 0}
    )
    return serialize_mongo_datetimes(updated)



@app.get("/analytics/departments/summary")
def department_summary(
    month: str,
    department: Optional[str] = None,
):
    """
    Department-level attendance summary for a calendar month.
    Aggregates employee and attendance data in MongoDB.
    """

    # Validate YYYY-MM strictly.
    try:
        parsed_month = datetime.strptime(month, "%Y-%m")
        if parsed_month.strftime("%Y-%m") != month:
            raise ValueError
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="month must use YYYY-MM format",
        )

    month_start = month + "-01"

    # Calculate the last calendar day of the month.
    if parsed_month.month == 12:
        next_month = parsed_month.replace(
            year=parsed_month.year + 1,
            month=1,
        )
    else:
        next_month = parsed_month.replace(
            month=parsed_month.month + 1,
        )

    next_month_start = next_month.strftime("%Y-%m") + "-01"
    month_end = (
        next_month - timedelta(days=1)
    ).strftime("%Y-%m-%d")

    employee_match = {
        "joined_on": {"$lte": month_end}
    }

    if department is not None:
        employee_match["department"] = department

    pipeline = [
        # Employees who joined by the end of the month.
        {"$match": employee_match},

        # Aggregate each employee's logs for this month.
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"employee_code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {
                                        "$eq": [
                                            "$emp_code",
                                            "$$employee_code",
                                        ]
                                    },
                                    {
                                        "$gte": [
                                            "$date",
                                            month_start,
                                        ]
                                    },
                                    {
                                        "$lt": [
                                            "$date",
                                            next_month_start,
                                        ]
                                    },
                                ]
                            }
                        }
                    },
                    {
                        "$set": {
                            "_attendance_day": {
                                "$dateFromString": {
                                    "dateString": "$date"
                                }
                            }
                        }
                    },
                    {
                        "$set": {
                            "_is_weekday": {
                                "$and": [
                                    {
                                        "$gte": [
                                            {
                                                "$dayOfWeek":
                                                    "$_attendance_day"
                                            },
                                            2,
                                        ]
                                    },
                                    {
                                        "$lte": [
                                            {
                                                "$dayOfWeek":
                                                    "$_attendance_day"
                                            },
                                            6,
                                        ]
                                    },
                                ]
                            }
                        }
                    },
                    {
                        "$group": {
                            "_id": None,

                            "present_days": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {
                                                    "$in": [
                                                        "$status",
                                                        [
                                                            "PRESENT",
                                                            "WFH",
                                                            "ON_DUTY",
                                                        ],
                                                    ]
                                                },
                                                "$_is_weekday",
                                            ]
                                        },
                                        {
                                            "$cond": [
                                                "$half_day",
                                                0.5,
                                                1,
                                            ]
                                        },
                                        0,
                                    ]
                                }
                            },

                            "work_hours_sum": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {
                                                    "$in": [
                                                        "$status",
                                                        [
                                                            "PRESENT",
                                                            "WFH",
                                                            "ON_DUTY",
                                                        ],
                                                    ]
                                                },
                                                {
                                                    "$ne": [
                                                        "$work_hours",
                                                        None,
                                                    ]
                                                },
                                            ]
                                        },
                                        "$work_hours",
                                        0,
                                    ]
                                }
                            },

                            "work_hours_count": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {
                                                    "$in": [
                                                        "$status",
                                                        [
                                                            "PRESENT",
                                                            "WFH",
                                                            "ON_DUTY",
                                                        ],
                                                    ]
                                                },
                                                {
                                                    "$ne": [
                                                        "$work_hours",
                                                        None,
                                                    ]
                                                },
                                            ]
                                        },
                                        1,
                                        0,
                                    ]
                                }
                            },

                            "late_count": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$gt": [
                                                "$late_minutes",
                                                0,
                                            ]
                                        },
                                        1,
                                        0,
                                    ]
                                }
                            },

                            "total_late_minutes": {
                                "$sum": "$late_minutes"
                            },

                            "leave_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$eq": ["$status", "LEAVE"]},
                                        1,
                                        0,
                                    ]
                                }
                            },

                            "on_duty_count": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$eq": [
                                                "$status",
                                                "ON_DUTY",
                                            ]
                                        },
                                        1,
                                        0,
                                    ]
                                }
                            },
                        }
                    },
                ],
                "as": "monthly_stats",
            }
        },

        # Employees without logs receive zero-valued metrics.
        {
            "$set": {
                "_stats": {
                    "$ifNull": [
                        {"$arrayElemAt": ["$monthly_stats", 0]},
                        {
                            "present_days": 0,
                            "work_hours_sum": 0,
                            "work_hours_count": 0,
                            "late_count": 0,
                            "total_late_minutes": 0,
                            "leave_count": 0,
                            "on_duty_count": 0,
                        },
                    ]
                }
            }
        },

        # Combine employees into department totals.
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {
                    "$sum": "$_stats.present_days"
                },
                "work_hours_sum": {
                    "$sum": "$_stats.work_hours_sum"
                },
                "work_hours_count": {
                    "$sum": "$_stats.work_hours_count"
                },
                "late_count": {
                    "$sum": "$_stats.late_count"
                },
                "total_late_minutes": {
                    "$sum": "$_stats.total_late_minutes"
                },
                "leave_count": {
                    "$sum": "$_stats.leave_count"
                },
                "on_duty_count": {
                    "$sum": "$_stats.on_duty_count"
                },
            }
        },

        # Average over qualifying attendance records, not employees.
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "present_days": 1,
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": ["$work_hours_count", 0]},
                        {
                            "$divide": [
                                "$work_hours_sum",
                                "$work_hours_count",
                            ]
                        },
                        None,
                    ]
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },

        {"$sort": {"department": 1}},
    ]

    items = list(db.employees.aggregate(pipeline))

    # MongoDB's $round uses ties-to-even; apply half-up rounding
    # to reported numeric values in Python.
    for item in items:
        item["present_days"] = float(
            Decimal(str(item["present_days"])).quantize(
                Decimal("0.01"),
                rounding=ROUND_HALF_UP,
            )
        )

        if item["avg_work_hours"] is not None:
            item["avg_work_hours"] = float(
                Decimal(str(item["avg_work_hours"])).quantize(
                    Decimal("0.01"),
                    rounding=ROUND_HALF_UP,
                )
            )

    return {
        "month": month,
        "items": items,
    }


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    parsed_month, month_start, next_month_start, month_end = parse_month(month)
    employee = db.employees.find_one(
        {"emp_code": emp_code}, {"_id": 0, "joined_on": 1}
    )
    if employee is None:
        raise HTTPException(404, "Employee not found")

    rows = list(
        db.attendance_logs.aggregate(
            build_employee_monthly_pipeline(
                emp_code, month_start, next_month_start
            )
        )
    )
    stats = rows[0] if rows else {
        "present_days": 0,
        "leave_days": 0,
        "late_count": 0,
        "total_late_minutes": 0,
        "total_overtime_minutes": 0,
    }

    first_day = max(
        datetime.strptime(employee["joined_on"], "%Y-%m-%d").date(),
        parsed_month.date(),
    )
    last_day = datetime.strptime(month_end, "%Y-%m-%d").date()
    working_days = (
        sum(
            1
            for offset in range((last_day - first_day).days + 1)
            if (first_day + timedelta(days=offset)).weekday() < 5
        )
        if first_day <= last_day else 0
    )

    present_days = round_half_up(stats["present_days"], 2)
    attendance_pct = (
        round_half_up(
            Decimal(str(present_days)) * Decimal(100) / working_days,
            2,
        )
        if working_days else None
    )
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": stats["leave_days"],
        "late_count": stats["late_count"],
        "total_late_minutes": stats["total_late_minutes"],
        "total_overtime_minutes": stats["total_overtime_minutes"],
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str,
    limit: int = Query(default=10, ge=1, le=50),
    department: Optional[str] = None,
):
    _, month_start, next_month_start, _ = parse_month(month)
    items = list(
        db.attendance_logs.aggregate(
            build_late_leaderboard_pipeline(
                month_start, next_month_start, department, limit
            )
        )
    )
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(alias="from"),
    to_date: str = Query(alias="to"),
):
    from_day = parse_calendar_date(from_date, "from")
    to_day = parse_calendar_date(to_date, "to")
    if to_day < from_day:
        raise HTTPException(422, "to must be on or after from")
    if (to_day - from_day).days + 1 > 92:
        raise HTTPException(422, "date range cannot exceed 92 days")
    if db.employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(404, "Department not found")

    pipeline = build_department_trend_pipeline(
        department, from_day, to_day
    )
    items = list(db.employees.aggregate(pipeline))
    for item in items:
        if item["attendance_rate"] is not None:
            item["attendance_rate"] = round_half_up(
                item["attendance_rate"], 4
            )
        if item["moving_avg_7d"] is not None:
            item["moving_avg_7d"] = round_half_up(
                item["moving_avg_7d"], 4
            )
    return {"department": department, "items": items}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal[
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    ],
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(default=10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[
        Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]
    ] = None,
    from_date: Optional[str] = Query(default=None, alias="from"),
    to_date: Optional[str] = Query(default=None, alias="to"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        collection = "attendance_logs"
        command = {
            "find": collection,
            "filter": build_attendance_query(
                emp_code, date_from, date_to, status
            ),
            "projection": {"_id": 0},
            "sort": {"date": -1, "emp_code": 1},
            "skip": (page - 1) * page_size,
            "limit": page_size,
        }
    elif endpoint in ("employee_monthly", "department_summary", "late_leaderboard"):
        if month is None:
            raise HTTPException(422, "month is required")
        _, month_start, next_month_start, month_end = parse_month(month)
        if endpoint == "employee_monthly":
            if emp_code is None:
                raise HTTPException(422, "emp_code is required")
            collection = "attendance_logs"
            pipeline = build_employee_monthly_pipeline(
                emp_code, month_start, next_month_start
            )
        elif endpoint == "department_summary":
            collection = "employees"
            pipeline = build_department_summary_pipeline(
                month_start, next_month_start, month_end, department
            )
        else:
            collection = "attendance_logs"
            pipeline = build_late_leaderboard_pipeline(
                month_start, next_month_start, department, limit
            )
        command = {
            "aggregate": collection,
            "pipeline": pipeline,
            "cursor": {},
        }
    else:
        if from_date is None or to_date is None:
            raise HTTPException(422, "from and to are required")
        if department is None:
            raise HTTPException(422, "department is required")
        from_day = parse_calendar_date(from_date, "from")
        to_day = parse_calendar_date(to_date, "to")
        if to_day < from_day:
            raise HTTPException(422, "to must be on or after from")
        if (to_day - from_day).days + 1 > 92:
            raise HTTPException(422, "date range cannot exceed 92 days")
        if db.employees.find_one(
            {"department": department},
            {"_id": 1},
        ) is None:
            raise HTTPException(404, "Department not found")
        collection = "employees"
        command = {
            "aggregate": collection,
            "pipeline": build_department_trend_pipeline(
                department, from_day, to_day
            ),
            "cursor": {},
        }

    explain = db.command(
        "explain", command, verbosity="executionStats"
    )
    return {
        "endpoint": endpoint,
        "collection": collection,
        "explain": json.loads(json_util.dumps(explain)),
    }


# --------------------------------------------------------------------------- #
# TODO - the rest of the contract (see openapi.yaml):
#   POST  /attendance/punch-out done
#   PATCH /attendance/{emp_code}/{date} done
#   GET   /analytics/employees/{emp_code}/monthly done
#   GET   /analytics/departments/summary done
#   GET   /analytics/leaderboard/late done
#   GET   /analytics/departments/{department}/trend done
#   GET   /admin/explain/{endpoint} done
# --------------------------------------------------------------------------- #
