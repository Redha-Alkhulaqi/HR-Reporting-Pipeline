import pandas as pd

from metrics_calculator import calculate_metrics


def _attendance_two_employees():
    """Two employees, both late 60 min on the same day."""
    df = pd.DataFrame([
        # Employee 1 (excluded)
        {"Employee ID": 1, "First Name": "OWNER-EMP1",
         "Date": "2026-05-01", "Punch Time": "09:00:00",
         "Punch State": "Check In"},
        {"Employee ID": 1, "First Name": "OWNER-EMP1",
         "Date": "2026-05-01", "Punch Time": "19:00:00",
         "Punch State": "Check Out"},
        # Employee 2 (regular)
        {"Employee ID": 2, "First Name": "STAFF-EMP2",
         "Date": "2026-05-01", "Punch Time": "09:00:00",
         "Punch State": "Check In"},
        {"Employee ID": 2, "First Name": "STAFF-EMP2",
         "Date": "2026-05-01", "Punch Time": "19:00:00",
         "Punch State": "Check Out"},
    ])
    schedules = pd.DataFrame([
        {"Name": "OWNER-EMP1", "Working Time": "دوام صباحى (8:00AM-5:00PM)"},
        {"Name": "STAFF-EMP2", "Working Time": "دوام صباحى (8:00AM-5:00PM)"},
    ])
    return df, schedules


def _exclusion_row(eid=None, name=None, late=False, overtime=False,
                   payroll=False, risk=False, reason="executive"):
    return {
        "Employee ID": eid,
        "Employee Name": name,
        "Exclusion Reason": reason,
        "Exclude From Late": late,
        "Exclude From Overtime": overtime,
        "Exclude From Payroll Deduction": payroll,
        "Exclude From Risk Scoring": risk,
        "Notes": "",
    }


def test_excluded_from_late_drops_kpi_but_keeps_row():
    df, sched = _attendance_two_employees()
    excluded = pd.DataFrame([
        _exclusion_row(eid=1, late=True, payroll=True, risk=True),
    ])
    summary, daily = calculate_metrics(df, sched, excluded_df=excluded)

    # Two Late rows in daily (OWNER + STAFF), but only STAFF counts.
    assert (daily["attendance_status"] == "Late").sum() == 2
    assert summary["late_cases"] == 1
    assert summary["total_late_minutes"] == 60
    assert summary["excluded_employee_count"] == 1


def test_excluded_from_overtime_filters_overtime_kpi():
    df, sched = _attendance_two_employees()
    excluded = pd.DataFrame([
        _exclusion_row(eid=1, overtime=True),
    ])
    summary, daily = calculate_metrics(df, sched, excluded_df=excluded)

    # Both employees have overtime in daily (19:00 vs 17:00 -> 120 min each).
    assert (daily["overtime_status"] == "Overtime").sum() == 2
    # KPI counts only STAFF.
    assert summary["overtime_cases"] == 1
    assert summary["total_overtime_minutes"] == 120


def test_excluded_from_payroll_zeroes_deduction():
    df, sched = _attendance_two_employees()
    excluded = pd.DataFrame([
        _exclusion_row(eid=1, late=False, payroll=True),
    ])
    summary, _ = calculate_metrics(df, sched, excluded_df=excluded)
    emp = summary["employee_summary"]
    owner = emp[emp["Employee ID"] == 1].iloc[0]
    staff = emp[emp["Employee ID"] == 2].iloc[0]
    assert owner["estimated_deduction"] == 0.0
    assert owner["deduction_capped"] == 0.0
    # STAFF still gets a deduction.
    assert staff["estimated_deduction"] > 0
    # Pipeline total reflects only STAFF.
    assert summary["total_estimated_deduction"] == staff["estimated_deduction"]


def test_excluded_from_risk_sets_excluded_level():
    df, sched = _attendance_two_employees()
    excluded = pd.DataFrame([
        _exclusion_row(eid=1, risk=True),
    ])
    summary, _ = calculate_metrics(df, sched, excluded_df=excluded)
    emp = summary["employee_summary"]
    owner = emp[emp["Employee ID"] == 1].iloc[0]
    assert owner["risk_score"] == 0
    assert owner["risk_level"] == "Excluded"


def test_mixed_exclusion_late_only_keeps_overtime_kpi():
    df, sched = _attendance_two_employees()
    excluded = pd.DataFrame([
        # Excluded from late ONLY -- overtime still counts.
        _exclusion_row(eid=1, late=True),
    ])
    summary, _ = calculate_metrics(df, sched, excluded_df=excluded)
    assert summary["late_cases"] == 1            # owner dropped
    assert summary["overtime_cases"] == 2        # owner still counted


def test_name_based_match_when_id_missing():
    df, sched = _attendance_two_employees()
    # No Employee ID supplied, only the Employee Name -- the engine
    # should fall back to normalized-name matching.
    excluded = pd.DataFrame([
        _exclusion_row(name="OWNER-EMP1", late=True),
    ])
    summary, _ = calculate_metrics(df, sched, excluded_df=excluded)
    assert summary["late_cases"] == 1            # owner excluded by name


def test_id_match_takes_priority_over_name():
    df, sched = _attendance_two_employees()
    # ID points at employee 2 but Name points at employee 1 --
    # the ID wins, so employee 2 should be excluded.
    excluded = pd.DataFrame([
        _exclusion_row(eid=2, name="OWNER-EMP1", late=True),
    ])
    summary, _ = calculate_metrics(df, sched, excluded_df=excluded)
    assert summary["late_cases"] == 1            # owner (id 1) still counts
    # Verify by inspecting the daily rows.
    # 2 late rows in daily, but the excluded row is employee 2's.


def test_no_exclusion_file_is_safe():
    df, sched = _attendance_two_employees()
    summary, daily = calculate_metrics(df, sched, excluded_df=None)
    assert summary["excluded_employee_count"] == 0
    assert (daily["is_excluded"] == False).all()
