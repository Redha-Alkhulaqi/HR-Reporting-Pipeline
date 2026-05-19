"""Split-shift manual punch correction tests.

The old correction engine rejected any manual punch whose state +
date already existed on the attendance frame. That broke split-shift
employees who legitimately need TWO Check Ins and TWO Check Outs per
day. The new model lets manual corrections APPEND alongside existing
same-state punches by default, and the downstream interval-aware
overtime/absence engines treat the multi-interval day correctly.

These tests pin:
- single-shift backward compat (append works, but min/max collapses
  cleanly so overtime stays sensible);
- split-shift append (4 events per day end up in the frame);
- end-to-end overtime calculation respects the appended intervals;
- end-to-end absence engine no longer flags the split-shift day as
  partial after the missing punches are filled in via corrections;
- exact-duplicate corrections stay no-op;
- the allow_override flag still REPLACES existing same-state punches.
"""
import pandas as pd

from manual_punch_corrections import apply_manual_punch_corrections
from metrics_calculator import calculate_metrics


# -- helpers --------------------------------------------------------------

def _punch(emp_id, name, date, time, state):
    return {
        "Employee ID": emp_id, "First Name": name,
        "Date": date, "Punch Time": time, "Punch State": state,
    }


def _correction(emp_id, date, ptype, corrected_time,
                approval="approved", evidence="camera",
                reason="forgot to punch", verifier="HR Admin"):
    return {
        "employee_code": emp_id,
        "employee_name": "n/a",
        "date": date,
        "punch_type": ptype,
        "corrected_time": corrected_time,
        "evidence_type": evidence,
        "approval_status": approval,
        "correction_reason": reason,
        "correction_verified_by": verifier,
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


# -- engine-level: append + duplicate + override -------------------------

def test_split_shift_evening_pair_appends_to_morning_pair():
    """The reported bug: BioTime captured the morning interval but the
    evening Check In + Check Out are missing. HR adds two manual
    corrections for the evening shift; both must land in the frame
    alongside the existing morning pair."""
    df = pd.DataFrame([
        _punch(4124537, "ABDUL SAMEER-EMP403", "2026-05-04",
               "09:03:00", "Check In"),
        _punch(4124537, "ABDUL SAMEER-EMP403", "2026-05-04",
               "13:20:00", "Check Out"),
    ])
    corrections = pd.DataFrame([
        _correction(4124537, "2026-05-04", "check_in", "18:03:00"),
        _correction(4124537, "2026-05-04", "check_out", "22:00:00"),
    ])
    out, rejected = apply_manual_punch_corrections(df, corrections)

    assert rejected.empty, (
        "Manual corrections must not be rejected for split-shift "
        f"second-interval inserts; got {rejected.to_dict('records')}"
    )
    # 2 BioTime + 2 manual = 4 events on this day.
    day = out[out["Date"] == "2026-05-04"]
    assert len(day) == 4
    cis = day[day["Punch State"] == "Check In"].sort_values("Punch Time")
    cos = day[day["Punch State"] == "Check Out"].sort_values("Punch Time")
    assert cis["Punch Time"].tolist() == ["09:03:00", "18:03:00"]
    assert cos["Punch Time"].tolist() == ["13:20:00", "22:00:00"]
    # Audit: the evening pair is marked appended; the morning pair is BioTime.
    manual_rows = day[day["is_manual_correction"]]
    assert (manual_rows["correction_action"] == "appended").all()


def test_morning_check_out_correction_appends_alongside_evening_check_out():
    """Inverse case: BioTime captured the evening CO at 22:00 but the
    morning CO at 13:20 is missing. HR's check_out correction at 13:20
    must APPEND alongside the existing 22:00, not be rejected."""
    df = pd.DataFrame([
        _punch(4124537, "ABDUL SAMEER-EMP403", "2026-05-04",
               "09:03:00", "Check In"),
        _punch(4124537, "ABDUL SAMEER-EMP403", "2026-05-04",
               "18:03:00", "Check In"),
        _punch(4124537, "ABDUL SAMEER-EMP403", "2026-05-04",
               "22:00:00", "Check Out"),
    ])
    corrections = pd.DataFrame([
        _correction(4124537, "2026-05-04", "check_out", "13:20:00"),
    ])
    out, rejected = apply_manual_punch_corrections(df, corrections)
    assert rejected.empty
    cos = out[out["Punch State"] == "Check Out"].sort_values("Punch Time")
    assert cos["Punch Time"].tolist() == ["13:20:00", "22:00:00"]


def test_single_shift_correction_still_appends_when_no_existing_punch():
    """Backward compat for the bread-and-butter case: employee
    forgot to clock in for a single 8-5 shift; the manual correction
    adds the missing Check In cleanly."""
    df = pd.DataFrame([
        _punch(1, "SOLO-EMP1", "2026-05-04", "17:00:00", "Check Out"),
    ])
    corrections = pd.DataFrame([
        _correction(1, "2026-05-04", "check_in", "08:00:00"),
    ])
    out, rejected = apply_manual_punch_corrections(df, corrections)
    assert rejected.empty
    cis = out[out["Punch State"] == "Check In"]
    assert len(cis) == 1
    assert cis.iloc[0]["Punch Time"] == "08:00:00"
    assert cis.iloc[0]["correction_action"] == "added"


def test_allow_override_still_replaces_same_state_punch():
    """The legacy override semantics survive: with allow_override=True
    the manual punch REPLACES the existing same-state punch instead of
    appending."""
    df = pd.DataFrame([
        _punch(1, "X-EMP1", "2026-05-04", "08:30:00", "Check In"),
    ])
    corrections = pd.DataFrame([
        _correction(1, "2026-05-04", "check_in", "08:00:00"),
    ])
    out, rejected = apply_manual_punch_corrections(
        df, corrections, allow_override=True,
    )
    cis = out[out["Punch State"] == "Check In"]
    assert len(cis) == 1                      # replaced, not appended
    assert cis.iloc[0]["Punch Time"] == "08:00:00"
    assert cis.iloc[0]["correction_action"] == "overridden"


# -- end-to-end: overtime + absence after split-shift corrections --------

def test_end_to_end_overtime_picks_up_appended_evening_interval():
    """After HR adds the missing evening CI + CO via corrections, the
    interval-aware overtime engine sees both pairs and produces a
    sensible overtime figure rather than the broken
    09:03 -> 22:00 single-span the old behavior implied."""
    df = pd.DataFrame([
        _punch(4124537, "SAMEER-EMP1", "2026-05-04",
               "09:03:00", "Check In"),
        _punch(4124537, "SAMEER-EMP1", "2026-05-04",
               "13:20:00", "Check Out"),
    ])
    corrections = pd.DataFrame([
        _correction(4124537, "2026-05-04", "check_in", "18:03:00"),
        _correction(4124537, "2026-05-04", "check_out", "22:30:00"),
    ])
    df, _ = apply_manual_punch_corrections(df, corrections)
    summary, daily = calculate_metrics(df, _split_schedules())

    row = daily.iloc[0]
    # Morning interval ends at 13:00; evening ends at 22:00. Check Ins
    # use min() so Shift Start is 09:03; matched-interval logic picks
    # the segment containing 22:30 -> evening interval. 22:30 vs end
    # 22:00 = 30 min overtime (above the 15-min grace + 30-min floor).
    assert row["overtime_minutes"] == 30
    assert row["overtime_status"] == "Overtime"


def test_end_to_end_absence_engine_no_longer_partial_after_corrections():
    """Before: evening CI missing -> absence engine reports 0.5 day
    partial absence (only morning interval attended).
    After: HR adds the evening CI via a manual correction -> both
    intervals attended -> 0.0 absence day value."""
    df = pd.DataFrame([
        _punch(4124537, "SAMEER-EMP1", "2026-05-05",
               "09:03:00", "Check In"),
        _punch(4124537, "SAMEER-EMP1", "2026-05-05",
               "13:20:00", "Check Out"),
        _punch(4124537, "SAMEER-EMP1", "2026-05-05",
               "22:00:00", "Check Out"),  # late CO but no evening CI
    ])
    # Without the correction, the morning interval is attended (CI 09:03)
    # but the evening interval has no Check In within its grace window
    # so absence_day_value would be 0.5.
    summary_before, daily_before = calculate_metrics(
        df, _split_schedules(),
        period_start="2026-05-05", period_end="2026-05-05",
    )
    ad_before = summary_before["absence_details"].iloc[0]
    assert ad_before["Absence Day Value"] == 0.5

    # Add the missing evening Check In via a manual correction.
    corrections = pd.DataFrame([
        _correction(4124537, "2026-05-05", "check_in", "18:03:00"),
    ])
    df_after, rejected = apply_manual_punch_corrections(df, corrections)
    assert rejected.empty
    summary_after, daily_after = calculate_metrics(
        df_after, _split_schedules(),
        period_start="2026-05-05", period_end="2026-05-05",
    )
    ad_after = summary_after["absence_details"].iloc[0]
    assert ad_after["Absence Day Value"] == 0.0
    assert ad_after["Attended Intervals"] == "09:00-13:00, 18:00-22:00"


def test_audit_columns_record_correction_action_per_row():
    """Every row in the attendance frame carries a `correction_action`
    column so HR can filter manual rows by intent (added / appended /
    overridden) in any downstream sheet."""
    df = pd.DataFrame([
        _punch(1, "X-EMP1", "2026-05-04", "08:30:00", "Check In"),
    ])
    corrections = pd.DataFrame([
        _correction(1, "2026-05-04", "check_out", "17:00:00"),    # added
        _correction(1, "2026-05-04", "check_in",  "18:00:00"),    # appended
    ])
    out, _ = apply_manual_punch_corrections(df, corrections)
    assert "correction_action" in out.columns
    # BioTime row keeps the empty default.
    biotime = out[~out["is_manual_correction"]].iloc[0]
    assert biotime["correction_action"] == ""
    # Manual rows carry their action.
    manual = out[out["is_manual_correction"]].sort_values("Punch Time")
    assert manual["correction_action"].tolist() == ["added", "appended"]


# -- event-day continuous-attendance split detection ---------------------

def test_emp403_event_day_split_2026_05_14_end_to_end():
    """The reported event-day case for ABDUL SAMEER PARAMMAL-EMP403:
    on 2026-05-14 the employee worked the BioTime span 09:03 -> 18:09
    continuously. HR splits it via two manual corrections (CO at
    13:00, CI at 13:01) so it reconciles against his 09:00-13:00 +
    18:00-22:00 split schedule.

    Required after the split:
      * 4 punches on the day (2 BioTime + 2 manual, none rejected)
      * both manual rows tagged correction_action='event_day_split'
      * source = 'manual_event_day_split'
      * default audit reason carries 'event day continuous attendance split'
      * Absence Day Value = 0.0 (full day, NOT partial)
      * Absence reason carries the event-day-split note
    """
    df = pd.DataFrame([
        _punch(4124537, "ABDUL SAMEER-EMP403", "2026-05-14",
               "09:03:27", "Check In"),
        _punch(4124537, "ABDUL SAMEER-EMP403", "2026-05-14",
               "09:03:29", "Check In"),
        _punch(4124537, "ABDUL SAMEER-EMP403", "2026-05-14",
               "18:09:28", "Check Out"),
        _punch(4124537, "ABDUL SAMEER-EMP403", "2026-05-14",
               "18:09:30", "Check Out"),
    ])
    corrections = pd.DataFrame([
        # Blank reason -- the engine should fill in the default
        # event-day-split note so audit readers see the intent.
        _correction(4124537, "2026-05-14", "check_out", "13:00:00",
                    reason=""),
        _correction(4124537, "2026-05-14", "check_in",  "13:01:00",
                    reason=""),
    ])
    out, rejected = apply_manual_punch_corrections(df, corrections)

    assert rejected.empty
    day = out[out["Date"] == "2026-05-14"]
    assert len(day) == 6   # 4 BioTime + 2 manual
    manual = day[day["is_manual_correction"]].sort_values("Punch Time")
    assert manual["correction_action"].tolist() == [
        "event_day_split", "event_day_split",
    ]
    assert (manual["correction_source"] == "manual_event_day_split").all()
    assert (manual["correction_reason"] ==
            "Manual correction - event day continuous attendance split").all()

    # End-to-end absence engine: the day must be classified as fully
    # attended (Absence Day Value = 0.0), NOT 0.5 partial.
    summary, daily = calculate_metrics(
        out, _split_schedules(),  # SAMEER-EMP1 fixture name; reuses split-shift label
        period_start="2026-05-14", period_end="2026-05-14",
    )
    # Filter absence details to our employee
    ad = summary["absence_details"]
    row = ad[ad["Employee ID"] == 4124537].iloc[0]
    assert row["Absence Day Value"] == 0.0
    assert bool(row["Counted As Absence"]) is False
    assert "event day continuous attendance split" in row["Absence Reason"].lower()


def test_event_day_split_not_triggered_when_no_surrounding_biotime_span():
    """The detection is intentionally narrow: it only fires when
    BioTime brackets the manual pair. A 'naked' CO+CI manual pair on
    a day with no BioTime Check Out after the boundary must NOT be
    tagged event_day_split (it would silence a partial-absence
    flag we want HR to investigate)."""
    df = pd.DataFrame([
        _punch(1, "X-EMP1", "2026-05-14", "12:00:00", "Check In"),
    ])
    corrections = pd.DataFrame([
        _correction(1, "2026-05-14", "check_out", "13:00:00"),
        _correction(1, "2026-05-14", "check_in",  "13:01:00"),
    ])
    out, _ = apply_manual_punch_corrections(df, corrections)
    manual = out[out["is_manual_correction"]]
    assert (manual["correction_action"] != "event_day_split").all()
    assert (manual["correction_source"] == "manual_camera_verified").all()


def test_event_day_split_not_triggered_when_gap_too_wide():
    """A CO at 13:00 and a CI at 14:00 (60-minute gap) is not a
    boundary insertion -- it's a long break. Don't tag as split."""
    df = pd.DataFrame([
        _punch(1, "X-EMP1", "2026-05-14", "09:00:00", "Check In"),
        _punch(1, "X-EMP1", "2026-05-14", "18:00:00", "Check Out"),
    ])
    corrections = pd.DataFrame([
        _correction(1, "2026-05-14", "check_out", "13:00:00"),
        _correction(1, "2026-05-14", "check_in",  "14:00:00"),
    ])
    out, _ = apply_manual_punch_corrections(df, corrections)
    manual = out[out["is_manual_correction"]].sort_values("Punch Time")
    assert (manual["correction_action"] == "appended").all()


def test_event_day_split_does_not_affect_normal_employees():
    """A single-shift employee with one CI + one CO never triggers
    the event-day-split pattern (HR would need to add a CI+CO pair
    explicitly, which is the opt-in)."""
    df = pd.DataFrame([
        _punch(1, "SOLO-EMP1", "2026-05-04", "08:00:00", "Check In"),
        _punch(1, "SOLO-EMP1", "2026-05-04", "17:00:00", "Check Out"),
    ])
    # Single manual correction -- nothing for the detector to pair up.
    corrections = pd.DataFrame([
        _correction(1, "2026-05-04", "check_in", "07:30:00"),
    ])
    out, _ = apply_manual_punch_corrections(df, corrections)
    manual = out[out["is_manual_correction"]].iloc[0]
    assert manual["correction_action"] == "appended"  # NOT event_day_split
    summary, _ = calculate_metrics(out, _single_schedules())
    # No false event-day-split tag means normal absence semantics still apply.
    ad = summary["absence_details"]
    assert "event day" not in ad.iloc[0]["Absence Reason"].lower()
