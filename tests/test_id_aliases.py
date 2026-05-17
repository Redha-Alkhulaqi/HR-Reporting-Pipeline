import pandas as pd

from data_loader import apply_employee_id_aliases
from metrics_calculator import calculate_metrics


def _punch(eid, name, time, state, date="2026-05-04"):
    return {
        "Employee ID": eid, "First Name": name, "Date": date,
        "Punch Time": time, "Punch State": state,
    }


def _alias(old, new, name, active=True, source="Old BioTime"):
    return {
        "Old Employee ID": old, "Current Employee ID": new,
        "Employee Name": name, "Source": source,
        "Active": active, "Notes": "",
    }


def test_old_id_replaced_with_current_id():
    df = pd.DataFrame([
        _punch(1003, "FAISAL BABU-PULLO", "08:00:00", "Check In"),
        _punch(1003, "FAISAL BABU-PULLO", "17:00:00", "Check Out"),
        _punch(9999, "OTHER", "08:00:00", "Check In"),
    ])
    aliases = pd.DataFrame([_alias(1003, 3003, "FAISAL BABU-PULLO")])
    out, audit, warns = apply_employee_id_aliases(df, aliases)
    assert warns == []
    assert (out.loc[out["original_employee_id"] == 1003, "Employee ID"] == 3003).all()
    assert (out.loc[out["original_employee_id"] == 9999, "Employee ID"] == 9999).all()
    assert out["id_alias_applied"].sum() == 2
    assert int(audit.iloc[0]["records_mapped"]) == 2


def test_missing_first_name_filled_from_alias():
    df = pd.DataFrame([
        # Existing First Name is BLANK -- alias should fill it in.
        _punch(1003, None, "08:00:00", "Check In"),
        # Existing First Name is set -- alias must NOT overwrite it.
        _punch(1003, "EXISTING", "17:00:00", "Check Out"),
    ])
    aliases = pd.DataFrame([_alias(1003, 3003, "FAISAL BABU-PULLO")])
    out, _, _ = apply_employee_id_aliases(df, aliases)
    first_row = out.iloc[0]
    second_row = out.iloc[1]
    assert first_row["First Name"] == "FAISAL BABU-PULLO"
    assert second_row["First Name"] == "EXISTING"


def test_inactive_alias_is_ignored():
    df = pd.DataFrame([_punch(1003, "X", "08:00:00", "Check In")])
    aliases = pd.DataFrame([_alias(1003, 3003, "X", active=False)])
    out, audit, _ = apply_employee_id_aliases(df, aliases)
    assert (out["Employee ID"] == 1003).all()
    assert (out["id_alias_applied"] == False).all()  # noqa: E712
    assert audit.empty


def test_duplicate_alias_warns_and_keeps_first():
    df = pd.DataFrame([_punch(1003, "X", "08:00:00", "Check In")])
    aliases = pd.DataFrame([
        _alias(1003, 3003, "X"),
        _alias(1003, 3009, "X"),  # conflicting Current ID
    ])
    out, _, warns = apply_employee_id_aliases(df, aliases)
    assert any("multiple Current IDs" in w for w in warns)
    # First-seen mapping wins.
    assert (out["Employee ID"] == 3003).all()


def test_current_id_missing_from_schedules_warns_but_maps():
    df = pd.DataFrame([_punch(1003, "GHOST", "08:00:00", "Check In")])
    aliases = pd.DataFrame([_alias(1003, 3003, "GHOST-EMP")])
    schedules = pd.DataFrame([
        {"Name": "OTHER-EMP", "Working Time": "(8:00AM-5:00PM)"},
    ])
    out, audit, warns = apply_employee_id_aliases(df, aliases, schedules)
    assert any("no matching Odoo schedule" in w for w in warns)
    # Mapping still applied even though Current ID isn't in Odoo.
    assert (out["Employee ID"] == 3003).all()
    assert int(audit.iloc[0]["records_mapped"]) == 1


def test_mapped_record_is_not_counted_as_orphan():
    """End-to-end: an old-ID employee gets remapped to a current ID
    that IS in Odoo schedules. The reconciliation must NOT flag the
    mapped employee as an orphan attendance record."""
    df = pd.DataFrame([
        _punch(1003, None, "08:00:00", "Check In"),
        _punch(1003, None, "17:00:00", "Check Out"),
    ])
    aliases = pd.DataFrame([_alias(1003, 3003, "FAISAL-EMP415")])
    schedules = pd.DataFrame([
        {"Name": "FAISAL-EMP415", "Working Time": "دوام صباحى (8:00AM-5:00PM)"},
    ])
    mapped_df, alias_audit, _ = apply_employee_id_aliases(df, aliases, schedules)
    summary, daily = calculate_metrics(
        mapped_df, schedules, time_off_df=None, alias_audit=alias_audit
    )
    # No orphans: the mapped employee resolves to FAISAL-EMP415 which
    # exists in the schedules file.
    assert summary["employees_missing_schedule"] == 0
    assert summary["unscheduled_active_employees"] == 0
    assert summary["orphan_attendance_records"] == 0
    # And the audit is exposed in summary.
    assert summary["employee_id_aliases_used"] == 1
    assert summary["employee_id_alias_records_mapped"] == 2
