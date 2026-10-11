"""Comprehensive unit, mocked, and real endpoint test suite for Phase 8 (Late Leaderboard API)."""
import re
import unittest
from unittest.mock import MagicMock, patch

from fastapi import HTTPException
from pydantic import ValidationError

from app.main import (
    Leaderboard,
    LeaderboardItem,
    build_late_leaderboard_pipeline,
    get_late_leaderboard,
)


class TestLateLeaderboardPipelineStructure(unittest.TestCase):
    def test_pipeline_stages_without_department(self):
        pipeline = build_late_leaderboard_pipeline("2026-07", limit=10)
        # Verify 8 stages
        self.assertEqual(len(pipeline), 8)
        # Stage 0: $match date in month and late_minutes > 0
        match_stage = pipeline[0]["$match"]
        self.assertEqual(match_stage["date"]["$gte"], "2026-07-01")
        self.assertEqual(match_stage["date"]["$lte"], "2026-07-31")
        self.assertEqual(match_stage["late_minutes"], {"$gt": 0})

        # Stage 1: $group by emp_code
        group_stage = pipeline[1]["$group"]
        self.assertEqual(group_stage["_id"], "$emp_code")
        self.assertEqual(group_stage["total_late_minutes"], {"$sum": "$late_minutes"})
        self.assertEqual(group_stage["late_count"], {"$sum": 1})

        # Stage 2: $lookup from employees
        lookup_stage = pipeline[2]["$lookup"]
        self.assertEqual(lookup_stage["from"], "employees")
        self.assertEqual(lookup_stage["localField"], "_id")
        self.assertEqual(lookup_stage["foreignField"], "emp_code")
        self.assertEqual(lookup_stage["as"], "emp")

        # Stage 3: $unwind emp
        unwind_stage = pipeline[3]["$unwind"]
        self.assertEqual(unwind_stage, "$emp")

        # Stage 4: $setWindowFields with $rank and NO emp_code in sortBy
        win_stage = pipeline[4]["$setWindowFields"]
        self.assertEqual(win_stage["sortBy"], {"total_late_minutes": -1})
        self.assertNotIn("emp_code", win_stage["sortBy"])
        self.assertEqual(win_stage["output"]["rank"], {"$rank": {}})

        # Stage 5: $match rank <= limit
        match_rank = pipeline[5]["$match"]
        self.assertEqual(match_rank["rank"], {"$lte": 10})

        # Stage 6: $project fields and exclude _id
        proj_stage = pipeline[6]["$project"]
        self.assertEqual(proj_stage["_id"], 0)
        self.assertEqual(proj_stage["rank"], 1)
        self.assertEqual(proj_stage["emp_code"], "$_id")
        self.assertEqual(proj_stage["name"], "$emp.name")
        self.assertEqual(proj_stage["department"], "$emp.department")
        self.assertEqual(proj_stage["total_late_minutes"], 1)
        self.assertEqual(proj_stage["late_count"], 1)

        # Stage 7: $sort outside window by total_late_minutes desc, emp_code asc
        sort_stage = pipeline[7]["$sort"]
        self.assertEqual(sort_stage, {"total_late_minutes": -1, "emp_code": 1})

    def test_pipeline_stages_with_department(self):
        pipeline = build_late_leaderboard_pipeline("2026-07", limit=5, department="Engineering")
        # 9 stages with department filter before window
        self.assertEqual(len(pipeline), 9)
        dept_match = pipeline[4]["$match"]
        self.assertEqual(dept_match, {"emp.department": "Engineering"})
        # window follows department match
        self.assertIn("$setWindowFields", pipeline[5])

    def test_month_parameter_regex_validation(self):
        pattern = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
        self.assertTrue(pattern.match("2026-07"))
        self.assertTrue(pattern.match("2026-01"))
        self.assertTrue(pattern.match("2026-12"))
        self.assertIsNone(pattern.match("2026-00"))
        self.assertIsNone(pattern.match("2026-13"))
        self.assertIsNone(pattern.match("2026/07"))
        self.assertIsNone(pattern.match("2026-7"))


