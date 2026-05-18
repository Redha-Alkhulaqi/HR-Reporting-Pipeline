import pandas as pd


def _load_table(file_path, label, excel_header=0):
    """Load a CSV or Excel file, dispatching by extension."""
    print(f"Loading {label}: {file_path}")
    suffix = file_path.suffix.lower()
    if suffix == ".csv":
        return pd.read_csv(file_path)
    if suffix in (".xlsx", ".xls"):
        return pd.read_excel(file_path, header=excel_header)
    raise ValueError(f"Unsupported file type: {suffix}")


def load_attendance_file(file_path):
    # BioTime export has a one-row "Transaction" banner above the real headers.
    return _load_table(file_path, "attendance file", excel_header=1)


_SCHEDULE_LABEL_ALIASES = (
    "Working Time", "Working Hours", "Resource Calendar",
    "Calendar", "Schedule",
)


def load_working_schedule_file(file_path):
    """Load the Odoo resource.resource export.

    Odoo exports the shift label under several names depending on the
    extract template ("Working Time", "Working Hours", "Resource
    Calendar", "Calendar", "Schedule"). Whichever variant exists is
    renamed to "Working Time" so the rest of the pipeline can keep
    using a single canonical column.
    """
    df = _load_table(file_path, "working schedule file")
    if "Working Time" not in df.columns:
        for alt in _SCHEDULE_LABEL_ALIASES:
            if alt == "Working Time":
                continue
            if alt in df.columns:
                print(
                    f"Schedule label column detected as '{alt}'; "
                    "renaming to 'Working Time' for downstream use."
                )
                df = df.rename(columns={alt: "Working Time"})
                break
    return df


def load_time_off_file(file_path):
    return _load_table(file_path, "time off file")


_EXCLUSION_COLUMNS = [
    "Employee ID", "Employee Name", "Exclusion Reason",
    "Exclude From Late", "Exclude From Overtime",
    "Exclude From Payroll Deduction", "Exclude From Risk Scoring",
    "Notes",
]


def load_excluded_employees_file(file_path):
    """Load policy-driven employee exclusions.

    The file is OPTIONAL. When it is missing we return an empty
    DataFrame with the expected schema so callers can treat the
    feature as a no-op without special-casing None.
    """
    if not file_path.exists():
        print(f"No exclusion file at {file_path}; proceeding without exclusions.")
        return pd.DataFrame(columns=_EXCLUSION_COLUMNS)
    return _load_table(file_path, "exclusion file")


_WEEKLY_OFF_COLUMNS = [
    "Employee ID", "Employee Name", "Weekly Off Days", "Notes",
]


def load_employee_weekly_off_file(file_path):
    """Load per-employee weekly off day overrides (OPTIONAL).

    Schema (one row per employee that deviates from the global
    WEEKLY_OFF_DAYS default):
      Employee ID       -- integer (preferred; matched first)
      Employee Name     -- fallback identifier when ID is missing
      Weekly Off Days   -- comma-separated weekday names, e.g.
                           "Friday,Saturday"
      Notes             -- free text for HR context (optional)

    Employees absent from this file fall back to config.WEEKLY_OFF_DAYS.
    Returns an empty DataFrame with the expected schema when the file
    is missing so callers can treat the feature as a no-op.
    """
    if not file_path.exists():
        print(
            f"No employee weekly-off file at {file_path}; "
            "using global WEEKLY_OFF_DAYS for every employee."
        )
        return pd.DataFrame(columns=_WEEKLY_OFF_COLUMNS)
    return _load_table(file_path, "employee weekly off file")


_ALIAS_COLUMNS = [
    "Old Employee ID", "Current Employee ID", "Employee Name",
    "Source", "Active", "Notes",
]


def load_employee_id_aliases_file(file_path):
    """Load the Old -> Current Employee ID alias map (OPTIONAL).

    Returns an empty DataFrame with the expected schema when the file
    is missing so callers can treat the feature as a no-op.
    """
    if not file_path.exists():
        print(f"No alias file at {file_path}; no historical IDs will be remapped.")
        return pd.DataFrame(columns=_ALIAS_COLUMNS)
    return _load_table(file_path, "employee ID alias file")


def _parse_bool_loose(value):
    """Tolerant bool parser for the Active column on the alias file."""
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not pd.isna(value):
        return bool(value)
    if pd.isna(value):
        return False
    return str(value).strip().lower() in {"true", "yes", "y", "1", "نعم"}


