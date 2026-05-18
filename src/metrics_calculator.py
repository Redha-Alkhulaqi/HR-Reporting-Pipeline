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
    BREAK_IN_STATES,
    BREAK_OUT_STATES,
    EARLY_LEAVE_GRACE_MINUTES,
    GRACE_MINUTES,
    LATE_MINUTE_COST,
    MAX_MONTHLY_DEDUCTION,
    MAX_REASONABLE_EARLY_LEAVE_MINUTES,
    MIN_OVERTIME_MINUTES,
    OVERTIME_GRACE_MINUTES,
    PUBLIC_HOLIDAYS,
    RISK_HIGH_THRESHOLD,
    RISK_MEDIUM_THRESHOLD,
    WEEKLY_OFF_DAYS,
)


# Time Off Type values containing any of these substrings (case-insensitive)
# are treated as approved EXCUSES (partial hourly permission). Every other
# approved time-off row is treated as LEAVE (full-day, supersedes attendance).
_EXCUSE_KEYWORDS = ("استأذان", "استئذان", "excuse", "permission")

# Time Off Type values containing any of these substrings (case-insensitive)
# are treated as SECONDMENT. Everything else that is not an excuse falls
# under VACATION (Annual / Sick / unpaid / etc.).
_SECONDMENT_KEYWORDS = ("secondment", "انتداب", "ندب", "intidab")

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


def _is_secondment_type(type_name):
    if type_name is None:
        return False
    text = str(type_name).lower()
    return any(kw.lower() in text for kw in _SECONDMENT_KEYWORDS)


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


# EMP-code pattern. Matches "EMP420", "emp 420", "EMP-420", "EMP_420".
_EMP_CODE_RE = re.compile(r"EMP[\s_\-]?(\d+)", re.IGNORECASE)


def _extract_emp_code(name):
    """Return the EMP code (e.g. "EMP420") inside a name, or None.

    Recognises any of:
        "MOHAMMED LAHIQ ALMUTAIRI-EMP420"
        "Mohammed Lahiq Almutairi EMP 420"
        "...-emp_420"
    The returned code is upper-cased and digit-only-suffixed so
    EMP-code matching is stable regardless of spacing or separators.
    """
    if name is None or (isinstance(name, float) and pd.isna(name)):
        return None
    match = _EMP_CODE_RE.search(str(name))
    if not match:
        return None
    return f"EMP{match.group(1)}"


def _strip_emp_code(name):
    """Return `name` with any EMP code (and trailing separator) removed.

    "MOHAMMED LAHIQ ALMUTAIRI-EMP420" -> "MOHAMMED LAHIQ ALMUTAIRI"
    """
    if name is None or (isinstance(name, float) and pd.isna(name)):
        return ""
    cleaned = _EMP_CODE_RE.sub("", str(name))
    # Trim trailing separators ("- ", " _", etc.) left by the cut.
    cleaned = re.sub(r"[\s_\-]+$", "", cleaned)
    return cleaned


def _strong_normalize(name):
    """Aggressive name key for schedule matching.

    Replaces NBSP (U+00A0) and other unicode whitespace with regular
    spaces, collapses runs of whitespace, uppercases, and trims. NBSP
    is a real-world hazard in Odoo exports -- a name like
    "MOHAMMED\\xa0LAHIQ ALMUTAIRI" looks identical to a human but does
    not match the BioTime "MOHAMMED LAHIQ ALMUTAIRI" with `==`.
    """
    if name is None or (isinstance(name, float) and pd.isna(name)):
        return ""
    text = str(name).replace("\xa0", " ").replace("​", " ")
    return " ".join(text.split()).upper()


# Candidate column names for the "shift label" column in the Odoo
# resource export. Ordered by preference; first existing column wins.
_SCHEDULE_LABEL_CANDIDATES = (
    "Working Time", "Working Hours", "Resource Calendar",
    "Calendar", "Schedule",
)


def _resolve_schedule_label_column(schedules_df):
    """Return the column name that carries the Odoo shift label, or None."""
    if schedules_df is None or schedules_df.empty:
        return None
    for col in _SCHEDULE_LABEL_CANDIDATES:
        if col in schedules_df.columns:
            return col
    return None


