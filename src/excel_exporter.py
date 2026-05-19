"""Write the monthly HR Excel report.

Sheets (in tab order):
- Dashboard                     : executive view -- KPIs + 4 charts.
- Employee Summary              : per-employee late/risk/payroll aggregates.
- Daily Attendance              : every classified employee-day row.
- Daily Trend                   : per-day status counts.
- Missing Punches               : check-in days with no check-out (optional).
- Department Summary            : per-department status counts (optional).
- Employee Reconciliation Details: per-ID audit table.
- Employee Master               : every employee + HR audit flags.
- Overtime                      : rows where overtime actually happened.
- Excluded Employees            : policy exclusions (optional).
- Reconciliation                : the high-level employee-count taxonomy.

Detail sheets get bold blue headers, frozen header row, auto-filter,
and auto-fit column widths. Files land in
REPORT_OUTPUT_DIR/YYYY-MM/hr_report_YYYYMMDD_HHMMSS.xlsx.

The Dashboard is deliberately uncluttered: KPIs on the left, four
consistently-sized charts in a 2x2 grid on the right, gridlines hidden,
and the small backing tables placed below the charts so they do not
crowd the visual area.
"""
from datetime import datetime

import pandas as pd
from openpyxl import Workbook
from openpyxl.chart import BarChart, LineChart, PieChart, Reference
from openpyxl.chart.label import DataLabelList
from openpyxl.formatting.rule import FormulaRule
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.utils.dataframe import dataframe_to_rows

from config import REPORT_OUTPUT_DIR


_HEADER_FONT = Font(bold=True, color="FFFFFF")
_HEADER_FILL = PatternFill("solid", fgColor="305496")
_TITLE_FONT = Font(bold=True, size=16, color="1F4E79")
_SECTION_FONT = Font(bold=True, size=11, color="1F4E79")
_CENTER = Alignment(horizontal="center", vertical="center")

# Every chart on the Dashboard uses the same size so the 2x2 grid stays
# aligned and no chart overlaps a neighbour.
_CHART_WIDTH = 13
_CHART_HEIGHT = 7


def _sanitize_row(row):
    """Convert pandas NA / NaT / NaN to None so openpyxl serializes the
    cell as empty instead of raising 'Cannot convert <NA> to Excel'."""
    return [None if pd.isna(v) else v for v in row]


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
        for c_offset, value in enumerate(_sanitize_row(row)):
            ws.cell(row=start_row + r_offset, column=c_offset + 1, value=value)
    _style_header_row(ws, row=start_row, n_cols=n_cols)
    data_start = start_row + 1
    data_end = start_row + len(df)
    next_row = data_end + 2  # one blank row of breathing room
    return start_row, data_start, data_end, next_row, n_cols


def _build_data_sheet(ws, df):
    """Populate a plain data sheet (header + rows + filter + freeze)."""
    if df is None or df.empty:
        ws.append(["(no data)"])
        return
    for row in dataframe_to_rows(df, index=False, header=True):
        ws.append(_sanitize_row(row))
    _style_header_row(ws, row=1, n_cols=ws.max_column)
    ws.freeze_panes = "A2"
    if ws.max_row > 1:
        ws.auto_filter.ref = ws.dimensions
    _autosize_columns(ws)


_PAYABLE_OT_HEADER_FILL = PatternFill("solid", fgColor="548235")   # dark green
_PAYABLE_OT_BODY_FILL = PatternFill("solid", fgColor="E2EFDA")      # light green
_PAYABLE_OT_BORDER_GREEN = "548235"
_NOTES_TITLE_FILL = PatternFill("solid", fgColor="DDEBF7")          # light blue


