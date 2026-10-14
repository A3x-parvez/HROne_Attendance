import unittest
from datetime import date, datetime
from types import SimpleNamespace

from bson.decimal128 import Decimal128
from fastapi import HTTPException

from app import main


class MissingAnalyticsTests(unittest.TestCase):
    def setUp(self):
        self.original_db = main.db

    def tearDown(self):
        main.db = self.original_db

    def test_employee_monthly_counts_workdays_from_join_date(self):
        captured = {}

        class Employees:
            def find_one(self, query, projection):
                return {"joined_on": "2026-07-13"}

        class Attendance:
            def aggregate(self, pipeline):
                captured["pipeline"] = pipeline
                return [
                    {
                        "present_days": 1.5,
                        "leave_days": 1,
                        "late_count": 2,
                        "total_late_minutes": 95,
                        "total_overtime_minutes": 30,
                    }
                ]

        main.db = SimpleNamespace(
            employees=Employees(), attendance_logs=Attendance()
        )

        result = main.employee_monthly("EMP0001", "2026-07")

        self.assertEqual(result["working_days"], 15)
        self.assertEqual(result["present_days"], 1.5)
        self.assertEqual(result["attendance_pct"], 10.0)
        self.assertEqual(result["total_late_minutes"], 95)
        self.assertEqual(
            captured["pipeline"][0]["$match"]["date"],
            {"$gte": "2026-07-01", "$lt": "2026-08-01"},
        )

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
        self.assertEqual(captured["command"]["aggregate"], "attendance_logs")
        self.assertEqual(result["explain"]["queryPlanner"]["winningPlan"]["stage"], "IXSCAN")

    def test_explain_requires_endpoint_parameters(self):
        with self.assertRaises(HTTPException) as error:
            main.explain_endpoint("employee_monthly")
        self.assertEqual(error.exception.status_code, 422)


if __name__ == "__main__":
    unittest.main()
