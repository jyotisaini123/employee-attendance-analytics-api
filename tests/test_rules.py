"""Pure-Python tests of rules R1-R5 and input validation. No MongoDB needed:  pytest tests/test_rules.py"""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import main
from app.main import (IST, UTC, attendance_date, compute_late_minutes, compute_overtime, compute_work_hours, derive,
                      dt_to_ms, ms_to_dt, month_bounds, round_half_up, working_days)
from decimal import Decimal

DAY = {"emp": {"shift_start": "09:30", "shift_end": "18:30"}, "night": {"shift_start": "22:00", "shift_end": "06:00"}}


def ist(y, mo, d, h, mi, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=IST).astimezone(UTC)


# ---- R2 late -------------------------------------------------------------- #
@pytest.mark.parametrize("h,mi,s,expected", [
    (9, 40, 0, 0),        # exactly 10:00 after start -> not late
    (9, 40, 1, 10),       # one second beyond -> 10 (counted from shift start)
    (10, 15, 59, 45),
    (9, 0, 0, 0),         # early
    (10, 5, 0, 35),
])
def test_late(h, mi, s, expected):
    assert compute_late_minutes(ist(2026, 7, 6, h, mi, s), "2026-07-06", "09:30") == expected


# ---- R3 overtime ---------------------------------------------------------- #
@pytest.mark.parametrize("h,mi,s,expected", [
    (18, 59, 59, 0),      # 29m59s -> below the 30 minute threshold
    (19, 0, 0, 30),       # exactly 30
    (19, 10, 0, 40),
    (18, 0, 0, 0),        # left early
])
def test_overtime(h, mi, s, expected):
    assert compute_overtime(ist(2026, 7, 6, h, mi, s), "2026-07-06", "09:30", "18:30") == expected


def test_overtime_overnight_ends_next_day():
    out = ist(2026, 7, 7, 6, 40)
    assert compute_overtime(out, "2026-07-06", "22:00", "06:00") == 40


# ---- R4 / R5 -------------------------------------------------------------- #
def test_work_hours_half_up_not_bankers():
    t0 = ist(2026, 7, 6, 9, 0)
    assert compute_work_hours(t0, t0 + timedelta(seconds=450)) == 0.13   # 0.125 -> 0.13 (round() gives 0.12)
    assert compute_work_hours(t0, t0 + timedelta(seconds=18)) == 0.01    # 0.005 -> 0.01
    assert compute_work_hours(t0, t0 + timedelta(hours=9, minutes=7)) == 9.12


