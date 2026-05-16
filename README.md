# HR Reporting Pipeline

Monthly HR attendance pipeline that ingests BioTime punches and Odoo
metadata, classifies each employee day, and produces an Excel dashboard
plus a Markdown brief sized for an LLM (Claude) to draft the monthly
report.

## Current Features

- Per-employee shift handling using the Odoo `resource.resource` export
  (no single fixed shift).
- Attendance status classification with five values:
  `Late`, `On Time`, `Approved Excuse`, `Leave`, `Missing Schedule`.
- Partial hourly excuse handling (overlap between the delay window and
  the approved excuse window).
- Excused vs unexcused delay accounting.
- Compound per-employee risk scoring (numeric `risk_score`,
  `risk_level`, plus a human-readable `risk_reason`) based on late
  frequency, unexcused minutes, missing check-outs, and repeated
  excuses.
- Configurable payroll deduction estimation (`LATE_MINUTE_COST`,
  `MAX_MONTHLY_DEDUCTION`) with per-employee and pipeline totals.
- Department-level aggregation when a Department column is detected.
- Missing Check-Out detection (days with a Check In but no Check Out).
- Daily attendance trend.
- Multi-sheet Excel report with embedded dashboard charts.
- Claude-ready Markdown brief leading with an auto-generated Executive
  Summary (highlights, concerns, risks, recommendations, action plan).
- Validation layer that surfaces invalid dates, unexpected punch
  states, duplicate rows, etc. via the log.
- Output versioning under `outputs/YYYY-MM/`.
- Employee Master + HR Audit Flags (chronic lateness, repeated missing
  checkouts, excessive excuses, no assigned schedule, attendance
  anomalies) on every employee.
- `data_quality_score` (0-100) summarizing missing schedules, missing
  checkouts, orphan rows, duplicate names, invalid punches.
- CLI period filter: `--month YYYY-MM` or `--from/--to YYYY-MM-DD`.
- pytest test suite covering metrics, validators, and time-off logic.

## Input Files

The pipeline reads three files from `data/`:

| File | Source | Purpose |
|---|---|---|
| `attendance_raw.xlsx` | BioTime export | Punch events (one row per punch). |
| `Resources (resource.resource).xlsx` | Odoo `resource.resource` | Per-employee Working Time labels (used to derive shift start). |
| `Time Off Custom - Simplified Duration Calculation (hr.leave).xlsx` | Odoo `hr.leave` | Approved leaves and hourly excuses. |

Place each new month's export in `data/` and re-run the pipeline.

## Output Files

Written to `outputs/YYYY-MM/` (created if absent):

- `hr_report_YYYYMMDD_HHMMSS.xlsx` — multi-sheet Excel report:
  - **Dashboard** — KPIs, status / excused tables, charts.
  - **Employee Summary** — per-employee late aggregates + risk tier.
  - **Daily Attendance** — every classified employee-day row.
  - **Daily Trend** — per-date counts and unexcused minutes.
  - **Missing Punches** — days with a Check In but no Check Out (optional).
  - **Department Summary** — per-department breakdown (optional).
- `claude_hr_report_input.md` — Markdown brief consumed by Claude.

## Attendance Status Logic

For each `(Employee ID, Date)` row:

1. **Missing Schedule** — employee not in the Odoo resources export.
   Lateness cannot be computed; reported separately for manual review.
2. **Leave** — an approved LEAVE (Annual / Sick / etc.) whose window
   covers the check-in moment. Leave wins over Excuse per priority.
3. **Approved Excuse / Late** — approved EXCUSES
   (`استأذان`, `Permission`, etc.) reduce the delay by the overlap
   between `(Shift Start → Check In)` and `(Excuse Start → Excuse End)`.
   - If the residual unexcused delay > `GRACE_MINUTES` (15) → **Late**.
   - Else, if any excuse contributed → **Approved Excuse**.
4. **On Time** — everything else (including delays inside the grace
   period and early arrivals).

`late_cases` and `total_late_minutes` count only **Late** rows; excused
minutes never flow into `total_late_minutes`.

## How to Run

```powershell
# One-time setup
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# Drop the three monthly export files into data/, then:
python src/main.py
```

The script is CWD-independent (paths are resolved from `PROJECT_ROOT`).

### Restricting the reporting period

By default the pipeline processes every row in `attendance_raw.xlsx`.
To restrict to a window:

```powershell
python src/main.py --month 2026-05            # one calendar month
python src/main.py --from 2026-05-01 --to 2026-05-15   # custom range
```

`--month` is mutually exclusive with `--from / --to` and just expands
to that month's bounds.

### Running tests

```powershell
python -m pytest tests/
```

## How to Validate Results

1. **Pipeline ran clean** — last console line is
   `Pipeline completed successfully.` Logs land in `logs/pipeline.log`.
2. **Excel exists** in `outputs/` and opens with these sheets:
   `Dashboard`, `Employee Summary`, `Daily Attendance`, `Daily Trend`,
   plus `Missing Punches` and `Department Summary` when applicable.
3. **Numbers reconcile**:
   - `Dashboard → Late Cases` equals row count of
     `Daily Attendance` where `attendance_status == Late`.
   - `Dashboard → Total Late Minutes (Unexcused)` equals the sum of
     `unexcused_delay_minutes` over those Late rows.
   - `excused_delay_minutes` never contributes to `total_late_minutes`.
4. **Spot-check classification** — open `claude_hr_report_input.md`,
   check that "Approved Excuse Records" and "Late Attendance Records"
   look sensible against the source punches.

## Known Limitations

- Overnight shifts that cross midnight are not specially handled. The
  current data contains daytime and evening shifts only.
- Department mapping uses the first non-null Department per
  Employee ID. Employees that switch departments mid-month appear under
  the first one seen.
- Excuse detection is keyword-based on `Time Off Type`
  (`استأذان` / `استئذان` / `excuse` / `permission`). Anything else with
  `Status = Approved` is treated as a Leave.
- The pipeline currently consumes static Excel exports; the
  `src/odoo_client.py` XML-RPC scaffold is not yet wired into `main.py`.

## Documentation

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — pipeline flow,
  modules, calculations, output structure, future roadmap.
- [HR_REPORTING_RULES_MASTER.md](HR_REPORTING_RULES_MASTER.md) — master
  reporting rules (lateness, leaves, status priorities).
- [MONTHLY_HR_REPORTING_WORKFLOW.md](MONTHLY_HR_REPORTING_WORKFLOW.md) —
  the monthly operating workflow.
- [CHANGELOG.md](CHANGELOG.md) — release notes.
