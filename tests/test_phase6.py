"""Comprehensive unit and mocked endpoint test suite for Phase 6 (Employee Monthly Analytics)."""
import calendar
import unittest
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

from fastapi import HTTPException
from pydantic import ValidationError

from app.main import (
    EmployeeMonthly,
    build_employee_monthly_pipeline,
    calculate_working_days,
    compute_attendance_pct,
    get_employee_monthly,
)


class TestWorkingDaysAndRoundingCalculations(unittest.TestCase):
    def test_calculate_working_days_full_month(self):
        # July 2026 has 31 days, starting on Wednesday (July 1), ending on Friday (July 31).
        # Weekdays count: 23
        wd = calculate_working_days("2026-01-05", "2026-07")
        self.assertEqual(wd, 23)

    def test_calculate_working_days_mid_month_weekday(self):
        # Joined Monday, July 13, 2026.
        # Weekdays from July 13 to July 31: 3 weeks x 5 days = 15 days
        wd = calculate_working_days("2026-07-13", "2026-07")
        self.assertEqual(wd, 15)

    def test_calculate_working_days_mid_month_weekend(self):
        # Joined Saturday, July 11, 2026.
        # First working day is Monday, July 13. Weekdays remain 15.
        wd = calculate_working_days("2026-07-11", "2026-07")
        self.assertEqual(wd, 15)

    def test_calculate_working_days_last_day_of_month(self):
        # July 31, 2026 is a Friday -> 1 working day
        wd = calculate_working_days("2026-07-31", "2026-07")
        self.assertEqual(wd, 1)

        # May 31, 2026 is a Sunday -> 0 working days
        wd_sun = calculate_working_days("2026-05-31", "2026-05")
        self.assertEqual(wd_sun, 0)

    def test_calculate_working_days_joined_after_month(self):
        # Joined in August 2026; requested month is July 2026 -> 0 working days
        wd = calculate_working_days("2026-08-01", "2026-07")
        self.assertEqual(wd, 0)

    def test_calculate_working_days_february_leap_and_non_leap(self):
        # February 2024 is a leap year (29 days). Feb 1 was Thu, Feb 29 was Thu -> 21 weekdays
        wd_leap = calculate_working_days("2020-01-01", "2024-02")
        self.assertEqual(wd_leap, 21)

        # February 2023 (28 days). Feb 1 was Wed, Feb 28 was Tue -> 20 weekdays
        wd_non_leap = calculate_working_days("2020-01-01", "2023-02")
        self.assertEqual(wd_non_leap, 20)

    def test_compute_attendance_pct_round_half_up(self):
        # 2 present days out of 23 working days = 8.69565...% -> 8.70%
        pct = compute_attendance_pct(2.0, 23)
        self.assertEqual(pct, 8.7)

        # 0.5 present days out of 23 = 2.1739...% -> 2.17%
        pct_half = compute_attendance_pct(0.5, 23)
        self.assertEqual(pct_half, 2.17)

        # 1 present day out of 15 working days = 6.6666...% -> 6.67%
        pct_third = compute_attendance_pct(1.0, 15)
        self.assertEqual(pct_third, 6.67)

        # Zero present days -> 0.0%
        self.assertEqual(compute_attendance_pct(0.0, 23), 0.0)

        # Zero working days -> None (null in json)
        self.assertIsNone(compute_attendance_pct(0.0, 0))

        # 100% attendance
        self.assertEqual(compute_attendance_pct(23.0, 23), 100.0)


class TestAggregationPipelineStructure(unittest.TestCase):
    def test_pipeline_stages_and_filters(self):
        pipeline = build_employee_monthly_pipeline("EMP0001", "2026-07")
        self.assertEqual(len(pipeline), 2)

        match_stage = pipeline[0]["$match"]
        self.assertEqual(match_stage["emp_code"], "EMP0001")
        self.assertEqual(match_stage["date"]["$gte"], "2026-07-01")
        self.assertEqual(match_stage["date"]["$lte"], "2026-07-31")

        group_stage = pipeline[1]["$group"]
        self.assertIsNone(group_stage["_id"])
        self.assertIn("present_days", group_stage)
        self.assertIn("leave_days", group_stage)
        self.assertIn("late_count", group_stage)
        self.assertIn("total_late_minutes", group_stage)
        self.assertIn("total_overtime_minutes", group_stage)

    def test_month_regex_pattern_validation(self):
        import re
        pattern = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
        self.assertTrue(pattern.match("2026-07"))
        self.assertTrue(pattern.match("2026-01"))
        self.assertTrue(pattern.match("2026-12"))

        self.assertIsNone(pattern.match("2026-00"))
        self.assertIsNone(pattern.match("2026-13"))
        self.assertIsNone(pattern.match("2026-7"))
        self.assertIsNone(pattern.match("invalid"))
        self.assertIsNone(pattern.match("2026/07"))


