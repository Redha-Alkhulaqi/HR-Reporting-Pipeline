import pandas as pd


def load_attendance_file(file_path):
    print("Loading attendance file...")

    preview = pd.read_excel(file_path, header=None)
    print(preview.head(15))

    return preview