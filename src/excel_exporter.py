from openpyxl import Workbook
from datetime import datetime

from config import PROJECT_ROOT


def export_report(data):
    print("Exporting Excel report...")

    wb = Workbook()
    ws = wb.active

    ws.title = "HR Summary"

    ws["A1"] = "HR Reporting Summary"
    ws["A3"] = "Total Employees"
    ws["B3"] = data.get("template_rows")

    ws["A4"] = "Total Columns"
    ws["B4"] = data.get("template_columns")

    output_dir = PROJECT_ROOT / "outputs"
    output_dir.mkdir(exist_ok=True)

    filename = output_dir / f"hr_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"

    wb.save(filename)

    print(f"Report saved: {filename}")