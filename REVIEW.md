# REVIEW.md

List every defect you found in the starter `app/main.py` (helpers and endpoints). For each one:

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | Missing `employee_monthly` route | The contract's employee/month aggregate did not exist. | `GET /analytics/employees/EMP0001/monthly?month=2026-07` returned 404. | Added the indexed MongoDB aggregation, join-date working-day calculation, and R7/R8 response fields. |
| 2 | Missing `late_leaderboard` route | There was no monthly late ranking or tie handling. | `GET /analytics/leaderboard/late?month=2026-07` returned 404; rank cutoffs could not include ties. | Added aggregation and competition ranking; applies the rank cutoff after ranking. |
| 3 | Missing `department_trend` route | Calendar days without logs, changing headcount, and rates were not returned. | Empty days vanished and the trend contract returned 404. | Added a 92-day-bounded aggregation using `$densify`, join-date headcount, rounded rates, and a seven-day window. |
| 4 | Missing `explain_endpoint` route | There was no way to inspect the named endpoints' execution plans. | Every request to `/admin/explain/{endpoint}` returned 404. | Added `executionStats` explains using matching endpoint query/pipeline definitions and JSON-safe explain output. |
| 5 | `department_summary` rounding | MongoDB `$round` applied ties-to-even before the Python half-up step, losing precision needed by R8. | A mean such as 9.125 could return 9.12 instead of 9.13. | Keep the aggregate mean unrounded and apply half-up rounding once in Python. |

Also note anything you looked at and decided was **not** a defect, and why.

The existing department summary route was already present, so it was retained rather than reimplemented. Its equivalent
aggregation builder is also used by the explain route. This change addresses the missing routes; it is not a line-by-line
audit of unrelated starter code.

The provided sample documents are valid examples, not a complete analytics test set: monthly and trend results must also
honor weekends, join dates, employees with no logs, and records outside the shown sample.
