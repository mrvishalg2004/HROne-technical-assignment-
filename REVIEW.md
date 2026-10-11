# REVIEW.md

List every defect you found in the starter `app/main.py` (helpers and endpoints). For each one:

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `health()` | Always returned `{"status": "ok"}` without pinging MongoDB. | Returns HTTP 200 even when database is disconnected or unconfigured. | Ping MongoDB with `client.admin.command("ping")` and raise HTTP 503 if unreachable. |
| 2 | `create_employee()` | Check-then-insert race condition (`find_one` followed by `insert_one`). | Concurrent duplicate `emp_code` requests both pass check and cause unhandled 500 or duplicate inserts. | Rely on unique database index `uniq_emp_code` and catch `DuplicateKeyError` to return HTTP 409. |
| 3 | `create_employee()` | `created_at` set to naive `datetime.now()` and serialized as ISO string. | Response fails schema validation requiring integer epoch milliseconds (`EpochMillis`). | Set UTC datetime truncated to seconds and serialize to epoch milliseconds. |
| 4 | `EmployeeIn` model | Missing validation for `emp_code` format, email regex, date format, and `shift_start != shift_end`. | Invalid employee codes, invalid emails, and identical shift start/end accepted. | Replaced with `EmployeeCreate` using regex fields and model validator enforcing distinct shift times. |
| 5 | `list_employees()` | Pagination offset bug: `skip = page * page_size` on 1-indexed page. | Requesting `page=1` skips the first 20 records and returns records 21-40. | Fixed offset to `skip = (page - 1) * page_size`. |
| 6 | `list_employees()` | `total = db.employees.count_documents({})` ignores query filters. | Filtering by department returns filtered items but `total` reflects entire collection count. | Passed filter dictionary: `count_documents(q)`. |
| 7 | `list_employees()` | Missing sort on `emp_code` ascending. | Employees returned in undefined natural storage order instead of `emp_code` ascending. | Added `.sort([("emp_code", 1)])` to cursor. |
| 8 | `list_employees()` | Missing validation for `page` and `page_size`. | `page=0` or `page_size=200` accepted instead of returning HTTP 422. | Added `Query(ge=1)` and `Query(ge=1, le=100)`. |
| 9 | `punch_in()` | Check-then-insert race condition on `(emp_code, date)`. | Concurrent punch-ins for the same employee and date race and create duplicates or crash. | Backed by unique compound index `uniq_emp_code_date` and catch `DuplicateKeyError` to return HTTP 409. |
| 10 | `punch_in()` | Unhandled `TypeError` when employee does not exist (`emp["shift_start"]`). | Punch-in with non-existent `emp_code` raises 500 error instead of 404. | Added check: if `emp` is None, raise HTTP 404 "Unknown employee". |
| 11 | `punch_in()` | Injected extra `id = str(res.inserted_id)` field into response. | Violates OpenAPI `AttendanceRecord` schema which has no internal ID field. | Removed `_id` and `id` from response in `serialize_attendance_doc`. |
| 12 | `punch_in()` | Ignored overnight shift date assignment (Rule R1). | Punch-in before `shift_end` (e.g. 05:45 IST on a 22:00-06:00 shift) recorded on next day instead of start day. | Implemented `determine_attendance_date` assigning punches before `shift_end` to previous day. |
| 13 | `punch_in()` | Instants not truncated to whole seconds (Rule R1). | Stored and returned microsecond fractions. | Truncated instants with `.replace(microsecond=0)`. |
| 14 | `punch_in()` | Returned `punch_in` as datetime string instead of epoch milliseconds. | Contract requires integer epoch milliseconds. | Serialized to integer epoch milliseconds. |
| 15 | `compute_late_minutes()` | Truncated minutes before grace check and ignored IST timezone offset. | Punch-in at 10m 30s floored to 10 and returned 0 instead of 10; UTC timestamps mismatched by 5h30m. | Converted to IST, evaluated elapsed seconds against 600s, and floored from shift start. |
| 16 | `compute_work_hours()` | Used Python standard `round()` (banker's rounding). | Halfway decimal values rounded to nearest even integer rather than half-up (Rule R4). | Used `decimal.Decimal` with `ROUND_HALF_UP`. |
| 17 | `compute_overtime()` | Missing 30-minute threshold (Rule R3) and overnight shift next-day handling. | Overtime < 30 minutes counted as overtime; overnight shifts calculated wrong end time. | Implemented next-day shift end calculation and enforced minimum 30-minute threshold. |
| 18 | `list_attendance()` | Loaded entire collection into memory with `list(db.attendance_logs.find(q))` and sorted in Python. | Severe memory exhaustion and high latency on large dataset (~100k records). | Replaced with MongoDB cursor `.sort([("date", -1), ("emp_code", 1)]).skip(skip).limit(page_size)`. |
| 19 | `list_attendance()` | Sort order only reversed `date`; omitted `emp_code` ascending sort. | Tied dates sorted nondeterministically. | Implemented compound sort on `[("date", -1), ("emp_code", 1)]`. |
| 20 | `list_attendance()` | Missing validation for `date_from > date_to`. | Inverted date range returned 200 with empty list instead of HTTP 422. | Added check raising HTTP 422 when `date_from > date_to`. |

Also note anything you looked at and decided was **not** a defect, and why:
- `IST` timezone definition `timezone(timedelta(hours=5, minutes=30))`: Correct UTC+05:30 offset matching Indian Standard Time.
- Storage of calendar dates as `YYYY-MM-DD` strings in MongoDB: Explicitly intentional per `DATA_MODEL.md` to avoid forcing unnecessary time zones onto calendar dates.
- Pydantic BaseModel for endpoints: FastAPI standard approach; retained and strengthened with Pydantic v2 validation.
