"""Centralized configuration: env-driven defaults for the pipeline.

Override any value via .env or environment variables. Keeping every tunable
here lets the rest of the code remain stable when business rules change.
"""
from dotenv import load_dotenv
from pathlib import Path
import os

load_dotenv()

# Path constants.
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Environment.
APP_ENV = os.getenv("APP_ENV", "development")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# Reporting output. Reports are written under
# REPORT_OUTPUT_DIR / YYYY-MM / hr_report_*.xlsx (see excel_exporter).
REPORT_OUTPUT_DIR = PROJECT_ROOT / os.getenv("REPORT_OUTPUT_DIR", "outputs")

# Lateness rules (HR_REPORTING_RULES_MASTER rule 6).
GRACE_MINUTES = int(os.getenv("GRACE_MINUTES", "15"))

# Risk scoring thresholds. Compared against the weighted risk_score
# computed in metrics_calculator -- NOT against raw late minutes.
RISK_HIGH_THRESHOLD = int(os.getenv("RISK_HIGH_THRESHOLD", "30"))
RISK_MEDIUM_THRESHOLD = int(os.getenv("RISK_MEDIUM_THRESHOLD", "15"))

# Payroll deduction estimation. LATE_MINUTE_COST is the deduction per
# unexcused minute of lateness (in the local payroll currency). The
# per-employee monthly total is capped at MAX_MONTHLY_DEDUCTION.
LATE_MINUTE_COST = float(os.getenv("LATE_MINUTE_COST", "5.0"))
MAX_MONTHLY_DEDUCTION = float(os.getenv("MAX_MONTHLY_DEDUCTION", "500.0"))

# Overtime rules.
# OVERTIME_GRACE_MINUTES: how long after Shift End a Check Out is still
#   considered the normal close-out rather than overtime.
# MIN_OVERTIME_MINUTES: floor below which any candidate overtime is
#   discarded (set 0 to count every minute beyond the grace).
OVERTIME_GRACE_MINUTES = int(os.getenv("OVERTIME_GRACE_MINUTES", "15"))
MIN_OVERTIME_MINUTES = int(os.getenv("MIN_OVERTIME_MINUTES", "30"))

# Early-leave rule. A Check Out before Shift End counts as Early Leave
# only when the gap exceeds this grace window.
EARLY_LEAVE_GRACE_MINUTES = int(os.getenv("EARLY_LEAVE_GRACE_MINUTES", "10"))

# Early-leave anomaly threshold. Above this many minutes the row is
# flagged for HR review (likely missing Check Out, wrong shift, device
# sync issue, or a partial attendance record). The row is NOT dropped
# from any KPI -- only annotated.
MAX_REASONABLE_EARLY_LEAVE_MINUTES = int(
    os.getenv("MAX_REASONABLE_EARLY_LEAVE_MINUTES", "180")
)

# Employee exclusions. When the exclusion file lacks an Employee ID for
# a row, fall back to matching the row's normalized Employee Name
# against the attendance file. Disable for stricter ID-only matching.
ALLOW_NAME_BASED_EXCLUSION_MATCH = (
    os.getenv("ALLOW_NAME_BASED_EXCLUSION_MATCH", "true").lower()
    in ("true", "1", "yes")
)

# When True, excluded employees are removed from every report-facing
# DataFrame before it leaves the pipeline -- the Excel workbook and
# the Claude markdown both behave as if those employees do not exist.
# The exclusion file and the validation log still note them for audit.
HIDE_EXCLUDED_EMPLOYEES_FROM_REPORT = (
    os.getenv("HIDE_EXCLUDED_EMPLOYEES_FROM_REPORT", "true").lower()
    in ("true", "1", "yes")
)

# Manual forgotten-punch correction safety toggle. When False (default)
# manual corrections only FILL missing punches -- they never overwrite an
# existing biometric one. Flip to True only when you intentionally want
# camera-verified times to take precedence over the device record.
ALLOW_OVERRIDE_EXISTING_PUNCH = (
    os.getenv("ALLOW_OVERRIDE_EXISTING_PUNCH", "false").lower()
    in ("true", "1", "yes")
)

# Break-out / break-in Punch State labels. Breaks are INFORMATIONAL
# only -- they never feed lateness, overtime, early leave, payroll, or
# risk scoring. Comma-separated to support env-driven overrides.
BREAK_OUT_STATES = [
    s.strip() for s in os.getenv(
        "BREAK_OUT_STATES",
        "Break Out,Lunch Out,Out Break,استراحة خروج",
    ).split(",") if s.strip()
]
BREAK_IN_STATES = [
    s.strip() for s in os.getenv(
        "BREAK_IN_STATES",
        "Break In,Lunch In,In Break,استراحة دخول",
    ).split(",") if s.strip()
]

# Absence rule. Weekday names (Python `%A` formatting: Monday, Tuesday,
# Wednesday, Thursday, Friday, Saturday, Sunday) that count as the
# weekly off day -- they are NEVER counted as absences. PUBLIC_HOLIDAYS
# is a comma-separated list of YYYY-MM-DD dates that are likewise
# excluded from the absence count.
WEEKLY_OFF_DAYS = [
    s.strip() for s in os.getenv("WEEKLY_OFF_DAYS", "Friday").split(",")
    if s.strip()
]
PUBLIC_HOLIDAYS = [
    s.strip() for s in os.getenv("PUBLIC_HOLIDAYS", "").split(",")
    if s.strip()
]
