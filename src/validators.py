"""Pre-flight validation for the monthly pipeline inputs.

Each validator returns a ValidationReport describing what it found:
- row_count keeps the volume signal
- warnings collect non-fatal issues that should be surfaced to the log

ValidationError is raised only when the file cannot be processed at all
(missing required columns, empty file). The pipeline's main try/except
catches the error and logs it before exiting non-zero.
"""
from dataclasses import dataclass, field
from typing import List

import pandas as pd


class ValidationError(Exception):
    """Raised when an input cannot be processed and the pipeline must stop."""


@dataclass
class ValidationReport:
    label: str
    row_count: int = 0
    warnings: List[str] = field(default_factory=list)

    def add(self, message):
        self.warnings.append(message)


_REQUIRED_ATTENDANCE = {
    "Employee ID", "First Name", "Date", "Punch Time", "Punch State",
}
_VALID_PUNCH_STATES = {"Check In", "Check Out", "Break In", "Break Out"}

_REQUIRED_SCHEDULES = {"Name", "Working Time"}

_REQUIRED_TIME_OFF = {
    "Employee", "Status", "Start Date", "End Date", "Time Off Type",
}


def _ensure_columns(df, required, label):
    missing = required - set(df.columns)
    if missing:
        raise ValidationError(
            f"{label}: missing required columns {sorted(missing)}"
        )


def validate_attendance(df):
    if df is None or df.empty:
        raise ValidationError("Attendance file is empty.")
    _ensure_columns(df, _REQUIRED_ATTENDANCE, "Attendance file")

    report = ValidationReport(label="attendance", row_count=len(df))

    invalid_dates = pd.to_datetime(df["Date"], errors="coerce").isna().sum()
    if invalid_dates:
        report.add(f"{invalid_dates} rows have unparseable Date values.")

    invalid_states = (~df["Punch State"].isin(_VALID_PUNCH_STATES)).sum()
    if invalid_states:
        report.add(
            f"{invalid_states} rows have unexpected Punch State values."
        )

    duplicate_count = int(df.duplicated().sum())
    if duplicate_count:
        report.add(f"{duplicate_count} fully-duplicate rows.")

    return report


def validate_schedules(df):
    if df is None or df.empty:
        raise ValidationError("Schedules file is empty.")
    _ensure_columns(df, _REQUIRED_SCHEDULES, "Schedules file")

    report = ValidationReport(label="schedules", row_count=len(df))
    duplicate_names = int(df["Name"].duplicated().sum())
    if duplicate_names:
        report.add(f"{duplicate_names} duplicate employee Name rows.")
    blank_working_time = int(df["Working Time"].isna().sum())
    if blank_working_time:
        report.add(
            f"{blank_working_time} rows have a blank Working Time; "
            "those employees will appear as Missing Schedule."
        )
    return report


def validate_time_off(df):
    # time_off is optional; None / empty passes silently.
    if df is None or df.empty:
        return ValidationReport(label="time_off", row_count=0)
    _ensure_columns(df, _REQUIRED_TIME_OFF, "Time off file")

    report = ValidationReport(label="time_off", row_count=len(df))

    invalid_start = pd.to_datetime(df["Start Date"], errors="coerce").isna().sum()
    invalid_end = pd.to_datetime(df["End Date"], errors="coerce").isna().sum()
    if invalid_start or invalid_end:
        report.add(
            f"{invalid_start} rows have invalid Start Date; "
            f"{invalid_end} rows have invalid End Date."
        )

    unknown_status = (~df["Status"].isin({"Approved", "To Approve", "Refused"})).sum()
    if unknown_status:
        report.add(f"{unknown_status} rows have an unexpected Status value.")

    return report
