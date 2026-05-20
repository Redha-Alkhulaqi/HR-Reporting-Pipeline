"""Gaming Friday Compensation business rule.

Some employees (e.g. Gaming showroom team) work on Fridays even
though Friday is the official weekly off. In exchange they take
one compensation day off during the work-week. The rule:

    compensation_days = min(worked_friday_count, weekday_absence_days)

For each compensated weekday absence:
- Absence Day Value drops to 0 (or by the available compensation
  budget for partial-day absences).
- Counted As Absence flips to False once the row is fully covered.
- The audit `Friday Compensation` column is set to True.
- Absence Reason is rewritten to cite the paired Friday date.

These tests pin the user's exact acceptance criteria + edge cases.
"""
import pandas as pd

from metrics_calculator import calculate_metrics


SCHEDULE = pd.DataFrame([{
    "Name": "ALI-EMP1",
    "Working Time": "دوام صباحى (8:00AM-5:00PM)",
}])


def _punch(eid, name, date, time, state):
    return {
        "Employee ID": eid, "First Name": name,
        "Date": date, "Punch Time": time, "Punch State": state,
    }


def _full_day(eid, name, date):
    return [
        _punch(eid, name, date, "08:00:00", "Check In"),
        _punch(eid, name, date, "17:00:00", "Check Out"),
    ]


# -- User's exact acceptance criteria ------------------------------------

def _scenario_one_absence_one_worked_friday():
    """6-day window 2026-05-03 (Sun) -> 2026-05-08 (Fri) with one
    absence on Thursday 2026-05-07 and a worked Friday 2026-05-08."""
    rows = []
    rows.extend(_full_day(1, "ALI-EMP1", "2026-05-03"))   # Sun worked
    rows.extend(_full_day(1, "ALI-EMP1", "2026-05-04"))   # Mon worked
    rows.extend(_full_day(1, "ALI-EMP1", "2026-05-05"))   # Tue worked
    rows.extend(_full_day(1, "ALI-EMP1", "2026-05-06"))   # Wed worked
    # Thu 2026-05-07 ABSENT (no punches)
    rows.extend(_full_day(1, "ALI-EMP1", "2026-05-08"))   # Fri WORKED
    return rows


def test_one_weekday_absence_plus_one_worked_friday_zeroes_the_absence():
    """The user's stated scenario:
    1 weekday absence + 1 worked Friday
      -> absence count decreases by 1
      -> Friday Compensation Days increases by 1
    """
    df = pd.DataFrame(_scenario_one_absence_one_worked_friday())
    summary, _ = calculate_metrics(
        df, SCHEDULE,
        period_start="2026-05-03", period_end="2026-05-08",
    )
    row = summary["executive_employee_summary"].iloc[0]
    # Absence count dropped from 1.0 (Thu 05-07) to 0.0.
    assert row["No of Absence Days"] == 0.0
    # Compensation column reflects the 1 day swap.
    assert row["Friday Compensation Days"] == 1
    # And the worked Friday is listed.
    assert "2026-05-08" in row["Friday Worked Dates"]


def test_absence_audit_row_carries_friday_compensation_fields():
    """The Absence Audit table exposes the same two fields so the
    per-employee audit balance still reconciles after compensation."""
    df = pd.DataFrame(_scenario_one_absence_one_worked_friday())
    summary, _ = calculate_metrics(
        df, SCHEDULE,
        period_start="2026-05-03", period_end="2026-05-08",
    )
    audit = summary["absence_audit"].iloc[0]
    assert int(audit["friday_compensation_days"]) == 1
    assert "2026-05-08" in audit["friday_worked_dates"]
    # The reduction is reflected in the absence_days total too.
    assert float(audit["absence_days"]) == 0.0


# -- Boundary / cap behavior --------------------------------------------

