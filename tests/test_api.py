"""
Integration tests against a REAL MongoDB (6.0+).  They are skipped unless MONGO_URI is set.

    MONGO_URI=mongodb://localhost:27017 pytest tests/test_api.py -v

Every run uses a throw-away database (hrone_test_<random>) that is dropped at the end.
Analytics are checked against a small, deliberately naive pure-Python reference that is written
independently of the app's pipelines, over a randomised dataset full of awkward cases.
"""
import os
import random
import uuid
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal

import pytest
from fastapi.testclient import TestClient
from pymongo import MongoClient

URI = os.getenv("MONGO_URI")
pytestmark = pytest.mark.skipif(not URI, reason="set MONGO_URI to run the MongoDB integration tests")

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
PRESENT = ("PRESENT", "WFH", "ON_DUTY")


def ms(iso: str) -> int:
    return int(datetime.fromisoformat(iso).timestamp() * 1000)


def hu(value, places):
    q = Decimal(1).scaleb(-places)
    return Decimal(value).quantize(q, rounding=ROUND_HALF_UP)


@pytest.fixture(scope="module")
def api():
    from app import main

    mongo = MongoClient(URI, tz_aware=True)
    name = "hrone_test_" + uuid.uuid4().hex[:8]
    main.client, main.db, main.indexes_ready = mongo, mongo[name], False
    main.ensure_indexes()
    main.ensure_indexes()  # idempotent
    yield TestClient(main.app), main.db
    mongo.drop_database(name)


def emp_body(code, dept="Eng", **kw):
    return {"emp_code": code, "name": f"Name {code}", "email": f"{code}@x.io", "department": dept,
            "joined_on": "2026-01-05", **kw}


# ----------------------------------------------------------------------------------------------- #
# Concurrency
# ----------------------------------------------------------------------------------------------- #
def test_health(api):
    assert api[0].get("/health").json() == {"status": "ok"}


def test_concurrent_employee_create(api):
    client, _ = api
    with ThreadPoolExecutor(16) as pool:
        codes = list(pool.map(lambda _: client.post("/employees", json=emp_body("EMP7000")).status_code, range(16)))
    assert codes.count(201) == 1 and codes.count(409) == 15


def test_concurrent_punch_in_and_out(api):
    client, db = api
    client.post("/employees", json=emp_body("EMP7001"))
    t_in = ms("2026-07-06T09:28:00+05:30")
    with ThreadPoolExecutor(16) as pool:
        codes = list(pool.map(lambda _: client.post("/attendance/punch-in",
                                                    json={"emp_code": "EMP7001", "punched_at": t_in}).status_code, range(16)))
    assert codes.count(201) == 1 and codes.count(409) == 15
    assert db.attendance_logs.count_documents({"emp_code": "EMP7001"}) == 1

    t_out = ms("2026-07-06T18:35:00+05:30")
    with ThreadPoolExecutor(16) as pool:
        codes = list(pool.map(lambda _: client.post("/attendance/punch-out",
                                                    json={"emp_code": "EMP7001", "punched_at": t_out}).status_code, range(16)))
    assert codes.count(200) == 1 and codes.count(409) == 15


def test_concurrent_regularize_keeps_every_history_entry(api):
    client, db = api
    client.post("/employees", json=emp_body("EMP7002"))
    client.post("/attendance/punch-in", json={"emp_code": "EMP7002", "punched_at": ms("2026-07-06T09:28:00+05:30")})

    def patch(i):
        body = {"punch_in": ms("2026-07-06T09:00:00+05:30") + i * 60_000, "reason": f"fix number {i}", "regularized_by": "hr"}
        return client.patch("/attendance/EMP7002/2026-07-06", json=body).status_code

    with ThreadPoolExecutor(8) as pool:
        codes = list(pool.map(patch, range(8)))
    assert set(codes) <= {200, 409, 422}
    doc = db.attendance_logs.find_one({"emp_code": "EMP7002"})
    assert len(doc["history"]) == codes.count(200)          # nothing silently lost
    assert codes.count(200) >= 1


