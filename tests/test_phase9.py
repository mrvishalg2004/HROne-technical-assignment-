"""Comprehensive unit, mocked, and real endpoint test suite for Phase 9 (Department Trend API)."""
import unittest
from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

from fastapi import HTTPException
from pydantic import ValidationError

from app.main import (
    Trend,
    TrendItem,
    build_department_trend_pipeline,
    get_department_trend,
)


class TestDepartmentTrendPipelineStructure(unittest.TestCase):
    def test_pipeline_stages(self):
        pipeline = build_department_trend_pipeline("Engineering", "2026-07-01", "2026-07-07")
        self.assertEqual(len(pipeline), 14)

        # Stage 0: $match date in range
        match_stage = pipeline[0]["$match"]
        self.assertEqual(match_stage["date"]["$gte"], "2026-07-01")
        self.assertEqual(match_stage["date"]["$lte"], "2026-07-07")

        # Stage 1-3: $lookup, $unwind, $match department
        self.assertEqual(pipeline[1]["$lookup"]["from"], "employees")
        self.assertEqual(pipeline[2]["$unwind"], "$emp")
        self.assertEqual(pipeline[3]["$match"]["emp.department"], "Engineering")

        # Stage 4: $group by date for presence and late
        group_stage = pipeline[4]["$group"]
        self.assertIn("present_count", group_stage)
        self.assertIn("late_count", group_stage)

        # Stage 6: $densify over cal_date
        densify_stage = pipeline[6]["$densify"]
        self.assertEqual(densify_stage["field"], "cal_date")
        self.assertEqual(densify_stage["range"]["step"], 1)
        self.assertEqual(densify_stage["range"]["unit"], "day")

        # Stage 8: $lookup employees for date-specific headcount
        hc_lookup = pipeline[8]["$lookup"]
        self.assertEqual(hc_lookup["from"], "employees")

        # Stage 11: $setWindowFields with documents: [-6, "current"]
        win_stage = pipeline[11]["$setWindowFields"]
        self.assertEqual(win_stage["output"]["moving_avg_7d"]["window"]["documents"], [-6, "current"])

        # Stage 12: $sort by date ascending
        self.assertEqual(pipeline[12]["$sort"], {"date": 1})


class TestMockedDepartmentTrendEndpoint(unittest.TestCase):
    def test_trend_validation_to_before_from_422(self):
        with self.assertRaises(HTTPException) as ctx:
            get_department_trend("Engineering", from_date="2026-07-10", to_date="2026-07-05")
        self.assertEqual(ctx.exception.status_code, 422)

    def test_trend_validation_range_exceeds_92_days_422(self):
        # 93 days range: from 2026-01-01 to 2026-04-03
        f_date = date(2026, 1, 1)
        t_date = f_date + timedelta(days=92)  # 93 days inclusive
        with self.assertRaises(HTTPException) as ctx:
            get_department_trend(
                "Engineering",
                from_date=f_date.isoformat(),
                to_date=t_date.isoformat(),
            )
        self.assertEqual(ctx.exception.status_code, 422)

    def test_trend_validation_range_within_92_days_allowed(self):
        # 92 days inclusive should pass date validation
        f_date = date(2026, 1, 1)
        t_date = f_date + timedelta(days=91)  # exactly 92 days
        with patch("app.main.db") as mock_db:
            mock_db.employees.find_one.return_value = {"department": "Engineering"}
            mock_db.attendance_logs.aggregate.return_value = []
            res = get_department_trend(
                "Engineering",
                from_date=f_date.isoformat(),
                to_date=t_date.isoformat(),
            )
            self.assertEqual(res["department"], "Engineering")
            self.assertEqual(res["items"], [])

    @patch("app.main.db")
    def test_trend_database_down_503(self, mock_db):
        with patch("app.main.db", None):
            with self.assertRaises(HTTPException) as ctx:
                get_department_trend("Engineering", from_date="2026-07-01", to_date="2026-07-05")
            self.assertEqual(ctx.exception.status_code, 503)

    @patch("app.main.db")
    def test_trend_unknown_department_404(self, mock_db):
        mock_db.employees.find_one.return_value = None
        with self.assertRaises(HTTPException) as ctx:
            get_department_trend("NonExistent", from_date="2026-07-01", to_date="2026-07-05")
        self.assertEqual(ctx.exception.status_code, 404)

    def test_trend_item_forbids_extra_fields(self):
        with self.assertRaises(ValidationError):
            TrendItem(
                date="2026-07-01",
                is_working_day=True,
                headcount=1,
                present_count=1.0,
                late_count=0,
                extra_forbidden_field=123,
            )


