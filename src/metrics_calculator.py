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
  Shift End (HH:MM or NaN),
  Shift End DateTime (Timestamp; rolls to next day for night shifts),
  Check Out DateTime (Timestamp; rolls to next day if before Check In),
  worked_minutes, scheduled_minutes (ints),
  overtime_minutes (int, >= 0),
  overtime_status (str: Overtime | No Overtime | Missing Check Out |
                   Missing Schedule),
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
    ALLOW_NAME_BASED_EXCLUSION_MATCH,
    EARLY_LEAVE_GRACE_MINUTES,
    GRACE_MINUTES,
    LATE_MINUTE_COST,
    MAX_MONTHLY_DEDUCTION,
    MIN_OVERTIME_MINUTES,
    OVERTIME_GRACE_MINUTES,
    RISK_HIGH_THRESHOLD,
    RISK_MEDIUM_THRESHOLD,
)


# Time Off Type values containing any of these substrings (case-insensitive)
# are treated as approved EXCUSES (partial hourly permission). Every other
# approved time-off row is treated as LEAVE (full-day, supersedes attendance).
_EXCUSE_KEYWORDS = ("استأذان", "استئذان", "excuse", "permission")

# Source columns that we accept as the employee's department / org unit.
_DEPARTMENT_COL_CANDIDATES = (
    "Department", "Department Name", "Work Location", "Company",
)


# Pair-matcher used by extract_shift_intervals to find every
# "HH:MM AM - HH:MM PM" pair inside an Odoo Working Time label.
_SHIFT_INTERVAL_RE = re.compile(
    r"(\d{1,2}:\d{2}\s*[AP]M)\s*-\s*(\d{1,2}:\d{2}\s*[AP]M)",
    re.IGNORECASE,
)


def extract_shift_intervals(working_time):
    """Return all shift intervals parsed from a Working Time label.

    Each interval is a `(start_HHMM, end_HHMM)` tuple. Examples:

      "(8:00AM-5:00PM)"
        -> [("08:00", "17:00")]

      "شفت صباحى (9:00AM-1:00PM) & شفت مسائى (6:00PM-10:00PM)"
        -> [("09:00", "13:00"), ("18:00", "22:00")]

    Returns an empty list when nothing parseable is found.
    """
    intervals = []
    for start_raw, end_raw in _SHIFT_INTERVAL_RE.findall(str(working_time)):
        try:
            start = datetime.strptime(
                start_raw.replace(" ", "").upper(), "%I:%M%p"
            ).strftime("%H:%M")
            end = datetime.strptime(
                end_raw.replace(" ", "").upper(), "%I:%M%p"
            ).strftime("%H:%M")
        except ValueError:
            continue
        intervals.append((start, end))
    return intervals


def extract_shift_start(working_time):
    """Return the first interval's start time (HH:MM) or None."""
    intervals = extract_shift_intervals(working_time)
    return intervals[0][0] if intervals else None


def extract_shift_end(working_time):
    """Return the LAST interval's end time (HH:MM) or None.

    For split-shift labels this is the close of the day, not the close
    of the first segment.
    """
    intervals = extract_shift_intervals(working_time)
    return intervals[-1][1] if intervals else None


def _is_excuse_type(type_name):
    if type_name is None:
        return False
    text = str(type_name).lower()
    return any(kw.lower() in text for kw in _EXCUSE_KEYWORDS)


_TRUTHY_STRINGS = {"true", "yes", "y", "1", "نعم"}


def _parse_bool(value):
    """Tolerant bool parser: handles TRUE/FALSE, Yes/No, 1/0, plus blanks."""
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not pd.isna(value):
        return bool(value)
    if pd.isna(value):
        return False
    return str(value).strip().lower() in _TRUTHY_STRINGS


def _normalize_name(name):
    """Lower-cased, whitespace-collapsed name key for fallback matching."""
    if name is None or pd.isna(name):
        return ""
    return " ".join(str(name).split()).lower()


