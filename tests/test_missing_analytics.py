import unittest
import asyncio
import json
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

from bson.decimal128 import Decimal128
from fastapi import HTTPException
from pymongo.errors import DuplicateKeyError
from pydantic import ValidationError

from app import main


class MissingAnalyticsTests(unittest.TestCase):
    def setUp(self):
        self.original_db = main.db

    def tearDown(self):
        main.db = self.original_db

    def asgi_post(self, path, body):
        messages = []
        payload = json.dumps(body).encode()
        request_sent = False

        async def receive():
            nonlocal request_sent
            if request_sent:
                return {"type": "http.disconnect"}
            request_sent = True
            return {
                "type": "http.request",
                "body": payload,
                "more_body": False,
            }

        async def send(message):
            messages.append(message)

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": [(b"content-type", b"application/json")],
            "client": ("test", 1234),
            "server": ("test", 80),
            "root_path": "",
        }
        asyncio.run(main.app(scope, receive, send))
        response = next(item for item in messages if item["type"] == "http.response.start")
        response_body = next(item for item in messages if item["type"] == "http.response.body")
        return response["status"], json.loads(response_body["body"])

    def test_employee_monthly_counts_workdays_from_join_date(self):
        captured = {}

        class Employees:
            def aggregate(self, pipeline):
                captured["pipeline"] = pipeline
                return [
                    {
                        "working_days": 15,
                        "present_days": 1.5,
                        "leave_days": 1,
                        "late_count": 2,
                        "total_late_minutes": 95,
                        "total_overtime_minutes": 30,
                    }
                ]

        main.db = SimpleNamespace(employees=Employees())

        result = main.employee_monthly("EMP0001", "2026-07")

        self.assertEqual(result["working_days"], 15)
        self.assertEqual(result["present_days"], 1.5)
        self.assertEqual(result["attendance_pct"], 10.0)
        self.assertEqual(result["total_late_minutes"], 95)
        lookup = next(
            stage["$lookup"]
            for stage in captured["pipeline"]
            if "$lookup" in stage
        )
        self.assertEqual(
            lookup["pipeline"][0]["$match"]["$expr"]["$and"][1:],
            [
                {"$gte": ["$date", "2026-07-01"]},
                {"$lt": ["$date", "2026-08-01"]},
            ],
        )
        self.assertTrue(
            any(
                "working_days" in stage.get("$set", {})
                for stage in captured["pipeline"]
            )
        )

    def test_employee_monthly_rate_uses_four_decimal_half_up(self):
        class Employees:
            def aggregate(self, pipeline):
                return [{
                    "working_days": 3,
                    "present_days": 0.5,
                    "leave_days": 0,
                    "late_count": 0,
                    "total_late_minutes": 0,
                    "total_overtime_minutes": 0,
                }]

        main.db = SimpleNamespace(employees=Employees())
        result = main.employee_monthly("EMP0001", "2026-07")
        self.assertEqual(result["working_days"], 3)
        self.assertEqual(result["attendance_pct"], 16.6667)

    def test_employee_monthly_handles_no_logs_and_unknown_employees(self):
        class Employees:
            def __init__(self, result):
                self.result = result

            def aggregate(self, pipeline):
                return self.result

        main.db = SimpleNamespace(employees=Employees([{
            "working_days": 22,
            "present_days": 0,
            "leave_days": 0,
            "late_count": 0,
            "total_late_minutes": 0,
            "total_overtime_minutes": 0,
        }]))
        result = main.employee_monthly("EMP0001", "2026-08")
        self.assertEqual(result["working_days"], 22)
        self.assertEqual(result["present_days"], 0)
        self.assertEqual(result["attendance_pct"], 0)

        main.db = SimpleNamespace(employees=Employees([]))
        with self.assertRaises(HTTPException) as error:
            main.employee_monthly("EMP9999", "2026-08")
        self.assertEqual(error.exception.status_code, 404)

    def test_month_parser_rejects_invalid_and_overflow_months(self):
        for month in ("2026-13", "2026-2", "9999-12"):
            with self.subTest(month=month):
                with self.assertRaises(HTTPException) as error:
                    main.parse_month(month)
                self.assertEqual(error.exception.status_code, 422)
        _, month_start, next_month, month_end = main.parse_month("2024-02")
        self.assertEqual(month_start, "2024-02-01")
        self.assertEqual(next_month, "2024-03-01")
        self.assertEqual(month_end, "2024-02-29")

    def test_employee_input_enforces_contract_fields_and_boundaries(self):
        valid = {
            "emp_code": "EMP0001",
            "name": "N" * 100,
            "email": "a@b.co",
            "department": "D" * 50,
            "joined_on": "2024-02-29",
        }
        employee = main.EmployeeIn.model_validate(valid)
        self.assertEqual(employee.shift_start, "09:30")
        self.assertEqual(employee.shift_end, "18:30")

        for field, value in (
            ("emp_code", "EMP123"),
            ("name", ""),
            ("name", "N" * 101),
            ("email", "not-an-email"),
            ("email", "a" * 117 + "@x.y"),
            ("department", ""),
            ("department", "D" * 51),
            ("joined_on", "2025-02-29"),
            ("joined_on", "2026-2-01"),
            ("shift_start", "24:00"),
            ("shift_end", "09:3"),
        ):
            with self.subTest(field=field, value=value):
                invalid = dict(valid)
                invalid[field] = value
                with self.assertRaises(ValidationError):
                    main.EmployeeIn.model_validate(invalid)

        with self.assertRaises(ValidationError):
            main.EmployeeIn.model_validate(
                {**valid, "shift_start": "08:00", "shift_end": "08:00"}
            )
        with self.assertRaises(ValidationError):
            main.EmployeeIn.model_validate(
                {key: value for key, value in valid.items() if key != "name"}
            )

    def test_http_body_validation_returns_422_before_database_access(self):
        employee_status, _ = self.asgi_post(
            "/employees",
            {
                "emp_code": "EMP12",
                "name": "Invalid",
                "email": "not-an-email",
                "department": "Engineering",
                "joined_on": "2026-02-30",
            },
        )
        punch_status, _ = self.asgi_post(
            "/attendance/punch-in",
            {"emp_code": "EMP0001", "punched_at": 1_783_312_500.5},
        )
        self.assertEqual(employee_status, 422)
        self.assertEqual(punch_status, 422)

    def test_employee_list_serializes_created_at_as_epoch_milliseconds(self):
        created_at = datetime(2026, 7, 14, 4, 0, tzinfo=timezone.utc)

        class Cursor:
            def sort(self, *args):
                return self

            def skip(self, *args):
                return self

            def limit(self, *args):
                return iter([{
                    "_id": "internal",
                    "emp_code": "EMP0001",
                    "name": "Asha Rao",
                    "email": "asha@example.com",
                    "department": "Engineering",
                    "shift_start": "09:30",
                    "shift_end": "18:30",
                    "joined_on": "2026-01-05",
                    "created_at": created_at,
                }])

        class Employees:
            def count_documents(self, query):
                return 1

            def find(self, query, projection):
                return Cursor()

        main.db = SimpleNamespace(employees=Employees())
        response = main.list_employees(page=1, page_size=20)
        employee = response["items"][0]
        self.assertEqual(
            employee["created_at"], int(created_at.timestamp() * 1000)
        )
        self.assertNotIn("_id", employee)

    def test_attendance_serializer_normalizes_legacy_fields_and_history(self):
        at = datetime(2026, 7, 15, 4, 30)
        record = main.serialize_attendance_record({
            "_id": "internal",
            "emp_code": "EMP0001",
            "date": "2026-07-14",
            "status": "PRESENT",
            "punch_in": None,
            "history": [{"at": at, "changes": {
                "punch_in": {"from": None, "to": at}
            }}],
        })
        self.assertNotIn("_id", record)
        self.assertIsNone(record["punch_out"])
        self.assertIsNone(record["work_hours"])
        self.assertEqual(record["late_minutes"], 0)
        self.assertEqual(record["overtime_minutes"], 0)
        self.assertFalse(record["half_day"])
        self.assertEqual(record["history"][0]["at"], int(at.replace(tzinfo=timezone.utc).timestamp() * 1000))
        self.assertEqual(
            record["history"][0]["changes"]["punch_in"]["to"],
            int(at.replace(tzinfo=timezone.utc).timestamp() * 1000),
        )

    def test_epoch_milliseconds_are_strict_and_bounded(self):
        for value in (100_000_000_000, 4_102_444_800_000):
            self.assertEqual(
                main.PunchInIn(emp_code="EMP0001", punched_at=value).punched_at,
                value,
            )

        for value in (
            99_999_999_999,
            4_102_444_800_001,
            1_783_312_500,
            1_783_312_500_000.0,
            "1783312500000",
            True,
            None,
        ):
            with self.subTest(value=value):
                with self.assertRaises(ValidationError):
                    main.PunchInIn(
                        emp_code="EMP0001", punched_at=value
                    )
        self.assertIsNone(main.PunchInIn(emp_code="EMP0001").punched_at)

        with self.assertRaises(ValidationError):
            main.PunchOutIn(emp_code="EMP0001", punched_at=None)
        with self.assertRaises(ValidationError):
            main.RegularizeIn(
                punch_in=None, reason="valid reason", regularized_by="admin"
            )
        with self.assertRaises(ValidationError):
            main.RegularizeIn(
                punch_out=4_102_444_800_001,
                reason="valid reason",
                regularized_by="admin",
            )
        with self.assertRaises(ValidationError):
            main.RegularizeIn(
                status=None, reason="valid reason", regularized_by="admin"
            )

    def test_late_and_work_hour_rounding_boundaries(self):
        shift_start = datetime(2026, 7, 6, 9, 30, tzinfo=main.IST)
        self.assertEqual(
            main.compute_late_minutes(
                shift_start + timedelta(minutes=10), "09:30", "2026-07-06"
            ),
            0,
        )
        self.assertEqual(
            main.compute_late_minutes(
                shift_start + timedelta(minutes=10, seconds=1),
                "09:30",
                "2026-07-06",
            ),
            10,
        )
        self.assertEqual(
            main.compute_late_minutes(
                shift_start + timedelta(minutes=11, seconds=59),
                "09:30",
                "2026-07-06",
            ),
            11,
        )
        start = datetime(2026, 7, 6, 9, 0, tzinfo=main.IST)
        self.assertEqual(
            main.compute_work_hours(
                start, start + timedelta(seconds=4 * 3600 + 29 * 60 + 41)
            ),
            4.49,
        )
        self.assertEqual(
            main.compute_work_hours(
                start, start + timedelta(seconds=4 * 3600 + 29 * 60 + 42)
            ),
            4.50,
        )
        self.assertTrue(
            main.compute_work_hours(
                start, start + timedelta(seconds=4 * 3600 + 29 * 60 + 41)
            ) < 4.50
        )
        self.assertFalse(
            main.compute_work_hours(
                start, start + timedelta(seconds=4 * 3600 + 29 * 60 + 42)
            ) < 4.50
        )

    def test_department_summary_uses_half_up_for_returned_means(self):
        class Employees:
            def aggregate(self, pipeline):
                return [{
                    "department": "Engineering",
                    "headcount": 1,
                    "present_days": 0.5,
                    "avg_work_hours": 9.125,
                    "late_count": 0,
                    "total_late_minutes": 0,
                    "leave_count": 0,
                    "on_duty_count": 0,
                }]

        main.db = SimpleNamespace(employees=Employees())
        response = main.department_summary("2026-07")
        self.assertEqual(response["items"][0]["avg_work_hours"], 9.13)
        self.assertEqual(response["items"][0]["present_days"], 0.5)

    def test_overtime_threshold_and_overnight_shift(self):
        shift_end = datetime(2026, 7, 6, 18, 30, tzinfo=main.IST)
        self.assertEqual(
            main.compute_overtime(
                shift_end + timedelta(minutes=29, seconds=59),
                "09:30", "18:30", "2026-07-06",
            ),
            0,
        )
        self.assertEqual(
            main.compute_overtime(
                shift_end + timedelta(minutes=30),
                "09:30", "18:30", "2026-07-06",
            ),
            30,
        )
        overnight_end = datetime(2026, 7, 7, 6, 0, tzinfo=main.IST)
        self.assertEqual(
            main.compute_overtime(
                overnight_end + timedelta(minutes=31),
                "22:00", "06:00", "2026-07-06",
            ),
            31,
        )

    def test_duplicate_punch_in_is_translated_to_conflict(self):
        class Employees:
            def find_one(self, query):
                return {
                    "emp_code": "EMP0001",
                    "shift_start": "09:30",
                    "shift_end": "18:30",
                }

        class Attendance:
            def insert_one(self, record):
                raise DuplicateKeyError("duplicate key")

        main.db = SimpleNamespace(
            employees=Employees(), attendance_logs=Attendance()
        )
        punched_at = int(
            datetime(2026, 7, 6, 9, 30, tzinfo=main.IST).timestamp() * 1000
        )
        with self.assertRaises(HTTPException) as error:
            main.punch_in(
                main.PunchInIn(emp_code="EMP0001", punched_at=punched_at)
            )
        self.assertEqual(error.exception.status_code, 409)

    def test_punch_out_uses_atomic_open_record_guard(self):
        record = {
            "_id": "record",
            "emp_code": "EMP0001",
            "date": "2026-07-06",
            "status": "PRESENT",
            "punch_in": datetime(2026, 7, 6, 4, 0),
            "punch_out": None,
            "history": [{"at": datetime(2026, 7, 7, 1, 0)}],
        }
        captured = {}

        class Employees:
            def find_one(self, query):
                return {
                    "emp_code": "EMP0001",
                    "shift_start": "09:30",
                    "shift_end": "18:30",
                }

        class Attendance:
            def find_one(self, query, sort=None):
                return deepcopy(record)

            def update_one(self, query, update):
                captured["query"] = query
                return SimpleNamespace(modified_count=0)

        main.db = SimpleNamespace(
            employees=Employees(), attendance_logs=Attendance()
        )
        punched_at = int(
            datetime(2026, 7, 6, 18, 30, tzinfo=main.IST).timestamp() * 1000
        )
        with self.assertRaises(HTTPException) as error:
            main.punch_out(
                main.PunchOutIn(emp_code="EMP0001", punched_at=punched_at)
            )
        self.assertEqual(error.exception.status_code, 409)
        self.assertEqual(captured["query"]["punch_out"], None)

    def test_late_leaderboard_ranks_before_applying_cutoff(self):
        pipeline = main.build_late_leaderboard_pipeline(
            "2026-07-01", "2026-08-01", "Engineering", 2
        )
        rank_index = next(
            index
            for index, stage in enumerate(pipeline)
            if "$setWindowFields" in stage
        )
        cutoff_index = next(
            index
            for index, stage in enumerate(pipeline)
            if stage.get("$match", {}).get("rank") == {"$lte": 2}
        )

        self.assertLess(rank_index, cutoff_index)
        self.assertEqual(
            pipeline[rank_index]["$setWindowFields"]["output"],
            {"rank": {"$rank": {}}},
        )

    def test_trend_pipeline_gap_fills_and_uses_seven_calendar_rows(self):
        pipeline = main.build_department_trend_pipeline(
            "Engineering", date(2026, 7, 1), date(2026, 7, 3)
        )

        densify = next(stage["$densify"] for stage in pipeline if "$densify" in stage)
        moving_average = next(
            stage["$setWindowFields"]["output"]["moving_avg_7d"]
            for stage in pipeline
            if "$setWindowFields" in stage
        )
        self.assertEqual(
            densify["range"]["bounds"],
            [datetime(2026, 7, 1), datetime(2026, 7, 4)],
        )
        self.assertEqual(moving_average["window"]["documents"], [-6, "current"])
        self.assertEqual(pipeline[-2], {"$sort": {"date": 1}})

    def test_department_summary_keeps_full_precision_until_half_up_rounding(self):
        pipeline = main.build_department_summary_pipeline(
            "2026-07-01", "2026-08-01", "2026-07-31", None
        )
        project = next(stage["$project"] for stage in pipeline if "$project" in stage)

        self.assertIn("$divide", project["avg_work_hours"]["$cond"][1])
        self.assertNotIn("$round", project["avg_work_hours"]["$cond"][1])
        self.assertEqual(main.round_half_up(9.125, 2), 9.13)

    def test_trend_rejects_ranges_over_92_days(self):
        with self.assertRaises(HTTPException) as error:
            main.department_trend(
                "Engineering", "2026-01-01", "2026-04-03"
            )
        self.assertEqual(error.exception.status_code, 422)

    def test_trend_rounds_rates_half_up_and_returns_aggregated_rows(self):
        class Employees:
            def find_one(self, query, projection):
                return {"_id": "department"}

            def aggregate(self, pipeline):
                return [
                    {
                        "date": "2026-07-01",
                        "is_working_day": True,
                        "headcount": 3,
                        "present_count": 1,
                        "late_count": 1,
                        "attendance_rate": Decimal128("0.3333"),
                        "moving_avg_7d": Decimal128("0.22225"),
                    }
                ]

        main.db = SimpleNamespace(employees=Employees())
        result = main.department_trend(
            "Engineering", "2026-07-01", "2026-07-01"
        )

        self.assertEqual(result["items"][0]["attendance_rate"], 0.3333)
        self.assertEqual(result["items"][0]["moving_avg_7d"], 0.2223)

    def test_explain_runs_execution_stats_for_monthly_pipeline(self):
        captured = {}

        class Database:
            def command(self, operation, command, verbosity):
                captured["command"] = command
                captured["verbosity"] = verbosity
                return {"queryPlanner": {"winningPlan": {"stage": "IXSCAN"}}}

        main.db = Database()
        result = main.explain_endpoint(
            "employee_monthly", emp_code="EMP0001", month="2026-07"
        )

        self.assertEqual(captured["verbosity"], "executionStats")
        self.assertEqual(captured["command"]["aggregate"], "employees")
        self.assertEqual(
            captured["command"]["pipeline"][0]["$match"],
            {"emp_code": "EMP0001"},
        )
        self.assertEqual(result["explain"]["queryPlanner"]["winningPlan"]["stage"], "IXSCAN")

    def test_explain_requires_endpoint_parameters(self):
        with self.assertRaises(HTTPException) as error:
            main.explain_endpoint("employee_monthly")
        self.assertEqual(error.exception.status_code, 422)

    def test_regularization_handles_legacy_history_and_tracks_only_changes(self):
        attendance_record = {
            "_id": "record",
            "emp_code": "EMP0001",
            "date": "2026-07-06",
            "status": "PRESENT",
            "punch_in": datetime(2026, 7, 6, 4, 0),
            "punch_out": None,
            "late_minutes": 0,
            "work_hours": None,
            "overtime_minutes": 0,
            "half_day": False,
        }

        class Employees:
            def find_one(self, query):
                return {
                    "emp_code": "EMP0001",
                    "shift_start": "09:30",
                    "shift_end": "18:30",
                }

        class Attendance:
            def find_one(self, query, projection=None):
                return deepcopy(attendance_record)

            def update_one(self, query, update):
                self.last_query = query
                if query.get("history") != {"$exists": False}:
                    return SimpleNamespace(modified_count=0)
                attendance_record.update(update["$set"])
                attendance_record.setdefault("history", []).append(
                    update["$push"]["history"]
                )
                return SimpleNamespace(modified_count=1)

        attendance = Attendance()
        main.db = SimpleNamespace(
            employees=Employees(), attendance_logs=attendance
        )
        response = main.regularize_attendance(
            "EMP0001",
            "2026-07-06",
            main.RegularizeIn(
                status="WFH",
                reason="corrected status",
                regularized_by="hr.admin",
            ),
        )

        self.assertEqual(len(response["history"]), 1)
        self.assertEqual(
            response["history"][0]["changes"],
            {"status": {"from": "PRESENT", "to": "WFH"}},
        )
        self.assertEqual(attendance_record["history"], response["history"])

    def test_regularization_rejects_noop_without_appending_history(self):
        attendance_record = {
            "_id": "record",
            "emp_code": "EMP0001",
            "date": "2026-07-06",
            "status": "PRESENT",
            "punch_in": datetime(2026, 7, 6, 4, 0),
            "punch_out": None,
            "work_hours": None,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False,
            "history": [],
        }

        class Employees:
            def find_one(self, query):
                return {"shift_start": "09:30", "shift_end": "18:30"}

        class Attendance:
            def find_one(self, query, projection=None):
                return deepcopy(attendance_record)

            def update_one(self, query, update):
                raise AssertionError("no-op must not write")

        main.db = SimpleNamespace(
            employees=Employees(), attendance_logs=Attendance()
        )
        with self.assertRaises(HTTPException) as error:
            main.regularize_attendance(
                "EMP0001",
                "2026-07-06",
                main.RegularizeIn(
                    reason="no actual change", regularized_by="hr.admin"
                ),
            )
        self.assertEqual(error.exception.status_code, 422)
        self.assertEqual(attendance_record["history"], [])


if __name__ == "__main__":
    unittest.main()
