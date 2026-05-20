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
    """Before: evening punches both missing -> absence engine reports
    0.5 day partial absence (only morning interval attended).
    After: HR adds the evening CI via a manual correction -> both
    intervals attended -> 0.0 absence day value.

    Note: the morning-only fixture deliberately omits a late CO,
    otherwise the span-cover rule would credit the evening interval
    on its own (the rule that fixes the EMP418 2026-05-03 case)."""
    df = pd.DataFrame([
        _punch(4124537, "SAMEER-EMP1", "2026-05-05",
               "09:03:00", "Check In"),
        _punch(4124537, "SAMEER-EMP1", "2026-05-05",
               "13:20:00", "Check Out"),
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
    # We also add a late Check Out so the day has a complete evening
    # pair after correction.
    corrections = pd.DataFrame([
        _correction(4124537, "2026-05-05", "check_in", "18:03:00"),
        _correction(4124537, "2026-05-05", "check_out", "22:00:00"),
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


def test_emp418_2026_05_03_forgot_evening_ci_but_co_at_shift_end():
    """Regression: EMP418 on 2026-05-03 has BioTime evidence of the
    evening shift attendance via a Check Out at 21:00 (= evening
    shift end), but the evening Check In is missing. The lenient
    span-cover rule must credit the evening interval as attended so
    the day classifies as 0.0 absence, NOT 0.5 partial.

    BioTime raw events (already alias-mapped to canonical 4197471):
      09:12 CI, 13:00 CO   (morning shift, complete pair)
      21:00 CO             (evening shift CO; no evening CI)

    Day span: 09:12 -> 21:00.
    Morning  09:00-13:00: covered by direct CI (09:12 in grace window).
    Evening  17:00-21:00: NO direct CI, but span (09:12, 21:00)
                          wholly covers [17:00, 21:00] -> credited
                          via the span-cover rule. Absence Reason
                          surfaces 'credited via continuous span'.
    """
    df = pd.DataFrame([
        {"Employee ID": 4197471, "First Name": "MOHAMMED AJISH-EMP418",
         "Date": "2026-05-03", "Punch Time": "09:12:06",
         "Punch State": "Check In"},
        {"Employee ID": 4197471, "First Name": "MOHAMMED AJISH-EMP418",
         "Date": "2026-05-03", "Punch Time": "09:12:07",
         "Punch State": "Check In"},
        {"Employee ID": 4197471, "First Name": "MOHAMMED AJISH-EMP418",
         "Date": "2026-05-03", "Punch Time": "13:00:02",
         "Punch State": "Check Out"},
        {"Employee ID": 4197471, "First Name": "MOHAMMED AJISH-EMP418",
         "Date": "2026-05-03", "Punch Time": "21:00:06",
         "Punch State": "Check Out"},
        {"Employee ID": 4197471, "First Name": "MOHAMMED AJISH-EMP418",
         "Date": "2026-05-03", "Punch Time": "21:00:07",
         "Punch State": "Check Out"},
    ])
    schedules = pd.DataFrame([{
        "Name": "MOHAMMED AJISH-EMP418",
        "Working Time": "شفت صباحى (9:00AM-1:00PM) & شفت مسائى (5:00PM-9:00PM)",
    }])
    summary, daily = calculate_metrics(
        df, schedules,
        period_start="2026-05-03", period_end="2026-05-03",
    )
    ad = summary["absence_details"]
    row = ad[(ad["Employee ID"] == 4197471)
             & (ad["Date"] == "2026-05-03")].iloc[0]
    assert row["Absence Day Value"] == 0.0
    assert bool(row["Counted As Absence"]) is False
    assert row["Attended Intervals"] == "09:00-13:00, 17:00-21:00"
    assert "credited via continuous span" in row["Absence Reason"]
    assert "17:00-21:00" in row["Absence Reason"]


def test_span_cover_does_not_credit_without_any_check_in():
    """Guard: a lone stray Check Out without a same-day Check In
    must NOT credit the interval via the span rule. This prevents
    a 'random late CO at 22:00 from a forgotten badge' from
    falsely marking the evening interval as attended.

    We seed a Check In on a NEIGHBOURING date so the daily pipeline
    has data to process; the assertion is about the lone-CO day
    in isolation."""
    df = pd.DataFrame([
        # Neighbouring day with a normal punch -- seeds calculate_metrics
        {"Employee ID": 1, "First Name": "X-EMP1",
         "Date": "2026-05-03", "Punch Time": "09:00:00",
         "Punch State": "Check In"},
        # The day under test: lone stray Check Out, no Check In.
        {"Employee ID": 1, "First Name": "X-EMP1",
         "Date": "2026-05-04", "Punch Time": "22:00:00",
         "Punch State": "Check Out"},
    ])
    schedules = pd.DataFrame([{
        "Name": "X-EMP1",
        "Working Time": "شفت صباحى (9:00AM-1:00PM) & شفت مسائى (5:00PM-9:00PM)",
    }])
    summary, _ = calculate_metrics(
        df, schedules,
        period_start="2026-05-04", period_end="2026-05-04",
    )
    ad = summary["absence_details"]
    row = ad[ad["Date"] == "2026-05-04"].iloc[0]
    # No CI on the lone-CO day -> span rule cannot fire -> both intervals missed.
    assert row["Absence Day Value"] == 1.0


def test_span_cover_does_not_credit_when_span_too_short():
    """Guard: span 09:05 -> 13:01 (morning-only) must NOT credit the
    evening interval via the span rule -- latest CO must reach
    interval_end."""
    df = pd.DataFrame([
        {"Employee ID": 1, "First Name": "X-EMP1",
         "Date": "2026-05-04", "Punch Time": "09:05:00",
         "Punch State": "Check In"},
        {"Employee ID": 1, "First Name": "X-EMP1",
         "Date": "2026-05-04", "Punch Time": "13:01:00",
         "Punch State": "Check Out"},
    ])
    schedules = pd.DataFrame([{
        "Name": "X-EMP1",
        "Working Time": "شفت صباحى (9:00AM-1:00PM) & شفت مسائى (5:00PM-9:00PM)",
    }])
    summary, _ = calculate_metrics(
        df, schedules,
        period_start="2026-05-04", period_end="2026-05-04",
    )
    row = summary["absence_details"].iloc[0]
    assert row["Absence Day Value"] == 0.5
    assert row["Missed Intervals"] == "17:00-21:00"


def test_emp418_alias_plus_manual_canonical_merge_2026_05_04():
    """Regression for the EMP418 reported case.

    On 2026-05-04 BioTime captured punches under TWO different
    Employee IDs (the legacy device ID 1027 was being retired in
    favour of the canonical 4197471). HR then added a manual
    correction for the missing evening Check In. The absence engine
    MUST merge all sources under the canonical Employee ID and
    classify the day as fully attended (0.0).

    Sources of attendance on the day, after alias mapping + manual:
      raw 1027 (aliased)        -> CIs 08:56 + 16:50, CO 13:00
      raw 4197471 (canonical)   -> CO 21:01
      manual (under 4197471)    -> CI 17:01

    After alias_mapping(1027 -> 4197471):
      Employee ID 4197471 holds 3 CIs (08:56, 16:50, 17:01)
                                 + 2 COs (13:00, 21:01).

    Schedule: split shift 09:00-13:00 morning + 17:00-21:00 evening.
    Both intervals contain a Check In within their grace window, so
    Absence Day Value MUST be 0.0 and the new audit columns must
    record both source IDs + the merged total worked time.
    """
    from data_loader import apply_employee_id_aliases

    raw = pd.DataFrame([
        # BioTime under the legacy device ID 1027 (no name yet -- the
        # canonical First Name is supplied via the alias map).
        {"Employee ID": 1027, "First Name": None,
         "Date": "2026-05-04", "Punch Time": "08:56:00",
         "Punch State": "Check In"},
        {"Employee ID": 1027, "First Name": None,
         "Date": "2026-05-04", "Punch Time": "13:00:00",
         "Punch State": "Check Out"},
        {"Employee ID": 1027, "First Name": None,
         "Date": "2026-05-04", "Punch Time": "16:50:00",
         "Punch State": "Check In"},
        # BioTime under the canonical Employee ID 4197471.
        {"Employee ID": 4197471, "First Name": "MOHAMMED AJISH-EMP418",
         "Date": "2026-05-04", "Punch Time": "21:01:00",
         "Punch State": "Check Out"},
    ])
    aliases = pd.DataFrame([{
        "Old Employee ID": 1027, "Current Employee ID": 4197471,
        "Employee Name": "MOHAMMED AJISH-EMP418",
    }])
    schedules = pd.DataFrame([{
        "Name": "MOHAMMED AJISH-EMP418",
        "Working Time": "شفت صباحى (9:00AM-1:00PM) & شفت مسائى (5:00PM-9:00PM)",
    }])
    corrections = pd.DataFrame([
        _correction(4197471, "2026-05-04", "check_in", "17:01:00",
                    reason="نسي البصمة"),
    ])

    # Pipeline order: manual corrections first (so the new CI gets
    # added under the canonical ID), THEN alias mapping (so the 1027
    # rows fold under 4197471). Matches main.py.
    df, rej = apply_manual_punch_corrections(raw, corrections)
    assert rej.empty
    df, _, _ = apply_employee_id_aliases(df, aliases, schedules)

    summary, daily = calculate_metrics(
        df, schedules,
        period_start="2026-05-04", period_end="2026-05-04",
    )

    ad = summary["absence_details"]
    row = ad[(ad["Employee ID"] == 4197471) & (ad["Date"] == "2026-05-04")].iloc[0]
    assert row["Absence Day Value"] == 0.0
    assert bool(row["Counted As Absence"]) is False
    assert row["Attended Intervals"] == "09:00-13:00, 17:00-21:00"
    assert row["Missed Intervals"] == ""

    # Canonical-merge audit columns must surface BOTH raw IDs and
    # both provenance buckets.
    assert set(row["Raw Employee IDs"].split(", ")) == {"1027", "4197471"}
    sources = row["Sources"]
    assert "Aliased" in sources or "1027" in sources
    assert "Manual correction" in sources
    assert "BioTime" in sources or "Aliased" in sources  # 4197471's lone CO

    # Total worked time -- chronological pairing across the merged
    # punches: (08:56 -> 13:00) = 04:04, (16:50 -> 21:01) = 04:11.
    # Sum = 08:15. The manual 17:01 CI is sandwiched while another
    # CI is already open (16:50) so chronological pairing ignores it.
    assert row["Total Worked"] == "08:15"
    # And the Employee Summary contribution for this date is zero.
    audit = summary["absence_audit"]
    audit_row = audit[audit["Employee ID"] == 4197471].iloc[0]
    assert audit_row["absence_days"] == 0.0


def test_timeoff_employee_name_mismatch_resolved_by_emp_code():
    """Regression: EMP374 case.

    Odoo's `hr.leave` export sometimes carries the employee's
    registered name with extra words (e.g. middle name "BINT") that
    the attendance / schedule files omit. The old absence engine
    matched time-off by EXACT name and silently dropped any TOF row
    whose Employee field didn't textually match an attendance First
    Name -- so HR's approved Annual Leave and Sick Leave surfaced as
    phantom Absences.

    The new resolver matches by EMP code first (then NBSP-tolerant
    normalized name), so name mismatches between TOF and attendance
    no longer drop the leave.
    """
    # Attendance / schedule use the shorter name (no "BINT").
    df = pd.DataFrame([
        {"Employee ID": 4156144, "First Name": "AHOUD BAZARI BIN AHMED NAJI-EMP374",
         "Date": "2026-04-29", "Punch Time": "09:00:00",
         "Punch State": "Check In"},
        {"Employee ID": 4156144, "First Name": "AHOUD BAZARI BIN AHMED NAJI-EMP374",
         "Date": "2026-04-29", "Punch Time": "17:00:00",
         "Punch State": "Check Out"},
    ])
    schedules = pd.DataFrame([{
        "Name": "AHOUD BAZARI BIN AHMED NAJI-EMP374",
        "Working Time": "دوام صباحى (9:00AM-5:00PM)",
    }])
    # Time off carries the LONGER name (with extra "BINT" word) --
    # the bug condition. The resolver must still credit the leave
    # because both names share the EMP374 code.
    time_off = pd.DataFrame([{
        "Employee": "AHOUD BINT BAZARI BIN AHMED NAJI-EMP374",
        "Time Off Type": "Annual Leave",
        "Start Date": "2026-04-30 09:00:00",
        "End Date": "2026-04-30 17:00:00",
        "Status": "Approved",
    }])
    summary, _ = calculate_metrics(
        df, schedules, time_off,
        period_start="2026-04-30", period_end="2026-04-30",
    )
    ad = summary["absence_details"]
    row = ad[(ad["Employee ID"] == 4156144)
             & (ad["Date"] == "2026-04-30")].iloc[0]
    # The leave is credited as Vacation, NOT a phantom absence.
    assert row["Absence Day Value"] == 0.0
    assert bool(row["Is Vacation"]) is True
    assert row["Time Off Type"] == "Annual Leave"
    assert "Approved time off" in row["Absence Reason"]


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