def _apply_payable_overtime_styling(ws, payable_col_idx):
    """Highlight the payable-overtime column to match the mockup.

    - Header cell: dark-green fill with the standard bold-white font.
    - Body cells: light-green fill, bold black font, centered.
    - Adds a thin dark-green border on left/right of every body cell
      and on the bottom of the header so the column visually pops
      out from its neighbors (like the green block in the mockup).
    """
    from openpyxl.styles import Border, Side
    if ws.max_row < 1:
        return
    header_cell = ws.cell(row=1, column=payable_col_idx)
    header_cell.fill = _PAYABLE_OT_HEADER_FILL
    # The shared _style_header_row already set white-bold font; keep it.

    side = Side(style="thin", color=_PAYABLE_OT_BORDER_GREEN)
    body_border = Border(left=side, right=side)
    header_border = Border(left=side, right=side, bottom=side)
    header_cell.border = header_border

    for r in range(2, ws.max_row + 1):
        cell = ws.cell(row=r, column=payable_col_idx)
        cell.fill = _PAYABLE_OT_BODY_FILL
        cell.font = Font(bold=True, color="000000")
        cell.alignment = _CENTER
        cell.border = body_border


def _append_executive_notes_block(ws, n_cols):
    """Append the Overtime Payable notes block at the bottom of the
    Employee Summary sheet, matching the mockup.

    The block sits two rows below the data, spans 6 columns, and
    documents the formula, rounding, and that actual overtime is
    preserved alongside payable.
    """
    start_row = ws.max_row + 3

    # Title row.
    title_cell = ws.cell(row=start_row, column=1, value=
                         "Notes -- Overtime Payable (1.5x)")
    title_cell.font = Font(bold=True, size=12, color="1F4E79")
    title_cell.fill = _NOTES_TITLE_FILL
    ws.merge_cells(start_row=start_row, start_column=1,
                   end_row=start_row, end_column=min(6, n_cols))

    notes = [
        "Overtime is now reported with a payroll multiplier of 1.5 for all employees.",
        "Column H (Total Over Time (Hours) (Actual)) = Actual overtime duration "
        "(physical hours worked beyond schedule).",
        "Column I (Total Over Time (Payable 1.5x) (Hours)) = Payable overtime "
        "hours after applying the 1.5x multiplier.",
        "Formula: Payable Overtime (Hours) = Actual Overtime (Hours) x 1.5.",
        "Rounding: per-row payable minutes are rounded half-up to the nearest "
        "minute; hour values displayed to 1 decimal place.",
        "Actual overtime hours are preserved for audit and operational analysis. "
        "Payable overtime hours apply the global 1.5x payroll multiplier.",
        "Multiplier is configured via OVERTIME_PAY_MULTIPLIER; set to 1.0 to "
        "disable the premium without code changes.",
    ]
    for i, line in enumerate(notes, start=1):
        c = ws.cell(row=start_row + i, column=1, value=f"•  {line}")
        c.alignment = Alignment(wrap_text=True, vertical="top")
        ws.merge_cells(start_row=start_row + i, start_column=1,
                       end_row=start_row + i,
                       end_column=min(6, n_cols))
        ws.row_dimensions[start_row + i].height = 18

    # Closing badge row.
    badge_row = start_row + len(notes) + 1
    badge = ws.cell(row=badge_row, column=1,
                    value="Multiplier applied globally: 1.5x")
    badge.font = Font(bold=True, color="1F4E79")
    badge.fill = _NOTES_TITLE_FILL
    badge.alignment = _CENTER
    ws.merge_cells(start_row=badge_row, start_column=1,
                   end_row=badge_row, end_column=min(6, n_cols))


