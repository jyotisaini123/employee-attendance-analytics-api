# DECISIONS.md

### 1. Indexes
I added a unique index on `emp_code` to avoid duplicate employees. I also added a unique index on `emp_code` and `date` to prevent multiple attendance records for the same employee on one day. Other indexes help find records faster.

### 2. Punch-in Race
If two punch-in requests come at the same time, both may try to save a record. The unique index allows only one record to be saved. The other request gets a duplicate key error, and the API returns a 409 Conflict response.

### 3. Ties
I used MongoDB's `$rank` function for the late leaderboard. Employees with the same total late minutes get the same rank. If multiple employees share the cutoff rank, all of them are included in the result.

### 4. Headcount
I start the department summary with the employees collection and use `$lookup` to find attendance records. This also includes employees who joined by the end of the month but have no attendance records.

### 5. Scaling
If the data grows 100 times, I would consider creating a daily summary collection. I would update it whenever attendance changes. This could reduce repeated calculations and make analytics faster. Before making changes, I would also check which queries are taking more time.