def _build_exclusion_rules(excluded_df):
    """Parse the exclusion DataFrame into a list of rule dicts.

    Each rule carries the resolved Employee ID (or None), the normalized
    name for fallback matching, the human-readable reason / notes, and
    the four boolean exclusion flags.
    """
    if excluded_df is None or excluded_df.empty:
        return []

    rules = []
    for _, row in excluded_df.iterrows():
        eid = None
        raw_id = row.get("Employee ID")
        if pd.notna(raw_id):
            try:
                eid = int(raw_id)
            except (ValueError, TypeError):
                eid = None
        raw_name = row.get("Employee Name")
        rules.append({
            "id": eid,
            "original_name": None if pd.isna(raw_name) else str(raw_name),
            "normalized_name": _normalize_name(raw_name),
            "reason": "" if pd.isna(row.get("Exclusion Reason")) else str(row.get("Exclusion Reason")),
            "notes": "" if pd.isna(row.get("Notes")) else str(row.get("Notes")),
            "flags": {
                "excluded_from_late": _parse_bool(row.get("Exclude From Late")),
                "excluded_from_overtime": _parse_bool(row.get("Exclude From Overtime")),
                "excluded_from_payroll": _parse_bool(row.get("Exclude From Payroll Deduction")),
                "excluded_from_risk": _parse_bool(row.get("Exclude From Risk Scoring")),
            },
        })
    return rules


def _match_exclusion(employee_id, first_name, rules, allow_name_match):
    """Return the matching rule (or None). ID match takes priority.

    Rules that carry an Employee ID apply ONLY by ID -- the Name on
    those rules is informational. Name matching is a fallback used
    only by rules with no Employee ID.
    """
    for rule in rules:
        if rule["id"] is not None and rule["id"] == employee_id:
            return rule
    if allow_name_match:
        target = _normalize_name(first_name)
        if target:
            for rule in rules:
                if rule["id"] is not None:
                    continue
                if rule["normalized_name"] and rule["normalized_name"] == target:
                    return rule
    return None


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


def _build_shift_intervals_lookup(schedules_df):
    """Return dict: cleaned employee name -> list of (start_HHMM, end_HHMM)."""
    schedules = schedules_df[["Name", "Working Time"]].copy()
    schedules["Name"] = schedules["Name"].astype(str).str.strip()
    schedules["intervals"] = schedules["Working Time"].apply(extract_shift_intervals)
    return schedules.set_index("Name")["intervals"].to_dict()




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


_OVERTIME_RESULT_COLS = [
    "Shift End DateTime", "Check Out DateTime",
    "worked_minutes", "scheduled_minutes", "matched_scheduled_minutes",
    "matched_shift_start", "matched_shift_end", "matched_shift_label",
    "shift_intervals",
    "overtime_minutes", "overtime_status",
    "early_leave_minutes", "early_leave_status",
]


def _empty_overtime_result(status):
    """Default values for rows that cannot be classified (missing data)."""
    return {
        "Shift End DateTime": pd.NaT,
        "Check Out DateTime": pd.NaT,
        "worked_minutes": 0,
        "scheduled_minutes": 0,
        "matched_scheduled_minutes": 0,
        "matched_shift_start": None,
        "matched_shift_end": None,
        "matched_shift_label": None,
        "shift_intervals": None,
        "overtime_minutes": 0,
        "overtime_status": status,
        "early_leave_minutes": 0,
        "early_leave_status": status,
    }


def _intervals_to_datetimes(date_str, intervals):
    """Convert string intervals to (start_dt, end_dt) tuples for one date.

    Handles night-shift wrap (end-before-start in clock time rolls to
    next day) and multi-segment chronology (next interval whose start
    precedes the previous interval's end rolls one day forward).
    """
    result = []
    prev_end = None
    for start_str, end_str in intervals:
        start_dt = pd.to_datetime(f"{date_str} {start_str}:00")
        end_dt = pd.to_datetime(f"{date_str} {end_str}:00")
        if end_dt < start_dt:
            end_dt += pd.Timedelta(days=1)
        if prev_end is not None and start_dt < prev_end:
            shift = pd.Timedelta(days=1)
            start_dt += shift
            end_dt += shift
        result.append((start_dt, end_dt))
        prev_end = end_dt
    return result