def _build_executive_employee_sheet(ws, df):
    """Render the simplified executive Employee Summary sheet.

    12 columns expected (in this exact order):
        1.  Employee ID
        2.  First Name
        3.  No of Absence Days
        4.  No of Permission Days
        5.  No of Vacation Days
        6.  No of Secondment Days
        7.  Total Late (Hours)
        8.  Total Over Time (Hours) (Actual)
        9.  Total Over Time (Payable 1.5x) (Hours)
        10. Total Early Leave (Hours)
        11. Break Time (Hours)
        12. Break Time (After Policy)

    Apply executive-friendly formatting: bold header, frozen header,
    auto-filter, centered numeric cells, thousands separator on
    integer columns, 1-decimal display on all hour columns. Column 9
    (Payable Overtime) is visually highlighted in green to mirror the
    mockup, and a Notes block is appended below the data describing
    the formula, rounding, and audit semantics.
    """
    if df is None or df.empty:
        ws.append(["(no data)"])
        return
    for row in dataframe_to_rows(df, index=False, header=True):
        ws.append(_sanitize_row(row))
    _style_header_row(ws, row=1, n_cols=ws.max_column)
    ws.freeze_panes = "A2"
    data_end_row = ws.max_row
    if data_end_row > 1:
        ws.auto_filter.ref = (
            f"A1:{get_column_letter(ws.max_column)}{data_end_row}"
        )
    _autosize_columns(ws)

    if data_end_row <= 1:
        return

    # Integer columns: Employee ID (1) plus the three whole-day-count
    # columns (4 Permission, 5 Vacation, 6 Secondment). Absence (col 3)
    # is rendered at 1 decimal because split-shift partial-attendance
    # days contribute fractional (e.g. 0.5) values to it.
    _format_numeric_cells(
        ws,
        rows=range(2, data_end_row + 1),
        cols=[1, 4, 5, 6],
        number_format="#,##0",
    )
    # 1-decimal columns: 3 Absence (fractional for split-shift),
    # 7 Late, 8 OT Actual, 9 OT Payable, 10 Early Leave, 11 Break,
    # 12 Break After Policy.
    _format_numeric_cells(
        ws,
        rows=range(2, data_end_row + 1),
        cols=[3, 7, 8, 9, 10, 11, 12],
        number_format="0.0",
    )
    # Visually highlight the payable-overtime column.
    _apply_payable_overtime_styling(ws, payable_col_idx=9)
    # Append the Notes block at the bottom.
    _append_executive_notes_block(ws, n_cols=ws.max_column)


def _apply_daily_conditional_formatting(ws, df):
    """Color-code each row in Daily Attendance by its dominant status.

    Rules are applied in priority order with stopIfTrue=True so each
    row gets exactly one fill, picking the most important condition.
    Priority (highest first): Excluded, Leave, Approved Excuse, Late,
    Early Leave, Missing Check Out, Overtime.
    """
    if df is None or df.empty:
        return

    cols = list(df.columns)
    n_rows = len(df)
    n_cols = len(cols)
    last_col_letter = get_column_letter(n_cols)
    range_str = f"A2:{last_col_letter}{n_rows + 1}"

    def col_letter(name):
        return get_column_letter(cols.index(name) + 1) if name in cols else None

    is_late_L = col_letter("is_late")
    unexcused_L = col_letter("unexcused_delay_minutes")
    overtime_L = col_letter("overtime_minutes")
    early_leave_L = col_letter("early_leave_minutes")
    early_leave_anomaly_L = col_letter("early_leave_anomaly")
    missing_co_L = col_letter("missing_check_out")
    status_L = col_letter("attendance_status")
    excluded_L = col_letter("is_excluded")

    def _add(formula, fill_hex, font_hex="000000", bold=False):
        ws.conditional_formatting.add(
            range_str,
            FormulaRule(
                formula=[formula],
                fill=PatternFill("solid", fgColor=fill_hex),
                font=Font(bold=bold, color=font_hex),
                stopIfTrue=True,
            ),
        )

    # Priority order (highest first). The first matching rule wins per row.
    if excluded_L:
        _add(f"${excluded_L}2=TRUE", "C8A2C8")              # light violet
    if status_L:
        _add(f'${status_L}2="Leave"', "BFBFBF")             # gray
        _add(f'${status_L}2="Approved Excuse"', "DDEBF7")   # light blue
    # Early-leave ANOMALIES outrank normal Late/Early Leave so HR can
    # spot data-quality issues at a glance (distinct dark red).
    if early_leave_anomaly_L:
        _add(
            f"${early_leave_anomaly_L}2=TRUE",
            "8B0000", font_hex="FFFFFF", bold=True,         # dark red + white bold
        )
    if is_late_L and unexcused_L:
        _add(
            f"OR(${is_late_L}2=TRUE,${unexcused_L}2>0)",
            "C00000", font_hex="FFFFFF", bold=True,         # red + white bold
        )
    if early_leave_L:
        _add(f"${early_leave_L}2>0", "CC6600",
             font_hex="FFFFFF")                              # dark orange
    if missing_co_L:
        _add(f"${missing_co_L}2=TRUE", "FFFF00", bold=True)  # yellow
    if overtime_L:
        _add(f"${overtime_L}2>0", "C6EFCE",
             font_hex="006100")                              # green


