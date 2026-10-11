"""Unit tests for MongoDB index definitions and startup initialization."""
import unittest
from unittest.mock import MagicMock
from pymongo import ASCENDING, DESCENDING, IndexModel

from app.main import INDEX_SPECS, init_indexes


class TestIndexDefinitions(unittest.TestCase):
    def test_index_specs_structure(self):
        self.assertIn("employees", INDEX_SPECS)
        self.assertIn("attendance_logs", INDEX_SPECS)

    def test_employees_unique_emp_code(self):
        emp_indexes = INDEX_SPECS["employees"]
        unique_emp = next((idx for idx in emp_indexes if idx.get("name") == "uniq_emp_code"), None)
        self.assertIsNotNone(unique_emp, "uniq_emp_code index must be defined")
        self.assertEqual(unique_emp["keys"], [("emp_code", ASCENDING)])
        self.assertTrue(unique_emp.get("unique"), "uniq_emp_code must be unique")

    def test_employees_department_compound_index(self):
        emp_indexes = INDEX_SPECS["employees"]
        dept_idx = next((idx for idx in emp_indexes if idx.get("name") == "idx_dept_emp_code"), None)
        self.assertIsNotNone(dept_idx, "idx_dept_emp_code index must be defined")
        self.assertEqual(dept_idx["keys"], [("department", ASCENDING), ("emp_code", ASCENDING)])

    def test_attendance_logs_unique_emp_code_date(self):
        att_indexes = INDEX_SPECS["attendance_logs"]
        uniq_compound = next((idx for idx in att_indexes if idx.get("name") == "uniq_emp_code_date"), None)
        self.assertIsNotNone(uniq_compound, "uniq_emp_code_date compound index must be defined")
        self.assertEqual(uniq_compound["keys"], [("emp_code", ASCENDING), ("date", ASCENDING)])
        self.assertTrue(uniq_compound.get("unique"), "uniq_emp_code_date must be unique")

    def test_attendance_logs_sorting_and_filtering_indexes(self):
        att_indexes = INDEX_SPECS["attendance_logs"]
        sort_idx = next((idx for idx in att_indexes if idx.get("name") == "idx_date_desc_emp_code_asc"), None)
        self.assertIsNotNone(sort_idx, "idx_date_desc_emp_code_asc index must be defined")
        self.assertEqual(sort_idx["keys"], [("date", DESCENDING), ("emp_code", ASCENDING)])

        date_asc = next((idx for idx in att_indexes if idx.get("name") == "idx_date_asc"), None)
        self.assertIsNotNone(date_asc, "idx_date_asc index must be defined")
        self.assertEqual(date_asc["keys"], [("date", ASCENDING)])

    def test_init_indexes_with_mock_db(self):
        mock_db = {}
        mock_emp_coll = MagicMock()
        mock_att_coll = MagicMock()
        mock_db["employees"] = mock_emp_coll
        mock_db["attendance_logs"] = mock_att_coll

        result = init_indexes(mock_db)
        self.assertTrue(result)
        self.assertTrue(mock_emp_coll.create_indexes.called)
        self.assertTrue(mock_att_coll.create_indexes.called)

        # Verify created models
        emp_models = mock_emp_coll.create_indexes.call_args[0][0]
        self.assertEqual(len(emp_models), 2)
        self.assertTrue(all(isinstance(m, IndexModel) for m in emp_models))

        att_models = mock_att_coll.create_indexes.call_args[0][0]
        self.assertEqual(len(att_models), 3)
        self.assertTrue(all(isinstance(m, IndexModel) for m in att_models))

    def test_init_indexes_handles_none_db_safely(self):
        result = init_indexes(None)
        # When target_db is None and app db is unconfigured/None, should return False without crashing
        self.assertFalse(result)

    def test_init_indexes_handles_db_exception_safely(self):
        failing_db = MagicMock()
        failing_db.__getitem__.side_effect = Exception("DB Connection Refused")
        result = init_indexes(failing_db)
        self.assertFalse(result)


if __name__ == "__main__":
    unittest.main()
