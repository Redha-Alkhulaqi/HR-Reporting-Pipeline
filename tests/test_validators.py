import pandas as pd
import pytest

from validators import (
    ValidationError,
    validate_attendance,
    validate_schedules,
    validate_time_off,
)


def _attendance_df(rows=None):
    return pd.DataFrame(rows or [
        {"Employee ID": 1, "First Name": "ALI", "Date": "2026-05-01",
         "Punch Time": "08:00:00", "Punch State": "Check In"},
    ])


def test_empty_attendance_raises():
    with pytest.raises(ValidationError):
        validate_attendance(pd.DataFrame())


def test_missing_required_column_raises():
    with pytest.raises(ValidationError):
        validate_attendance(pd.DataFrame([{"Employee ID": 1}]))


def test_invalid_punch_state_warns():
    report = validate_attendance(_attendance_df([
        {"Employee ID": 1, "First Name": "ALI", "Date": "2026-05-01",
         "Punch Time": "08:00:00", "Punch State": "Coffee Break"},
    ]))
    assert any("Punch State" in w for w in report.warnings)


def test_invalid_date_warns():
    report = validate_attendance(_attendance_df([
        {"Employee ID": 1, "First Name": "ALI", "Date": "not-a-date",
         "Punch Time": "08:00:00", "Punch State": "Check In"},
    ]))
    assert any("Date" in w for w in report.warnings)


def test_duplicate_rows_warns():
    row = {"Employee ID": 1, "First Name": "ALI", "Date": "2026-05-01",
           "Punch Time": "08:00:00", "Punch State": "Check In"}
    report = validate_attendance(pd.DataFrame([row, row]))
    assert any("duplicate" in w.lower() for w in report.warnings)


def test_schedules_missing_columns_raises():
    with pytest.raises(ValidationError):
        validate_schedules(pd.DataFrame([{"Name": "ALI"}]))


def test_schedules_blank_working_time_warns():
    df = pd.DataFrame([{"Name": "ALI", "Working Time": None}])
    report = validate_schedules(df)
    assert any("Working Time" in w for w in report.warnings)


def test_time_off_none_is_ok():
    report = validate_time_off(None)
    assert report.row_count == 0
    assert report.warnings == []


def test_time_off_unknown_status_warns():
    df = pd.DataFrame([{
        "Employee": "ALI", "Status": "Pending Review",
        "Start Date": "2026-05-01", "End Date": "2026-05-02",
        "Time Off Type": "Annual",
    }])
    report = validate_time_off(df)
    assert any("Status" in w for w in report.warnings)