class ScheduleLookup:
    """Multi-key schedule lookup with deterministic priority.

    Priority used by `match`:
      1. EMP code (e.g. "EMP420") if present in both the attendance
         name and exactly one schedule row.
      2. Exact `_strong_normalize` match (handles NBSP, casing,
         whitespace collapse) when the result is unique.
      3. Stripped-EMP normalized match (compare names with the EMP
         code removed) when the result is unique.
      4. Substring "fuzzy-safe" match: schedule name contains the
         attendance name or vice versa, only when exactly one
         schedule row qualifies (avoids picking the wrong twin).

    `match(first_name)` returns a dict with the matched intervals,
    the matched schedule name, the rule that fired ("emp_code",
    "exact_normalized", "stripped_emp", "substring_unique", or "" for
    no match), and the raw Working Time label.
    """

    def __init__(self, schedules_df, label_column="Working Time"):
        self._label_column = label_column
        self._by_emp_code = {}
        self._by_strong_norm = {}
        self._by_stripped = {}
        self._all_entries = []
        self._duplicate_emp_codes = set()
        self._duplicate_strong = set()
        self._duplicate_stripped = set()
        if schedules_df is None or schedules_df.empty:
            return
        if label_column not in schedules_df.columns:
            return
        for _, row in schedules_df.iterrows():
            raw_name = row.get("Name")
            if raw_name is None or (isinstance(raw_name, float) and pd.isna(raw_name)):
                continue
            label = row.get(label_column)
            intervals = extract_shift_intervals(label)
            entry = {
                "name": str(raw_name),
                "label": None if pd.isna(label) else str(label),
                "intervals": intervals,
            }
            self._all_entries.append(entry)
            emp_code = _extract_emp_code(raw_name)
            if emp_code:
                if emp_code in self._by_emp_code and self._by_emp_code[emp_code] is not entry:
                    self._duplicate_emp_codes.add(emp_code)
                else:
                    self._by_emp_code[emp_code] = entry
            strong = _strong_normalize(raw_name)
            if strong:
                if strong in self._by_strong_norm and self._by_strong_norm[strong] is not entry:
                    self._duplicate_strong.add(strong)
                else:
                    self._by_strong_norm[strong] = entry
            stripped = _strong_normalize(_strip_emp_code(raw_name))
            if stripped:
                if stripped in self._by_stripped and self._by_stripped[stripped] is not entry:
                    self._duplicate_stripped.add(stripped)
                else:
                    self._by_stripped[stripped] = entry

    @staticmethod
    def _empty_match():
        return {
            "intervals": [],
            "matched_name": None,
            "matched_by": "",
            "working_time": None,
        }

    def match(self, attendance_name):
        if attendance_name is None or (isinstance(attendance_name, float) and pd.isna(attendance_name)):
            return self._empty_match()
        text = str(attendance_name)

        emp_code = _extract_emp_code(text)
        if emp_code and emp_code in self._by_emp_code and emp_code not in self._duplicate_emp_codes:
            entry = self._by_emp_code[emp_code]
            return {
                "intervals": entry["intervals"],
                "matched_name": entry["name"],
                "matched_by": "emp_code",
                "working_time": entry["label"],
            }

        strong = _strong_normalize(text)
        if strong and strong in self._by_strong_norm and strong not in self._duplicate_strong:
            entry = self._by_strong_norm[strong]
            return {
                "intervals": entry["intervals"],
                "matched_name": entry["name"],
                "matched_by": "exact_normalized",
                "working_time": entry["label"],
            }

        stripped = _strong_normalize(_strip_emp_code(text))
        if stripped and stripped in self._by_stripped and stripped not in self._duplicate_stripped:
            entry = self._by_stripped[stripped]
            return {
                "intervals": entry["intervals"],
                "matched_name": entry["name"],
                "matched_by": "stripped_emp",
                "working_time": entry["label"],
            }

        # Last-resort: bidirectional substring match, but only if EXACTLY one
        # candidate matches (so we never silently pick the wrong twin).
        if stripped or strong:
            needle = stripped or strong
            candidates = []
            for entry in self._all_entries:
                hay_strong = _strong_normalize(entry["name"])
                hay_stripped = _strong_normalize(_strip_emp_code(entry["name"]))
                if not (hay_strong or hay_stripped):
                    continue
                if (
                    needle and hay_stripped and (needle in hay_stripped or hay_stripped in needle)
                ) or (
                    needle and hay_strong and (needle in hay_strong or hay_strong in needle)
                ):
                    candidates.append(entry)
            unique = {id(c): c for c in candidates}
            if len(unique) == 1:
                entry = next(iter(unique.values()))
                return {
                    "intervals": entry["intervals"],
                    "matched_name": entry["name"],
                    "matched_by": "substring_unique",
                    "working_time": entry["label"],
                }

        return self._empty_match()


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
    """Return dict: cleaned employee name -> list of (start_HHMM, end_HHMM).

    Kept for backward compatibility (a few callers expect the dict).
    New code should prefer `ScheduleLookup`, which understands EMP codes
    and NBSP-laced Odoo names. The dict produced here is keyed by the
    schedule's raw `Name` after .strip() and represents the legacy view.
    """
    label_col = _resolve_schedule_label_column(schedules_df) or "Working Time"
    if label_col not in schedules_df.columns or "Name" not in schedules_df.columns:
        return {}
    schedules = schedules_df[["Name", label_col]].copy()
    schedules["Name"] = schedules["Name"].astype(str).str.strip()
    schedules["intervals"] = schedules[label_col].apply(extract_shift_intervals)
    return schedules.set_index("Name")["intervals"].to_dict()




def _build_daily_attendance(df, schedule_lookup):
    """Aggregate punches into one row per (employee, day) with raw delay info.

    `schedule_lookup` is a `ScheduleLookup` instance. Each unique
    `First Name` in the attendance data is resolved through the
    multi-key matcher; the matched intervals fill the `Shift Start`
    and `matched_*` columns. Missing matches become `missing_schedule=True`.
    """
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

    # Resolve schedule per unique name once, then map back to all rows.
    unique_names = daily["First Name"].drop_duplicates().tolist()
    name_to_match = {n: schedule_lookup.match(n) for n in unique_names}

    daily["Shift Start"] = daily["First Name"].map(
        lambda n: (name_to_match[n]["intervals"][0][0]
                   if name_to_match.get(n) and name_to_match[n]["intervals"]
                   else None)
    )
    daily["matched_schedule_name"] = daily["First Name"].map(
        lambda n: name_to_match.get(n, {}).get("matched_name")
    )
    daily["matched_by"] = daily["First Name"].map(
        lambda n: name_to_match.get(n, {}).get("matched_by") or ""
    )
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
    "early_leave_anomaly", "early_leave_anomaly_reason",
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
        "early_leave_anomaly": False,
        "early_leave_anomaly_reason": "",
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

    # Flag implausibly large early-leave values for HR review. We DO
    # NOT drop the row -- it still contributes to the totals -- because
    # the underlying cause is usually data-quality (missing Check Out,
    # wrong shift, device sync) that HR should chase up.
    if early_leave > MAX_REASONABLE_EARLY_LEAVE_MINUTES:
        anomaly, anomaly_reason = True, "Exceeds reasonable threshold"
    else:
        anomaly, anomaly_reason = False, ""

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
        "early_leave_anomaly": anomaly,
        "early_leave_anomaly_reason": anomaly_reason,
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


def _compute_break_minutes(start_str, end_str):
    """Minutes between two HH:MM:SS punch strings (handles midnight wrap)."""
    start = datetime.strptime(start_str, "%H:%M:%S")
    end = datetime.strptime(end_str, "%H:%M:%S")
    if end < start:
        end += pd.Timedelta(days=1).to_pytimedelta()
    return int((end - start).total_seconds() / 60)


