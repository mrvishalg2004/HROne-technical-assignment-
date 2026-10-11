"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
import calendar
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator
from pymongo import ASCENDING, DESCENDING, IndexModel, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

load_dotenv(override=False)

mongo_uri = os.getenv("MONGO_URI")
mongo_db = os.getenv("MONGO_DB") or "hrone_attendance"
client = None
db = None

if mongo_uri:
    try:
        client = MongoClient(mongo_uri, serverSelectionTimeoutMS=5000, tz_aware=True)
        db = client[mongo_db]
    except Exception:
        client = None
        db = None

INDEX_SPECS = {
    "employees": [
        {"keys": [("emp_code", ASCENDING)], "unique": True, "name": "uniq_emp_code"},
        {"keys": [("department", ASCENDING), ("emp_code", ASCENDING)], "name": "idx_dept_emp_code"},
    ],
    "attendance_logs": [
        {"keys": [("emp_code", ASCENDING), ("date", ASCENDING)], "unique": True, "name": "uniq_emp_code_date"},
        {"keys": [("date", DESCENDING), ("emp_code", ASCENDING)], "name": "idx_date_desc_emp_code_asc"},
        {"keys": [("date", ASCENDING)], "name": "idx_date_asc"},
    ],
}


def init_indexes(target_db=None) -> bool:
    target = target_db if target_db is not None else db
    if target is None:
        return False
    try:
        for coll_name, specs in INDEX_SPECS.items():
            models = [
                IndexModel(spec["keys"], **{k: v for k, v in spec.items() if k != "keys"})
                for spec in specs
            ]
            target[coll_name].create_indexes(models)
        return True
    except Exception:
        return False


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_indexes()
    yield


IST = timezone(timedelta(hours=5, minutes=30))

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Helpers & Derived Calculations
# --------------------------------------------------------------------------- #
def determine_attendance_date(punch_in_ist: datetime, shift_start: str, shift_end: str) -> str:
    is_overnight = shift_end <= shift_start
    if is_overnight:
        eh, em = map(int, shift_end.split(":"))
        shift_end_time = datetime.strptime(f"{eh:02d}:{em:02d}", "%H:%M").time()
        if punch_in_ist.time() < shift_end_time:
            return (punch_in_ist.date() - timedelta(days=1)).isoformat()
    return punch_in_ist.date().isoformat()


