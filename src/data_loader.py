import pandas as pd


def load_attendance_file(file_path):
    print("Loading attendance file...")

    # Row 0 of the export is a stray "Transaction" banner; real headers are on row 1.
    df = pd.read_excel(file_path, header=1)

    return df