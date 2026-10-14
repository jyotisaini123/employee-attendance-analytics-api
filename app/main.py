"""

Employee Attendance & Analytics API  (FastAPI + MongoDB)



Run:  uvicorn app.main:app --port 8000

Env:  MONGO_URI, MONGO_DB   (a local .env is loaded, but real environment variables win)



Layout of this file (everything lives here on purpose):

  1. configuration + database handle

  2. time helpers (IST, epoch milliseconds, whole-second truncation)

  3. business rules R1-R5 (late, overtime, work hours, half day)

  4. request models + validation types

  5. response serializers

  6. endpoints: health, employees, attendance

  7. aggregation pipeline builders (shared by the analytics endpoints AND the explain endpoint)

  8. analytics endpoints

  9. explain endpoint

"""

import json

import os

from contextlib import asynccontextmanager

from datetime import datetime, timedelta, timezone

from decimal import ROUND_HALF_UP, Decimal

from typing import Annotated, Any, Literal, Optional



from bson import json_util

from dotenv import load_dotenv

from fastapi import FastAPI, HTTPException, Query

from fastapi.responses import JSONResponse

from pydantic import AfterValidator, BaseModel, Field, field_validator, model_validator

from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument

from pymongo.errors import DuplicateKeyError, PyMongoError



# --------------------------------------------------------------------------- #

# 1. Configuration + database

# --------------------------------------------------------------------------- #

load_dotenv()  

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")

MONGO_DB = os.getenv("MONGO_DB", "attendance_db")





client = MongoClient(MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=5000)

db = client[MONGO_DB]



IST = timezone(timedelta(hours=5, minutes=30))

UTC = timezone.utc

PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]  # R6

GRACE_SECONDS = 10 * 60  # R2

OVERTIME_MIN = 30  # R3

HALF_DAY_BELOW = Decimal("4.50")  # R5



indexes_ready = False





def ensure_indexes() -> None:

    """Create every index the app needs. Safe to run any number of times."""

    global indexes_ready

    emp, logs = db.employees, db.attendance_logs


    emp.create_index([("emp_code", ASCENDING)], unique=True, name="emp_code_unique")

    emp.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="department_emp_code")

    emp.create_index([("joined_on", ASCENDING)], name="joined_on")


    logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="emp_code_date_unique")

    logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="date_desc_emp_code")

    logs.create_index(

        [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], name="status_date_emp_code"

    )

    logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="emp_code_punch_in")

    indexes_ready = True





@asynccontextmanager

async def lifespan(_app: FastAPI):

    try:

        ensure_indexes()

    except PyMongoError as exc:                                              

        print(f"[startup] could not create indexes yet: {exc}")

    yield





app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)





@app.exception_handler(PyMongoError)

def mongo_error_handler(_request, _exc):

    return JSONResponse(status_code=503, content={"detail": "database unavailable"})





def fail(status: int, message: str):

    """Raise an HTTP error. 422 uses FastAPI's list-shaped detail so every 422 looks the same."""

    if status == 422:

        raise HTTPException(422, detail=[{"loc": ["body"], "msg": message, "type": "value_error"}])

    raise HTTPException(status, detail=message)





# --------------------------------------------------------------------------- #

# 2. Time helpers

# --------------------------------------------------------------------------- #

MS_MIN = 100_000_000_000 

MS_MAX = 4_102_444_800_000  