def compute_late_minutes(punch_in_ist: datetime, shift_start: str, attendance_date: str) -> int:
    sh, sm = map(int, shift_start.split(":"))
    start_dt = datetime.fromisoformat(attendance_date).replace(hour=sh, minute=sm, second=0, microsecond=0, tzinfo=IST)
    elapsed = (punch_in_ist - start_dt).total_seconds()
    if elapsed <= 600:
        return 0
    return int(elapsed // 60)


def compute_overtime(punch_out_ist: datetime, shift_end: str, shift_start: str, attendance_date: str) -> int:
    eh, em = map(int, shift_end.split(":"))
    is_overnight = shift_end <= shift_start
    end_date = datetime.fromisoformat(attendance_date) + (timedelta(days=1) if is_overnight else timedelta(days=0))
    end_dt = end_date.replace(hour=eh, minute=em, second=0, microsecond=0, tzinfo=IST)
    diff = (punch_out_ist - end_dt).total_seconds()
    if diff < 0:
        return 0
    minutes = int(diff // 60)
    return minutes if minutes >= 30 else 0


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    diff_sec = Decimal(str(int((punch_out_utc - punch_in_utc).total_seconds())))
    hours = diff_sec / Decimal(3600)
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_half_day(work_hours: Optional[float]) -> bool:
    if work_hours is None:
        return False
    return work_hours < 4.50


def ensure_utc(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def serialize_employee_doc(doc: dict) -> dict:
    d = dict(doc)
    d.pop("_id", None)
    if isinstance(d.get("created_at"), datetime):
        d["created_at"] = int(ensure_utc(d["created_at"]).timestamp() * 1000)
    return d


def serialize_attendance_doc(doc: dict) -> dict:
    d = dict(doc)
    d.pop("_id", None)
    d.pop("id", None)

    if isinstance(d.get("punch_in"), datetime):
        d["punch_in"] = int(ensure_utc(d["punch_in"]).timestamp() * 1000)
    elif d.get("punch_in") is None:
        d["punch_in"] = None

    if isinstance(d.get("punch_out"), datetime):
        d["punch_out"] = int(ensure_utc(d["punch_out"]).timestamp() * 1000)
    elif d.get("punch_out") is None:
        d["punch_out"] = None

    d.setdefault("late_minutes", 0)
    d.setdefault("overtime_minutes", 0)
    d.setdefault("half_day", False)
    d.setdefault("work_hours", None)

    history = d.get("history") or []
    clean_history = []
    for h in history:
        h_entry = dict(h)
        if isinstance(h_entry.get("at"), datetime):
            h_entry["at"] = int(ensure_utc(h_entry["at"]).timestamp() * 1000)
        changes = dict(h_entry.get("changes", {}))
        for field in ("punch_in", "punch_out"):
            if field in changes:
                ch = dict(changes[field])
                if isinstance(ch.get("from"), datetime):
                    ch["from"] = int(ensure_utc(ch["from"]).timestamp() * 1000)
                if isinstance(ch.get("to"), datetime):
                    ch["to"] = int(ensure_utc(ch["to"]).timestamp() * 1000)
                changes[field] = ch
        h_entry["changes"] = changes
        clean_history.append(h_entry)
    d["history"] = clean_history
    return d


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", max_length=120)
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")

    @model_validator(mode="after")
    def validate_shifts(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


EmployeeIn = EmployeeCreate


class PunchInRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    emp_code: str
    punched_at: Optional[StrictInt] = Field(default=None, ge=100000000000, le=4102444800000)
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


PunchInIn = PunchInRequest


class PunchOutRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    emp_code: str
    punched_at: Optional[StrictInt] = Field(default=None, ge=100000000000, le=4102444800000)


class RegularizeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[StrictInt] = Field(default=None, ge=100000000000, le=4102444800000)
    punch_out: Optional[StrictInt] = Field(default=None, ge=100000000000, le=4102444800000)
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


class EmployeeMonthly(BaseModel):
    model_config = ConfigDict(extra="forbid")
    emp_code: str
    month: str
    working_days: int
    present_days: float
    leave_days: int
    late_count: int
    total_late_minutes: int
    total_overtime_minutes: int
    attendance_pct: Optional[float] = None


class DepartmentSummaryItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    department: str
    headcount: int
    present_days: float
    avg_work_hours: Optional[float] = None
    late_count: int
    total_late_minutes: int
    leave_count: int
    on_duty_count: int


class DepartmentSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    month: str
    items: list[DepartmentSummaryItem]


class LeaderboardItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rank: int = Field(ge=1)
    emp_code: str
    name: str
    department: str
    total_late_minutes: int
    late_count: int


class Leaderboard(BaseModel):
    model_config = ConfigDict(extra="forbid")
    month: str
    items: list[LeaderboardItem]


class TrendItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    date: str
    is_working_day: bool
    headcount: int
    present_count: float
    late_count: int
    attendance_rate: Optional[float] = None
    moving_avg_7d: Optional[float] = None


class Trend(BaseModel):
    model_config = ConfigDict(extra="forbid")
    department: str
    items: list[TrendItem]


class ExplainResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    endpoint: str
    collection: str
    explain: dict


# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    if not client:
        raise HTTPException(status_code=503, detail="Database unavailable")
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="Database unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    if db is None:
        raise HTTPException(status_code=503, detail="Database unavailable")
    doc = body.model_dump()
    doc["created_at"] = datetime.now(timezone.utc).replace(microsecond=0)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")
    return serialize_employee_doc(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    if db is None:
        raise HTTPException(status_code=503, detail="Database unavailable")
    p = page if isinstance(page, int) else 1
    ps = page_size if isinstance(page_size, int) else 20
    q = {}
    if isinstance(department, str):
        q["department"] = department
    total = db.employees.count_documents(q)
    skip = (p - 1) * ps
    cursor = db.employees.find(q).sort([("emp_code", 1)]).skip(skip).limit(ps)
    items = [serialize_employee_doc(d) for d in cursor]
    return {"items": items, "total": total, "page": p, "page_size": ps}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    if db is None:
        raise HTTPException(status_code=503, detail="Database unavailable")
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Unknown employee")

    if body.punched_at is not None:
        punch_in_utc = datetime.fromtimestamp(body.punched_at / 1000, tz=timezone.utc).replace(microsecond=0)
    else:
        punch_in_utc = datetime.now(timezone.utc).replace(microsecond=0)
    punch_in_ist = punch_in_utc.astimezone(IST)

    attendance_date = determine_attendance_date(punch_in_ist, emp["shift_start"], emp["shift_end"])
    late_minutes = compute_late_minutes(punch_in_ist, emp["shift_start"], attendance_date)

    doc = {
        "emp_code": body.emp_code,
        "date": attendance_date,
        "status": body.status,
        "punch_in": punch_in_utc,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_minutes,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="already punched in for this date")
    return serialize_attendance_doc(doc)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = Query(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    date_to: Optional[str] = Query(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    if db is None:
        raise HTTPException(status_code=503, detail="Database unavailable")
    d_from = date_from if isinstance(date_from, str) else None
    d_to = date_to if isinstance(date_to, str) else None
    if d_from and d_to and d_from > d_to:
        raise HTTPException(status_code=422, detail="date_from must not be greater than date_to")

    p = page if isinstance(page, int) else 1
    ps = page_size if isinstance(page_size, int) else 20

    q = {}
    if isinstance(emp_code, str):
        q["emp_code"] = emp_code
    if d_from or d_to:
        q["date"] = {}
        if d_from:
            q["date"]["$gte"] = d_from
        if d_to:
            q["date"]["$lte"] = d_to
    if isinstance(status, str):
        q["status"] = status

    total = db.attendance_logs.count_documents(q)
    skip = (p - 1) * ps
    cursor = db.attendance_logs.find(q).sort([("date", -1), ("emp_code", 1)]).skip(skip).limit(ps)
    items = [serialize_attendance_doc(d) for d in cursor]
    return {"items": items, "total": total, "page": p, "page_size": ps}


@app.post("/attendance/punch-out", status_code=200)
def punch_out(body: PunchOutRequest):
    if db is None:
        raise HTTPException(status_code=503, detail="Database unavailable")
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Unknown employee")

    if body.punched_at is not None:
        punch_out_utc = datetime.fromtimestamp(body.punched_at / 1000, tz=timezone.utc).replace(microsecond=0)
    else:
        punch_out_utc = datetime.now(timezone.utc).replace(microsecond=0)

    records = list(
        db.attendance_logs.find(
            {"emp_code": body.emp_code, "punch_in": {"$ne": None}}
        )
        .sort([("punch_in", -1)])
        .limit(1)
    )
    if not records:
        raise HTTPException(status_code=404, detail="No punch-in found")

    record = records[0]
    punch_in_utc = ensure_utc(record["punch_in"])
    if punch_out_utc <= punch_in_utc:
        raise HTTPException(status_code=422, detail="punched_at must be after punch_in")

    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="Record already punched out")

    if (punch_out_utc - punch_in_utc).total_seconds() > 24 * 3600:
        raise HTTPException(status_code=422, detail="punched_at cannot be more than 24 hours after punch_in")

    work_hours = compute_work_hours(punch_in_utc, punch_out_utc)
    punch_out_ist = punch_out_utc.astimezone(IST)
    overtime_minutes = compute_overtime(punch_out_ist, emp["shift_end"], emp["shift_start"], record["date"])
    half_day = compute_half_day(work_hours)

    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "punch_out": None,
        },
        {
            "$set": {
                "punch_out": punch_out_utc,
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(status_code=409, detail="Record already punched out")
    return serialize_attendance_doc(updated)


@app.patch("/attendance/{emp_code}/{date}", status_code=200)
def regularize_attendance(
    emp_code: str,
    date: str = Path(pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    body: RegularizeRequest = ...,
):
    if db is None:
        raise HTTPException(status_code=503, detail="Database unavailable")
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Unknown employee")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(status_code=404, detail="No attendance record found for this date")

    target_status = body.status if body.status is not None else record.get("status")

    if target_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(
                status_code=422,
                detail="punch times cannot be supplied when status is ABSENT or LEAVE",
            )
        final_punch_in_utc = None
        final_punch_out_utc = None
    else:
        if body.punch_in is not None:
            final_punch_in_utc = datetime.fromtimestamp(body.punch_in / 1000, tz=timezone.utc).replace(microsecond=0)
        else:
            final_punch_in_utc = ensure_utc(record.get("punch_in"))

        if body.punch_out is not None:
            final_punch_out_utc = datetime.fromtimestamp(body.punch_out / 1000, tz=timezone.utc).replace(microsecond=0)
        else:
            final_punch_out_utc = ensure_utc(record.get("punch_out"))

    if target_status in ("PRESENT", "WFH", "ON_DUTY"):
        if final_punch_in_utc is None:
            raise HTTPException(status_code=422, detail="Presence status requires punch_in")
        final_punch_in_ist = final_punch_in_utc.astimezone(IST)
        att_date = determine_attendance_date(final_punch_in_ist, emp["shift_start"], emp["shift_end"])
        if att_date != date:
            raise HTTPException(status_code=422, detail="punch_in must stay on the record's attendance date")

    if final_punch_in_utc is not None and final_punch_out_utc is not None:
        if final_punch_out_utc <= final_punch_in_utc:
            raise HTTPException(status_code=422, detail="punch_out must be after punch_in")
        if (final_punch_out_utc - final_punch_in_utc).total_seconds() > 24 * 3600:
            raise HTTPException(status_code=422, detail="punch_out must be within 24 hours of punch_in")

    if target_status in ("PRESENT", "WFH", "ON_DUTY"):
        final_late_minutes = compute_late_minutes(final_punch_in_ist, emp["shift_start"], date)
        if final_punch_out_utc is not None:
            final_punch_out_ist = final_punch_out_utc.astimezone(IST)
            final_work_hours = compute_work_hours(final_punch_in_utc, final_punch_out_utc)
            final_overtime_minutes = compute_overtime(
                final_punch_out_ist, emp["shift_end"], emp["shift_start"], date
            )
            final_half_day = compute_half_day(final_work_hours)
        else:
            final_work_hours = None
            final_overtime_minutes = 0
            final_half_day = False
    else:
        final_late_minutes = 0
        final_work_hours = None
        final_overtime_minutes = 0
        final_half_day = False

    changes = {}
    fields_to_check = [
        ("status", record.get("status"), target_status),
        ("punch_in", ensure_utc(record.get("punch_in")), final_punch_in_utc),
        ("punch_out", ensure_utc(record.get("punch_out")), final_punch_out_utc),
        ("work_hours", record.get("work_hours"), final_work_hours),
        ("late_minutes", record.get("late_minutes", 0), final_late_minutes),
        ("overtime_minutes", record.get("overtime_minutes", 0), final_overtime_minutes),
        ("half_day", record.get("half_day", False), final_half_day),
    ]
    for field_name, old_val, new_val in fields_to_check:
        if old_val != new_val:
            changes[field_name] = {"from": old_val, "to": new_val}

    if not changes:
        raise HTTPException(status_code=422, detail="Request changes nothing")

    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    history_entry = {
        "at": now_utc,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    raw_history = record.get("history")
    if isinstance(raw_history, list):
        history_len = len(raw_history)
        is_array = True
    else:
        history_len = 0
        is_array = False

    if history_len == 0:
        filter_query = {
            "_id": record["_id"],
            "$or": [
                {"history": {"$size": 0}},
                {"history": {"$exists": False}},
                {"history": None},
            ],
        }
    else:
        filter_query = {
            "_id": record["_id"],
            "history": {"$size": history_len},
        }

    set_doc = {
        "status": target_status,
        "punch_in": final_punch_in_utc,
        "punch_out": final_punch_out_utc,
        "work_hours": final_work_hours,
        "late_minutes": final_late_minutes,
        "overtime_minutes": final_overtime_minutes,
        "half_day": final_half_day,
    }

    if not is_array:
        set_doc["history"] = [history_entry]
        update_doc = {"$set": set_doc}
    else:
        update_doc = {
            "$set": set_doc,
            "$push": {"history": history_entry},
        }

    updated = db.attendance_logs.find_one_and_update(
        filter_query,
        update_doc,
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(status_code=409, detail="Concurrent modification conflict")
    return serialize_attendance_doc(updated)


def calculate_working_days(joined_on_str: str, month_str: str) -> int:
    year, month_num = map(int, month_str.split("-"))
    last_day = calendar.monthrange(year, month_num)[1]
    first_date = date(year, month_num, 1)
    last_date = date(year, month_num, last_day)

    joined_date = date.fromisoformat(joined_on_str)
    start_date = max(first_date, joined_date)

    if start_date > last_date:
        return 0

    working_days = 0
    cur = start_date
    while cur <= last_date:
        if cur.weekday() < 5:
            working_days += 1
        cur += timedelta(days=1)
    return working_days


def compute_attendance_pct(present_days: float, working_days: int) -> Optional[float]:
    if working_days <= 0:
        return None
    val = (Decimal(str(present_days)) / Decimal(str(working_days))) * Decimal("100")
    return float(val.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def build_employee_monthly_pipeline(emp_code: str, month: str) -> list:
    year, month_num = map(int, month.split("-"))
    last_day = calendar.monthrange(year, month_num)[1]
    start_date = f"{month}-01"
    end_date = f"{month}-{last_day:02d}"

    return [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": start_date, "$lte": end_date},
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {
                                        "$lte": [
                                            {
                                                "$isoDayOfWeek": {
                                                    "$dateFromString": {
                                                        "dateString": "$date"
                                                    }
                                                }
                                            },
                                            5,
                                        ]
                                    },
                                    {
                                        "$in": [
                                            "$status",
                                            ["PRESENT", "WFH", "ON_DUTY"],
                                        ]
                                    },
                                ]
                            },
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$status", "LEAVE"]},
                            1,
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
                "total_overtime_minutes": {
                    "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                },
            }
        },
    ]


@app.get(
    "/analytics/employees/{emp_code}/monthly",
    response_model=EmployeeMonthly,
    status_code=200,
)
def get_employee_monthly(
    emp_code: str,
    month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    if db is None:
        raise HTTPException(status_code=503, detail="Database unavailable")
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Unknown employee")

    working_days = calculate_working_days(emp["joined_on"], month)
    pipeline = build_employee_monthly_pipeline(emp_code, month)
    cursor = db.attendance_logs.aggregate(pipeline)
    results = list(cursor)

    if results:
        agg = results[0]
        raw_present = agg.get("present_days", 0.0)
        present_days = float(
            Decimal(str(raw_present)).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
        )
        leave_days = int(agg.get("leave_days", 0))
        late_count = int(agg.get("late_count", 0))
        total_late_minutes = int(agg.get("total_late_minutes", 0))
        total_overtime_minutes = int(agg.get("total_overtime_minutes", 0))
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late_minutes = 0
        total_overtime_minutes = 0

    attendance_pct = compute_attendance_pct(present_days, working_days)

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct,
    }


def build_department_summary_pipeline(month: str, department: Optional[str] = None) -> list:
    year, month_num = map(int, month.split("-"))
    last_day = calendar.monthrange(year, month_num)[1]
    start_date = f"{month}-01"
    end_date = f"{month}-{last_day:02d}"

    match_filter = {"joined_on": {"$lte": end_date}}
    if department:
        match_filter["department"] = department

    return [
        {"$match": match_filter},
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "emp_codes": {"$push": "$emp_code"},
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"dept_emps": "$emp_codes"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$in": ["$emp_code", "$$dept_emps"]},
                                    {"$gte": ["$date", start_date]},
                                    {"$lte": ["$date", end_date]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": "$_id",
                "headcount": {"$first": "$headcount"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {
                                        "$lte": [
                                            {
                                                "$isoDayOfWeek": {
                                                    "$dateFromString": {
                                                        "dateString": "$logs.date"
                                                    }
                                                }
                                            },
                                            5,
                                        ]
                                    },
                                    {
                                        "$in": [
                                            "$logs.status",
                                            ["PRESENT", "WFH", "ON_DUTY"],
                                        ]
                                    },
                                ]
                            },
                            {"$cond": ["$logs.half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "leave_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {"$eq": ["$logs.status", "LEAVE"]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "on_duty_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {"$eq": ["$logs.status", "ON_DUTY"]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {
                                        "$gt": [
                                            {
                                                "$ifNull": [
                                                    "$logs.late_minutes",
                                                    0,
                                                ]
                                            },
                                            0,
                                        ]
                                    },
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {
                        "$cond": [
                            {"$ne": ["$logs", None]},
                            {"$ifNull": ["$logs.late_minutes", 0]},
                            0,
                        ]
                    }
                },
                "wh_sum": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {
                                        "$in": [
                                            "$logs.status",
                                            ["PRESENT", "WFH", "ON_DUTY"],
                                        ]
                                    },
                                    {"$isNumber": "$logs.work_hours"},
                                ]
                            },
                            "$logs.work_hours",
                            0.0,
                        ]
                    }
                },
                "wh_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {
                                        "$in": [
                                            "$logs.status",
                                            ["PRESENT", "WFH", "ON_DUTY"],
                                        ]
                                    },
                                    {"$isNumber": "$logs.work_hours"},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
            }
        },
        {"$sort": {"_id": 1}},
    ]


@app.get(
    "/analytics/departments/summary",
    response_model=DepartmentSummary,
    status_code=200,
)
def get_department_summary(
    month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(default=None),
):
    if db is None:
        raise HTTPException(status_code=503, detail="Database unavailable")

    pipeline = build_department_summary_pipeline(month, department)
    cursor = db.employees.aggregate(pipeline)
    results = list(cursor)

    items = []
    for r in results:
        headcount = int(r.get("headcount", 0))
        raw_present = r.get("present_days", 0.0)
        present_days = float(
            Decimal(str(raw_present)).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
        )
        wh_cnt = int(r.get("wh_count", 0))
        if wh_cnt > 0:
            avg_wh = float(
                (Decimal(str(r.get("wh_sum", 0.0))) / Decimal(str(wh_cnt))).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                )
            )
        else:
            avg_wh = None

        late_count = int(r.get("late_count", 0))
        total_late_minutes = int(r.get("total_late_minutes", 0))
        leave_count = int(r.get("leave_count", 0))
        on_duty_count = int(r.get("on_duty_count", 0))

        items.append(
            {
                "department": r["_id"],
                "headcount": headcount,
                "present_days": present_days,
                "avg_work_hours": avg_wh,
                "late_count": late_count,
                "total_late_minutes": total_late_minutes,
                "leave_count": leave_count,
                "on_duty_count": on_duty_count,
            }
        )

    return {"month": month, "items": items}