def apply_employee_id_aliases(df, aliases_df, schedules_df=None):
    """Remap historical BioTime IDs to current IDs in the attendance df.

    Returns
        (mapped_df, alias_audit_df, warnings)

    - `mapped_df` is a copy of `df` with `Employee ID` updated where an
      active alias matched, plus four audit columns always added:
        original_employee_id, mapped_employee_id, id_alias_applied,
        alias_source.
    - First Name is filled from the alias's Employee Name ONLY when the
      existing First Name is missing / blank -- the alias never
      overwrites a non-empty name.
    - `alias_audit_df` carries one row per active alias entry (whether
      it matched any rows or not).
    - `warnings` lists validation issues (duplicate Old IDs, Current
      IDs absent from Odoo schedules, ...).

    Inactive aliases (Active=FALSE) are skipped entirely.
    """
    out = df.copy()
    out["original_employee_id"] = out["Employee ID"]
    out["mapped_employee_id"] = out["Employee ID"]
    out["id_alias_applied"] = False
    out["alias_source"] = ""

    warnings = []

    if aliases_df is None or aliases_df.empty:
        return out, pd.DataFrame(columns=[
            "original_employee_id", "mapped_employee_id", "employee_name",
            "alias_source", "records_mapped", "notes",
        ]), warnings

    active = aliases_df.copy()
    if "Active" in active.columns:
        active = active[active["Active"].apply(_parse_bool_loose)]
    if active.empty:
        return out, pd.DataFrame(columns=[
            "original_employee_id", "mapped_employee_id", "employee_name",
            "alias_source", "records_mapped", "notes",
        ]), warnings

    schedule_names = set()
    if schedules_df is not None and "Name" in schedules_df.columns:
        schedule_names = {
            " ".join(str(n).split()).lower()
            for n in schedules_df["Name"].dropna()
        }

    lookup = {}            # old_id -> new_id
    name_lookup = {}       # old_id -> Employee Name (from alias file)
    source_lookup = {}     # old_id -> alias Source
    notes_lookup = {}      # old_id -> Notes
    audit_seed = []        # active alias rows for the audit DataFrame

    for _, row in active.iterrows():
        try:
            old_id = int(row["Old Employee ID"])
            new_id = int(row["Current Employee ID"])
        except (ValueError, TypeError, KeyError):
            continue
        if old_id in lookup and lookup[old_id] != new_id:
            warnings.append(
                f"Old Employee ID {old_id} maps to multiple Current IDs "
                f"({lookup[old_id]} and {new_id}); using the first."
            )
            continue
        if old_id in lookup:
            # Duplicate active row pointing at the SAME current ID;
            # ignore silently (idempotent).
            continue

        emp_name = row.get("Employee Name")
        emp_name_str = "" if pd.isna(emp_name) else str(emp_name).strip()
        if (
            schedule_names
            and emp_name_str
            and " ".join(emp_name_str.split()).lower() not in schedule_names
        ):
            warnings.append(
                f"Alias {old_id} -> {new_id} ({emp_name_str}) has no "
                "matching Odoo schedule entry; mapping still applied."
            )

        lookup[old_id] = new_id
        name_lookup[old_id] = emp_name_str
        source_lookup[old_id] = (
            "" if pd.isna(row.get("Source")) else str(row.get("Source"))
        )
        notes_lookup[old_id] = (
            "" if pd.isna(row.get("Notes")) else str(row.get("Notes"))
        )
        audit_seed.append(old_id)

    if not lookup:
        return out, pd.DataFrame(columns=[
            "original_employee_id", "mapped_employee_id", "employee_name",
            "alias_source", "records_mapped", "notes",
        ]), warnings

    mask = out["Employee ID"].isin(lookup.keys())
    out.loc[mask, "mapped_employee_id"] = out.loc[mask, "Employee ID"].map(lookup)
    out.loc[mask, "alias_source"] = (
        out.loc[mask, "Employee ID"].map(source_lookup).fillna("")
    )
    out.loc[mask, "id_alias_applied"] = True

    # Fill missing First Name with the alias's Employee Name.
    if mask.any():
        for old_id, alias_name in name_lookup.items():
            if not alias_name:
                continue
            sub_mask = mask & (out["Employee ID"] == old_id)
            existing = out.loc[sub_mask, "First Name"]
            blank = existing.isna() | (existing.astype(str).str.strip() == "")
            out.loc[sub_mask & blank, "First Name"] = alias_name

    # Replace Employee ID with the mapped ID -- AFTER everything that
    # needed the original value is computed.
    out.loc[mask, "Employee ID"] = out.loc[mask, "mapped_employee_id"]

    # Build audit (one row per active alias entry, whether it matched
    # any attendance rows or not, so HR can spot configured-but-unused
    # aliases).
    audit_rows = []
    for old_id in audit_seed:
        records = int((df["Employee ID"] == old_id).sum())
        audit_rows.append({
            "original_employee_id": old_id,
            "mapped_employee_id": lookup[old_id],
            "employee_name": name_lookup.get(old_id) or None,
            "alias_source": source_lookup.get(old_id, ""),
            "records_mapped": records,
            "notes": notes_lookup.get(old_id, ""),
        })
    audit_df = pd.DataFrame(audit_rows)
    return out, audit_df, warnings
