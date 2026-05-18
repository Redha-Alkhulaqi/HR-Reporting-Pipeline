"""Overtime policy override tests.

The pipeline supports per-employee overtime policy overrides driven
by `data/overtime_policy_overrides.xlsx`. These tests pin down the
new TOTAL_SPAN_MINUS_8H policy, the audit columns it adds to every
daily row, and the guarantee that employees WITHOUT an override fall
through to the unchanged standard matched-interval logic.
"""
import pandas as pd

from metrics_calculator import (
    OVERTIME_POLICY_STANDARD,
    OVERTIME_POLICY_TOTAL_SPAN_MINUS_8H,
    _parse_standard_hours_to_minutes,
    build_overtime_policy_overrides,
    calculate_metrics,
)


# -- helpers --------------------------------------------------------------

def _punches(emp_id, name, date, check_in, check_out):
    rows = [
        {"Employee ID": emp_id, "First Name": name,
         "Date": date, "Punch Time": check_in, "Punch State": "Check In"},
    ]
    if check_out is not None:
        rows.append({
            "Employee ID": emp_id, "First Name": name,
            "Date": date, "Punch Time": check_out, "Punch State": "Check Out",
        })
    return rows


def _schedule(name="ALAA-EMP410", label="(10:00AM-6:00PM)"):
    return pd.DataFrame([{"Name": name, "Working Time": label}])


def _override(emp_id=4125249, policy="TOTAL_SPAN_MINUS_8H",
              standard="08:00", active=True):
    return pd.DataFrame([{
        "Employee ID": emp_id,
        "Employee Name": "ALAA-EMP410",
        "Policy Type": policy,
        "Standard Hours": standard,
        "Active": active,
        "Notes": "",
    }])


# -- _parse_standard_hours_to_minutes ------------------------------------

def test_parse_standard_hours_handles_colon_format():
    assert _parse_standard_hours_to_minutes("08:00") == 480
    assert _parse_standard_hours_to_minutes("8:30") == 510
    assert _parse_standard_hours_to_minutes("9:00") == 540


def test_parse_standard_hours_handles_plain_numbers():
    assert _parse_standard_hours_to_minutes("8") == 480
    assert _parse_standard_hours_to_minutes("8.5") == 510
    assert _parse_standard_hours_to_minutes(8) == 480
    assert _parse_standard_hours_to_minutes(8.5) == 510


def test_parse_standard_hours_defaults_when_blank():
    assert _parse_standard_hours_to_minutes(None) == 480
    assert _parse_standard_hours_to_minutes("") == 480
    assert _parse_standard_hours_to_minutes(float("nan")) == 480


# -- build_overtime_policy_overrides --------------------------------------

def test_build_overtime_policy_overrides_basic():
    overrides_df = _override(emp_id=42, standard="08:00", active=True)
    result, warnings = build_overtime_policy_overrides(overrides_df)
    assert warnings == []
    assert 42 in result
    assert result[42]["type"] == OVERTIME_POLICY_TOTAL_SPAN_MINUS_8H
    assert result[42]["standard_minutes"] == 480
    assert "8h00" in result[42]["note"]


def test_build_overtime_policy_overrides_drops_inactive_rows():
    overrides_df = _override(emp_id=42, active=False)
    result, warnings = build_overtime_policy_overrides(overrides_df)
    assert result == {}
    assert warnings == []


def test_build_overtime_policy_overrides_rejects_unknown_policy():
    overrides_df = _override(emp_id=42, policy="MAGIC_POLICY")
    result, warnings = build_overtime_policy_overrides(overrides_df)
    assert result == {}
    assert any("unsupported policy" in w for w in warnings)


def test_build_overtime_policy_overrides_first_duplicate_wins():
    overrides_df = pd.concat([
        _override(emp_id=42, standard="08:00"),
        _override(emp_id=42, standard="09:00"),
    ], ignore_index=True)
    result, warnings = build_overtime_policy_overrides(overrides_df)
    assert result[42]["standard_minutes"] == 480
    assert any("duplicate" in w for w in warnings)


def test_build_overtime_policy_overrides_empty_input():
    result, warnings = build_overtime_policy_overrides(None)
    assert result == {} and warnings == []
    result, warnings = build_overtime_policy_overrides(pd.DataFrame())
    assert result == {} and warnings == []


# -- end-to-end: TOTAL_SPAN_MINUS_8H policy ------------------------------

