import pandas as pd

from metrics_calculator import calculate_metrics


EXECUTIVE_COLUMNS = [
    "Employee ID", "First Name",
    "No of Absence Days", "No of Permission Days",
    "No of Vacation Days", "No of Secondment Days",
    "Total Late (Hours)",
    "Total Over Time (Hours) (Actual)",
    "Total Over Time (Payable 1.5x) (Hours)",
    "Total Early Leave (Hours)",
    "Break Time (Hours)", "Break Time (After Policy)",
]


def _schedules(name="ALI-EMP1", working="دوام صباحى (8:00AM-5:00PM)"):
    return pd.DataFrame([{"Name": name, "Working Time": working}])


def _punch(employee_id, name, date, time, state):
    return {
        "Employee ID": employee_id, "First Name": name,
        "Date": date, "Punch Time": time, "Punch State": state,
    }


def test_executive_summary_has_exact_12_columns():
    df = pd.DataFrame([
        _punch(1, "ALI-EMP1", "2026-05-01", "08:00:00", "Check In"),
        _punch(1, "ALI-EMP1", "2026-05-01", "17:00:00", "Check Out"),
    ])
    summary, _ = calculate_metrics(df, _schedules())
    exec_df = summary["executive_employee_summary"]
    assert list(exec_df.columns) == EXECUTIVE_COLUMNS


def test_payable_overtime_column_uses_1_5x_actual():
    """Mockup examples for the new payable column. All hour values are
    rounded to 1 decimal place ("All hour values are rounded to 1
    decimal place using standard rounding"):

      Actual 10.5h -> Payable 15.8h
      Actual  4.2h -> Payable  6.3h
      Actual 39.4h -> Payable 59.1h

    Each scenario splits the target minutes across N days with a
    constant per-day overtime chunk above MIN_OVERTIME_MINUTES so the
    per-day classifier always fires.
    """
    cases = [
        # (total_overtime_min, per_day_overtime_min, expected_actual_h, expected_payable_h)
        (630,  210, 10.5, 15.8),   # 3 days * 3h30 -> 15.75 -> 15.8 (1 dp)
        (252,  126,  4.2,  6.3),   # 2 days * 2h06 ->  6.30 ->  6.3 (1 dp)
        (2364, 197, 39.4, 59.1),   # 12 days * 3h17 -> 59.10 -> 59.1 (1 dp)
    ]
    for total_min, per_day_min, expected_actual, expected_payable in cases:
        assert per_day_min >= 30, "per-day OT must clear MIN_OVERTIME_MINUTES"
        n_days = total_min // per_day_min
        assert n_days * per_day_min == total_min, "case must be exact"

        rows = []
        for d in range(1, n_days + 1):
            date = f"2026-05-{d:02d}"
            check_out_total = 17 * 60 + per_day_min     # 17:00 + per-day OT
            h, m = divmod(check_out_total, 60)
            rows.append(_punch(1, "ALI-EMP1", date, "08:00:00", "Check In"))
            rows.append(_punch(
                1, "ALI-EMP1", date,
                f"{h:02d}:{m:02d}:00", "Check Out",
            ))
        summary, _ = calculate_metrics(pd.DataFrame(rows), _schedules())
        row = summary["executive_employee_summary"].iloc[0]
        assert row["Total Over Time (Hours) (Actual)"] == expected_actual, (
            f"actual mismatch for {total_min} min: "
            f"got {row['Total Over Time (Hours) (Actual)']}"
        )
        assert row["Total Over Time (Payable 1.5x) (Hours)"] == expected_payable, (
            f"payable mismatch for {total_min} min: "
            f"got {row['Total Over Time (Payable 1.5x) (Hours)']}"
        )


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