def test_compensation_caps_at_min_of_friday_and_absence_counts():
    """compensation = min(friday_count, weekday_absence_count).

    Setup: 1 worked Friday + 3 weekday absences. Only ONE absence
    should be compensated; the other two stay flagged.
    """
    rows = []
    rows.extend(_full_day(1, "ALI-EMP1", "2026-05-03"))   # Sun worked
    # Mon 05-04, Tue 05-05, Wed 05-06 ABSENT (no punches)
    rows.extend(_full_day(1, "ALI-EMP1", "2026-05-07"))   # Thu worked
    rows.extend(_full_day(1, "ALI-EMP1", "2026-05-08"))   # Fri WORKED
    df = pd.DataFrame(rows)
    summary, _ = calculate_metrics(
        df, SCHEDULE,
        period_start="2026-05-03", period_end="2026-05-08",
    )
    row = summary["executive_employee_summary"].iloc[0]
    # 3 weekday absences - 1 compensated = 2 remaining.
    assert row["No of Absence Days"] == 2.0
    assert row["Friday Compensation Days"] == 1


def test_more_worked_fridays_than_absences_caps_compensation():
    """2 worked Fridays + 1 weekday absence
       -> compensation = min(2, 1) = 1, NOT 2.
    The extra worked Friday is informational only (no employee gets
    paid for two compensation days when they only had one absence)."""
    rows = []
    rows.extend(_full_day(1, "ALI-EMP1", "2026-05-03"))   # Sun worked
    rows.extend(_full_day(1, "ALI-EMP1", "2026-05-04"))   # Mon worked
    rows.extend(_full_day(1, "ALI-EMP1", "2026-05-05"))   # Tue worked
    rows.extend(_full_day(1, "ALI-EMP1", "2026-05-06"))   # Wed worked
    # Thu 05-07 ABSENT
    rows.extend(_full_day(1, "ALI-EMP1", "2026-05-08"))   # Fri WORKED
    # Sat 05-09, Sun 05-10, Mon 05-11, Tue 05-12, Wed 05-13, Thu 05-14 worked
    for d in ("2026-05-09", "2026-05-10", "2026-05-11", "2026-05-12",
              "2026-05-13", "2026-05-14"):
        rows.extend(_full_day(1, "ALI-EMP1", d))
    rows.extend(_full_day(1, "ALI-EMP1", "2026-05-15"))   # Fri WORKED (2nd)
    df = pd.DataFrame(rows)
    summary, _ = calculate_metrics(
        df, SCHEDULE,
        period_start="2026-05-03", period_end="2026-05-15",
    )
    row = summary["executive_employee_summary"].iloc[0]
    assert row["No of Absence Days"] == 0.0
    assert row["Friday Compensation Days"] == 1


def test_no_compensation_when_no_friday_worked():
    """Sanity: an employee with 1 weekday absence and no worked
    Friday keeps the 1.0 absence -- no free compensation."""
    rows = []
    rows.extend(_full_day(1, "ALI-EMP1", "2026-05-03"))   # Sun worked
    rows.extend(_full_day(1, "ALI-EMP1", "2026-05-04"))   # Mon worked
    rows.extend(_full_day(1, "ALI-EMP1", "2026-05-05"))   # Tue worked
    rows.extend(_full_day(1, "ALI-EMP1", "2026-05-06"))   # Wed worked
    # Thu 05-07 ABSENT
    # Fri 05-08 not worked (weekly off)
    df = pd.DataFrame(rows)
    summary, _ = calculate_metrics(
        df, SCHEDULE,
        period_start="2026-05-03", period_end="2026-05-08",
    )
    row = summary["executive_employee_summary"].iloc[0]
    assert row["No of Absence Days"] == 1.0
    assert row["Friday Compensation Days"] == 0