def build_late_leaderboard_pipeline(
    month: str,
    limit: int = 10,
    department: Optional[str] = None,
) -> list:
    year, month_num = map(int, month.split("-"))
    last_day = calendar.monthrange(year, month_num)[1]
    start_date = f"{month}-01"
    end_date = f"{month}-{last_day:02d}"

    match_filter: dict = {
        "date": {"$gte": start_date, "$lte": end_date},
        "late_minutes": {"$gt": 0},
    }

    pipeline = [
        {"$match": match_filter},
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]

    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {"$rank": {}}
                },
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
    ])

    return pipeline


@app.get(
    "/analytics/leaderboard/late",
    response_model=Leaderboard,
    status_code=200,
)
def get_late_leaderboard(
    month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(default=10, ge=1, le=50),
    department: Optional[str] = Query(default=None),
):
    if db is None:
        raise HTTPException(status_code=503, detail="Database unavailable")

    pipeline = build_late_leaderboard_pipeline(
        month=month, limit=limit, department=department
    )
    cursor = db.attendance_logs.aggregate(pipeline)
    items = list(cursor)

    return {"month": month, "items": items}


def build_department_trend_pipeline(
    department: str,
    from_date: str,
    to_date: str,
) -> list:
    f_date = date.fromisoformat(from_date)
    t_date = date.fromisoformat(to_date)
    t_date_plus_1 = t_date + timedelta(days=1)
    from_dt = datetime(f_date.year, f_date.month, f_date.day, tzinfo=timezone.utc)
    to_dt_plus_1 = datetime(
        t_date_plus_1.year, t_date_plus_1.month, t_date_plus_1.day, tzinfo=timezone.utc
    )

    return [
        {
            "$match": {
                "date": {"$gte": from_date, "$lte": to_date}
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
        {"$match": {"emp.department": department}},
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
                },
            }
        },
        {
            "$project": {
                "date": "$_id",
                "cal_date": {"$dateFromString": {"dateString": "$_id"}},
                "present_count": 1,
                "late_count": 1,
            }
        },
        {
            "$densify": {
                "field": "cal_date",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [from_dt, to_dt_plus_1],
                },
            }
        },
        {
            "$project": {
                "date": {
                    "$dateToString": {"format": "%Y-%m-%d", "date": "$cal_date"}
                },
                "cal_date": 1,
                "is_working_day": {
                    "$lte": [{"$isoDayOfWeek": "$cal_date"}, 5]
                },
                "present_count": {"$ifNull": ["$present_count", 0.0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"day_str": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$day_str"]},
                                ]
                            }
                        }
                    },
                    {"$count": "hc"},
                ],
                "as": "emp_hc",
            }
        },
        {
            "$project": {
                "date": 1,
                "cal_date": 1,
                "is_working_day": 1,
                "headcount": {"$ifNull": [{"$first": "$emp_hc.hc"}, 0]},
                "present_count": 1,
                "late_count": 1,
            }
        },
        {
            "$project": {
                "date": 1,
                "cal_date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_working_day",
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                },
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"cal_date": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {
                            "documents": [-6, "current"],
                        },
                    }
                },
            }
        },
        {"$sort": {"date": 1}},
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1,
            }
        },
    ]