def test_total_late_excludes_approved_excuse_minutes():
    """Regression: Total Late (Hours) must use UNEXCUSED delay, NOT
    raw delay. An approved-permission day where HR has signed off
    on the late arrival must contribute 0 hours to Total Late.

    Setup: employee scheduled 08:00, arrives 09:30 (90 min raw
    delay). HR has an approved 90-minute permission (استئذان)
    covering 08:00 - 09:30. Daily classifier marks the row as
    Approved Excuse, unexcused_delay_minutes=0. Executive summary
    must respect that.
    """
    df = pd.DataFrame([
        _punch(1, "ALI-EMP1", "2026-05-04", "09:30:00", "Check In"),
        _punch(1, "ALI-EMP1", "2026-05-04", "17:00:00", "Check Out"),
    ])
    time_off = pd.DataFrame([{
        "Employee": "ALI-EMP1",
        "Time Off Type": "استأذان -",
        "Start Date": "2026-05-04 08:00:00",
        "End Date":   "2026-05-04 09:30:00",
        "Status": "Approved",
    }])
    summary, daily = calculate_metrics(df, _schedules(), time_off)
    # Sanity: the daily row is Approved Excuse, not Late.
    row_daily = daily.iloc[0]
    assert row_daily["attendance_status"] == "Approved Excuse"
    assert row_daily["unexcused_delay_minutes"] == 0
    # Executive Summary Total Late MUST be 0 -- the day is excused.
    row_exec = summary["executive_employee_summary"].iloc[0]
    assert row_exec["Total Late (Hours)"] == 0.0


def test_total_late_uses_unexcused_remainder_when_partial_excuse():
    """If an approved permission covers only PART of the delay, the
    executive Total Late must count the unexcused remainder (with
    the 15-min grace applied to the remainder, not the raw delay)."""
    # 60 min raw delay (08:00 shift, 09:00 check-in). HR approves
    # a 40-min permission covering 08:00-08:40. Unexcused = 20 min.
    df = pd.DataFrame([
        _punch(1, "ALI-EMP1", "2026-05-04", "09:00:00", "Check In"),
        _punch(1, "ALI-EMP1", "2026-05-04", "17:00:00", "Check Out"),
    ])
    time_off = pd.DataFrame([{
        "Employee": "ALI-EMP1",
        "Time Off Type": "استأذان -",
        "Start Date": "2026-05-04 08:00:00",
        "End Date":   "2026-05-04 08:40:00",
        "Status": "Approved",
    }])
    summary, daily = calculate_metrics(df, _schedules(), time_off)
    row_daily = daily.iloc[0]
    assert row_daily["unexcused_delay_minutes"] == 20
    # 20 unexcused > 15 grace -> count full 20 min. 20/60 ~= 0.33 -> 0.3 hrs.
    row_exec = summary["executive_employee_summary"].iloc[0]
    assert row_exec["Total Late (Hours)"] == 0.3


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
    # Annual Leave classifies as a Vacation Day (not Permission, not Secondment).
    assert ali["No of Vacation Days"] == 1
    assert ali["No of Permission Days"] == 0
    assert ali["No of Secondment Days"] == 0


