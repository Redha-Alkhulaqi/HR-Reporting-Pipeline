"""Write the monthly HR Excel report with multiple sheets and dashboard charts.

Sheets:
- Dashboard           : KPIs, status / excused tables, embedded charts.
- Employee Summary    : per-employee late + risk + payroll aggregates.
- Daily Attendance    : the full daily DataFrame with every classified row.
- Daily Trend         : per-day counts and unexcused minutes.
- Missing Punches     : days with a Check In but no Check Out (optional).
- Department Summary  : per-department status counts (optional; only when
                        the source data exposed a Department column).

Data sheets get bold blue headers, frozen header row, auto-filter, and
readable column widths. Files are written under
REPORT_OUTPUT_DIR/YYYY-MM/hr_report_YYYYMMDD_HHMMSS.xlsx so each month
is naturally archived.
"""
from datetime import datetime

from openpyxl import Workbook
from openpyxl.chart import BarChart, LineChart, PieChart, Reference
from openpyxl.chart.label import DataLabelList
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.utils.dataframe import dataframe_to_rows

from config import MAX_MONTHLY_DEDUCTION, REPORT_OUTPUT_DIR


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
    """Write a DataFrame anchored at start_row. Returns:
        (header_row, data_start_row, data_end_row, next_blank_row, n_cols).
    """
    n_cols = len(df.columns)
    for r_offset, row in enumerate(dataframe_to_rows(df, index=False, header=True)):
        for c_offset, value in enumerate(row):
            ws.cell(row=start_row + r_offset, column=c_offset + 1, value=value)
    _style_header_row(ws, row=start_row, n_cols=n_cols)
    data_start = start_row + 1
    data_end = start_row + len(df)
    next_row = data_end + 2  # leave one blank row
    return start_row, data_start, data_end, next_row, n_cols


def _build_data_sheet(ws, df):
    """Populate a plain data sheet (header + rows + filter + freeze)."""
    if df is None or df.empty:
        ws.append(["(no data)"])
        return
    for row in dataframe_to_rows(df, index=False, header=True):
        ws.append(row)
    _style_header_row(ws, row=1, n_cols=ws.max_column)
    ws.freeze_panes = "A2"
    if ws.max_row > 1:
        ws.auto_filter.ref = ws.dimensions
    _autosize_columns(ws)


def _pie_chart(title, labels, values):
    chart = PieChart()
    chart.title = title
    chart.add_data(values, titles_from_data=False)
    chart.set_categories(labels)
    chart.width = 14
    chart.height = 9
    chart.dataLabels = DataLabelList(showPercent=True)
    return chart


def _bar_chart(title, labels, values, horizontal=False):
    chart = BarChart()
    if horizontal:
        chart.type = "bar"
    chart.title = title
    chart.legend = None
    chart.add_data(values, titles_from_data=False)
    chart.set_categories(labels)
    chart.width = 18
    chart.height = 10
    return chart


def _line_chart(title, labels, values_with_header):
    chart = LineChart()
    chart.title = title
    chart.add_data(values_with_header, titles_from_data=True)
    chart.set_categories(labels)
    chart.width = 22
    chart.height = 10
    return chart


