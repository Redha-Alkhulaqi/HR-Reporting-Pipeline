import pandas as pd

from metrics_calculator import (
    _compute_risk,
    calculate_metrics,
    extract_shift_start,
)


def test_extract_shift_start_morning():
    assert extract_shift_start("دوام صباحى (9:00AM-6:00PM)") == "09:00"


def test_extract_shift_start_evening():
    assert extract_shift_start("دوام مسائى (1:30PM-10:30PM)") == "13:30"


def test_extract_shift_start_no_time_returns_none():
    assert extract_shift_start("no time present") is None


def test_compute_risk_no_flags_is_low():
    score, level, reason = _compute_risk(0, 0, 0, 0)
    assert score == 0
    assert level == "Low Risk"
    assert reason == "no flags"


def test_compute_risk_chronic_is_high():
    # 20 lates*2=40, 1000 min // 60 = 16, 10 missing*2=20, 4 excuses
    # = 40 + 16 + 20 + 4 = 80, comfortably above the High threshold.
    _, level, _ = _compute_risk(20, 1000, 10, 4)
    assert level == "High Risk"


def _make_inputs(check_in_time, shift_label="دوام صباحى (8:00AM-5:00PM)"):
    df = pd.DataFrame([
        {"Employee ID": 1, "First Name": "ALI-EMP1",
         "Date": "2026-05-01", "Punch Time": check_in_time,
         "Punch State": "Check In"},
        {"Employee ID": 1, "First Name": "ALI-EMP1",
         "Date": "2026-05-01", "Punch Time": "17:00:00",
         "Punch State": "Check Out"},
    ])
    schedules = pd.DataFrame([{"Name": "ALI-EMP1", "Working Time": shift_label}])
    return df, schedules


def test_calculate_metrics_on_time_within_grace():
    df, schedules = _make_inputs("08:10:00")  # 10 min, within 15-min grace
    summary, daily = calculate_metrics(df, schedules)
    assert summary["late_cases"] == 0
    assert (daily["attendance_status"] == "On Time").all()


def test_calculate_metrics_late_outside_grace():
    df, schedules = _make_inputs("09:00:00")  # 60 min late
    summary, daily = calculate_metrics(df, schedules)
    assert summary["late_cases"] == 1
    assert summary["total_late_minutes"] == 60
    assert (daily["attendance_status"] == "Late").all()


def test_missing_schedule_employee_is_not_late():
    df = pd.DataFrame([
        {"Employee ID": 99, "First Name": "GHOST-EMP99",
         "Date": "2026-05-01", "Punch Time": "10:00:00",
         "Punch State": "Check In"},
    ])
    schedules = pd.DataFrame([{
        "Name": "ALI-EMP1",
        "Working Time": "دوام صباحى (8:00AM-5:00PM)",
    }])
    summary, daily = calculate_metrics(df, schedules)
    assert summary["missing_schedule_cases"] == 1
    assert summary["late_cases"] == 0
    assert (daily["attendance_status"] == "Missing Schedule").all()


def test_data_quality_score_clean_data_is_high():
    df, schedules = _make_inputs("08:00:00")
    summary, _ = calculate_metrics(df, schedules)
    assert summary["data_quality_score"] >= 85
