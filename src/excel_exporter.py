from openpyxl import Workbook
from openpyxl.utils.dataframe import dataframe_to_rows
from datetime import datetime

from config import REPORT_OUTPUT_DIR


def export_report(summary, daily):
    print("Exporting Excel report...")

    wb = Workbook()
    ws = wb.active
    ws.title = "HR Summary"

    ws["A1"] = "HR Reporting Summary"
    ws["A3"] = "Total Employees"
    ws["B3"] = summary.get("total_employees")

    ws["A4"] = "Late Cases"
    ws["B4"] = summary.get("late_cases")

    ws["A5"] = "Total Late Minutes"
    ws["B5"] = summary.get("total_late_minutes")

    detail = wb.create_sheet("Daily Attendance")
    for row in dataframe_to_rows(daily, index=False, header=True):
        detail.append(row)

    REPORT_OUTPUT_DIR.mkdir(exist_ok=True)

    filename = REPORT_OUTPUT_DIR / f"hr_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"

    wb.save(filename)

    print(f"Report saved: {filename}")
