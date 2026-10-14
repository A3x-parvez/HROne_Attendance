# REVIEW.md

List every defect you found in the starter `app/main.py` (helpers and endpoints). For each one:

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | Missing `employee_monthly` route | The contract's employee/month aggregate did not exist. | `GET /analytics/employees/EMP0001/monthly?month=2026-07` returned 404. | Added the indexed MongoDB aggregation, join-date working-day calculation, and R7/R8 response fields. |
| 2 | Missing `late_leaderboard` route | There was no monthly late ranking or tie handling. | `GET /analytics/leaderboard/late?month=2026-07` returned 404; rank cutoffs could not include ties. | Added aggregation and competition ranking; applies the rank cutoff after ranking. |
| 3 | Missing `department_trend` route | Calendar days without logs, changing headcount, and rates were not returned. | Empty days vanished and the trend contract returned 404. | Added a 92-day-bounded aggregation using `$densify`, join-date headcount, rounded rates, and a seven-day window. |
| 4 | Missing `explain_endpoint` route | There was no way to inspect the named endpoints' execution plans. | Every request to `/admin/explain/{endpoint}` returned 404. | Added `executionStats` explains using matching endpoint query/pipeline definitions and JSON-safe explain output. |
| 5 | `department_summary` rounding | MongoDB `$round` applied ties-to-even before the Python half-up step, losing precision needed by R8. | A mean such as 9.125 could return 9.12 instead of 9.13. | Keep the aggregate mean unrounded and apply half-up rounding once in Python. |
| 6 | `EmployeeIn` | Employee fields, date/time formats, and equal shifts were not validated against `EmployeeCreate`. | Invalid employee codes, impossible dates, or `25:00` shifts passed model validation. | Added contract-sized/pattern constraints, real calendar-date validation, and a differing-shifts check. |
| 7 | `PunchInIn`, `PunchOutIn`, `RegularizeIn` | Plain `int` fields allowed Pydantic to coerce booleans, floats, and numeric strings; there was no maximum timestamp bound. | Inputs such as `true`, `"1783312500000"`, or an out-of-range integer could be accepted/coerced. | Added strict integer epoch-millisecond fields bounded by the OpenAPI minimum and maximum; explicit null is rejected where the schema is non-nullable. |
| 8 | `employee_monthly` | `attendance_pct` was rounded to two decimal places instead of four for rates. | A result of 0.5 present days across 3 working days reported `16.67` instead of `16.6667`. | Apply decimal half-up rounding to four places for the percentage. |
| 9 | `regularize_attendance` compare-and-update | A legacy record without a `history` field could not satisfy the update filter `history: []`, so valid corrections always lost the compare-and-update. | Regularizing a record missing optional legacy history returned 409. | Match missing history with `$exists: false`, while retaining the existing optimistic concurrency check. |
| 10 | `department_summary` month parsing | December of year 9999 overflowed while constructing the next month, producing an unhandled error. | Requesting `month=9999-12` raised a server error. | Use the shared strict month parser, which converts unrepresentable next-month boundaries into HTTP 422. |
| 11 | Employee and attendance response serialization | Employee-list `created_at` was a BSON datetime and the sample legacy attendance record omitted schema-required fields; punch-out did not serialize existing history timestamps. | Responses contained ISO datetime strings or omitted required attendance fields. | Serialize BSON instants to epoch milliseconds, strip `_id`, and supply contract defaults for absent legacy attendance fields. |

Also note anything you looked at and decided was **not** a defect, and why.

The existing department summary route was already present, so it was retained rather than reimplemented. Its equivalent
aggregation builder is also used by the explain route. This change addresses the missing routes; it is not a line-by-line
audit of unrelated starter code.

The late-arrival, overtime, overnight-shift, and half-day calculation helpers already followed their stated thresholds and
were retained. The provided sample documents are valid examples, not a complete analytics test set: monthly and trend
results must also honor weekends, join dates, employees with no logs, and records outside the shown sample.
