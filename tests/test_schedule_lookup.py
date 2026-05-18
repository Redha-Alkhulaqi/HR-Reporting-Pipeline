"""Schedule-matching regression tests.

Covers the Odoo-export quirks that previously surfaced as Missing
Schedule even when the employee had a Working Time in Odoo: NBSP
inside the name, EMP-code-only matching, and alternate label columns
(Working Hours / Resource Calendar).
"""
import pandas as pd

from metrics_calculator import (
    ScheduleLookup,
    _extract_emp_code,
    _strip_emp_code,
    _strong_normalize,
    calculate_metrics,
)


def test_extract_emp_code_variants():
    assert _extract_emp_code("MOHAMMED LAHIQ ALMUTAIRI-EMP420") == "EMP420"
    assert _extract_emp_code("Mohammed Lahiq Almutairi emp 420") == "EMP420"
    assert _extract_emp_code("ALI-emp_99") == "EMP99"
    assert _extract_emp_code("No code here") is None
    assert _extract_emp_code(None) is None


def test_strip_emp_code():
    assert (
        _strip_emp_code("MOHAMMED LAHIQ ALMUTAIRI-EMP420")
        == "MOHAMMED LAHIQ ALMUTAIRI"
    )
    assert _strip_emp_code("ALI EMP 12") == "ALI"


def test_strong_normalize_collapses_nbsp():
    # NBSP between MOHAMMED and LAHIQ must collapse to a single space.
    assert (
        _strong_normalize("MOHAMMED\xa0LAHIQ ALMUTAIRI-EMP420")
        == "MOHAMMED LAHIQ ALMUTAIRI-EMP420"
    )


def test_schedule_lookup_matches_via_emp_code_when_names_differ():
    # Attendance carries the BioTime name, schedule carries an Odoo
    # variant with an NBSP inside it.
    schedules = pd.DataFrame([
        {"Name": "MOHAMMED\xa0LAHIQ ALMUTAIRI-EMP420",
         "Working Time": "دوام صباحى (9:00AM-6:00PM)"},
    ])
    lookup = ScheduleLookup(schedules)
    result = lookup.match("MOHAMMED LAHIQ ALMUTAIRI-EMP420")
    assert result["matched_by"] == "emp_code"
    assert result["intervals"] == [("09:00", "18:00")]
    assert result["matched_name"] == "MOHAMMED\xa0LAHIQ ALMUTAIRI-EMP420"


def test_schedule_lookup_exact_normalized_when_no_emp_code():
    schedules = pd.DataFrame([
        {"Name": " ALI  HASSAN ", "Working Time": "(8:00AM-5:00PM)"},
    ])
    result = ScheduleLookup(schedules).match("ali hassan")
    assert result["matched_by"] == "exact_normalized"
    assert result["intervals"] == [("08:00", "17:00")]


def test_schedule_lookup_stripped_emp_match():
    # Schedule has the EMP code, attendance does not.
    schedules = pd.DataFrame([
        {"Name": "OMAR SAID-EMP777", "Working Time": "(8:00AM-5:00PM)"},
    ])
    result = ScheduleLookup(schedules).match("OMAR SAID")
    assert result["matched_by"] == "stripped_emp"
    assert result["intervals"] == [("08:00", "17:00")]


def test_schedule_lookup_returns_empty_for_no_match():
    schedules = pd.DataFrame([
        {"Name": "ALI-EMP1", "Working Time": "(8:00AM-5:00PM)"},
    ])
    result = ScheduleLookup(schedules).match("UNKNOWN-EMP999")
    assert result["matched_by"] == ""
    assert result["intervals"] == []


def test_schedule_lookup_alternate_label_column():
    schedules = pd.DataFrame([
        {"Name": "ALI-EMP1", "Working Hours": "(8:00AM-5:00PM)"},
    ])
    lookup = ScheduleLookup(schedules, label_column="Working Hours")
    result = lookup.match("ALI-EMP1")
    assert result["intervals"] == [("08:00", "17:00")]


def test_calculate_metrics_emp420_nbsp_regression():
    """The reported bug: EMP420 has an NBSP in the Odoo Name. Prior to
    this fix he showed as Missing Schedule; now he must match.
    """
    df = pd.DataFrame([
        {"Employee ID": 4117328,
         "First Name": "MOHAMMED LAHIQ ALMUTAIRI-EMP420",
         "Date": "2026-05-01", "Punch Time": "09:05:00",
         "Punch State": "Check In"},
        {"Employee ID": 4117328,
         "First Name": "MOHAMMED LAHIQ ALMUTAIRI-EMP420",
         "Date": "2026-05-01", "Punch Time": "18:00:00",
         "Punch State": "Check Out"},
    ])
    schedules = pd.DataFrame([{
        "Name": "MOHAMMED\xa0LAHIQ ALMUTAIRI-EMP420",
        "Working Time": "دوام صباحى (9:00AM-6:00PM)",
    }])
    summary, daily = calculate_metrics(df, schedules)
    assert summary["missing_schedule_cases"] == 0
    assert (daily["attendance_status"] != "Missing Schedule").all()
    assert daily.iloc[0]["matched_by"] == "emp_code"

    audit = summary["schedule_lookup_audit"]
    assert len(audit) == 1
    row = audit.iloc[0]
    assert row["matched_by"] == "emp_code"
    assert bool(row["missing_schedule"]) is False
    assert row["shift_start"] == "09:00"
    assert row["shift_end"] == "18:00"
