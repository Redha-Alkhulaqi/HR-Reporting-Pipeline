from data_loader import load_attendance_file
from attendance_engine import process_attendance
from report_generator import generate_report
from excel_exporter import export_report


def main():
    print("Starting HR Reporting Pipeline...")

    df = load_attendance_file(
        "HR_Monthly_Report_Template.xlsx"
    )

    attendance_data = process_attendance(df)

    generate_report(attendance_data)

    export_report(attendance_data)

    print("Pipeline completed successfully.")


if __name__ == "__main__":
    main()