def test_permission_vacation_and_secondment_day_counts():
    """Each approved time-off type is bucketed correctly:
       - استأذان / "Permission" -> No of Permission Days
       - Annual Leave / Sick Leave -> No of Vacation Days
       - Secondment / انتداب -> No of Secondment Days
    """
    df = pd.DataFrame([
        # ALI checks in on three other days so he is in the reporting
        # population. The time-off-only days fill the buckets below.
        _punch(1, "ALI-EMP1", "2026-05-01", "08:00:00", "Check In"),
        _punch(1, "ALI-EMP1", "2026-05-01", "17:00:00", "Check Out"),
        _punch(1, "ALI-EMP1", "2026-05-02", "08:00:00", "Check In"),
        _punch(1, "ALI-EMP1", "2026-05-02", "17:00:00", "Check Out"),
        _punch(1, "ALI-EMP1", "2026-05-03", "08:00:00", "Check In"),
        _punch(1, "ALI-EMP1", "2026-05-03", "17:00:00", "Check Out"),
        _punch(1, "ALI-EMP1", "2026-05-06", "08:00:00", "Check In"),
        _punch(1, "ALI-EMP1", "2026-05-06", "17:00:00", "Check Out"),
        # Colleague punches on the time-off-only dates so they enter the
        # reporting period.
        _punch(2, "ZAIN-EMP2", "2026-05-04", "08:00:00", "Check In"),
        _punch(2, "ZAIN-EMP2", "2026-05-04", "17:00:00", "Check Out"),
        _punch(2, "ZAIN-EMP2", "2026-05-05", "08:00:00", "Check In"),
        _punch(2, "ZAIN-EMP2", "2026-05-05", "17:00:00", "Check Out"),
        _punch(2, "ZAIN-EMP2", "2026-05-07", "08:00:00", "Check In"),
        _punch(2, "ZAIN-EMP2", "2026-05-07", "17:00:00", "Check Out"),
    ])
    schedules = pd.DataFrame([
        {"Name": "ALI-EMP1", "Working Time": "دوام صباحى (8:00AM-5:00PM)"},
        {"Name": "ZAIN-EMP2", "Working Time": "دوام صباحى (8:00AM-5:00PM)"},
    ])
    time_off = pd.DataFrame([
        # Permission (excuse).
        {"Employee": "ALI-EMP1", "Status": "Approved",
         "Start Date": "2026-05-04 08:00:00",
         "End Date": "2026-05-04 10:00:00",
         "Time Off Type": "استأذان"},
        # Vacation (Annual Leave) -- two days.
        {"Employee": "ALI-EMP1", "Status": "Approved",
         "Start Date": "2026-05-05 00:00:00",
         "End Date": "2026-05-05 23:59:00",
         "Time Off Type": "Annual Leave"},
        {"Employee": "ALI-EMP1", "Status": "Approved",
         "Start Date": "2026-05-07 00:00:00",
         "End Date": "2026-05-07 23:59:00",
         "Time Off Type": "Sick Leave"},
        # Secondment (English).
        {"Employee": "ZAIN-EMP2", "Status": "Approved",
         "Start Date": "2026-05-01 00:00:00",
         "End Date": "2026-05-02 23:59:00",
         "Time Off Type": "Secondment"},
        # Secondment (Arabic).
        {"Employee": "ZAIN-EMP2", "Status": "Approved",
         "Start Date": "2026-05-03 00:00:00",
         "End Date": "2026-05-03 23:59:00",
         "Time Off Type": "انتداب"},
    ])
    summary, _ = calculate_metrics(df, schedules, time_off)
    exec_df = summary["executive_employee_summary"]

    ali = exec_df[exec_df["Employee ID"] == 1].iloc[0]
    assert ali["No of Permission Days"] == 1
    assert ali["No of Vacation Days"] == 2
    assert ali["No of Secondment Days"] == 0

    zain = exec_df[exec_df["Employee ID"] == 2].iloc[0]
    assert zain["No of Permission Days"] == 0
    assert zain["No of Vacation Days"] == 0
    # ZAIN's secondment spans 2026-05-01..2026-05-03. 2026-05-01 is
    # Friday (weekly off), so only the two scheduled working days
    # (Sat 05-02, Sun 05-03) count toward Secondment Days.
    assert zain["No of Secondment Days"] == 2


def test_excluded_employees_are_dropped_from_executive_summary():
    df = pd.DataFrame([
        _punch(1, "ALI-EMP1", "2026-05-01", "08:00:00", "Check In"),
        _punch(1, "ALI-EMP1", "2026-05-01", "17:00:00", "Check Out"),
        _punch(2, "ZAIN-EMP2", "2026-05-01", "08:00:00", "Check In"),
        _punch(2, "ZAIN-EMP2", "2026-05-01", "17:00:00", "Check Out"),
    ])
    schedules = pd.DataFrame([
        {"Name": "ALI-EMP1", "Working Time": "دوام صباحى (8:00AM-5:00PM)"},
        {"Name": "ZAIN-EMP2", "Working Time": "دوام صباحى (8:00AM-5:00PM)"},
    ])
    excluded = pd.DataFrame([{
        "Employee ID": 2,
        "Employee Name": "ZAIN-EMP2",
        "Exclusion Reason": "Test exclusion",
        "Notes": "",
        "Exclude From Late": True,
        "Exclude From Overtime": True,
        "Exclude From Payroll Deduction": True,
        "Exclude From Risk Scoring": True,
    }])
    summary, _ = calculate_metrics(df, schedules, excluded_df=excluded)
    exec_df = summary["executive_employee_summary"]
    assert 2 not in exec_df["Employee ID"].tolist()
    assert 1 in exec_df["Employee ID"].tolist()


