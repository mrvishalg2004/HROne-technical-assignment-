"""Comprehensive unit, mocked, and real local MongoDB tests for Phase 10:
GET /admin/explain/{endpoint}

Covers:
- All 5 supported endpoints: attendance_list, employee_monthly, department_summary, late_leaderboard, department_trend.
- Parameter requirements, validation, and boundary conditions (422).
- Database unavailable handling (503).
- ExecutionStats verbosity and exact MongoDB explain command dispatch.
- Raw explain response structure and Pydantic schema validation.
- Real local MongoDB execution (localhost:27017) with index initialization.
"""
import unittest
from datetime import date
from unittest.mock import MagicMock, patch

from fastapi import HTTPException
from pydantic import ValidationError

from app.main import (
    ExplainResponse,
    build_department_summary_pipeline,
    build_department_trend_pipeline,
    build_employee_monthly_pipeline,
    build_late_leaderboard_pipeline,
    explain_endpoint,
    init_indexes,
)


class TestAdminExplainMockedContract(unittest.TestCase):
    """Unit and mocked tests for /admin/explain/{endpoint} contract compliance."""

    def test_database_unavailable_raises_503(self):
        with patch("app.main.db", None):
            with self.assertRaises(HTTPException) as ctx:
                explain_endpoint("attendance_list")
            self.assertEqual(ctx.exception.status_code, 503)
            self.assertIn("Database unavailable", ctx.exception.detail)

    def test_unsupported_endpoint_raises_422(self):
        mock_db = MagicMock()
        with patch("app.main.db", mock_db):
            with self.assertRaises(HTTPException) as ctx:
                explain_endpoint("unsupported_route")  # type: ignore
            self.assertEqual(ctx.exception.status_code, 422)
            self.assertIn("Unsupported endpoint", ctx.exception.detail)

    def test_unsupported_endpoint_raises_422_even_if_db_none(self):
        with patch("app.main.db", None):
            with self.assertRaises(HTTPException) as ctx:
                explain_endpoint("unsupported_route")  # type: ignore
            self.assertEqual(ctx.exception.status_code, 422)

    # -------------------------------------------------------------------------
    # 1. attendance_list
    # -------------------------------------------------------------------------
    def test_attendance_list_date_order_validation(self):
        mock_db = MagicMock()
        with patch("app.main.db", mock_db):
            with self.assertRaises(HTTPException) as ctx:
                explain_endpoint("attendance_list", date_from="2026-03-15", date_to="2026-03-01")
            self.assertEqual(ctx.exception.status_code, 422)
            self.assertIn("date_from must not be greater than date_to", ctx.exception.detail)

    def test_attendance_list_pagination_validation(self):
        mock_db = MagicMock()
        with patch("app.main.db", mock_db):
            with self.assertRaises(HTTPException) as ctx:
                explain_endpoint("attendance_list", page=0)
            self.assertEqual(ctx.exception.status_code, 422)

            with self.assertRaises(HTTPException) as ctx2:
                explain_endpoint("attendance_list", page_size=150)
            self.assertEqual(ctx2.exception.status_code, 422)

    def test_attendance_list_invalid_date_format(self):
        mock_db = MagicMock()
        with patch("app.main.db", mock_db):
            with self.assertRaises(HTTPException) as ctx:
                explain_endpoint("attendance_list", date_from="invalid-date")
            self.assertEqual(ctx.exception.status_code, 422)

    def test_attendance_list_invalid_status(self):
        mock_db = MagicMock()
        with patch("app.main.db", mock_db):
            with self.assertRaises(HTTPException) as ctx:
                explain_endpoint("attendance_list", status="INVALID_STATUS")  # type: ignore
            self.assertEqual(ctx.exception.status_code, 422)
            self.assertIn("Invalid status", ctx.exception.detail)

    def test_attendance_list_command_structure(self):
        mock_db = MagicMock()
        mock_db.command.return_value = {"ok": 1.0, "executionStats": {"nReturned": 0}}

        with patch("app.main.db", mock_db):
            res = explain_endpoint(
                "attendance_list",
                emp_code="EMP001",
                date_from="2026-03-01",
                date_to="2026-03-31",
                status="PRESENT",
                page=2,
                page_size=10,
            )

        self.assertEqual(res["endpoint"], "attendance_list")
        self.assertEqual(res["collection"], "attendance_logs")
        self.assertEqual(res["explain"], {"ok": 1.0, "executionStats": {"nReturned": 0}})

        mock_db.command.assert_called_once_with(
            "explain",
            {
                "find": "attendance_logs",
                "filter": {
                    "emp_code": "EMP001",
                    "date": {"$gte": "2026-03-01", "$lte": "2026-03-31"},
                    "status": "PRESENT",
                },
                "sort": {"date": -1, "emp_code": 1},
                "skip": 10,
                "limit": 10,
            },
            verbosity="executionStats",
        )

        validated = ExplainResponse(**res)
        self.assertEqual(validated.endpoint, "attendance_list")

    # -------------------------------------------------------------------------
    # 2. employee_monthly
    # -------------------------------------------------------------------------
    def test_employee_monthly_missing_emp_code(self):
        mock_db = MagicMock()
        with patch("app.main.db", mock_db):
            with self.assertRaises(HTTPException) as ctx:
                explain_endpoint("employee_monthly", month="2026-03")
            self.assertEqual(ctx.exception.status_code, 422)
            self.assertIn("emp_code", ctx.exception.detail)

    def test_employee_monthly_missing_month(self):
        mock_db = MagicMock()
        with patch("app.main.db", mock_db):
            with self.assertRaises(HTTPException) as ctx:
                explain_endpoint("employee_monthly", emp_code="EMP001")
            self.assertEqual(ctx.exception.status_code, 422)
            self.assertIn("month", ctx.exception.detail)

    def test_employee_monthly_invalid_month_format(self):
        mock_db = MagicMock()
        with patch("app.main.db", mock_db):
            with self.assertRaises(HTTPException) as ctx:
                explain_endpoint("employee_monthly", emp_code="EMP001", month="2026-13")
            self.assertEqual(ctx.exception.status_code, 422)

    def test_employee_monthly_command_structure(self):
        mock_db = MagicMock()
        mock_db.command.return_value = {"ok": 1.0, "stages": []}

        with patch("app.main.db", mock_db):
            res = explain_endpoint("employee_monthly", emp_code="EMP001", month="2026-03")

        self.assertEqual(res["endpoint"], "employee_monthly")
        self.assertEqual(res["collection"], "attendance_logs")

        expected_pipe = build_employee_monthly_pipeline("EMP001", "2026-03")
        mock_db.command.assert_called_once_with(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": expected_pipe, "cursor": {}},
            verbosity="executionStats",
        )
        ExplainResponse(**res)

    # -------------------------------------------------------------------------
    # 3. department_summary
    # -------------------------------------------------------------------------
    def test_department_summary_missing_month(self):
        mock_db = MagicMock()
        with patch("app.main.db", mock_db):
            with self.assertRaises(HTTPException) as ctx:
                explain_endpoint("department_summary")
            self.assertEqual(ctx.exception.status_code, 422)
            self.assertIn("month", ctx.exception.detail)

    def test_department_summary_invalid_month(self):
        mock_db = MagicMock()
        with patch("app.main.db", mock_db):
            with self.assertRaises(HTTPException) as ctx:
                explain_endpoint("department_summary", month="bad-month")
            self.assertEqual(ctx.exception.status_code, 422)

    def test_department_summary_command_structure(self):
        mock_db = MagicMock()
        mock_db.command.return_value = {"ok": 1.0, "stages": []}

        with patch("app.main.db", mock_db):
            res = explain_endpoint("department_summary", month="2026-03", department="Engineering")

        self.assertEqual(res["endpoint"], "department_summary")
        self.assertEqual(res["collection"], "employees")

        expected_pipe = build_department_summary_pipeline("2026-03", "Engineering")
        mock_db.command.assert_called_once_with(
            "explain",
            {"aggregate": "employees", "pipeline": expected_pipe, "cursor": {}},
            verbosity="executionStats",
        )
        ExplainResponse(**res)

    # -------------------------------------------------------------------------
    # 4. late_leaderboard
    # -------------------------------------------------------------------------
    def test_late_leaderboard_missing_month(self):
        mock_db = MagicMock()
        with patch("app.main.db", mock_db):
            with self.assertRaises(HTTPException) as ctx:
                explain_endpoint("late_leaderboard")
            self.assertEqual(ctx.exception.status_code, 422)
            self.assertIn("month", ctx.exception.detail)

    def test_late_leaderboard_invalid_limit(self):
        mock_db = MagicMock()
        with patch("app.main.db", mock_db):
            with self.assertRaises(HTTPException) as ctx:
                explain_endpoint("late_leaderboard", month="2026-03", limit=0)
            self.assertEqual(ctx.exception.status_code, 422)

            with self.assertRaises(HTTPException) as ctx2:
                explain_endpoint("late_leaderboard", month="2026-03", limit=51)
            self.assertEqual(ctx2.exception.status_code, 422)

    def test_late_leaderboard_command_structure(self):
        mock_db = MagicMock()
        mock_db.command.return_value = {"ok": 1.0, "stages": []}

        with patch("app.main.db", mock_db):
            res = explain_endpoint("late_leaderboard", month="2026-03", limit=25, department="Sales")

        self.assertEqual(res["endpoint"], "late_leaderboard")
        self.assertEqual(res["collection"], "attendance_logs")

        expected_pipe = build_late_leaderboard_pipeline("2026-03", limit=25, department="Sales")
        mock_db.command.assert_called_once_with(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": expected_pipe, "cursor": {}},
            verbosity="executionStats",
        )
        ExplainResponse(**res)

    # -------------------------------------------------------------------------
    # 5. department_trend
    # -------------------------------------------------------------------------
    def test_department_trend_missing_department(self):
        mock_db = MagicMock()
        with patch("app.main.db", mock_db):
            with self.assertRaises(HTTPException) as ctx:
                explain_endpoint("department_trend", from_date="2026-03-01", to_date="2026-03-07")
            self.assertEqual(ctx.exception.status_code, 422)
            self.assertIn("department", ctx.exception.detail)

    def test_department_trend_missing_from_or_to(self):
        mock_db = MagicMock()
        with patch("app.main.db", mock_db):
            with self.assertRaises(HTTPException) as ctx:
                explain_endpoint("department_trend", department="HR", to_date="2026-03-07")
            self.assertEqual(ctx.exception.status_code, 422)
            self.assertIn("from", ctx.exception.detail)

            with self.assertRaises(HTTPException) as ctx2:
                explain_endpoint("department_trend", department="HR", from_date="2026-03-01")
            self.assertEqual(ctx2.exception.status_code, 422)
            self.assertIn("to", ctx2.exception.detail)

    def test_department_trend_invalid_dates(self):
        mock_db = MagicMock()
        with patch("app.main.db", mock_db):
            # to < from
            with self.assertRaises(HTTPException) as ctx:
                explain_endpoint("department_trend", department="HR", from_date="2026-03-10", to_date="2026-03-05")
            self.assertEqual(ctx.exception.status_code, 422)

            # range > 92 days
            with self.assertRaises(HTTPException) as ctx2:
                explain_endpoint("department_trend", department="HR", from_date="2026-01-01", to_date="2026-04-15")
            self.assertEqual(ctx2.exception.status_code, 422)
            self.assertIn("92 days", ctx2.exception.detail)

    def test_department_trend_command_structure(self):
        mock_db = MagicMock()
        mock_db.command.return_value = {"ok": 1.0, "stages": []}

        with patch("app.main.db", mock_db):
            res = explain_endpoint(
                "department_trend",
                department="Engineering",
                **{"from": "2026-03-01", "to": "2026-03-10"}
            )

        self.assertEqual(res["endpoint"], "department_trend")
        self.assertEqual(res["collection"], "attendance_logs")

        expected_pipe = build_department_trend_pipeline("Engineering", "2026-03-01", "2026-03-10")
        mock_db.command.assert_called_once_with(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": expected_pipe, "cursor": {}},
            verbosity="executionStats",
        )
        ExplainResponse(**res)

    # -------------------------------------------------------------------------
    # Response Model Strictness
    # -------------------------------------------------------------------------
    def test_explain_response_forbids_extra_fields(self):
        with self.assertRaises(ValidationError):
            ExplainResponse(
                endpoint="attendance_list",
                collection="attendance_logs",
                explain={"ok": 1.0},
                extra_field="disallowed",
            )


