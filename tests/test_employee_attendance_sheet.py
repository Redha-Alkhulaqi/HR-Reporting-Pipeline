"""Employee Attendance audit-sheet tests.

The Employee Attendance sheet is purely a presentation layer over the
raw punch dataframe. It groups by (original Employee ID, Date) and
emits paired-shift columns so HR can review split-shift days at a
glance. These tests pin:

- the 11-column shape
- split-shift partitioning (morning vs evening punches)
- alias-aware grouping (raw ID preserved alongside canonical name)
- manual-correction tagging in the Source / Notes column
- single-shift backward compat (Shift 2 columns left empty)
"""
import pandas as pd

from excel_exporter import (
    _EMP_ATTENDANCE_COLS,
    build_employee_attendance_rows,
)


# -- helpers --------------------------------------------------------------

def _punch(eid, name, date, time, state, original_eid=None,
           id_alias_applied=False, is_manual_correction=False):
    return {
        "Employee ID": eid,
        "First Name": name,
        "Date": date,
        "Punch Time": time,
        "Punch State": state,
        "original_employee_id": (
            original_eid if original_eid is not None else eid
        ),
        "id_alias_applied": id_alias_applied,
        "is_manual_correction": is_manual_correction,
    }


SPLIT_SHIFT_LABEL = "شفت صباحى (9:00AM-1:00PM) & شفت مسائى (6:00PM-10:00PM)"


def _split_schedules():
    return pd.DataFrame([{
        "Name": "SAMEER-EMP1",
        "Working Time": SPLIT_SHIFT_LABEL,
    }])


def _single_schedules():
    return pd.DataFrame([{
        "Name": "SOLO-EMP1",
        "Working Time": "(8:00AM-5:00PM)",
    }])


# -- schema ---------------------------------------------------------------

def test_attendance_sheet_returns_expected_11_columns():
    df = pd.DataFrame([
        _punch(1, "SAMEER-EMP1", "2026-05-05", "09:05:00", "Check In"),
        _punch(1, "SAMEER-EMP1", "2026-05-05", "13:01:00", "Check Out"),
    ])
    out = build_employee_attendance_rows(df, _split_schedules())
    assert list(out.columns) == _EMP_ATTENDANCE_COLS
    assert len(_EMP_ATTENDANCE_COLS) == 11


def test_attendance_sheet_is_empty_when_no_punches():
    out = build_employee_attendance_rows(pd.DataFrame(), _split_schedules())
    assert list(out.columns) == _EMP_ATTENDANCE_COLS
    assert out.empty


# -- split-shift partitioning --------------------------------------------

def test_split_shift_both_intervals_one_row_per_day():
    df = pd.DataFrame([
        _punch(1, "SAMEER-EMP1", "2026-05-05", "09:05:00", "Check In"),
        _punch(1, "SAMEER-EMP1", "2026-05-05", "13:01:00", "Check Out"),
        _punch(1, "SAMEER-EMP1", "2026-05-05", "18:02:00", "Check In"),
        _punch(1, "SAMEER-EMP1", "2026-05-05", "22:01:00", "Check Out"),
    ])
    out = build_employee_attendance_rows(df, _split_schedules())
    assert len(out) == 1
    row = out.iloc[0]
    assert row["Shift 1 Check-In"] == "09:05"
    assert row["Shift 1 Check-Out"] == "13:01"
    assert row["Shift 2 Check-In"] == "18:02"
    assert row["Shift 2 Check-Out"] == "22:01"
    # 9:05->13:01 = 3:56; 18:02->22:01 = 3:59; total = 7:55.
    assert row["Total Time"] == "07:55"
    assert row["Source / Notes"] == "BioTime"


def test_split_shift_only_evening_leaves_shift1_empty():
    df = pd.DataFrame([
        _punch(1, "SAMEER-EMP1", "2026-05-04", "18:04:00", "Check In"),
        _punch(1, "SAMEER-EMP1", "2026-05-04", "22:01:00", "Check Out"),
    ])
    out = build_employee_attendance_rows(df, _split_schedules())
    row = out.iloc[0]
    assert row["Shift 1 Check-In"] == ""
    assert row["Shift 1 Check-Out"] == ""
    assert row["Shift 2 Check-In"] == "18:04"
    assert row["Shift 2 Check-Out"] == "22:01"
    assert row["Total Time"] == "03:57"


# -- alias-aware grouping ------------------------------------------------

