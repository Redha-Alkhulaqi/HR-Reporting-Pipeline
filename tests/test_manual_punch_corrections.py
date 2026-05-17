import pandas as pd
import pytest

from manual_punch_corrections import (
    REQUIRED_COLUMNS,
    apply_manual_punch_corrections,
)


def _punch(eid, name, time, state, date="2026-05-04"):
    return {
        "Employee ID": eid, "First Name": name, "Date": date,
        "Punch Time": time, "Punch State": state,
    }


def _correction(eid=1003, date="2026-05-04", punch_type="check_in",
                corrected_time="08:00:00", evidence="camera",
                approval="approved", reason="forgot to clock in",
                verifier="HR Manager"):
    return {
        "employee_code": eid, "date": date, "punch_type": punch_type,
        "corrected_time": corrected_time, "evidence_type": evidence,
        "approval_status": approval,
        "correction_reason": reason,
        "correction_verified_by": verifier,
    }


def test_no_corrections_file_adds_audit_columns():
    df = pd.DataFrame([_punch(1003, "X", "08:00:00", "Check In")])
    out, rejected = apply_manual_punch_corrections(df, None)
    assert "is_manual_correction" in out.columns
    assert "correction_source" in out.columns
    assert (out["is_manual_correction"] == False).all()  # noqa: E712
    assert rejected.empty


def test_approved_camera_correction_fills_missing_check_in():
    df = pd.DataFrame([
        # Only Check Out exists; Check In is missing.
        _punch(1003, "FAISAL", "17:00:00", "Check Out"),
    ])
    corrections = pd.DataFrame([_correction()])
    out, rejected = apply_manual_punch_corrections(df, corrections)
    assert rejected.empty
    added = out[out["is_manual_correction"]]
    assert len(added) == 1
    row = added.iloc[0]
    assert row["Punch State"] == "Check In"
    assert row["Punch Time"] == "08:00:00"
    assert row["correction_source"] == "manual_camera_verified"
    assert row["correction_reason"] == "forgot to clock in"
    assert row["correction_verified_by"] == "HR Manager"


def test_approved_camera_correction_fills_missing_check_out():
    df = pd.DataFrame([_punch(1003, "FAISAL", "08:00:00", "Check In")])
    corrections = pd.DataFrame([_correction(
        punch_type="check_out", corrected_time="17:30:00",
    )])
    out, rejected = apply_manual_punch_corrections(df, corrections)
    assert rejected.empty
    added = out[out["is_manual_correction"]]
    assert added.iloc[0]["Punch State"] == "Check Out"
    assert added.iloc[0]["Punch Time"] == "17:30:00"


def test_pending_approval_is_rejected():
    df = pd.DataFrame([_punch(1003, "X", "17:00:00", "Check Out")])
    corrections = pd.DataFrame([_correction(approval="pending")])
    out, rejected = apply_manual_punch_corrections(df, corrections)
    assert not out["is_manual_correction"].any()
    assert len(rejected) == 1
    assert "approval_status=pending" in rejected.iloc[0]["rejection_reason"]


def test_non_camera_evidence_is_rejected():
    df = pd.DataFrame([_punch(1003, "X", "17:00:00", "Check Out")])
    corrections = pd.DataFrame([_correction(evidence="email")])
    out, rejected = apply_manual_punch_corrections(df, corrections)
    assert not out["is_manual_correction"].any()
    assert len(rejected) == 1
    assert "evidence_type=email" in rejected.iloc[0]["rejection_reason"]


def test_existing_punch_not_overwritten_by_default():
    df = pd.DataFrame([
        _punch(1003, "X", "08:30:00", "Check In"),   # already exists
        _punch(1003, "X", "17:00:00", "Check Out"),
    ])
    corrections = pd.DataFrame([_correction(corrected_time="08:00:00")])
    out, rejected = apply_manual_punch_corrections(df, corrections)
    # Original 08:30 preserved, no insert, rejected with reason.
    assert (out["Punch Time"] == "08:30:00").sum() == 1
    assert not out["is_manual_correction"].any()
    assert len(rejected) == 1
    assert rejected.iloc[0]["rejection_reason"] == "existing_check_in_already_present"


def test_existing_punch_overwritten_when_allow_override():
    df = pd.DataFrame([
        _punch(1003, "X", "08:30:00", "Check In"),
    ])
    corrections = pd.DataFrame([_correction(corrected_time="08:00:00")])
    out, rejected = apply_manual_punch_corrections(
        df, corrections, allow_override=True
    )
    assert rejected.empty
    assert (out["Punch Time"] == "08:00:00").all()
    assert out["is_manual_correction"].sum() == 1
    assert out.iloc[0]["correction_source"] == "manual_camera_verified"


def test_missing_required_columns_raises():
    df = pd.DataFrame([_punch(1003, "X", "17:00:00", "Check Out")])
    bad = pd.DataFrame([{"employee_code": 1003, "date": "2026-05-04"}])
    with pytest.raises(ValueError, match="missing required columns"):
        apply_manual_punch_corrections(df, bad)


def test_invalid_time_is_rejected():
    df = pd.DataFrame([_punch(1003, "X", "17:00:00", "Check Out")])
    corrections = pd.DataFrame([_correction(corrected_time="nope")])
    _, rejected = apply_manual_punch_corrections(df, corrections)
    assert len(rejected) == 1
    assert "invalid date or corrected_time" in rejected.iloc[0]["rejection_reason"]


def test_required_columns_constant_matches_validation():
    # Guard against drift between docstring and code.
    assert REQUIRED_COLUMNS == [
        "employee_code", "date", "punch_type", "corrected_time",
        "evidence_type", "approval_status",
    ]


def test_corrected_check_in_uses_existing_employee_first_name():
    df = pd.DataFrame([
        _punch(1003, "FAISAL", "17:00:00", "Check Out"),
    ])
    corrections = pd.DataFrame([_correction()])
    out, _ = apply_manual_punch_corrections(df, corrections)
    added = out[out["is_manual_correction"]].iloc[0]
    assert added["First Name"] == "FAISAL"


def test_both_check_in_and_check_out_can_be_added():
    df = pd.DataFrame([
        # Employee with NO punches today.
        _punch(2000, "OTHER", "08:00:00", "Check In", date="2026-05-03"),
    ])
    corrections = pd.DataFrame([
        _correction(eid=2000, date="2026-05-04",
                    punch_type="check_in", corrected_time="08:00:00"),
        _correction(eid=2000, date="2026-05-04",
                    punch_type="check_out", corrected_time="17:00:00"),
    ])
    out, rejected = apply_manual_punch_corrections(df, corrections)
    assert rejected.empty
    added = out[out["is_manual_correction"]]
    assert len(added) == 2
    assert set(added["Punch State"]) == {"Check In", "Check Out"}
