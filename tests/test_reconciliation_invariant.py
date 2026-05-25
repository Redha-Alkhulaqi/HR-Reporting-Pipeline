"""Reconciliation invariant must hold for every non-excluded employee:

    scheduled_working_days
      == attended_days + permission_days + vacation_days
         + secondment_days + absence_days

These tests pin the math regardless of attendance shape. The bug that
this file was added to catch was: a day with `Has Attendance=True`
AND an approved permission (e.g. "leave-early" excuse, "استأذان")
zeroed both Attended and Absence Day Values, while the audit's
permission bucket required `~Has Attendance` -- so the day vanished
from every bucket and the per-employee sum came up short by 1.

The fix: when the employee actually attended, the day is counted as
ATTENDED. The permission is reflected in the Late / Early Leave
excuse subtraction (in minutes), not as a duplicate full-day count.
"""
import pandas as pd

from metrics_calculator import calculate_metrics


def _punch(eid, name, date, time, state):
    return {
        "Employee ID": eid, "First Name": name,
        "Date": date, "Punch Time": time, "Punch State": state,
    }


def _full_day(eid, name, date, ci="08:00:00", co="17:00:00"):
    return [
        _punch(eid, name, date, ci, "Check In"),
        _punch(eid, name, date, co, "Check Out"),
    ]


# A standard schedule + a contiguous Sat-Wed reporting window so the
# invariant tests pin a known scheduled_working_days = 5.
SCHEDULE = pd.DataFrame([{
    "Name": "ALI-EMP1",
    "Working Time": "دوام صباحى (8:00AM-5:00PM)",
}])
PERIOD_START, PERIOD_END = "2026-05-02", "2026-05-06"  # Sat..Wed (5 days)


def _audit_row(summary, eid):
    audit = summary["absence_audit"]
    matches = audit[audit["Employee ID"] == eid]
    assert not matches.empty, f"no audit row for {eid}"
    return matches.iloc[0]


def _assert_balances(summary):
    audit = summary["absence_audit"]
    assert (audit["reconciliation_delta"] == 0).all(), \
        f"reconciliation breaks: {audit[audit['reconciliation_delta'] != 0].to_dict()}"
    assert summary["absence_audit_breaks"] == [], \
        f"breaks logged: {summary['absence_audit_breaks']}"


# ---- Scenario 1: pure absence (no excuse) -------------------------------

def test_employee_with_pure_absence_balances():
    """Single absence with no excuse -> absence bucket gets the day."""
    rows = []
    rows.extend(_full_day(1, "ALI-EMP1", "2026-05-02"))
    rows.extend(_full_day(1, "ALI-EMP1", "2026-05-03"))
    # Mon 2026-05-04 ABSENT, no excuse.
    rows.extend(_full_day(1, "ALI-EMP1", "2026-05-05"))
    rows.extend(_full_day(1, "ALI-EMP1", "2026-05-06"))
    summary, _ = calculate_metrics(
        pd.DataFrame(rows), SCHEDULE,
        period_start=PERIOD_START, period_end=PERIOD_END,
    )
    row = _audit_row(summary, 1)
    assert row["scheduled_working_days"] == 5
    assert row["attended_days"] == 4.0
    assert row["absence_days"] == 1.0
    assert row["permission_days"] == 0
    _assert_balances(summary)


# ---- Scenario 2: permission day (no attendance) -------------------------

def test_employee_with_full_day_permission_balances():
    """Approved permission with NO check-in -> permission bucket."""
    rows = []
    rows.extend(_full_day(1, "ALI-EMP1", "2026-05-02"))
    rows.extend(_full_day(1, "ALI-EMP1", "2026-05-03"))
    # Mon 2026-05-04: permission, no attendance.
    rows.extend(_full_day(1, "ALI-EMP1", "2026-05-05"))
    rows.extend(_full_day(1, "ALI-EMP1", "2026-05-06"))
    time_off = pd.DataFrame([{
        "Employee": "ALI-EMP1",
        "Time Off Type": "استأذان",
        "Start Date": "2026-05-04 00:00:00",
        "End Date":   "2026-05-04 23:59:59",
        "Status": "Approved",
    }])
    summary, _ = calculate_metrics(
        pd.DataFrame(rows), SCHEDULE, time_off,
        period_start=PERIOD_START, period_end=PERIOD_END,
    )
    row = _audit_row(summary, 1)
    assert row["scheduled_working_days"] == 5
    assert row["attended_days"] == 4.0
    assert row["permission_days"] == 1
    assert row["absence_days"] == 0.0
    _assert_balances(summary)


# ---- Scenario 3: ATTENDED + approved permission (regression) ------------

