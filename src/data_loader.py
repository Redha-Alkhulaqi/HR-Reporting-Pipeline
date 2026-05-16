import pandas as pd


def load_attendance_file(file_path):
    print(f"Loading attendance file: {file_path}")

    suffix = file_path.suffix.lower()

    if suffix == ".csv":
        df = pd.read_csv(file_path)

    elif suffix in [".xlsx", ".xls"]:
        df = pd.read_excel(file_path, header=1)

    else:
        raise ValueError(f"Unsupported file type: {suffix}")

    return df

def load_working_schedule_file(file_path):
    print(f"Loading working schedule file: {file_path}")

    suffix = file_path.suffix.lower()

    if suffix == ".csv":
        df = pd.read_csv(file_path)
    elif suffix in [".xlsx", ".xls"]:
        df = pd.read_excel(file_path)
    else:
        raise ValueError(f"Unsupported file type: {suffix}")

    return df