class TestRealMongoDepartmentTrendExecution(unittest.TestCase):
    """Executes department trend aggregation against local MongoDB (localhost:27017).
    Atlas integration remains unverified.
    """

    def setUp(self):
        from pymongo import MongoClient
        try:
            self.client = MongoClient("mongodb://localhost:27017", serverSelectionTimeoutMS=500)
            self.client.admin.command("ping")
            self.test_db = self.client["test_audit_phase9_local"]
            self.test_db.employees.drop()
            self.test_db.attendance_logs.drop()
        except Exception:
            raise unittest.SkipTest("Local MongoDB not available for live aggregation execution")

    def tearDown(self):
        if hasattr(self, "client"):
            self.client.drop_database("test_audit_phase9_local")
            self.client.close()

    def test_live_department_trend_gap_filling_and_moving_avg(self):
        # 1. Employees:
        # E01 joined Jan 1, 2026
        # E02 joined July 3, 2026 (Friday)
        # E03 belongs to HR (should be excluded)
        self.test_db.employees.insert_many([
            {"emp_code": "E01", "name": "Alice", "department": "Engineering", "joined_on": "2026-01-01"},
            {"emp_code": "E02", "name": "Bob", "department": "Engineering", "joined_on": "2026-07-03"},
            {"emp_code": "E03", "name": "Charlie", "department": "HR", "joined_on": "2026-01-01"},
        ])

        # 2. Attendance logs for July 1-7, 2026 (Wednesday through Tuesday):
        # July 1 (Wed): E01 PRESENT, full day -> present=1.0, late=0, hc=1 -> rate=1.0000
        # July 2 (Thu): NO LOGS AT ALL (gap day) -> present=0.0, late=0, hc=1 -> rate=0.0000
        # July 3 (Fri): E01 WFH half_day, E02 PRESENT late=15 -> present=1.5, late=1, hc=2 -> rate=0.7500
        # July 4 (Sat): Weekend, E01 ON_DUTY -> present=1.0, late=0, hc=2 -> rate=null
        # July 5 (Sun): Weekend, NO LOGS -> present=0.0, late=0, hc=2 -> rate=null
        # July 6 (Mon): E01 PRESENT, E02 PRESENT -> present=2.0, late=0, hc=2 -> rate=1.0000
        # July 7 (Tue): E01 PRESENT half_day -> present=0.5, late=0, hc=2 -> rate=0.2500
        self.test_db.attendance_logs.insert_many([
            {"emp_code": "E01", "date": "2026-07-01", "status": "PRESENT", "late_minutes": 0, "half_day": False},
            {"emp_code": "E01", "date": "2026-07-03", "status": "WFH", "late_minutes": 0, "half_day": True},
            {"emp_code": "E02", "date": "2026-07-03", "status": "PRESENT", "late_minutes": 15, "half_day": False},
            {"emp_code": "E01", "date": "2026-07-04", "status": "ON_DUTY", "late_minutes": 0, "half_day": False},
            {"emp_code": "E01", "date": "2026-07-06", "status": "PRESENT", "late_minutes": 0, "half_day": False},
            {"emp_code": "E02", "date": "2026-07-06", "status": "PRESENT", "late_minutes": 0, "half_day": False},
            {"emp_code": "E01", "date": "2026-07-07", "status": "PRESENT", "late_minutes": 0, "half_day": True},
        ])

        with patch("app.main.db", self.test_db):
            res = get_department_trend("Engineering", from_date="2026-07-01", to_date="2026-07-07")

        self.assertEqual(res["department"], "Engineering")
        items = res["items"]
        # Exactly 7 rows (inclusive calendar days)
        self.assertEqual(len(items), 7)

        # Day 1: 2026-07-01 (Wed)
        d1 = items[0]
        self.assertEqual(d1["date"], "2026-07-01")
        self.assertTrue(d1["is_working_day"])
        self.assertEqual(d1["headcount"], 1)
        self.assertEqual(d1["present_count"], 1.0)
        self.assertEqual(d1["late_count"], 0)
        self.assertEqual(d1["attendance_rate"], 1.0)
        self.assertEqual(d1["moving_avg_7d"], 1.0)

        # Day 2: 2026-07-02 (Thu) - Filled by $densify
        d2 = items[1]
        self.assertEqual(d2["date"], "2026-07-02")
        self.assertTrue(d2["is_working_day"])
        self.assertEqual(d2["headcount"], 1)
        self.assertEqual(d2["present_count"], 0.0)
        self.assertEqual(d2["late_count"], 0)
        self.assertEqual(d2["attendance_rate"], 0.0)
        # Shorter window: mean(1.0, 0.0) = 0.5000
        self.assertEqual(d2["moving_avg_7d"], 0.5)

        # Day 3: 2026-07-03 (Fri) - E02 joined today -> headcount increases to 2!
        d3 = items[2]
        self.assertEqual(d3["date"], "2026-07-03")
        self.assertTrue(d3["is_working_day"])
        self.assertEqual(d3["headcount"], 2)
        self.assertEqual(d3["present_count"], 1.5)
        self.assertEqual(d3["late_count"], 1)
        self.assertEqual(d3["attendance_rate"], 0.75)
        # mean(1.0, 0.0, 0.75) = 0.5833
        self.assertEqual(d3["moving_avg_7d"], 0.5833)

        # Day 4: 2026-07-04 (Sat) - Weekend with ON_DUTY
        d4 = items[3]
        self.assertEqual(d4["date"], "2026-07-04")
        self.assertFalse(d4["is_working_day"])
        self.assertEqual(d4["headcount"], 2)
        self.assertEqual(d4["present_count"], 1.0)
        self.assertIsNone(d4["attendance_rate"])  # null on weekend
        # moving_avg_7d ignores weekend null rate: still 0.5833
        self.assertEqual(d4["moving_avg_7d"], 0.5833)

        # Day 5: 2026-07-05 (Sun) - Weekend with no logs
        d5 = items[4]
        self.assertEqual(d5["date"], "2026-07-05")
        self.assertFalse(d5["is_working_day"])
        self.assertEqual(d5["headcount"], 2)
        self.assertEqual(d5["present_count"], 0.0)
        self.assertIsNone(d5["attendance_rate"])
        self.assertEqual(d5["moving_avg_7d"], 0.5833)

        # Day 6: 2026-07-06 (Mon)
        d6 = items[5]
        self.assertEqual(d6["date"], "2026-07-06")
        self.assertTrue(d6["is_working_day"])
        self.assertEqual(d6["headcount"], 2)
        self.assertEqual(d6["present_count"], 2.0)
        self.assertEqual(d6["attendance_rate"], 1.0)
        # mean(1.0, 0.0, 0.75, 1.0) = 3.75 / 4 = 0.6875
        self.assertEqual(d6["moving_avg_7d"], 0.6875)

        # Day 7: 2026-07-07 (Tue)
        d7 = items[6]
        self.assertEqual(d7["date"], "2026-07-07")
        self.assertTrue(d7["is_working_day"])
        self.assertEqual(d7["headcount"], 2)
        self.assertEqual(d7["present_count"], 0.5)
        self.assertEqual(d7["attendance_rate"], 0.25)
        # mean(1.0, 0.0, 0.75, 1.0, 0.25) = 3.0 / 5 = 0.6000
        self.assertEqual(d7["moving_avg_7d"], 0.6)

    def test_live_department_trend_zero_logs_full_range(self):
        # Department with 1 employee, but zero attendance logs
        self.test_db.employees.insert_one({
            "emp_code": "E01",
            "name": "Alice",
            "department": "QA",
            "joined_on": "2026-07-01",
        })

        with patch("app.main.db", self.test_db):
            res = get_department_trend("QA", from_date="2026-07-01", to_date="2026-07-03")

        items = res["items"]
        self.assertEqual(len(items), 3)
        for item in items:
            self.assertEqual(item["headcount"], 1)
            self.assertEqual(item["present_count"], 0.0)
            self.assertEqual(item["late_count"], 0)
            self.assertEqual(item["attendance_rate"], 0.0)
            self.assertEqual(item["moving_avg_7d"], 0.0)


if __name__ == "__main__":
    unittest.main()