def _pie_chart(title, labels, values):
    chart = PieChart()
    chart.title = title
    chart.add_data(values, titles_from_data=False)
    chart.set_categories(labels)
    chart.width = _CHART_WIDTH
    chart.height = _CHART_HEIGHT
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
    chart.width = _CHART_WIDTH
    chart.height = _CHART_HEIGHT
    return chart


def _line_chart(title, labels, values_with_header):
    chart = LineChart()
    chart.title = title
    chart.legend = None
    chart.add_data(values_with_header, titles_from_data=True)
    chart.set_categories(labels)
    chart.width = _CHART_WIDTH
    chart.height = _CHART_HEIGHT
    return chart


def _format_numeric_cells(ws, rows, cols, number_format):
    """Apply thousands separator + centered alignment to a numeric block."""
    align = Alignment(horizontal="center", vertical="center")
    for row in rows:
        for col in cols:
            cell = ws.cell(row=row, column=col)
            cell.number_format = number_format
            cell.alignment = align


def _build_dashboard(wb, summary):
    ws = wb["Dashboard"]
    # Clean executive look: no gridlines.
    ws.sheet_view.showGridLines = False

    # Title bar.
    ws.merge_cells("A1:O1")
    title = ws["A1"]
    title.value = "HR Reporting Dashboard"
    title.font = _TITLE_FONT
    title.alignment = Alignment(horizontal="left", vertical="center")

    # ---------------- Executive KPIs (cols A-B) ----------------
    kpis = [
        ("Reporting Population",
         summary.get("reporting_population", summary.get("total_employees", 0)),
         "#,##0"),
        ("Data Quality Score", summary.get("data_quality_score", 0), "0.0"),
        ("Late Cases", summary["late_cases"], "#,##0"),
        ("Total Late Minutes (Unexcused)", summary["total_late_minutes"], "#,##0"),
        ("Approved Excuse Cases", summary["approved_excuse_cases"], "#,##0"),
        ("Leave Cases", summary["leave_cases"], "#,##0"),
        ("Missing Schedule Cases", summary["missing_schedule_cases"], "#,##0"),
        ("Missing Check-Out Cases", summary["missing_check_out_cases"], "#,##0"),
        ("High Risk Employees", summary.get("high_risk_employees", 0), "#,##0"),
        ("Excluded Employees", summary.get("excluded_employee_count", 0), "#,##0"),
        ("Estimated Deduction (capped)", summary.get("total_deduction_capped", 0),
         "#,##0.00"),
        ("Overtime Cases", summary.get("overtime_cases", 0), "#,##0"),
        ("Total Over Time (Hours) (Actual)",
         summary.get("total_overtime_hours", 0), "0.0"),
        ("Overtime Multiplier",
         summary.get("overtime_multiplier", 1.0), "0.00"),
        ("Total Over Time (Payable 1.5x) (Hours)",
         summary.get("total_overtime_payable_hours", 0), "0.00"),
        ("Early Leave Cases", summary.get("early_leave_cases", 0), "#,##0"),
        ("Total Early Leave Minutes", summary.get("total_early_leave_minutes", 0), "#,##0"),
        ("Early Leave Anomalies (review)",
         summary.get("early_leave_anomaly_cases", 0), "#,##0"),
        # Break analytics -- INFORMATIONAL only, no charts.
        ("Total Break Count (info)", summary.get("total_break_count", 0), "#,##0"),
        ("Total Break Minutes (info)", summary.get("total_break_minutes", 0), "#,##0"),
        ("Employees With Breaks (info)",
         summary.get("employees_with_breaks", 0), "#,##0"),
        ("Incomplete Break Records (info)",
         summary.get("incomplete_break_records", 0), "#,##0"),
        # Employee ID alias mapping -- INFORMATIONAL.
        ("Aliases Used (info)",
         summary.get("employee_id_aliases_used", 0), "#,##0"),
        ("Alias Records Mapped (info)",
         summary.get("employee_id_alias_records_mapped", 0), "#,##0"),
    ]
    ws.cell(row=3, column=1, value="Metric")
    ws.cell(row=3, column=2, value="Value")
    _style_header_row(ws, row=3, n_cols=2)
    centered = Alignment(horizontal="center", vertical="center")
    for i, (label, value, fmt) in enumerate(kpis, start=4):
        ws.cell(row=i, column=1, value=label)
        cell = ws.cell(row=i, column=2, value=value)
        cell.number_format = fmt
        cell.alignment = centered

    # ---------------- Backing tables (placed below the charts) ----------------
    # Each chart is 13cm x 7cm (~18 rows tall). Three chart rows anchored
    # at rows 3 / 22 / 41 cover roughly rows 3-58, so data tables start
    # safely at row 60.
    DATA_START = 60
    section_label = ws.cell(row=DATA_START, column=1, value="Underlying Data")
    section_label.font = _SECTION_FONT

    # Attendance Status table (drives Chart 1).
    ws.cell(row=DATA_START + 1, column=1,
            value="Attendance Status").font = _SECTION_FONT
    status_df = summary["status_summary"]
    _, status_data_start, status_data_end, status_next, status_n_cols = _write_dataframe(
        ws, status_df, DATA_START + 2
    )
    _format_numeric_cells(
        ws,
        rows=range(status_data_start, status_data_end + 1),
        cols=range(2, status_n_cols + 1),
        number_format="#,##0",
    )

    # Top Overtime table (simplified). Drives Chart 4.
    top_overtime_full = summary.get("top_overtime_employees")
    if top_overtime_full is not None and not top_overtime_full.empty:
        simplified_overtime = top_overtime_full[[
            "Employee ID", "First Name",
            "total_overtime_minutes", "total_overtime_hours",
        ]].head(10).copy()
    else:
        simplified_overtime = pd.DataFrame(columns=[
            "Employee ID", "First Name",
            "total_overtime_minutes", "total_overtime_hours",
        ])
    ws.cell(row=status_next, column=1,
            value="Top Overtime Employees").font = _SECTION_FONT
    _, ot_data_start, ot_data_end, ot_next, _ = _write_dataframe(
        ws, simplified_overtime, status_next + 1
    )
    if not simplified_overtime.empty:
        # Employee ID col 1 and total_overtime_minutes col 3 use integer
        # thousands format; total_overtime_hours col 4 uses one decimal.
        _format_numeric_cells(
            ws,
            rows=range(ot_data_start, ot_data_end + 1),
            cols=[1, 3],
            number_format="#,##0",
        )
        _format_numeric_cells(
            ws,
            rows=range(ot_data_start, ot_data_end + 1),
            cols=[4],
            number_format="0.0",
        )

    # Top Early Leave table -- mirrors the Top Overtime layout: 4 cols
    # ending with the hours conversion for an executive-friendly read.
    top_el_full = summary.get("top_early_leave_employees")
    el_cols = [
        "Employee ID", "First Name",
        "total_early_leave_minutes", "total_early_leave_hours",
    ]
    if top_el_full is not None and not top_el_full.empty:
        simplified_el = top_el_full[el_cols].head(10).copy()
    else:
        simplified_el = pd.DataFrame(columns=el_cols)
    ws.cell(row=ot_next, column=1,
            value="Top Early Leave Employees").font = _SECTION_FONT
    _, el_data_start, el_data_end, _, _ = _write_dataframe(
        ws, simplified_el, ot_next + 1
    )
    if not simplified_el.empty:
        _format_numeric_cells(
            ws,
            rows=range(el_data_start, el_data_end + 1),
            cols=[1, 3],
            number_format="#,##0",
        )
        _format_numeric_cells(
            ws,
            rows=range(el_data_start, el_data_end + 1),
            cols=[4],
            number_format="0.0",
        )

    # ---------------- Charts: 2x2 grid (anchors leave visual buffer) ----------------
    # Layout:
    #   C3   K3
    #   C22  K22
    # Each chart 13cm x 7cm leaves a column/row of breathing room between
    # neighbours and above the underlying-data section that starts at row 42.

    # Chart 1 (top-left): Attendance Status pie.
    pie_labels = Reference(ws, min_col=1,
                           min_row=status_data_start, max_row=status_data_end)
    pie_values = Reference(ws, min_col=2,
                           min_row=status_data_start, max_row=status_data_end)
    ws.add_chart(
        _pie_chart("Attendance Status Breakdown", pie_labels, pie_values),
        "C3",
    )

    # Chart 2 (top-right): Daily Late Trend (references Daily Trend sheet).
    if "Daily Trend" in wb.sheetnames:
        trend_ws = wb["Daily Trend"]
        if trend_ws.max_row > 1:
            # Daily Trend cols: 1=Date, 2=total_records, 3=late_cases, ...
            trend_labels = Reference(
                trend_ws, min_col=1, min_row=2, max_row=trend_ws.max_row
            )
            trend_values = Reference(
                trend_ws, min_col=3, min_row=1, max_row=trend_ws.max_row
            )
            ws.add_chart(
                _line_chart("Daily Late Trend", trend_labels, trend_values),
                "K3",
            )

    # Chart 3 (bottom-left): Top Late Employees -- references the
    # simplified Employee Summary sheet which is sorted by
    # Total Late (Hours) desc.
    emp_ws = wb["Employee Summary"]
    if emp_ws.max_row > 1:
        n_late = min(10, emp_ws.max_row - 1)
        # Executive sheet cols: 1=Employee ID, 2=First Name,
        # 3=Absence, 4=Permission, 5=Vacation, 6=Secondment,
        # 7=Total Late (Hours), 8=Overtime, 9=Early Leave,
        # 10=Break, 11=Break After Policy.
        late_labels = Reference(emp_ws, min_col=2, min_row=2, max_row=1 + n_late)
        late_values = Reference(emp_ws, min_col=7, min_row=2, max_row=1 + n_late)
        ws.add_chart(
            _bar_chart("Top Late Employees", late_labels, late_values,
                       horizontal=True),
            "C22",
        )

    # Chart 4 (bottom-right): Top Overtime Employees.
    if ot_data_end >= ot_data_start and not simplified_overtime.empty:
        ot_labels = Reference(ws, min_col=2,
                              min_row=ot_data_start, max_row=ot_data_end)
        ot_values = Reference(ws, min_col=3,
                              min_row=ot_data_start, max_row=ot_data_end)
        ws.add_chart(
            _bar_chart("Top Overtime Employees", ot_labels, ot_values,
                       horizontal=True),
            "K22",
        )

    # Chart 5 (third row, left): Top Early Leave Employees.
    if el_data_end >= el_data_start and not simplified_el.empty:
        el_labels = Reference(ws, min_col=2,
                              min_row=el_data_start, max_row=el_data_end)
        el_values = Reference(ws, min_col=3,
                              min_row=el_data_start, max_row=el_data_end)
        ws.add_chart(
            _bar_chart("Top Early Leave Employees", el_labels, el_values,
                       horizontal=True),
            "C41",
        )

    # ---------------- Column widths + freeze ----------------
    ws.column_dimensions["A"].width = 38
    ws.column_dimensions["B"].width = 18
    for letter in ("C", "D", "E", "F", "G", "H", "I",
                   "J", "K", "L", "M", "N", "O", "P", "Q"):
        ws.column_dimensions[letter].width = 11
    # Pin the title row and KPI header so they stay visible when scrolling.
    ws.freeze_panes = "A4"