def _compute_breaks(punches):
    """Walk one day's break punches in time order and pair them.

    `punches` is a list of (time_str, state_str). Each Break Out opens
    a window; the next Break In closes it. Anything left unclosed (or
    a Break In with no open window) is counted as incomplete.

    Returns (break_count, total_break_minutes, incomplete_break_count).
    """
    break_count = 0
    total_minutes = 0
    incomplete = 0
    open_at = None
    out_set = set(BREAK_OUT_STATES)
    in_set = set(BREAK_IN_STATES)
    for time_str, state in punches:
        if state in out_set:
            if open_at is not None:
                incomplete += 1  # Previous break-out never closed.
            open_at = time_str
        elif state in in_set:
            if open_at is not None:
                total_minutes += _compute_break_minutes(open_at, time_str)
                break_count += 1
                open_at = None
            else:
                incomplete += 1  # Break-in without matching break-out.
    if open_at is not None:
        incomplete += 1
    return break_count, total_minutes, incomplete


def _attach_break_info(daily, df):
    """Add break_count, total_break_minutes, incomplete_break_count to daily.

    Breaks are INFORMATIONAL only -- they never feed lateness,
    overtime, early leave, payroll, or risk scoring. The columns are
    surfaced for HR visibility on the Daily Attendance and Break
    Summary sheets.
    """
    all_break_states = set(BREAK_OUT_STATES) | set(BREAK_IN_STATES)
    break_punches = df[df["Punch State"].isin(all_break_states)]

    if break_punches.empty:
        daily["break_count"] = 0
        daily["total_break_minutes"] = 0
        daily["incomplete_break_count"] = 0
        return daily

    rows = []
    for (eid, date), group in break_punches.groupby(["Employee ID", "Date"]):
        ordered = group.sort_values("Punch Time")
        punches = list(
            zip(ordered["Punch Time"].astype(str),
                ordered["Punch State"].astype(str))
        )
        bc, bm, ic = _compute_breaks(punches)
        rows.append({
            "Employee ID": eid, "Date": date,
            "break_count": bc,
            "total_break_minutes": bm,
            "incomplete_break_count": ic,
        })
    breaks_df = pd.DataFrame(rows)
    daily = daily.merge(breaks_df, on=["Employee ID", "Date"], how="left")
    daily["break_count"] = daily["break_count"].fillna(0).astype(int)
    daily["total_break_minutes"] = daily["total_break_minutes"].fillna(0).astype(int)
    daily["incomplete_break_count"] = daily["incomplete_break_count"].fillna(0).astype(int)
    return daily


_ABSENCE_DETAILS_COLS = [
    "Employee ID", "First Name", "Date", "Weekday",
    "Is Scheduled Working Day", "Has Attendance",
    "Time Off Type", "Is Permission", "Is Vacation",
    "Is Secondment", "Weekly Off Days", "Is Weekly Off",
    "Is Holiday", "Is Excluded",
    "Counted As Absence", "Absence Reason",
]


_VALID_WEEKDAYS = (
    "Monday", "Tuesday", "Wednesday", "Thursday",
    "Friday", "Saturday", "Sunday",
)


def _normalize_weekday_token(token):
    """Best-effort weekday-name normalization (handles abbreviations
    and case differences). Returns the canonical capitalized name or
    None when the token is not a recognizable weekday.
    """
    if token is None:
        return None
    text = str(token).strip().lower()
    if not text:
        return None
    for day in _VALID_WEEKDAYS:
        d = day.lower()
        if text == d or text == d[:3] or d.startswith(text):
            return day
    return None


