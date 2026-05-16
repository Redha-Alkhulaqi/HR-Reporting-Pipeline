# Changelog

## 2026-05-16 (Early leave + conditional formatting)
- Added Early Leave detection with `EARLY_LEAVE_GRACE_MINUTES` (default
  10) in `config.py`. A Check Out before Shift End counts as Early
  Leave only when the gap exceeds the grace window.
- Added daily columns `early_leave_minutes` and `early_leave_status`.
  Status values: `Early Leave`, `Normal`, `Missing Check Out`,
  `Missing Schedule`. Night-shift wraparound honored.
- Extended overtime / early-leave classification: both are computed
  in one pass against the same Shift End. Overtime status stays
  unchanged when an employee leaves early, and vice versa.
- Added summary KPIs: `early_leave_cases`,
  `total_early_leave_minutes`, `employees_with_early_leave`, plus a
  `top_early_leave_employees` DataFrame (honors `excluded_from_late`).
- Extended `employee_summary` with `early_leave_cases` and
  `total_early_leave_minutes`.
- Excel: new Early Leave sheet, two new Dashboard KPIs
  (`Early Leave Cases`, `Total Early Leave Minutes`), and a fifth
  Dashboard chart (`Top Early Leave Employees`) anchored under the
  existing 2x2 grid.
- Excel conditional formatting on Daily Attendance: per-row coloring
  by priority (Excluded > Leave > Approved Excuse > Late > Early
  Leave > Missing Check Out > Overtime) using FormulaRule with
  `stopIfTrue=True` so each row picks one color.
- Claude markdown: new `## Early Leave Analysis` section with the
  top early-leave employees table and the calculation notes.
- Added `tests/test_early_leave.py` covering normal close-out, grace
  threshold, beyond grace, missing check-out, missing schedule,
  night-shift wraparound, and the overtime-vs-early-leave distinction
  (7 new tests; 49 total).

## 2026-05-16 (Employee exclusion rules)
- Added optional `data/excluded_employees.xlsx` input with columns
  `Employee ID, Employee Name, Exclusion Reason, Exclude From Late,
  Exclude From Overtime, Exclude From Payroll Deduction,
  Exclude From Risk Scoring, Notes`. Loader returns an empty
  DataFrame when the file is absent so the feature is no-op by default.
- Added `load_excluded_employees_file` in data_loader.
- Added an exclusion engine in metrics_calculator that stamps every
  daily row with `is_excluded`, `exclusion_reason`,
  `excluded_from_late`, `excluded_from_overtime`,
  `excluded_from_payroll`, `excluded_from_risk`. Raw operational rows
  are NOT mutated -- only KPI aggregations honor the flags.
- KPI filters: `late_cases` / `total_late_minutes` drop rows where
  `excluded_from_late`; `overtime_cases` / `total_overtime_minutes`
  / `top_overtime_employees` drop rows where `excluded_from_overtime`;
  per-employee `estimated_deduction` / `deduction_capped` zero out
  when `excluded_from_payroll`; per-employee `risk_score` becomes 0
  and `risk_level` becomes `"Excluded"` when `excluded_from_risk`.
- Matching: Employee ID is the primary key. Rules without an ID fall
  back to normalized-name matching (lower-cased and whitespace-
  collapsed). Configurable via `ALLOW_NAME_BASED_EXCLUSION_MATCH`.
- Added an `excluded_employees_summary` DataFrame surfaced as a new
  `Excluded Employees` sheet in Excel and a new `## Excluded
  Employees` section in the Claude markdown.
- Added `tests/test_exclusions.py` covering ID-based, name-based,
  mixed-flag, and ID-over-name-priority cases (8 new tests; 42 total).
- Added `ALLOW_NAME_BASED_EXCLUSION_MATCH` to `config.py`.

## 2026-05-16 (Overtime analytics)
- Added `extract_shift_end` that pulls the Shift End from Odoo
  Working Time labels (uses the LAST end token for split shifts).
- Added overtime columns to daily: `Shift End`, `Shift End DateTime`,
  `Check Out DateTime`, `worked_minutes`, `scheduled_minutes`,
  `overtime_minutes`, `overtime_status`.
- Overtime is computed as `Check Out - Shift End` past the configured
  grace period and discarded under `MIN_OVERTIME_MINUTES`. Missing
  Check Out / Missing Schedule rows are surfaced with explicit
  status values rather than counted as overtime.
- Night-shift handling: Shift End and Check Out roll to the next day
  when their clock time precedes Shift Start / Check In.
- Added overtime KPIs (`overtime_cases`, `total_overtime_minutes`,
  `total_overtime_hours`, `employees_with_overtime`,
  `avg_overtime_minutes`) and a `top_overtime_employees` DataFrame.
- Extended `employee_summary` with per-employee overtime aggregates.
- Excel: new Overtime Summary KPIs and Top Overtime Employees chart
  on the Dashboard, plus a dedicated Overtime sheet.
- Claude markdown: new Overtime Analysis section with the top
  overtime employees and the calculation notes.
- Added `tests/test_overtime.py` covering the standard cases plus the
  night-shift wraparound (10 new tests; 34 total).
- Added `OVERTIME_GRACE_MINUTES` and `MIN_OVERTIME_MINUTES` to
  `config.py`.

## 2026-05-16 (Production hardening)
- Added Employee Master DataFrame (one row per Employee ID with Odoo
  Resource, Attendance Presence, Schedule Presence, Status Consistency,
  per-employee counters, and a comma-separated `audit_flags` column).
  Exposed as an `Employee Master` sheet in Excel.
- Added HR audit flags: `chronic_lateness`, `repeated_missing_checkouts`,
  `excessive_excuses`, `no_assigned_schedule`, `attendance_anomaly`.
- Added `data_quality_score` (0..100) computed from missing schedules,
  missing checkouts, orphan rows, duplicate names, missing employee IDs,
  and invalid punches; surfaced as a KPI on the Dashboard and in the
  Claude Executive Summary.
- Added per-source data-quality counters in summary:
  `orphan_attendance_records`, `duplicate_employee_names`,
  `missing_employee_ids`, `unscheduled_active_employees`,
  `invalid_punches_count`.
- Added CLI arguments: `--month YYYY-MM`, `--from`, `--to` for
  restricting the reporting period without editing code.
- Added `src/pdf_exporter.py` placeholder (export_pdf currently raises
  NotImplementedError; planned_output_path documents where the PDF
  will land once implemented).
- Added pytest suite under `tests/`:
  `test_metrics.py`, `test_validators.py`, `test_timeoff_logic.py`.
- Added `FINAL_PROJECT_STATUS.md`.

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
