"""Compute attendance KPIs from BioTime punches and Odoo metadata.

Inputs
- df            : BioTime punches export. One row per punch event.
                  Required columns: Employee ID, First Name, Date,
                  Punch Time, Punch State. Optional: Department
                  (or Department Name / Work Location / Company).
- schedules_df  : Odoo resource.resource export. One row per employee.
                  Required columns: Name, Working Time.
- time_off_df   : Optional Odoo hr.leave export. One row per leave
                  request. Required columns: Employee, Status,
                  Start Date, End Date, Time Off Type. Only rows with
                  Status == "Approved" are honored.

Output of calculate_metrics
- summary : dict of KPI scalars (incl. payroll totals and the explicit
            employee-count taxonomy described below) plus these
            DataFrames (each may be None / empty when source data is
            unavailable):
              employee_summary, status_summary, excused_vs_unexcused,
              department_summary, missing_punch_summary, daily_trend,
              employee_reconciliation, employee_reconciliation_details.
- daily   : DataFrame, one row per (Employee ID, Date). See schema below.

Employee-count taxonomy (auditable, never the bare `Employee ID.nunique`)
- attendance_file_employees : every unique Employee ID anywhere in the
                              BioTime export (incl. check-out / break
                              punches and inactive IDs).
- employees_with_checkins   : unique Employee IDs that recorded at
                              least one Check In during the period.
- scheduled_employees       : unique Name rows in the Odoo
                              resource.resource export.
- employees_missing_schedule: employees with a Check In whose name has
                              no match in the Odoo resources export.
- reporting_population      : the count we publish for this report
                              (= employees_with_checkins for now).
`total_employees` is kept as a backward-compat alias of
reporting_population.

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
  is_late (bool: attendance_status == "Late", kept for back compat),
  Check Out (HH:MM:SS or NaN  -- latest Check Out punch that day),
  has_check_out (bool), missing_check_out (bool),
  Department (str or NaN  -- only when source data exposed one).

Employee Summary schema (per-employee, sorted by risk_score desc)
  Employee ID, First Name, total_late_minutes, late_count,
  avg_late_minutes, missing_checkout_count, excuse_count, risk_score,
  risk_level, risk_reason, estimated_deduction, deduction_capped.

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

Risk scoring (compound, replaces the old minutes-only band)
  risk_score = min(late_count*2, 40)
             + min(total_late_minutes // 60, 20)   # capped hours bucket
             + min(missing_checkout_count*2, 20)
             + min(excuse_count, 5)
  level is RISK_HIGH_THRESHOLD / RISK_MEDIUM_THRESHOLD bands of the
  score. risk_reason is a short, human-readable text composed from the
  contributing factors.
"""
import re
from datetime import datetime

import pandas as pd

from config import (
    GRACE_MINUTES,
    LATE_MINUTE_COST,
    MAX_MONTHLY_DEDUCTION,
    RISK_HIGH_THRESHOLD,
    RISK_MEDIUM_THRESHOLD,
)


# Time Off Type values containing any of these substrings (case-insensitive)
# are treated as approved EXCUSES (partial hourly permission). Every other
# approved time-off row is treated as LEAVE (full-day, supersedes attendance).
_EXCUSE_KEYWORDS = ("استأذان", "استئذان", "excuse", "permission")

_SHIFT_TIME_RE = re.compile(r"\((\d{1,2}:\d{2}\s*[AP]M)", re.IGNORECASE)

# Source columns that we accept as the employee's department / org unit.
_DEPARTMENT_COL_CANDIDATES = (
    "Department", "Department Name", "Work Location", "Company",
)


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


def _delay_minutes(punch_time, shift_start):
    check_in = datetime.strptime(punch_time, "%H:%M:%S")
    shift_start_time = datetime.strptime(shift_start, "%H:%M")
    return int((check_in - shift_start_time).total_seconds() / 60)


def _find_column(df, candidates):
    for col in candidates:
        if col in df.columns:
            return col
    return None