def test_attended_plus_approved_permission_counts_as_attended_not_lost():
    """Regression: previously zeroed both attended and permission for
    the day, leaving a 1.0 reconciliation gap. With the fix, the day
    is counted as ATTENDED (the permission is shown in the Absence
    Reason text and excused-minute KPI, but does NOT also count as a
    full permission day, which would double-count a single calendar
    day)."""
    rows = []
    rows.extend(_full_day(1, "ALI-EMP1", "2026-05-02"))
    rows.extend(_full_day(1, "ALI-EMP1", "2026-05-03"))
    # Mon 2026-05-04: ATTENDED + approved permission (e.g. permission
    # to leave early -> employee still came in for the morning).
    rows.extend(_full_day(1, "ALI-EMP1", "2026-05-04"))
    rows.extend(_full_day(1, "ALI-EMP1", "2026-05-05"))
    rows.extend(_full_day(1, "ALI-EMP1", "2026-05-06"))
    time_off = pd.DataFrame([{
        "Employee": "ALI-EMP1",
        "Time Off Type": "استأذان",
        "Start Date": "2026-05-04 13:00:00",
        "End Date":   "2026-05-04 17:00:00",
        "Status": "Approved",
    }])
    summary, _ = calculate_metrics(
        pd.DataFrame(rows), SCHEDULE, time_off,
        period_start=PERIOD_START, period_end=PERIOD_END,
    )
    row = _audit_row(summary, 1)
    assert row["scheduled_working_days"] == 5
    # The attended + permission day counts ONLY as attended.
    assert row["attended_days"] == 5.0
    assert row["permission_days"] == 0
    assert row["absence_days"] == 0.0
    _assert_balances(summary)
    # Reason text still surfaces the permission for HR audit.
    details = summary["absence_details"]
    target = details[(details["Employee ID"] == 1)
                     & (details["Date"] == "2026-05-04")].iloc[0]
    assert "Attended with approved time off" in target["Absence Reason"]


# ---- Scenario 4: vacation day -------------------------------------------

def test_employee_with_vacation_day_balances():
    rows = []
    rows.extend(_full_day(1, "ALI-EMP1", "2026-05-02"))
    rows.extend(_full_day(1, "ALI-EMP1", "2026-05-03"))
    # Mon 2026-05-04: Annual Leave, no attendance.
    rows.extend(_full_day(1, "ALI-EMP1", "2026-05-05"))
    rows.extend(_full_day(1, "ALI-EMP1", "2026-05-06"))
    time_off = pd.DataFrame([{
        "Employee": "ALI-EMP1",
        "Time Off Type": "Annual Leave",
        "Start Date": "2026-05-04 00:00:00",
        "End Date":   "2026-05-04 23:59:59",
        "Status": "Approved",
    }])
    summary, _ = calculate_metrics(
        pd.DataFrame(rows), SCHEDULE, time_off,
        period_start=PERIOD_START, period_end=PERIOD_END,
    )
    row = _audit_row(summary, 1)
    assert row["scheduled_working_days"] == 5
    assert row["attended_days"] == 4.0
    assert row["vacation_days"] == 1
    assert row["absence_days"] == 0.0
    _assert_balances(summary)


# ---- Scenario 5: secondment day -----------------------------------------

def test_employee_with_secondment_day_balances():
    rows = []
    rows.extend(_full_day(1, "ALI-EMP1", "2026-05-02"))
    rows.extend(_full_day(1, "ALI-EMP1", "2026-05-03"))
    # Mon 2026-05-04: Secondment, no attendance.
    rows.extend(_full_day(1, "ALI-EMP1", "2026-05-05"))
    rows.extend(_full_day(1, "ALI-EMP1", "2026-05-06"))
    time_off = pd.DataFrame([{
        "Employee": "ALI-EMP1",
        "Time Off Type": "Secondment",
        "Start Date": "2026-05-04 00:00:00",
        "End Date":   "2026-05-04 23:59:59",
        "Status": "Approved",
    }])
    summary, _ = calculate_metrics(
        pd.DataFrame(rows), SCHEDULE, time_off,
        period_start=PERIOD_START, period_end=PERIOD_END,
    )
    row = _audit_row(summary, 1)
    assert row["scheduled_working_days"] == 5
    assert row["attended_days"] == 4.0
    assert row["secondment_days"] == 1
    assert row["absence_days"] == 0.0
    _assert_balances(summary)


# ---- Scenario 6: Friday Compensation ------------------------------------