ABSENCE_DETAILS_COLUMNS = [
    "Employee ID", "First Name", "Date", "Weekday",
    "Is Scheduled Working Day", "Has Attendance",
    "Time Off Type", "Is Permission", "Is Vacation",
    "Is Secondment", "Weekly Off Days", "Is Weekly Off",
    "Is Holiday", "Is Excluded",
    "Counted As Absence", "Absence Reason",
    "Scheduled Intervals", "Check-In Times", "Check-Out Times",
    "Attended Intervals", "Missed Intervals",
    "Attended Day Value", "Absence Day Value",
    # Canonical-identity merge audit columns appended in the
    # alias+manual merge release. They make it explicit which raw
    # source IDs contributed punches to a given canonical (Employee
    # ID, Date) and what the merged total worked time was.
    "Raw Employee IDs", "Sources", "Total Worked",
]


def test_absence_details_has_required_schema():
    df = pd.DataFrame([
        _punch(1, "ALI-EMP1", "2026-05-02", "08:00:00", "Check In"),
        _punch(1, "ALI-EMP1", "2026-05-02", "17:00:00", "Check Out"),
    ])
    summary, _ = calculate_metrics(df, _schedules())
    details = summary["absence_details"]
    assert list(details.columns) == ABSENCE_DETAILS_COLUMNS


def test_absence_uses_full_calendar_not_just_punched_dates():
    """An employee who never punches on a particular date inside the
    period must still be considered for absence on that date -- the
    old logic only walked dates that had at least one punch."""
    df = pd.DataFrame([
        # ALI punches only at the period bounds. The interior dates
        # (Sun 05-03 and Mon 05-04) are working days that ALI missed.
        _punch(1, "ALI-EMP1", "2026-05-02", "08:00:00", "Check In"),
        _punch(1, "ALI-EMP1", "2026-05-02", "17:00:00", "Check Out"),
        _punch(1, "ALI-EMP1", "2026-05-05", "08:00:00", "Check In"),
        _punch(1, "ALI-EMP1", "2026-05-05", "17:00:00", "Check Out"),
    ])
    schedules = pd.DataFrame([
        {"Name": "ALI-EMP1", "Working Time": "دوام صباحى (8:00AM-5:00PM)"},
    ])
    summary, _ = calculate_metrics(df, schedules)
    ali = summary["executive_employee_summary"].iloc[0]
    # 2026-05-03 (Sun) and 2026-05-04 (Mon) are scheduled working days
    # with no attendance and no time off -> 2 absences.
    assert ali["No of Absence Days"] == 2
    details = summary["absence_details"]
    interior = details[
        (details["Employee ID"] == 1)
        & details["Date"].isin(["2026-05-03", "2026-05-04"])
    ]
    assert (interior["Counted As Absence"] == True).all()  # noqa: E712


