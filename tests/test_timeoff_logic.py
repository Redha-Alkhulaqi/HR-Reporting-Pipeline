import pandas as pd

from metrics_calculator import calculate_metrics


def _base_inputs(check_in_time):
    df = pd.DataFrame([
        {"Employee ID": 1, "First Name": "ALI-EMP1",
         "Date": "2026-05-01", "Punch Time": check_in_time,
         "Punch State": "Check In"},
    ])
    schedules = pd.DataFrame([
        {"Name": "ALI-EMP1", "Working Time": "دوام صباحى (8:00AM-5:00PM)"},
    ])
    return df, schedules


def test_excuse_fully_covers_delay():
    df, sched = _base_inputs("10:00:00")  # 120 min delay
    time_off = pd.DataFrame([{
        "Employee": "ALI-EMP1",
        "Status": "Approved",
        "Start Date": "2026-05-01 08:00:00",
        "End Date": "2026-05-01 10:30:00",
        "Time Off Type": "استأذان -",
    }])
    summary, daily = calculate_metrics(df, sched, time_off)
    assert (daily["attendance_status"] == "Approved Excuse").all()
    assert summary["late_cases"] == 0


def test_excuse_partial_still_late():
    df, sched = _base_inputs("10:00:00")  # 120 min delay
    time_off = pd.DataFrame([{
        "Employee": "ALI-EMP1",
        "Status": "Approved",
        "Start Date": "2026-05-01 08:00:00",
        "End Date": "2026-05-01 08:30:00",  # only 30 min covered
        "Time Off Type": "استأذان -",
    }])
    summary, daily = calculate_metrics(df, sched, time_off)
    assert (daily["attendance_status"] == "Late").all()
    # Unexcused = 120 - 30 = 90 minutes
    assert summary["total_late_minutes"] == 90


def test_leave_supersedes_lateness():
    df, sched = _base_inputs("10:00:00")
    time_off = pd.DataFrame([{
        "Employee": "ALI-EMP1",
        "Status": "Approved",
        "Start Date": "2026-05-01 00:00:00",
        "End Date": "2026-05-01 23:59:00",
        "Time Off Type": "Annual Leave",
    }])
    summary, daily = calculate_metrics(df, sched, time_off)
    assert (daily["attendance_status"] == "Leave").all()
    assert summary["late_cases"] == 0


def test_unapproved_time_off_is_ignored():
    df, sched = _base_inputs("09:00:00")  # 60 min late
    time_off = pd.DataFrame([{
        "Employee": "ALI-EMP1",
        "Status": "Refused",
        "Start Date": "2026-05-01 08:00:00",
        "End Date": "2026-05-01 10:00:00",
        "Time Off Type": "استأذان -",
    }])
    summary, daily = calculate_metrics(df, sched, time_off)
    assert summary["late_cases"] == 1


def test_no_timeoff_data_is_safe():
    df, sched = _base_inputs("08:00:00")
    summary, daily = calculate_metrics(df, sched, time_off_df=None)
    assert (daily["attendance_status"] == "On Time").all()


def test_leave_wins_over_excuse_on_same_day():
    df, sched = _base_inputs("10:00:00")  # 120 min delay
    time_off = pd.DataFrame([
        {
            "Employee": "ALI-EMP1", "Status": "Approved",
            "Start Date": "2026-05-01 08:00:00",
            "End Date": "2026-05-01 09:00:00",
            "Time Off Type": "استأذان -",
        },
        {
            "Employee": "ALI-EMP1", "Status": "Approved",
            "Start Date": "2026-05-01 00:00:00",
            "End Date": "2026-05-01 23:59:00",
            "Time Off Type": "Annual Leave",
        },
    ])
    summary, daily = calculate_metrics(df, sched, time_off)
    assert (daily["attendance_status"] == "Leave").all()
