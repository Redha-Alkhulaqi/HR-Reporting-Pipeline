"""Write the monthly HR Excel report with three sheets:

- Dashboard          : headline KPIs plus status / excused-vs-unexcused tables.
- Employee Summary   : per-employee late aggregates + risk tier.
- Daily Attendance   : full daily DataFrame with every classified row.

Data sheets get bold blue headers, frozen header row, auto-filter, and
readable column widths so the file is usable as a quick HR worksheet.
"""
from datetime import datetime

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.utils.dataframe import dataframe_to_rows

from config import REPORT_OUTPUT_DIR


_HEADER_FONT = Font(bold=True, color="FFFFFF")
_HEADER_FILL = PatternFill("solid", fgColor="305496")
_TITLE_FONT = Font(bold=True, size=14, color="1F4E79")
_SECTION_FONT = Font(bold=True, size=12, color="1F4E79")
_CENTER = Alignment(horizontal="center", vertical="center")


def _style_header_row(ws, row, n_cols):
    for col in range(1, n_cols + 1):
        cell = ws.cell(row=row, column=col)
        cell.font = _HEADER_FONT
        cell.fill = _HEADER_FILL
        cell.alignment = _CENTER


def _autosize_columns(ws, min_width=12, max_width=45):
    for col_idx, col_cells in enumerate(ws.columns, start=1):
        longest = 0
        for cell in col_cells:
            if cell.value is None:
                continue
            longest = max(longest, len(str(cell.value)))
        width = min(max_width, max(min_width, longest + 2))
        ws.column_dimensions[get_column_letter(col_idx)].width = width


def _write_dataframe(ws, df, start_row):
    """Write DataFrame at start_row and return the next blank row."""
    n_cols = len(df.columns)
    for r_offset, row in enumerate(
        dataframe_to_rows(df, index=False, header=True)
    ):
        for c_offset, value in enumerate(row):
            ws.cell(row=start_row + r_offset, column=c_offset + 1, value=value)
    _style_header_row(ws, row=start_row, n_cols=n_cols)
    return start_row + 1 + len(df) + 1  # header + data + one blank


def _build_dashboard(ws, summary):
    ws["A1"] = "HR Reporting Dashboard"
    ws["A1"].font = _TITLE_FONT
    ws.merge_cells("A1:B1")

    kpis = [
        ("Total Employees", summary["total_employees"]),
        ("Late Cases", summary["late_cases"]),
        ("Total Late Minutes (Unexcused)", summary["total_late_minutes"]),
        ("Approved Excuse Cases", summary["approved_excuse_cases"]),
        ("Leave Cases", summary["leave_cases"]),
        ("Missing Schedule Cases", summary["missing_schedule_cases"]),
        ("Excused Delay Minutes", summary["excused_delay_minutes"]),
    ]
    ws.cell(row=3, column=1, value="Metric")
    ws.cell(row=3, column=2, value="Value")
    _style_header_row(ws, row=3, n_cols=2)
    for i, (label, value) in enumerate(kpis, start=4):
        ws.cell(row=i, column=1, value=label)
        ws.cell(row=i, column=2, value=value)

    next_row = 4 + len(kpis) + 1
    ws.cell(row=next_row, column=1, value="Attendance Status Breakdown").font = _SECTION_FONT
    next_row += 1
    next_row = _write_dataframe(ws, summary["status_summary"], next_row)

    ws.cell(row=next_row, column=1, value="Excused vs Unexcused").font = _SECTION_FONT
    next_row += 1
    _write_dataframe(ws, summary["excused_vs_unexcused"], next_row)

    ws.column_dimensions["A"].width = 36
    ws.column_dimensions["B"].width = 22
    ws.column_dimensions["C"].width = 24
    ws.column_dimensions["D"].width = 24


def _build_data_sheet(ws, df):
    for row in dataframe_to_rows(df, index=False, header=True):
        ws.append(row)
    if ws.max_row >= 1:
        _style_header_row(ws, row=1, n_cols=ws.max_column)
        ws.freeze_panes = "A2"
        if ws.max_row > 1:
            ws.auto_filter.ref = ws.dimensions
    _autosize_columns(ws)


def export_report(summary, daily):
    print("Exporting Excel report...")

    wb = Workbook()
    dashboard = wb.active
    dashboard.title = "Dashboard"
    _build_dashboard(dashboard, summary)

    emp_sheet = wb.create_sheet("Employee Summary")
    _build_data_sheet(emp_sheet, summary["employee_summary"])

    daily_sheet = wb.create_sheet("Daily Attendance")
    _build_data_sheet(daily_sheet, daily)

    REPORT_OUTPUT_DIR.mkdir(exist_ok=True)
    filename = REPORT_OUTPUT_DIR / f"hr_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    wb.save(filename)

    print(f"Report saved: {filename}")