class TestEmployeeMonthlyEndpoint(unittest.TestCase):
    @patch("app.main.db")
    def test_get_employee_monthly_unknown_employee_404(self, mock_db):
        mock_db.employees.find_one.return_value = None
        with self.assertRaises(HTTPException) as ctx:
            get_employee_monthly("EMP9999", "2026-07")
        self.assertEqual(ctx.exception.status_code, 404)

    @patch("app.main.db")
    def test_get_employee_monthly_joined_after_month_zero_working_days(self, mock_db):
        mock_db.employees.find_one.return_value = {
            "emp_code": "EMP0001",
            "joined_on": "2026-08-01",
        }
        mock_db.attendance_logs.aggregate.return_value = []

        res = get_employee_monthly("EMP0001", "2026-07")
        self.assertEqual(res["emp_code"], "EMP0001")
        self.assertEqual(res["month"], "2026-07")
        self.assertEqual(res["working_days"], 0)
        self.assertEqual(res["present_days"], 0.0)
        self.assertEqual(res["leave_days"], 0)
        self.assertEqual(res["late_count"], 0)
        self.assertEqual(res["total_late_minutes"], 0)
        self.assertEqual(res["total_overtime_minutes"], 0)
        self.assertIsNone(res["attendance_pct"])

    @patch("app.main.db")
    def test_get_employee_monthly_no_logs_in_month(self, mock_db):
        mock_db.employees.find_one.return_value = {
            "emp_code": "EMP0001",
            "joined_on": "2026-01-05",
        }
        mock_db.attendance_logs.aggregate.return_value = []

        res = get_employee_monthly("EMP0001", "2026-07")
        self.assertEqual(res["working_days"], 23)
        self.assertEqual(res["present_days"], 0.0)
        self.assertEqual(res["leave_days"], 0)
        self.assertEqual(res["late_count"], 0)
        self.assertEqual(res["total_late_minutes"], 0)
        self.assertEqual(res["total_overtime_minutes"], 0)
        self.assertEqual(res["attendance_pct"], 0.0)

    @patch("app.main.db")
    def test_get_employee_monthly_mocked_aggregation_success(self, mock_db):
        # Simulating employee with 2 full present days in July 2026
        mock_db.employees.find_one.return_value = {
            "emp_code": "EMP0001",
            "joined_on": "2026-01-05",
        }
        mock_db.attendance_logs.aggregate.return_value = [
            {
                "_id": None,
                "present_days": 2.0,
                "leave_days": 0,
                "late_count": 0,
                "total_late_minutes": 0,
                "total_overtime_minutes": 0,
            }
        ]

        res = get_employee_monthly("EMP0001", "2026-07")
        self.assertEqual(res["emp_code"], "EMP0001")
        self.assertEqual(res["month"], "2026-07")
        self.assertEqual(res["working_days"], 23)
        self.assertEqual(res["present_days"], 2.0)
        self.assertEqual(res["leave_days"], 0)
        self.assertEqual(res["late_count"], 0)
        self.assertEqual(res["attendance_pct"], 8.7)

    @patch("app.main.db")
    def test_get_employee_monthly_half_day_and_leave(self, mock_db):
        # Simulating EMP0004: 1 half-day (0.5), 1 absent day
        mock_db.employees.find_one.return_value = {
            "emp_code": "EMP0004",
            "joined_on": "2026-02-01",
        }
        mock_db.attendance_logs.aggregate.return_value = [
            {
                "_id": None,
                "present_days": 0.5,
                "leave_days": 0,
                "late_count": 0,
                "total_late_minutes": 0,
                "total_overtime_minutes": 0,
            }
        ]

        res = get_employee_monthly("EMP0004", "2026-07")
        self.assertEqual(res["working_days"], 23)
        self.assertEqual(res["present_days"], 0.5)
        self.assertEqual(res["attendance_pct"], 2.17)

    @patch("app.main.db")
    def test_get_employee_monthly_late_and_overtime_with_leave(self, mock_db):
        # Simulating EMP0003: 1 present day (late 35, ot 40), 1 leave day
        mock_db.employees.find_one.return_value = {
            "emp_code": "EMP0003",
            "joined_on": "2026-02-01",
        }
        mock_db.attendance_logs.aggregate.return_value = [
            {
                "_id": None,
                "present_days": 1.0,
                "leave_days": 1,
                "late_count": 1,
                "total_late_minutes": 35,
                "total_overtime_minutes": 40,
            }
        ]

        res = get_employee_monthly("EMP0003", "2026-07")
        self.assertEqual(res["working_days"], 23)
        self.assertEqual(res["present_days"], 1.0)
        self.assertEqual(res["leave_days"], 1)
        self.assertEqual(res["late_count"], 1)
        self.assertEqual(res["total_late_minutes"], 35)
        self.assertEqual(res["total_overtime_minutes"], 40)
        self.assertEqual(res["attendance_pct"], 4.35)

    @patch("app.main.db")
    def test_get_employee_monthly_database_down_503(self, mock_db):
        with patch("app.main.db", None):
            with self.assertRaises(HTTPException) as ctx:
                get_employee_monthly("EMP0001", "2026-07")
            self.assertEqual(ctx.exception.status_code, 503)