class TestMockedLeaderboardEndpoint(unittest.TestCase):
    @patch("app.main.db")
    def test_get_leaderboard_database_down_503(self, mock_db):
        with patch("app.main.db", None):
            with self.assertRaises(HTTPException) as ctx:
                get_late_leaderboard(month="2026-07")
            self.assertEqual(ctx.exception.status_code, 503)

    @patch("app.main.db")
    def test_get_leaderboard_empty_results(self, mock_db):
        mock_db.attendance_logs.aggregate.return_value = []
        res = get_late_leaderboard(month="2026-07")
        self.assertEqual(res["month"], "2026-07")
        self.assertEqual(res["items"], [])

    @patch("app.main.db")
    def test_get_leaderboard_mocked_results_serializes_cleanly(self, mock_db):
        mock_db.attendance_logs.aggregate.return_value = [
            {
                "rank": 1,
                "emp_code": "E01",
                "name": "Alice",
                "department": "Engineering",
                "total_late_minutes": 50,
                "late_count": 2,
            },
            {
                "rank": 2,
                "emp_code": "E02",
                "name": "Bob",
                "department": "Engineering",
                "total_late_minutes": 30,
                "late_count": 1,
            },
        ]
        res = get_late_leaderboard(month="2026-07")
        self.assertEqual(res["month"], "2026-07")
        self.assertEqual(len(res["items"]), 2)
        self.assertEqual(res["items"][0]["rank"], 1)
        self.assertEqual(res["items"][0]["emp_code"], "E01")
        self.assertEqual(res["items"][0]["name"], "Alice")
        self.assertEqual(res["items"][0]["department"], "Engineering")
        self.assertEqual(res["items"][0]["total_late_minutes"], 50)
        self.assertEqual(res["items"][0]["late_count"], 2)

    def test_leaderboard_item_forbids_extra_fields(self):
        with self.assertRaises(ValidationError):
            LeaderboardItem(
                rank=1,
                emp_code="E01",
                name="Alice",
                department="Engineering",
                total_late_minutes=50,
                late_count=2,
                _id="some_id",
            )

    def test_leaderboard_item_requires_rank_ge_1(self):
        with self.assertRaises(ValidationError):
            LeaderboardItem(
                rank=0,
                emp_code="E01",
                name="Alice",
                department="Engineering",
                total_late_minutes=50,
                late_count=2,
            )


