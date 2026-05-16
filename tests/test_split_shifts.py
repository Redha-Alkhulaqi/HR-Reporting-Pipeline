import pandas as pd

from metrics_calculator import (
    calculate_metrics,
    extract_shift_intervals,
)


SINGLE = "دوام صباحى (8:00AM-5:00PM)"
SPLIT = "شفت صباحى (9:00AM-1:00PM) & شفت مسائى (4:00PM-8:00PM)"
NIGHT = "ليلى (8:00PM-4:00AM)"


def _inputs(check_in, check_out, shift):
    """One employee, one day. check_out=None omits the check-out row."""
    rows = [{
        "Employee ID": 1, "First Name": "ALI-EMP1",
        "Date": "2026-05-01", "Punch Time": check_in,
        "Punch State": "Check In",
    }]
    if check_out is not None:
        rows.append({
            "Employee ID": 1, "First Name": "ALI-EMP1",
            "Date": "2026-05-01", "Punch Time": check_out,
            "Punch State": "Check Out",
        })
    df = pd.DataFrame(rows)
    schedules = pd.DataFrame([{"Name": "ALI-EMP1", "Working Time": shift}])
    return df, schedules


def test_extract_intervals_single_shift():
    assert extract_shift_intervals(SINGLE) == [("08:00", "17:00")]


def test_extract_intervals_split_shift():
    assert extract_shift_intervals(SPLIT) == [
        ("09:00", "13:00"), ("16:00", "20:00"),
    ]


def test_extract_intervals_night_shift():
    assert extract_shift_intervals(NIGHT) == [("20:00", "04:00")]


def test_extract_intervals_returns_empty_when_unparseable():
    assert extract_shift_intervals("no time here") == []


def test_single_shift_no_early_leave():
    df, sched = _inputs("08:00:00", "17:00:00", SINGLE)
    summary, daily = calculate_metrics(df, sched)
    row = daily.iloc[0]
    assert row["early_leave_status"] == "Normal"
    assert row["overtime_status"] == "No Overtime"
    assert row["matched_shift_label"] == "08:00-17:00"
    assert row["matched_scheduled_minutes"] == 540
    assert summary["early_leave_cases"] == 0


def test_single_shift_early_leave_counted():
    df, sched = _inputs("08:00:00", "16:30:00", SINGLE)  # 30 min early
    summary, daily = calculate_metrics(df, sched)
    assert (daily["early_leave_status"] == "Early Leave").all()
    assert summary["total_early_leave_minutes"] == 30


def test_split_shift_morning_segment_normal_checkout():
    """Worked morning only, checked out at morning end -> Normal."""
    df, sched = _inputs("09:00:00", "13:00:00", SPLIT)
    summary, daily = calculate_metrics(df, sched)
    row = daily.iloc[0]
    assert row["early_leave_status"] == "Normal"
    assert row["overtime_status"] == "No Overtime"
    assert row["matched_shift_label"] == "09:00-13:00"
    assert row["matched_scheduled_minutes"] == 240
    # scheduled (sum) = 4h morning + 4h evening = 480 min.
    assert row["scheduled_minutes"] == 480
    assert summary["early_leave_cases"] == 0
    assert summary["overtime_cases"] == 0


def test_split_shift_evening_segment_normal_checkout():
    """Worked evening only, checked out at evening end -> Normal."""
    df, sched = _inputs("16:00:00", "20:00:00", SPLIT)
    summary, daily = calculate_metrics(df, sched)
    row = daily.iloc[0]
    assert row["early_leave_status"] == "Normal"
    assert row["overtime_status"] == "No Overtime"
    assert row["matched_shift_label"] == "16:00-20:00"
    assert summary["early_leave_cases"] == 0


def test_split_shift_gap_is_not_early_leave():
    """Worked morning, checked out at 2pm (during the gap) -> Normal.

    Pre-fix bug: matched against evening end (8pm) -> 6h early leave.
    With the fix: matched = morning, delta past morning end is in the
    gap before evening starts -> No Overtime, No Early Leave.
    """
    df, sched = _inputs("09:00:00", "14:00:00", SPLIT)
    summary, daily = calculate_metrics(df, sched)
    row = daily.iloc[0]
    assert row["early_leave_status"] == "Normal"
    assert row["overtime_status"] == "No Overtime"
    assert row["matched_shift_label"] == "09:00-13:00"
    assert summary["early_leave_cases"] == 0
    assert summary["overtime_cases"] == 0


def test_split_shift_overtime_after_matched_evening():
    """Worked full day, checked out 1h after evening end -> Overtime."""
    df, sched = _inputs("09:00:00", "21:00:00", SPLIT)
    summary, daily = calculate_metrics(df, sched)
    row = daily.iloc[0]
    assert row["overtime_status"] == "Overtime"
    assert row["matched_shift_label"] == "16:00-20:00"
    assert row["overtime_minutes"] == 60
    assert row["early_leave_status"] == "Normal"


def test_split_shift_early_leave_from_evening():
    """Worked full day but checked out 30 min before evening end."""
    df, sched = _inputs("09:00:00", "19:30:00", SPLIT)
    summary, daily = calculate_metrics(df, sched)
    row = daily.iloc[0]
    assert row["early_leave_status"] == "Early Leave"
    assert row["matched_shift_label"] == "16:00-20:00"
    assert row["early_leave_minutes"] == 30


def test_night_shift_crossing_midnight_still_works():
    df, sched = _inputs("20:00:00", "05:00:00", NIGHT)
    summary, daily = calculate_metrics(df, sched)
    row = daily.iloc[0]
    assert row["matched_shift_label"] == "20:00-04:00"
    assert row["worked_minutes"] == 540  # 9 hours
    assert row["scheduled_minutes"] == 480  # 8 hours scheduled
    assert row["overtime_status"] == "Overtime"
    assert row["overtime_minutes"] == 60
