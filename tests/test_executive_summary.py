import pandas as pd

from metrics_calculator import calculate_metrics


EXECUTIVE_COLUMNS = [
    "Employee ID", "First Name", "No of Absence Days",
    "Total Late (Hours)", "Total Over Time (Hours)",
    "Total Early Leave (Hours)", "Break Time (Hours)",
    "Break Time (After Policy)",
]


def _schedules(name="ALI-EMP1", working="دوام صباحى (8:00AM-5:00PM)"):
    return pd.DataFrame([{"Name": name, "Working Time": working}])


def _punch(employee_id, name, date, time, state):
    return {
        "Employee ID": employee_id, "First Name": name,
        "Date": date, "Punch Time": time, "Punch State": state,
    }


def test_executive_summary_has_exact_8_columns():
    df = pd.DataFrame([
        _punch(1, "ALI-EMP1", "2026-05-01", "08:00:00", "Check In"),
        _punch(1, "ALI-EMP1", "2026-05-01", "17:00:00", "Check Out"),
    ])
    summary, _ = calculate_metrics(df, _schedules())
    exec_df = summary["executive_employee_summary"]
    assert list(exec_df.columns) == EXECUTIVE_COLUMNS


def test_late_under_grace_counts_zero_hours():
    df = pd.DataFrame([
        _punch(1, "ALI-EMP1", "2026-05-01", "08:15:00", "Check In"),  # 15 min
        _punch(1, "ALI-EMP1", "2026-05-01", "17:00:00", "Check Out"),
    ])
    summary, _ = calculate_metrics(df, _schedules())
    row = summary["executive_employee_summary"].iloc[0]
    assert row["Total Late (Hours)"] == 0.0


def test_late_above_grace_counts_full_minutes_in_hours():
    df = pd.DataFrame([
        _punch(1, "ALI-EMP1", "2026-05-01", "08:16:00", "Check In"),  # 16 min
        _punch(1, "ALI-EMP1", "2026-05-01", "17:00:00", "Check Out"),
    ])
    summary, _ = calculate_metrics(df, _schedules())
    row = summary["executive_employee_summary"].iloc[0]
    # 16 / 60 = 0.266... rounds to 0.3.
    assert row["Total Late (Hours)"] == 0.3


def test_early_leave_under_5min_grace_counts_zero():
    df = pd.DataFrame([
        _punch(1, "ALI-EMP1", "2026-05-01", "08:00:00", "Check In"),
        _punch(1, "ALI-EMP1", "2026-05-01", "16:56:00", "Check Out"),  # 4 min
    ])
    summary, _ = calculate_metrics(df, _schedules())
    row = summary["executive_employee_summary"].iloc[0]
    assert row["Total Early Leave (Hours)"] == 0.0


def test_early_leave_above_5min_grace_counts_full():
    df = pd.DataFrame([
        _punch(1, "ALI-EMP1", "2026-05-01", "08:00:00", "Check In"),
        _punch(1, "ALI-EMP1", "2026-05-01", "16:54:00", "Check Out"),  # 6 min
    ])
    summary, _ = calculate_metrics(df, _schedules())
    row = summary["executive_employee_summary"].iloc[0]
    # 6 / 60 = 0.1.
    assert row["Total Early Leave (Hours)"] == 0.1


def test_break_after_policy_ignores_first_60_minutes():
    # 90 min of break -> policy counts only 30 min beyond the first hour.
    df = pd.DataFrame([
        _punch(1, "ALI-EMP1", "2026-05-01", "08:00:00", "Check In"),
        _punch(1, "ALI-EMP1", "2026-05-01", "12:00:00", "Break Out"),
        _punch(1, "ALI-EMP1", "2026-05-01", "13:30:00", "Break In"),
        _punch(1, "ALI-EMP1", "2026-05-01", "17:00:00", "Check Out"),
    ])
    summary, _ = calculate_metrics(df, _schedules())
    row = summary["executive_employee_summary"].iloc[0]
    assert row["Break Time (Hours)"] == 1.5   # 90 / 60
    assert row["Break Time (After Policy)"] == 0.5  # 30 / 60


def test_break_under_60_minutes_has_zero_after_policy():
    df = pd.DataFrame([
        _punch(1, "ALI-EMP1", "2026-05-01", "08:00:00", "Check In"),
        _punch(1, "ALI-EMP1", "2026-05-01", "12:00:00", "Break Out"),
        _punch(1, "ALI-EMP1", "2026-05-01", "12:30:00", "Break In"),
        _punch(1, "ALI-EMP1", "2026-05-01", "17:00:00", "Check Out"),
    ])
    summary, _ = calculate_metrics(df, _schedules())
    row = summary["executive_employee_summary"].iloc[0]
    assert row["Break Time (Hours)"] == 0.5
    assert row["Break Time (After Policy)"] == 0.0


