# DECISIONS.md

1. **Indexes.** Created unique index `uniq_emp_code` on `employees`, compound unique `uniq_emp_code_date` on `attendance_logs` for idempotent upserts and `$lookup` joins, `idx_date_desc_emp_code_asc` for pagination, and `idx_date_asc` for date-range analytics. Rejected a single-field index on `status`: with only 5 values, its low cardinality causes poor selectivity compared to the date-sorted compound index.

2. **Punch-in race.** Concurrent identical punch-ins both evaluate attendance date. MongoDB atomically enforces the unique `(emp_code, date)` index. The first insert succeeds (HTTP 201); the second triggers `DuplicateKeyError`, caught to return HTTP 409 Conflict safely.

3. **Ties.** Rank uses `$rank` in `$setWindowFields` (1, 2, 2, 4). Cutoff filtering matches `rank <= limit` before sorting by `(rank, emp_code)`. Any employees sharing a rank within the limit are returned together rather than truncated arbitrarily.

4. **Headcount.** The pipeline originates on `employees` filtered by `joined_on <= period_end`, then performs an outer `$lookup` into `attendance_logs`. Employees without logs remain with zero metrics and null average work hours.

5. **100x Scale.** As a scaling proposal for 100x data (~10M documents), the architecture would transition from real-time aggregations to pre-computed nocturnal summary rollups, and shard `attendance_logs` by hashed `emp_code` and `date`.