@app.get(
    "/analytics/departments/{department}/trend",
    response_model=Trend,
    status_code=200,
)
def get_department_trend(
    department: str,
    from_date: str = Query(alias="from", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    to_date: str = Query(alias="to", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
):
    try:
        f_date = date.fromisoformat(from_date)
        t_date = date.fromisoformat(to_date)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid date format")

    if t_date < f_date:
        raise HTTPException(status_code=422, detail="'to' date must be >= 'from' date")

    day_count = (t_date - f_date).days + 1
    if day_count > 92:
        raise HTTPException(status_code=422, detail="Date range exceeds 92 days")

    if db is None:
        raise HTTPException(status_code=503, detail="Database unavailable")

    dept_emp = db.employees.find_one({"department": department})
    if not dept_emp:
        raise HTTPException(status_code=404, detail="Unknown department")

    pipeline = build_department_trend_pipeline(department, from_date, to_date)
    cursor = db.attendance_logs.aggregate(pipeline)
    results = list(cursor)

    items = []
    for r in results:
        raw_rate = r.get("attendance_rate")
        if raw_rate is not None:
            att_rate = float(
                Decimal(str(raw_rate)).quantize(
                    Decimal("0.0001"), rounding=ROUND_HALF_UP
                )
            )
        else:
            att_rate = None

        raw_mavg = r.get("moving_avg_7d")
        if raw_mavg is not None:
            mavg = float(
                Decimal(str(raw_mavg)).quantize(
                    Decimal("0.0001"), rounding=ROUND_HALF_UP
                )
            )
        else:
            mavg = None

        items.append(
            {
                "date": r["date"],
                "is_working_day": bool(r["is_working_day"]),
                "headcount": int(r.get("headcount", 0)),
                "present_count": float(r.get("present_count", 0.0)),
                "late_count": int(r.get("late_count", 0)),
                "attendance_rate": att_rate,
                "moving_avg_7d": mavg,
            }
        )

    return {"department": department, "items": items}


@app.get(
    "/admin/explain/{endpoint}",
    response_model=ExplainResponse,
    status_code=200,
)
def explain_endpoint(
    endpoint: Literal[
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    ],
    emp_code: Optional[str] = Query(default=None),
    month: Optional[str] = Query(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(default=None),
    limit: Optional[int] = Query(default=10, ge=1, le=50),
    date_from: Optional[str] = Query(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    date_to: Optional[str] = Query(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = Query(default=None),
    from_date: Optional[str] = Query(default=None, alias="from", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    to_date: Optional[str] = Query(default=None, alias="to", pattern=r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    **kwargs,
):
    valid_endpoints = (
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    )
    if endpoint not in valid_endpoints:
        raise HTTPException(status_code=422, detail=f"Unsupported endpoint: {endpoint}")

    if db is None:
        raise HTTPException(status_code=503, detail="Database unavailable")

    # Normalize parameters for direct function invocation
    m_val = month if isinstance(month, str) else None
    if m_val is not None and not re.match(r"^\d{4}-(0[1-9]|1[0-2])$", m_val):
        raise HTTPException(status_code=422, detail="Invalid month format, expected YYYY-MM")

    e_code = emp_code if isinstance(emp_code, str) else None
    dept = department if isinstance(department, str) else None
    lim = limit if isinstance(limit, int) else 10
    d_from = date_from if isinstance(date_from, str) else None
    d_to = date_to if isinstance(date_to, str) else None
    st = status if isinstance(status, str) else None
    if st is not None and st not in ("PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"):
        raise HTTPException(status_code=422, detail="Invalid status")
    p = page if isinstance(page, int) else 1
    ps = page_size if isinstance(page_size, int) else 20
    f_date_str = from_date if isinstance(from_date, str) else kwargs.get("from")
    t_date_str = to_date if isinstance(to_date, str) else kwargs.get("to")

    if endpoint == "attendance_list":
        if p < 1:
            raise HTTPException(status_code=422, detail="page must be >= 1")
        if ps < 1 or ps > 100:
            raise HTTPException(status_code=422, detail="page_size must be between 1 and 100")

        if d_from:
            try:
                date.fromisoformat(d_from)
            except ValueError:
                raise HTTPException(status_code=422, detail="Invalid date_from format")
        if d_to:
            try:
                date.fromisoformat(d_to)
            except ValueError:
                raise HTTPException(status_code=422, detail="Invalid date_to format")
        if d_from and d_to and d_from > d_to:
            raise HTTPException(status_code=422, detail="date_from must not be greater than date_to")

        q = {}
        if e_code:
            q["emp_code"] = e_code
        if d_from or d_to:
            q["date"] = {}
            if d_from:
                q["date"]["$gte"] = d_from
            if d_to:
                q["date"]["$lte"] = d_to
        if st:
            q["status"] = st

        skip = (p - 1) * ps
        find_cmd = {
            "find": "attendance_logs",
            "filter": q,
            "sort": {"date": -1, "emp_code": 1},
            "skip": skip,
            "limit": ps,
        }
        explain_doc = db.command("explain", find_cmd, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_doc}

    elif endpoint == "employee_monthly":
        if not e_code:
            raise HTTPException(status_code=422, detail="Missing required parameter: emp_code")
        if not m_val:
            raise HTTPException(status_code=422, detail="Missing required parameter: month")

        pipeline = build_employee_monthly_pipeline(e_code, m_val)
        explain_doc = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_doc}

    elif endpoint == "department_summary":
        if not m_val:
            raise HTTPException(status_code=422, detail="Missing required parameter: month")

        pipeline = build_department_summary_pipeline(m_val, dept)
        explain_doc = db.command(
            "explain",
            {"aggregate": "employees", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "employees", "explain": explain_doc}

    elif endpoint == "late_leaderboard":
        if not m_val:
            raise HTTPException(status_code=422, detail="Missing required parameter: month")

        if lim < 1 or lim > 50:
            raise HTTPException(status_code=422, detail="limit must be between 1 and 50")

        pipeline = build_late_leaderboard_pipeline(m_val, limit=lim, department=dept)
        explain_doc = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_doc}

    elif endpoint == "department_trend":
        if not dept:
            raise HTTPException(status_code=422, detail="Missing required parameter: department")
        if not f_date_str:
            raise HTTPException(status_code=422, detail="Missing required parameter: from")
        if not t_date_str:
            raise HTTPException(status_code=422, detail="Missing required parameter: to")

        try:
            f_d = date.fromisoformat(f_date_str)
            t_d = date.fromisoformat(t_date_str)
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid date format")

        if t_d < f_d:
            raise HTTPException(status_code=422, detail="'to' date must be >= 'from' date")

        if (t_d - f_d).days + 1 > 92:
            raise HTTPException(status_code=422, detail="Date range exceeds 92 days")

        pipeline = build_department_trend_pipeline(dept, f_date_str, t_date_str)
        explain_doc = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_doc}
