import pandas as pd

from metrics_calculator import calculate_metrics, filter_inputs_for_report


def _attendance_rows(eid, name, date="2026-05-04"):
    return [
        {"Employee ID": eid, "First Name": name,
         "Date": date, "Punch Time": "08:00:00",
         "Punch State": "Check In"},
        {"Employee ID": eid, "First Name": name,
         "Date": date, "Punch Time": "17:00:00",
         "Punch State": "Check Out"},
    ]


def _two_employee_inputs():
    """ALI (id=1) is the regular employee; ZAIN (id=2) will be excluded."""
    df = pd.DataFrame(
        _attendance_rows(1, "ALI-EMP1") + _attendance_rows(2, "ZAIN-EMP2")
    )
    schedules = pd.DataFrame([
        {"Name": "ALI-EMP1", "Working Time": "دوام صباحى (8:00AM-5:00PM)"},
        {"Name": "ZAIN-EMP2", "Working Time": "دوام صباحى (8:00AM-5:00PM)"},
    ])
    return df, schedules


def _excluded_by_id(eid, name=None):
    return pd.DataFrame([{
        "Employee ID": eid,
        "Employee Name": name,
        "Exclusion Reason": "Owner",
        "Exclude From Late": "TRUE",
        "Exclude From Overtime": "TRUE",
        "Exclude From Payroll Deduction": "TRUE",
        "Exclude From Risk Scoring": "TRUE",
        "Notes": "",
    }])


def test_filter_drops_excluded_by_id():
    df, schedules = _two_employee_inputs()
    excluded = _excluded_by_id(2)
    r_df, r_sched, r_tof, hidden = filter_inputs_for_report(
        df, schedules, None, excluded
    )
    assert hidden == 1
    assert 2 not in set(r_df["Employee ID"].unique())
    assert 1 in set(r_df["Employee ID"].unique())
    # Schedules also lose the excluded name.
    assert "ZAIN-EMP2" not in set(r_sched["Name"].unique())


def test_filter_drops_excluded_by_name_only():
    df, schedules = _two_employee_inputs()
    # Exclusion has NO ID -- engine must match by name.
    excluded = pd.DataFrame([{
        "Employee ID": None,
        "Employee Name": "ZAIN-EMP2",
        "Exclusion Reason": "name-only rule",
        "Exclude From Late": "TRUE",
        "Exclude From Overtime": "TRUE",
        "Exclude From Payroll Deduction": "TRUE",
        "Exclude From Risk Scoring": "TRUE",
        "Notes": "",
    }])
    r_df, _, _, hidden = filter_inputs_for_report(df, schedules, None, excluded)
    assert hidden == 1
    assert 2 not in set(r_df["Employee ID"].unique())


def test_no_excluded_employees_anywhere_in_report_summary():
    """End-to-end: after filter + recompute, every report-facing
    DataFrame must contain no trace of the excluded employee."""
    df, schedules = _two_employee_inputs()
    excluded = _excluded_by_id(2)

    r_df, r_sched, r_tof, hidden = filter_inputs_for_report(
        df, schedules, None, excluded
    )
    report_summary, report_daily = calculate_metrics(
        r_df, r_sched, r_tof, excluded_df=None
    )

    assert hidden == 1
    excluded_id = 2

    # daily
    assert excluded_id not in set(report_daily["Employee ID"].unique())

    # KPIs reflect only the remaining employee.
    assert report_summary["reporting_population"] == 1

    # Executive summary -- the headline sheet.
    exec_df = report_summary["executive_employee_summary"]
    assert excluded_id not in set(exec_df["Employee ID"].unique())

    # Every report-facing DataFrame -- spot-check the IDs.
    for key in (
        "employee_summary",
        "employee_master",
        "employee_reconciliation_details",
        "absence_details",
    ):
        sub_df = report_summary.get(key)
        if sub_df is not None and not sub_df.empty and "Employee ID" in sub_df.columns:
            assert excluded_id not in set(sub_df["Employee ID"].unique()), (
                f"excluded employee leaked into {key}"
            )

    # The Excluded Employees section/sheet disappears because no
    # exclusion file is passed to the recompute step.
    excluded_summary = report_summary.get("excluded_employees_summary")
    assert excluded_summary is None or excluded_summary.empty


def test_empty_exclusion_file_is_noop():
    df, schedules = _two_employee_inputs()
    r_df, r_sched, r_tof, hidden = filter_inputs_for_report(
        df, schedules, None, excluded_df=None
    )
    assert hidden == 0
    assert r_df is df  # identity-equal, not a fresh copy.
