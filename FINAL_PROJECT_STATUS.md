# HR Reporting Pipeline — Final Project Status

This file is the single-page status snapshot. For deep references, see
[README.md](README.md), [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md),
and [HR_REPORTING_RULES_MASTER.md](HR_REPORTING_RULES_MASTER.md).

## Implemented Features

### Ingestion & validation
- Loaders for the three monthly Odoo / BioTime exports
  (`attendance_raw.xlsx`, `Resources (resource.resource).xlsx`,
  `Time Off Custom - Simplified Duration Calculation (hr.leave).xlsx`).
- Pre-flight validators: required columns, empty file detection,
  invalid dates / punch states, duplicate rows, blank Working Time,
  unknown leave status. Hard failures raise `ValidationError`;
  soft issues land in the log as warnings.

### Attendance classification
- Per-employee shift handling using the Odoo `resource.resource`
  Working Time label (first HH:MM AM/PM token).
- Five attendance statuses: `Late`, `On Time`, `Approved Excuse`,
  `Leave`, `Missing Schedule`.
- 15-minute grace period (`GRACE_MINUTES`, configurable).
- Partial hourly excuse handling: overlap between the delay window
  and the approved excuse window is deducted.
- Leave wins over Excuse priority (HR_REPORTING_RULES_MASTER rule 3).
- Negative delay (early arrival) clamped to zero.

### Risk, payroll, reconciliation
- Compound per-employee `risk_score` + `risk_level` + `risk_reason`
  driven by late frequency, unexcused minutes, missing check-outs,
  and excessive excuses.
- Payroll deduction estimate per employee (`estimated_deduction`,
  `deduction_capped`); totals on Dashboard.
- Auditable employee-count taxonomy:
  `attendance_file_employees`, `employees_with_checkins`,
  `scheduled_employees`, `employees_missing_schedule`,
  `reporting_population`. `total_employees` retained as a
  backward-compat alias.
- Employee Master + HR Audit Flags: `chronic_lateness`,
  `repeated_missing_checkouts`, `excessive_excuses`,
  `no_assigned_schedule`, `attendance_anomaly`.
- `data_quality_score` (0..100) summarizing missing schedules,
  missing checkouts, orphan rows, duplicate names, missing IDs,
  invalid punches.
- Overtime analytics: `extract_shift_end` from Odoo Working Time,
  per-day overtime classification (`Overtime` / `No Overtime` /
  `Missing Check Out` / `Missing Schedule`), configurable grace and
  minimum thresholds, night-shift handling (Shift End / Check Out
  roll to next day), dashboard KPIs, Top Overtime Employees chart,
  dedicated Overtime sheet, and a Claude Overtime Analysis section.
- Employee exclusion rules sourced from `data/excluded_employees.xlsx`
  (optional). Per-employee booleans selectively suppress contribution
  to the Late, Overtime, Payroll, or Risk Scoring KPIs while leaving
  operational rows visible. ID match takes priority; normalized-name
  match is the fallback (toggle with
  `ALLOW_NAME_BASED_EXCLUSION_MATCH`).

### Reporting outputs
- Multi-sheet Excel report with embedded dashboard charts:
  Dashboard (KPIs + pie/bar/line charts), Employee Summary,
  Daily Attendance, Daily Trend, Missing Punches (optional),
  Department Summary (optional), Employee Reconciliation Details,
  Employee Master.
- Claude-facing Markdown brief (`claude_hr_report_input.md`) with
  Executive Summary, Summary KPIs, Employee Count Reconciliation,
  Data Quality, Status Breakdown, Excused vs Unexcused, Daily Trend,
  Top Late Employees, HR Audit Flags, Department Summary, Approved
  Excuse Records, Missing Punch Analysis, Missing Schedule Employees,
  Late Attendance Records, Business Logic Notes, Instructions for
  Claude.
- Output versioning: every run writes to
  `outputs/YYYY-MM/hr_report_*.xlsx` and
  `outputs/YYYY-MM/claude_hr_report_input.md`.

### Operations
- CLI period filter: `--month YYYY-MM` or `--from / --to YYYY-MM-DD`.
- Centralized configuration in `src/config.py` (env-driven defaults):
  `GRACE_MINUTES`, `RISK_HIGH_THRESHOLD`, `RISK_MEDIUM_THRESHOLD`,
  `LATE_MINUTE_COST`, `MAX_MONTHLY_DEDUCTION`, `LOG_LEVEL`,
  `REPORT_OUTPUT_DIR`.
- Structured logging to `logs/pipeline.log` (phase counts, validation
  warnings, single metric-summary line per run).
- pytest test suite under `tests/`.
- `src/pdf_exporter.py` placeholder for the future PDF channel.

## Architecture Summary

```
data/        loaders         validators        metrics_calculator
  *.xlsx -->  data_loader --> validators -->     calculate_metrics
                                                  |
                                                  v
                                     summary dict + daily DataFrame
                                                  |
                              +-------------------+--------------------+
                              v                   v                    v
                       report_generator    excel_exporter        ai_summary_generator
                        (console)         (Dashboard, sheets,     (Claude .md brief)
                                           4 charts)
```

Modules: `config`, `data_loader`, `validators`, `metrics_calculator`,
`excel_exporter`, `ai_summary_generator`, `report_generator`,
`odoo_client` (XML-RPC scaffold), `pdf_exporter` (placeholder).

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the full diagram
and per-rule calculation details.

## Current KPIs (last full-month run, 2026-05 data)

| KPI | Value |
|---|---|
| Reporting Population | 45 |
| Attendance File Employees | 70 |
| Scheduled Employees (Odoo) | 62 |
| Employees Missing Schedule | 1 |
| Late Cases | 128 |
| Total Late Minutes (Unexcused) | 7,530 |
| Approved Excuse Cases | 8 |
| Leave Cases | 1 |
| Missing Check-Out Cases | 51 |
| High Risk Employees | 4 |
| Estimated Deduction (capped) | 12,690 |
| Data Quality Score | 87.9 / 100 |

## Future Roadmap

- **PDF export**: implement `pdf_exporter.export_pdf` (renderer choice,
  Arabic / RTL fonts, page templates).
- **Live Odoo pull**: replace the static XLSX exports with
  `odoo_client.fetch_employee_schedules` and an equivalent attendance
  fetch.
- **Overnight shifts**: handle shifts that cross midnight (current data
  contains daytime / evening shifts only).
- **Per-tenant config**: move audit flag thresholds and risk weights
  out of `metrics_calculator` into `config.py` so HR can tune severity
  bands without a code change.
- **Persistent store**: keep monthly runs in SQLite / Postgres so the
  trend chart can extend beyond a single month.
- **Approval workflow**: gate payroll deduction with an HR sign-off
  step before publishing.

## Known Limitations

- Excuse detection is keyword-based on `Time Off Type`
  (`استأذان` / `استئذان` / `excuse` / `permission`). Anything else with
  `Status = Approved` is treated as a Leave.
- Department mapping uses the first non-null Department per
  Employee ID. Employees that switch departments mid-month appear
  under the first one seen.
- Overnight shifts are not specially handled; the partial-overlap math
  is correct for same-day shift windows.
- BioTime exports may contain inactive / decommissioned IDs that have
  no Check In; these are surfaced via the Employee Reconciliation
  Details sheet but do NOT inflate the headline `reporting_population`.
- The pipeline currently consumes static Excel exports; the
  `src/odoo_client.py` XML-RPC scaffold is not yet wired into `main.py`.
