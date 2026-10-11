"""Focused unit and mocked endpoint tests for Phase 4."""
import unittest
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

from fastapi import HTTPException
from pydantic import ValidationError
from pymongo.errors import DuplicateKeyError

from app.main import (
    IST,
    EmployeeCreate,
    PunchInRequest,
    compute_half_day,
    compute_late_minutes,
    compute_overtime,
    compute_work_hours,
    create_employee,
    determine_attendance_date,
    list_attendance,
    list_employees,
    punch_in,
    serialize_attendance_doc,
    serialize_employee_doc,
)


class TestValidationAndModels(unittest.TestCase):
    def test_employee_create_valid(self):
        emp = EmployeeCreate(
            emp_code="EMP0001",
            name="Asha Rao",
            email="asha@example.com",
            department="Engineering",
            shift_start="09:30",
            shift_end="18:30",
            joined_on="2026-01-05",
        )
        self.assertEqual(emp.emp_code, "EMP0001")

    def test_employee_create_invalid_emp_code(self):
        with self.assertRaises(ValidationError):
            EmployeeCreate(
                emp_code="INVALID",
                name="Asha Rao",
                email="asha@example.com",
                department="Engineering",
                joined_on="2026-01-05",
            )

    def test_employee_create_identical_shifts_rejected(self):
        with self.assertRaises(ValidationError):
            EmployeeCreate(
                emp_code="EMP0001",
                name="Asha Rao",
                email="asha@example.com",
                department="Engineering",
                shift_start="09:30",
                shift_end="09:30",
                joined_on="2026-01-05",
            )

    def test_punch_in_request_valid(self):
        p = PunchInRequest(emp_code="EMP0001", punched_at=1783312500000, status="WFH")
        self.assertEqual(p.status, "WFH")

    def test_punch_in_request_rejects_seconds(self):
        with self.assertRaises(ValidationError):
            PunchInRequest(emp_code="EMP0001", punched_at=1783312500)

    def test_punch_in_request_rejects_floats(self):
        with self.assertRaises(ValidationError):
            PunchInRequest(emp_code="EMP0001", punched_at=1783312500000.5)

    def test_punch_in_request_rejects_invalid_status(self):
        with self.assertRaises(ValidationError):
            PunchInRequest(emp_code="EMP0001", status="ABSENT")


class TestDerivedCalculations(unittest.TestCase):
    def test_determine_attendance_date_standard_shift(self):
        p_ist = datetime(2026, 7, 6, 9, 28, 0, tzinfo=IST)
        d = determine_attendance_date(p_ist, "09:30", "18:30")
        self.assertEqual(d, "2026-07-06")

    def test_determine_attendance_date_overnight_shift_before_midnight(self):
        # 22:00 - 06:00 shift punched in at 21:55 IST on 2026-07-06
        p_ist = datetime(2026, 7, 6, 21, 55, 0, tzinfo=IST)
        d = determine_attendance_date(p_ist, "22:00", "06:00")
        self.assertEqual(d, "2026-07-06")

    def test_determine_attendance_date_overnight_shift_after_midnight(self):
        # 22:00 - 06:00 shift punched in at 05:45 IST on 2026-07-07 (earlier than shift_end 06:00)
        p_ist = datetime(2026, 7, 7, 5, 45, 0, tzinfo=IST)
        d = determine_attendance_date(p_ist, "22:00", "06:00")
        self.assertEqual(d, "2026-07-06")

    def test_late_minutes_grace_boundary(self):
        # Shift 09:30: 09:40:00 -> 0 (exactly 10 min grace)
        t1 = datetime(2026, 7, 6, 9, 40, 0, tzinfo=IST)
        self.assertEqual(compute_late_minutes(t1, "09:30", "2026-07-06"), 0)

        # Shift 09:30: 09:40:01 -> 10 (strictly more than 10 min -> floor from shift start)
        t2 = datetime(2026, 7, 6, 9, 40, 1, tzinfo=IST)
        self.assertEqual(compute_late_minutes(t2, "09:30", "2026-07-06"), 10)

        # Shift 09:30: 10:15:59 -> 45
        t3 = datetime(2026, 7, 6, 10, 15, 59, tzinfo=IST)
        self.assertEqual(compute_late_minutes(t3, "09:30", "2026-07-06"), 45)

    def test_overtime_minimum_threshold(self):
        # Shift ends 18:30. Punch out 18:59 (29 min -> under 30 min minimum) -> 0
        t1 = datetime(2026, 7, 6, 18, 59, 0, tzinfo=IST)
        self.assertEqual(compute_overtime(t1, "18:30", "09:30", "2026-07-06"), 0)

        # Punch out 19:00 (30 min -> >= 30 min) -> 30
        t2 = datetime(2026, 7, 6, 19, 0, 0, tzinfo=IST)
        self.assertEqual(compute_overtime(t2, "18:30", "09:30", "2026-07-06"), 30)

        # Punch out 19:10 (40 min) -> 40
        t3 = datetime(2026, 7, 6, 19, 10, 0, tzinfo=IST)
        self.assertEqual(compute_overtime(t3, "18:30", "09:30", "2026-07-06"), 40)

    def test_overtime_overnight_shift(self):
        # Overnight shift 22:00 - 06:00 on attendance date 2026-07-06. Ends 2026-07-07 06:00.
        # Punch out 2026-07-07 06:40 (40 min overtime) -> 40
        t = datetime(2026, 7, 7, 6, 40, 0, tzinfo=IST)
        self.assertEqual(compute_overtime(t, "06:00", "22:00", "2026-07-06"), 40)

    def test_work_hours_round_half_up(self):
        p_in = datetime(2026, 7, 6, 3, 58, 0, tzinfo=timezone.utc)
        p_out = datetime(2026, 7, 6, 13, 5, 0, tzinfo=timezone.utc)
        self.assertEqual(compute_work_hours(p_in, p_out), 9.12)

    def test_half_day_rule(self):
        self.assertTrue(compute_half_day(3.50))
        self.assertTrue(compute_half_day(4.49))
        self.assertFalse(compute_half_day(4.50))
        self.assertFalse(compute_half_day(9.12))