def _build_dashboard(wb, summary):
    ws = wb["Dashboard"]
    ws["A1"] = "HR Reporting Dashboard"
    ws["A1"].font = _TITLE_FONT
    ws.merge_cells("A1:B1")

    kpis = [
        ("Reporting Population (employees with check-ins)",
         summary.get("reporting_population", summary.get("total_employees"))),
        ("Late Cases", summary["late_cases"]),
        ("Total Late Minutes (Unexcused)", summary["total_late_minutes"]),
        ("Approved Excuse Cases", summary["approved_excuse_cases"]),
        ("Leave Cases", summary["leave_cases"]),
        ("Missing Schedule Cases", summary["missing_schedule_cases"]),
        ("Missing Check-Out Cases", summary["missing_check_out_cases"]),
        ("Excused Delay Minutes", summary["excused_delay_minutes"]),
        ("High Risk Employees", summary.get("high_risk_employees", 0)),
        ("Estimated Deduction (uncapped)", summary.get("total_estimated_deduction", 0)),
        (
            f"Estimated Deduction (capped at {MAX_MONTHLY_DEDUCTION:.0f}/employee)",
            summary.get("total_deduction_capped", 0),
        ),
    ]
    ws.cell(row=3, column=1, value="Metric")
    ws.cell(row=3, column=2, value="Value")
    _style_header_row(ws, row=3, n_cols=2)
    for i, (label, value) in enumerate(kpis, start=4):
        ws.cell(row=i, column=1, value=label)
        ws.cell(row=i, column=2, value=value)

    next_row = 4 + len(kpis) + 1

    # Status breakdown table + pie chart anchored to its right.
    ws.cell(row=next_row, column=1, value="Attendance Status Breakdown").font = _SECTION_FONT
    next_row += 1
    header_row, status_start, status_end, next_row, _ = _write_dataframe(
        ws, summary["status_summary"], next_row
    )
    pie_labels = Reference(ws, min_col=1, min_row=status_start, max_row=status_end)
    pie_values = Reference(ws, min_col=2, min_row=status_start, max_row=status_end)
    ws.add_chart(
        _pie_chart("Attendance Status Breakdown", pie_labels, pie_values),
        f"E{header_row - 1}",
    )

    # Excused vs Unexcused table + bar chart.
    ws.cell(row=next_row, column=1, value="Excused vs Unexcused").font = _SECTION_FONT
    next_row += 1
    header_row, evu_start, evu_end, next_row, _ = _write_dataframe(
        ws, summary["excused_vs_unexcused"], next_row
    )
    evu_labels = Reference(ws, min_col=1, min_row=evu_start, max_row=evu_end)
    evu_values = Reference(ws, min_col=2, min_row=evu_start, max_row=evu_end)
    ws.add_chart(
        _bar_chart("Excused vs Unexcused (minutes)", evu_labels, evu_values),
        f"E{header_row - 1}",
    )

    # Top 10 Late Employees bar chart -- references Employee Summary sheet.
    emp_ws = wb["Employee Summary"]
    if emp_ws.max_row > 1:
        n_rows = min(10, emp_ws.max_row - 1)
        last_row = 1 + n_rows
        # Employee Summary cols (1-based): 1=Employee ID, 2=First Name,
        # 3=total_late_minutes, 4=late_count, ... Plot First Name -> minutes.
        labels = Reference(emp_ws, min_col=2, min_row=2, max_row=last_row)
        values = Reference(emp_ws, min_col=3, min_row=2, max_row=last_row)
        ws.cell(row=next_row, column=1, value="Top 10 Late Employees").font = _SECTION_FONT
        ws.add_chart(
            _bar_chart(
                "Top 10 Late Employees (Unexcused Minutes)",
                labels, values, horizontal=True,
            ),
            f"A{next_row + 1}",
        )
        next_row += 22

    # Daily Trend line chart -- references Daily Trend sheet.
    if "Daily Trend" in wb.sheetnames:
        trend_ws = wb["Daily Trend"]
        if trend_ws.max_row > 1:
            last_row = trend_ws.max_row
            # Daily Trend cols: 1=Date, 2=total_records, 3=late_cases, ...
            labels = Reference(trend_ws, min_col=1, min_row=2, max_row=last_row)
            values = Reference(trend_ws, min_col=3, min_row=1, max_row=last_row)
            ws.cell(row=next_row - 22, column=13, value="Daily Trend").font = _SECTION_FONT
            ws.add_chart(
                _line_chart("Daily Trend: Late Cases", labels, values),
                f"M{next_row - 21}",
            )

    # Employee Reconciliation section -- makes the headline count auditable.
    reconciliation = summary.get("employee_reconciliation")
    if reconciliation is not None and not reconciliation.empty:
        ws.cell(row=next_row, column=1,
                value="Employee Reconciliation").font = _SECTION_FONT
        next_row += 1
        ws.cell(
            row=next_row, column=1,
            value=(
                "Reporting Population is what this report publishes. The "
                "other rows explain why it can differ from Odoo / BioTime."
            ),
        )
        next_row += 2
        _, _, _, next_row, _ = _write_dataframe(ws, reconciliation, next_row)

    ws.column_dimensions["A"].width = 42
    ws.column_dimensions["B"].width = 22
    ws.column_dimensions["C"].width = 38
    ws.column_dimensions["D"].width = 70


def export_report(summary, daily):
    print("Exporting Excel report...")

    wb = Workbook()
    wb.active.title = "Dashboard"

    # Always-present data sheets, in this tab order.
    _build_data_sheet(wb.create_sheet("Employee Summary"), summary["employee_summary"])
    _build_data_sheet(wb.create_sheet("Daily Attendance"), daily)
    _build_data_sheet(wb.create_sheet("Daily Trend"), summary.get("daily_trend"))

    # Optional sheets -- only added when data is available.
    missing_punches = summary.get("missing_punch_summary")
    if missing_punches is not None and not missing_punches.empty:
        _build_data_sheet(wb.create_sheet("Missing Punches"), missing_punches)

    department_summary = summary.get("department_summary")
    if department_summary is not None and not department_summary.empty:
        _build_data_sheet(wb.create_sheet("Department Summary"), department_summary)

    reconciliation_details = summary.get("employee_reconciliation_details")
    if reconciliation_details is not None and not reconciliation_details.empty:
        _build_data_sheet(
            wb.create_sheet("Employee Reconciliation Details"),
            reconciliation_details,
        )

    # Build Dashboard LAST so it can reference positions on the other sheets.
    _build_dashboard(wb, summary)

    now = datetime.now()
    monthly_dir = REPORT_OUTPUT_DIR / now.strftime("%Y-%m")
    monthly_dir.mkdir(parents=True, exist_ok=True)
    filename = monthly_dir / f"hr_report_{now.strftime('%Y%m%d_%H%M%S')}.xlsx"
    wb.save(filename)

    print(f"Report saved: {filename}")