def export_report(summary, daily):
    print("Exporting Excel report...")

    wb = Workbook()
    wb.active.title = "Dashboard"

    # Always-present data sheets, in this tab order.
    _build_executive_employee_sheet(
        wb.create_sheet("Employee Summary"),
        summary.get("executive_employee_summary"),
    )
    daily_ws = wb.create_sheet("Daily Attendance")
    _build_data_sheet(daily_ws, daily)
    _apply_daily_conditional_formatting(daily_ws, daily)
    _build_data_sheet(wb.create_sheet("Daily Trend"), summary.get("daily_trend"))

    # Optional sheets -- only added when the source data supplied them.
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

    employee_master = summary.get("employee_master")
    if employee_master is not None and not employee_master.empty:
        _build_data_sheet(wb.create_sheet("Employee Master"), employee_master)

    # Overtime sheet -- only rows where overtime actually happened.
    overtime_rows = daily[daily.get("overtime_status") == "Overtime"]
    if not overtime_rows.empty:
        _build_data_sheet(
            wb.create_sheet("Overtime"),
            overtime_rows[[
                "Employee ID", "First Name", "Date",
                "Check In", "Check Out", "Shift Start", "Shift End",
                "matched_shift_start", "matched_shift_end",
                "matched_shift_label", "shift_intervals",
                "worked_minutes", "scheduled_minutes",
                "matched_scheduled_minutes",
                "overtime_minutes", "overtime_status",
                "overtime_policy", "overtime_calculation_note",
                "overtime_multiplier",
                "overtime_payable_minutes", "overtime_payable_hours",
            ]],
        )

    # Early Leave sheet -- only rows where the employee genuinely left early.
    early_leave_rows = daily[daily.get("early_leave_status") == "Early Leave"]
    if not early_leave_rows.empty:
        _build_data_sheet(
            wb.create_sheet("Early Leave"),
            early_leave_rows[[
                "Employee ID", "First Name", "Date",
                "Check In", "Check Out", "Shift Start", "Shift End",
                "matched_shift_start", "matched_shift_end",
                "matched_shift_label", "shift_intervals",
                "matched_scheduled_minutes",
                "early_leave_minutes", "early_leave_status",
                "early_leave_anomaly", "early_leave_anomaly_reason",
            ]],
        )

    excluded_summary = summary.get("excluded_employees_summary")
    if excluded_summary is not None and not excluded_summary.empty:
        _build_data_sheet(
            wb.create_sheet("Excluded Employees"), excluded_summary
        )

    # Break analytics sheet -- informational, only when breaks exist.
    break_summary = summary.get("break_summary")
    if break_summary is not None and not break_summary.empty:
        _build_data_sheet(wb.create_sheet("Break Summary"), break_summary)

    # Absence audit ledger -- every (employee, date) and why it was
    # or wasn't counted as an absence.
    absence_details = summary.get("absence_details")
    if absence_details is not None and not absence_details.empty:
        _build_data_sheet(
            wb.create_sheet("Absence Details"), absence_details
        )

    # Per-employee audit totals (scheduled / attended / permission /
    # vacation / secondment / absence) with a reconciliation_delta so
    # HR can spot bookkeeping inconsistencies at a glance.
    absence_audit = summary.get("absence_audit")
    if absence_audit is not None and not absence_audit.empty:
        _build_data_sheet(
            wb.create_sheet("Absence Audit"), absence_audit
        )

    # Employee ID alias audit -- which historical IDs got remapped to
    # current IDs (only when at least one alias was configured active).
    alias_audit = summary.get("alias_audit")
    if alias_audit is not None and not alias_audit.empty:
        _build_data_sheet(
            wb.create_sheet("Employee ID Alias Audit"), alias_audit
        )

    # Schedule lookup audit -- one row per (Employee ID, attendance name)
    # describing how the Odoo schedule was matched (or why not). Always
    # emitted so HR can audit Missing Schedule rows even when zero rows
    # are missing.
    schedule_audit = summary.get("schedule_lookup_audit")
    if schedule_audit is not None and not schedule_audit.empty:
        _build_data_sheet(
            wb.create_sheet("Schedule Lookup Audit"), schedule_audit
        )

    # Manual punch corrections that did NOT clear the approval / evidence
    # gates -- exposed for Exceptions & Manual Review follow-up.
    rejected_corrections = summary.get("rejected_punch_corrections")
    if rejected_corrections is not None and not rejected_corrections.empty:
        _build_data_sheet(
            wb.create_sheet("Manual Punch Rejections"), rejected_corrections
        )

    # High-level reconciliation table lives on its own sheet so the
    # Dashboard stays uncluttered.
    reconciliation = summary.get("employee_reconciliation")
    if reconciliation is not None and not reconciliation.empty:
        _build_data_sheet(wb.create_sheet("Reconciliation"), reconciliation)

    # Build Dashboard LAST so it can reference positions on the other sheets.
    _build_dashboard(wb, summary)

    now = datetime.now()
    monthly_dir = REPORT_OUTPUT_DIR / now.strftime("%Y-%m")
    monthly_dir.mkdir(parents=True, exist_ok=True)
    filename = monthly_dir / f"hr_report_{now.strftime('%Y%m%d_%H%M%S')}.xlsx"
    wb.save(filename)

    print(f"Report saved: {filename}")