def test_policy_overtime_3h30_when_span_is_11h30():
    """Example from the spec: in 08:00, out 19:30, standard 8h -> 3h30."""
    df = pd.DataFrame(_punches(
        4125249, "ALAA-EMP410", "2026-05-01", "08:00:00", "19:30:00",
    ))
    summary, daily = calculate_metrics(
        df, _schedule(),
        overtime_policy_overrides_df=_override(),
    )
    row = daily.iloc[0]
    assert row["overtime_policy"] == OVERTIME_POLICY_TOTAL_SPAN_MINUS_8H
    assert row["overtime_minutes"] == 210  # 11h30 - 8h = 3h30 = 210 min
    assert row["overtime_status"] == "Overtime"
    assert "minus 8h" in row["overtime_calculation_note"]
    assert summary["total_overtime_minutes"] == 210


def test_policy_overtime_floors_to_zero_when_span_below_standard():
    df = pd.DataFrame(_punches(
        4125249, "ALAA-EMP410", "2026-05-01", "09:00:00", "15:00:00",
    ))
    summary, daily = calculate_metrics(
        df, _schedule(),
        overtime_policy_overrides_df=_override(),
    )
    row = daily.iloc[0]
    assert row["overtime_minutes"] == 0
    assert row["overtime_status"] == "No Overtime"
    assert row["overtime_policy"] == OVERTIME_POLICY_TOTAL_SPAN_MINUS_8H


def test_policy_overtime_respects_custom_standard_hours():
    """Same 11h30 span but standard set to 9h -> overtime 2h30."""
    df = pd.DataFrame(_punches(
        4125249, "ALAA-EMP410", "2026-05-01", "08:00:00", "19:30:00",
    ))
    summary, daily = calculate_metrics(
        df, _schedule(),
        overtime_policy_overrides_df=_override(standard="09:00"),
    )
    assert daily.iloc[0]["overtime_minutes"] == 150  # 11h30 - 9h


def test_policy_missing_check_out_still_reports_missing():
    df = pd.DataFrame(_punches(
        4125249, "ALAA-EMP410", "2026-05-01", "08:00:00", None,
    ))
    _, daily = calculate_metrics(
        df, _schedule(),
        overtime_policy_overrides_df=_override(),
    )
    row = daily.iloc[0]
    assert row["overtime_status"] == "Missing Check Out"
    assert row["overtime_minutes"] == 0


def test_policy_bypasses_missing_schedule():
    """Selected employees may not need a real schedule. The policy
    still computes overtime from the punches alone."""
    df = pd.DataFrame(_punches(
        4125249, "GHOST-EMP999", "2026-05-01", "08:00:00", "19:30:00",
    ))
    schedules = pd.DataFrame([
        {"Name": "SOMEONE ELSE-EMP1", "Working Time": "(8:00AM-5:00PM)"},
    ])
    _, daily = calculate_metrics(
        df, schedules,
        overtime_policy_overrides_df=_override(emp_id=4125249),
    )
    row = daily.iloc[0]
    # daily attendance still says missing_schedule because no shift
    # was resolved, but the overtime block honors the policy.
    assert bool(row["missing_schedule"]) is True
    assert row["overtime_policy"] == OVERTIME_POLICY_TOTAL_SPAN_MINUS_8H
    assert row["overtime_minutes"] == 210


# -- standard employees stay on the unchanged path -----------------------

def test_standard_employee_unchanged_when_overrides_loaded():
    """A non-overridden employee must keep the standard matched-
    interval overtime semantics, even when the overrides file is
    populated for someone else.
    """
    df = pd.DataFrame(_punches(
        1, "REGULAR-EMP1", "2026-05-01", "08:00:00", "19:30:00",
    ))
    schedules = pd.DataFrame([
        {"Name": "REGULAR-EMP1", "Working Time": "(8:00AM-5:00PM)"},
    ])
    _, daily = calculate_metrics(
        df, schedules,
        overtime_policy_overrides_df=_override(emp_id=4125249),
    )
    row = daily.iloc[0]
    assert row["overtime_policy"] == OVERTIME_POLICY_STANDARD
    # Standard: matched interval ends 17:00; check-out 19:30 -> 150 min.
    assert row["overtime_minutes"] == 150


def test_audit_columns_present_when_no_override_file():
    """Even without an overrides file, every daily row carries the new
    audit columns so consumers can assume they exist."""
    df = pd.DataFrame(_punches(
        1, "REGULAR-EMP1", "2026-05-01", "08:05:00", "17:00:00",
    ))
    schedules = pd.DataFrame([
        {"Name": "REGULAR-EMP1", "Working Time": "(8:00AM-5:00PM)"},
    ])
    _, daily = calculate_metrics(df, schedules)
    row = daily.iloc[0]
    assert "overtime_policy" in daily.columns
    assert "overtime_calculation_note" in daily.columns
    assert row["overtime_policy"] == OVERTIME_POLICY_STANDARD
    assert "matched-interval" in row["overtime_calculation_note"]
