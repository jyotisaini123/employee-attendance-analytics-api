# DECISIONS.md  (about 150-200 words in total - plain language, your own words)


1. **Indexes:** I created a unique index on `employees.emp_code` to prevent duplicate employees and a unique compound index on `attendance_logs(emp_code, date)` to prevent duplicate attendance records. Other indexes support attendance filtering, sorting, and punch-out lookups. I rejected an index on `late_minutes` because queries filter by date first.

2. **Punch-in race:** If two identical punch-in requests arrive simultaneously, both may pass the employee check. The unique index allows only one insert. The second insert raises `DuplicateKeyError`, and the API returns HTTP 409 Conflict.

3. **Ties:** The leaderboard uses `$rank`, so employees with equal late minutes receive the same rank. If a tie occurs at the limit, all employees with that rank are returned, even if the result exceeds the limit.

4. **Headcount:** The department summary starts from the employees collection and uses `$lookup` to find attendance logs. This keeps employees with no attendance records included in the department headcount.

5. **Scaling:** If the system had 100 times more data, I would maintain a daily summary collection updated when attendance changes. This would reduce repeated calculations over all attendance records.