# ----------------------------------------------------------------------------------------------- #
# Analytics vs. a naive reference, on a randomised awkward dataset
# ----------------------------------------------------------------------------------------------- #
MONTH, FIRST, LAST = "2026-07", "2026-07-01", "2026-07-31"
EMPLOYEES = [  # (code, dept, joined_on)
    ("EMP1001", "Alpha", "2026-01-05"), ("EMP1002", "Alpha", "2026-07-13"), ("EMP1003", "Alpha", "2026-07-31"),
    ("EMP1004", "Alpha", "2026-08-01"),   # joins after the month: not counted
    ("EMP1005", "Bravo", "2026-02-01"), ("EMP1006", "Bravo", "2026-02-01"),
    ("EMP1007", "Charlie", "2026-03-10"),   # never has any log
    ("EMP1008", "Delta", "2026-03-10"), ("EMP1009", "Delta", "2026-03-10"), ("EMP1010", "Delta", "2026-03-10"),
]


def days_of(first, last):
    d, end = datetime.fromisoformat(first), datetime.fromisoformat(last)
    while d <= end:
        yield d.date().isoformat(), d.weekday()
        d += timedelta(days=1)


@pytest.fixture(scope="module")
def dataset(api):
    client, db = api
    rnd = random.Random(7)
    db.employees.delete_many({})
    db.attendance_logs.delete_many({})
    db.employees.insert_many([{"emp_code": c, "name": f"N {c}", "email": "a@b.co", "department": d, "shift_start": "09:30",
                               "shift_end": "18:30", "joined_on": j, "created_at": datetime(2026, 1, 1, tzinfo=UTC)}
                              for c, d, j in EMPLOYEES])
    logs = []
    for code, _, _ in EMPLOYEES:
        if code == "EMP1007":
            continue
        for day, _wd in days_of("2026-06-28", "2026-08-02"):      # a little outside the month on both sides
            r = rnd.random()
            if r < 0.15:
                continue                                              # no record at all
            if r < 0.25:
                logs.append({"emp_code": code, "date": day, "status": rnd.choice(["ABSENT", "LEAVE"]), "punch_in": None,
                             "punch_out": None, "work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False})
                continue
            status = rnd.choice(["PRESENT", "PRESENT", "WFH", "ON_DUTY"])
            closed = rnd.random() < 0.9
            wh = round(rnd.uniform(2, 11), 2) if closed else None
            doc = {"emp_code": code, "date": day, "status": status, "punch_in": datetime(2026, 7, 1, 4, 0, tzinfo=UTC),
                   "punch_out": datetime(2026, 7, 1, 13, 0, tzinfo=UTC) if closed else None, "work_hours": wh,
                   "late_minutes": rnd.choice([0, 0, 0, 5, 11, 30, 45, 45]), "overtime_minutes": rnd.choice([0, 0, 30, 45]),
                   "half_day": bool(wh is not None and wh < 4.5), "history": []}
            if rnd.random() < 0.1:                                    # legacy shape
                del doc["history"], doc["half_day"]
            logs.append(doc)
    db.attendance_logs.insert_many(logs)
    return logs


def month_logs(logs, code=None):
    return [l for l in logs if FIRST <= l["date"] <= LAST and (code is None or l["emp_code"] == code)]


def is_present(l):
    return l["status"] in PRESENT


def weekday(l):
    return datetime.fromisoformat(l["date"]).weekday()


def halves(l):
    return Decimal("0.5") if l.get("half_day") else Decimal(1)


def ref_working_days(joined):
    start = max(FIRST, joined)
    return sum(1 for _d, wd in days_of(start, LAST) if wd < 5) if start <= LAST else 0


def test_employee_monthly_matches_reference(api, dataset):
    client, _ = api
    for code, _dept, joined in EMPLOYEES:
        got = client.get(f"/analytics/employees/{code}/monthly", params={"month": MONTH}).json()
        ls = month_logs(dataset, code)
        present = sum((halves(l) for l in ls if is_present(l) and weekday(l) < 5), Decimal(0))
        wd = ref_working_days(joined)
        assert got["working_days"] == wd, code
        assert Decimal(str(got["present_days"])) == present, code
        assert got["leave_days"] == sum(1 for l in ls if l["status"] == "LEAVE")
        assert got["late_count"] == sum(1 for l in ls if l.get("late_minutes", 0) > 0)
        assert got["total_late_minutes"] == sum(l.get("late_minutes", 0) for l in ls)
        assert got["total_overtime_minutes"] == sum(l.get("overtime_minutes", 0) for l in ls)
        expected_pct = None if wd == 0 else float(hu(present / wd * 100, 2))
        assert got["attendance_pct"] == expected_pct, code
    assert client.get("/analytics/employees/NOPE1/monthly", params={"month": MONTH}).status_code == 404


def test_department_summary_matches_reference(api, dataset):
    client, _ = api
    got = client.get("/analytics/departments/summary", params={"month": MONTH}).json()
    by_dept = defaultdict(list)
    for code, dept, joined in EMPLOYEES:
        if joined <= LAST:
            by_dept[dept].append(code)
    assert [i["department"] for i in got["items"]] == sorted(by_dept)
    for item in got["items"]:
        codes = by_dept[item["department"]]
        ls = [l for c in codes for l in month_logs(dataset, c)]
        assert item["headcount"] == len(codes)
        assert Decimal(str(item["present_days"])) == sum((halves(l) for l in ls if is_present(l) and weekday(l) < 5), Decimal(0))
        hours = [Decimal(str(l["work_hours"])) for l in ls if is_present(l) and l.get("work_hours") is not None]
        assert item["avg_work_hours"] == (float(hu(sum(hours) / len(hours), 2)) if hours else None)
        assert item["late_count"] == sum(1 for l in ls if l.get("late_minutes", 0) > 0)
        assert item["total_late_minutes"] == sum(l.get("late_minutes", 0) for l in ls)
        assert item["leave_count"] == sum(1 for l in ls if l["status"] == "LEAVE")
        assert item["on_duty_count"] == sum(1 for l in ls if l["status"] == "ON_DUTY")
    charlie = next(i for i in got["items"] if i["department"] == "Charlie")           # employee with no logs at all
    assert charlie["headcount"] == 1 and charlie["avg_work_hours"] is None and charlie["present_days"] == 0
    one = client.get("/analytics/departments/summary", params={"month": MONTH, "department": "Bravo"}).json()
    assert [i["department"] for i in one["items"]] == ["Bravo"]


def ref_leaderboard(logs, dept=None):
    dept_of = {c: d for c, d, _ in EMPLOYEES}
    totals = defaultdict(lambda: [0, 0])
    for l in month_logs(logs):
        late = l.get("late_minutes", 0)
        if late > 0 and l["emp_code"] in dept_of and (dept is None or dept_of[l["emp_code"]] == dept):
            totals[l["emp_code"]][0] += late
            totals[l["emp_code"]][1] += 1
    rows = sorted(((c, t, n) for c, (t, n) in totals.items()), key=lambda r: (-r[1], r[0]))
    # standard competition ranking: 1 + number of employees with strictly more late minutes
    return [(1 + sum(1 for r in rows if r[1] > t), c, t, n) for c, t, n in rows]


@pytest.mark.parametrize("dept", [None, "Alpha", "Delta"])
@pytest.mark.parametrize("limit", [1, 3, 10, 50])
def test_late_leaderboard_matches_reference(api, dataset, dept, limit):
    client, _ = api
    params = {"month": MONTH, "limit": limit, **({"department": dept} if dept else {})}
    got = client.get("/analytics/leaderboard/late", params=params).json()["items"]
    expected = [r for r in ref_leaderboard(dataset, dept) if r[0] <= limit]
    assert [(i["rank"], i["emp_code"], i["total_late_minutes"], i["late_count"]) for i in got] == expected


def test_leaderboard_ties_share_rank_and_skip(api):
    client, db = api
    db.employees.delete_many({})
    db.attendance_logs.delete_many({})
    for c in ("EMP2001", "EMP2002", "EMP2003", "EMP2004"):
        db.employees.insert_one({"emp_code": c, "name": c, "email": "a@b.co", "department": "T", "shift_start": "09:30",
                                 "shift_end": "18:30", "joined_on": "2026-01-01", "created_at": datetime(2026, 1, 1, tzinfo=UTC)})
    for c, late in (("EMP2001", 50), ("EMP2002", 30), ("EMP2003", 30), ("EMP2004", 10)):
        db.attendance_logs.insert_one({"emp_code": c, "date": "2026-07-06", "status": "PRESENT", "late_minutes": late})
    db.attendance_logs.insert_one({"emp_code": "GHOST", "date": "2026-07-06", "status": "PRESENT", "late_minutes": 999})  # unknown employee
    got = client.get("/analytics/leaderboard/late", params={"month": MONTH, "limit": 2}).json()["items"]
    assert [(i["rank"], i["emp_code"]) for i in got] == [(1, "EMP2001"), (2, "EMP2002"), (2, "EMP2003")]   # tie at cutoff kept
    got = client.get("/analytics/leaderboard/late", params={"month": MONTH, "limit": 50}).json()["items"]
    assert [i["rank"] for i in got] == [1, 2, 2, 4]


def test_department_trend_matches_reference(api, dataset):
    client, db = api
    reinsert_dataset(db, dataset)  # the tie test above emptied the collections
    frm, to = "2026-07-01", "2026-07-31"
    for dept in ("Alpha", "Bravo", "Charlie"):
        got = client.get(f"/analytics/departments/{dept}/trend", params={"from": frm, "to": to}).json()["items"]
        emps = [(c, j) for c, d, j in EMPLOYEES if d == dept]
        rows, rates = [], []
        for day, wd in days_of(frm, to):
            ls = [l for l in dataset if l["date"] == day and l["emp_code"] in {c for c, _ in emps}]
            headcount = sum(1 for _, j in emps if j <= day)
            present = sum((halves(l) for l in ls if is_present(l)), Decimal(0))
            rate = hu(present / headcount, 4) if wd < 5 and headcount > 0 else None
            rates.append(rate)
            window = [r for r in rates[-7:] if r is not None]
            moving = hu(sum(window) / len(window), 4) if window else None
            rows.append({"date": day, "is_working_day": wd < 5, "headcount": headcount, "present_count": float(present),
                         "late_count": sum(1 for l in ls if l.get("late_minutes", 0) > 0),
                         "attendance_rate": None if rate is None else float(rate), "moving_avg_7d": None if moving is None else float(moving)})
        assert len(got) == 31
        assert got == rows, dept
    assert client.get("/analytics/departments/Nope/trend", params={"from": frm, "to": to}).status_code == 404


def test_trend_for_department_with_employees_but_no_logs_still_has_every_day(api, dataset):
    client, db = api
    reinsert_dataset(db, dataset)
    got = client.get("/analytics/departments/Charlie/trend", params={"from": "2026-07-01", "to": "2026-07-10"}).json()["items"]
    assert [i["date"] for i in got] == [f"2026-07-{d:02d}" for d in range(1, 11)]
    assert got[0]["attendance_rate"] == 0.0 and got[0]["moving_avg_7d"] == 0.0     # Wed, working day, no records
    assert got[3]["is_working_day"] is False and got[3]["attendance_rate"] is None  # Sat 4 July
    assert got[3]["moving_avg_7d"] == 0.0                                           # mean of the non-null rates before it


def reinsert_dataset(db, logs):
    """Put the randomised dataset back (the leaderboard tie test empties the collections)."""
    db.employees.delete_many({})
    db.attendance_logs.delete_many({})
    db.employees.insert_many([{"emp_code": c, "name": f"N {c}", "email": "a@b.co", "department": d, "shift_start": "09:30",
                               "shift_end": "18:30", "joined_on": j, "created_at": datetime(2026, 1, 1, tzinfo=UTC)}
                              for c, d, j in EMPLOYEES])
    db.attendance_logs.insert_many([{k: v for k, v in l.items() if k != "_id"} for l in logs])


# ----------------------------------------------------------------------------------------------- #
# Explain: every endpoint must use an index and never scan the collection
# ----------------------------------------------------------------------------------------------- #
@pytest.mark.parametrize("endpoint,params", [
    ("attendance_list", {}),
    ("attendance_list", {"emp_code": "EMP1001", "date_from": FIRST, "date_to": LAST, "status": "PRESENT", "page": 2, "page_size": 5}),
    ("attendance_list", {"status": "LEAVE"}),
    ("employee_monthly", {"emp_code": "EMP1001", "month": MONTH}),
    ("department_summary", {"month": MONTH}),
    ("department_summary", {"month": MONTH, "department": "Alpha"}),
    ("late_leaderboard", {"month": MONTH, "limit": 5}),
    ("department_trend", {"department": "Alpha", "from": FIRST, "to": LAST}),
])
def test_explain_uses_indexes(api, endpoint, params):
    client, _ = api
    r = client.get(f"/admin/explain/{endpoint}", params=params)
    assert r.status_code == 200, r.text
    text = r.text
    assert "IXSCAN" in text and "COLLSCAN" not in text, text[:2000]