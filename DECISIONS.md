# Design decisions

1. **Indexes.** Unique `employees.emp_code` and `attendance_logs(emp_code, date)` indexes enforce employee identity and
   one attendance record per employee per day. The latter supports employee/date lookups. `(date DESC, emp_code ASC)`
   supports attendance listing; employee `joined_on` and `(department, joined_on)` support headcount filters. I skipped
   a `status` index because it has few values and adds write cost.
2. **Punch-in race.** Concurrent requests for the same employee and attendance date may both reach insertion. The unique
   attendance index allows one insert; MongoDB rejects the other, and the route returns 409 for `DuplicateKeyError`.
3. **Ties.** The leaderboard uses competition ranks and sorts ties by `emp_code`. If a tie reaches the `limit` rank, all
   employees in that tie are returned; the next rank is skipped.
4. **Headcount.** Department summary starts from employees who joined by month end, then looks up their logs. Employees
   with no logs remain in the aggregation and count toward headcount.
5. **At 100x data.** I would inspect real execution plans and measure latency first. If monthly reports become too costly,
   I would consider precomputed rollups, weighing their faster reads against the extra update and consistency work.