def test_employee_with_friday_compensation_balances():
    """Worked-Friday cancels one weekday absence. Both the reduced
    absence AND the per-employee reconciliation must still balance."""
    rows = []
    rows.extend(_full_day(1, "ALI-EMP1", "2026-05-02"))   # Sat worked
    rows.extend(_full_day(1, "ALI-EMP1", "2026-05-03"))   # Sun worked
    # Mon 2026-05-04 ABSENT (no punches, no time off)
    rows.extend(_full_day(1, "ALI-EMP1", "2026-05-05"))   # Tue worked
    rows.extend(_full_day(1, "ALI-EMP1", "2026-05-06"))   # Wed worked
    rows.extend(_full_day(1, "ALI-EMP1", "2026-05-07"))   # Thu worked
    rows.extend(_full_day(1, "ALI-EMP1", "2026-05-08"))   # Fri WORKED
    summary, _ = calculate_metrics(
        pd.DataFrame(rows), SCHEDULE,
        period_start="2026-05-02", period_end="2026-05-08",
    )
    row = _audit_row(summary, 1)
    # Sat..Thu = 6 scheduled days (Fri is weekly off).
    assert row["scheduled_working_days"] == 6
    # Mon absence offset by Fri compensation -> attended takes its place.
    assert row["attended_days"] == 6.0
    assert row["absence_days"] == 0.0
    assert int(row["friday_compensation_days"]) == 1
    _assert_balances(summary)


# ---- Scenario 7: weekly-off override ------------------------------------

def test_employee_with_weekly_off_override_balances():
    """Custom rotation (Mon+Tue off) -- the 2 rotation days are NOT
    scheduled, and the reconciliation must still balance."""
    rows = []
    # Sat,Sun,Wed worked; Mon,Tue are weekly-off-override days.
    for d in ("2026-05-02", "2026-05-03", "2026-05-06"):
        rows.extend(_full_day(1, "ALI-EMP1", d))
    weekly_off = pd.DataFrame([{
        "Employee ID": 1, "Employee Name": "ALI-EMP1",
        "Weekly Off Days": "Monday,Tuesday,Friday", "Notes": "",
    }])
    summary, _ = calculate_metrics(
        pd.DataFrame(rows), SCHEDULE, weekly_off_df=weekly_off,
        period_start=PERIOD_START, period_end=PERIOD_END,
    )
    row = _audit_row(summary, 1)
    # 5 days minus 2 override off days = 3 scheduled.
    assert row["scheduled_working_days"] == 3
    assert row["attended_days"] == 3.0
    assert row["absence_days"] == 0.0
    assert "Monday,Tuesday" in row["weekly_off_days"]
    _assert_balances(summary)


# ---- Scenario 8: reconciliation delta is zero (combined) ----------------

def test_combined_scenario_delta_is_zero():
    """Mix of every kind of day in one employee, one period: attended,
    pure absence, full-day permission, vacation, secondment, AND
    attended-with-permission. The audit row's delta must be 0."""
    rows = []
    # Sat 05-02 attended; Sun 05-03 ABSENT (no punch, no excuse).
    rows.extend(_full_day(1, "ALI-EMP1", "2026-05-02"))
    # Mon 05-04 permission (no punch).
    # Tue 05-05 vacation (no punch).
    # Wed 05-06 secondment (no punch).
    # Thu 05-07 ATTENDED + partial-day permission.
    rows.extend(_full_day(1, "ALI-EMP1", "2026-05-07"))
    time_off = pd.DataFrame([
        {"Employee": "ALI-EMP1", "Time Off Type": "استأذان",
         "Start Date": "2026-05-04 00:00:00",
         "End Date":   "2026-05-04 23:59:59", "Status": "Approved"},
        {"Employee": "ALI-EMP1", "Time Off Type": "Annual Leave",
         "Start Date": "2026-05-05 00:00:00",
         "End Date":   "2026-05-05 23:59:59", "Status": "Approved"},
        {"Employee": "ALI-EMP1", "Time Off Type": "Secondment",
         "Start Date": "2026-05-06 00:00:00",
         "End Date":   "2026-05-06 23:59:59", "Status": "Approved"},
        {"Employee": "ALI-EMP1", "Time Off Type": "استأذان",
         "Start Date": "2026-05-07 14:00:00",
         "End Date":   "2026-05-07 17:00:00", "Status": "Approved"},
    ])
    summary, _ = calculate_metrics(
        pd.DataFrame(rows), SCHEDULE, time_off,
        period_start="2026-05-02", period_end="2026-05-07",
    )
    row = _audit_row(summary, 1)
    # Sat,Sun,Mon,Tue,Wed,Thu = 6 scheduled working days.
    assert row["scheduled_working_days"] == 6
    # Attended: Sat + Thu (Thu's afternoon-permission does NOT reduce
    # the day-count -- the day is attended, the permission is logged
    # as the reason and excuses related lates / early-leaves).
    assert row["attended_days"] == 2.0
    assert row["absence_days"] == 1.0       # Sun
    assert row["permission_days"] == 1      # Mon
    assert row["vacation_days"] == 1        # Tue
    assert row["secondment_days"] == 1      # Wed
    # Equation: 2 + 1 + 1 + 1 + 1 = 6 ✓
    assert row["reconciliation_delta"] == 0
    _assert_balances(summary)