def _compute_risk(late_count, total_late_minutes, missing_checkout_count, excuse_count):
    """Return (risk_score, risk_level, risk_reason).

    See the module docstring for the scoring weights and thresholds.
    """
    score = int(
        min(late_count * 2, 40)
        + min(total_late_minutes // 60, 20)
        + min(missing_checkout_count * 2, 20)
        + min(excuse_count, 5)
    )

    if score >= RISK_HIGH_THRESHOLD:
        level = "High Risk"
    elif score >= RISK_MEDIUM_THRESHOLD:
        level = "Medium Risk"
    else:
        level = "Low Risk"

    parts = []
    if late_count:
        parts.append(f"{late_count} late days")
    if total_late_minutes:
        parts.append(f"{total_late_minutes} unexcused min")
    if missing_checkout_count:
        parts.append(f"{missing_checkout_count} missing check-outs")
    if excuse_count:
        parts.append(f"{excuse_count} excused day(s)")
    reason = "; ".join(parts) if parts else "no flags"

    return score, level, reason


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
    """Return dict: cleaned employee name -> list of (start, end, type, is_excuse)."""
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


def _attach_checkout_info(daily, df):
    """Merge each day's latest Check Out punch into daily. Adds Check Out,
    has_check_out, missing_check_out columns."""
    checkouts = df[df["Punch State"] == "Check Out"]
    if checkouts.empty:
        daily["Check Out"] = pd.NA
    else:
        per_day_checkout = (
            checkouts.groupby(["Employee ID", "Date"])["Punch Time"]
            .max()
            .reset_index()
            .rename(columns={"Punch Time": "Check Out"})
        )
        daily = daily.merge(per_day_checkout, on=["Employee ID", "Date"], how="left")
    daily["has_check_out"] = daily["Check Out"].notna()
    daily["missing_check_out"] = ~daily["has_check_out"]
    return daily


def _attach_department(daily, df):
    """Map a Department column onto daily when source data exposes one."""
    dept_col = _find_column(df, _DEPARTMENT_COL_CANDIDATES)
    if dept_col is None:
        daily["Department"] = pd.NA
        return daily
    emp_to_dept = (
        df[["Employee ID", dept_col]]
        .dropna()
        .drop_duplicates("Employee ID")
        .set_index("Employee ID")[dept_col]
        .to_dict()
    )
    daily["Department"] = daily["Employee ID"].map(emp_to_dept)
    return daily


def _build_employee_summary(daily):
    """Per-employee risk + payroll summary.

    Covers every employee with at least one late day, missing check-out,
    or approved excuse. Each row carries: aggregate counters, the compound
    risk_score / risk_level / risk_reason, and estimated_deduction /
    deduction_capped using the configured payroll rate.
    """
    per_emp = (
        daily.groupby(["Employee ID", "First Name"])
        .agg(
            late_count=("is_late", "sum"),
            total_late_minutes=("unexcused_delay_minutes", "sum"),
            missing_checkout_count=("missing_check_out", "sum"),
            excuse_count=(
                "attendance_status",
                lambda s: int((s == "Approved Excuse").sum()),
            ),
        )
        .reset_index()
    )
    per_emp = per_emp[
        (per_emp["late_count"] > 0)
        | (per_emp["missing_checkout_count"] > 0)
        | (per_emp["excuse_count"] > 0)
    ].copy()

    if per_emp.empty:
        return per_emp

    per_emp["total_late_minutes"] = per_emp["total_late_minutes"].astype(int)
    per_emp["late_count"] = per_emp["late_count"].astype(int)
    per_emp["missing_checkout_count"] = per_emp["missing_checkout_count"].astype(int)
    per_emp["excuse_count"] = per_emp["excuse_count"].astype(int)
    per_emp["avg_late_minutes"] = per_emp.apply(
        lambda r: int(r["total_late_minutes"] / r["late_count"])
        if r["late_count"] else 0,
        axis=1,
    )

    risk_df = per_emp.apply(
        lambda r: pd.Series(
            _compute_risk(
                int(r["late_count"]),
                int(r["total_late_minutes"]),
                int(r["missing_checkout_count"]),
                int(r["excuse_count"]),
            ),
            index=["risk_score", "risk_level", "risk_reason"],
        ),
        axis=1,
    )
    per_emp["risk_score"] = risk_df["risk_score"].astype(int)
    per_emp["risk_level"] = risk_df["risk_level"]
    per_emp["risk_reason"] = risk_df["risk_reason"]

    per_emp["estimated_deduction"] = (
        per_emp["total_late_minutes"] * LATE_MINUTE_COST
    ).round(2)
    per_emp["deduction_capped"] = (
        per_emp["estimated_deduction"].clip(upper=MAX_MONTHLY_DEDUCTION).round(2)
    )

    column_order = [
        "Employee ID", "First Name",
        "total_late_minutes", "late_count", "avg_late_minutes",
        "missing_checkout_count", "excuse_count",
        "risk_score", "risk_level", "risk_reason",
        "estimated_deduction", "deduction_capped",
    ]
    return (
        per_emp[column_order]
        .sort_values(by=["risk_score", "total_late_minutes"], ascending=False)
        .reset_index(drop=True)
    )


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


def _aggregate_status_counts(daily, group_col):
    """Group daily by group_col and return per-status counts + unexcused sums."""
    work = daily[[group_col, "attendance_status", "unexcused_delay_minutes"]].copy()
    work["total_records"] = 1
    for status, col in (
        ("Late", "late_cases"),
        ("Approved Excuse", "approved_excuse_cases"),
        ("Leave", "leave_cases"),
        ("Missing Schedule", "missing_schedule_cases"),
    ):
        work[col] = (work["attendance_status"] == status).astype(int)
    return (
        work.groupby(group_col, dropna=True)
        .agg(
            total_records=("total_records", "sum"),
            late_cases=("late_cases", "sum"),
            approved_excuse_cases=("approved_excuse_cases", "sum"),
            leave_cases=("leave_cases", "sum"),
            missing_schedule_cases=("missing_schedule_cases", "sum"),
            total_unexcused_delay_minutes=("unexcused_delay_minutes", "sum"),
        )
        .reset_index()
    )


def _build_department_summary(daily):
    if "Department" not in daily.columns or daily["Department"].isna().all():
        return None
    grp = _aggregate_status_counts(daily, "Department")
    grp["total_unexcused_delay_minutes"] = grp["total_unexcused_delay_minutes"].astype(int)
    return grp.sort_values("late_cases", ascending=False).reset_index(drop=True)


def _build_missing_punch_summary(daily):
    rows = daily[daily["missing_check_out"]]
    cols = ["Employee ID", "First Name", "Date", "Check In", "Shift Start"]
    if rows.empty:
        return pd.DataFrame(columns=cols)
    return rows[cols].copy().reset_index(drop=True)


def _build_daily_trend(daily):
    grp = _aggregate_status_counts(daily, "Date")
    grp["total_unexcused_delay_minutes"] = grp["total_unexcused_delay_minutes"].astype(int)
    return grp.sort_values("Date").reset_index(drop=True)


def _build_employee_reconciliation(df, schedules_df, daily):
    """Return (reconciliation_table, counts_dict).

    The table is the human-readable explainer; the dict carries the raw
    integers so they can flow straight into the summary KPIs.
    """
    counts = {
        "attendance_file_employees": int(df["Employee ID"].nunique()),
        "employees_with_checkins": int(daily["Employee ID"].nunique()),
        "scheduled_employees": int(schedules_df["Name"].dropna().nunique()),
        "employees_missing_schedule": int(
            daily.loc[daily["missing_schedule"], "Employee ID"].nunique()
        ),
    }
    counts["reporting_population"] = counts["employees_with_checkins"]

    rows = [
        (
            "Attendance File Employees",
            counts["attendance_file_employees"],
            "attendance_raw.xlsx",
            "Unique Employee ID values anywhere in the BioTime export, "
            "including check-out and break punches. May still include "
            "inactive or decommissioned IDs.",
        ),
        (
            "Employees With Check-ins",
            counts["employees_with_checkins"],
            "attendance_raw.xlsx (Check In rows)",
            "Unique Employee IDs that recorded at least one Check In "
            "during the export period.",
        ),
        (
            "Scheduled Employees From Odoo Resources",
            counts["scheduled_employees"],
            "Resources (resource.resource).xlsx",
            "Unique Name values in the Odoo resource export -- employees "
            "with an active Working Time assignment.",
        ),
        (
            "Employees Missing Schedule",
            counts["employees_missing_schedule"],
            "derived",
            "Employees with at least one Check In whose First Name does "
            "not match any Name in the Odoo resources export.",
        ),
        (
            "Reporting Population",
            counts["reporting_population"],
            "derived",
            "The employee count used for this monthly report. Equals "
            "Employees With Check-ins for the export period.",
        ),
    ]
    table = pd.DataFrame(rows, columns=["metric", "count", "source", "definition"])
    return table, counts


def _build_employee_reconciliation_details(df, schedules_df, daily):
    """Per-employee reconciliation rows covering every ID in the attendance
    file: Employee ID, First Name, has_schedule, has_checkin, attendance_status_count.

    Employees in the attendance file with no Check In get
    attendance_status_count = 0 so they remain visible for audit.
    """
    all_emp = (
        df.dropna(subset=["Employee ID"])
        .groupby("Employee ID")["First Name"]
        .first()
        .reset_index()
    )
    all_emp["First Name"] = all_emp["First Name"].astype(str).str.strip()

    schedule_names = set(
        schedules_df["Name"].dropna().astype(str).str.strip()
    )
    all_emp["has_schedule"] = all_emp["First Name"].isin(schedule_names)

    checkin_ids = set(daily["Employee ID"].unique())
    all_emp["has_checkin"] = all_emp["Employee ID"].isin(checkin_ids)

    status_counts = daily.groupby("Employee ID").size().to_dict()
    all_emp["attendance_status_count"] = (
        all_emp["Employee ID"].map(status_counts).fillna(0).astype(int)
    )

    return all_emp.sort_values("Employee ID").reset_index(drop=True)


def calculate_metrics(df, schedules_df, time_off_df=None):
    shift_lookup = _build_shift_lookup(schedules_df)
    daily = _build_daily_attendance(df, shift_lookup)
    daily = _attach_attendance_status(daily, time_off_df)
    daily = _attach_checkout_info(daily, df)
    daily = _attach_department(daily, df)

    late_rows = daily[daily["attendance_status"] == "Late"]
    status_summary = _build_status_summary(daily)
    excused_vs_unexcused = _build_excused_vs_unexcused(daily)
    employee_summary = _build_employee_summary(daily)
    department_summary = _build_department_summary(daily)
    missing_punch_summary = _build_missing_punch_summary(daily)
    daily_trend = _build_daily_trend(daily)
    reconciliation_table, employee_counts = _build_employee_reconciliation(
        df, schedules_df, daily
    )
    reconciliation_details = _build_employee_reconciliation_details(
        df, schedules_df, daily
    )

    if employee_summary is not None and not employee_summary.empty:
        total_est = float(employee_summary["estimated_deduction"].sum())
        total_capped = float(employee_summary["deduction_capped"].sum())
        high_risk_employees = int((employee_summary["risk_level"] == "High Risk").sum())
    else:
        total_est = 0.0
        total_capped = 0.0
        high_risk_employees = 0

    summary = {
        # Auditable employee-count taxonomy. See module docstring.
        "attendance_file_employees": employee_counts["attendance_file_employees"],
        "employees_with_checkins": employee_counts["employees_with_checkins"],
        "scheduled_employees": employee_counts["scheduled_employees"],
        "employees_missing_schedule": employee_counts["employees_missing_schedule"],
        "reporting_population": employee_counts["reporting_population"],
        # Backward-compat alias. Will be removed once all consumers migrate.
        "total_employees": employee_counts["reporting_population"],
        "late_cases": int(len(late_rows)),
        "total_late_minutes": int(late_rows["unexcused_delay_minutes"].sum()),
        "approved_excuse_cases": int((daily["attendance_status"] == "Approved Excuse").sum()),
        "leave_cases": int((daily["attendance_status"] == "Leave").sum()),
        "missing_schedule_cases": int((daily["attendance_status"] == "Missing Schedule").sum()),
        "missing_check_out_cases": int(daily["missing_check_out"].sum()),
        "excused_delay_minutes": int(daily["excused_delay_minutes"].sum()),
        "total_estimated_deduction": round(total_est, 2),
        "total_deduction_capped": round(total_capped, 2),
        "high_risk_employees": high_risk_employees,
        "employee_summary": employee_summary,
        "status_summary": status_summary,
        "excused_vs_unexcused": excused_vs_unexcused,
        "department_summary": department_summary,
        "missing_punch_summary": missing_punch_summary,
        "daily_trend": daily_trend,
        "employee_reconciliation": reconciliation_table,
        "employee_reconciliation_details": reconciliation_details,
    }
    return summary, daily
