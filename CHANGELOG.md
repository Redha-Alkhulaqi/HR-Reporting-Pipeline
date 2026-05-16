# Changelog

## 2026-05-16 (Employee count reconciliation)
- Replaced the ambiguous `total_employees` KPI with five explicit,
  auditable counts in `summary`: `attendance_file_employees`,
  `employees_with_checkins`, `scheduled_employees`,
  `employees_missing_schedule`, `reporting_population`.
  `total_employees` is retained as a backward-compat alias of
  `reporting_population`.
- Added an `employee_reconciliation` DataFrame (metric / count / source
  / definition) and surfaced it as a new "Employee Reconciliation"
  section on the Excel Dashboard.
- Added a per-ID `Employee Reconciliation Details` sheet listing
  `Employee ID, First Name, has_schedule, has_checkin,
  attendance_status_count` so HR can chase every discrepancy.
- Added an "Employee Count Reconciliation" section to the Claude
  Markdown brief that explains why our headline can differ from the
  Odoo Employees screen and the BioTime dashboard.
- Documented the count taxonomy in docs/ARCHITECTURE.md.

## 2026-05-16 (Phase 3)
- Added payroll deduction estimation (LATE_MINUTE_COST and
  MAX_MONTHLY_DEDUCTION; per-employee and total figures on the
  dashboard and in the Claude report).
- Replaced minutes-only risk banding with a compound risk_score
  (late frequency, unexcused minutes, missing check-outs, excuses),
  plus risk_reason text on every employee row.
- Added an auto-generated Executive Summary section to the Claude
  Markdown brief (highlights, top concerns, operational risks,
  HR recommendations, payroll impact, action plan).
- Introduced a validators.py layer (required columns, empty file,
  invalid dates, unexpected punch states, duplicate rows). Pipeline
  fails fast with a clear message on hard errors and logs warnings
  otherwise.
- Improved structured logging: phase counts, validation summaries,
  metric counters in one line.
- Centralized every tunable in src/config.py (grace period, risk
  thresholds, payroll rates, output dir).
- Output versioning: reports now land under outputs/YYYY-MM/.
- New docs/ARCHITECTURE.md describing pipeline flow, modules,
  calculations, output structure, and roadmap.

## 2026-05-16
- Added attendance status classification.
- Added partial hourly excuse handling.
- Added excused vs unexcused summaries.
- Improved Excel dashboard.
- Improved Claude input report.
- Added next-phase analytics enhancements.
