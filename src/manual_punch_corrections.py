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

Multi-interval (split-shift) attendance model
---------------------------------------------
Manual corrections APPEND new punches to the attendance frame by default.
The downstream pipeline (daily aggregator, interval-aware overtime engine,
absence engine, Employee Attendance audit sheet) already treats multiple
Check Ins / Check Outs per (employee, date) as first-class data, so an
employee who needs two pairs of punches in a split-shift day can have HR
add the missing morning Check Out / evening Check In / evening Check Out
without colliding with the existing morning Check In.

Action semantics (recorded in the `correction_action` audit column):
- "added"             : no existing same-state punch on that date; clean insert.
- "appended"          : a same-state punch exists at a DIFFERENT time;
                        the manual punch lands alongside it (the typical
                        split-shift second-interval case).
- "overridden"        : `ALLOW_OVERRIDE_EXISTING_PUNCH=true`, so the new
                        punch REPLACES the existing same-state punch at
                        a different time (legacy "wrong device punch" use
                        case).
- "duplicate_skipped" : the manual correction matches an existing punch
                        exactly (same time + state); skipped silently.

The module is import-safe: when the input file is missing the loader
returns an empty schema and ``apply_manual_punch_corrections`` becomes a
no-op that still adds the audit columns to the attendance frame so
downstream code can rely on them.
"""
from datetime import datetime, time

import pandas as pd

from config import ALLOW_OVERRIDE_EXISTING_PUNCH


REQUIRED_COLUMNS = [
    "employee_code", "date", "punch_type", "corrected_time",
    "evidence_type", "approval_status",
]

# Event-day-split detection -- triggers only when a manual correction
# pair clearly INSERTS a boundary inside an existing continuous span.
# We require:
#   * one manual Check Out + one manual Check In on the same (emp, date)
#   * the two manual times within `_EVENT_DAY_SPLIT_MAX_GAP_MIN` minutes
#   * a BioTime Check In that predates both manual times
#   * a BioTime Check Out that follows both manual times
# When this holds, both manual rows are tagged
# correction_action="event_day_split", source="manual_event_day_split",
# and the absence engine treats the day as fully attended even when the
# split breaks the schedule's interval windows.
_EVENT_DAY_SPLIT_MAX_GAP_MIN = 5
_EVENT_DAY_SPLIT_DEFAULT_REASON = (
    "Manual correction - event day continuous attendance split"
)


def _to_minutes(time_str):
    """'HH:MM:SS' / 'HH:MM' -> minutes from midnight. None on bad input."""
    try:
        parts = str(time_str).split(":")
        return int(parts[0]) * 60 + int(parts[1])
    except (ValueError, AttributeError, IndexError):
        return None

_PUNCH_TYPE_TO_STATE = {
    "check_in": "Check In",
    "check_out": "Check Out",
}

_AUDIT_DEFAULTS = {
    "correction_source": "",
    "is_manual_correction": False,
    "correction_reason": "",
    "correction_verified_by": "",
    # correction_action: "" for BioTime rows; otherwise one of
    # "added" / "appended" / "overridden" (see module docstring).
    "correction_action": "",
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


def _exact_duplicate_mask(att_df, emp_id, date_str, punch_state, time_str):
    """Detect a punch at the EXACT same emp + date + state + time. Used
    to skip no-op re-imports without surfacing them as conflicts."""
    same_state = _existing_punch_mask(att_df, emp_id, date_str, punch_state)
    if not same_state.any():
        return same_state
    times = att_df["Punch Time"].astype(str)
    return same_state & (times == time_str)


def _detect_event_day_splits(inserts, att_df):
    """Tag insert rows that form an event-day continuous-attendance split.

    HR sometimes needs to split a single continuous BioTime span (e.g.
    employee worked 09:03 through 18:09 straight on an event day) into
    TWO accounting intervals so the day reconciles against a split-
    shift schedule. They do this by adding two manual corrections:
    a Check Out at the boundary and a Check In one minute later.

    This helper detects that exact pattern -- WITHOUT a global change
    to how all manual corrections are interpreted -- and rewrites
    those two `inserts` rows with the dedicated audit tags. Inserts
    that don't match the pattern are left untouched and stay as the
    plain "appended" appendees handled by the previous safety-net
    release.

    The detection is intentionally narrow:
    1. exactly ONE manual CI and ONE manual CO on the same (emp, date),
    2. the two manual times within `_EVENT_DAY_SPLIT_MAX_GAP_MIN` of
       each other,
    3. a BioTime Check In on that date with time STRICTLY BEFORE both
       manual times (the morning anchor of the continuous span),
    4. a BioTime Check Out on that date with time STRICTLY AFTER both
       manual times (the evening anchor of the continuous span).
    Anything else -- 1 manual punch, 3+ manual punches, manual pair
    far apart, no surrounding BioTime span -- keeps the regular
    "appended" semantics so normal employees see no behaviour change.
    """
    if not inserts:
        return

    groups = {}
    for ins in inserts:
        key = (ins["Employee ID"], ins["Date"])
        groups.setdefault(key, []).append(ins)

    att_dates = pd.to_datetime(att_df["Date"], errors="coerce").dt.strftime("%Y-%m-%d")

    for (emp_id, date_str), group in groups.items():
        manual_cis = [r for r in group if r["Punch State"] == "Check In"]
        manual_cos = [r for r in group if r["Punch State"] == "Check Out"]
        if len(manual_cis) != 1 or len(manual_cos) != 1:
            continue
        ci_min = _to_minutes(manual_cis[0]["Punch Time"])
        co_min = _to_minutes(manual_cos[0]["Punch Time"])
        if ci_min is None or co_min is None:
            continue
        if abs(ci_min - co_min) > _EVENT_DAY_SPLIT_MAX_GAP_MIN:
            continue
        lo, hi = min(ci_min, co_min), max(ci_min, co_min)

        # Inspect BioTime rows on this (emp, date): we need a CI
        # strictly before the split boundary and a CO strictly after.
        day_mask = (
            (att_df["Employee ID"] == emp_id)
            & (att_dates == date_str)
            & (~att_df["is_manual_correction"].astype(bool))
        )
        biotime = att_df[day_mask]
        if biotime.empty:
            continue
        bt_ci_times = [
            _to_minutes(t) for t in
            biotime.loc[biotime["Punch State"] == "Check In", "Punch Time"]
                   .astype(str).tolist()
        ]
        bt_co_times = [
            _to_minutes(t) for t in
            biotime.loc[biotime["Punch State"] == "Check Out", "Punch Time"]
                   .astype(str).tolist()
        ]
        has_pre_ci = any(t is not None and t < lo for t in bt_ci_times)
        has_post_co = any(t is not None and t > hi for t in bt_co_times)
        if not (has_pre_ci and has_post_co):
            continue

        # All four conditions hold -> tag both inserts.
        for ins in group:
            ins["correction_action"] = "event_day_split"
            ins["correction_source"] = "manual_event_day_split"
            if not ins.get("correction_reason"):
                ins["correction_reason"] = _EVENT_DAY_SPLIT_DEFAULT_REASON


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

        # 1) Exact duplicate (same emp + date + state + time) -> no-op.
        # HR sometimes re-imports the same correction file; skipping
        # these silently keeps the rejected sheet uncluttered.
        if _exact_duplicate_mask(att, emp_id, date_str, state, time_str).any():
            rejected_rows.append((row, "duplicate_already_recorded"))
            continue

        # 2) Same-state punch exists at a DIFFERENT time. Two intents
        # are possible; the `allow_override` flag disambiguates:
        #   - allow_override=True   -> legacy clobber semantics.
        #     The existing same-state punch is REPLACED with the new
        #     time. Use when HR is correcting a wrong device punch.
        #   - allow_override=False  -> default APPEND semantics.
        #     The manual punch lands ALONGSIDE the existing one. Split-
        #     shift employees need this for their second-interval pair.
        existing = _existing_punch_mask(att, emp_id, date_str, state)
        if existing.any() and allow_override:
            overrides.append({
                "mask": existing, "time": time_str,
                "reason": reason_str, "verifier": verifier_str,
            })
            continue

        action = "appended" if existing.any() else "added"

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
            "correction_action": action,
        })

    for ov in overrides:
        att.loc[ov["mask"], "Punch Time"] = ov["time"]
        att.loc[ov["mask"], "correction_source"] = "manual_camera_verified"
        att.loc[ov["mask"], "is_manual_correction"] = True
        att.loc[ov["mask"], "correction_reason"] = ov["reason"]
        att.loc[ov["mask"], "correction_verified_by"] = ov["verifier"]
        att.loc[ov["mask"], "correction_action"] = "overridden"

    # Narrow event-day-split detection runs ONLY against the candidate
    # inserts -- it never re-tags rows the user did not explicitly add.
    _detect_event_day_splits(inserts, att)

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
