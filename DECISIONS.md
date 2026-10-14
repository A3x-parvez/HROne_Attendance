# DECISIONS.md

1. **Indexes.** The unique `employees.emp_code` index keeps employee codes unique. The unique `(emp_code, date)` attendance
   index both prevents duplicate daily records and supports employee/date lookups. `(date DESC, emp_code ASC)` supports
   attendance listing and month-wide log scans. `joined_on` and `(department, joined_on)` support headcount filters.
   I did not add an index on `status`: it has few values, and the date-led index already narrows the common list query
   without the extra write cost.
2. **Punch-in race.** Both requests can pass the employee lookup, but they insert the same `(emp_code, date)`. MongoDB's
   unique index admits one insert; the other raises `DuplicateKeyError`, which the route returns as 409.
3. **Ties.** The leaderboard uses MongoDB competition ranking, then filters on `rank <= limit`. Employees tied at the
   cutoff are all returned, and the next rank is skipped.
4. **Headcount.** Department summary starts with employees who joined by month end and looks up logs. Employees with no
   matching logs remain in the pipeline with zero-valued stats, so they still contribute to headcount.
5. **100x data.** I would measure the explain plans first, then consider precomputed monthly rollups and archival policies
   if the live aggregations no longer meet latency targets.