def test_absence_days_counts_working_days_employee_missed():
    # Two employees over three "working" days. ALI checks in only
    # on day 1; ZAIN works all three. Absence for ALI = 2.
    df = pd.DataFrame([
        _punch(1, "ALI-EMP1", "2026-05-01", "08:00:00", "Check In"),
        _punch(1, "ALI-EMP1", "2026-05-01", "17:00:00", "Check Out"),
        _punch(2, "ZAIN-EMP2", "2026-05-01", "08:00:00", "Check In"),
        _punch(2, "ZAIN-EMP2", "2026-05-01", "17:00:00", "Check Out"),
        _punch(2, "ZAIN-EMP2", "2026-05-02", "08:00:00", "Check In"),
        _punch(2, "ZAIN-EMP2", "2026-05-02", "17:00:00", "Check Out"),
        _punch(2, "ZAIN-EMP2", "2026-05-03", "08:00:00", "Check In"),
        _punch(2, "ZAIN-EMP2", "2026-05-03", "17:00:00", "Check Out"),
    ])
    schedules = pd.DataFrame([
        {"Name": "ALI-EMP1", "Working Time": "دوام صباحى (8:00AM-5:00PM)"},
        {"Name": "ZAIN-EMP2", "Working Time": "دوام صباحى (8:00AM-5:00PM)"},
    ])
    summary, _ = calculate_metrics(df, schedules)
    exec_df = summary["executive_employee_summary"]
    ali = exec_df[exec_df["Employee ID"] == 1].iloc[0]
    zain = exec_df[exec_df["Employee ID"] == 2].iloc[0]
    assert ali["No of Absence Days"] == 2
    assert zain["No of Absence Days"] == 0


def test_friday_off_days_are_not_counted_as_absence():
    """ALI works Sat-Thu; Fridays are weekly off. Even though a
    colleague checks in on the Fridays (so Fridays appear in the
    reporting period), ALI's missing Fridays must NOT count as
    absences."""
    rows = []
    # ALI works every Sat-Thu between 2026-05-02 and 2026-05-14.
    ali_dates = [
        "2026-05-02", "2026-05-03", "2026-05-04", "2026-05-05",
        "2026-05-06", "2026-05-07",
        "2026-05-09", "2026-05-10", "2026-05-11", "2026-05-12",
        "2026-05-13", "2026-05-14",
    ]
    for d in ali_dates:
        rows.append(_punch(1, "ALI-EMP1", d, "08:00:00", "Check In"))
        rows.append(_punch(1, "ALI-EMP1", d, "17:00:00", "Check Out"))
    # ZAIN works the two Fridays so they enter the reporting period.
    for d in ("2026-05-01", "2026-05-08"):
        rows.append(_punch(2, "ZAIN-EMP2", d, "08:00:00", "Check In"))
        rows.append(_punch(2, "ZAIN-EMP2", d, "17:00:00", "Check Out"))
    df = pd.DataFrame(rows)
    schedules = pd.DataFrame([
        {"Name": "ALI-EMP1", "Working Time": "دوام صباحى (8:00AM-5:00PM)"},
        {"Name": "ZAIN-EMP2", "Working Time": "دوام صباحى (8:00AM-5:00PM)"},
    ])
    summary, _ = calculate_metrics(df, schedules)
    exec_df = summary["executive_employee_summary"]
    ali = exec_df[exec_df["Employee ID"] == 1].iloc[0]
    assert ali["No of Absence Days"] == 0

    # And the audit ledger explains it.
    details = summary["absence_details"]
    ali_fridays = details[
        (details["Employee ID"] == 1)
        & (details["Weekday"] == "Friday")
    ]
    assert (ali_fridays["Counted As Absence"] == False).all()  # noqa: E712
    assert (ali_fridays["Is Weekly Off"] == True).all()  # noqa: E712


def test_approved_leave_does_not_count_as_absence():
    df = pd.DataFrame([
        _punch(1, "ALI-EMP1", "2026-05-01", "08:00:00", "Check In"),
        _punch(1, "ALI-EMP1", "2026-05-01", "17:00:00", "Check Out"),
        # Day 2 a colleague worked (so the day is "working") but
        # ALI was on Annual Leave -> NOT counted as absence.
        _punch(2, "ZAIN-EMP2", "2026-05-02", "08:00:00", "Check In"),
        _punch(2, "ZAIN-EMP2", "2026-05-02", "17:00:00", "Check Out"),
    ])
    schedules = pd.DataFrame([
        {"Name": "ALI-EMP1", "Working Time": "دوام صباحى (8:00AM-5:00PM)"},
        {"Name": "ZAIN-EMP2", "Working Time": "دوام صباحى (8:00AM-5:00PM)"},
    ])
    time_off = pd.DataFrame([{
        "Employee": "ALI-EMP1",
        "Status": "Approved",
        "Start Date": "2026-05-02 00:00:00",
        "End Date": "2026-05-02 23:59:00",
        "Time Off Type": "Annual Leave",
    }])
    summary, _ = calculate_metrics(df, schedules, time_off)
    exec_df = summary["executive_employee_summary"]
    ali = exec_df[exec_df["Employee ID"] == 1].iloc[0]
    assert ali["No of Absence Days"] == 0
