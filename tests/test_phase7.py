"""Comprehensive unit and mocked/real endpoint test suite for Phase 7 (Department Summary Analytics)."""
import re
import unittest
from decimal import Decimal
from unittest.mock import MagicMock, patch

from fastapi import HTTPException
from pydantic import ValidationError

from app.main import (
    DepartmentSummary,
    DepartmentSummaryItem,
    build_department_summary_pipeline,
    get_department_summary,
)


class TestDepartmentSummaryPipelineStructure(unittest.TestCase):
    def test_pipeline_stages_and_filters_all_departments(self):
        pipeline = build_department_summary_pipeline("2026-07")
        self.assertEqual(len(pipeline), 6)

        # Stage 0: Match employees joined on or before month end
        match_stage = pipeline[0]["$match"]
        self.assertEqual(match_stage["joined_on"]["$lte"], "2026-07-31")
        self.assertNotIn("department", match_stage)

        # Stage 1: Group by department to get headcount
        group_dept = pipeline[1]["$group"]
        self.assertEqual(group_dept["_id"], "$department")
        self.assertEqual(group_dept["headcount"], {"$sum": 1})

        # Stage 2: Cross-collection lookup into attendance_logs
        lookup_stage = pipeline[2]["$lookup"]
        self.assertEqual(lookup_stage["from"], "attendance_logs")

        # Stage 3: Unwind preserving departments with no logs
        unwind_stage = pipeline[3]["$unwind"]
        self.assertTrue(unwind_stage["preserveNullAndEmptyArrays"])

        # Stage 4: Aggregate attendance metrics and work hours
        agg_stage = pipeline[4]["$group"]
        self.assertIn("present_days", agg_stage)
        self.assertIn("leave_count", agg_stage)
        self.assertIn("on_duty_count", agg_stage)
        self.assertIn("late_count", agg_stage)
        self.assertIn("total_late_minutes", agg_stage)
        self.assertIn("wh_sum", agg_stage)
        self.assertIn("wh_count", agg_stage)

        # Stage 5: Sort by department name ascending
        sort_stage = pipeline[5]["$sort"]
        self.assertEqual(sort_stage["_id"], 1)

    def test_pipeline_filters_specific_department(self):
        pipeline = build_department_summary_pipeline("2026-07", department="Engineering")
        match_stage = pipeline[0]["$match"]
        self.assertEqual(match_stage["department"], "Engineering")
        self.assertEqual(match_stage["joined_on"]["$lte"], "2026-07-31")

    def test_month_parameter_regex_pattern(self):
        pattern = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
        self.assertTrue(pattern.match("2026-07"))
        self.assertTrue(pattern.match("2026-01"))
        self.assertTrue(pattern.match("2026-12"))

        self.assertIsNone(pattern.match("2026-00"))
        self.assertIsNone(pattern.match("2026-13"))
        self.assertIsNone(pattern.match("2026-7"))
        self.assertIsNone(pattern.match("2026/07"))
        self.assertIsNone(pattern.match("bad_month"))


