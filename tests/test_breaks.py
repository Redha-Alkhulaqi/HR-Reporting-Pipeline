import pandas as pd

from metrics_calculator import calculate_metrics


SCHED_LABEL = "دوام صباحى (8:00AM-5:00PM)"


def _punch(employee_id, name, time, state, date="2026-05-01"):
    return {
        "Employee ID": employee_id, "First Name": name,
        "Date": date, "Punch Time": time, "Punch State": state,
    }


def _schedules(name=None):
    name = name or "ALI-EMP1"
    return pd.DataFrame([{"Name": name, "Working Time": SCHED_LABEL}])


def test_normal_break_out_in_pair():
    df = pd.DataFrame([
        _punch(1, "ALI-EMP1", "08:00:00", "Check In"),
        _punch(1, "ALI-EMP1", "12:00:00", "Break Out"),
        _punch(1, "ALI-EMP1", "12:30:00", "Break In"),
        _punch(1, "ALI-EMP1", "17:00:00", "Check Out"),
    ])
    summary, daily = calculate_metrics(df, _schedules())
    row = daily.iloc[0]
    assert row["break_count"] == 1
    assert row["total_break_minutes"] == 30
    assert row["incomplete_break_count"] == 0
    assert summary["total_break_count"] == 1
    assert summary["total_break_minutes"] == 30
    assert summary["employees_with_breaks"] == 1
    assert summary["incomplete_break_records"] == 0


def test_multiple_breaks_same_day():
    df = pd.DataFrame([
        _punch(1, "ALI-EMP1", "08:00:00", "Check In"),
        _punch(1, "ALI-EMP1", "10:00:00", "Break Out"),
        _punch(1, "ALI-EMP1", "10:15:00", "Break In"),
        _punch(1, "ALI-EMP1", "13:00:00", "Lunch Out"),
        _punch(1, "ALI-EMP1", "13:45:00", "Lunch In"),
        _punch(1, "ALI-EMP1", "17:00:00", "Check Out"),
    ])
    summary, daily = calculate_metrics(df, _schedules())
    row = daily.iloc[0]
    assert row["break_count"] == 2
    # 15 min + 45 min = 60 min total.
    assert row["total_break_minutes"] == 60
    assert row["incomplete_break_count"] == 0


def test_missing_break_in_is_incomplete():
    df = pd.DataFrame([
        _punch(1, "ALI-EMP1", "08:00:00", "Check In"),
        _punch(1, "ALI-EMP1", "12:00:00", "Break Out"),
        # No matching Break In.
        _punch(1, "ALI-EMP1", "17:00:00", "Check Out"),
    ])
    summary, daily = calculate_metrics(df, _schedules())
    row = daily.iloc[0]
    assert row["break_count"] == 0
    assert row["total_break_minutes"] == 0
    assert row["incomplete_break_count"] == 1
    assert summary["incomplete_break_records"] == 1


def test_missing_break_out_is_incomplete():
    df = pd.DataFrame([
        _punch(1, "ALI-EMP1", "08:00:00", "Check In"),
        # Break In with no preceding Break Out.
        _punch(1, "ALI-EMP1", "12:30:00", "Break In"),
        _punch(1, "ALI-EMP1", "17:00:00", "Check Out"),
    ])
    summary, daily = calculate_metrics(df, _schedules())
    row = daily.iloc[0]
    assert row["break_count"] == 0
    assert row["incomplete_break_count"] == 1


def test_break_does_not_affect_existing_kpis():
    """Same attendance once WITH break punches and once WITHOUT --
    every non-break KPI must be identical."""
    base = [
        _punch(1, "ALI-EMP1", "09:00:00", "Check In"),   # 60 min late
        _punch(1, "ALI-EMP1", "18:00:00", "Check Out"),  # 60 min overtime
    ]
    with_breaks = base + [
        _punch(1, "ALI-EMP1", "12:00:00", "Break Out"),
        _punch(1, "ALI-EMP1", "12:30:00", "Break In"),
    ]
    s_plain, d_plain = calculate_metrics(pd.DataFrame(base), _schedules())
    s_brk, d_brk = calculate_metrics(pd.DataFrame(with_breaks), _schedules())

    for key in (
        "late_cases", "total_late_minutes",
        "overtime_cases", "total_overtime_minutes",
        "early_leave_cases", "total_early_leave_minutes",
        "approved_excuse_cases", "leave_cases",
        "missing_schedule_cases", "missing_check_out_cases",
        "data_quality_score",
        "total_estimated_deduction", "total_deduction_capped",
        "high_risk_employees",
    ):
        assert s_plain[key] == s_brk[key], (
            f"break punches must not affect {key}: "
            f"plain={s_plain[key]!r} vs with_breaks={s_brk[key]!r}"
        )
    # But the break columns DO show up.
    assert s_brk["total_break_count"] == 1
    assert s_brk["total_break_minutes"] == 30


def test_arabic_break_labels_recognized():
    df = pd.DataFrame([
        _punch(1, "ALI-EMP1", "08:00:00", "Check In"),
        _punch(1, "ALI-EMP1", "12:00:00", "استراحة خروج"),
        _punch(1, "ALI-EMP1", "12:20:00", "استراحة دخول"),
        _punch(1, "ALI-EMP1", "17:00:00", "Check Out"),
    ])
    summary, daily = calculate_metrics(df, _schedules())
    assert daily.iloc[0]["break_count"] == 1
    assert daily.iloc[0]["total_break_minutes"] == 20
