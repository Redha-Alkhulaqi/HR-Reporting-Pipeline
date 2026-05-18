"""Centralized overtime payroll-multiplier tests.

The pipeline applies a global `OVERTIME_PAY_MULTIPLIER` (default 1.5)
in `_apply_overtime_payroll_adjustment` AFTER every overtime
classifier. These tests pin down:

- the per-row `overtime_payable_minutes` / `overtime_payable_hours` /
  `overtime_multiplier` audit columns;
- the summary-level `total_overtime_payable_minutes` /
  `total_overtime_payable_hours` / `overtime_multiplier` fields;
- half-up rounding (so 1:30 raw becomes 2:15 payable);
- raw fields are preserved unchanged (backward compat);
- the multiplier applies after the TOTAL_SPAN_MINUS_8H policy too,
  with no double-counting.
"""
import pandas as pd

from metrics_calculator import (
    OVERTIME_POLICY_TOTAL_SPAN_MINUS_8H,
    _apply_overtime_payroll_adjustment,
    _round_half_up,
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


def _schedule(name, label):
    return pd.DataFrame([{"Name": name, "Working Time": label}])


def _override(emp_id, standard="08:00", active=True):
    return pd.DataFrame([{
        "Employee ID": emp_id,
        "Employee Name": "PLACEHOLDER",
        "Policy Type": "TOTAL_SPAN_MINUS_8H",
        "Standard Hours": standard,
        "Active": active,
        "Notes": "",
    }])


# -- _round_half_up -------------------------------------------------------

def test_round_half_up_basic():
    assert _round_half_up(0.4) == 0
    assert _round_half_up(0.5) == 1
    assert _round_half_up(1.5) == 2
    assert _round_half_up(2.5) == 3   # half-up beats banker's (Python would give 2)
    assert _round_half_up(4.5) == 5   # half-up beats banker's (Python would give 4)
    assert _round_half_up(67.5) == 68
    assert _round_half_up(0) == 0


# -- _apply_overtime_payroll_adjustment (unit) ----------------------------

def test_helper_adds_columns_and_uses_default_multiplier():
    df = pd.DataFrame({"overtime_minutes": [0, 30, 60, 90, 120]})
    out = _apply_overtime_payroll_adjustment(df.copy())
    # Raw column preserved.
    assert list(out["overtime_minutes"]) == [0, 30, 60, 90, 120]
    # Payable = raw * 1.5, half-up.
    assert list(out["overtime_payable_minutes"]) == [0, 45, 90, 135, 180]
    # Hours are minutes / 60 rounded to 1dp.
    assert list(out["overtime_payable_hours"]) == [0.0, 0.8, 1.5, 2.2, 3.0]
    # Multiplier surfaced per row.
    assert (out["overtime_multiplier"] == 1.5).all()


def test_helper_honours_explicit_multiplier_arg():
    df = pd.DataFrame({"overtime_minutes": [60, 120]})
    out = _apply_overtime_payroll_adjustment(df.copy(), multiplier=1.25)
    assert list(out["overtime_payable_minutes"]) == [75, 150]
    assert (out["overtime_multiplier"] == 1.25).all()


def test_helper_is_noop_when_overtime_column_missing():
    df = pd.DataFrame({"x": [1, 2]})
    out = _apply_overtime_payroll_adjustment(df.copy())
    assert "overtime_payable_minutes" not in out.columns


# -- end-to-end: standard employee + multiplier ---------------------------

def test_standard_2h_overtime_pays_3h():
    """Spec example: 2:00 raw -> 3:00 payable."""
    df = pd.DataFrame(_punches(
        1, "REGULAR-EMP1", "2026-05-01", "08:00:00", "19:00:00",
    ))
    schedules = _schedule("REGULAR-EMP1", "(8:00AM-5:00PM)")  # 8h shift
    summary, daily = calculate_metrics(df, schedules)
    row = daily.iloc[0]
    # Raw matched-interval overtime: 19:00 - 17:00 = 120 min.
    assert row["overtime_minutes"] == 120
    # Payable: 120 * 1.5 = 180 min = 3.0 h.
    assert row["overtime_payable_minutes"] == 180
    assert row["overtime_payable_hours"] == 3.0
    assert row["overtime_multiplier"] == 1.5
    # Summary fields.
    assert summary["overtime_multiplier"] == 1.5
    assert summary["total_overtime_minutes"] == 120        # raw preserved
    assert summary["total_overtime_hours"] == 2.0          # raw preserved
    assert summary["total_overtime_payable_minutes"] == 180
    assert summary["total_overtime_payable_hours"] == 3.0


def test_fractional_overtime_uses_half_up_rounding():
    """Spec example: 1:30 raw -> 2:15 payable."""
    # 17:00 shift end, 18:30 check-out -> 90 min raw overtime.
    df = pd.DataFrame(_punches(
        1, "REGULAR-EMP1", "2026-05-01", "08:00:00", "18:30:00",
    ))
    schedules = _schedule("REGULAR-EMP1", "(8:00AM-5:00PM)")
    _, daily = calculate_metrics(df, schedules)
    row = daily.iloc[0]
    assert row["overtime_minutes"] == 90
    # 90 * 1.5 = 135 min = 2h15.
    assert row["overtime_payable_minutes"] == 135
    assert row["overtime_payable_hours"] == 2.2  # 135/60 = 2.25 -> 2.2 (banker's via Series.round; sanity-check)


def test_zero_overtime_zero_payable():
    """On Time employee leaves at shift end -> no overtime, no payable."""
    df = pd.DataFrame(_punches(
        1, "REGULAR-EMP1", "2026-05-01", "08:00:00", "17:00:00",
    ))
    schedules = _schedule("REGULAR-EMP1", "(8:00AM-5:00PM)")
    _, daily = calculate_metrics(df, schedules)
    row = daily.iloc[0]
    assert row["overtime_minutes"] == 0
    assert row["overtime_payable_minutes"] == 0
    assert row["overtime_payable_hours"] == 0.0
    assert row["overtime_multiplier"] == 1.5  # still surfaced for auditors


# -- end-to-end: TOTAL_SPAN_MINUS_8H + multiplier -------------------------

def test_total_span_policy_then_multiplier_3h30_to_5h15():
    """EMP410 spec example with multiplier: 3h30 raw -> 5h15 payable."""
    df = pd.DataFrame(_punches(
        4125249, "ALAA-EMP410", "2026-05-01", "08:00:00", "19:30:00",
    ))
    # His Odoo shift exists but the policy bypasses it; we still ship a
    # plausible schedule so the row has shift metadata for downstream.
    schedules = _schedule("ALAA-EMP410", "(10:00AM-6:00PM)")
    _, daily = calculate_metrics(
        df, schedules,
        overtime_policy_overrides_df=_override(4125249),
    )
    row = daily.iloc[0]
    # Policy raw: 11h30 span - 8h standard = 3h30 = 210 min.
    assert row["overtime_minutes"] == 210
    assert row["overtime_policy"] == OVERTIME_POLICY_TOTAL_SPAN_MINUS_8H
    # Multiplier still applies AFTER the policy classifier.
    assert row["overtime_payable_minutes"] == 315          # 210 * 1.5
    assert row["overtime_payable_hours"] == 5.2            # 315/60 = 5.25 -> 5.2 banker's via Series.round
    assert row["overtime_multiplier"] == 1.5


def test_multiplier_does_not_double_count_against_policy():
    """The policy classifier must not contain any 1.5 logic itself.
    A 0-overtime span must stay 0 after the multiplier."""
    # 6h span < 8h standard -> 0 raw overtime.
    df = pd.DataFrame(_punches(
        4125249, "ALAA-EMP410", "2026-05-01", "10:00:00", "16:00:00",
    ))
    schedules = _schedule("ALAA-EMP410", "(10:00AM-6:00PM)")
    _, daily = calculate_metrics(
        df, schedules,
        overtime_policy_overrides_df=_override(4125249),
    )
    row = daily.iloc[0]
    assert row["overtime_minutes"] == 0
    assert row["overtime_payable_minutes"] == 0


# -- backward compatibility ----------------------------------------------

def test_raw_overtime_fields_preserved_alongside_payable():
    """Backward compat: every raw overtime field that existed before
    the multiplier release still exists, still carries the physical
    duration, and the payable fields run in parallel without
    mutating the raw side.
    """
    df = pd.DataFrame(_punches(
        1, "REGULAR-EMP1", "2026-05-01", "08:00:00", "19:30:00",
    ))
    schedules = _schedule("REGULAR-EMP1", "(8:00AM-5:00PM)")
    summary, daily = calculate_metrics(df, schedules)

    # Raw side (every prior consumer keeps working).
    expected_raw_keys = {
        "overtime_cases", "total_overtime_minutes", "total_overtime_hours",
        "employees_with_overtime", "avg_overtime_minutes",
    }
    assert expected_raw_keys.issubset(summary.keys())
    assert summary["total_overtime_minutes"] == 150        # 19:30 - 17:00
    assert summary["total_overtime_hours"] == 2.5          # raw
    assert "overtime_minutes" in daily.columns
    assert int(daily.iloc[0]["overtime_minutes"]) == 150   # raw, never mutated

    # Payable side runs in parallel without changing raw.
    expected_payable_keys = {
        "overtime_multiplier",
        "total_overtime_payable_minutes",
        "total_overtime_payable_hours",
    }
    assert expected_payable_keys.issubset(summary.keys())
    assert summary["total_overtime_payable_minutes"] == 225   # 150 * 1.5
    assert summary["total_overtime_payable_hours"] == 3.8     # 225/60 ~ 3.75 -> banker's 3.8
    assert summary["overtime_multiplier"] == 1.5


def test_explicit_multiplier_of_1_disables_premium():
    """Passing multiplier=1.0 to the centralized helper yields
    payable == raw exactly (so HR can revert the premium without
    touching code by setting OVERTIME_PAY_MULTIPLIER=1.0).
    """
    df = pd.DataFrame({"overtime_minutes": [0, 30, 90, 120, 210]})
    out = _apply_overtime_payroll_adjustment(df.copy(), multiplier=1.0)
    assert list(out["overtime_payable_minutes"]) == [0, 30, 90, 120, 210]
    assert (out["overtime_multiplier"] == 1.0).all()
