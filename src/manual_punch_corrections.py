"""Apply manually-verified forgotten-punch corrections to the attendance frame.

This is a TEMPORARY safety net for cases where an employee forgot to clock
in/out and HR has visual evidence (camera review) of when they actually
arrived or left. Use sparingly -- the long-term fix is to make sure every
employee uses the biometric terminal.

Only corrections that are
- approval_status == "approved"  AND
- evidence_type   == "camera"
get applied. Everything else is returned in the rejected DataFrame so it
can surface in an Exceptions & Manual Review view.

Existing biometric punches are NEVER overwritten unless the caller flips
ALLOW_OVERRIDE_EXISTING_PUNCH in config.

The module is import-safe: when the input file is missing the loader
returns an empty schema and ``apply_manual_punch_corrections`` becomes a
no-op that still adds the four audit columns to the attendance frame so
downstream code can rely on them.
"""
from datetime import datetime, time

import pandas as pd

from config import ALLOW_OVERRIDE_EXISTING_PUNCH


REQUIRED_COLUMNS = [
    "employee_code", "date", "punch_type", "corrected_time",
    "evidence_type", "approval_status",
]

_PUNCH_TYPE_TO_STATE = {
    "check_in": "Check In",
    "check_out": "Check Out",
}

_AUDIT_DEFAULTS = {
    "correction_source": "",
    "is_manual_correction": False,
    "correction_reason": "",
    "correction_verified_by": "",
}


def load_manual_punch_corrections_file(file_path):
    """Load the manual-corrections workbook. Returns empty DF when missing."""
    if not file_path.exists():
        print(f"No manual corrections file at {file_path}; skipping.")
        return pd.DataFrame(columns=REQUIRED_COLUMNS)
    suffix = file_path.suffix.lower()
    print(f"Loading manual punch corrections: {file_path}")
    if suffix == ".csv":
        return pd.read_csv(file_path)
    if suffix in (".xlsx", ".xls"):
        return pd.read_excel(file_path)
    raise ValueError(f"Unsupported file type: {suffix}")


def _ensure_audit_columns(df):
    out = df.copy()
    for col, default in _AUDIT_DEFAULTS.items():
        if col not in out.columns:
            out[col] = default
    return out


def _is_blank(value):
    if value is None:
        return True
    try:
        if pd.isna(value):
            return True
    except (TypeError, ValueError):
        pass
    return False


def _normalize_time(value):
    if _is_blank(value):
        return None
    if isinstance(value, time):
        return value.strftime("%H:%M:%S")
    if hasattr(value, "strftime"):
        return value.strftime("%H:%M:%S")
    s = str(value).strip()
    if not s or s.lower() == "nan":
        return None
    for fmt in ("%H:%M:%S", "%H:%M", "%I:%M %p", "%I:%M:%S %p"):
        try:
            return datetime.strptime(s, fmt).strftime("%H:%M:%S")
        except ValueError:
            continue
    return None


def _normalize_date(value):
    if _is_blank(value):
        return None
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%d")
    try:
        return pd.to_datetime(value).strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return None


def _existing_punch_mask(att_df, emp_id, date_str, punch_state):
    att_dates = pd.to_datetime(att_df["Date"], errors="coerce").dt.strftime("%Y-%m-%d")
    return (
        (att_df["Employee ID"] == emp_id)
        & (att_dates == date_str)
        & (att_df["Punch State"] == punch_state)
    )


def _empty_rejected():
    return pd.DataFrame(columns=list(REQUIRED_COLUMNS) + ["rejection_reason"])