class TestRealMongoLeaderboardExecution(unittest.TestCase):
    """Executes leaderboard aggregation against local MongoDB (localhost:27017).
    Atlas integration remains unverified.
    """

    def setUp(self):
        from pymongo import MongoClient
        try:
            self.client = MongoClient("mongodb://localhost:27017", serverSelectionTimeoutMS=500)
            self.client.admin.command("ping")
            self.test_db = self.client["test_audit_phase8_local"]
            self.test_db.employees.drop()
            self.test_db.attendance_logs.drop()
        except Exception:
            raise unittest.SkipTest("Local MongoDB not available for live aggregation execution")

    def tearDown(self):
        if hasattr(self, "client"):
            self.client.drop_database("test_audit_phase8_local")
            self.client.close()

    def test_live_leaderboard_ranking_ties_and_limit(self):
        # 1. Insert employees
        self.test_db.employees.insert_many([
            {"emp_code": "E01", "name": "Alice", "department": "Engineering"},
            {"emp_code": "E02", "name": "Bob", "department": "Engineering"},
            {"emp_code": "E03", "name": "Charlie", "department": "Engineering"},
            {"emp_code": "E04", "name": "David", "department": "Sales"},
            {"emp_code": "E05", "name": "Eve", "department": "Sales"},
            {"emp_code": "E06", "name": "Frank", "department": "Sales"},
        ])

        # 2. Insert attendance logs for July 2026:
        self.test_db.attendance_logs.insert_many([
            # E01: 50 late minutes (two records: 20 + 30)
            {"emp_code": "E01", "date": "2026-07-06", "late_minutes": 20},
            {"emp_code": "E01", "date": "2026-07-07", "late_minutes": 30},
            # E02: 50 late minutes (one record, tied with E01!)
            {"emp_code": "E02", "date": "2026-07-06", "late_minutes": 50},
            # E03: 40 late minutes
            {"emp_code": "E03", "date": "2026-07-08", "late_minutes": 40},
            # E04: 30 late minutes
            {"emp_code": "E04", "date": "2026-07-09", "late_minutes": 30},
            # E05: 30 late minutes (tied with E04!)
            {"emp_code": "E05", "date": "2026-07-10", "late_minutes": 30},
            # E06: 20 late minutes (weekend log, should count!)
            {"emp_code": "E06", "date": "2026-07-11", "late_minutes": 20},
            # Orphan log: employee not in employees collection (must be ignored)
            {"emp_code": "E99", "date": "2026-07-06", "late_minutes": 100},
            # Zero late minutes log: must be ignored
            {"emp_code": "E01", "date": "2026-07-13", "late_minutes": 0},
            # Another month log: must be ignored
            {"emp_code": "E03", "date": "2026-08-01", "late_minutes": 200},
        ])

        # A. Test full leaderboard with limit=10:
        pipeline = build_late_leaderboard_pipeline("2026-07", limit=10)
        items = list(self.test_db.attendance_logs.aggregate(pipeline))

        # Expected:
        # Rank 1: E01 (50 min), E02 (50 min) - ordered by emp_code: E01 then E02
        # Rank 3: E03 (40 min) - rank 2 skipped!
        # Rank 4: E04 (30 min), E05 (30 min) - ordered by emp_code: E04 then E05
        # Rank 6: E06 (20 min) - rank 5 skipped!
        self.assertEqual(len(items), 6)

        # Check E01 & E02 tie
        self.assertEqual(items[0]["emp_code"], "E01")
        self.assertEqual(items[0]["rank"], 1)
        self.assertEqual(items[0]["total_late_minutes"], 50)
        self.assertEqual(items[0]["late_count"], 2)

        self.assertEqual(items[1]["emp_code"], "E02")
        self.assertEqual(items[1]["rank"], 1)
        self.assertEqual(items[1]["total_late_minutes"], 50)
        self.assertEqual(items[1]["late_count"], 1)

        # Check E03 rank 3 (rank 2 skipped)
        self.assertEqual(items[2]["emp_code"], "E03")
        self.assertEqual(items[2]["rank"], 3)
        self.assertEqual(items[2]["total_late_minutes"], 40)

        # Check E04 & E05 rank 4
        self.assertEqual(items[3]["emp_code"], "E04")
        self.assertEqual(items[3]["rank"], 4)
        self.assertEqual(items[4]["emp_code"], "E05")
        self.assertEqual(items[4]["rank"], 4)

        # Check E06 rank 6 (rank 5 skipped)
        self.assertEqual(items[5]["emp_code"], "E06")
        self.assertEqual(items[5]["rank"], 6)

        # Verify _id is not present in output documents
        for doc in items:
            self.assertNotIn("_id", doc)

        # B. Test limit=1 (both rank 1 ties must be returned, so 2 rows!)
        p_limit1 = build_late_leaderboard_pipeline("2026-07", limit=1)
        items_limit1 = list(self.test_db.attendance_logs.aggregate(p_limit1))
        self.assertEqual(len(items_limit1), 2)
        self.assertEqual([i["emp_code"] for i in items_limit1], ["E01", "E02"])

        # C. Test limit=2 (only rank 1 employees returned, since rank 2 does not exist)
        p_limit2 = build_late_leaderboard_pipeline("2026-07", limit=2)
        items_limit2 = list(self.test_db.attendance_logs.aggregate(p_limit2))
        self.assertEqual(len(items_limit2), 2)
        self.assertEqual([i["emp_code"] for i in items_limit2], ["E01", "E02"])

        # D. Test limit=4 (ranks 1, 3, 4 returned -> total 5 rows!)
        p_limit4 = build_late_leaderboard_pipeline("2026-07", limit=4)
        items_limit4 = list(self.test_db.attendance_logs.aggregate(p_limit4))
        self.assertEqual(len(items_limit4), 5)
        self.assertEqual([i["emp_code"] for i in items_limit4], ["E01", "E02", "E03", "E04", "E05"])

        # E. Test department filter: department="Sales"
        p_sales = build_late_leaderboard_pipeline("2026-07", limit=10, department="Sales")
        items_sales = list(self.test_db.attendance_logs.aggregate(p_sales))
        # Within Sales: E04 and E05 have 30 min (rank 1), E06 has 20 min (rank 3)
        self.assertEqual(len(items_sales), 3)
        self.assertEqual(items_sales[0]["emp_code"], "E04")
        self.assertEqual(items_sales[0]["rank"], 1)
        self.assertEqual(items_sales[1]["emp_code"], "E05")
        self.assertEqual(items_sales[1]["rank"], 1)
        self.assertEqual(items_sales[2]["emp_code"], "E06")
        self.assertEqual(items_sales[2]["rank"], 3)

        # F. Test empty month: month with no late logs
        p_empty = build_late_leaderboard_pipeline("2026-06", limit=10)
        items_empty = list(self.test_db.attendance_logs.aggregate(p_empty))
        self.assertEqual(items_empty, [])


if __name__ == "__main__":
    unittest.main()