def ms_to_dt(ms: int) -> datetime:

    """Epoch milliseconds -> UTC datetime, truncated to whole seconds (R1)."""

    return datetime.fromtimestamp(ms // 1000, tz=UTC)





def dt_to_ms(value: Optional[datetime]) -> Optional[int]:

    """UTC/aware/naive datetime -> epoch milliseconds (naive is treated as UTC)."""

    if value is None:

        return None

    if value.tzinfo is None:

        value = value.replace(tzinfo=UTC)

    delta = value - datetime(1970, 1, 1, tzinfo=UTC)

    return (delta.days * 86400 + delta.seconds) * 1000 + delta.microseconds // 1000





def now_dt() -> datetime:

    return datetime.now(UTC).replace(microsecond=0)





def parse_day(day: str) -> datetime:

    """'YYYY-MM-DD' -> midnight (naive, only used for date arithmetic)."""

    return datetime.strptime(day, "%Y-%m-%d")





def at_ist(day: str, hhmm: str) -> datetime:

    """The IST instant for 'HH:MM' on the calendar day 'YYYY-MM-DD'."""

    hour, minute = map(int, hhmm.split(":"))

    return parse_day(day).replace(hour=hour, minute=minute, tzinfo=IST)





def is_overnight(shift_start: str, shift_end: str) -> bool:

    return shift_end <= shift_start  





# --------------------------------------------------------------------------- #

# 3. Business rules R1-R5

# --------------------------------------------------------------------------- #

def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:

    """R1: the IST date of the punch-in; overnight shifts that began 'yesterday' use yesterday."""

    local = punch_in.astimezone(IST)

    day = local.date()

    if is_overnight(shift_start, shift_end):

        end_h, end_m = map(int, shift_end.split(":"))

        if (local.hour, local.minute, local.second) < (end_h, end_m, 0):

            day = day - timedelta(days=1)

    return day.isoformat()





def compute_late_minutes(punch_in: datetime, day: str, shift_start: str) -> int:

    """R2: late only if strictly more than 10:00 after shift_start; minutes counted from shift_start."""

    seconds = int((punch_in - at_ist(day, shift_start)).total_seconds())

    return seconds // 60 if seconds > GRACE_SECONDS else 0





def shift_end_instant(day: str, shift_start: str, shift_end: str) -> datetime:

    end = at_ist(day, shift_end)

    return end + timedelta(days=1) if is_overnight(shift_start, shift_end) else end





def compute_overtime(punch_out: datetime, day: str, shift_start: str, shift_end: str) -> int:

    """R3: whole minutes after shift_end, but only when that is at least 30."""

    seconds = int((punch_out - shift_end_instant(day, shift_start, shift_end)).total_seconds())

    minutes = seconds // 60 if seconds > 0 else 0

    return minutes if minutes >= OVERTIME_MIN else 0





def round_half_up(value: Decimal, places: int) -> Decimal:

    return value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)





def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:

    """R4: seconds / 3600, rounded to 2 decimals half-up (Decimal, never float rounding)."""

    seconds = int((punch_out - punch_in).total_seconds())

    return float(round_half_up(Decimal(seconds) / Decimal(3600), 2))





def derive(status: str, punch_in: Optional[datetime], punch_out: Optional[datetime], day: str, emp: dict) -> dict:

    """All four derived fields for a record, following the table in DATA_MODEL.md."""

    out = {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}

    if status not in PRESENCE or punch_in is None:

        return out

    out["late_minutes"] = compute_late_minutes(punch_in, day, emp["shift_start"])

    if punch_out is not None:

        out["work_hours"] = compute_work_hours(punch_in, punch_out)

        out["overtime_minutes"] = compute_overtime(punch_out, day, emp["shift_start"], emp["shift_end"])

        out["half_day"] = Decimal(str(out["work_hours"])) < HALF_DAY_BELOW  

    return out





# --------------------------------------------------------------------------- #

# 4. Request models + validation types

# --------------------------------------------------------------------------- #

def _valid_date(value: str) -> str:

    datetime.strptime(value, "%Y-%m-%d")  

    return value





def _valid_month(value: str) -> str:

    datetime.strptime(value + "-01", "%Y-%m-%d")

    return value







