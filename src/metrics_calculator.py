"""Compute attendance KPIs from BioTime punches and Odoo metadata.

Inputs
- df            : BioTime punches export. One row per punch event.
                  Required columns: Employee ID, First Name, Date,
                  Punch Time, Punch State.
- schedules_df  : Odoo resource.resource export. One row per employee.
                  Required columns: Name, Working Time.
- time_off_df   : Optional Odoo hr.leave export. One row per leave
                  request. Required columns: Employee, Status,
                  Start Date, End Date, Time Off Type. Only rows with
                  Status == "Approved" are honored.

Output of calculate_metrics
- summary : dict with total_employees, late_cases, total_late_minutes,
            and a per-employee employee_summary DataFrame.
- daily   : DataFrame with one row per (Employee ID, Date). Columns:
            Employee ID, First Name, Date, Check In (HH:MM:SS),
            Shift Start (HH:MM or NaN), missing_schedule (bool),
            Delay Minutes (int; 0 for missing-schedule rows),
            Check DateTime (Timestamp), has_time_off (bool),
            time_off_type (str or None), is_late (bool).

Lateness rule (HR_REPORTING_RULES_MASTER rule 6):
- A row is late when its Delay Minutes exceeds GRACE_MINUTES AND the
  employee did NOT have an approved leave covering that check-in.
- Employees absent from the schedules export get Delay Minutes 0 and
  are never marked late; missing_schedule surfaces them for review.
"""
import re
from datetime import datetime

import pandas as pd


GRACE_MINUTES = 15                # Rule 6 grace period after shift start.
RISK_HIGH_THRESHOLD = 1000        # Total late minutes that flag High Risk.
RISK_MEDIUM_THRESHOLD = 500       # Total late minutes that flag Medium Risk.

_SHIFT_TIME_RE = re.compile(r"\((\d{1,2}:\d{2}\s*[AP]M)", re.IGNORECASE)


def extract_shift_start(working_time):
    """Pull the first HH:MMAM/PM token from an Odoo Working Time label."""
    match = _SHIFT_TIME_RE.search(str(working_time))
    if not match:
        return None
    time_text = match.group(1).replace(" ", "").upper()
    return datetime.strptime(time_text, "%I:%M%p").strftime("%H:%M")


def classify_risk(minutes):
    if minutes >= RISK_HIGH_THRESHOLD:
        return "High Risk"
    if minutes >= RISK_MEDIUM_THRESHOLD:
        return "Medium Risk"
    return "Low Risk"


def _delay_minutes(punch_time, shift_start):
    check_in = datetime.strptime(punch_time, "%H:%M:%S")
    shift_start_time = datetime.strptime(shift_start, "%H:%M")
    return int((check_in - shift_start_time).total_seconds() / 60)


def _build_shift_lookup(schedules_df):
    """Return dict: cleaned employee name -> Shift Start (HH:MM)."""
    schedules = schedules_df[["Name", "Working Time"]].copy()
    # Strip whitespace so name variants ("NAME" vs "NAME ") do not break
    # the lookup against attendance First Name.
    schedules["Name"] = schedules["Name"].astype(str).str.strip()
    schedules["Shift Start"] = schedules["Working Time"].apply(extract_shift_start)
    return schedules.set_index("Name")["Shift Start"].to_dict()


def _build_daily_attendance(df, shift_lookup):
    """Aggregate punches into one row per (employee, day) with delay info."""
    check_ins = df[df["Punch State"] == "Check In"].copy()
    check_ins["First Name"] = check_ins["First Name"].astype(str).str.strip()

    # First check-in per employee per day. Punch Time is a zero-padded
    # HH:MM:SS string, so a lexical min is also the chronological earliest.
    daily = (
        check_ins.groupby(["Employee ID", "First Name", "Date"])["Punch Time"]
        .min()
        .reset_index()
        .rename(columns={"Punch Time": "Check In"})
    )

    daily["Shift Start"] = daily["First Name"].map(shift_lookup)
    daily["missing_schedule"] = daily["Shift Start"].isna()

    # Employees not listed in the schedules file have no Shift Start; we
    # cannot compute lateness for them, so leave their delay at 0.
    daily["Delay Minutes"] = daily.apply(
        lambda row: _delay_minutes(row["Check In"], row["Shift Start"])
        if pd.notna(row["Shift Start"])
        else 0,
        axis=1,
    )

    daily["Check DateTime"] = pd.to_datetime(
        daily["Date"].astype(str) + " " + daily["Check In"].astype(str)
    )
    return daily


def _build_approved_leaves(time_off_df):
    """Return dict: cleaned employee name -> list of (start, end, type).

    Hoisting the filter, strip, and date parsing out of the per-row apply
    turns an O(D x L) cost into O(L) plus O(D) lookups.
    """
    if time_off_df is None or time_off_df.empty:
        return {}

    leaves = time_off_df[time_off_df["Status"] == "Approved"].copy()
    leaves["Employee"] = leaves["Employee"].astype(str).str.strip()
    leaves["Start Date"] = pd.to_datetime(leaves["Start Date"])
    leaves["End Date"] = pd.to_datetime(leaves["End Date"])

    by_employee = {}
    for name, rows in leaves.groupby("Employee"):
        by_employee[name] = list(
            rows[["Start Date", "End Date", "Time Off Type"]].itertuples(
                index=False, name=None
            )
        )
    return by_employee


def _lookup_time_off(employee_name, check_dt, approved_leaves):
    """Return (has_time_off, time_off_type) for one (employee, check_dt)."""
    name = str(employee_name).strip()
    for start, end, type_ in approved_leaves.get(name, ()):
        if start <= check_dt <= end:
            return True, type_
    return False, None


def _attach_time_off(daily, time_off_df):
    approved_leaves = _build_approved_leaves(time_off_df)
    daily[["has_time_off", "time_off_type"]] = daily.apply(
        lambda row: pd.Series(
            _lookup_time_off(
                row["First Name"], row["Check DateTime"], approved_leaves
            )
        ),
        axis=1,
    )
    return daily


def _build_employee_summary(daily):
    late_rows = daily[daily["is_late"]]
    employee_summary = (
        late_rows.groupby("Employee ID")
        .agg(
            late_count=("is_late", "sum"),
            total_late_minutes=("Delay Minutes", "sum"),
            avg_late_minutes=("Delay Minutes", "mean"),
        )
        .reset_index()
        .sort_values(by="total_late_minutes", ascending=False)
    )
    employee_summary["risk_level"] = employee_summary["total_late_minutes"].apply(
        classify_risk
    )
    return employee_summary


def calculate_metrics(df, schedules_df, time_off_df=None):
    shift_lookup = _build_shift_lookup(schedules_df)
    daily = _build_daily_attendance(df, shift_lookup)
    daily = _attach_time_off(daily, time_off_df)

    # Grace period is a threshold: once exceeded, the full delay counts as
    # late minutes per Rule 6's MAX(0, Check-in - Shift Start). Approved
    # leave at the check-in moment overrides the late flag.
    daily["is_late"] = (daily["Delay Minutes"] > GRACE_MINUTES) & (~daily["has_time_off"])

    employee_summary = _build_employee_summary(daily)
    summary = {
        "total_employees": df["Employee ID"].nunique(),
        "late_cases": int(daily["is_late"].sum()),
        "total_late_minutes": int(daily.loc[daily["is_late"], "Delay Minutes"].sum()),
        "employee_summary": employee_summary,
    }
    return summary, daily