def test_absence_audit_balance_equation():
    """For every non-excluded employee:
       scheduled_working_days = attended + permission + vacation
                              + secondment + absence
    """
    df = pd.DataFrame([
        # Sat 05-02 attended, Sun 05-03 absent, Mon 05-04 permission,
        # Tue 05-05 vacation, Wed 05-06 secondment.
        _punch(1, "ALI-EMP1", "2026-05-02", "08:00:00", "Check In"),
        _punch(1, "ALI-EMP1", "2026-05-02", "17:00:00", "Check Out"),
        _punch(2, "ZAIN-EMP2", "2026-05-02", "08:00:00", "Check In"),
        _punch(2, "ZAIN-EMP2", "2026-05-03", "08:00:00", "Check In"),
        _punch(2, "ZAIN-EMP2", "2026-05-04", "08:00:00", "Check In"),
        _punch(2, "ZAIN-EMP2", "2026-05-05", "08:00:00", "Check In"),
        _punch(2, "ZAIN-EMP2", "2026-05-06", "08:00:00", "Check In"),
    ])
    schedules = pd.DataFrame([
        {"Name": "ALI-EMP1", "Working Time": "دوام صباحى (8:00AM-5:00PM)"},
        {"Name": "ZAIN-EMP2", "Working Time": "دوام صباحى (8:00AM-5:00PM)"},
    ])
    time_off = pd.DataFrame([
        {"Employee": "ALI-EMP1", "Status": "Approved",
         "Start Date": "2026-05-04 09:00:00",
         "End Date": "2026-05-04 10:00:00",
         "Time Off Type": "استأذان"},
        {"Employee": "ALI-EMP1", "Status": "Approved",
         "Start Date": "2026-05-05 00:00:00",
         "End Date": "2026-05-05 23:59:00",
         "Time Off Type": "Annual Leave"},
        {"Employee": "ALI-EMP1", "Status": "Approved",
         "Start Date": "2026-05-06 00:00:00",
         "End Date": "2026-05-06 23:59:00",
         "Time Off Type": "Secondment"},
    ])
    summary, _ = calculate_metrics(df, schedules, time_off)
    audit = summary["absence_audit"]
    assert not audit.empty

    # Every per-employee row balances.
    assert (audit["reconciliation_delta"] == 0).all()

    ali = audit[audit["Employee ID"] == 1].iloc[0]
    # Period 2026-05-02..2026-05-06: Sat,Sun,Mon,Tue,Wed = 5 working days.
    assert ali["scheduled_working_days"] == 5
    assert ali["attended_days"] == 1       # 05-02
    assert ali["absence_days"] == 1        # 05-03
    assert ali["permission_days"] == 1     # 05-04
    assert ali["vacation_days"] == 1       # 05-05
    assert ali["secondment_days"] == 1     # 05-06
    # No warnings emitted because every employee balances.
    assert summary["absence_audit_breaks"] == []


def _two_employee_schedules():
    return pd.DataFrame([
        {"Name": "ALI-EMP1", "Working Time": "دوام صباحى (8:00AM-5:00PM)"},
        {"Name": "ZAIN-EMP2", "Working Time": "دوام صباحى (8:00AM-5:00PM)"},
    ])


def _two_week_period_df():
    """Fri 05-01 .. Thu 05-14 (14 calendar days). Each employee has
    at least one punch so they enter the reporting universe; the
    contiguous period covers the full two weeks."""
    rows = []
    for d in ("2026-05-01", "2026-05-14"):
        rows.append(_punch(1, "ALI-EMP1", d, "08:00:00", "Check In"))
        rows.append(_punch(1, "ALI-EMP1", d, "17:00:00", "Check Out"))
        rows.append(_punch(2, "ZAIN-EMP2", d, "08:00:00", "Check In"))
        rows.append(_punch(2, "ZAIN-EMP2", d, "17:00:00", "Check Out"))
    return pd.DataFrame(rows)


def test_default_friday_only_weekly_off_excludes_friday_from_absence():
    df = _two_week_period_df()
    summary, _ = calculate_metrics(df, _two_employee_schedules())
    audit = summary["absence_audit"]
    ali = audit[audit["Employee ID"] == 1].iloc[0]
    # 14 days minus 2 Fridays = 12 scheduled working days.
    assert ali["scheduled_working_days"] == 12
    assert ali["weekly_off_days"] == "Friday"
    # No Friday is counted as absence in the per-row ledger.
    details = summary["absence_details"]
    fridays = details[
        (details["Employee ID"] == 1) & (details["Weekday"] == "Friday")
    ]
    assert (fridays["Is Weekly Off"] == True).all()  # noqa: E712
    assert (fridays["Counted As Absence"] == False).all()  # noqa: E712