DateStr = Annotated[str, Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"), AfterValidator(_valid_date)]

MonthStr = Annotated[str, Field(pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$"), AfterValidator(_valid_month)]

HHMM = Annotated[str, Field(pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")]



EpochMs = Annotated[int, Field(strict=True, ge=MS_MIN, le=MS_MAX)]

Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]

PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]

Page = Annotated[int, Query(ge=1, le=1_000_000_000)]

PageSize = Annotated[int, Query(ge=1, le=100)]





class EmployeeIn(BaseModel):

    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")

    name: str = Field(min_length=1, max_length=100)

    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+**\.**[^@\s]+$")

    department: str = Field(min_length=1, max_length=50)

    shift_start: HHMM = "09:30"

    shift_end: HHMM = "18:30"

    joined_on: DateStr



    @model_validator(mode="after")

    def shifts_differ(self):

        if self.shift_start == self.shift_end:

            raise ValueError("shift_start must differ from shift_end")

        return self





class PunchInIn(BaseModel):

    emp_code: str

    punched_at: EpochMs = None 

    status: PresenceStatus = "PRESENT"





class PunchOutIn(BaseModel):

    emp_code: str

    punched_at: EpochMs = None 





class RegularizeIn(BaseModel):

    status: Status = None 

    punch_in: EpochMs = None  

    punch_out: EpochMs = None  

    reason: str = Field(min_length=5, max_length=200)

    regularized_by: str = Field(min_length=1, max_length=50)







# --------------------------------------------------------------------------- #

# 5. Response serializers (the API never shows _id and always shows epoch milliseconds)

# --------------------------------------------------------------------------- #

def employee_out(doc: dict) -> dict:

    return {

        "emp_code": doc["emp_code"],

        "name": doc["name"],

        "email": doc["email"],

        "department": doc["department"],

        "shift_start": doc["shift_start"],

        "shift_end": doc["shift_end"],

        "joined_on": doc["joined_on"],

        "created_at": dt_to_ms(doc.get("created_at")),

    }





def _history_value(field: str, value: Any) -> Any:

    return dt_to_ms(value) if field in ("punch_in", "punch_out") else value





def history_out(entry: dict) -> dict:

    changes = {

        field: {"from": _history_value(field, ch.get("from")), "to": _history_value(field, ch.get("to"))}

        for field, ch in (entry.get("changes") or {}).items()

    }

    return {"at": dt_to_ms(entry.get("at")), "by": entry.get("by"), "reason": entry.get("reason"), "changes": changes}





def record_out(doc: dict) -> dict:

    """Seeded documents may lack late_minutes / overtime_minutes / half_day / history: default them."""

    return {

        "emp_code": doc["emp_code"],

        "date": doc["date"],

        "status": doc["status"],

        "punch_in": dt_to_ms(doc.get("punch_in")),

        "punch_out": dt_to_ms(doc.get("punch_out")),

        "work_hours": doc.get("work_hours"),

        "late_minutes": doc.get("late_minutes") or 0,

        "overtime_minutes": doc.get("overtime_minutes") or 0,

        "half_day": bool(doc.get("half_day") or False),

        "history": [history_out(h) for h in (doc.get("history") or [])],

    }





def page_out(items: list, total: int, page: int, page_size: int) -> dict:

    return {"items": items, "total": total, "page": page, "page_size": page_size}





def get_employee_or_404(emp_code: str) -> dict:

    emp = db.employees.find_one({"emp_code": emp_code}, {"_id": 0})

    if emp is None:

        fail(404, "employee not found")

    return emp





# --------------------------------------------------------------------------- #

# 6. Endpoints: health, employees, attendance

# --------------------------------------------------------------------------- #

@app.get("/health")

def health():

    try:

        client.admin.command("ping")

        if not indexes_ready:

            ensure_indexes()

    except PyMongoError:

        fail(503, "database unavailable")

    return {"status": "ok"}





@app.post("/employees", status_code=201)

def create_employee(body: EmployeeIn):

    doc = body.model_dump()

    doc["created_at"] = now_dt()

    try:

        db.employees.insert_one(doc)  

    except DuplicateKeyError:

        fail(409, "emp_code already exists")

    return employee_out(doc)





@app.get("/employees")

def list_employees(department: Optional[str] = None, page: Page = 1, page_size: PageSize = 20):

    query = {} if department is None else {"department": department}

    total = db.employees.count_documents(query)

    cursor = (

        db.employees.find(query, {"_id": 0}).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)

    )

    return page_out([employee_out(d) for d in cursor], total, page, page_size)





@app.post("/attendance/punch-in", status_code=201)

def punch_in(body: PunchInIn):

    emp = get_employee_or_404(body.emp_code)

    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_dt()

    day = attendance_date(ts, emp["shift_start"], emp["shift_end"])

    doc = {"emp_code": body.emp_code, "date": day, "status": body.status, "punch_in": ts, "punch_out": None}

    doc.update(derive(body.status, ts, None, day, emp))

    doc["history"] = []

    try:

        db.attendance_logs.insert_one(doc)  # unique (emp_code, date): exactly one parallel request wins

    except DuplicateKeyError:

        fail(409, "already punched in for this date")

    return record_out(doc)





def attendance_query(emp_code, date_from, date_to, status) -> dict:

    """Filter shared by GET /attendance and the explain endpoint."""

    if date_from is not None and date_to is not None and date_from > date_to:

        fail(422, "date_from must not be after date_to")

    query: dict = {}

    if emp_code is not None:

        query["emp_code"] = emp_code

    if date_from is not None or date_to is not None:

        query["date"] = {}

        if date_from is not None:

            query["date"]["$gte"] = date_from

        if date_to is not None:

            query["date"]["$lte"] = date_to

    if status is not None:

        query["status"] = status

    return query





ATTENDANCE_SORT = [("date", DESCENDING), ("emp_code", ASCENDING)]





@app.get("/attendance")

def list_attendance(

    emp_code: Optional[str] = None,

    date_from: Annotated[Optional[DateStr], Query()] = None,

    date_to: Annotated[Optional[DateStr], Query()] = None,

    status: Optional[Status] = None,

    page: Page = 1,

    page_size: PageSize = 20,

):

    query = attendance_query(emp_code, date_from, date_to, status)

    total = db.attendance_logs.count_documents(query)

    cursor = (

        db.attendance_logs.find(query, {"_id": 0}).sort(ATTENDANCE_SORT).skip((page - 1) * page_size).limit(page_size)

    )

    return page_out([record_out(d) for d in cursor], total, page, page_size)





@app.post("/attendance/punch-out")

def punch_out(body: PunchOutIn):

    emp = get_employee_or_404(body.emp_code)

    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_dt()



    rec = db.attendance_logs.find_one(

    {

        "emp_code": body.emp_code,

        "date": ts.strftime("%Y-%m-%d")

    },

    {"_id": 0},

    sort=[("punch_in", DESCENDING)]

)

    if rec is None:

        fail(404, "no punch-in found for this employee")

    if rec.get("punch_out") is not None:

        fail(409, "already punched out")

    if ts <= rec["punch_in"]:

        fail(422, "punched_at must be after the punch-in")

    if ts - rec["punch_in"] > timedelta(hours=24):

        fail(422, "punch-out is more than 24 hours after the punch-in")

    fields = derive(rec["status"], rec["punch_in"], ts, rec["date"], emp)

    fields["punch_out"] = ts

    # Conditional update: only succeeds while the record is still open and unchanged.

    updated = db.attendance_logs.find_one_and_update(

        {"emp_code": rec["emp_code"], "date": rec["date"], "punch_in": rec["punch_in"], "punch_out": None},

        {"$set": fields},

        projection={"_id": 0},

        return_document=ReturnDocument.AFTER,

    )

    if updated is None:  # someone else punched out (or corrected the record) first

        fail(409, "already punched out")

    return record_out(updated)





@app.patch("/attendance/{emp_code}/{date}")

def regularize(emp_code: str, date: DateStr, body: RegularizeIn):

    emp = get_employee_or_404(emp_code)

    sent = body.model_fields_set

    for _attempt in range(5):  # optimistic concurrency: re-read and retry if someone edited meanwhile

        rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date}, {"_id": 0})

        if rec is None:

            fail(404, "no attendance record for this date")

        cur_status, cur_in, cur_out = rec["status"], rec.get("punch_in"), rec.get("punch_out")



        new_status = body.status if "status" in sent else cur_status

        new_in = ms_to_dt(body.punch_in) if "punch_in" in sent else cur_in

        new_out = ms_to_dt(body.punch_out) if "punch_out" in sent else cur_out



        if new_status in ("ABSENT", "LEAVE"):

            if "punch_in" in sent or "punch_out" in sent:

                fail(422, "ABSENT/LEAVE clear the punch times; do not send punch_in or punch_out")

            new_in = new_out = None

        else:

            if new_in is None:

                fail(422, "a presence status requires a punch_in")

            if "punch_in" in sent and attendance_date(new_in, emp["shift_start"], emp["shift_end"]) != date:

                fail(422, "punch_in must stay on the record's attendance date")

            if new_out is not None and not (new_in < new_out <= new_in + timedelta(hours=24)):

                fail(422, "punch_out must be after punch_in and within 24 hours")



        new_fields = {"status": new_status, "punch_in": new_in, "punch_out": new_out}

        new_fields.update(derive(new_status, new_in, new_out, date, emp))

        old_fields = {

            "status": cur_status,

            "punch_in": cur_in,

            "punch_out": cur_out,

            "work_hours": rec.get("work_hours"),

            "late_minutes": rec.get("late_minutes") or 0,

            "overtime_minutes": rec.get("overtime_minutes") or 0,

            "half_day": bool(rec.get("half_day") or False),

        }

        changes = {k: {"from": old_fields[k], "to": v} for k, v in new_fields.items() if old_fields[k] != v}

        if not any(k in changes for k in ("status", "punch_in", "punch_out")):

            fail(422, "the request changes nothing")



        entry = {"at": now_dt(), "by": body.regularized_by, "reason": body.reason, "changes": changes}

        # Compare-and-set on the state we read; $push appends, so history is never overwritten.

        updated = db.attendance_logs.find_one_and_update(

            {"emp_code": emp_code, "date": date, "status": cur_status, "punch_in": cur_in, "punch_out": cur_out},

            {"$set": new_fields, "$push": {"history": entry}},

            projection={"_id": 0},

            return_document=ReturnDocument.AFTER,

        )

        if updated is not None:

            return record_out(updated)

    fail(409, "the record is being edited concurrently, retry")





