"""Split-shift absence regression tests.

The absence engine used to use a binary "any check-in -> present"
rule per (employee, date). That under-counted absences for
split-shift employees: an employee who attended only the evening
shift on a morning+evening schedule was scored as fully present.

The engine now walks each scheduled interval independently and
records a fractional `Absence Day Value` -- 0.0 for a fully
attended day, 0.5 for a half-day partial absence, 1.0 for a
no-show. These tests pin the new behavior and guard against
regressions for the single-shift and missing-schedule paths.
"""
import pandas as pd

from metrics_calculator import calculate_metrics


# -- helpers --------------------------------------------------------------

def _punch(emp_id, name, date, time, state):
    return {
        "Employee ID": emp_id, "First Name": name,
        "Date": date, "Punch Time": time, "Punch State": state,
    }


SPLIT_SHIFT_LABEL = "شفت صباحى (9:00AM-1:00PM) & شفت مسائى (6:00PM-10:00PM)"


def _split_shift_schedules():
    """One employee on a 9-13 / 18-22 split shift, like EMP403."""
    return pd.DataFrame([{
        "Name": "SAMEER-EMP1",
        "Working Time": SPLIT_SHIFT_LABEL,
    }])


def _single_shift_schedules():
    return pd.DataFrame([{
        "Name": "SOLO-EMP1",
        "Working Time": "(8:00AM-5:00PM)",
    }])


def _both_shifts_punches(emp_id, name, date):
    """One Check In + Check Out per scheduled interval."""
    return [
        _punch(emp_id, name, date, "09:05:00", "Check In"),
        _punch(emp_id, name, date, "13:01:00", "Check Out"),
        _punch(emp_id, name, date, "18:02:00", "Check In"),
        _punch(emp_id, name, date, "22:01:00", "Check Out"),
    ]


def _morning_only_punches(emp_id, name, date):
    return [
        _punch(emp_id, name, date, "09:05:00", "Check In"),
        _punch(emp_id, name, date, "13:01:00", "Check Out"),
    ]


def _evening_only_punches(emp_id, name, date):
    """Mirror of EMP403 on 2026-05-04 -- only the evening shift."""
    return [
        _punch(emp_id, name, date, "18:04:00", "Check In"),
        _punch(emp_id, name, date, "22:01:00", "Check Out"),
    ]


def _details_for(summary, date):
    ad = summary["absence_details"]
    sub = ad[ad["Date"] == date]
    assert len(sub) == 1, (
        f"expected 1 absence-detail row for {date}, got {len(sub)}"
    )
    return sub.iloc[0]


# -- split-shift tests ----------------------------------------------------

def test_split_shift_both_intervals_attended_is_no_absence():
    rows = _both_shifts_punches(1, "SAMEER-EMP1", "2026-05-05")
    summary, _ = calculate_metrics(
        pd.DataFrame(rows), _split_shift_schedules(),
        period_start="2026-05-05", period_end="2026-05-05",
    )
    row = _details_for(summary, "2026-05-05")
    assert row["Absence Day Value"] == 0.0
    assert row["Attended Day Value"] == 1.0
    assert bool(row["Counted As Absence"]) is False
    assert row["Attended Intervals"] == "09:00-13:00, 18:00-22:00"
    assert row["Missed Intervals"] == ""
    audit = summary["absence_audit"].iloc[0]
    assert audit["absence_days"] == 0.0
    assert audit["attended_days"] == 1.0


def test_split_shift_evening_only_is_half_day_absence():
    """The EMP403 2026-05-04 case: only the evening shift attended."""
    rows = _evening_only_punches(1, "SAMEER-EMP1", "2026-05-04")
    summary, _ = calculate_metrics(
        pd.DataFrame(rows), _split_shift_schedules(),
        period_start="2026-05-04", period_end="2026-05-04",
    )
    row = _details_for(summary, "2026-05-04")
    assert row["Absence Day Value"] == 0.5
    assert row["Attended Day Value"] == 0.5
    assert bool(row["Counted As Absence"]) is True  # any missed interval counts
    assert row["Attended Intervals"] == "18:00-22:00"
    assert row["Missed Intervals"] == "09:00-13:00"
    assert "Partial absence" in row["Absence Reason"]
    audit = summary["absence_audit"].iloc[0]
    assert audit["absence_days"] == 0.5
    assert audit["attended_days"] == 0.5
    assert audit["reconciliation_delta"] == 0  # balances


def test_split_shift_morning_only_is_half_day_absence():
    rows = _morning_only_punches(1, "SAMEER-EMP1", "2026-05-05")
    summary, _ = calculate_metrics(
        pd.DataFrame(rows), _split_shift_schedules(),
        period_start="2026-05-05", period_end="2026-05-05",
    )
    row = _details_for(summary, "2026-05-05")
    assert row["Absence Day Value"] == 0.5
    assert row["Attended Intervals"] == "09:00-13:00"
    assert row["Missed Intervals"] == "18:00-22:00"


def test_split_shift_no_punches_is_full_absence():
    """No check-ins at all on a scheduled split-shift day."""
    # Provide a punch on a different date so the period is non-empty.
    rows = _both_shifts_punches(1, "SAMEER-EMP1", "2026-05-06")
    summary, _ = calculate_metrics(
        pd.DataFrame(rows), _split_shift_schedules(),
        period_start="2026-05-05", period_end="2026-05-06",
    )
    row = _details_for(summary, "2026-05-05")
    assert row["Absence Day Value"] == 1.0
    assert row["Attended Day Value"] == 0.0
    assert bool(row["Counted As Absence"]) is True
    assert row["Missed Intervals"] == "09:00-13:00, 18:00-22:00"


