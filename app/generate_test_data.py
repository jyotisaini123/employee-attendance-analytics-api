
import os
import random
from datetime import date, timedelta

from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI")
if not MONGO_URI:
    raise ValueError("MONGO_URI is missing from .env")

client = MongoClient(MONGO_URI)
db = client["attendance_perf_test"]

employees = []
attendance = []

start_date = date(2025, 1, 1)

# Generate 1,000 employees
for i in range(1, 1001):
    emp_code = f"EMP{i:04d}"

    employees.append({
        "emp_code": emp_code,
        "name": f"Employee {i}",
        "email": f"employee{i}@example.com",
        "department": ["IT", "HR", "Sales", "Finance"][i % 4],
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": start_date.isoformat()
    })

# Generate 100,000 attendance records
for i in range(100_000):
    emp = employees[i % len(employees)]
    day = start_date + timedelta(days=i // len(employees))

    attendance.append({
        "emp_code": emp["emp_code"],
        "date": day.isoformat(),
        "status": random.choice(["PRESENT", "ABSENT", "WFH", "LEAVE"]),
        "punch_in": None,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": 0,
        "overtime_minutes": 0,
        "half_day": False,
        "history": []
    })

# Use only the dedicated test database
db.employees.drop()
db.attendance_logs.drop()

db.employees.insert_many(employees)
db.attendance_logs.insert_many(attendance)

print("Employees:", db.employees.count_documents({}))
print("Attendance records:", db.attendance_logs.count_documents({}))

client.close()