# --------------------------------------------------------------------------- #

# 7. Aggregation pipeline builders

# --------------------------------------------------------------------------- #

def month_bounds(month: str) -> tuple[str, str, str]:

    """('2026-07-01', '2026-08-01', '2026-07-31'): ISO date strings compare correctly as text."""

    first = datetime.strptime(month + "-01", "%Y-%m-%d")

    nxt = (first.replace(day=28) + timedelta(days=4)).replace(day=1)

    last = nxt - timedelta(days=1)

    return first.strftime("%Y-%m-%d"), nxt.strftime("%Y-%m-%d"), last.strftime("%Y-%m-%d")





def metrics_stage() -> dict:

    """Per-record numbers read from the STORED fields (missing -> default). Counts are integers:

    'ph' = present half-units (full day 2, half day 1) so nothing is summed as a float."""

    day = {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}}

    present = {"$in": ["$status", PRESENCE]}

    ph = {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 1, 2]}

    late = {"$ifNull": ["$late_minutes", 0]}

    has_hours = {"$and": [present, {"$isNumber": "$work_hours"}]}

    return {

        "$project": {

            "_id": 0,

            "date": 1,

            "ph_any": {"$cond": [present, ph, 0]},

            "ph_weekday": {"$cond": [{"$and": [present, {"$lte": [{"$isoDayOfWeek": day}, 5]}]}, ph, 0]},

            "late": late,

            "is_late": {"$cond": [{"$gt": [late, 0]}, 1, 0]},

            "overtime": {"$ifNull": ["$overtime_minutes", 0]},

            "is_leave": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]},

            "is_on_duty": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]},

            # work_hours in whole hundredths: exact integer sums, rounded half-up later in Python.

            "wh_cents": {"$cond": [has_hours, {"$toLong": {"$round": [{"$multiply": ["$work_hours", 100]}, 0]}}, 0]},

            "wh_n": {"$cond": [has_hours, 1, 0]},

        }

    }





