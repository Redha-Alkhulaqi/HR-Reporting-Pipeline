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