def test_single_shift_unchanged_when_attended():
    """Backward compat: single-shift employees produce 0.0 / 1.0
    absence values, just like before the fractional refactor."""
    rows = [
        _punch(1, "SOLO-EMP1", "2026-05-05", "08:00:00", "Check In"),
        _punch(1, "SOLO-EMP1", "2026-05-05", "17:00:00", "Check Out"),
    ]
    summary, _ = calculate_metrics(
        pd.DataFrame(rows), _single_shift_schedules(),
        period_start="2026-05-05", period_end="2026-05-05",
    )
    row = _details_for(summary, "2026-05-05")
    assert row["Absence Day Value"] == 0.0
    assert bool(row["Counted As Absence"]) is False


def test_single_shift_unchanged_when_absent():
    rows = [
        _punch(1, "SOLO-EMP1", "2026-05-06", "08:00:00", "Check In"),
    ]
    summary, _ = calculate_metrics(
        pd.DataFrame(rows), _single_shift_schedules(),
        period_start="2026-05-05", period_end="2026-05-06",
    )
    row = _details_for(summary, "2026-05-05")
    assert row["Absence Day Value"] == 1.0
    assert bool(row["Counted As Absence"]) is True


def test_weekly_off_never_yields_absence_even_with_split_shift():
    """Friday is the default weekly off; the engine must not count it
    as an absence even when the employee has a split-shift schedule
    and no attendance."""
    # 2026-05-01 is a Friday.
    # Provide a punch on a different scheduled day so period spans.
    rows = _both_shifts_punches(1, "SAMEER-EMP1", "2026-05-02")
    summary, _ = calculate_metrics(
        pd.DataFrame(rows), _split_shift_schedules(),
        period_start="2026-05-01", period_end="2026-05-02",
    )
    row = _details_for(summary, "2026-05-01")
    assert bool(row["Is Weekly Off"]) is True
    assert bool(row["Is Scheduled Working Day"]) is False
    assert row["Absence Day Value"] == 0.0
    assert bool(row["Counted As Absence"]) is False


def test_missing_schedule_split_shift_employee_does_not_get_false_absence():
    """An employee absent from the Odoo resources file should not
    generate phantom absences -- we cannot tell what they were
    scheduled to do."""
    rows = _both_shifts_punches(99, "GHOST-EMP99", "2026-05-05")
    summary, _ = calculate_metrics(
        pd.DataFrame(rows),
        pd.DataFrame([{"Name": "DIFFERENT-EMP1",
                       "Working Time": SPLIT_SHIFT_LABEL}]),
        period_start="2026-05-05", period_end="2026-05-05",
    )
    row = _details_for(summary, "2026-05-05")
    assert bool(row["Is Scheduled Working Day"]) is False
    assert row["Absence Day Value"] == 0.0


def test_split_shift_check_in_outside_intervals_falls_back_to_attended():
    """A weird single Check In at 14:30 (between morning & evening)
    falls in neither interval's grace window. The conservative
    fallback rule keeps this as fully attended -- we never flag a
    partial absence purely because of a check-in time quirk."""
    rows = [
        _punch(1, "SAMEER-EMP1", "2026-05-05", "14:30:00", "Check In"),
    ]
    summary, _ = calculate_metrics(
        pd.DataFrame(rows), _split_shift_schedules(),
        period_start="2026-05-05", period_end="2026-05-05",
    )
    row = _details_for(summary, "2026-05-05")
    assert row["Absence Day Value"] == 0.0
    assert row["Attended Day Value"] == 1.0
    assert bool(row["Counted As Absence"]) is False


def test_split_shift_audit_balances_with_mixed_days():
    """8 scheduled days, with mixed full/half/missed attendance.
    Verify the audit reconciliation_delta is exactly zero."""
    name = "SAMEER-EMP1"
    rows = []
    # 4 fully attended days, 2 half-day (evening only), 2 full absence.
    full_dates = ["2026-04-26", "2026-04-27", "2026-04-28", "2026-04-29"]
    half_dates = ["2026-04-30", "2026-05-02"]
    # absent: 2026-05-03, 2026-05-04 -- no punches at all
    for d in full_dates:
        rows.extend(_both_shifts_punches(1, name, d))
    for d in half_dates:
        rows.extend(_evening_only_punches(1, name, d))
    summary, _ = calculate_metrics(
        pd.DataFrame(rows), _split_shift_schedules(),
        period_start="2026-04-26", period_end="2026-05-04",
    )
    audit = summary["absence_audit"].iloc[0]
    # 2026-05-01 is Friday (weekly off) -> 8 scheduled working days.
    assert audit["scheduled_working_days"] == 8
    assert audit["attended_days"] == 5.0   # 4 full + 2*0.5
    assert audit["absence_days"] == 3.0    # 2 full + 2*0.5
    assert audit["reconciliation_delta"] == 0
    # Executive summary mirrors.
    exec_row = summary["executive_employee_summary"].iloc[0]
    assert exec_row["No of Absence Days"] == 3.0