def monthly_pipeline(emp_code: str, month: str) -> list:

    start, nxt, _ = month_bounds(month)

    return [

        {"$match": {"emp_code": emp_code, "date": {"$gte": start, "$lt": nxt}}},

        metrics_stage(),

        {

            "$group": {

                "_id": None,

                "ph": {"$sum": "$ph_weekday"},

                "leave_days": {"$sum": "$is_leave"},

                "late_count": {"$sum": "$is_late"},

                "total_late_minutes": {"$sum": "$late"},

                "total_overtime_minutes": {"$sum": "$overtime"},

            }

        },

    ]





def summary_pipeline(month: str, department: Optional[str]) -> list:

    """Starts from EMPLOYEES so people with no logs still count (R9), then left-joins their logs."""

    start, nxt, last = month_bounds(month)

    match: dict = {"joined_on": {"$lte": last}}

    if department is not None:

        match["department"] = department

    return [

        {"$match": match},

        {

            "$lookup": {

                "from": "attendance_logs",

                "localField": "emp_code",

                "foreignField": "emp_code",

                "pipeline": [{"$match": {"date": {"$gte": start, "$lt": nxt}}}, metrics_stage()],

                "as": "logs",

            }

        },

        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},

        {

            "$group": {

                "_id": "$department",

                "emp_codes": {"$addToSet": "$emp_code"},

                "ph": {"$sum": "$logs.ph_weekday"},

                "late_count": {"$sum": "$logs.is_late"},

                "total_late_minutes": {"$sum": "$logs.late"},

                "leave_count": {"$sum": "$logs.is_leave"},

                "on_duty_count": {"$sum": "$logs.is_on_duty"},

                "wh_cents": {"$sum": "$logs.wh_cents"},

                "wh_n": {"$sum": "$logs.wh_n"},

            }

        },

        {

            "$project": {

                "headcount": {"$size": "$emp_codes"},

                "ph": 1,

                "late_count": 1,

                "total_late_minutes": 1,

                "leave_count": 1,

                "on_duty_count": 1,

                "wh_cents": 1,

                "wh_n": 1,

            }

        },

        {"$sort": {"_id": 1}},

    ]