def _parse_weekly_off_days(value):
    """Parse a 'Friday,Saturday' style cell into a set of canonical
    weekday names. Empty / unparseable cells yield an empty set."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return set()
    text = str(value).strip()
    if not text:
        return set()
    parts = re.split(r"[,;/|]+", text)
    out = set()
    for raw in parts:
        canonical = _normalize_weekday_token(raw)
        if canonical:
            out.add(canonical)
    return out


def _build_weekly_off_lookup(weekly_off_df, df,
                             default_weekly_off=None,
                             allow_name_match=True):
    """Return dict {Employee ID: set of weekday names off}.

    Overrides come from `weekly_off_df` (the optional
    `employee_weekly_off.xlsx`). Match priority: explicit Employee ID
    row, then normalized name fallback. Employees absent from the
    override file inherit `default_weekly_off`.
    """
    default_set = set(default_weekly_off) if default_weekly_off else set()
    lookup = {}

    if df is None or df.empty:
        return lookup

    # Build name -> [ids] map for fallback matching.
    df_clean = df[df["Employee ID"].notna()].copy()
    df_clean["First Name"] = df_clean["First Name"].astype(str).str.strip()
    name_to_ids = {}
    all_ids = set()
    for eid, sub in df_clean.groupby("Employee ID"):
        all_ids.add(eid)
        non_empty = sub["First Name"][sub["First Name"].astype(bool)]
        if not non_empty.empty:
            name_to_ids.setdefault(_normalize_name(non_empty.iloc[0]), []).append(eid)

    # Seed every employee with the global default.
    for eid in all_ids:
        lookup[eid] = set(default_set)

    if weekly_off_df is None or weekly_off_df.empty:
        return lookup

    for _, row in weekly_off_df.iterrows():
        days = _parse_weekly_off_days(row.get("Weekly Off Days"))
        if not days:
            continue
        raw_id = row.get("Employee ID")
        eid = None
        if pd.notna(raw_id):
            try:
                eid = int(raw_id)
            except (ValueError, TypeError):
                eid = None
        if eid is not None and eid in lookup:
            lookup[eid] = days
            continue
        if allow_name_match:
            target = _normalize_name(row.get("Employee Name"))
            if target and target in name_to_ids:
                for matched_eid in name_to_ids[target]:
                    lookup[matched_eid] = days
    return lookup


def _build_absence_details(daily, df, schedules_df, time_off_df, excluded_df,
                            weekly_off_df=None,
                            period_start=None, period_end=None):
    """Return one row per (employee, date) explaining why a day was or
    was not counted as an absence.

    The reporting period is the contiguous calendar from `period_start`
    to `period_end`. When either bound is omitted it is inferred from
    the min/max Date in `df`, but callers should pass explicit bounds
    whenever they know the intended reporting window so stray
    out-of-window dates (e.g. mis-entered manual corrections) do not
    inflate the absence count. Crucially the calendar includes dates
    where NO employee punched, so zero-attendance days are still
    considered. The employee universe is every unique Employee ID that
    appears anywhere in `df` (Check In, Check Out, breaks alike),
    which catches employees who only have non-Check-In punches.

    A day is counted as absence iff ALL of:
      - employee has a shift assigned in Odoo (otherwise we cannot
        decide expected days),
      - weekday is NOT in WEEKLY_OFF_DAYS,
      - date is NOT in PUBLIC_HOLIDAYS,
      - employee has no Check In that day,
      - employee has no approved time off (permission, vacation/sick,
        secondment) covering that day,
      - employee is not on the exclusion list.

    Time off rows are categorized into Permission (excuse type),
    Secondment (secondment keywords), or Vacation (every other
    approved type, e.g. Annual Leave / Sick Leave).
    """
    if df is None or df.empty:
        return pd.DataFrame(columns=_ABSENCE_DETAILS_COLS)

    # Contiguous reporting-period calendar. Bounds come from the
    # caller when known; otherwise fall back to df's min/max Date.
    parsed_dates = pd.to_datetime(df["Date"], errors="coerce").dropna()
    if parsed_dates.empty and (period_start is None or period_end is None):
        return pd.DataFrame(columns=_ABSENCE_DETAILS_COLS)
    if period_start is None:
        period_start = parsed_dates.min()
    if period_end is None:
        period_end = parsed_dates.max()
    period_start = pd.to_datetime(period_start).normalize()
    period_end = pd.to_datetime(period_end).normalize()
    if period_end < period_start:
        return pd.DataFrame(columns=_ABSENCE_DETAILS_COLS)
    period_dates = [
        d.strftime("%Y-%m-%d")
        for d in pd.date_range(period_start, period_end, freq="D")
    ]

    # Employee universe: every Employee ID seen anywhere in df.
    emp_to_name = {}
    df_clean = df[df["Employee ID"].notna()].copy()
    df_clean["First Name"] = df_clean["First Name"].astype(str).str.strip()
    for eid, sub in df_clean.groupby("Employee ID"):
        non_empty = sub["First Name"][sub["First Name"].astype(bool)]
        emp_to_name[eid] = non_empty.iloc[0] if not non_empty.empty else ""

    # Cross-reference Check Ins -- only Check Ins count as attendance.
    check_ins = df_clean[df_clean["Punch State"] == "Check In"]
    attendance_set = set()
    if not check_ins.empty:
        for eid, date in check_ins[["Employee ID", "Date"]].itertuples(
            index=False
        ):
            attendance_set.add((eid, str(date)))

    # Approved time off -> (Employee ID, Date) -> (category, type_name).
    name_to_ids = {}
    for eid, name in emp_to_name.items():
        if name:
            name_to_ids.setdefault(name, []).append(eid)

    timeoff_by_id = {}
    if time_off_df is not None and not time_off_df.empty:
        approved = time_off_df[time_off_df["Status"] == "Approved"].copy()
        approved["Employee"] = approved["Employee"].astype(str).str.strip()
        approved["Start Date"] = pd.to_datetime(
            approved["Start Date"], errors="coerce"
        )
        approved["End Date"] = pd.to_datetime(
            approved["End Date"], errors="coerce"
        )
        approved = approved.dropna(subset=["Start Date", "End Date"])
        for _, row in approved.iterrows():
            ids = name_to_ids.get(row["Employee"], [])
            if not ids:
                continue
            type_name = row["Time Off Type"]
            if _is_secondment_type(type_name):
                category = "secondment"
            elif _is_excuse_type(type_name):
                category = "permission"
            else:
                category = "vacation"
            for d in pd.date_range(
                row["Start Date"].normalize(),
                row["End Date"].normalize(),
                freq="D",
            ):
                ds = d.strftime("%Y-%m-%d")
                for eid in ids:
                    # Don't overwrite a stronger category (vacation +
                    # secondment overlap is extremely rare; first wins).
                    timeoff_by_id.setdefault(eid, {}).setdefault(
                        ds, (category, type_name)
                    )

    # Exclusions: resolve once per Employee ID.
    exclusion_rules = (
        _build_exclusion_rules(excluded_df)
        if excluded_df is not None else []
    )
    excluded_ids = set()
    if exclusion_rules:
        for eid, name in emp_to_name.items():
            rule = _match_exclusion(
                eid, name, exclusion_rules,
                ALLOW_NAME_BASED_EXCLUSION_MATCH,
            )
            if rule is not None and any(rule["flags"].values()):
                excluded_ids.add(eid)

    scheduled_names = set(
        schedules_df["Name"].dropna().astype(str).str.strip()
    )
    # Per-employee weekly off: caller-supplied overrides win, otherwise
    # the global WEEKLY_OFF_DAYS default applies.
    weekly_off_lookup = _build_weekly_off_lookup(
        weekly_off_df, df,
        default_weekly_off=WEEKLY_OFF_DAYS,
        allow_name_match=ALLOW_NAME_BASED_EXCLUSION_MATCH,
    )
    holiday_set = set(PUBLIC_HOLIDAYS)

    def _format_off_days(days):
        return ",".join(d for d in _VALID_WEEKDAYS if d in days)

    rows = []
    for eid in sorted(emp_to_name.keys()):
        name = emp_to_name[eid]
        has_schedule = bool(name) and name in scheduled_names
        is_excluded = eid in excluded_ids
        emp_timeoff = timeoff_by_id.get(eid, {})
        emp_off_days = weekly_off_lookup.get(eid, set(WEEKLY_OFF_DAYS))
        emp_off_days_label = _format_off_days(emp_off_days)
        for date_str in period_dates:
            weekday = pd.to_datetime(date_str).strftime("%A")
            is_weekly_off = weekday in emp_off_days
            is_holiday = date_str in holiday_set
            is_scheduled = (
                has_schedule and not is_weekly_off and not is_holiday
            )
            has_att = (eid, date_str) in attendance_set
            tof = emp_timeoff.get(date_str)
            type_name = tof[1] if tof else None
            is_permission = tof is not None and tof[0] == "permission"
            is_vacation = tof is not None and tof[0] == "vacation"
            is_secondment = tof is not None and tof[0] == "secondment"
            has_off = tof is not None
            counted = (
                is_scheduled
                and not has_att
                and not has_off
                and not is_excluded
            )

            if counted:
                reason = "Absent (no attendance and no approved time off)"
            elif is_excluded:
                reason = "Excluded employee"
            elif is_holiday:
                reason = "Public holiday"
            elif is_weekly_off:
                reason = f"Weekly off ({weekday})"
            elif not has_schedule:
                reason = "Employee has no Odoo schedule"
            elif has_att and has_off:
                reason = f"Attended with approved time off ({type_name})"
            elif has_att:
                reason = "Present (has attendance)"
            elif has_off:
                reason = f"Approved time off ({type_name})"
            else:
                reason = ""

            rows.append({
                "Employee ID": eid,
                "First Name": name,
                "Date": date_str,
                "Weekday": weekday,
                "Is Scheduled Working Day": is_scheduled,
                "Has Attendance": has_att,
                "Time Off Type": type_name,
                "Is Permission": is_permission,
                "Is Vacation": is_vacation,
                "Is Secondment": is_secondment,
                "Weekly Off Days": emp_off_days_label,
                "Is Weekly Off": is_weekly_off,
                "Is Holiday": is_holiday,
                "Is Excluded": is_excluded,
                "Counted As Absence": counted,
                "Absence Reason": reason,
            })
    return pd.DataFrame(rows, columns=_ABSENCE_DETAILS_COLS)


_ABSENCE_AUDIT_COLS = [
    "Employee ID", "First Name",
    "weekly_off_days", "scheduled_weekdays",
    "scheduled_working_days", "attended_days",
    "permission_days", "vacation_days", "secondment_days",
    "absence_days", "reconciliation_delta",
]


def _build_absence_audit(absence_details):
    """Per-employee audit totals derived from Absence Details.

    For each non-excluded employee:
      scheduled_working_days =
          attended_days + permission_days + vacation_days
        + secondment_days + absence_days

    The five RHS buckets are mutually exclusive over each scheduled
    working day (priority: attended > permission > vacation >
    secondment > absence) so a non-zero `reconciliation_delta` flags a
    bookkeeping inconsistency for HR to investigate.

    `weekly_off_days` is the per-employee off-day policy (e.g.
    "Friday,Saturday"); `scheduled_weekdays` is the complement.
    """
    if absence_details is None or absence_details.empty:
        return pd.DataFrame(columns=_ABSENCE_AUDIT_COLS)

    df = absence_details[~absence_details["Is Excluded"]].copy()
    if df.empty:
        return pd.DataFrame(columns=_ABSENCE_AUDIT_COLS)

    working = df["Is Scheduled Working Day"]
    df["_sched"] = working
    df["_attended"] = working & df["Has Attendance"]
    df["_permission"] = (
        working & ~df["Has Attendance"] & df["Is Permission"]
    )
    df["_vacation"] = (
        working & ~df["Has Attendance"] & df["Is Vacation"] & ~df["Is Permission"]
    )
    df["_secondment"] = (
        working & ~df["Has Attendance"] & df["Is Secondment"]
        & ~df["Is Permission"] & ~df["Is Vacation"]
    )
    df["_absence"] = df["Counted As Absence"]

    grp = (
        df.groupby(["Employee ID", "First Name"], as_index=False)
        .agg(
            scheduled_working_days=("_sched", "sum"),
            attended_days=("_attended", "sum"),
            permission_days=("_permission", "sum"),
            vacation_days=("_vacation", "sum"),
            secondment_days=("_secondment", "sum"),
            absence_days=("_absence", "sum"),
        )
    )
    int_cols = [
        "scheduled_working_days", "attended_days",
        "permission_days", "vacation_days",
        "secondment_days", "absence_days",
    ]
    for col in int_cols:
        grp[col] = grp[col].astype(int)
    grp["reconciliation_delta"] = (
        grp["scheduled_working_days"]
        - (grp["attended_days"] + grp["permission_days"]
           + grp["vacation_days"] + grp["secondment_days"]
           + grp["absence_days"])
    )

    # Attach the per-employee weekly off policy (and its complement)
    # so the audit row tells HR exactly which weekdays drove the
    # scheduled-working-days figure.
    off_lookup = (
        df.groupby("Employee ID")["Weekly Off Days"].first().to_dict()
    )
    grp["weekly_off_days"] = grp["Employee ID"].map(off_lookup).fillna("")

    def _complement_weekdays(off_label):
        off = _parse_weekly_off_days(off_label)
        return ",".join(d for d in _VALID_WEEKDAYS if d not in off)

    grp["scheduled_weekdays"] = grp["weekly_off_days"].apply(_complement_weekdays)
    return grp[_ABSENCE_AUDIT_COLS].sort_values(
        by=["reconciliation_delta", "absence_days", "First Name"],
        ascending=[False, False, True],
    ).reset_index(drop=True)


def _category_counts_from_audit(absence_audit):
    """Return (absence, permission, vacation, secondment) dicts keyed
    by Employee ID, derived from the audit table. The audit is the
    single source of truth for these four counts so they stay
    internally consistent with the reconciliation balance."""
    empty = ({}, {}, {}, {})
    if absence_audit is None or absence_audit.empty:
        return empty
    return (
        dict(zip(absence_audit["Employee ID"], absence_audit["absence_days"])),
        dict(zip(absence_audit["Employee ID"], absence_audit["permission_days"])),
        dict(zip(absence_audit["Employee ID"], absence_audit["vacation_days"])),
        dict(zip(absence_audit["Employee ID"], absence_audit["secondment_days"])),
    )


# Executive-summary tuning. These thresholds intentionally DIFFER from
# the pipeline-wide constants so HR can present a stricter or looser
# headline figure without changing the underlying daily classification.
_EXEC_LATE_GRACE_MINUTES = 15
_EXEC_EARLY_LEAVE_GRACE_MINUTES = 5
_BREAK_POLICY_FREE_MINUTES = 60


_EXECUTIVE_SUMMARY_COLUMNS = [
    "Employee ID", "First Name",
    "No of Absence Days", "No of Permission Days",
    "No of Vacation Days", "No of Secondment Days",
    "Total Late (Hours)", "Total Over Time (Hours)",
    "Total Early Leave (Hours)",
    "Break Time (Hours)", "Break Time (After Policy)",
]


def _build_executive_employee_summary(daily, absence_by_id,
                                      permission_by_id, vacation_by_id,
                                      secondment_by_id):
    """Build the 11-column executive view of per-employee totals.

    Columns (exact names, in order):
      Employee ID, First Name,
      No of Absence Days, No of Permission Days,
      No of Vacation Days, No of Secondment Days,
      Total Late (Hours), Total Over Time (Hours),
      Total Early Leave (Hours),
      Break Time (Hours), Break Time (After Policy)

    Late and Early Leave use their OWN executive grace thresholds
    (15 and 5 minutes) so the executive figure is independent of the
    pipeline's per-row classification grace. Break Time (After Policy)
    ignores the first 60 break minutes per day and counts only the
    excess. `absence_by_id` is the audited count from
    `_build_absence_details` -- weekly-off days and holidays are
    already excluded. Employees flagged via the exclusion file are
    dropped entirely from this executive view.
    """
    cols = _EXECUTIVE_SUMMARY_COLUMNS
    if daily.empty:
        return pd.DataFrame(columns=cols)

    visible = daily
    if "is_excluded" in daily.columns:
        visible = daily[~daily["is_excluded"]]
    if visible.empty:
        return pd.DataFrame(columns=cols)

    work = pd.DataFrame({
        "Employee ID": visible["Employee ID"],
        "First Name": visible["First Name"],
    })

    raw_delay = visible["Delay Minutes"].clip(lower=0).astype(int)
    work["_late_min"] = raw_delay.where(raw_delay > _EXEC_LATE_GRACE_MINUTES, 0)

    # Raw early-leave gap = matched Shift End - Check Out (clamped >= 0).
    # We use the datetime columns so split-shift matching is honored.
    def _raw_early(row):
        if not row["has_check_out"]:
            return 0
        se = row.get("Shift End DateTime")
        co = row.get("Check Out DateTime")
        if pd.isna(se) or pd.isna(co):
            return 0
        gap = int((se - co).total_seconds() / 60)
        return max(0, gap)

    raw_early = visible.apply(_raw_early, axis=1)
    work["_early_leave_min"] = raw_early.where(
        raw_early > _EXEC_EARLY_LEAVE_GRACE_MINUTES, 0
    )

    work["_overtime_min"] = visible["overtime_minutes"].astype(int)
    work["_break_min"] = visible["total_break_minutes"].astype(int)
    work["_break_after_policy_min"] = (
        work["_break_min"] - _BREAK_POLICY_FREE_MINUTES
    ).clip(lower=0)

    grp = (
        work.groupby(["Employee ID", "First Name"], as_index=False)
        .agg(
            late_min=("_late_min", "sum"),
            overtime_min=("_overtime_min", "sum"),
            early_leave_min=("_early_leave_min", "sum"),
            break_min=("_break_min", "sum"),
            break_after_policy_min=("_break_after_policy_min", "sum"),
        )
    )

    grp["No of Absence Days"] = (
        grp["Employee ID"].map(absence_by_id).fillna(0).astype(int)
    )
    grp["No of Permission Days"] = (
        grp["Employee ID"].map(permission_by_id).fillna(0).astype(int)
    )
    grp["No of Vacation Days"] = (
        grp["Employee ID"].map(vacation_by_id).fillna(0).astype(int)
    )
    grp["No of Secondment Days"] = (
        grp["Employee ID"].map(secondment_by_id).fillna(0).astype(int)
    )
    grp["Total Late (Hours)"] = (grp["late_min"] / 60).round(1)
    grp["Total Over Time (Hours)"] = (grp["overtime_min"] / 60).round(1)
    grp["Total Early Leave (Hours)"] = (grp["early_leave_min"] / 60).round(1)
    grp["Break Time (Hours)"] = (grp["break_min"] / 60).round(1)
    grp["Break Time (After Policy)"] = (grp["break_after_policy_min"] / 60).round(1)

    return (
        grp[cols]
        .sort_values("Total Late (Hours)", ascending=False)
        .reset_index(drop=True)
    )


def _build_break_summary(daily):
    """Per-employee break aggregates for the Break Summary sheet."""
    cols = [
        "Employee ID", "First Name", "total_break_count",
        "total_break_minutes", "avg_break_minutes",
        "incomplete_break_count",
    ]
    if "break_count" not in daily.columns:
        return pd.DataFrame(columns=cols)
    grp = (
        daily.groupby(["Employee ID", "First Name"])
        .agg(
            total_break_count=("break_count", "sum"),
            total_break_minutes=("total_break_minutes", "sum"),
            incomplete_break_count=("incomplete_break_count", "sum"),
        )
        .reset_index()
    )
    grp = grp[
        (grp["total_break_count"] > 0)
        | (grp["incomplete_break_count"] > 0)
    ].copy()
    if grp.empty:
        return pd.DataFrame(columns=cols)
    grp["total_break_count"] = grp["total_break_count"].astype(int)
    grp["total_break_minutes"] = grp["total_break_minutes"].astype(int)
    grp["incomplete_break_count"] = grp["incomplete_break_count"].astype(int)
    grp["avg_break_minutes"] = grp.apply(
        lambda r: int(r["total_break_minutes"] / r["total_break_count"])
        if r["total_break_count"] else 0,
        axis=1,
    )
    return grp[cols].sort_values(
        by="total_break_minutes", ascending=False
    ).reset_index(drop=True)


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
            total_break_count=("break_count", "sum"),
            total_break_minutes=("total_break_minutes", "sum"),
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
    per_emp["total_break_count"] = per_emp["total_break_count"].astype(int)
    per_emp["total_break_minutes"] = per_emp["total_break_minutes"].astype(int)
    per_emp["avg_break_minutes"] = per_emp.apply(
        lambda r: int(r["total_break_minutes"] / r["total_break_count"])
        if r["total_break_count"] else 0,
        axis=1,
    )
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
        "total_break_count", "total_break_minutes", "avg_break_minutes",
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
    cols = [
        "Employee ID", "First Name",
        "early_leave_cases",
        "total_early_leave_minutes", "total_early_leave_hours",
    ]
    el_rows = daily[
        (daily["early_leave_status"] == "Early Leave")
        & (~daily.get("excluded_from_late", False))
    ]
    if el_rows.empty:
        return pd.DataFrame(columns=cols)
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
    top["total_early_leave_hours"] = (top["total_early_leave_minutes"] / 60).round(1)
    return top[cols].reset_index(drop=True)


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


def filter_inputs_for_report(df, schedules_df, time_off_df, excluded_df):
    """Drop excluded employees from the raw inputs so the report sees
    none of them. Returns
        (filtered_df, filtered_schedules, filtered_time_off, hidden_count).

    The same matching rules used by `_attach_exclusion_info` apply:
    Employee ID matches by ID; rules with no ID match by normalized
    Employee Name. Schedules and time-off rows for the excluded names
    are also filtered so downstream Reconciliation / lookups do not
    show the hidden employees.

    `hidden_count` is the number of distinct Employee IDs actually
    removed from `df` -- exclusion rules pointing at people who never
    appear in the attendance file contribute zero.
    """
    if excluded_df is None or excluded_df.empty:
        return df, schedules_df, time_off_df, 0

    rules = _build_exclusion_rules(excluded_df)
    excluded_ids = set()
    excluded_names = set()
    for rule in rules:
        if rule["id"] is not None:
            excluded_ids.add(rule["id"])
        elif rule["normalized_name"]:
            excluded_names.add(rule["normalized_name"])

    if not excluded_ids and not excluded_names:
        return df, schedules_df, time_off_df, 0

    # Filter attendance df by ID and/or normalized name.
    df_mask = pd.Series(False, index=df.index)
    if excluded_ids:
        df_mask = df_mask | df["Employee ID"].isin(excluded_ids)
    if excluded_names:
        df_norm = df["First Name"].astype(str).apply(_normalize_name)
        df_mask = df_mask | df_norm.isin(excluded_names)
    filtered_df = df[~df_mask].copy()

    # Filter schedules by name (no IDs in the schedules file).
    sched_mask = pd.Series(False, index=schedules_df.index)
    if excluded_names:
        sched_norm = schedules_df["Name"].astype(str).apply(_normalize_name)
        sched_mask = sched_mask | sched_norm.isin(excluded_names)
    # Names that came with IDs may also appear in schedules; resolve
    # by walking the attendance df to find the names that go with the
    # excluded IDs, then drop those from schedules too.
    if excluded_ids:
        id_to_names = (
            df.loc[df["Employee ID"].isin(excluded_ids), "First Name"]
            .dropna()
            .astype(str)
            .apply(_normalize_name)
            .unique()
        )
        if len(id_to_names):
            sched_norm = schedules_df["Name"].astype(str).apply(_normalize_name)
            sched_mask = sched_mask | sched_norm.isin(set(id_to_names))
    filtered_schedules = schedules_df[~sched_mask].copy()

    # Filter time off by Employee name.
    if time_off_df is not None and not time_off_df.empty:
        tof_mask = pd.Series(False, index=time_off_df.index)
        all_names_to_drop = set(excluded_names)
        if excluded_ids:
            all_names_to_drop |= set(
                df.loc[df["Employee ID"].isin(excluded_ids), "First Name"]
                .dropna()
                .astype(str)
                .apply(_normalize_name)
                .unique()
            )
        if all_names_to_drop:
            tof_norm = time_off_df["Employee"].astype(str).apply(_normalize_name)
            tof_mask = tof_mask | tof_norm.isin(all_names_to_drop)
        filtered_time_off = time_off_df[~tof_mask].copy()
    else:
        filtered_time_off = time_off_df

    hidden_count = int(df.loc[df_mask, "Employee ID"].nunique())
    return filtered_df, filtered_schedules, filtered_time_off, hidden_count


_SCHEDULE_AUDIT_COLUMNS = [
    "Employee ID", "First Name", "attendance_employee_name",
    "matched_schedule_name", "matched_by",
    "working_time_raw", "shift_start", "shift_end",
    "missing_schedule", "missing_reason",
]


def _build_schedule_lookup_audit(df, schedules_df, schedule_lookup):
    """One row per (Employee ID, First Name) in attendance.

    Documents how the schedule was matched (or why it was not). The
    matched_by column makes it easy to spot fragile matches:
    "emp_code" / "exact_normalized" are reliable; "stripped_emp" or
    "substring_unique" tend to need HR follow-up.

    Side effect: warns to stdout when an employee carries an EMP code
    that exists nowhere in the resources export -- the most common
    cause of a stale Odoo extract.
    """
    label_col = _resolve_schedule_label_column(schedules_df) or "Working Time"
    if df is None or df.empty or "First Name" not in df.columns:
        return pd.DataFrame(columns=_SCHEDULE_AUDIT_COLUMNS)

    pairs = (
        df[["Employee ID", "First Name"]]
        .dropna(subset=["First Name"])
        .drop_duplicates()
        .sort_values(["First Name", "Employee ID"])
        .reset_index(drop=True)
    )

    schedule_emp_codes = set()
    if schedules_df is not None and "Name" in schedules_df.columns:
        for raw in schedules_df["Name"].dropna():
            code = _extract_emp_code(raw)
            if code:
                schedule_emp_codes.add(code)

    rows = []
    for _, r in pairs.iterrows():
        first_name = str(r["First Name"])
        attendance_name = first_name.strip()
        match = schedule_lookup.match(first_name)
        intervals = match["intervals"]
        shift_start = intervals[0][0] if intervals else None
        shift_end = intervals[-1][1] if intervals else None
        missing = not intervals
        emp_code = _extract_emp_code(first_name)

        if missing:
            if emp_code and emp_code in schedule_emp_codes:
                reason = (
                    f"EMP code {emp_code} appears in Odoo resources but "
                    f"shift label could not be parsed (check {label_col})"
                )
            elif emp_code:
                reason = (
                    f"EMP code {emp_code} absent from Odoo resources export "
                    f"-- refresh the Resources file"
                )
            else:
                reason = (
                    "Attendance name has no EMP code and no normalized "
                    "match in Odoo resources"
                )
            # Surface the warning to the log so HR / engineers spot
            # stale extracts without having to open the audit sheet.
            print(
                f"WARNING: Schedule not matched for {attendance_name} "
                f"(Employee ID {r['Employee ID']}). {reason}"
            )
        else:
            reason = ""

        rows.append({
            "Employee ID": r["Employee ID"],
            "First Name": attendance_name,
            "attendance_employee_name": attendance_name,
            "matched_schedule_name": match["matched_name"],
            "matched_by": match["matched_by"] or ("none" if missing else ""),
            "working_time_raw": match["working_time"],
            "shift_start": shift_start,
            "shift_end": shift_end,
            "missing_schedule": missing,
            "missing_reason": reason,
        })
    return pd.DataFrame(rows, columns=_SCHEDULE_AUDIT_COLUMNS)


def calculate_metrics(df, schedules_df, time_off_df=None, excluded_df=None,
                      alias_audit=None, period_start=None, period_end=None,
                      weekly_off_df=None):
    # Build the source-of-truth schedule matcher ONCE. It indexes the
    # Odoo resources by EMP code, by NBSP-collapsed normalized name,
    # and by stripped-EMP name so the BioTime "First Name" still finds
    # the right shift even when Odoo's name has spacing quirks.
    label_column = _resolve_schedule_label_column(schedules_df) or "Working Time"
    schedule_lookup = ScheduleLookup(schedules_df, label_column=label_column)
    daily = _build_daily_attendance(df, schedule_lookup)
    # Per-row intervals dict keyed by the BioTime First Name, derived
    # from the same matcher used for Shift Start so overtime / early
    # leave honour the same matching decisions.
    intervals_lookup = {
        n: schedule_lookup.match(n)["intervals"]
        for n in daily["First Name"].drop_duplicates()
    }
    schedule_lookup_audit = _build_schedule_lookup_audit(
        df, schedules_df, schedule_lookup
    )
    daily = _attach_attendance_status(daily, time_off_df)
    daily = _attach_checkout_info(daily, df)
    daily = _attach_overtime_info(daily, intervals_lookup)
    daily = _attach_department(daily, df)
    # Break info is INFORMATIONAL only -- attached before the
    # exclusion pass and never consumed by any downstream KPI.
    daily = _attach_break_info(daily, df)
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

    # Absence audit: per-(employee, date) ledger that respects weekly
    # off days, holidays, attendance, and approved time off. Built from
    # `df` (not `daily`) so the reporting calendar covers EVERY date in
    # the period -- including days where no one punched -- and the
    # employee universe includes IDs that only have non-Check-In
    # punches. The executive summary's day counts derive from this
    # ledger so the headline numbers always trace back to the audit.
    absence_details = _build_absence_details(
        daily, df, schedules_df, time_off_df, excluded_df,
        weekly_off_df=weekly_off_df,
        period_start=period_start, period_end=period_end,
    )
    absence_audit = _build_absence_audit(absence_details)
    (absence_by_id, permission_by_id,
     vacation_by_id, secondment_by_id) = _category_counts_from_audit(absence_audit)

    # Surface reconciliation breaks to the validation log so HR can
    # spot bookkeeping inconsistencies before publishing the report.
    audit_breaks = []
    if not absence_audit.empty:
        broken = absence_audit[absence_audit["reconciliation_delta"] != 0]
        for _, r in broken.iterrows():
            audit_breaks.append(
                f"Employee {r['Employee ID']} ({r['First Name']}): "
                f"scheduled={r['scheduled_working_days']} but "
                f"attended+permission+vacation+secondment+absence="
                f"{r['scheduled_working_days'] - r['reconciliation_delta']} "
                f"(delta={r['reconciliation_delta']})"
            )

    # Executive view of the Employee Summary sheet (11 named columns,
    # hours-based, independent grace thresholds). Lives alongside the
    # internal employee_summary so the Markdown / charts keep working.
    executive_employee_summary = _build_executive_employee_summary(
        daily, absence_by_id,
        permission_by_id, vacation_by_id, secondment_by_id,
    )

    # Break aggregates -- purely informational, no exclusion gating.
    break_summary = _build_break_summary(daily)
    total_break_count = int(daily["break_count"].sum())
    total_break_minutes = int(daily["total_break_minutes"].sum())
    employees_with_breaks = int(
        daily.loc[daily["break_count"] > 0, "Employee ID"].nunique()
    )
    incomplete_break_records = int(daily["incomplete_break_count"].sum())

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
        "early_leave_anomaly_cases": int(
            daily["early_leave_anomaly"].sum()
            if "early_leave_anomaly" in daily.columns else 0
        ),
        # Break analytics -- INFORMATIONAL ONLY (never affects any KPI above).
        "total_break_count": total_break_count,
        "total_break_minutes": total_break_minutes,
        "employees_with_breaks": employees_with_breaks,
        "incomplete_break_records": incomplete_break_records,
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
        "break_summary": break_summary,
        "executive_employee_summary": executive_employee_summary,
        "absence_details": absence_details,
        "absence_audit": absence_audit,
        "absence_audit_breaks": audit_breaks,
        "excluded_employees_summary": excluded_employees_summary,
        "alias_audit": (
            alias_audit if alias_audit is not None else pd.DataFrame()
        ),
        "employee_id_aliases_used": (
            int(len(alias_audit)) if alias_audit is not None
            and not alias_audit.empty else 0
        ),
        "employee_id_alias_records_mapped": (
            int(alias_audit["records_mapped"].sum())
            if alias_audit is not None and not alias_audit.empty else 0
        ),
        "excluded_employee_count": int(
            daily.loc[daily["is_excluded"], "Employee ID"].nunique()
        ),
        "schedule_lookup_audit": schedule_lookup_audit,
    }
    return summary, daily
