"""Comprehensive unit and mocked endpoint test suite for Phase 5 (Punch-Out and Regularization)."""
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

from fastapi import HTTPException
from pydantic import ValidationError

from app.main import (
    IST,
    PunchOutRequest,
    RegularizeRequest,
    punch_out,
    regularize_attendance,
    serialize_attendance_doc,
)


class TestPunchOutEndpoint(unittest.TestCase):
    @patch("app.main.db")
    def test_punch_out_unknown_employee_404(self, mock_db):
        mock_db.employees.find_one.return_value = None
        req = PunchOutRequest(emp_code="EMP9999")
        with self.assertRaises(HTTPException) as ctx:
            punch_out(req)
        self.assertEqual(ctx.exception.status_code, 404)

    @patch("app.main.db")
    def test_punch_out_no_record_found_404(self, mock_db):
        mock_db.employees.find_one.return_value = {"emp_code": "EMP0001"}
        mock_cursor = MagicMock()
        mock_cursor.sort.return_value = mock_cursor
        mock_cursor.limit.return_value = []
        mock_db.attendance_logs.find.return_value = mock_cursor

        req = PunchOutRequest(emp_code="EMP0001")
        with self.assertRaises(HTTPException) as ctx:
            punch_out(req)
        self.assertEqual(ctx.exception.status_code, 404)

    @patch("app.main.db")
    def test_punch_out_already_punched_out_409(self, mock_db):
        mock_db.employees.find_one.return_value = {"emp_code": "EMP0001"}
        p_in = datetime(2026, 7, 6, 3, 58, 0, tzinfo=timezone.utc)
        p_out = datetime(2026, 7, 6, 13, 5, 0, tzinfo=timezone.utc)
        mock_cursor = MagicMock()
        mock_cursor.sort.return_value = mock_cursor
        mock_cursor.limit.return_value = [{"_id": "id1", "punch_in": p_in, "punch_out": p_out}]
        mock_db.attendance_logs.find.return_value = mock_cursor

        req = PunchOutRequest(emp_code="EMP0001")
        with self.assertRaises(HTTPException) as ctx:
            punch_out(req)
        self.assertEqual(ctx.exception.status_code, 409)

    @patch("app.main.db")
    def test_punch_out_before_punch_in_422(self, mock_db):
        mock_db.employees.find_one.return_value = {"emp_code": "EMP0001"}
        p_in = datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc)
        mock_cursor = MagicMock()
        mock_cursor.sort.return_value = mock_cursor
        mock_cursor.limit.return_value = [{"_id": "id1", "punch_in": p_in, "punch_out": None}]
        mock_db.attendance_logs.find.return_value = mock_cursor

        # Punch out at 03:59:00 UTC (before punch-in)
        req = PunchOutRequest(emp_code="EMP0001", punched_at=int(p_in.timestamp() * 1000) - 10000)
        with self.assertRaises(HTTPException) as ctx:
            punch_out(req)
        self.assertEqual(ctx.exception.status_code, 422)

    @patch("app.main.db")
    def test_punch_out_exceeds_24_hours_422(self, mock_db):
        mock_db.employees.find_one.return_value = {"emp_code": "EMP0001"}
        p_in = datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc)
        mock_cursor = MagicMock()
        mock_cursor.sort.return_value = mock_cursor
        mock_cursor.limit.return_value = [{"_id": "id1", "punch_in": p_in, "punch_out": None}]
        mock_db.attendance_logs.find.return_value = mock_cursor

        # 25 hours later
        p_out_ms = int((p_in + timedelta(hours=25)).timestamp() * 1000)
        req = PunchOutRequest(emp_code="EMP0001", punched_at=p_out_ms)
        with self.assertRaises(HTTPException) as ctx:
            punch_out(req)
        self.assertEqual(ctx.exception.status_code, 422)

    @patch("app.main.db")
    def test_punch_out_success_computes_derived_fields(self, mock_db):
        mock_db.employees.find_one.return_value = {
            "emp_code": "EMP0001",
            "shift_start": "09:30",
            "shift_end": "18:30",
        }
        # Sample 1: in 03:58:00Z, out 13:05:00Z (9.12 hours, 0 overtime, False half day)
        p_in = datetime(2026, 7, 6, 3, 58, 0, tzinfo=timezone.utc)
        p_out = datetime(2026, 7, 6, 13, 5, 0, tzinfo=timezone.utc)

        mock_cursor = MagicMock()
        mock_cursor.sort.return_value = mock_cursor
        mock_cursor.limit.return_value = [
            {"_id": "rec1", "emp_code": "EMP0001", "date": "2026-07-06", "punch_in": p_in, "punch_out": None}
        ]
        mock_db.attendance_logs.find.return_value = mock_cursor

        mock_db.attendance_logs.find_one_and_update.return_value = {
            "_id": "rec1",
            "emp_code": "EMP0001",
            "date": "2026-07-06",
            "status": "PRESENT",
            "punch_in": p_in,
            "punch_out": p_out,
            "work_hours": 9.12,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False,
            "history": [],
        }

        req = PunchOutRequest(emp_code="EMP0001", punched_at=int(p_out.timestamp() * 1000))
        res = punch_out(req)
        self.assertEqual(res["work_hours"], 9.12)
        self.assertEqual(res["overtime_minutes"], 0)
        self.assertFalse(res["half_day"])
        self.assertEqual(res["punch_out"], int(p_out.timestamp() * 1000))
        self.assertNotIn("_id", res)

    @patch("app.main.db")
    def test_punch_out_race_condition_second_request_409(self, mock_db):
        mock_db.employees.find_one.return_value = {
            "emp_code": "EMP0001",
            "shift_start": "09:30",
            "shift_end": "18:30",
        }
        p_in = datetime(2026, 7, 6, 3, 58, 0, tzinfo=timezone.utc)
        p_out = datetime(2026, 7, 6, 13, 5, 0, tzinfo=timezone.utc)

        mock_cursor = MagicMock()
        mock_cursor.sort.return_value = mock_cursor
        mock_cursor.limit.return_value = [
            {"_id": "rec1", "emp_code": "EMP0001", "date": "2026-07-06", "punch_in": p_in, "punch_out": None}
        ]
        mock_db.attendance_logs.find.return_value = mock_cursor
        # Another request updated it first, so atomic find_one_and_update returns None
        mock_db.attendance_logs.find_one_and_update.return_value = None

        req = PunchOutRequest(emp_code="EMP0001", punched_at=int(p_out.timestamp() * 1000))
        with self.assertRaises(HTTPException) as ctx:
            punch_out(req)
        self.assertEqual(ctx.exception.status_code, 409)