def test_per_employee_friday_plus_saturday_off():
    """Custom override file removes Saturdays from ALI's absence
    calculation while ZAIN still uses the global default."""
    df = _two_week_period_df()
    weekly_off = pd.DataFrame([
        {"Employee ID": 1, "Employee Name": "ALI-EMP1",
         "Weekly Off Days": "Friday,Saturday", "Notes": ""},
    ])
    summary, _ = calculate_metrics(
        df, _two_employee_schedules(), weekly_off_df=weekly_off,
    )
    audit = summary["absence_audit"]
    ali = audit[audit["Employee ID"] == 1].iloc[0]
    zain = audit[audit["Employee ID"] == 2].iloc[0]

    # ALI: 14 days - 2 Fridays - 2 Saturdays = 10 scheduled working days.
    assert ali["scheduled_working_days"] == 10
    assert ali["weekly_off_days"] == "Friday,Saturday"
    assert "Saturday" not in ali["scheduled_weekdays"]
    # ZAIN keeps the global Friday-only default = 12 working days.
    assert zain["scheduled_working_days"] == 12
    assert zain["weekly_off_days"] == "Friday"

    details = summary["absence_details"]
    ali_saturdays = details[
        (details["Employee ID"] == 1) & (details["Weekday"] == "Saturday")
    ]
    assert (ali_saturdays["Is Weekly Off"] == True).all()  # noqa: E712
    assert (ali_saturdays["Counted As Absence"] == False).all()  # noqa: E712
    # ZAIN's Saturdays must remain scheduled.
    zain_saturdays = details[
        (details["Employee ID"] == 2) & (details["Weekday"] == "Saturday")
    ]
    assert (zain_saturdays["Is Weekly Off"] == False).all()  # noqa: E712


def test_per_employee_custom_rotation_off_days():
    """An employee on a Mon+Tue rotation still gets the override
    respected, even though the rest of the company is Friday-off."""
    df = _two_week_period_df()
    weekly_off = pd.DataFrame([
        {"Employee ID": 1, "Employee Name": "ALI-EMP1",
         "Weekly Off Days": "Monday,Tuesday", "Notes": "Custom rotation"},
    ])
    summary, _ = calculate_metrics(
        df, _two_employee_schedules(), weekly_off_df=weekly_off,
    )
    audit = summary["absence_audit"]
    ali = audit[audit["Employee ID"] == 1].iloc[0]
    # 14-day period contains 2 Mondays and 2 Tuesdays = 4 off days.
    assert ali["scheduled_working_days"] == 14 - 4
    assert ali["weekly_off_days"] == "Monday,Tuesday"

    details = summary["absence_details"]
    rotation_days = details[
        (details["Employee ID"] == 1)
        & details["Weekday"].isin(["Monday", "Tuesday"])
    ]
    assert (rotation_days["Is Weekly Off"] == True).all()  # noqa: E712
    assert (rotation_days["Counted As Absence"] == False).all()  # noqa: E712


def test_weekly_off_never_counts_as_absence_even_without_attendance():
    df = _two_week_period_df()
    weekly_off = pd.DataFrame([
        {"Employee ID": 1, "Employee Name": "ALI-EMP1",
         "Weekly Off Days": "Friday,Saturday", "Notes": ""},
    ])
    summary, _ = calculate_metrics(
        df, _two_employee_schedules(), weekly_off_df=weekly_off,
    )
    details = summary["absence_details"]
    # ALI has no Check In on 2026-05-08 (Friday) or 2026-05-09 (Saturday)
    # but neither must be counted as absence.
    weekly_off_rows = details[
        (details["Employee ID"] == 1)
        & details["Date"].isin(["2026-05-08", "2026-05-09"])
    ]
    assert (weekly_off_rows["Has Attendance"] == False).all()  # noqa: E712
    assert (weekly_off_rows["Counted As Absence"] == False).all()  # noqa: E712
    assert (weekly_off_rows["Is Weekly Off"] == True).all()  # noqa: E712