def _find_matched_interval_idx(check_in_dt, check_out_dt, interval_dts):
    """Pick the interval that matters for this employee-day.

    Priority:
      1. The interval that CONTAINS Check Out (end-of-day signal).
      2. If Check Out is past the last interval's end, the last interval
         (overtime extending past the final segment).
      3. The interval that contains Check In (employee started here).
      4. The interval whose start is closest to Check In (best guess).
    """
    for i, (start, end) in enumerate(interval_dts):
        if start <= check_out_dt <= end:
            return i
    if check_out_dt > interval_dts[-1][1]:
        return len(interval_dts) - 1
    for i, (start, end) in enumerate(interval_dts):
        if start <= check_in_dt <= end:
            return i
    return min(
        range(len(interval_dts)),
        key=lambda i: abs((interval_dts[i][0] - check_in_dt).total_seconds()),
    )


def _classify_overtime_row(row, intervals_lookup):
    """Per-row overtime AND early-leave computation against the MATCHED
    interval (the one the employee actually worked), not the day's final
    interval. This makes split-shift days reconcile correctly:

      - A morning-only employee on a 9am-1pm / 4pm-8pm schedule who
        leaves at 1pm is normal (matched = morning, delta = 0).
      - The same employee leaving at 2pm is in the GAP between segments
        and is also normal (no overtime, no early leave).
      - The same employee leaving at 8pm matches the evening interval.

    Night-shift wrap is honored (Shift End rolls to next day if it
    falls before Shift Start in clock time, same for Check Out vs
    Check In).
    """
    if not row["has_check_out"]:
        return _empty_overtime_result("Missing Check Out")
    if row["missing_schedule"]:
        return _empty_overtime_result("Missing Schedule")

    name = str(row["First Name"]).strip()
    intervals = intervals_lookup.get(name) or []
    if not intervals:
        return _empty_overtime_result("Missing Schedule")

    date_str = row["Date"]
    check_in_dt = pd.to_datetime(f"{date_str} {row['Check In']}")
    check_out_dt = pd.to_datetime(f"{date_str} {row['Check Out']}")
    if check_out_dt < check_in_dt:
        check_out_dt += pd.Timedelta(days=1)

    interval_dts = _intervals_to_datetimes(date_str, intervals)
    matched_idx = _find_matched_interval_idx(check_in_dt, check_out_dt, interval_dts)
    matched_start_dt, matched_end_dt = interval_dts[matched_idx]

    worked = int((check_out_dt - check_in_dt).total_seconds() / 60)
    scheduled = sum(
        int((end - start).total_seconds() / 60) for start, end in interval_dts
    )
    matched_scheduled = int(
        (matched_end_dt - matched_start_dt).total_seconds() / 60
    )

    delta = int((check_out_dt - matched_end_dt).total_seconds() / 60)
    in_gap = (
        delta > 0
        and matched_idx < len(interval_dts) - 1
        and check_out_dt < interval_dts[matched_idx + 1][0]
    )

    if in_gap or (delta >= 0 and delta <= OVERTIME_GRACE_MINUTES):
        overtime, ot_status = 0, "No Overtime"
        early_leave, el_status = 0, "Normal"
    elif delta > 0:
        if delta < MIN_OVERTIME_MINUTES:
            overtime, ot_status = 0, "No Overtime"
        else:
            overtime, ot_status = delta, "Overtime"
        early_leave, el_status = 0, "Normal"
    else:
        # delta < 0 -- Check Out before matched interval's end.
        overtime, ot_status = 0, "No Overtime"
        gap = -delta
        if gap > EARLY_LEAVE_GRACE_MINUTES:
            early_leave, el_status = gap, "Early Leave"
        else:
            early_leave, el_status = 0, "Normal"

    matched_start_str, matched_end_str = intervals[matched_idx]
    return {
        "Shift End DateTime": matched_end_dt,
        "Check Out DateTime": check_out_dt,
        "worked_minutes": worked,
        "scheduled_minutes": scheduled,
        "matched_scheduled_minutes": matched_scheduled,
        "matched_shift_start": matched_start_str,
        "matched_shift_end": matched_end_str,
        "matched_shift_label": f"{matched_start_str}-{matched_end_str}",
        "shift_intervals": " / ".join(f"{s}-{e}" for s, e in intervals),
        "overtime_minutes": overtime,
        "overtime_status": ot_status,
        "early_leave_minutes": early_leave,
        "early_leave_status": el_status,
    }


