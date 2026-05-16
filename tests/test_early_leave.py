import pandas as pd

from metrics_calculator import calculate_metrics


def _inputs(check_in, check_out, shift="دوام صباحى (8:00AM-5:00PM)"):
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


def test_normal_check_out_at_shift_end():
    df, sched = _inputs("08:00:00", "17:00:00")  # exactly at Shift End
    summary, daily = calculate_metrics(df, sched)
    assert (daily["early_leave_status"] == "Normal").all()
    assert summary["early_leave_cases"] == 0


def test_within_grace_is_normal():
    # 5 min early -- below EARLY_LEAVE_GRACE_MINUTES (10).
    df, sched = _inputs("08:00:00", "16:55:00")
    summary, daily = calculate_metrics(df, sched)
    assert (daily["early_leave_status"] == "Normal").all()
    assert summary["early_leave_cases"] == 0


def test_beyond_grace_is_early_leave():
    # 30 min early -- above grace.
    df, sched = _inputs("08:00:00", "16:30:00")
    summary, daily = calculate_metrics(df, sched)
    assert (daily["early_leave_status"] == "Early Leave").all()
    assert summary["early_leave_cases"] == 1
    assert summary["total_early_leave_minutes"] == 30
    assert summary["employees_with_early_leave"] == 1


def test_missing_checkout_blocks_early_leave():
    df, sched = _inputs("08:00:00", check_out=None)
    summary, daily = calculate_metrics(df, sched)
    assert (daily["early_leave_status"] == "Missing Check Out").all()
    assert summary["early_leave_cases"] == 0


def test_missing_schedule_blocks_early_leave():
    df = pd.DataFrame([
        {"Employee ID": 99, "First Name": "GHOST-EMP99",
         "Date": "2026-05-01", "Punch Time": "08:00:00",
         "Punch State": "Check In"},
        {"Employee ID": 99, "First Name": "GHOST-EMP99",
         "Date": "2026-05-01", "Punch Time": "12:00:00",
         "Punch State": "Check Out"},
    ])
    schedules = pd.DataFrame([
        {"Name": "ALI-EMP1", "Working Time": "(8:00AM-5:00PM)"},
    ])
    summary, daily = calculate_metrics(df, schedules)
    assert (daily["early_leave_status"] == "Missing Schedule").all()
    assert summary["early_leave_cases"] == 0


def test_night_shift_early_leave():
    # Shift 8PM-4AM, check out at 3AM the next day -- 60 min early.
    df, sched = _inputs("20:00:00", "03:00:00",
                        shift="ليلى (8:00PM-4:00AM)")
    summary, daily = calculate_metrics(df, sched)
    row = daily.iloc[0]
    assert row["early_leave_status"] == "Early Leave"
    assert row["early_leave_minutes"] == 60
    assert summary["total_early_leave_minutes"] == 60


def test_overtime_keeps_early_leave_normal():
    # Stay past shift end -- early leave should NOT trigger.
    df, sched = _inputs("08:00:00", "18:00:00")
    summary, daily = calculate_metrics(df, sched)
    assert (daily["early_leave_status"] == "Normal").all()
    assert (daily["overtime_status"] == "Overtime").all()


def test_early_leave_under_threshold_is_not_anomaly():
    # 30 min early -- well under MAX_REASONABLE_EARLY_LEAVE_MINUTES (180).
    df, sched = _inputs("08:00:00", "16:30:00")
    summary, daily = calculate_metrics(df, sched)
    row = daily.iloc[0]
    assert row["early_leave_status"] == "Early Leave"
    assert row["early_leave_anomaly"] == False  # noqa: E712
    assert row["early_leave_anomaly_reason"] == ""
    assert summary["early_leave_anomaly_cases"] == 0


def test_early_leave_above_threshold_is_anomaly_but_kept():
    # Check-out 8 hours early (480 min) -- well above 180-min threshold.
    df, sched = _inputs("08:00:00", "09:00:00")
    summary, daily = calculate_metrics(df, sched)
    row = daily.iloc[0]
    assert row["early_leave_status"] == "Early Leave"
    assert row["early_leave_minutes"] == 480
    assert row["early_leave_anomaly"] == True  # noqa: E712
    assert row["early_leave_anomaly_reason"] == "Exceeds reasonable threshold"
    # Anomaly row STILL counts toward the totals -- it is only flagged.
    assert summary["total_early_leave_minutes"] == 480
    assert summary["early_leave_anomaly_cases"] == 1