class TestAdminExplainRealLocalMongo(unittest.TestCase):
    """Executes explain command against real local MongoDB (localhost:27017).
    Atlas integration remains unverified due to external DNS NXDOMAIN.
    """

    def setUp(self):
        from pymongo import MongoClient
        try:
            self.client = MongoClient("mongodb://localhost:27017", serverSelectionTimeoutMS=500)
            self.client.admin.command("ping")
            self.test_db = self.client["test_audit_phase10_local"]
            self.test_db.employees.drop()
            self.test_db.attendance_logs.drop()
            init_indexes(self.test_db)
        except Exception:
            raise unittest.SkipTest("Local MongoDB not available for live explain execution")

    def tearDown(self):
        if hasattr(self, "client"):
            self.client.drop_database("test_audit_phase10_local")
            self.client.close()

    def _seed_basic_data(self):
        self.test_db.employees.insert_many([
            {"emp_code": "E01", "name": "Alice", "department": "Engineering", "joined_on": "2026-01-01"},
            {"emp_code": "E02", "name": "Bob", "department": "Engineering", "joined_on": "2026-02-01"},
        ])
        self.test_db.attendance_logs.insert_many([
            {"emp_code": "E01", "date": "2026-03-02", "status": "PRESENT", "late_minutes": 10, "half_day": False},
            {"emp_code": "E02", "date": "2026-03-02", "status": "WFH", "late_minutes": 0, "half_day": False},
        ])

    def test_live_explain_attendance_list(self):
        self._seed_basic_data()
        with patch("app.main.db", self.test_db):
            res = explain_endpoint(
                "attendance_list",
                emp_code="E01",
                date_from="2026-03-01",
                date_to="2026-03-31",
                status="PRESENT",
            )

        resp = ExplainResponse(**res)
        self.assertEqual(resp.endpoint, "attendance_list")
        self.assertEqual(resp.collection, "attendance_logs")
        exp = resp.explain
        self.assertEqual(exp.get("ok"), 1.0)
        self.assertIn("queryPlanner", exp)
        self.assertIn("executionStats", exp)

    def test_live_explain_employee_monthly(self):
        self._seed_basic_data()
        with patch("app.main.db", self.test_db):
            res = explain_endpoint("employee_monthly", emp_code="E01", month="2026-03")

        resp = ExplainResponse(**res)
        self.assertEqual(resp.endpoint, "employee_monthly")
        self.assertEqual(resp.collection, "attendance_logs")
        exp = resp.explain
        self.assertEqual(exp.get("ok"), 1.0)
        # Either top-level executionStats or stages contains execution stats
        has_stats = "executionStats" in exp or "stages" in exp
        self.assertTrue(has_stats)

    def test_live_explain_department_summary(self):
        self._seed_basic_data()
        with patch("app.main.db", self.test_db):
            res = explain_endpoint("department_summary", month="2026-03", department="Engineering")

        resp = ExplainResponse(**res)
        self.assertEqual(resp.endpoint, "department_summary")
        self.assertEqual(resp.collection, "employees")
        exp = resp.explain
        self.assertEqual(exp.get("ok"), 1.0)
        has_stats = "executionStats" in exp or "stages" in exp
        self.assertTrue(has_stats)

    def test_live_explain_late_leaderboard(self):
        self._seed_basic_data()
        with patch("app.main.db", self.test_db):
            res = explain_endpoint("late_leaderboard", month="2026-03", limit=10, department="Engineering")

        resp = ExplainResponse(**res)
        self.assertEqual(resp.endpoint, "late_leaderboard")
        self.assertEqual(resp.collection, "attendance_logs")
        exp = resp.explain
        self.assertEqual(exp.get("ok"), 1.0)
        has_stats = "executionStats" in exp or "stages" in exp
        self.assertTrue(has_stats)

    def test_live_explain_department_trend(self):
        self._seed_basic_data()
        with patch("app.main.db", self.test_db):
            res = explain_endpoint(
                "department_trend",
                department="Engineering",
                from_date="2026-03-01",
                to_date="2026-03-07",
            )

        resp = ExplainResponse(**res)
        self.assertEqual(resp.endpoint, "department_trend")
        self.assertEqual(resp.collection, "attendance_logs")
        exp = resp.explain
        self.assertEqual(exp.get("ok"), 1.0)
        has_stats = "executionStats" in exp or "stages" in exp
        self.assertTrue(has_stats)


if __name__ == "__main__":
    unittest.main()