class TestMockedDepartmentSummaryEndpoint(unittest.TestCase):
    @patch("app.main.db")
    def test_get_department_summary_database_down_503(self, mock_db):
        with patch("app.main.db", None):
            with self.assertRaises(HTTPException) as ctx:
                get_department_summary("2026-07")
            self.assertEqual(ctx.exception.status_code, 503)

    @patch("app.main.db")
    def test_get_department_summary_empty_results(self, mock_db):
        mock_db.employees.aggregate.return_value = []
        res = get_department_summary("2026-07")
        self.assertEqual(res["month"], "2026-07")
        self.assertEqual(res["items"], [])

    @patch("app.main.db")
    def test_get_department_summary_mocked_results_and_rounding(self, mock_db):
        # 3 qualifying records with sum = 27.13, count = 3 -> mean 9.0433... -> 9.04
        mock_db.employees.aggregate.return_value = [
            {
                "_id": "Engineering",
                "headcount": 2,
                "present_days": 3.0,
                "wh_sum": 27.13,
                "wh_count": 3,
                "late_count": 0,
                "total_late_minutes": 0,
                "leave_count": 0,
                "on_duty_count": 0,
            },
            {
                "_id": "QA",
                "headcount": 1,
                "present_days": 0.0,
                "wh_sum": 0.0,
                "wh_count": 0,
                "late_count": 0,
                "total_late_minutes": 0,
                "leave_count": 0,
                "on_duty_count": 0,
            },
        ]

        res = get_department_summary("2026-07")
        self.assertEqual(res["month"], "2026-07")
        self.assertEqual(len(res["items"]), 2)

        eng = res["items"][0]
        self.assertEqual(eng["department"], "Engineering")
        self.assertEqual(eng["headcount"], 2)
        self.assertEqual(eng["present_days"], 3.0)
        self.assertEqual(eng["avg_work_hours"], 9.04)

        qa = res["items"][1]
        self.assertEqual(qa["department"], "QA")
        self.assertEqual(qa["headcount"], 1)
        self.assertEqual(qa["present_days"], 0.0)
        self.assertIsNone(qa["avg_work_hours"])

    def test_department_summary_item_forbids_extra_fields(self):
        with self.assertRaises(ValidationError):
            DepartmentSummaryItem(
                department="Engineering",
                headcount=2,
                present_days=3.0,
                late_count=0,
                total_late_minutes=0,
                leave_count=0,
                on_duty_count=0,
                unwanted_extra_field=123,
            )


