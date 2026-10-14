# REVIEW.md

I reviewed the starter `app/main.py` and documented the defects I found and fixed.

| # | Where (function / line) | What is wrong | How you'd notice it | How I fixed it |
|---|---|---|---|---|
| 1 | `compute_late_minutes` | Rounding before checking the 10-minute threshold gives incorrect results. | Punch in at 09:40:01 for a 09:30 shift. | Compare seconds against 600 before calculating whole minutes. |

| 2 | `compute_work_hours` | Standard rounding does not follow the required half-up rule. | 450 seconds should produce 0.13 hours. | Use Decimal and ROUND_HALF_UP. |

| 3 | `compute_overtime` | Overtime is counted even when it is less than 30 minutes. | Punch out 10 minutes after shift end. | Apply the 30-minute minimum. |

| 4 | `compute_overtime` | Overnight shift end is calculated on the wrong date. | A 22:00–06:00 shift ends the next day. | Calculate the correct next-day shift end. |

| 5 | `compute_overtime` | Naive and timezone-aware datetimes cause errors. | Reading punch-out data can raise TypeError. | Use timezone-aware UTC datetimes. |

| 6 | `EmployeeIn` | Employee input validation is incomplete. | Invalid email, date or shift values are accepted. | Add format, date and shift validation. |

| 7 | `PunchInIn` | Invalid attendance statuses are accepted. | Sending status `banana` may succeed. | Restrict allowed statuses. |

| 8 | `PunchInIn` | Punch timestamps accept invalid types and ranges. | A seconds-based timestamp creates the wrong date. | Validate epoch milliseconds strictly. |

| 9 | `GET /health` | Health check does not verify MongoDB connectivity. | It reports success when MongoDB is unavailable. | Ping MongoDB and return 503 on failure. |

| 10 | `POST /employees` | Concurrent requests can create duplicate employees. | Simultaneous requests use the same employee code. | Add a unique index and handle duplicate-key errors. |

| 11 | `GET /employees` | Pagination, filtering and sorting are incorrect. | Page 1 skips records or totals ignore filters. | Fix skip calculation, count, sorting and limits. |

| 12 | `POST /attendance/punch-in` | Unknown employees can cause server errors. | Punch in with an unknown employee code. | Return 404 when the employee is missing. |

| 13 | `POST /attendance/punch-in` | Attendance dates and overnight shifts are calculated incorrectly. | A late-night punch can belong to the wrong date. | Use UTC timestamps and IST attendance-date logic. |

| 14 | `POST /attendance/punch-in` | Concurrent punch-ins can create duplicate records. | Two simultaneous requests use the same employee and date. | Add a unique compound index and return 409 for duplicates. |

| 15 | `GET /attendance` | Filtering, pagination, validation and output formatting are incorrect. | Invalid dates or page values return incorrect results. | Apply database-side filtering, stable sorting and validation. |

| 16 | Startup | Required indexes are not created. | Queries scan collections and duplicates are not prevented. | Create named indexes during startup. |

## Looked at and found correct

- The IST timezone offset is correct.
- Environment variables take priority over `.env` values.
- Removing MongoDB `_id` from responses is correct.
- Create endpoints return HTTP 201.
- Attendance list responses use the required pagination structure.
- Employee codes are used as identifiers.