def test_half_day_uses_rounded_hours():
    t0 = ist(2026, 7, 6, 9, 0)
    emp = DAY["emp"]
    # 4h29m42s = 4.495 h -> rounds to 4.50 -> NOT a half day
    assert derive("PRESENT", t0, t0 + timedelta(hours=4, minutes=29, seconds=42), "2026-07-06", emp)["half_day"] is False
    # 4h29m24s = 4.49 h -> half day
    assert derive("PRESENT", t0, t0 + timedelta(hours=4, minutes=29, seconds=24), "2026-07-06", emp)["half_day"] is True
    assert derive("ABSENT", None, None, "2026-07-06", emp) == {
        "work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}


# ---- R1 dates ------------------------------------------------------------- #
def test_attendance_date_ist_not_utc():
    # 20:00 UTC on 6 July is 01:30 IST on 7 July
    assert attendance_date(datetime(2026, 7, 6, 20, 0, tzinfo=UTC), "09:30", "18:30") == "2026-07-07"


def test_attendance_date_overnight():
    assert attendance_date(ist(2026, 7, 6, 21, 55), "22:00", "06:00") == "2026-07-06"
    assert attendance_date(ist(2026, 7, 7, 0, 15), "22:00", "06:00") == "2026-07-06"   # after midnight: previous day
    assert attendance_date(ist(2026, 7, 7, 5, 59, 59), "22:00", "06:00") == "2026-07-06"
    assert attendance_date(ist(2026, 7, 7, 6, 0, 0), "22:00", "06:00") == "2026-07-07"  # not earlier than shift_end


def test_truncation_and_roundtrip():
    ms = 1783312500999
    assert dt_to_ms(ms_to_dt(ms)) == 1783312500000
    assert dt_to_ms(datetime(2026, 7, 6, 3, 58, tzinfo=UTC)) == 1783310280000


def test_sample_documents_match_rules():
    emp = DAY["emp"]
    d = derive("PRESENT", datetime(2026, 7, 6, 4, 35, tzinfo=UTC), datetime(2026, 7, 6, 13, 40, tzinfo=UTC), "2026-07-06", emp)
    assert d == {"work_hours": 9.08, "late_minutes": 35, "overtime_minutes": 40, "half_day": False}
    d = derive("PRESENT", datetime(2026, 7, 6, 16, 25, tzinfo=UTC), datetime(2026, 7, 7, 1, 10, tzinfo=UTC), "2026-07-06", DAY["night"])
    assert d == {"work_hours": 8.75, "late_minutes": 0, "overtime_minutes": 40, "half_day": False}
    d = derive("WFH", datetime(2026, 7, 7, 4, 0, tzinfo=UTC), datetime(2026, 7, 7, 7, 30, tzinfo=UTC), "2026-07-07", emp)
    assert d["work_hours"] == 3.5 and d["half_day"] is True


# ---- calendar helpers ----------------------------------------------------- #
def test_month_bounds_and_working_days():
    assert month_bounds("2026-07") == ("2026-07-01", "2026-08-01", "2026-07-31")
    assert month_bounds("2026-12") == ("2026-12-01", "2027-01-01", "2026-12-31")
    assert working_days("2026-07", "2026-01-05") == 23          # July 2026 has 23 weekdays
    assert working_days("2026-07", "2026-07-13") == 15          # joined Mon 13 July
    assert working_days("2026-07", "2026-07-31") == 1
    assert working_days("2026-07", "2026-08-01") == 0           # joined after the month
    assert working_days("2026-02", "2026-01-01") == 20


def test_round_half_up():
    assert round_half_up(Decimal("2.675"), 2) == Decimal("2.68")
    assert round_half_up(Decimal("0.00005"), 4) == Decimal("0.0001")


# ---- validation (422 happens before any database access) ------------------- #
client = TestClient(main.app)
EMP = {"emp_code": "EMP0099", "name": "A", "email": "a@b.co", "department": "X", "joined_on": "2026-07-01"}


@pytest.mark.parametrize("patch", [
    {"emp_code": "abc"}, {"emp_code": "EMP12"}, {"email": "nope"}, {"name": ""}, {"joined_on": "2026-02-30"},
    {"joined_on": "20260701"}, {"shift_start": "9:5"}, {"shift_start": "24:00"},
    {"shift_start": "10:00", "shift_end": "10:00"},
])
def test_employee_validation_422(patch):
    assert client.post("/employees", json={**EMP, **patch}).status_code == 422


@pytest.mark.parametrize("value", [1783312500, 1783312500.5, 1783312500000.0, "1783312500000", True, None, 5_000_000_000_000])
def test_bad_timestamp_422(value):
    assert client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": value}).status_code == 422
    assert client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": value}).status_code == 422


@pytest.mark.parametrize("status", ["ABSENT", "LEAVE", "banana"])
def test_punch_in_status_422(status):
    assert client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "status": status}).status_code == 422


@pytest.mark.parametrize("qs", [
    "page=0", "page=-1", "page_size=0", "page_size=101", "status=BAD", "date_from=2026-13-01", "date_from=20260701",
    "date_from=2026-07-10&date_to=2026-07-01",
])
def test_attendance_query_422(qs):
    assert client.get("/attendance?" + qs).status_code == 422


@pytest.mark.parametrize("path", [
    "/analytics/leaderboard/late", "/analytics/leaderboard/late?month=2026-13", "/analytics/leaderboard/late?month=2026-07&limit=0",
    "/analytics/leaderboard/late?month=2026-07&limit=51", "/analytics/departments/summary", "/analytics/employees/EMP0001/monthly",
    "/analytics/departments/X/trend?from=2026-07-01", "/analytics/departments/X/trend?from=2026-07-10&to=2026-07-01",
    "/admin/explain/nope", "/admin/explain/employee_monthly", "/admin/explain/late_leaderboard",
])
def test_analytics_validation_422(path):
    assert client.get(path).status_code == 422


def test_regularize_body_validation():
    base = "/attendance/EMP0001/2026-07-06"
    assert client.patch(base, json={"reason": "bad", "regularized_by": "hr"}).status_code == 422          # reason too short
    assert client.patch(base, json={"reason": "valid reason", "regularized_by": "hr", "punch_in": 1783312500}).status_code == 422
    assert client.patch("/attendance/EMP0001/2026-02-30", json={"reason": "valid reason", "regularized_by": "hr"}).status_code == 422


def test_trend_range_over_92_days():
    r = client.get("/analytics/departments/X/trend?from=2026-01-01&to=2026-12-31")
    assert r.status_code == 422