class TestRealMongoDepartmentSummaryExecution(unittest.TestCase):
    """Executes the cross-collection aggregation against local MongoDB (localhost:27017).
    Distinguished from mocked tests. Atlas integration remains unverified.
    """

    def setUp(self):
        from pymongo import MongoClient
        try:
            self.client = MongoClient("mongodb://localhost:27017", serverSelectionTimeoutMS=500)
            self.client.admin.command("ping")
            self.test_db = self.client["test_audit_phase7_local"]
            self.test_db.employees.drop()
            self.test_db.attendance_logs.drop()
        except Exception:
            raise unittest.SkipTest("Local MongoDB not available for live aggregation execution")

    def tearDown(self):
        if hasattr(self, "client"):
            self.client.drop_database("test_audit_phase7_local")
            self.client.close()

    def test_live_cross_collection_department_summary(self):
        # 1. Engineering: 2 employees (one early, one mid-month July 13)
        # 2. QA: 1 employee (has zero logs, but should appear with headcount 1)
        # 3. HR: 1 employee (joins Aug 1 -> after month end, so HR should be omitted)
        # 4. Sales: 2 employees (one present with overtime/late, one half-day WFH, one leave)
        self.test_db.employees.insert_many([
            {"emp_code": "E01", "department": "Engineering", "joined_on": "2026-01-05"},
            {"emp_code": "E02", "department": "Engineering", "joined_on": "2026-07-13"},
            {"emp_code": "E03", "department": "QA", "joined_on": "2026-01-01"},
            {"emp_code": "E04", "department": "HR", "joined_on": "2026-08-01"},
            {"emp_code": "E05", "department": "Sales", "joined_on": "2026-02-01"},
            {"emp_code": "E06", "department": "Sales", "joined_on": "2026-02-01"},
        ])

        # Attendance logs for July 2026:
        self.test_db.attendance_logs.insert_many([
            # Engineering:
            {"emp_code": "E01", "date": "2026-07-06", "status": "PRESENT", "work_hours": 9.12, "late_minutes": 0, "overtime_minutes": 0, "half_day": False},
            {"emp_code": "E02", "date": "2026-07-14", "status": "PRESENT", "work_hours": 8.98, "late_minutes": 0, "overtime_minutes": 0, "half_day": False},
            # Missing work_hours, half_day, late_minutes (e.g. unpunched or omitted derived fields):
            {"emp_code": "E01", "date": "2026-07-20", "status": "PRESENT"},
            # Sales:
            {"emp_code": "E05", "date": "2026-07-06", "status": "PRESENT", "work_hours": 9.08, "late_minutes": 35, "overtime_minutes": 40, "half_day": False},
            {"emp_code": "E05", "date": "2026-07-08", "status": "LEAVE", "work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False},
            {"emp_code": "E06", "date": "2026-07-07", "status": "WFH", "work_hours": 3.50, "late_minutes": 0, "overtime_minutes": 0, "half_day": True},
            # Weekend log for E06: ON_DUTY, late=10, ot=45 (should not increment present_days, but count on_duty, late, ot)
            {"emp_code": "E06", "date": "2026-07-11", "status": "ON_DUTY", "work_hours": 4.00, "late_minutes": 10, "overtime_minutes": 45, "half_day": False},
        ])

        # Test all departments
        pipeline = build_department_summary_pipeline("2026-07")
        results = list(self.test_db.employees.aggregate(pipeline))

        # Expected: Engineering, QA, Sales (HR omitted because headcount is 0)
        self.assertEqual(len(results), 3)
        depts = [r["_id"] for r in results]
        self.assertEqual(depts, ["Engineering", "QA", "Sales"])

        # Check Engineering:
        eng = results[0]
        self.assertEqual(eng["headcount"], 2)
        # 1.0 (Jul 6) + 1.0 (Jul 14) + 1.0 (Jul 20 with missing half_day default) = 3.0
        self.assertEqual(eng["present_days"], 3.0)
        # wh_count is exactly 2 because missing work_hours on Jul 20 is not counted
        self.assertEqual(eng["wh_count"], 2)
        # mean work hours: (9.12 + 8.98) / 2 = 9.05 (not diluted by missing work_hours)
        avg_eng = float((Decimal(str(eng["wh_sum"])) / Decimal(str(eng["wh_count"]))).quantize(Decimal("0.01")))
        self.assertEqual(avg_eng, 9.05)
        self.assertEqual(eng["late_count"], 0)

        # Check QA (zero logs):
        qa = results[1]
        self.assertEqual(qa["headcount"], 1)
        self.assertEqual(qa["present_days"], 0.0)
        self.assertEqual(qa["wh_count"], 0)
        self.assertEqual(qa["late_count"], 0)
        self.assertEqual(qa["leave_count"], 0)
        self.assertEqual(qa["on_duty_count"], 0)

        # Check Sales:
        sales = results[2]
        self.assertEqual(sales["headcount"], 2)
        # present_days: 1.0 (E05 Mon) + 0.5 (E06 Tue half) + 0.0 (E06 Sat weekend) = 1.5
        self.assertEqual(sales["present_days"], 1.5)
        # mean work hours includes the 3 records with work_hours: 9.08 + 3.50 + 4.00 = 16.58 / 3 = 5.5266... -> 5.53
        avg_sales = float((Decimal(str(sales["wh_sum"])) / Decimal(str(sales["wh_count"]))).quantize(Decimal("0.01")))
        self.assertEqual(avg_sales, 5.53)
        self.assertEqual(sales["leave_count"], 1)
        self.assertEqual(sales["on_duty_count"], 1)
        self.assertEqual(sales["late_count"], 2)  # E05 (35) + E06 (10)
        self.assertEqual(sales["total_late_minutes"], 45)

        # Test filtering by specific department:
        eng_pipeline = build_department_summary_pipeline("2026-07", department="Engineering")
        eng_results = list(self.test_db.employees.aggregate(eng_pipeline))
        self.assertEqual(len(eng_results), 1)
        self.assertEqual(eng_results[0]["_id"], "Engineering")

        # Test filtering by non-existent department:
        non_pipeline = build_department_summary_pipeline("2026-07", department="UnknownDept")
        non_results = list(self.test_db.employees.aggregate(non_pipeline))
        self.assertEqual(len(non_results), 0)


if __name__ == "__main__":
    unittest.main()