class TestRegularizeEndpoint(unittest.TestCase):
    @patch("app.main.db")
    def test_regularize_unknown_employee_404(self, mock_db):
        mock_db.employees.find_one.return_value = None
        req = RegularizeRequest(reason="Fixing log", regularized_by="admin")
        with self.assertRaises(HTTPException) as ctx:
            regularize_attendance("EMP9999", "2026-07-06", req)
        self.assertEqual(ctx.exception.status_code, 404)

    @patch("app.main.db")
    def test_regularize_no_record_404(self, mock_db):
        mock_db.employees.find_one.return_value = {"emp_code": "EMP0001"}
        mock_db.attendance_logs.find_one.return_value = None
        req = RegularizeRequest(reason="Fixing log", regularized_by="admin")
        with self.assertRaises(HTTPException) as ctx:
            regularize_attendance("EMP0001", "2026-07-06", req)
        self.assertEqual(ctx.exception.status_code, 404)

    @patch("app.main.db")
    def test_regularize_absent_with_punches_422(self, mock_db):
        mock_db.employees.find_one.return_value = {"emp_code": "EMP0001"}
        mock_db.attendance_logs.find_one.return_value = {"emp_code": "EMP0001", "date": "2026-07-06"}
        req = RegularizeRequest(status="ABSENT", punch_in=1783312500000, reason="marking absent", regularized_by="admin")
        with self.assertRaises(HTTPException) as ctx:
            regularize_attendance("EMP0001", "2026-07-06", req)
        self.assertEqual(ctx.exception.status_code, 422)

    @patch("app.main.db")
    def test_regularize_changes_nothing_422(self, mock_db):
        p_in = datetime(2026, 7, 6, 3, 58, 0, tzinfo=timezone.utc)
        mock_db.employees.find_one.return_value = {
            "emp_code": "EMP0001",
            "shift_start": "09:30",
            "shift_end": "18:30",
        }
        mock_db.attendance_logs.find_one.return_value = {
            "_id": "rec1",
            "emp_code": "EMP0001",
            "date": "2026-07-06",
            "status": "PRESENT",
            "punch_in": p_in,
            "punch_out": None,
            "work_hours": None,
            "late_minutes": 0,
            "overtime_minutes": 0,
            "half_day": False,
            "history": [],
        }
        # Sending identical punch_in and status
        req = RegularizeRequest(
            status="PRESENT",
            punch_in=int(p_in.timestamp() * 1000),
            reason="redundant edit",
            regularized_by="admin",
        )
        with self.assertRaises(HTTPException) as ctx:
            regularize_attendance("EMP0001", "2026-07-06", req)
        self.assertEqual(ctx.exception.status_code, 422)

    @patch("app.main.db")
    def test_regularize_punch_in_wrong_date_422(self, mock_db):
        mock_db.employees.find_one.return_value = {
            "emp_code": "EMP0001",
            "shift_start": "09:30",
            "shift_end": "18:30",
        }
        mock_db.attendance_logs.find_one.return_value = {
            "_id": "rec1",
            "emp_code": "EMP0001",
            "date": "2026-07-06",
            "status": "PRESENT",
            "punch_in": datetime(2026, 7, 6, 3, 58, 0, tzinfo=timezone.utc),
        }
        # Punch in on 2026-07-07 instead of 2026-07-06
        p_wrong = datetime(2026, 7, 7, 4, 0, 0, tzinfo=timezone.utc)
        req = RegularizeRequest(
            punch_in=int(p_wrong.timestamp() * 1000),
            reason="wrong day punch",
            regularized_by="admin",
        )
        with self.assertRaises(HTTPException) as ctx:
            regularize_attendance("EMP0001", "2026-07-06", req)
        self.assertEqual(ctx.exception.status_code, 422)

    @patch("app.main.db")
    def test_regularize_success_sample_8(self, mock_db):
        # Corresponds to Sample 8 in DATA_MODEL.md:
        # HR corrects punch-in from 10:05 IST (04:35Z) to 09:28 IST (03:58Z).
        # late_minutes changes from 35 to 0; work_hours changes from 8.42 to 9.03.
        old_in = datetime(2026, 7, 7, 4, 35, 0, tzinfo=timezone.utc)
        old_out = datetime(2026, 7, 7, 13, 0, 0, tzinfo=timezone.utc)
        new_in = datetime(2026, 7, 7, 3, 58, 0, tzinfo=timezone.utc)

        mock_db.employees.find_one.return_value = {
            "emp_code": "EMP0001",
            "shift_start": "09:30",
            "shift_end": "18:30",
        }
        mock_db.attendance_logs.find_one.return_value = {
            "_id": "rec8",
            "emp_code": "EMP0001",
            "date": "2026-07-07",
            "status": "PRESENT",
            "punch_in": old_in,
            "punch_out": old_out,
            "work_hours": 8.42,
            "late_minutes": 35,
            "overtime_minutes": 0,
            "half_day": False,
            "history": [],
        }

        def mock_update(filter_q, update_doc, return_document):
            # Assert only changed fields are in history
            hist = update_doc["$push"]["history"]
            changes = hist["changes"]
            self.assertIn("punch_in", changes)
            self.assertIn("late_minutes", changes)
            self.assertIn("work_hours", changes)
            self.assertNotIn("punch_out", changes)
            self.assertNotIn("status", changes)
            self.assertEqual(changes["late_minutes"], {"from": 35, "to": 0})
            self.assertEqual(changes["work_hours"], {"from": 8.42, "to": 9.03})

            return {
                "_id": "rec8",
                "emp_code": "EMP0001",
                "date": "2026-07-07",
                "status": "PRESENT",
                "punch_in": new_in,
                "punch_out": old_out,
                "work_hours": 9.03,
                "late_minutes": 0,
                "overtime_minutes": 0,
                "half_day": False,
                "history": [hist],
            }

        mock_db.attendance_logs.find_one_and_update.side_effect = mock_update

        req = RegularizeRequest(
            punch_in=int(new_in.timestamp() * 1000),
            reason="biometric glitch",
            regularized_by="hr.admin",
        )
        res = regularize_attendance("EMP0001", "2026-07-07", req)
        self.assertEqual(res["late_minutes"], 0)
        self.assertEqual(res["work_hours"], 9.03)
        self.assertEqual(len(res["history"]), 1)
        self.assertEqual(res["history"][0]["by"], "hr.admin")

    @patch("app.main.db")
    def test_regularize_concurrency_conflict_409(self, mock_db):
        mock_db.employees.find_one.return_value = {
            "emp_code": "EMP0001",
            "shift_start": "09:30",
            "shift_end": "18:30",
        }
        mock_db.attendance_logs.find_one.return_value = {
            "_id": "rec1",
            "emp_code": "EMP0001",
            "date": "2026-07-06",
            "status": "PRESENT",
            "punch_in": datetime(2026, 7, 6, 4, 35, 0, tzinfo=timezone.utc),
            "history": [],
        }
        # Another request updated history in the meantime -> find_one_and_update returns None
        mock_db.attendance_logs.find_one_and_update.return_value = None

        new_in = datetime(2026, 7, 6, 3, 58, 0, tzinfo=timezone.utc)
        req = RegularizeRequest(
            punch_in=int(new_in.timestamp() * 1000),
            reason="fixing punch",
            regularized_by="admin",
        )
        with self.assertRaises(HTTPException) as ctx:
            regularize_attendance("EMP0001", "2026-07-06", req)
        self.assertEqual(ctx.exception.status_code, 409)

    def test_regularize_request_rejects_derived_fields_422(self):
        with self.assertRaises(ValidationError):
            RegularizeRequest(
                reason="valid reason",
                regularized_by="hr.admin",
                work_hours=8.5,
            )

    @patch("app.main.db")
    def test_regularize_to_absent_clears_punches_and_derived(self, mock_db):
        mock_db.employees.find_one.return_value = {
            "emp_code": "EMP0001",
            "shift_start": "09:30",
            "shift_end": "18:30",
        }
        p_in = datetime(2026, 7, 6, 4, 35, 0, tzinfo=timezone.utc)
        p_out = datetime(2026, 7, 6, 13, 40, 0, tzinfo=timezone.utc)
        mock_db.attendance_logs.find_one.return_value = {
            "_id": "rec1",
            "emp_code": "EMP0001",
            "date": "2026-07-06",
            "status": "PRESENT",
            "punch_in": p_in,
            "punch_out": p_out,
            "work_hours": 9.08,
            "late_minutes": 35,
            "overtime_minutes": 40,
            "half_day": False,
            "history": [],
        }

        captured_update = {}

        def fake_find_one_and_update(filt, upd, return_document=None):
            captured_update["filt"] = filt
            captured_update["upd"] = upd
            doc = {
                "_id": "rec1",
                "emp_code": "EMP0001",
                "date": "2026-07-06",
                "status": "ABSENT",
                "punch_in": None,
                "punch_out": None,
                "work_hours": None,
                "late_minutes": 0,
                "overtime_minutes": 0,
                "half_day": False,
                "history": [upd["$push"]["history"]],
            }
            return doc

        mock_db.attendance_logs.find_one_and_update.side_effect = fake_find_one_and_update

        req = RegularizeRequest(
            status="ABSENT",
            reason="employee did not attend",
            regularized_by="manager",
        )
        res = regularize_attendance("EMP0001", "2026-07-06", req)
        self.assertEqual(res["status"], "ABSENT")
        self.assertIsNone(res["punch_in"])
        self.assertIsNone(res["punch_out"])
        self.assertIsNone(res["work_hours"])
        self.assertEqual(res["late_minutes"], 0)
        self.assertEqual(res["overtime_minutes"], 0)
        self.assertFalse(res["half_day"])

        changes = captured_update["upd"]["$push"]["history"]["changes"]
        self.assertIn("status", changes)
        self.assertIn("punch_in", changes)
        self.assertIn("punch_out", changes)
        self.assertIn("work_hours", changes)
        self.assertIn("late_minutes", changes)
        self.assertIn("overtime_minutes", changes)

    def test_serialize_attendance_doc_naive_datetime_exact_epoch_ms(self):
        # PyMongo returns naive UTC datetime by default
        naive_utc = datetime(2026, 7, 6, 3, 58, 0)
        doc = {
            "emp_code": "EMP0001",
            "date": "2026-07-06",
            "status": "PRESENT",
            "punch_in": naive_utc,
            "punch_out": None,
        }
        serialized = serialize_attendance_doc(doc)
        # 2026-07-06T03:58:00Z in epoch ms is 1783310280000
        self.assertEqual(serialized["punch_in"], 1783310280000)
        self.assertIsNone(serialized["work_hours"])
        self.assertEqual(serialized["late_minutes"], 0)
        self.assertEqual(serialized["overtime_minutes"], 0)
        self.assertFalse(serialized["half_day"])
        self.assertEqual(serialized["history"], [])


if __name__ == "__main__":
    unittest.main()