def _attach_overtime_info(daily, intervals_lookup):
    """Add per-segment shift-close fields to daily.

    `Shift End` is preserved as the LAST interval's end (a stable daily
    label). The per-row matched_shift_* / shift_intervals columns and
    the overtime / early-leave fields use the segment the employee
    actually worked.
    """
    shift_end_lookup = {
        name: (intervals[-1][1] if intervals else None)
        for name, intervals in intervals_lookup.items()
    }
    daily["Shift End"] = daily["First Name"].map(shift_end_lookup)

    rows = daily.apply(
        lambda r: pd.Series(
            _classify_overtime_row(r, intervals_lookup),
            index=_OVERTIME_RESULT_COLS,
        ),
        axis=1,
    )
    for col in _OVERTIME_RESULT_COLS:
        daily[col] = rows[col]
    daily["worked_minutes"] = daily["worked_minutes"].astype(int)
    daily["scheduled_minutes"] = daily["scheduled_minutes"].astype(int)
    daily["matched_scheduled_minutes"] = daily["matched_scheduled_minutes"].astype(int)
    daily["overtime_minutes"] = daily["overtime_minutes"].astype(int)
    daily["early_leave_minutes"] = daily["early_leave_minutes"].astype(int)
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


def _attach_exclusion_info(daily, excluded_df, allow_name_match=ALLOW_NAME_BASED_EXCLUSION_MATCH):
    """Stamp each daily row with the four exclusion flags + reason.

    Non-excluded rows get is_excluded=False and every flag False. Raw
    operational columns (attendance_status, overtime_minutes, etc.)
    are left untouched so HR can still see what the employee did; only
    downstream KPI aggregations honor the flags.
    """
    rules = _build_exclusion_rules(excluded_df)
    columns = [
        "is_excluded", "exclusion_reason",
        "excluded_from_late", "excluded_from_overtime",
        "excluded_from_payroll", "excluded_from_risk",
    ]

    if not rules:
        daily["is_excluded"] = False
        daily["exclusion_reason"] = ""
        for col in ("excluded_from_late", "excluded_from_overtime",
                    "excluded_from_payroll", "excluded_from_risk"):
            daily[col] = False
        return daily

    def _row(row):
        rule = _match_exclusion(
            row["Employee ID"], row["First Name"], rules, allow_name_match
        )
        if rule is None:
            return pd.Series({c: (False if c != "exclusion_reason" else "") for c in columns})
        flags = rule["flags"]
        return pd.Series({
            "is_excluded": any(flags.values()),
            "exclusion_reason": rule["reason"],
            "excluded_from_late": flags["excluded_from_late"],
            "excluded_from_overtime": flags["excluded_from_overtime"],
            "excluded_from_payroll": flags["excluded_from_payroll"],
            "excluded_from_risk": flags["excluded_from_risk"],
        })

    result = daily.apply(_row, axis=1)
    for col in columns:
        daily[col] = result[col]
    return daily