def leaderboard_pipeline(month: str, department: Optional[str], limit: int) -> list:

    start, nxt, _ = month_bounds(month)

    pipeline: list = [

        {"$match": {"date": {"$gte": start, "$lt": nxt}, "late_minutes": {"$gt": 0}}},

        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},

        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},

        {"$unwind": "$emp"},                                            

    ]

    if department is not None:

        pipeline.append({"$match": {"emp.department": department}})                                   

    pipeline += [

        # $rank = standard competition ranking: ties share a rank, the next rank is skipped (1,2,2,4).

        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},

        {"$match": {"rank": {"$lte": limit}}},  # limit applies AFTER ranking

        {"$sort": {"total_late_minutes": -1, "_id": 1}},

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

    ]

    return pipeline





def trend_pipeline(emp_codes: list, joined_on: list, date_from: str, date_to: str) -> list:

    first = parse_day(date_from).replace(tzinfo=UTC)

    after_last = parse_day(date_to).replace(tzinfo=UTC) + timedelta(days=1)

    headcount = {

        "$size": {"$filter": {"input": {"$literal": joined_on}, "as": "j", "cond": {"$lte": ["$$j", "$date"]}}}

    }

    return [

        {"$match": {"emp_code": {"$in": emp_codes}, "date": {"$gte": date_from, "$lte": date_to}}},

        metrics_stage(),

        {"$group": {"_id": "$date", "ph": {"$sum": "$ph_any"}, "late_count": {"$sum": "$is_late"}}},

        {"$addFields": {"day": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d"}}}},

        # Gap filling: one document for EVERY day in [from, to], weekends and empty days included.

        {"$densify": {"field": "day", "range": {"step": 1, "unit": "day", "bounds": [first, after_last]}}},

        {

            "$set": {

                "date": {"$dateToString": {"date": "$day", "format": "%Y-%m-%d"}},

                "ph": {"$ifNull": ["$ph", 0]},

                "late_count": {"$ifNull": ["$late_count", 0]},

                "is_working_day": {"$lte": [{"$isoDayOfWeek": "$day"}, 5]},

            }

        },

        {"$set": {"headcount": headcount}},

        {

            "$set": {

                # attendance_rate in whole ten-thousandths, half-up: floor(x + 0.5). ph is in half-units.

                "rate_i": {

                    "$cond": [

                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},

                        {"$toLong": {"$floor": {"$add": [{"$divide": [{"$multiply": ["$ph", 5000]}, "$headcount"]}, 0.5]}}},

                        None,

                    ]

                }

            }

        },

        {"$set": {"rate_n": {"$cond": [{"$eq": ["$rate_i", None]}, 0, 1]}}},

        {

            # 7-day moving average over the current row and up to 6 earlier rows INSIDE the range.

            "$setWindowFields": {

                "sortBy": {"day": 1},

                "output": {

                    "ma_sum": {"$sum": "$rate_i", "window": {"documents": [-6, 0]}},

                    "ma_n": {"$sum": "$rate_n", "window": {"documents": [-6, 0]}},

                },

            }

        },

        {"$sort": {"day": 1}},

        {

            "$project": {

                "_id": 0,

                "date": 1,

                "is_working_day": 1,

                "headcount": 1,

                "ph": 1,

                "late_count": 1,

                "rate_i": 1,

                "ma_sum": 1,

                "ma_n": 1,

            }

        },

    ]





def run_aggregate(collection, pipeline: list) -> list:

    return list(collection.aggregate(pipeline, allowDiskUse=True))





def trend_inputs(department: str, date_from: str, date_to: str) -> tuple[list, list]:

    """Validate the range, load the department's employees (small), 404 when there are none."""

    if date_to < date_from:

        fail(422, "'to' must not be before 'from'")

    if (parse_day(date_to) - parse_day(date_from)).days + 1 > 92:

        fail(422, "the range may not exceed 92 days")

    emps = list(db.employees.find({"department": department}, {"_id": 0, "emp_code": 1, "joined_on": 1}))

    if not emps:

        fail(404, "unknown department")



    print("Department:", department)

    print("Employee codes:", [e["emp_code"] for e in emps])

    print("Joining dates:", [e["joined_on"] for e in emps])



    return [e["emp_code"] for e in emps], [e["joined_on"] for e in emps]











# --------------------------------------------------------------------------- #

# 8. Analytics endpoints

# --------------------------------------------------------------------------- #

def working_days(month: str, joined_on: str) -> int:

    """R7: Mon-Fri in the month, counted from joined_on when that is later than the 1st."""

    start, _, last = month_bounds(month)

    day = parse_day(max(start, joined_on))

    end = parse_day(last)

    count = 0

    while day <= end:

        if day.weekday() < 5:

            count += 1

        day += timedelta(days=1)

    return count





@app.get("/analytics/employees/{emp_code}/monthly")

def employee_monthly(emp_code: str, month: Annotated[MonthStr, Query()]):

    emp = get_employee_or_404(emp_code)

    rows = run_aggregate(db.attendance_logs, monthly_pipeline(emp_code, month))

    row = rows[0] if rows else {}

    halves = int(row.get("ph", 0))

    days = working_days(month, emp["joined_on"])

    pct = None

    if days > 0:

        pct = float(round_half_up(Decimal(halves) * 50 / Decimal(days), 2))                       

    return {

        "emp_code": emp_code,

        "month": month,

        "working_days": days,

        "present_days": halves / 2,

        "leave_days": int(row.get("leave_days", 0)),

        "late_count": int(row.get("late_count", 0)),

        "total_late_minutes": int(row.get("total_late_minutes", 0)),

        "total_overtime_minutes": int(row.get("total_overtime_minutes", 0)),

        "attendance_pct": pct,

    }





@app.get("/analytics/departments/summary")

def department_summary(month: Annotated[MonthStr, Query()], department: Optional[str] = None):

    rows = run_aggregate(db.employees, summary_pipeline(month, department))

    items = []

    for r in rows:

        avg = None

        if r["wh_n"] > 0:  # mean over RECORDS: total hundredths / (100 x record count)

            avg = float(round_half_up(Decimal(int(r["wh_cents"])) / Decimal(100 * int(r["wh_n"])), 2))

        items.append(

            {

                "department": r["_id"],

                "headcount": int(r["headcount"]),

                "present_days": int(r["ph"]) / 2,

                "avg_work_hours": avg,

                "late_count": int(r["late_count"]),

                "total_late_minutes": int(r["total_late_minutes"]),

                "leave_count": int(r["leave_count"]),

                "on_duty_count": int(r["on_duty_count"]),

            }

        )

    return {"month": month, "items": items}





@app.get("/analytics/leaderboard/late")

def late_leaderboard(

    month: Annotated[MonthStr, Query()],

    limit: Annotated[int, Query(ge=1, le=50)] = 10,

    department: Optional[str] = None,

):

    rows = run_aggregate(db.attendance_logs, leaderboard_pipeline(month, department, limit))

    items = [

        {

            "rank": int(r["rank"]),

            "emp_code": r["emp_code"],

            "name": r["name"],

            "department": r["department"],

            "total_late_minutes": int(r["total_late_minutes"]),

            "late_count": int(r["late_count"]),

        }

        for r in rows

    ]

    return {"month": month, "items": items}





@app.get("/analytics/departments/{department}/trend")

def department_trend(

    department: str,

    date_from: Annotated[DateStr, Query(alias="from")],

    date_to: Annotated[DateStr, Query(alias="to")],

):

    codes, joined = trend_inputs(department, date_from, date_to)

    rows = run_aggregate(db.attendance_logs, trend_pipeline(codes, joined, date_from, date_to))

    items = []

    for r in rows:

        rate = None if r["rate_i"] is None else float(Decimal(int(r["rate_i"])) / 10000)

        moving = None

        if int(r["ma_n"]) > 0:                                                                            

            moving = float(round_half_up(Decimal(int(r["ma_sum"])) / Decimal(int(r["ma_n"]) * 10000), 4))

        items.append(

            {

                "date": r["date"],

                "is_working_day": bool(r["is_working_day"]),

                "headcount": int(r["headcount"]),

                "present_count": int(r["ph"]) / 2,

                "late_count": int(r["late_count"]),

                "attendance_rate": rate,

                "moving_avg_7d": moving,

            }

        )

    return {"department": department, "items": items}





# --------------------------------------------------------------------------- #

# 9. Explain endpoint

# --------------------------------------------------------------------------- #

def need(value: Any, name: str) -> Any:

    if value is None:

        raise HTTPException(

            422, detail=[{"loc": ["query", name], "msg": "Field required", "type": "missing", "input": None}]

        )

    return value





@app.get("/admin/explain/{endpoint}")

def explain_endpoint(

    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],

    emp_code: Optional[str] = None,

    month: Annotated[Optional[MonthStr], Query()] = None,

    department: Optional[str] = None,

    limit: Annotated[int, Query(ge=1, le=50)] = 10,

    date_from: Annotated[Optional[DateStr], Query()] = None,

    date_to: Annotated[Optional[DateStr], Query()] = None,

    status: Optional[Status] = None,

    from_: Annotated[Optional[DateStr], Query(alias="from")] = None,

    to: Annotated[Optional[DateStr], Query()] = None,

    page: Page = 1,

    page_size: PageSize = 20,

):

    """Builds the SAME filter / sort / pagination / pipeline as the real endpoint, then asks MongoDB to explain it."""

    if endpoint == "attendance_list":

        collection = "attendance_logs"

        command = {

            "find": collection,

            "filter": attendance_query(emp_code, date_from, date_to, status),

            "sort": dict(ATTENDANCE_SORT),

            "skip": (page - 1) * page_size,

            "limit": page_size,

        }

    else:

        if endpoint == "employee_monthly":

            collection = "attendance_logs"

            pipeline = monthly_pipeline(need(emp_code, "emp_code"), need(month, "month"))

        elif endpoint == "department_summary":

            collection = "employees"

            pipeline = summary_pipeline(need(month, "month"), department)

        elif endpoint == "late_leaderboard":

            collection = "attendance_logs"

            pipeline = leaderboard_pipeline(need(month, "month"), department, limit)

        else:                    

            collection = "attendance_logs"

            codes, joined = trend_inputs(need(department, "department"), need(from_, "from"), need(to, "to"))

            pipeline = trend_pipeline(codes, joined, from_, to)

        command = {"aggregate": collection, "pipeline": pipeline, "cursor": {}, "allowDiskUse": True}

    raw = db.command({"explain": command, "verbosity": "executionStats"})

    # Convert BSON types (dates, Int64, ...) to plain JSON.

    plain = json.loads(json_util.dumps(raw, json_options=json_util.RELAXED_JSON_OPTIONS))

    return {"endpoint": endpoint, "collection": collection, "explain": plain}

# --------------------------------------------------------------------------- #
# TODO - the rest of the contract (see openapi.yaml):
#   POST  /attendance/punch-out
#   PATCH /attendance/{emp_code}/{date}
#   GET   /analytics/employees/{emp_code}/monthly
#   GET   /analytics/departments/summary
#   GET   /analytics/leaderboard/late
#   GET   /analytics/departments/{department}/trend
#   GET   /admin/explain/{endpoint}
# --------------------------------------------------------------------------- #