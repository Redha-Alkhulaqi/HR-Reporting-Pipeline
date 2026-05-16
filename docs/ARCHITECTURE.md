# HR Reporting Pipeline — Architecture

This document describes the runtime architecture of the monthly HR
reporting pipeline. Read it together with
[HR_REPORTING_RULES_MASTER.md](../HR_REPORTING_RULES_MASTER.md) for the
authoritative business rules.

## Pipeline Flow

```
data/                        src/                       outputs/YYYY-MM/
+----------------------+     +----------------------+   +---------------------+
| attendance_raw.xlsx  | --> | data_loader.py       |   |                     |
| Resources (...).xlsx | --> |  load_attendance_*   |   |                     |
| Time Off (...).xlsx  | --> |  load_working_*      |   |                     |
+----------------------+     |  load_time_off_*     |   |                     |
                             +----------+-----------+   |                     |
                                        |               |                     |
                                        v               |                     |
                             +----------------------+   |                     |
                             | validators.py        |   |                     |
                             |  validate_attendance |   |                     |
                             |  validate_schedules  |   |                     |
                             |  validate_time_off   |   |                     |
                             +----------+-----------+   |                     |
                                        |               |                     |
                                        v               |                     |
                             +----------------------+   |                     |
                             | metrics_calculator.py|   |                     |
                             |  calculate_metrics   |   |                     |
                             |  -> summary + daily  |   |                     |
                             +----+-----+------+----+   |                     |
                                  |     |      |        |                     |
                       +----------+     |      +---------+                    |
                       v                v                v                    |
              +----------------+  +-----------+  +------------------+         |
              | report_        |  | excel_    |  | ai_summary_      |  -->   | hr_report_*.xlsx
              | generator.py   |  | exporter  |  | generator.py     |        | claude_hr_report_input.md
              | (console only) |  +-----------+  +------------------+         |
              +----------------+                                              +---------------------+
```

`main.py` orchestrates the flow, surfaces structured log lines via
`logging`, and stops the run cleanly on `ValidationError`.

## Modules

| Module | Purpose |
|---|---|
| `config.py` | Single source of truth for tunables. Reads env / .env. |
| `data_loader.py` | Thin CSV/XLSX loaders dispatched by extension. |
| `validators.py` | Pre-flight checks; raises `ValidationError` if a file cannot be processed. |
| `metrics_calculator.py` | Builds `daily` DataFrame and the `summary` dict. |
| `excel_exporter.py` | Multi-sheet workbook with embedded dashboard charts. |
| `ai_summary_generator.py` | Claude-facing Markdown brief, including an Executive Summary. |
| `report_generator.py` | Console KPIs (DataFrames suppressed). |
| `odoo_client.py` | XML-RPC scaffold for future direct Odoo integration. |
| `pdf_exporter.py` | Placeholder for a future PDF export channel (not wired into main yet). |
| `validators.py` | Pre-flight validation; ValidationError stops the run, warnings get logged. |

## Business Rules (summary)

| Rule | Source | Effect on `attendance_status` |
|---|---|---|
| **6 — Grace period** | `GRACE_MINUTES` (15 by default) | Within grace → not Late. |
| **5 — Partial excuse** | `استأذان` keyword + overlap math | Excuse reduces delay by overlap minutes only. |
| **3 — Leave priority** | Approved non-excuse leave covering check-in | Status becomes `Leave`; supersedes excuse. |
| **8 — Missing punch** | Day has Check In but no Check Out | Flagged on `missing_check_out`; surfaced in report. |
| **Schedule lookup** | Odoo `resource.resource` -> `Working Time` | Absent name → `Missing Schedule`. |

## Employee Count Taxonomy

A single "total employees" number is dangerously ambiguous, so the
pipeline publishes five explicit counts (every one of them lives on the
`summary` dict and on the Dashboard's Employee Reconciliation table):

| Count | Source | Meaning |
|---|---|---|
| `attendance_file_employees` | `attendance_raw.xlsx` | Unique Employee IDs anywhere in the BioTime export (incl. inactive). |
| `employees_with_checkins` | attendance Check In rows | Unique IDs that recorded at least one Check In during the period. |
| `scheduled_employees` | Odoo `resource.resource` | Unique Names with a Working Time assignment. |
| `employees_missing_schedule` | derived | Check-in employees absent from the Odoo resources export. |
| `reporting_population` | derived (= `employees_with_checkins`) | The number we publish for this report. |

`total_employees` is kept as a backward-compat alias of
`reporting_population` and will be dropped once every consumer reads
the explicit counts.

## Calculations

### Lateness
- `Delay Minutes = Check In − Shift Start` (raw minutes, can be
  negative for early arrivals).
- For each approved EXCUSE row overlapping the delay window:
  `excused_delay_minutes += overlap`.
- `unexcused_delay_minutes = max(0, Delay Minutes) − excused_delay_minutes`.
- `is_late = unexcused_delay_minutes > GRACE_MINUTES` AND no Leave that
  day. Renders the `attendance_status` value.

### Risk score (compound)
```
score = min(late_count * 2, 40)
      + min(total_late_minutes // 60, 20)   # capped hours
      + min(missing_checkout_count * 2, 20)
      + min(excuse_count, 5)
```
Bands: `score >= RISK_HIGH_THRESHOLD` → High; `>= RISK_MEDIUM_THRESHOLD`
→ Medium; else Low. `risk_reason` is a short composed sentence listing
the contributing factors.

### Payroll
```
estimated_deduction = total_late_minutes * LATE_MINUTE_COST
deduction_capped    = min(estimated_deduction, MAX_MONTHLY_DEDUCTION)
```
Per-employee values live on `employee_summary`. The Dashboard exposes
totals: `total_estimated_deduction` and `total_deduction_capped`.

## Output Structure

```
outputs/
└── YYYY-MM/
    ├── hr_report_YYYYMMDD_HHMMSS.xlsx
    │   ├── Dashboard          (KPIs + 4 charts)
    │   ├── Employee Summary   (per-employee risk + payroll)
    │   ├── Daily Attendance   (one row per employee-day)
    │   ├── Daily Trend        (one row per date)
    │   ├── Missing Punches                (optional)
    │   ├── Department Summary             (optional)
    │   └── Employee Reconciliation Details (per-ID audit table)
    └── claude_hr_report_input.md
        ├── Executive Summary
        ├── Summary KPIs
        ├── Employee Count Reconciliation
        ├── Attendance / Excused breakdowns
        ├── Daily Trend
        ├── Top Late Employees
        ├── Department Summary
        ├── Approved Excuse / Missing Punch / Missing Schedule sections
        ├── Late Attendance Records
        ├── Business Logic Notes
        └── Instructions for Claude
```

## Logging

`logs/pipeline.log` is the durable record. Each run writes:
- one INFO line per phase (load, validate, compute, export);
- WARNING lines for every validator finding (invalid dates, duplicate
  rows, unknown leave statuses, etc.);
- an ERROR + traceback on failure.

## Future Roadmap

- Replace the XLSX exports with a live pull via `odoo_client.py`
  (XML-RPC `search_read`).
- Handle overnight shifts that cross midnight (currently absent from
  the source data but a likely future case).
- Move risk weights into config so HR can tune severity bands without a
  code change.
- Add an "approval workflow" sheet that requires sign-off before the
  payroll deduction is applied.
- Persist runs into a small SQLite / Postgres store so trend charts
  can extend beyond a single month.
