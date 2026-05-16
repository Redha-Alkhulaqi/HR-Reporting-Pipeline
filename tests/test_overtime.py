import pandas as pd

from metrics_calculator import calculate_metrics, extract_shift_end


def _inputs(check_in, check_out, shift="دوام صباحى (8:00AM-5:00PM)"):
    """Build one-employee, one-day inputs with optional missing check-out."""
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


def test_extract_shift_end_single_shift():
    assert extract_shift_end("دوام صباحى (8:00AM-5:00PM)") == "17:00"


def test_extract_shift_end_split_shift_uses_last_block():
    label = "شفت صباحى (9:00AM-1:00PM) & شفت مسائى (6:00PM-10:00PM)"
    assert extract_shift_end(label) == "22:00"


def test_extract_shift_end_returns_none_when_unparseable():
    assert extract_shift_end("no shift times here") is None


def test_no_overtime_when_check_out_at_shift_end():
    df, sched = _inputs("08:00:00", "17:00:00")
    summary, daily = calculate_metrics(df, sched)
    assert (daily["overtime_status"] == "No Overtime").all()
    assert summary["overtime_cases"] == 0


def test_no_overtime_within_grace_period():
    # Shift end 17:00, check out 17:10 -> 10 min, inside 15-min grace.
    df, sched = _inputs("08:00:00", "17:10:00")
    summary, daily = calculate_metrics(df, sched)
    assert (daily["overtime_status"] == "No Overtime").all()
    assert summary["overtime_cases"] == 0


def test_overtime_above_min_threshold():
    # Shift end 17:00, check out 18:00 -> 60 min, comfortably above
    # grace (15) and min threshold (30).
    df, sched = _inputs("08:00:00", "18:00:00")
    summary, daily = calculate_metrics(df, sched)
    assert (daily["overtime_status"] == "Overtime").all()
    assert summary["overtime_cases"] == 1
    assert summary["total_overtime_minutes"] == 60


def test_overtime_below_min_threshold_is_dropped():
    # Shift end 17:00, check out 17:20 -> 20 min, exceeds grace (15)
    # but below MIN_OVERTIME_MINUTES (30) so it does not count.
    df, sched = _inputs("08:00:00", "17:20:00")
    summary, daily = calculate_metrics(df, sched)
    assert (daily["overtime_status"] == "No Overtime").all()
    assert summary["overtime_cases"] == 0


def test_missing_checkout_blocks_overtime():
    df, sched = _inputs("08:00:00", check_out=None)
    summary, daily = calculate_metrics(df, sched)
    assert (daily["overtime_status"] == "Missing Check Out").all()
    assert summary["overtime_cases"] == 0


def test_missing_schedule_blocks_overtime():
    # Employee not in schedules export.
    df = pd.DataFrame([
        {"Employee ID": 99, "First Name": "GHOST-EMP99",
         "Date": "2026-05-01", "Punch Time": "08:00:00",
         "Punch State": "Check In"},
        {"Employee ID": 99, "First Name": "GHOST-EMP99",
         "Date": "2026-05-01", "Punch Time": "20:00:00",
         "Punch State": "Check Out"},
    ])
    schedules = pd.DataFrame([
        {"Name": "ALI-EMP1", "Working Time": "(8:00AM-5:00PM)"},
    ])
    summary, daily = calculate_metrics(df, schedules)
    assert (daily["overtime_status"] == "Missing Schedule").all()
    assert summary["overtime_cases"] == 0


def test_night_shift_crossing_midnight():
    # Shift 8PM-4AM. Check in 20:00, check out 05:00 (next day).
    # Worked = 9h, scheduled = 8h, overtime = 60 min.
    df, sched = _inputs("20:00:00", "05:00:00",
                        shift="ليلى (8:00PM-4:00AM)")
    summary, daily = calculate_metrics(df, sched)
    row = daily.iloc[0]
    assert row["overtime_status"] == "Overtime"
    assert row["scheduled_minutes"] == 480  # 8h
    assert row["worked_minutes"] == 540  # 9h
    assert row["overtime_minutes"] == 60
    assert summary["total_overtime_minutes"] == 60
