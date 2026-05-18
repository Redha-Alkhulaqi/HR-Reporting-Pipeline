# HR Reporting Pipeline — Agent Instructions

## Project Purpose

A **monthly HR attendance reporting pipeline** that:
- Ingests BioTime punch data (Check In/Out) and Odoo HR metadata (employee shifts, time off)
- Classifies each employee day into one of 5 statuses: **Late**, **On Time**, **Approved Excuse**, **Leave**, **Missing Schedule**
- Computes compound risk scores (late frequency + unexcused minutes + missing check-outs + excuses)
- Generates Excel dashboard (HR/payroll review) + Markdown brief (for LLM like Claude to draft executive summary)
- Produces policy-compliant payroll deduction estimates and audit trail

**Key stakeholder**: HR Dept. Needs monthly attendance reports with clear risk flags, policy audit trail, and an LLM-friendly summary for executive review.

See [README.md](README.md) for full feature list and [FINAL_PROJECT_STATUS.md](FINAL_PROJECT_STATUS.md) for current KPIs and roadmap.

---

## Quick Start

### Environment Setup
```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### Run the Pipeline
```powershell
# Entire pipeline (all attendance data)
python src/main.py

# Specific month
python src/main.py --month 2026-05

# Custom date range
python src/main.py --from 2026-05-01 --to 2026-05-15
```

### Run Tests
```powershell
pytest tests/                           # Full suite
pytest tests/test_metrics.py -v         # Specific test file
pytest tests/test_timeoff_logic.py -v   # Leave/excuse logic
pytest tests/test_overtime.py -v        # Overtime calculation
```

### Key Output Directories
- **outputs/YYYY-MM/** — Generated Excel + Markdown briefs
  - `hr_report_YYYYMMDD_HHMMSS.xlsx` — Multi-sheet dashboard
  - `claude_hr_report_input.md` — Markdown brief for Claude
- **tests/** — 14 test files covering all business logic

---

## Architecture & Module Responsibilities

### Core Data Flow
```
data/*.xlsx (BioTime/Odoo exports)
    ↓ (data_loader)
Raw DataFrames (punches, resources, time off)
    ↓ (validators)
Validation passed; optional fixes applied (manual punches, ID aliases, exclusions)
    ↓ (metrics_calculator)
Daily attendance records with status, risk, payroll, overtime
    ↓ (report_generator + exporters)
Excel dashboard + Markdown brief → outputs/YYYY-MM/
```

### Module Map

| Module | File | Role |
|--------|------|------|
| **main** | [src/main.py](src/main.py) | Entry point; CLI argparse; orchestrates pipeline |
| **config** | [src/config.py](src/config.py) | All tunables (env-driven via .env) — single source of truth |
| **data_loader** | [src/data_loader.py](src/data_loader.py) | Loads 3 Odoo/BioTime XLSX exports; applies alias & exclusion logic |
| **validators** | [src/validators.py](src/validators.py) | Pre-flight checks; raises ValidationError for blocking issues |
| **metrics_calculator** | [src/metrics_calculator.py](src/metrics_calculator.py) | **Core logic**: shift parsing, delay calc, risk scoring, overtime |
| **excel_exporter** | [src/excel_exporter.py](src/excel_exporter.py) | Multi-sheet workbook with embedded KPI charts |
| **ai_summary_generator** | [src/ai_summary_generator.py](src/ai_summary_generator.py) | Markdown brief + auto-generated Executive Summary for Claude |
| **report_generator** | [src/report_generator.py](src/report_generator.py) | Console KPI output |
| **manual_punch_corrections** | [src/manual_punch_corrections.py](src/manual_punch_corrections.py) | Loads camera-verified punch corrections from optional XLSX |
| **odoo_client** | [src/odoo_client.py](src/odoo_client.py) | XML-RPC scaffold for future live Odoo integration |

---

## Business Logic & Conventions

### Attendance Status Classification (Mutually Exclusive)

Each employee-day is classified into exactly one of five statuses (priority order matters):

1. **Missing Schedule** — Employee has punches but no shift defined in Odoo export
2. **Leave** — Employee has an approved LEAVE entry overlapping the day (highest priority per Rule 3)
3. **Approved Excuse** — Employee has an approved hourly excuse (استأذان/permission) overlapping the delay window
4. **Late** — Unexcused delay > `GRACE_MINUTES` (default: 15 min)
5. **On Time** — Everything else

**Key insight**: Leave takes priority over excuses (Rule 3). An employee on Leave cannot be "Excused Late"—they are Leave.

### Delay & Lateness Calculation

```
delay_minutes = Check_In_Time − Shift_Start_Time
              (negative means early; clamped to 0 for missing schedule)
              
is_late = (delay_minutes > GRACE_MINUTES) AND NOT excused
```

**Excuse overlap**: If an approved excuse spans part of the delay window, the overlapped portion reduces the delay. Partial excuses are supported.

### Risk Score Compound Calculation

Risk score (0–100, auditable) = sum of:
- Late frequency contribution: `min(late_count × 2, 40)`
- Unexcused delay minutes: `min(total_unexcused_minutes ÷ 60, 20)`
- Missing check-outs: `min(missing_checkout_count × 2, 20)`
- Repeated excuses: `min(excuse_count, 5)`

**Risk levels**:
- **High**: score ≥ 30
- **Medium**: score ≥ 15
- **Low**: score < 15

See [HR_REPORTING_RULES_MASTER.md](HR_REPORTING_RULES_MASTER.md) for complete business rules (11 rules covering leave priority, grace, finalization, etc.).

### Overtime Detection

Per-employee per-day:
```
overtime_minutes = max(0, Check_Out_Time − Shift_End_Time)
```

**Night-shift handling**: If Check Out or Shift End is before Check In (e.g., employee clocks out at 1 AM), that punch rolls to the next day for comparison.

**Status**: `No Overtime` | `Minor Overtime` | `Significant Overtime` (threshold configurable in [config.py](src/config.py))

### Shift Parsing Convention

Odoo `Working Time` label format (Arabic or English):
```
دوام صباحى (9:00AM-6:00PM)
Morning Shift (09:00-17:00)
```

**Extraction**: Parse first `HH:MM` or `H:MM` token as Shift Start using regex. See `extract_shift_start()` in [metrics_calculator.py](src/metrics_calculator.py).

### Employee Taxonomies (Auditable)

Not just `nunique(Employee_ID)`, but explicit counts:
- **attendance_file_employees** — unique IDs in BioTime export
- **employees_with_checkins** — unique IDs with ≥1 Check In during period
- **scheduled_employees** — unique names with Working Time in Odoo export
- **employees_missing_schedule** — Check In IDs absent from Odoo resources
- **reporting_population** — published KPI (= employees_with_checkins)

Discrepancies surface data sync gaps.

### Exclusion Policies

Optional [data/excluded_employees.xlsx](data/excluded_employees.xlsx) flags owners/executives/exempt staff. Per-employee, per-KPI:
- Exclude from **Late** KPI
- Exclude from **Overtime** KPI
- Exclude from **Payroll Deduction** KPI
- Exclude from **Risk Scoring** KPI

With flag `HIDE_EXCLUDED_EMPLOYEES_FROM_REPORT=true` (default), excluded employees are removed from all report sheets and Claude markdown (not just KPI totals).

---

## Recent Additions (2026-05)

### Employee ID Aliases
Optional [data/employee_id_aliases.xlsx](data/employee_id_aliases.xlsx) remaps BioTime device IDs to current Odoo IDs (e.g., old device ID → new corporate ID). Applied immediately after load, before validation. Logged warnings if one old ID maps to multiple current IDs (first wins).

### Manual Punch Corrections
Optional [data/manual_forgotten_punches.xlsx](data/manual_forgotten_punches.xlsx) supplies camera-verified punches (e.g., security override footage). Applied before alias remapping. Fills missing punches; respects `ALLOW_OVERRIDE_EXISTING_PUNCH` config flag.

### Hide Excluded Employees
New config flag `HIDE_EXCLUDED_EMPLOYEES_FROM_REPORT` (default True). When enabled, excluded employees vanish from ALL report sheets and Claude markdown—not just KPI totals. HR can publish a clean report without exemptions visible to payroll.

---

## Common Pitfalls & Limitations

### ⚠️ Do NOT

1. **Hardcode config values.** Use [config.py](src/config.py) or `.env` file. All tunables go there.
2. **Assume all employees have a defined shift.** Missing Schedule is a valid status; test for it explicitly.
3. **Forget about day-of-month exclusion.** Rule 9 excludes the current day from final KPIs (data may still be incomplete).
4. **Mix leave and excuse logic.** Leave has absolute priority (Rule 3); an employee on Leave cannot be "Excused Late."
5. **Hardcode Arabic or English shift labels.** Use `extract_shift_start()` with fuzzy regex matching; support both.

### Known Limitations

1. **Excuse detection is keyword-based.** Time Off Type must contain `استأذان`, `استئذان`, `permission`, or `excuse`. Anything else with `Status=Approved` is treated as Leave.
2. **Overnight shifts not battle-tested.** Shift rollover works (next day), but only day/evening data in production so far. Cross-midnight shifts welcome.
3. **Department mid-month changes.** Employees switching departments appear in both. First non-null Department per ID is used.
4. **Current day always pending.** Today's data is incomplete; excluded from KPIs automatically (Rule 9).
5. **Finalization fragility.** If many employees have missing check-outs on a prior day, that day also marked pending (Rule 8).

### Environment Issues

- **Python 3.8+** required (f-strings, type hints).
- **.env file** is optional but strongly recommended. Copy from [config.py](src/config.py) defaults if needed.
- **XLSX parsing** assumes openpyxl (not xlrd); files must be Excel 2007+ format.
- **Locale-dependent date parsing** can fail if system locale differs from expected. Use ISO 8601 (YYYY-MM-DD) format in .env and CLI args.

---

## Key Files & Navigation

### Domain Authority
- [HR_REPORTING_RULES_MASTER.md](HR_REPORTING_RULES_MASTER.md) — 11 complete business rules (gospel truth for all logic)
- [FINAL_PROJECT_STATUS.md](FINAL_PROJECT_STATUS.md) — Current KPIs, feature status, known gaps, 2026 roadmap
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — Detailed module relationships & design decisions

### Implementation Examples
- [tests/test_metrics.py](tests/test_metrics.py) — Fixture pattern (`_make_inputs()`) and basic lateness/risk tests
- [tests/test_timeoff_logic.py](tests/test_timeoff_logic.py) — Leave priority, excuse overlap logic (exemplifies Rule 3)
- [tests/test_overtime.py](tests/test_overtime.py) — Overtime calculation with night-shift rollover
- [tests/test_id_aliases.py](tests/test_id_aliases.py) — Employee ID remapping behavior
- [tests/conftest.py](tests/conftest.py) — Pytest setup; adds `src/` to path

### Entry Points for Common Tasks

| Task | Start Here |
|------|-----------|
| Add a new KPI or metric | [src/metrics_calculator.py](src/metrics_calculator.py) — extend `calculate_metrics()` |
| Change risk score weights | [src/config.py](src/config.py) — tunables at top; test in [tests/test_metrics.py](tests/test_metrics.py) |
| Add a new report sheet | [src/excel_exporter.py](src/excel_exporter.py) — add sheet with `ws.append()` or `ws.write()` |
| Modify Claude brief format | [src/ai_summary_generator.py](src/ai_summary_generator.py) — update Markdown template strings |
| Add data validation | [src/validators.py](src/validators.py) — add a check function; raise `ValidationError` |
| Debug a specific day | [src/report_generator.py](src/report_generator.py) — filter and print; add breakpoint |
| Test a business rule | [tests/](tests/) — follow pattern in `test_metrics.py`, fixture-based |

---

## When Working on This Code

### Style & Conventions

- **Dataframe columns**: Capitalized with underscores (e.g., `Employee_ID`, `Check_In`, `risk_score`).
- **Config keys**: UPPERCASE with underscores (e.g., `GRACE_MINUTES`, `MAX_MONTHLY_DEDUCTION`).
- **Function names**: snake_case; descriptive of side effects or return type (e.g., `calculate_metrics()`, `extract_shift_start()`).
- **Test fixture pattern**: `def _make_inputs()` returns a dict of test DataFrames.

### Before Changing Business Logic

1. Check [HR_REPORTING_RULES_MASTER.md](HR_REPORTING_RULES_MASTER.md) for the authoritative rule.
2. Search [tests/](tests/) for existing tests of that rule; run them first.
3. Update tests alongside logic (or add new tests).
4. Run the full test suite: `pytest tests/ -v`.

### Debugging Tips

- **Enable debug logging**: Set `DEBUG=true` in `.env`.
- **Inspect intermediate data**: Use [src/report_generator.py](src/report_generator.py) console output or `print(df.head())` in metrics_calculator.
- **Use pytest fixtures**: Write a test with `_make_inputs()` to isolate a single scenario.
- **Check exclusion flags**: If a metric isn't in the report, verify [data/excluded_employees.xlsx](data/excluded_employees.xlsx) doesn't exclude that employee.

---

## Testing Strategy

- **Unit tests** ([tests/test_*.py](tests/)) cover individual business rules with minimal fixtures.
- **Fixtures** are defined per test file using `_make_inputs()` pattern (minimal DataFrames for focus).
- **Integration**: Full pipeline runs via `main.py` and writes to `outputs/YYYY-MM/`. Inspect Excel + Markdown for sanity.
- **Regression**: Add a test for every bug fix; commit test + fix together.

Run often: `pytest tests/ -v`

---

## Output Structure

Each run creates `outputs/YYYY-MM/` with:

```
hr_report_YYYYMMDD_HHMMSS.xlsx
├── Dashboard (KPIs + 4 embedded charts)
├── Employee Summary (per-employee risk, payroll, exclusion flags)
├── Daily Attendance (detail rows)
├── Daily Trend (daily KPI rollup)
├── Overtime (if overtime enabled)
├── Top Overtime Employees (chart)
├── Missing Punches (optional; blank if none)
├── Department Summary (optional; if Department column detected)
├── Employee Reconciliation Details (audit trail)
└── Employee Master (master reference data)

claude_hr_report_input.md
├── Executive Summary (auto-generated highlights + risks)
├── KPI Highlights
├── Risk Analysis
├── Overtime Analysis (if enabled)
└── Department Breakdown (if available)
```

Claude consumes the Markdown file to draft the final executive report.

---

## Feedback & Iteration

- **Unclear sections**: Ask questions in pull requests or issues.
- **Found a bug**: Add a test first, then fix (prevent regression).
- **New business rule**: Update [HR_REPORTING_RULES_MASTER.md](HR_REPORTING_RULES_MASTER.md), then implement + test.
- **Config missing**: Add to [config.py](src/config.py) with a sensible default; document in `.env.example`.

---

## Future Roadmap

From [FINAL_PROJECT_STATUS.md](FINAL_PROJECT_STATUS.md):
- **Live Odoo pull** — Replace XLSX exports with XML-RPC calls (scaffold in [odoo_client.py](src/odoo_client.py))
- **Cross-midnight shifts** — Full support for overnight shift logic
- **Per-tenant config** — Externalize risk weights and thresholds without code changes
- **Persistent storage** — SQLite/Postgres for trend charts beyond 1 month
- **Approval workflow** — HR sign-off before payroll deduction finalized
- **PDF export** — Alternative to Excel for printing