class TestRealMongoAggregationExecution(unittest.TestCase):
    """Executes the exact aggregation pipeline against a real MongoDB engine (local instance).
    Clearly distinguished from mocked unit tests.
    """

    def setUp(self):
        from pymongo import MongoClient
        try:
            self.client = MongoClient("mongodb://localhost:27017", serverSelectionTimeoutMS=500)
            self.client.admin.command("ping")
            self.test_db = self.client["test_audit_phase6_local"]
            self.test_db.drop_collection("attendance_logs")
        except Exception:
            raise unittest.SkipTest("Local MongoDB not available for live aggregation execution")

    def tearDown(self):
        if hasattr(self, "client"):
            self.client.drop_database("test_audit_phase6_local")
            self.client.close()

    def test_live_aggregation_pipeline_execution(self):
        # 1. 2026-07-06 (Mon): PRESENT, full day
        # 2. 2026-07-07 (Tue): WFH, half day (0.5)
        # 3. 2026-07-08 (Wed): LEAVE
        # 4. 2026-07-11 (Sat): PRESENT on weekend (0 towards present_days, 15 late, 60 ot)
        # 5. 2026-07-13 (Mon): ON_DUTY, full day (20 late, 30 ot)
        self.test_db.attendance_logs.insert_many([
            {"emp_code": "EMP0001", "date": "2026-07-06", "status": "PRESENT", "half_day": False, "late_minutes": 0, "overtime_minutes": 0},
            {"emp_code": "EMP0001", "date": "2026-07-07", "status": "WFH", "half_day": True, "late_minutes": 0, "overtime_minutes": 0},
            {"emp_code": "EMP0001", "date": "2026-07-08", "status": "LEAVE", "late_minutes": 0, "overtime_minutes": 0},
            {"emp_code": "EMP0001", "date": "2026-07-11", "status": "PRESENT", "half_day": False, "late_minutes": 15, "overtime_minutes": 60},
            {"emp_code": "EMP0001", "date": "2026-07-13", "status": "ON_DUTY", "half_day": False, "late_minutes": 20, "overtime_minutes": 30},
        ])

        pipeline = build_employee_monthly_pipeline("EMP0001", "2026-07")
        results = list(self.test_db.attendance_logs.aggregate(pipeline))
        self.assertEqual(len(results), 1)

        row = results[0]
        # present_days = 1.0 (Mon) + 0.5 (Tue) + 0.0 (Sat weekend) + 1.0 (Mon) = 2.5
        self.assertEqual(row["present_days"], 2.5)
        # leave_days = 1 (Wed)
        self.assertEqual(row["leave_days"], 1)
        # late_count = 2 (Sat + Mon)
        self.assertEqual(row["late_count"], 2)
        # total_late_minutes = 15 + 20 = 35
        self.assertEqual(row["total_late_minutes"], 35)
        # total_overtime_minutes = 60 + 30 = 90
        self.assertEqual(row["total_overtime_minutes"], 90)


if __name__ == "__main__":
    unittest.main()