def test_friday_absence_alone_is_not_eligible_for_compensation():
    """When the default weekly off is Friday, a no-show Friday is NOT
    an absence in the first place. A worked Friday with no weekday
    absences => 0 compensation needed."""
    rows = []
    rows.extend(_full_day(1, "ALI-EMP1", "2026-05-03"))   # Sun worked
    rows.extend(_full_day(1, "ALI-EMP1", "2026-05-04"))   # Mon worked
    rows.extend(_full_day(1, "ALI-EMP1", "2026-05-05"))   # Tue worked
    rows.extend(_full_day(1, "ALI-EMP1", "2026-05-06"))   # Wed worked
    rows.extend(_full_day(1, "ALI-EMP1", "2026-05-07"))   # Thu worked
    rows.extend(_full_day(1, "ALI-EMP1", "2026-05-08"))   # Fri WORKED
    df = pd.DataFrame(rows)
    summary, _ = calculate_metrics(
        df, SCHEDULE,
        period_start="2026-05-03", period_end="2026-05-08",
    )
    row = summary["executive_employee_summary"].iloc[0]
    # No weekday absences -> no compensation reported.
    assert row["No of Absence Days"] == 0.0
    assert row["Friday Compensation Days"] == 0


def test_friday_compensation_does_not_change_vacation_permission_secondment():
    """Critical: the compensation rule must NOT touch Vacation /
    Permission / Secondment day counts -- those carry their own
    explicit time-off entries."""
    rows = []
    rows.extend(_full_day(1, "ALI-EMP1", "2026-05-03"))   # Sun worked
    rows.extend(_full_day(1, "ALI-EMP1", "2026-05-04"))   # Mon worked
    rows.extend(_full_day(1, "ALI-EMP1", "2026-05-05"))   # Tue worked
    # Wed 05-06 vacation (no punches; covered by Annual Leave entry)
    # Thu 05-07 ABSENT (no punches)
    rows.extend(_full_day(1, "ALI-EMP1", "2026-05-08"))   # Fri WORKED
    df = pd.DataFrame(rows)
    time_off = pd.DataFrame([{
        "Employee": "ALI-EMP1",
        "Time Off Type": "Annual Leave",
        "Start Date": "2026-05-06 00:00:00",
        "End Date":   "2026-05-06 23:59:59",
        "Status": "Approved",
    }])
    summary, _ = calculate_metrics(
        df, SCHEDULE, time_off,
        period_start="2026-05-03", period_end="2026-05-08",
    )
    row = summary["executive_employee_summary"].iloc[0]
    # Thu absence compensated, Wed vacation untouched.
    assert row["No of Absence Days"] == 0.0
    assert row["No of Vacation Days"] == 1
    assert row["Friday Compensation Days"] == 1


def test_absence_details_row_carries_friday_compensation_audit():
    """The Absence Details ledger marks the compensated row so HR can
    see WHICH weekday absence was swapped for WHICH Friday."""
    df = pd.DataFrame(_scenario_one_absence_one_worked_friday())
    summary, _ = calculate_metrics(
        df, SCHEDULE,
        period_start="2026-05-03", period_end="2026-05-08",
    )
    details = summary["absence_details"]
    thu = details[details["Date"] == "2026-05-07"].iloc[0]
    assert bool(thu["Friday Compensation"]) is True
    assert thu["Absence Day Value"] == 0.0
    assert "2026-05-08" in thu["Absence Reason"]
    assert "Gaming Friday Compensation" in thu["Absence Reason"]


def test_summary_log_records_per_employee_swap():
    """The pipeline emits a `friday_compensation_log` list of
    'employee_id=... compensated_absence_date=... friday_worked_date=...'
    entries so main.py can ship them straight to the log file."""
    df = pd.DataFrame(_scenario_one_absence_one_worked_friday())
    summary, _ = calculate_metrics(
        df, SCHEDULE,
        period_start="2026-05-03", period_end="2026-05-08",
    )
    log = summary["friday_compensation_log"]
    assert len(log) == 1
    msg = log[0]
    assert "employee_id=1" in msg
    assert "compensated_absence_date=2026-05-07" in msg
    assert "friday_worked_date=2026-05-08" in msg