def apply_manual_punch_corrections(attendance_df, corrections_df,
                                   allow_override=None):
    """Apply approved + camera-verified corrections to ``attendance_df``.

    Returns ``(corrected_attendance_df, rejected_corrections_df)``.

    - The corrected frame always carries the four audit columns
      (``correction_source``, ``is_manual_correction``,
      ``correction_reason``, ``correction_verified_by``). For rows that
      were not touched by a correction those columns hold the defaults
      from ``_AUDIT_DEFAULTS``.
    - The rejected frame copies each unapplied input row verbatim and
      appends a ``rejection_reason`` column.
    """
    if allow_override is None:
        allow_override = ALLOW_OVERRIDE_EXISTING_PUNCH

    att = _ensure_audit_columns(attendance_df)

    if corrections_df is None or corrections_df.empty:
        return att, _empty_rejected()

    missing_cols = set(REQUIRED_COLUMNS) - set(corrections_df.columns)
    if missing_cols:
        raise ValueError(
            "Manual corrections file missing required columns: "
            f"{sorted(missing_cols)}"
        )

    rejected_rows = []
    inserts = []
    overrides = []

    for _, row in corrections_df.iterrows():
        emp_raw = row.get("employee_code")
        date_raw = row.get("date")
        ptype = str(row.get("punch_type") or "").strip().lower()
        ctime = row.get("corrected_time")
        evidence = str(row.get("evidence_type") or "").strip().lower()
        approval = str(row.get("approval_status") or "").strip().lower()
        reason_str = "" if _is_blank(row.get("correction_reason")) else str(row.get("correction_reason"))
        verifier_str = "" if _is_blank(row.get("correction_verified_by")) else str(row.get("correction_verified_by"))

        if approval != "approved":
            rejected_rows.append((row, f"approval_status={approval or 'blank'}"))
            continue
        if evidence != "camera":
            rejected_rows.append((row, f"evidence_type={evidence or 'blank'}"))
            continue
        if ptype not in _PUNCH_TYPE_TO_STATE:
            rejected_rows.append((row, f"invalid punch_type={ptype or 'blank'}"))
            continue
        try:
            emp_id = int(float(emp_raw))
        except (ValueError, TypeError):
            rejected_rows.append((row, "invalid employee_code"))
            continue

        date_str = _normalize_date(date_raw)
        time_str = _normalize_time(ctime)
        if date_str is None or time_str is None:
            rejected_rows.append((row, "invalid date or corrected_time"))
            continue

        state = _PUNCH_TYPE_TO_STATE[ptype]
        existing = _existing_punch_mask(att, emp_id, date_str, state)

        if existing.any():
            if not allow_override:
                rejected_rows.append((row, "punch_already_exists"))
                continue
            overrides.append({
                "mask": existing, "time": time_str,
                "reason": reason_str, "verifier": verifier_str,
            })
            continue

        emp_rows = att.loc[att["Employee ID"] == emp_id, "First Name"]
        first_name_val = emp_rows.iloc[0] if not emp_rows.empty else None

        inserts.append({
            "Employee ID": emp_id,
            "First Name": first_name_val,
            "Date": date_str,
            "Punch Time": time_str,
            "Punch State": state,
            "correction_source": "manual_camera_verified",
            "is_manual_correction": True,
            "correction_reason": reason_str,
            "correction_verified_by": verifier_str,
        })

    for ov in overrides:
        att.loc[ov["mask"], "Punch Time"] = ov["time"]
        att.loc[ov["mask"], "correction_source"] = "manual_camera_verified"
        att.loc[ov["mask"], "is_manual_correction"] = True
        att.loc[ov["mask"], "correction_reason"] = ov["reason"]
        att.loc[ov["mask"], "correction_verified_by"] = ov["verifier"]

    if inserts:
        att = pd.concat([att, pd.DataFrame(inserts)], ignore_index=True)

    att["is_manual_correction"] = att["is_manual_correction"].fillna(False).astype(bool)

    if rejected_rows:
        rejected_df = pd.DataFrame(
            [{**r.to_dict(), "rejection_reason": reason}
             for r, reason in rejected_rows]
        )
    else:
        rejected_df = _empty_rejected()

    return att, rejected_df