def test_attendance_groups_by_original_id_keeping_canonical_name():
    """Two raw IDs (1017 and 4124537) that map to the same canonical
    employee should produce TWO rows for the same date, both showing
    the canonical Name. The aliased row says so in Source / Notes."""
    df = pd.DataFrame([
        # Aliased rows (raw ID = 1017, mapped to 4124537).
        _punch(4124537, "ABDUL SAMEER-EMP403", "2026-05-04",
               "09:05:00", "Check In",
               original_eid=1017, id_alias_applied=True),
        _punch(4124537, "ABDUL SAMEER-EMP403", "2026-05-04",
               "13:01:00", "Check Out",
               original_eid=1017, id_alias_applied=True),
        # Canonical rows (raw ID = 4124537, no alias).
        _punch(4124537, "ABDUL SAMEER-EMP403", "2026-05-04",
               "18:04:00", "Check In"),
        _punch(4124537, "ABDUL SAMEER-EMP403", "2026-05-04",
               "22:01:00", "Check Out"),
    ])
    out = build_employee_attendance_rows(df, pd.DataFrame([{
        "Name": "ABDUL SAMEER-EMP403",
        "Working Time": SPLIT_SHIFT_LABEL,
    }]))
    assert len(out) == 2  # one row per raw ID
    # Both rows carry the canonical name.
    assert (out["Canonical Employee Name"] == "ABDUL SAMEER-EMP403").all()
    # One row has Raw=1017 with "Aliased" tag, the other Raw=4124537.
    aliased = out[out["Raw Employee ID"] == 1017].iloc[0]
    canonical = out[out["Raw Employee ID"] == 4124537].iloc[0]
    assert "Aliased from 1017" in aliased["Source / Notes"]
    assert canonical["Source / Notes"] == "BioTime"
    # Aliased row holds the morning punches (08:00-15:30 split boundary).
    assert aliased["Shift 1 Check-In"] == "09:05"
    assert aliased["Shift 1 Check-Out"] == "13:01"
    assert canonical["Shift 2 Check-In"] == "18:04"
    assert canonical["Shift 2 Check-Out"] == "22:01"


# -- manual-correction tagging -------------------------------------------

def test_manual_correction_shows_in_source_notes():
    df = pd.DataFrame([
        _punch(1, "SAMEER-EMP1", "2026-05-05", "09:00:00", "Check In",
               is_manual_correction=True),
        _punch(1, "SAMEER-EMP1", "2026-05-05", "13:01:00", "Check Out"),
    ])
    out = build_employee_attendance_rows(df, _split_schedules())
    assert "Manual correction" in out.iloc[0]["Source / Notes"]


def test_aliased_and_manual_combine_in_notes():
    df = pd.DataFrame([
        _punch(4124537, "X-EMP1", "2026-05-05", "09:00:00", "Check In",
               original_eid=1017, id_alias_applied=True,
               is_manual_correction=True),
    ])
    out = build_employee_attendance_rows(df, _split_schedules())
    notes = out.iloc[0]["Source / Notes"]
    assert "Manual correction" in notes
    assert "Aliased from 1017" in notes


# -- single-shift backward compat ----------------------------------------

def test_single_shift_leaves_shift2_empty():
    df = pd.DataFrame([
        _punch(1, "SOLO-EMP1", "2026-05-05", "08:00:00", "Check In"),
        _punch(1, "SOLO-EMP1", "2026-05-05", "17:00:00", "Check Out"),
    ])
    out = build_employee_attendance_rows(df, _single_schedules())
    row = out.iloc[0]
    assert row["Shift 1 Check-In"] == "08:00"
    assert row["Shift 1 Check-Out"] == "17:00"
    assert row["Shift 2 Check-In"] == ""
    assert row["Shift 2 Check-Out"] == ""
    assert row["Total Time"] == "09:00"


def test_breaks_are_excluded_from_attendance_sheet():
    df = pd.DataFrame([
        _punch(1, "SOLO-EMP1", "2026-05-05", "08:00:00", "Check In"),
        _punch(1, "SOLO-EMP1", "2026-05-05", "12:00:00", "Break Out"),
        _punch(1, "SOLO-EMP1", "2026-05-05", "12:30:00", "Break In"),
        _punch(1, "SOLO-EMP1", "2026-05-05", "17:00:00", "Check Out"),
    ])
    out = build_employee_attendance_rows(df, _single_schedules())
    # The Break In/Out punches must not appear as Shift 2 entries.
    row = out.iloc[0]
    assert row["Shift 2 Check-In"] == ""
    assert row["Shift 2 Check-Out"] == ""


def test_duplicate_subsecond_punches_collapse_via_minmax():
    """Devices sometimes emit two events 1 second apart for the same
    physical punch. The first/last min/max collapse should make the
    sheet display a clean single value per cell."""
    df = pd.DataFrame([
        _punch(1, "SAMEER-EMP1", "2026-05-05", "09:08:54", "Check In"),
        _punch(1, "SAMEER-EMP1", "2026-05-05", "09:08:55", "Check In"),
        _punch(1, "SAMEER-EMP1", "2026-05-05", "13:01:14", "Check Out"),
        _punch(1, "SAMEER-EMP1", "2026-05-05", "13:01:15", "Check Out"),
    ])
    out = build_employee_attendance_rows(df, _split_schedules())
    row = out.iloc[0]
    assert row["Shift 1 Check-In"] == "09:08"   # min of the two CIs
    assert row["Shift 1 Check-Out"] == "13:01"  # max of the two COs
