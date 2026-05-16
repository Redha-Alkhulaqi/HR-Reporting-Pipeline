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
- summary : dict of KPI scalars plus three DataFrames:
            employee_summary, status_summary, excused_vs_unexcused.
- daily   : DataFrame, one row per (Employee ID, Date). See schema below.

Daily DataFrame schema
  Employee ID, First Name, Date, Check In (HH:MM:SS),
  Shift Start (HH:MM or NaN), missing_schedule (bool),
  Delay Minutes (raw int; negative when early; 0 when missing_schedule),
  Check DateTime (Timestamp),
  excused_delay_minutes (int, >= 0),
  unexcused_delay_minutes (int, >= 0),
  time_off_type (str or None  -- the type that classified the row),
  attendance_status (str: Late | On Time | Approved Excuse | Leave |
                     Missing Schedule),
  is_late (bool: attendance_status == "Late", kept for back compat).

Status classification (HR_REPORTING_RULES_MASTER rules 3, 5, 6)
- Missing Schedule : employee absent from the resources export.
- Leave            : any approved LEAVE row (Annual / Sick / etc.) whose
                     window covers the check-in moment. Leave wins over
                     excuse per the priority rule.
- Approved Excuse  : approved EXCUSE rows (partial hourly permissions
                     such as استأذان) reduce the delay by their overlap
                     with the (Shift Start -> Check In) window. If the
                     residual unexcused delay is within GRACE_MINUTES,
                     the day is Approved Excuse.
- Late             : unexcused_delay_minutes > GRACE_MINUTES.
- On Time          : everything else.

