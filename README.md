# Employee Attendance & Analytics API

FastAPI and MongoDB backend service for employee attendance tracking, managerial corrections, and departmental analytics. Built to meet the full OpenAPI 3.0 contract and business rules (R1–R10).

## Implemented Endpoints

- **Health & Lifecycle**:
  - `GET /health`: Returns 200 when MongoDB responds to a ping command, otherwise 503.
- **Employees**:
  - `POST /employees`: Create employee (validates unique `emp_code`, email, distinct shift times; 409 on duplicate).
  - `GET /employees`: Paginated employee list with department filtering, sorted by `emp_code` ascending.
- **Attendance & Corrections**:
  - `POST /attendance/punch-in`: Records daily punch-in; handles overnight shifts (R1), grace period & lateness (R2), race-safe via unique compound index (409 on conflict).
  - `GET /attendance`: Paginated attendance list supporting employee, date-range, and status filters.
  - `POST /attendance/punch-out`: Calculates derived work hours (R4), overtime (R3), and half-day status (R5).
  - `PATCH /attendance/{emp_code}/{date}`: Regularizes punch times/status, recalculates derived metrics, and appends audit trail to `history`.
- **Analytics & Explain**:
  - `GET /analytics/employees/{emp_code}/monthly`: Employee monthly aggregation (attendance percentage, work hours, overtime, leave days).
  - `GET /analytics/departments/summary`: Department monthly summary with cross-collection `$lookup` (headcount, attendance metrics, average work hours).
  - `GET /analytics/leaderboard/late`: Late-comers ranking using `$setWindowFields` and `$rank` with tie handling.
  - `GET /analytics/departments/{department}/trend`: Daily trend series with `$densify` gap-filling and 7-day moving averages.
  - `GET /admin/explain/{endpoint}`: MongoDB raw explain output (`executionStats` verbosity) for production queries and aggregation pipelines.

## Prerequisites

- **Python**: 3.11 or newer (tested on Python 3.14)
- **MongoDB**: 6.0 or newer (tested on MongoDB 7.0/9.0)

## Setup & Installation

1. Create and activate a Python virtual environment:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   ```

2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

3. Configure environment variables (using placeholders):
   ```bash
   export MONGO_URI="mongodb://localhost:27017"
   export MONGO_DB="hrone_attendance"
   ```
   *Note: Real environment variables take precedence over `.env` files. Never commit credentials.*

4. (Optional) Seed initial sample data:
   ```bash
   python sample_seed.py
   ```

## Starting the Application

Start the API with `uvicorn`:
```bash
uvicorn app.main:app --port 8000
```
Indexes (`uniq_emp_code`, `idx_dept_emp_code`, `uniq_emp_code_date`, `idx_date_desc_emp_code_asc`, `idx_date_asc`) are created automatically and idempotently during startup.

## API Documentation & Health Check

- **Health Check**: `GET http://localhost:8000/health` (returns HTTP 200 `{"status": "ok"}` when MongoDB responds to a ping; HTTP 503 `{"detail": "Database unavailable"}` when unreachable)
- **Interactive Swagger UI**: `http://localhost:8000/docs`
- **ReDoc Documentation**: `http://localhost:8000/redoc`

## Testing & Verification

Run the complete test suite:
```bash
.venv/bin/python -m unittest discover -v tests
```

### What the Test Suite Verifies
The test suite consists of 119 tests across 8 test modules (109 unit/mocked tests, 10 live database tests):
- Strict validation, timezone conversion (IST UTC+05:30), and derived field math (`ROUND_HALF_UP`).
- Concurrency and race safety for punch-ins, punch-outs, and regularizations.
- Multi-collection aggregation pipelines across month boundaries, leap years, gap-filling, and window functions.
- Raw MongoDB `explain` dispatch with `executionStats` verbosity and index validation.

### Database Connectivity Note
- **Local MongoDB**: Fully verified. 10 integration tests run against a local MongoDB instance (`localhost:27017`), demonstrating index-backed execution plans (`IXSCAN`) and zero collection scans (`COLLSCAN`) on lookup joins.
- **MongoDB Atlas**: **UNVERIFIED**. The Atlas SRV host provided in `.env` cannot be resolved in this environment due to external DNS `NXDOMAIN` errors. No tests claim Atlas integration.