class TestMockedEndpoints(unittest.TestCase):
    @patch("app.main.db")
    def test_create_employee_duplicate_raises_409(self, mock_db):
        mock_db.employees.insert_one.side_effect = DuplicateKeyError("E11000 duplicate key error")
        body = EmployeeCreate(
            emp_code="EMP0001",
            name="Asha Rao",
            email="asha@example.com",
            department="Engineering",
            joined_on="2026-01-05",
        )
        with self.assertRaises(HTTPException) as ctx:
            create_employee(body)
        self.assertEqual(ctx.exception.status_code, 409)

    @patch("app.main.db")
    def test_create_employee_success_serializes_epoch_ms(self, mock_db):
        mock_db.employees.insert_one.return_value = MagicMock()
        body = EmployeeCreate(
            emp_code="EMP0001",
            name="Asha Rao",
            email="asha@example.com",
            department="Engineering",
            joined_on="2026-01-05",
        )
        res = create_employee(body)
        self.assertNotIn("_id", res)
        self.assertIsInstance(res["created_at"], int)
        self.assertGreaterEqual(res["created_at"], 100000000000)

    @patch("app.main.db")
    def test_list_employees_pagination_and_sorting(self, mock_db):
        mock_cursor = MagicMock()
        mock_cursor.sort.return_value = mock_cursor
        mock_cursor.skip.return_value = mock_cursor
        mock_cursor.limit.return_value = [
            {"emp_code": "EMP0001", "name": "Asha", "created_at": datetime.now(timezone.utc)}
        ]
        mock_db.employees.find.return_value = mock_cursor
        mock_db.employees.count_documents.return_value = 1

        res = list_employees(department="Engineering", page=2, page_size=10)
        self.assertEqual(res["total"], 1)
        self.assertEqual(res["page"], 2)
        mock_cursor.sort.assert_called_with([("emp_code", 1)])
        mock_cursor.skip.assert_called_with(10)  # (2 - 1) * 10
        mock_cursor.limit.assert_called_with(10)

    @patch("app.main.db")
    def test_punch_in_missing_employee_raises_404(self, mock_db):
        mock_db.employees.find_one.return_value = None
        req = PunchInRequest(emp_code="EMP9999")
        with self.assertRaises(HTTPException) as ctx:
            punch_in(req)
        self.assertEqual(ctx.exception.status_code, 404)

    @patch("app.main.db")
    def test_punch_in_duplicate_raises_409(self, mock_db):
        mock_db.employees.find_one.return_value = {
            "emp_code": "EMP0001",
            "shift_start": "09:30",
            "shift_end": "18:30",
        }
        mock_db.attendance_logs.insert_one.side_effect = DuplicateKeyError("duplicate")
        req = PunchInRequest(emp_code="EMP0001")
        with self.assertRaises(HTTPException) as ctx:
            punch_in(req)
        self.assertEqual(ctx.exception.status_code, 409)

    @patch("app.main.db")
    def test_punch_in_success_serializes_clean_contract(self, mock_db):
        mock_db.employees.find_one.return_value = {
            "emp_code": "EMP0001",
            "shift_start": "09:30",
            "shift_end": "18:30",
        }
        mock_db.attendance_logs.insert_one.return_value = MagicMock()
        # Punch at 10:05 IST (late 35 min)
        # 10:05 IST = 04:35 UTC on 2026-07-06
        dt = datetime(2026, 7, 6, 4, 35, 0, tzinfo=timezone.utc)
        epoch_ms = int(dt.timestamp() * 1000)

        req = PunchInRequest(emp_code="EMP0001", punched_at=epoch_ms, status="PRESENT")
        res = punch_in(req)
        self.assertEqual(res["date"], "2026-07-06")
        self.assertEqual(res["late_minutes"], 35)
        self.assertEqual(res["overtime_minutes"], 0)
        self.assertFalse(res["half_day"])
        self.assertIsNone(res["work_hours"])
        self.assertIsNone(res["punch_out"])
        self.assertEqual(res["punch_in"], epoch_ms)
        self.assertNotIn("id", res)
        self.assertNotIn("_id", res)

    @patch("app.main.db")
    def test_list_attendance_date_range_validation_422(self, mock_db):
        with self.assertRaises(HTTPException) as ctx:
            list_attendance(date_from="2026-07-10", date_to="2026-07-05")
        self.assertEqual(ctx.exception.status_code, 422)

    @patch("app.main.db")
    def test_list_attendance_pagination_and_compound_sort(self, mock_db):
        mock_cursor = MagicMock()
        mock_cursor.sort.return_value = mock_cursor
        mock_cursor.skip.return_value = mock_cursor
        mock_cursor.limit.return_value = []
        mock_db.attendance_logs.find.return_value = mock_cursor
        mock_db.attendance_logs.count_documents.return_value = 50

        res = list_attendance(emp_code="EMP0001", page=3, page_size=20)
        mock_cursor.sort.assert_called_with([("date", -1), ("emp_code", 1)])
        mock_cursor.skip.assert_called_with(40)  # (3 - 1) * 20
        mock_cursor.limit.assert_called_with(20)
        self.assertEqual(res["total"], 50)


if __name__ == "__main__":
    unittest.main()
