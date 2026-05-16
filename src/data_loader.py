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


def load_working_schedule_file(file_path):
    return _load_table(file_path, "working schedule file")


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
