def process_attendance(df):
    print("Processing attendance data...")

    summary = {
        "template_rows": len(df),
        "template_columns": len(df.columns),
        "report_type": "Monthly HR Attendance Report Template"
    }

    return summary