def _build_excluded_employees_summary(daily, excluded_df, allow_name_match=ALLOW_NAME_BASED_EXCLUSION_MATCH):
    """One row per entry in the exclusion file, joined with the
    operational record count from daily so HR sees the policy + reality."""
    rules = _build_exclusion_rules(excluded_df)
    if not rules:
        return pd.DataFrame()

    counts = daily.groupby("Employee ID").size().to_dict()
    name_to_id = {}
    if allow_name_match:
        name_to_id = (
            daily.dropna(subset=["First Name"])
            .assign(_norm=lambda d: d["First Name"].astype(str).apply(_normalize_name))
            .groupby("_norm")["Employee ID"]
            .first()
            .to_dict()
        )

    rows = []
    for rule in rules:
        eid = rule["id"]
        if (eid is None or eid not in counts) and allow_name_match:
            eid = name_to_id.get(rule["normalized_name"], eid)
        rows.append({
            "Employee ID": eid,
            "Employee Name": rule["original_name"],
            "Exclusion Reason": rule["reason"],
            "Excluded From Late": rule["flags"]["excluded_from_late"],
            "Excluded From Overtime": rule["flags"]["excluded_from_overtime"],
            "Excluded From Payroll": rule["flags"]["excluded_from_payroll"],
            "Excluded From Risk": rule["flags"]["excluded_from_risk"],
            "Operational Records": int(counts.get(eid, 0)),
            "Notes": rule["notes"],
        })
    return pd.DataFrame(rows)


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
            overtime_cases=(
                "overtime_status",
                lambda s: int((s == "Overtime").sum()),
            ),
            total_overtime_minutes=("overtime_minutes", "sum"),
            early_leave_cases=(
                "early_leave_status",
                lambda s: int((s == "Early Leave").sum()),
            ),
            total_early_leave_minutes=("early_leave_minutes", "sum"),
            is_excluded=("is_excluded", "any"),
            exclusion_reason=("exclusion_reason", "first"),
            excluded_from_late=("excluded_from_late", "any"),
            excluded_from_overtime=("excluded_from_overtime", "any"),
            excluded_from_payroll=("excluded_from_payroll", "any"),
            excluded_from_risk=("excluded_from_risk", "any"),
        )
        .reset_index()
    )
    per_emp = per_emp[
        (per_emp["late_count"] > 0)
        | (per_emp["missing_checkout_count"] > 0)
        | (per_emp["excuse_count"] > 0)
        | (per_emp["overtime_cases"] > 0)
        | (per_emp["early_leave_cases"] > 0)
        | (per_emp["is_excluded"])
    ].copy()

    if per_emp.empty:
        return per_emp

    per_emp["total_late_minutes"] = per_emp["total_late_minutes"].astype(int)
    per_emp["late_count"] = per_emp["late_count"].astype(int)
    per_emp["missing_checkout_count"] = per_emp["missing_checkout_count"].astype(int)
    per_emp["excuse_count"] = per_emp["excuse_count"].astype(int)
    per_emp["overtime_cases"] = per_emp["overtime_cases"].astype(int)
    per_emp["total_overtime_minutes"] = per_emp["total_overtime_minutes"].astype(int)
    per_emp["total_overtime_hours"] = (per_emp["total_overtime_minutes"] / 60).round(1)
    per_emp["early_leave_cases"] = per_emp["early_leave_cases"].astype(int)
    per_emp["total_early_leave_minutes"] = per_emp["total_early_leave_minutes"].astype(int)
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

    # Risk exclusion: zero the score and mark the level as "Excluded".
    excluded_risk_mask = per_emp["excluded_from_risk"]
    per_emp.loc[excluded_risk_mask, "risk_score"] = 0
    per_emp.loc[excluded_risk_mask, "risk_level"] = "Excluded"

    per_emp["estimated_deduction"] = (
        per_emp["total_late_minutes"] * LATE_MINUTE_COST
    ).round(2)
    per_emp["deduction_capped"] = (
        per_emp["estimated_deduction"].clip(upper=MAX_MONTHLY_DEDUCTION).round(2)
    )

    # Payroll exclusion: zero out both deduction fields.
    excluded_payroll_mask = per_emp["excluded_from_payroll"]
    per_emp.loc[excluded_payroll_mask, "estimated_deduction"] = 0.0
    per_emp.loc[excluded_payroll_mask, "deduction_capped"] = 0.0

    column_order = [
        "Employee ID", "First Name",
        "total_late_minutes", "late_count", "avg_late_minutes",
        "missing_checkout_count", "excuse_count",
        "overtime_cases", "total_overtime_minutes", "total_overtime_hours",
        "early_leave_cases", "total_early_leave_minutes",
        "risk_score", "risk_level", "risk_reason",
        "estimated_deduction", "deduction_capped",
        "is_excluded", "exclusion_reason",
        "excluded_from_late", "excluded_from_overtime",
        "excluded_from_payroll", "excluded_from_risk",
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


def _build_top_early_leave_employees(daily):
    """Per-employee early-leave aggregates, sorted by total minutes desc.

    Early leave is a discipline-adjacent metric, so we honor the same
    `excluded_from_late` flag used by the lateness KPIs.
    """
    el_rows = daily[
        (daily["early_leave_status"] == "Early Leave")
        & (~daily.get("excluded_from_late", False))
    ]
    if el_rows.empty:
        return pd.DataFrame(
            columns=[
                "Employee ID", "First Name",
                "early_leave_cases", "total_early_leave_minutes",
            ]
        )
    top = (
        el_rows.groupby(["Employee ID", "First Name"])
        .agg(
            early_leave_cases=("early_leave_minutes", "size"),
            total_early_leave_minutes=("early_leave_minutes", "sum"),
        )
        .reset_index()
        .sort_values("total_early_leave_minutes", ascending=False)
    )
    top["total_early_leave_minutes"] = top["total_early_leave_minutes"].astype(int)
    return top.reset_index(drop=True)


def _build_top_overtime_employees(daily):
    """Per-employee overtime aggregates, sorted by total minutes desc.

    Excluded-from-overtime rows are filtered out so the chart reflects
    the management view of who is genuinely earning overtime.
    """
    ot_rows = daily[
        (daily["overtime_status"] == "Overtime")
        & (~daily.get("excluded_from_overtime", False))
    ]
    if ot_rows.empty:
        return pd.DataFrame(
            columns=[
                "Employee ID", "First Name",
                "overtime_cases", "total_overtime_minutes",
                "avg_overtime_minutes", "total_overtime_hours",
            ]
        )
    top = (
        ot_rows.groupby(["Employee ID", "First Name"])
        .agg(
            overtime_cases=("overtime_minutes", "size"),
            total_overtime_minutes=("overtime_minutes", "sum"),
            avg_overtime_minutes=("overtime_minutes", "mean"),
        )
        .reset_index()
        .sort_values("total_overtime_minutes", ascending=False)
    )
    top["total_overtime_minutes"] = top["total_overtime_minutes"].astype(int)
    top["avg_overtime_minutes"] = top["avg_overtime_minutes"].astype(int)
    top["total_overtime_hours"] = (top["total_overtime_minutes"] / 60).round(1)
    return top.reset_index(drop=True)


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


_VALID_PUNCH_STATES = {"Check In", "Check Out", "Break In", "Break Out"}

# Per-employee thresholds for HR audit flags. Kept here so HR can find
# them in one place without hunting through config; promote to config.py
# if the values need to vary by tenant.
_CHRONIC_LATE_THRESHOLD = 5
_REPEATED_MISSING_CHECKOUT_THRESHOLD = 5
_EXCESSIVE_EXCUSE_THRESHOLD = 4
_ANOMALY_DELAY_THRESHOLD_MIN = 240  # 4+ hours suggests wrong-shift assignment


def _build_employee_master(df, schedules_df, daily):
    """Per-employee reconciliation + HR audit flags.

    One row per Employee ID seen in the attendance file. Columns:
      Employee ID, First Name, Odoo Resource, Attendance Presence,
      Schedule Presence, Status Consistency, checkin_count, late_count,
      excuse_count, missing_checkout_count, audit_flags.

    audit_flags is a comma-separated string drawn from:
      chronic_lateness, repeated_missing_checkouts, excessive_excuses,
      no_assigned_schedule, attendance_anomaly.
    """
    daily_agg = (
        daily.groupby("Employee ID")
        .agg(
            checkin_count=("Date", "size"),
            late_count=("is_late", "sum"),
            excuse_count=(
                "attendance_status",
                lambda s: int((s == "Approved Excuse").sum()),
            ),
            missing_checkout_count=("missing_check_out", "sum"),
            max_delay=("unexcused_delay_minutes", "max"),
        )
        .reset_index()
    )

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
    all_emp["Schedule Presence"] = all_emp["First Name"].isin(schedule_names)
    all_emp["Odoo Resource"] = all_emp["First Name"].where(
        all_emp["Schedule Presence"], None
    )

    master = all_emp.merge(daily_agg, on="Employee ID", how="left")
    for col in ["checkin_count", "late_count", "excuse_count",
                "missing_checkout_count", "max_delay"]:
        master[col] = master[col].fillna(0).astype(int)
    master["Attendance Presence"] = master["checkin_count"] > 0

    def _consistency(row):
        if row["Attendance Presence"] and row["Schedule Presence"]:
            return "Consistent"
        if row["Attendance Presence"]:
            return "Orphan (no schedule)"
        if row["Schedule Presence"]:
            return "Inactive (no check-ins)"
        return "Unknown"

    master["Status Consistency"] = master.apply(_consistency, axis=1)

    def _flags(row):
        flags = []
        if row["late_count"] >= _CHRONIC_LATE_THRESHOLD:
            flags.append("chronic_lateness")
        if row["missing_checkout_count"] >= _REPEATED_MISSING_CHECKOUT_THRESHOLD:
            flags.append("repeated_missing_checkouts")
        if row["excuse_count"] >= _EXCESSIVE_EXCUSE_THRESHOLD:
            flags.append("excessive_excuses")
        if row["Attendance Presence"] and not row["Schedule Presence"]:
            flags.append("no_assigned_schedule")
        if row["max_delay"] >= _ANOMALY_DELAY_THRESHOLD_MIN:
            flags.append("attendance_anomaly")
        return ", ".join(flags)

    master["audit_flags"] = master.apply(_flags, axis=1)

    return master[
        [
            "Employee ID", "First Name", "Odoo Resource",
            "Attendance Presence", "Schedule Presence", "Status Consistency",
            "checkin_count", "late_count", "excuse_count",
            "missing_checkout_count", "audit_flags",
        ]
    ].sort_values("Employee ID").reset_index(drop=True)


def _compute_data_quality_score(
    df, daily, employee_master,
    missing_schedule_cases, missing_check_out_cases,
    unscheduled_active, duplicate_names, missing_ids, invalid_punches,
):
    """Return a 0..100 data quality score. Higher is cleaner data.

    Each contributing factor adds a capped penalty so that catastrophic
    data still tops out at 0 (never goes negative) and no single factor
    can dominate the score.
    """
    total_daily = max(len(daily), 1)
    total_emp = max(len(employee_master), 1)
    total_punches = max(len(df), 1)

    penalties = {
        "missing_schedule": min(25, 100 * missing_schedule_cases / total_daily),
        "missing_checkout": min(15, 100 * missing_check_out_cases / total_daily),
        "orphan_employees": min(20, 100 * unscheduled_active / total_emp),
        "duplicate_names": min(10, duplicate_names * 5),
        "missing_employee_ids": min(15, missing_ids),
        "invalid_punches": min(15, 100 * invalid_punches / total_punches),
    }
    score = max(0.0, 100.0 - sum(penalties.values()))
    return round(score, 1)


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


def calculate_metrics(df, schedules_df, time_off_df=None, excluded_df=None):
    # Build the source-of-truth intervals lookup ONCE; derive the
    # single-time lookups from it so all three views stay consistent
    # even when shifts are split.
    intervals_lookup = _build_shift_intervals_lookup(schedules_df)
    shift_lookup = {
        name: (intervals[0][0] if intervals else None)
        for name, intervals in intervals_lookup.items()
    }
    daily = _build_daily_attendance(df, shift_lookup)
    daily = _attach_attendance_status(daily, time_off_df)
    daily = _attach_checkout_info(daily, df)
    daily = _attach_overtime_info(daily, intervals_lookup)
    daily = _attach_department(daily, df)
    daily = _attach_exclusion_info(daily, excluded_df)

    # KPI aggregates honor exclusions. Raw daily rows remain untouched
    # so HR can still inspect what each employee did.
    late_rows = daily[
        (daily["attendance_status"] == "Late")
        & (~daily["excluded_from_late"])
    ]
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
    employee_master = _build_employee_master(df, schedules_df, daily)
    top_overtime_employees = _build_top_overtime_employees(daily)
    top_early_leave_employees = _build_top_early_leave_employees(daily)

    overtime_rows = daily[
        (daily["overtime_status"] == "Overtime")
        & (~daily["excluded_from_overtime"])
    ]
    overtime_cases = int(len(overtime_rows))
    total_overtime_minutes = int(overtime_rows["overtime_minutes"].sum())
    total_overtime_hours = round(total_overtime_minutes / 60, 1)
    employees_with_overtime = int(overtime_rows["Employee ID"].nunique()) if overtime_cases else 0
    avg_overtime_minutes = (
        int(overtime_rows["overtime_minutes"].mean()) if overtime_cases else 0
    )

    # Early-leave honors the same exclusion flag as the lateness KPIs.
    early_leave_rows = daily[
        (daily["early_leave_status"] == "Early Leave")
        & (~daily["excluded_from_late"])
    ]
    early_leave_cases = int(len(early_leave_rows))
    total_early_leave_minutes = int(early_leave_rows["early_leave_minutes"].sum())
    employees_with_early_leave = (
        int(early_leave_rows["Employee ID"].nunique()) if early_leave_cases else 0
    )

    excluded_employees_summary = _build_excluded_employees_summary(daily, excluded_df)

    schedule_names = set(schedules_df["Name"].dropna().astype(str).str.strip())
    orphan_attendance_records = int(
        (~df["First Name"].astype(str).str.strip().isin(schedule_names)).sum()
    )
    unscheduled_active = int(
        ((employee_master["Attendance Presence"]) & (~employee_master["Schedule Presence"])).sum()
    )
    duplicate_names = int(
        (employee_master["First Name"].value_counts() > 1).sum()
    )
    missing_ids = int(df["Employee ID"].isna().sum())
    invalid_punches = int((~df["Punch State"].isin(_VALID_PUNCH_STATES)).sum())

    missing_schedule_cases = int((daily["attendance_status"] == "Missing Schedule").sum())
    missing_check_out_cases = int(daily["missing_check_out"].sum())
    data_quality_score = _compute_data_quality_score(
        df, daily, employee_master,
        missing_schedule_cases, missing_check_out_cases,
        unscheduled_active, duplicate_names, missing_ids, invalid_punches,
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
        "missing_schedule_cases": missing_schedule_cases,
        "missing_check_out_cases": missing_check_out_cases,
        "excused_delay_minutes": int(daily["excused_delay_minutes"].sum()),
        "total_estimated_deduction": round(total_est, 2),
        "total_deduction_capped": round(total_capped, 2),
        "high_risk_employees": high_risk_employees,
        # Data-quality / audit signals from the employee master.
        "orphan_attendance_records": orphan_attendance_records,
        "duplicate_employee_names": duplicate_names,
        "missing_employee_ids": missing_ids,
        "unscheduled_active_employees": unscheduled_active,
        "invalid_punches_count": invalid_punches,
        "data_quality_score": data_quality_score,
        # Overtime KPIs.
        "overtime_cases": overtime_cases,
        "total_overtime_minutes": total_overtime_minutes,
        "total_overtime_hours": total_overtime_hours,
        "employees_with_overtime": employees_with_overtime,
        "avg_overtime_minutes": avg_overtime_minutes,
        # Early leave KPIs.
        "early_leave_cases": early_leave_cases,
        "total_early_leave_minutes": total_early_leave_minutes,
        "employees_with_early_leave": employees_with_early_leave,
        # DataFrames.
        "employee_summary": employee_summary,
        "status_summary": status_summary,
        "excused_vs_unexcused": excused_vs_unexcused,
        "department_summary": department_summary,
        "missing_punch_summary": missing_punch_summary,
        "daily_trend": daily_trend,
        "employee_reconciliation": reconciliation_table,
        "employee_reconciliation_details": reconciliation_details,
        "employee_master": employee_master,
        "top_overtime_employees": top_overtime_employees,
        "top_early_leave_employees": top_early_leave_employees,
        "excluded_employees_summary": excluded_employees_summary,
        "excluded_employee_count": int(
            daily.loc[daily["is_excluded"], "Employee ID"].nunique()
        ),
    }
    return summary, daily