Only Late rows contribute to late_cases / total_late_minutes.
"""
import re
from datetime import datetime

import pandas as pd


GRACE_MINUTES = 15                # Rule 6 grace period after shift start.
RISK_HIGH_THRESHOLD = 1000        # Total late minutes that flag High Risk.
RISK_MEDIUM_THRESHOLD = 500       # Total late minutes that flag Medium Risk.

# Time Off Type values containing any of these substrings (case-insensitive)
# are treated as approved EXCUSES (partial hourly permission). Every other
# approved time-off row is treated as LEAVE (full-day, supersedes attendance).
_EXCUSE_KEYWORDS = ("استأذان", "استئذان", "excuse", "permission")

_SHIFT_TIME_RE = re.compile(r"\((\d{1,2}:\d{2}\s*[AP]M)", re.IGNORECASE)


def extract_shift_start(working_time):
    """Pull the first HH:MMAM/PM token from an Odoo Working Time label."""
    match = _SHIFT_TIME_RE.search(str(working_time))
    if not match:
        return None
    time_text = match.group(1).replace(" ", "").upper()
    return datetime.strptime(time_text, "%I:%M%p").strftime("%H:%M")


def _is_excuse_type(type_name):
    if type_name is None:
        return False
    text = str(type_name).lower()
    return any(kw.lower() in text for kw in _EXCUSE_KEYWORDS)


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
    schedules["Name"] = schedules["Name"].astype(str).str.strip()
    schedules["Shift Start"] = schedules["Working Time"].apply(extract_shift_start)
    return schedules.set_index("Name")["Shift Start"].to_dict()


def _build_daily_attendance(df, shift_lookup):
    """Aggregate punches into one row per (employee, day) with raw delay info."""
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
    """Return dict: cleaned employee name -> list of (start, end, type, is_excuse).

    Hoisting the filter, strip, and date parsing out of the per-row apply
    turns an O(D x L) cost into O(L) prep plus O(D) lookups.
    """
    if time_off_df is None or time_off_df.empty:
        return {}

    leaves = time_off_df[time_off_df["Status"] == "Approved"].copy()
    leaves["Employee"] = leaves["Employee"].astype(str).str.strip()
    leaves["Start Date"] = pd.to_datetime(leaves["Start Date"], errors="coerce")
    leaves["End Date"] = pd.to_datetime(leaves["End Date"], errors="coerce")
    leaves = leaves.dropna(subset=["Start Date", "End Date"])
    leaves["is_excuse"] = leaves["Time Off Type"].apply(_is_excuse_type)

    by_employee = {}
    for name, rows in leaves.groupby("Employee"):
        by_employee[name] = list(
            rows[["Start Date", "End Date", "Time Off Type", "is_excuse"]].itertuples(
                index=False, name=None
            )
        )
    return by_employee


def _classify_row(row, approved_leaves):
    """Return (status, excused_minutes, unexcused_minutes, time_off_type)."""
    if row["missing_schedule"]:
        return "Missing Schedule", 0, 0, None

    # Negative delay (early arrival) cannot be excused or unexcused; clamp.
    total_delay = max(0, int(row["Delay Minutes"]))

    name = str(row["First Name"]).strip()
    leaves_for_employee = approved_leaves.get(name, ())

    if not leaves_for_employee:
        if total_delay > GRACE_MINUTES:
            return "Late", 0, total_delay, None
        return "On Time", 0, 0, None

    check_in_dt = row["Check DateTime"]

    # Leave wins over excuse (rule 3 priority).
    for start, end, type_, is_excuse in leaves_for_employee:
        if (not is_excuse) and start <= check_in_dt <= end:
            return "Leave", 0, 0, type_

    shift_start_dt = pd.to_datetime(f"{row['Date']} {row['Shift Start']}:00")

    excused = 0
    excuse_type = None
    for start, end, type_, is_excuse in leaves_for_employee:
        if not is_excuse:
            continue
        overlap_start = max(shift_start_dt, start)
        overlap_end = min(check_in_dt, end)
        if overlap_end > overlap_start:
            excused += int((overlap_end - overlap_start).total_seconds() / 60)
            excuse_type = type_

    excused = min(excused, total_delay)
    unexcused = total_delay - excused

    if unexcused > GRACE_MINUTES:
        return "Late", excused, unexcused, excuse_type
    if excused > 0:
        return "Approved Excuse", excused, unexcused, excuse_type
    return "On Time", 0, 0, None


def _attach_attendance_status(daily, time_off_df):
    approved_leaves = _build_approved_leaves(time_off_df)
    columns = ["attendance_status", "excused_delay_minutes",
               "unexcused_delay_minutes", "time_off_type"]
    result = daily.apply(
        lambda row: pd.Series(_classify_row(row, approved_leaves), index=columns),
        axis=1,
    )
    daily["attendance_status"] = result["attendance_status"]
    daily["excused_delay_minutes"] = result["excused_delay_minutes"].astype(int)
    daily["unexcused_delay_minutes"] = result["unexcused_delay_minutes"].astype(int)
    daily["time_off_type"] = result["time_off_type"]
    daily["is_late"] = daily["attendance_status"] == "Late"
    return daily


def _build_employee_summary(daily):
    late_rows = daily[daily["attendance_status"] == "Late"]
    employee_summary = (
        late_rows.groupby("Employee ID")
        .agg(
            late_count=("is_late", "sum"),
            total_late_minutes=("unexcused_delay_minutes", "sum"),
            avg_late_minutes=("unexcused_delay_minutes", "mean"),
        )
        .reset_index()
        .sort_values(by="total_late_minutes", ascending=False)
    )
    employee_summary["risk_level"] = employee_summary["total_late_minutes"].apply(
        classify_risk
    )
    return employee_summary


def _build_status_summary(daily):
    grp = (
        daily.groupby("attendance_status")
        .agg(
            count=("attendance_status", "size"),
            unexcused_delay_minutes=("unexcused_delay_minutes", "sum"),
            excused_delay_minutes=("excused_delay_minutes", "sum"),
        )
        .reset_index()
        .rename(columns={"attendance_status": "Status"})
        .sort_values("count", ascending=False)
    )
    grp["unexcused_delay_minutes"] = grp["unexcused_delay_minutes"].astype(int)
    grp["excused_delay_minutes"] = grp["excused_delay_minutes"].astype(int)
    return grp.reset_index(drop=True)


def _build_excused_vs_unexcused(daily):
    excused = int(daily["excused_delay_minutes"].sum())
    unexcused = int(daily["unexcused_delay_minutes"].sum())
    return pd.DataFrame(
        {
            "Metric": [
                "Excused Delay Minutes",
                "Unexcused Delay Minutes",
                "Total Delay Minutes",
            ],
            "Minutes": [excused, unexcused, excused + unexcused],
        }
    )


def calculate_metrics(df, schedules_df, time_off_df=None):
    shift_lookup = _build_shift_lookup(schedules_df)
    daily = _build_daily_attendance(df, shift_lookup)
    daily = _attach_attendance_status(daily, time_off_df)

    late_rows = daily[daily["attendance_status"] == "Late"]
    status_summary = _build_status_summary(daily)
    excused_vs_unexcused = _build_excused_vs_unexcused(daily)
    employee_summary = _build_employee_summary(daily)

    summary = {
        "total_employees": int(df["Employee ID"].nunique()),
        "late_cases": int(len(late_rows)),
        "total_late_minutes": int(late_rows["unexcused_delay_minutes"].sum()),
        "approved_excuse_cases": int(
            (daily["attendance_status"] == "Approved Excuse").sum()
        ),
        "leave_cases": int((daily["attendance_status"] == "Leave").sum()),
        "missing_schedule_cases": int(
            (daily["attendance_status"] == "Missing Schedule").sum()
        ),
        "excused_delay_minutes": int(daily["excused_delay_minutes"].sum()),
        "employee_summary": employee_summary,
        "status_summary": status_summary,
        "excused_vs_unexcused": excused_vs_unexcused,
    }
    return summary, daily
