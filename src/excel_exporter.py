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

# Reporting Period header (rendered at the top of executive sheets).
_PERIOD_HEADER_FILL = PatternFill("solid", fgColor="DDEBF7")     # light blue
_PERIOD_TIMESTAMP_FILL = PatternFill("solid", fgColor="F2F2F2")  # light gray
_PERIOD_FONT = Font(bold=True, size=14, color="1F4E79")
_PERIOD_TIMESTAMP_FONT = Font(bold=True, size=11, color="555555")
_PERIOD_BLOCK_ROWS = 3  # period text + generated timestamp + blank spacer


def _format_period_date(value):
    """Render a date-like value as 'YYYY-MM-DD'. None / NaT -> empty."""
    if value is None:
        return ""
    try:
        ts = pd.to_datetime(value)
        if pd.isna(ts):
            return ""
        return ts.strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return str(value)


def _has_period(period_start, period_end):
    """True when at least one of the two bounds is non-empty."""
    return _format_period_date(period_start) != "" or \
        _format_period_date(period_end) != ""


def _write_reporting_period_block(ws, period_start, period_end, n_cols):
    """Render the 3-row Reporting Period header at the top of `ws`.

    Layout:
        Row 1: 'Reporting Period: YYYY-MM-DD to YYYY-MM-DD'
               (bold, size 14, centered, light-blue fill).
        Row 2: 'Generated On: YYYY-MM-DD HH:MM AM/PM'
               (bold, size 11, centered, light-gray fill).
        Row 3: blank spacer (a small placeholder value pins ws.max_row
               so subsequent ws.append() lands at row 4).

    Returns the next row index available for downstream content (4).
    Cells are merged across `n_cols` so the banner spans the report
    width regardless of how many data columns follow.
    """
    start_text = _format_period_date(period_start)
    end_text = _format_period_date(period_end)
    if start_text and end_text:
        period_text = f"Reporting Period: {start_text} to {end_text}"
    elif start_text:
        period_text = f"Reporting Period: from {start_text}"
    elif end_text:
        period_text = f"Reporting Period: through {end_text}"
    else:
        period_text = "Reporting Period: full attendance file"

    generated_text = (
        f"Generated On: {datetime.now().strftime('%Y-%m-%d %I:%M %p')}"
    )

    width = max(1, n_cols)

    # Row 1: Reporting Period.
    ws.cell(row=1, column=1, value=period_text)
    if width > 1:
        ws.merge_cells(start_row=1, start_column=1,
                       end_row=1, end_column=width)
    c1 = ws.cell(row=1, column=1)
    c1.font = _PERIOD_FONT
    c1.alignment = Alignment(horizontal="center", vertical="center")
    c1.fill = _PERIOD_HEADER_FILL
    ws.row_dimensions[1].height = 26

    # Row 2: Generated timestamp.
    ws.cell(row=2, column=1, value=generated_text)
    if width > 1:
        ws.merge_cells(start_row=2, start_column=1,
                       end_row=2, end_column=width)
    c2 = ws.cell(row=2, column=1)
    c2.font = _PERIOD_TIMESTAMP_FONT
    c2.alignment = Alignment(horizontal="center", vertical="center")
    c2.fill = _PERIOD_TIMESTAMP_FILL
    ws.row_dimensions[2].height = 18

    # Row 3: blank spacer. We must write SOMETHING (even a single space)
    # so openpyxl counts the row toward ws.max_row -- otherwise the
    # next ws.append() would overwrite this spacer row.
    ws.cell(row=3, column=1, value=" ")
    ws.row_dimensions[3].height = 6

    return _PERIOD_BLOCK_ROWS + 1

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


def _format_employee_id_columns_as_text(
    ws, header_row, data_start, data_end, n_cols,
):
    """Force any 'Employee ID' column to render as plain TEXT.

    Why: Excel infers a numeric type for large integer IDs (4195162)
    and applies a thousand-separator format (4,195,162) by default,
    which is wrong for an identifier column. Employee IDs are codes,
    not measurements -- they must display exactly as stored.

    Behavior:
    - Walks the header row and locates every column whose header
      contains 'Employee ID' (covers 'Employee ID', 'Raw Employee ID',
      'Canonical Employee ID', 'Old Employee ID', 'Current Employee ID').
    - For each data cell in those columns rewrites the value as a bare
      string, strips any thousand-separator commas and surrounding
      whitespace, and pins number_format='@' so Excel keeps the cell as
      TEXT on any subsequent edit.
    """
    if data_end < data_start:
        return
    for col in range(1, n_cols + 1):
        header = ws.cell(row=header_row, column=col).value
        if not isinstance(header, str) or "Employee ID" not in header:
            continue
        for r in range(data_start, data_end + 1):
            cell = ws.cell(row=r, column=col)
            v = cell.value
            if v is None or v == "":
                continue
            if isinstance(v, float) and v.is_integer():
                text = str(int(v))
            elif isinstance(v, (int, float)):
                text = str(v)
            else:
                text = str(v).replace(",", "").strip()
            cell.value = text
            cell.number_format = "@"


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
    _format_employee_id_columns_as_text(
        ws, header_row=start_row, data_start=data_start,
        data_end=data_end, n_cols=n_cols,
    )
    return start_row, data_start, data_end, next_row, n_cols


def _build_data_sheet(ws, df, period_start=None, period_end=None):
    """Populate a plain data sheet (header + rows + filter + freeze).

    When `period_start` / `period_end` are provided, a 3-row Reporting
    Period banner is rendered at the very top of the sheet and the
    table header / data / freeze pane / auto-filter all shift down
    accordingly.
    """
    show_period = _has_period(period_start, period_end)
    if df is None or df.empty:
        if show_period:
            _write_reporting_period_block(ws, period_start, period_end, n_cols=1)
            ws.cell(row=_PERIOD_BLOCK_ROWS + 1, column=1, value="(no data)")
        else:
            ws.append(["(no data)"])
        return

    n_cols = len(df.columns)
    if show_period:
        header_row = _write_reporting_period_block(
            ws, period_start, period_end, n_cols=n_cols,
        )
    else:
        header_row = 1

    for r_offset, row in enumerate(dataframe_to_rows(df, index=False, header=True)):
        for c_offset, value in enumerate(_sanitize_row(row)):
            ws.cell(row=header_row + r_offset, column=c_offset + 1, value=value)

    data_end = header_row + len(df)
    _style_header_row(ws, row=header_row, n_cols=n_cols)
    ws.freeze_panes = f"A{header_row + 1}"
    last_col_letter = get_column_letter(n_cols)
    ws.auto_filter.ref = f"A{header_row}:{last_col_letter}{data_end}"
    _autosize_columns(ws)
    _format_employee_id_columns_as_text(
        ws, header_row=header_row, data_start=header_row + 1,
        data_end=data_end, n_cols=n_cols,
    )


_PAYABLE_OT_HEADER_FILL = PatternFill("solid", fgColor="548235")   # dark green
_PAYABLE_OT_BODY_FILL = PatternFill("solid", fgColor="E2EFDA")      # light green
_PAYABLE_OT_BORDER_GREEN = "548235"
_NOTES_TITLE_FILL = PatternFill("solid", fgColor="DDEBF7")          # light blue


def _apply_payable_overtime_styling(
    ws, payable_col_idx, header_row=1, data_end_row=None,
):
    """Highlight the payable-overtime column to match the mockup.

    - Header cell: dark-green fill with the standard bold-white font.
    - Body cells: light-green fill, bold black font, centered.
    - Adds a thin dark-green border on left/right of every body cell
      and on the bottom of the header so the column visually pops
      out from its neighbors (like the green block in the mockup).

    `header_row` is the row of the table header (1 by default, 4 when
    the sheet carries a Reporting Period banner). `data_end_row`
    defaults to `ws.max_row` for backward compat.
    """
    from openpyxl.styles import Border, Side
    if data_end_row is None:
        data_end_row = ws.max_row
    if data_end_row < header_row:
        return
    header_cell = ws.cell(row=header_row, column=payable_col_idx)
    header_cell.fill = _PAYABLE_OT_HEADER_FILL
    # The shared _style_header_row already set white-bold font; keep it.

    side = Side(style="thin", color=_PAYABLE_OT_BORDER_GREEN)
    body_border = Border(left=side, right=side)
    header_border = Border(left=side, right=side, bottom=side)
    header_cell.border = header_border

    for r in range(header_row + 1, data_end_row + 1):
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


# ---------------------------------------------------------------------------
# Employee Attendance sheet
# ---------------------------------------------------------------------------
# Audit-focused daily summary, one row per (raw Employee ID, Date). Split-
# shift employees get two pairs of Check-In / Check-Out columns so HR can
# review morning vs evening attendance at a glance. Rows that arrived via
# alias mapping or a manual punch correction are visually highlighted.


_EMP_ATTENDANCE_COLS = [
    "Raw Employee ID", "Canonical Employee Name", "Canonical Employee ID",
    "Date", "Weekday",
    "Shift 1 Check-In", "Shift 1 Check-Out",
    "Shift 2 Check-In", "Shift 2 Check-Out",
    "Total Time", "Source / Notes",
]


def _hhmm(time_str):
    """'HH:MM:SS' -> 'HH:MM' for readable display. Empty/NA stays empty."""
    if time_str is None or (isinstance(time_str, float) and pd.isna(time_str)):
        return ""
    text = str(time_str).strip()
    if not text:
        return ""
    if len(text) >= 5 and text[2] == ":":
        return text[:5]
    return text


def _time_str_to_minutes(time_str):
    """'HH:MM:SS' -> int minutes from midnight. Returns None on bad input."""
    try:
        parts = str(time_str).split(":")
        return int(parts[0]) * 60 + int(parts[1])
    except (ValueError, AttributeError, IndexError):
        return None


def _minutes_between(start_str, end_str):
    """Minutes between two HH:MM[:SS] strings. Handles midnight wrap."""
    start = _time_str_to_minutes(start_str)
    end = _time_str_to_minutes(end_str)
    if start is None or end is None:
        return 0
    if end < start:
        end += 24 * 60
    return end - start


def _format_hours_minutes(total_minutes):
    """Render a minutes count as 'HH:MM'."""
    if total_minutes <= 0:
        return ""
    hours, minutes = divmod(int(total_minutes), 60)
    return f"{hours:02d}:{minutes:02d}"


def _shift_split_boundary(intervals):
    """Time-of-day (minutes from midnight) that splits a 2-interval
    schedule into Shift 1 / Shift 2 partitions. Returns None when the
    schedule has a single interval or an overlapping pair.
    """
    if not intervals or len(intervals) < 2:
        return None
    try:
        first_end_h, first_end_m = map(int, intervals[0][1].split(":"))
        second_start_h, second_start_m = map(int, intervals[1][0].split(":"))
    except (ValueError, IndexError, AttributeError):
        return None
    first_end_min = first_end_h * 60 + first_end_m
    second_start_min = second_start_h * 60 + second_start_m
    if second_start_min <= first_end_min:
        return None  # overlapping or back-to-back -- can't split
    return (first_end_min + second_start_min) // 2


def build_employee_attendance_rows(raw_punches, schedules_df):
    """Return the per-(raw Employee ID, Date) daily-summary rows.

    Public so tests can pin the shape without touching openpyxl. The
    rendering side of this is `_build_employee_attendance_sheet`.

    Behavior:
    - Only Check In + Check Out events count. Breaks have their own sheet.
    - Rows are grouped by `original_employee_id` so HR sees the RAW
      source ID (pre-alias); a parallel column carries the canonical
      Name + ID. If alias mapping is not present in `raw_punches`,
      the canonical ID is reused as the raw ID.
    - Split-shift partitioning uses the midpoint between the first
      interval's end and the second interval's start as the boundary.
      Single-interval employees put every punch into Shift 1; rows
      without a parseable schedule also collapse into Shift 1.
    - Shift 1/2 Check-In = earliest CI in that partition;
      Shift 1/2 Check-Out = latest CO in that partition. Sub-second
      duplicates collapse naturally via min/max.
    - Total Time = sum of paired-shift worked minutes ('HH:MM').
    - Source / Notes documents alias remapping and any manual
      correction that contributed to the row.
    """
    if raw_punches is None or raw_punches.empty:
        return pd.DataFrame(columns=_EMP_ATTENDANCE_COLS)

    df = raw_punches.copy()
    if "original_employee_id" not in df.columns:
        df["original_employee_id"] = df["Employee ID"]
    df = df[df["Punch State"].isin(["Check In", "Check Out"])].copy()
    if df.empty:
        return pd.DataFrame(columns=_EMP_ATTENDANCE_COLS)

    # Canonical name lookup keyed by canonical Employee ID.
    canon_name_by_id = {}
    for ceid, sub in df.groupby("Employee ID"):
        names = sub["First Name"].dropna().astype(str).str.strip()
        names = names[names != ""]
        canon_name_by_id[ceid] = names.iloc[0] if not names.empty else ""

    # Schedule intervals (per canonical name) -- reuse the ScheduleLookup
    # to honor EMP-code + NBSP-normalized matching.
    from metrics_calculator import (
        ScheduleLookup,
        _resolve_schedule_label_column,
    )
    label_col = _resolve_schedule_label_column(schedules_df) or "Working Time"
    schedule_lookup = ScheduleLookup(schedules_df, label_column=label_col)
    intervals_by_name = {
        n: schedule_lookup.match(n)["intervals"]
        for n in canon_name_by_id.values() if n
    }

    rows = []
    for (raw_eid, date), grp in df.groupby(
        ["original_employee_id", "Date"], sort=False
    ):
        canonical_eid = grp["Employee ID"].iloc[0]
        canonical_name = canon_name_by_id.get(canonical_eid, "")
        # Chronological CI/CO pairing: walk events in time order and
        # close each open interval when the first Check Out arrives.
        # This robustly handles split-shifts, event-day manual splits,
        # and sub-second duplicates without depending on a static
        # midpoint boundary (which broke when HR's manual split landed
        # before the configured boundary).
        events = sorted(
            (str(t), s) for t, s in zip(
                grp["Punch Time"].astype(str), grp["Punch State"]
            )
        )
        paired_intervals = []
        open_ci = None
        for time_str, state in events:
            if _time_str_to_minutes(time_str) is None:
                continue
            if state == "Check In":
                if open_ci is None:
                    open_ci = time_str
                # else: subsequent Check In while one is already open
                # -- keep the earlier one (sub-second device duplicates).
            elif state == "Check Out":
                if open_ci is not None:
                    paired_intervals.append((open_ci, time_str))
                    open_ci = None

        # Column-placement strategy:
        # - 2+ chronological pairs AND schedule has 2+ intervals
        #   -> chronological order (Shift 1 = first paired, Shift 2 =
        #   second). Supports event-day splits where the boundary
        #   doesn't fall at the schedule's static midpoint.
        # - Otherwise -> boundary-based slot placement. Each event is
        #   bucketed into Shift 1 / Shift 2 by its time-of-day. This
        #   guarantees that unpaired CIs/COs (e.g. when a raw-ID row
        #   contributes only Check-Ins and the canonical Check-Outs
        #   live in a separate raw-ID row) still appear in their
        #   natural slots instead of disappearing from the audit.
        intervals = intervals_by_name.get(canonical_name) or []
        boundary_min = _shift_split_boundary(intervals)
        s1_ci, s1_co, s2_ci, s2_co = "", "", "", ""
        if len(paired_intervals) >= 2 and len(intervals) >= 2:
            s1_ci, s1_co = paired_intervals[0]
            s2_ci, s2_co = paired_intervals[1]
        else:
            s1_cis, s1_cos, s2_cis, s2_cos = [], [], [], []
            for time_str, state in events:
                t_min = _time_str_to_minutes(time_str)
                if t_min is None:
                    continue
                if boundary_min is None or t_min < boundary_min:
                    ci_b, co_b = s1_cis, s1_cos
                else:
                    ci_b, co_b = s2_cis, s2_cos
                if state == "Check In":
                    ci_b.append(time_str)
                elif state == "Check Out":
                    co_b.append(time_str)
            s1_ci = min(s1_cis) if s1_cis else ""
            s1_co = max(s1_cos) if s1_cos else ""
            s2_ci = min(s2_cis) if s2_cis else ""
            s2_co = max(s2_cos) if s2_cos else ""

        total_min = 0
        for ci_str, co_str in ((s1_ci, s1_co), (s2_ci, s2_co)):
            if ci_str and co_str:
                total_min += _minutes_between(ci_str, co_str)

        notes_parts = []
        is_event_split = (
            "correction_action" in grp.columns
            and (grp["correction_action"] == "event_day_split").any()
        )
        if is_event_split:
            notes_parts.append("Event-day split (manual)")
        elif "is_manual_correction" in grp.columns and grp["is_manual_correction"].any():
            notes_parts.append("Manual correction")
        if raw_eid != canonical_eid:
            notes_parts.append(f"Aliased from {raw_eid} -> {canonical_eid}")
        source_label = "; ".join(notes_parts) if notes_parts else "BioTime"

        try:
            weekday = pd.to_datetime(date).strftime("%a")
        except (ValueError, TypeError):
            weekday = ""

        rows.append({
            "Raw Employee ID": raw_eid,
            "Canonical Employee Name": canonical_name,
            "Canonical Employee ID": canonical_eid,
            "Date": str(date),
            "Weekday": weekday,
            "Shift 1 Check-In": _hhmm(s1_ci),
            "Shift 1 Check-Out": _hhmm(s1_co),
            "Shift 2 Check-In": _hhmm(s2_ci),
            "Shift 2 Check-Out": _hhmm(s2_co),
            "Total Time": _format_hours_minutes(total_min),
            "Source / Notes": source_label,
        })

    result = pd.DataFrame(rows, columns=_EMP_ATTENDANCE_COLS)
    if not result.empty:
        result = result.sort_values(
            by=["Canonical Employee Name", "Date", "Raw Employee ID"]
        ).reset_index(drop=True)
    return result


_ATTENDANCE_ALIAS_FILL = PatternFill("solid", fgColor="FFF2CC")    # light gold
_ATTENDANCE_MANUAL_FILL = PatternFill("solid", fgColor="DDEBF7")   # light blue


def _build_employee_attendance_sheet(ws, df, period_start=None, period_end=None):
    """Render the Employee Attendance audit sheet.

    When `period_start` / `period_end` are provided, a 3-row Reporting
    Period banner is rendered above the table.
    """
    show_period = _has_period(period_start, period_end)
    if df is None or df.empty:
        if show_period:
            _write_reporting_period_block(ws, period_start, period_end, n_cols=1)
            ws.cell(row=_PERIOD_BLOCK_ROWS + 1, column=1,
                    value="(no attendance data available)")
        else:
            ws.append(["(no attendance data available)"])
        return

    n_cols = len(df.columns)
    if show_period:
        header_row = _write_reporting_period_block(
            ws, period_start, period_end, n_cols=n_cols,
        )
    else:
        header_row = 1
    for r_offset, row in enumerate(dataframe_to_rows(df, index=False, header=True)):
        for c_offset, value in enumerate(_sanitize_row(row)):
            ws.cell(row=header_row + r_offset, column=c_offset + 1, value=value)
    data_end = header_row + len(df)
    _style_header_row(ws, row=header_row, n_cols=n_cols)
    # Freeze the employee/date identification columns + header row so
    # HR can scroll across many days without losing the row anchor.
    ws.freeze_panes = f"F{header_row + 1}"
    last_col_letter = get_column_letter(n_cols)
    ws.auto_filter.ref = f"A{header_row}:{last_col_letter}{data_end}"
    _autosize_columns(ws, min_width=10, max_width=45)
    _format_employee_id_columns_as_text(
        ws, header_row=header_row, data_start=header_row + 1,
        data_end=data_end, n_cols=n_cols,
    )

    # Visual cue: tint rows that came in via alias mapping (gold) or
    # via a manual punch correction (blue). HR's eye lands on these
    # for audit much faster than reading the Source / Notes column.
    source_col = _EMP_ATTENDANCE_COLS.index("Source / Notes") + 1
    for r in range(header_row + 1, data_end + 1):
        value = ws.cell(row=r, column=source_col).value
        if not isinstance(value, str):
            continue
        fill = None
        if "Aliased" in value:
            fill = _ATTENDANCE_ALIAS_FILL
        elif "Manual" in value:
            fill = _ATTENDANCE_MANUAL_FILL
        if fill is not None:
            for c in range(1, n_cols + 1):
                ws.cell(row=r, column=c).fill = fill


def _build_executive_employee_sheet(ws, df, period_start=None, period_end=None):
    """Render the simplified executive Employee Summary sheet.

    14 columns expected (in this exact order):
        1.  Employee ID
        2.  First Name
        3.  No of Absence Days       (reduced by Friday compensation)
        4.  No of Permission Days
        5.  No of Vacation Days
        6.  No of Secondment Days
        7.  Total Late (Hours)
        8.  Total Over Time (Hours) (Actual)
        9.  Total Over Time (Payable 1.5x) (Hours)
        10. Total Early Leave (Hours)
        11. Break Time (Hours)
        12. Break Time (After Policy)
        13. Friday Compensation Days  (Gaming Friday Compensation)
        14. Friday Worked Dates       (audit list of paired Fridays)

    Apply executive-friendly formatting: bold header, frozen header,
    auto-filter, centered numeric cells, thousands separator on
    integer columns, 1-decimal display on all hour columns. Column 9
    (Payable Overtime) is visually highlighted in green to mirror the
    mockup, and a Notes block is appended below the data describing
    the formula, rounding, and audit semantics.

    When `period_start` / `period_end` are provided, a 3-row Reporting
    Period banner is rendered above the table. All numeric formatters,
    the payable-OT highlight, and the Employee ID TEXT pinning shift
    by the banner's height accordingly.
    """
    show_period = _has_period(period_start, period_end)
    if df is None or df.empty:
        if show_period:
            _write_reporting_period_block(ws, period_start, period_end, n_cols=1)
            ws.cell(row=_PERIOD_BLOCK_ROWS + 1, column=1, value="(no data)")
        else:
            ws.append(["(no data)"])
        return

    n_cols = len(df.columns)
    if show_period:
        header_row = _write_reporting_period_block(
            ws, period_start, period_end, n_cols=n_cols,
        )
    else:
        header_row = 1
    for r_offset, row in enumerate(dataframe_to_rows(df, index=False, header=True)):
        for c_offset, value in enumerate(_sanitize_row(row)):
            ws.cell(row=header_row + r_offset, column=c_offset + 1, value=value)
    data_end_row = header_row + len(df)
    _style_header_row(ws, row=header_row, n_cols=n_cols)
    ws.freeze_panes = f"A{header_row + 1}"
    ws.auto_filter.ref = (
        f"A{header_row}:{get_column_letter(n_cols)}{data_end_row}"
    )
    _autosize_columns(ws)

    # Integer columns: the three whole-day-count columns (4 Permission,
    # 5 Vacation, 6 Secondment) and the Gaming Friday Compensation Days
    # column (13). Employee ID (col 1) is intentionally NOT included
    # because it is an identifier, not a measurement -- it gets its
    # own TEXT (@) format below to suppress thousand separators.
    # Absence (col 3) is rendered at 1 decimal because split-shift
    # partial-attendance days contribute fractional (e.g. 0.5) values.
    _format_numeric_cells(
        ws,
        rows=range(header_row + 1, data_end_row + 1),
        cols=[4, 5, 6, 13],
        number_format="#,##0",
    )
    # 1-decimal columns: 3 Absence (fractional for split-shift),
    # 7 Late, 8 OT Actual, 9 OT Payable, 10 Early Leave, 11 Break,
    # 12 Break After Policy.
    _format_numeric_cells(
        ws,
        rows=range(header_row + 1, data_end_row + 1),
        cols=[3, 7, 8, 9, 10, 11, 12],
        number_format="0.0",
    )
    # Visually highlight the payable-overtime column.
    _apply_payable_overtime_styling(
        ws, payable_col_idx=9,
        header_row=header_row, data_end_row=data_end_row,
    )
    # Pin Employee ID column(s) to TEXT (@) so Excel never inserts
    # thousand separators into the identifier (e.g. 4195162, not
    # 4,195,162). Run AFTER the numeric formatters above so this is
    # the last write to those cells.
    _format_employee_id_columns_as_text(
        ws, header_row=header_row, data_start=header_row + 1,
        data_end=data_end_row, n_cols=n_cols,
    )
    # Append the Notes block at the bottom.
    _append_executive_notes_block(ws, n_cols=n_cols)


def _apply_daily_conditional_formatting(ws, df, data_start_row=2):
    """Color-code each row in Daily Attendance by its dominant status.

    Rules are applied in priority order with stopIfTrue=True so each
    row gets exactly one fill, picking the most important condition.
    Priority (highest first): Excluded, Leave, Approved Excuse, Late,
    Early Leave, Missing Check Out, Overtime.

    `data_start_row` is the spreadsheet row of the FIRST data row
    (defaults to 2). It moves down to 5 when the sheet carries a
    Reporting Period banner.
    """
    if df is None or df.empty:
        return

    cols = list(df.columns)
    n_rows = len(df)
    n_cols = len(cols)
    last_col_letter = get_column_letter(n_cols)
    data_end_row = data_start_row + n_rows - 1
    range_str = (
        f"A{data_start_row}:{last_col_letter}{data_end_row}"
    )
    # Conditional-formatting formulas reference the first data row
    # (Excel auto-shifts the row number for subsequent rows in the
    # range). Keep this in lockstep with `data_start_row` so the
    # rules still hit the same cells after the period banner pushed
    # everything down.
    R = data_start_row

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
        _add(f"${excluded_L}{R}=TRUE", "C8A2C8")              # light violet
    if status_L:
        _add(f'${status_L}{R}="Leave"', "BFBFBF")             # gray
        _add(f'${status_L}{R}="Approved Excuse"', "DDEBF7")   # light blue
    # Early-leave ANOMALIES outrank normal Late/Early Leave so HR can
    # spot data-quality issues at a glance (distinct dark red).
    if early_leave_anomaly_L:
        _add(
            f"${early_leave_anomaly_L}{R}=TRUE",
            "8B0000", font_hex="FFFFFF", bold=True,           # dark red + white bold
        )
    if is_late_L and unexcused_L:
        _add(
            f"OR(${is_late_L}{R}=TRUE,${unexcused_L}{R}>0)",
            "C00000", font_hex="FFFFFF", bold=True,           # red + white bold
        )
    if early_leave_L:
        _add(f"${early_leave_L}{R}>0", "CC6600",
             font_hex="FFFFFF")                                # dark orange
    if missing_co_L:
        _add(f"${missing_co_L}{R}=TRUE", "FFFF00", bold=True)  # yellow
    if overtime_L:
        _add(f"${overtime_L}{R}>0", "C6EFCE",
             font_hex="006100")                              # green


def _pie_chart(title, labels, values):
    chart = PieChart()
    chart.title = title
    chart.add_data(values, titles_from_data=False)
    chart.set_categories(labels)
    chart.width = _CHART_WIDTH
    chart.height = _CHART_HEIGHT
    # Show 'Category, percent' (e.g. 'On Time, 78%') and explicitly
    # hide the series name so labels don't read 'Series1, ...'. The
    # left-side legend stays redundant-but-helpful for small slices.
    chart.dataLabels = DataLabelList(
        showCatName=True,
        showPercent=True,
        showSerName=False,
        showVal=False,
    )
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
    # A single-series line should render as one consistent colour;
    # Excel otherwise auto-cycles segments through theme colours which
    # makes the trend look like a rainbow.
    chart.varyColors = False
    return chart


def _format_numeric_cells(ws, rows, cols, number_format):
    """Apply thousands separator + centered alignment to a numeric block."""
    align = Alignment(horizontal="center", vertical="center")
    for row in rows:
        for col in cols:
            cell = ws.cell(row=row, column=col)
            cell.number_format = number_format
            cell.alignment = align


def _build_dashboard(wb, summary, period_start=None, period_end=None):
    ws = wb["Dashboard"]
    # Clean executive look: no gridlines.
    ws.sheet_view.showGridLines = False

    show_period = _has_period(period_start, period_end)
    # Vertical-shift the entire Dashboard layout when the period banner
    # is rendered. The banner takes rows 2-4 (period + generated +
    # spacer), pushing the KPI header from row 3 to row 5 and every
    # chart anchor down by 2 rows.
    row_shift = 2 if show_period else 0

    # Title bar.
    ws.merge_cells("A1:O1")
    title = ws["A1"]
    title.value = "HR Reporting Dashboard"
    title.font = _TITLE_FONT
    title.alignment = Alignment(horizontal="left", vertical="center")
    ws.row_dimensions[1].height = 28

    if show_period:
        # Row 2: Reporting Period (matches the banner used on other sheets).
        start_text = _format_period_date(period_start)
        end_text = _format_period_date(period_end)
        if start_text and end_text:
            period_text = f"Reporting Period: {start_text} to {end_text}"
        elif start_text:
            period_text = f"Reporting Period: from {start_text}"
        elif end_text:
            period_text = f"Reporting Period: through {end_text}"
        else:
            period_text = "Reporting Period: full attendance file"
        ws.merge_cells("A2:O2")
        period_cell = ws["A2"]
        period_cell.value = period_text
        period_cell.font = _PERIOD_FONT
        period_cell.alignment = Alignment(horizontal="center", vertical="center")
        period_cell.fill = _PERIOD_HEADER_FILL
        ws.row_dimensions[2].height = 26

        # Row 3: Generated timestamp.
        ws.merge_cells("A3:O3")
        gen_cell = ws["A3"]
        gen_cell.value = (
            f"Generated On: {datetime.now().strftime('%Y-%m-%d %I:%M %p')}"
        )
        gen_cell.font = _PERIOD_TIMESTAMP_FONT
        gen_cell.alignment = Alignment(horizontal="center", vertical="center")
        gen_cell.fill = _PERIOD_TIMESTAMP_FILL
        ws.row_dimensions[3].height = 18

        # Row 4: blank spacer.
        ws.row_dimensions[4].height = 6
    else:
        # Subtitle with the report timestamp -- helps HR confirm freshness
        # when the workbook is forwarded or archived.
        ws.merge_cells("A2:O2")
        subtitle = ws["A2"]
        subtitle.value = (
            f"Generated {datetime.now().strftime('%Y-%m-%d %H:%M')}"
        )
        subtitle.font = Font(italic=True, color="555555", size=10)
        subtitle.alignment = Alignment(horizontal="left", vertical="center")

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
    kpi_header_row = 3 + row_shift  # row 3 by default, row 5 with banner
    kpi_data_start = kpi_header_row + 1
    ws.cell(row=kpi_header_row, column=1, value="Metric")
    ws.cell(row=kpi_header_row, column=2, value="Value")
    _style_header_row(ws, row=kpi_header_row, n_cols=2)
    centered = Alignment(horizontal="center", vertical="center")
    # Zebra striping for the KPI block + green accent for the payable
    # overtime KPI so it visually matches the Employee Summary
    # highlight. Yellow-tinted accent for the data-quality score.
    band = PatternFill("solid", fgColor="F2F2F2")
    payable_accent = PatternFill("solid", fgColor="E2EFDA")
    dq_accent = PatternFill("solid", fgColor="FFF2CC")
    accent_labels = {"Total Over Time (Payable 1.5x) (Hours)": payable_accent,
                     "Data Quality Score": dq_accent}
    for i, (label, value, fmt) in enumerate(kpis, start=kpi_data_start):
        label_cell = ws.cell(row=i, column=1, value=label)
        cell = ws.cell(row=i, column=2, value=value)
        cell.number_format = fmt
        cell.alignment = centered
        accent = accent_labels.get(label)
        if accent is not None:
            label_cell.fill = accent
            cell.fill = accent
            label_cell.font = Font(bold=True)
            cell.font = Font(bold=True)
        elif (i - kpi_data_start) % 2 == 1:  # alternate-row band
            label_cell.fill = band
            cell.fill = band

    # ---------------- Backing tables (placed below the charts) ----------------
    # Each chart is 13cm x 7cm (~18 rows tall). Three chart rows anchored
    # at the chart anchor rows below cover roughly 56 rows, so data tables
    # start safely 2 rows further down.
    DATA_START = 60 + row_shift
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
        # total_overtime_minutes col 3 uses integer thousands format;
        # total_overtime_hours col 4 uses one decimal. Employee ID
        # (col 1) is intentionally excluded -- _write_dataframe already
        # pinned it to TEXT (@) so Excel does not render commas in
        # the identifier (4195162, not 4,195,162).
        _format_numeric_cells(
            ws,
            rows=range(ot_data_start, ot_data_end + 1),
            cols=[3],
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
        # Employee ID (col 1) intentionally excluded -- already pinned
        # to TEXT (@) by _write_dataframe to suppress comma separators.
        _format_numeric_cells(
            ws,
            rows=range(el_data_start, el_data_end + 1),
            cols=[3],
            number_format="#,##0",
        )
        _format_numeric_cells(
            ws,
            rows=range(el_data_start, el_data_end + 1),
            cols=[4],
            number_format="0.0",
        )

    # ---------------- Charts: 2x2+1 grid (anchors leave visual buffer) ----------------
    # Layout: anchors live at C5 / K5 / C24 / K24 / C43 by default; when
    # the Reporting Period banner is rendered (`row_shift = 2`) the
    # entire grid drops to C7 / K7 / C26 / K26 / C45 so the pie title
    # does NOT collide with the KPI header. Each chart is 13cm x 7cm.
    chart_anchor = lambda row: f"{{col}}{row + row_shift}"  # not used; explicit below
    anchor_tl = f"C{5 + row_shift}"   # top-left  (Attendance Status pie)
    anchor_tr = f"K{5 + row_shift}"   # top-right (Daily Late Trend)
    anchor_ml = f"C{24 + row_shift}"  # middle-left (Top Late)
    anchor_mr = f"K{24 + row_shift}"  # middle-right (Top Overtime)
    anchor_bl = f"C{43 + row_shift}"  # bottom-left (Top Early Leave)

    # Cross-sheet header offsets: the Employee Summary / Daily Trend
    # sheets carry their OWN period banner when the dashboard does, so
    # their data rows live at row 5+ instead of row 2+. Compute the
    # first-data-row for each sheet from its actual header.
    other_sheet_first_data_row = _PERIOD_BLOCK_ROWS + 2 if show_period else 2

    # Chart 1 (top-left): Attendance Status pie.
    pie_labels = Reference(ws, min_col=1,
                           min_row=status_data_start, max_row=status_data_end)
    pie_values = Reference(ws, min_col=2,
                           min_row=status_data_start, max_row=status_data_end)
    ws.add_chart(
        _pie_chart("Attendance Status Breakdown", pie_labels, pie_values),
        anchor_tl,
    )

    # Chart 2 (top-right): Daily Late Trend (references Daily Trend sheet).
    if "Daily Trend" in wb.sheetnames:
        trend_ws = wb["Daily Trend"]
        trend_first_data = other_sheet_first_data_row
        trend_header_row = trend_first_data - 1
        if trend_ws.max_row >= trend_first_data:
            # Daily Trend cols: 1=Date, 2=total_records, 3=late_cases, ...
            trend_labels = Reference(
                trend_ws, min_col=1,
                min_row=trend_first_data, max_row=trend_ws.max_row,
            )
            # titles_from_data=True -> include the header row for series name.
            trend_values = Reference(
                trend_ws, min_col=3,
                min_row=trend_header_row, max_row=trend_ws.max_row,
            )
            ws.add_chart(
                _line_chart("Daily Late Trend", trend_labels, trend_values),
                anchor_tr,
            )

    # Chart 3 (middle-left): Top Late Employees -- references the
    # simplified Employee Summary sheet which is sorted by
    # Total Late (Hours) desc.
    emp_ws = wb["Employee Summary"]
    emp_first_data = other_sheet_first_data_row
    if emp_ws.max_row >= emp_first_data:
        n_late = min(10, emp_ws.max_row - emp_first_data + 1)
        # Executive sheet cols: 1=Employee ID, 2=First Name,
        # 3=Absence, ..., 7=Total Late (Hours), ...
        late_labels = Reference(
            emp_ws, min_col=2,
            min_row=emp_first_data, max_row=emp_first_data + n_late - 1,
        )
        late_values = Reference(
            emp_ws, min_col=7,
            min_row=emp_first_data, max_row=emp_first_data + n_late - 1,
        )
        ws.add_chart(
            _bar_chart("Top Late Employees", late_labels, late_values,
                       horizontal=True),
            anchor_ml,
        )

    # Chart 4 (middle-right): Top Overtime Employees.
    if ot_data_end >= ot_data_start and not simplified_overtime.empty:
        ot_labels = Reference(ws, min_col=2,
                              min_row=ot_data_start, max_row=ot_data_end)
        ot_values = Reference(ws, min_col=3,
                              min_row=ot_data_start, max_row=ot_data_end)
        ws.add_chart(
            _bar_chart("Top Overtime Employees", ot_labels, ot_values,
                       horizontal=True),
            anchor_mr,
        )

    # Chart 5 (bottom-left): Top Early Leave Employees.
    if el_data_end >= el_data_start and not simplified_el.empty:
        el_labels = Reference(ws, min_col=2,
                              min_row=el_data_start, max_row=el_data_end)
        el_values = Reference(ws, min_col=3,
                              min_row=el_data_start, max_row=el_data_end)
        ws.add_chart(
            _bar_chart("Top Early Leave Employees", el_labels, el_values,
                       horizontal=True),
            anchor_bl,
        )

    # ---------------- Column widths + freeze ----------------
    ws.column_dimensions["A"].width = 38
    ws.column_dimensions["B"].width = 18
    for letter in ("C", "D", "E", "F", "G", "H", "I",
                   "J", "K", "L", "M", "N", "O", "P", "Q"):
        ws.column_dimensions[letter].width = 11
    # Pin the title row and KPI header so they stay visible when scrolling.
    # Default: freeze below KPI header at A4. With banner: A6.
    ws.freeze_panes = f"A{kpi_data_start}"


def export_report(
    summary, daily, raw_punches=None, schedules_df=None,
    period_start=None, period_end=None,
):
    """Write the monthly Excel workbook.

    `raw_punches` is the post-alias, post-manual-correction punch
    events dataframe (one row per Check In / Check Out / Break event).
    When provided alongside `schedules_df`, the Employee Attendance
    audit sheet is rendered with paired-shift columns. Both arguments
    are optional for backward compat -- old callers that pass only
    `summary` + `daily` keep working and simply skip the new sheet.

    `period_start` / `period_end` drive the Reporting Period banner at
    the top of every executive sheet (Dashboard, Employee Summary,
    Employee Attendance, Daily Attendance, Daily Trend, Department
    Summary, and every audit sheet). When both are None the banner is
    suppressed and sheets keep the legacy row-1 header.
    """
    print("Exporting Excel report...")

    wb = Workbook()
    wb.active.title = "Dashboard"

    # Pre-resolve the period-banner kwargs once so every sheet builder
    # call below stays uniform and the banner can be turned off by a
    # single `None` pair in tests / legacy callers.
    pkw = {"period_start": period_start, "period_end": period_end}
    daily_data_start = (
        _PERIOD_BLOCK_ROWS + 2 if _has_period(period_start, period_end) else 2
    )

    # Always-present data sheets, in this tab order.
    _build_executive_employee_sheet(
        wb.create_sheet("Employee Summary"),
        summary.get("executive_employee_summary"),
        **pkw,
    )
    # Employee Attendance -- audit-focused daily summary with split-
    # shift columns. Placed right after Employee Summary so HR can
    # cross-reference the executive totals with the per-day source.
    if raw_punches is not None and schedules_df is not None:
        attendance_rows = build_employee_attendance_rows(
            raw_punches, schedules_df,
        )
        _build_employee_attendance_sheet(
            wb.create_sheet("Employee Attendance"),
            attendance_rows,
            **pkw,
        )
    daily_ws = wb.create_sheet("Daily Attendance")
    _build_data_sheet(daily_ws, daily, **pkw)
    _apply_daily_conditional_formatting(
        daily_ws, daily, data_start_row=daily_data_start,
    )
    _build_data_sheet(
        wb.create_sheet("Daily Trend"), summary.get("daily_trend"), **pkw,
    )

    # Optional sheets -- only added when the source data supplied them.
    missing_punches = summary.get("missing_punch_summary")
    if missing_punches is not None and not missing_punches.empty:
        _build_data_sheet(
            wb.create_sheet("Missing Punches"), missing_punches, **pkw,
        )

    department_summary = summary.get("department_summary")
    if department_summary is not None and not department_summary.empty:
        _build_data_sheet(
            wb.create_sheet("Department Summary"), department_summary, **pkw,
        )

    reconciliation_details = summary.get("employee_reconciliation_details")
    if reconciliation_details is not None and not reconciliation_details.empty:
        _build_data_sheet(
            wb.create_sheet("Employee Reconciliation Details"),
            reconciliation_details, **pkw,
        )

    employee_master = summary.get("employee_master")
    if employee_master is not None and not employee_master.empty:
        _build_data_sheet(
            wb.create_sheet("Employee Master"), employee_master, **pkw,
        )

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
            **pkw,
        )

    # Early Leave sheet -- only rows where the employee genuinely left early.
    early_leave_rows = daily[daily.get("early_leave_status") == "Early Leave"]
    if not early_leave_rows.empty:
        el_cols = [
            "Employee ID", "First Name", "Date",
            "Check In", "Check Out", "Shift Start", "Shift End",
            "matched_shift_start", "matched_shift_end",
            "matched_shift_label", "shift_intervals",
            "matched_scheduled_minutes",
            "early_leave_minutes", "early_leave_status",
            "early_leave_anomaly", "early_leave_anomaly_reason",
        ]
        # Excused / unexcused early-leave columns are appended when
        # _attach_excused_early_leave_info ran (which it does in the
        # standard pipeline). HR uses these to see which portion of
        # the gap was covered by an approved permission.
        for extra in ("excused_early_leave_minutes",
                      "unexcused_early_leave_minutes"):
            if extra in early_leave_rows.columns:
                el_cols.append(extra)
        _build_data_sheet(
            wb.create_sheet("Early Leave"),
            early_leave_rows[el_cols],
            **pkw,
        )

    excluded_summary = summary.get("excluded_employees_summary")
    if excluded_summary is not None and not excluded_summary.empty:
        _build_data_sheet(
            wb.create_sheet("Excluded Employees"), excluded_summary, **pkw,
        )

    # Break analytics sheet -- informational, only when breaks exist.
    break_summary = summary.get("break_summary")
    if break_summary is not None and not break_summary.empty:
        _build_data_sheet(
            wb.create_sheet("Break Summary"), break_summary, **pkw,
        )

    # Absence audit ledger -- every (employee, date) and why it was
    # or wasn't counted as an absence.
    absence_details = summary.get("absence_details")
    if absence_details is not None and not absence_details.empty:
        _build_data_sheet(
            wb.create_sheet("Absence Details"), absence_details, **pkw,
        )

    # Per-employee audit totals (scheduled / attended / permission /
    # vacation / secondment / absence) with a reconciliation_delta so
    # HR can spot bookkeeping inconsistencies at a glance.
    absence_audit = summary.get("absence_audit")
    if absence_audit is not None and not absence_audit.empty:
        _build_data_sheet(
            wb.create_sheet("Absence Audit"), absence_audit, **pkw,
        )

    # Employee ID alias audit -- which historical IDs got remapped to
    # current IDs (only when at least one alias was configured active).
    alias_audit = summary.get("alias_audit")
    if alias_audit is not None and not alias_audit.empty:
        _build_data_sheet(
            wb.create_sheet("Employee ID Alias Audit"), alias_audit, **pkw,
        )

    # Schedule lookup audit -- one row per (Employee ID, attendance name)
    # describing how the Odoo schedule was matched (or why not). Always
    # emitted so HR can audit Missing Schedule rows even when zero rows
    # are missing.
    schedule_audit = summary.get("schedule_lookup_audit")
    if schedule_audit is not None and not schedule_audit.empty:
        _build_data_sheet(
            wb.create_sheet("Schedule Lookup Audit"), schedule_audit, **pkw,
        )

    # Manual punch corrections that did NOT clear the approval / evidence
    # gates -- exposed for Exceptions & Manual Review follow-up.
    rejected_corrections = summary.get("rejected_punch_corrections")
    if rejected_corrections is not None and not rejected_corrections.empty:
        _build_data_sheet(
            wb.create_sheet("Manual Punch Rejections"),
            rejected_corrections, **pkw,
        )

    # High-level reconciliation table lives on its own sheet so the
    # Dashboard stays uncluttered.
    reconciliation = summary.get("employee_reconciliation")
    if reconciliation is not None and not reconciliation.empty:
        _build_data_sheet(
            wb.create_sheet("Reconciliation"), reconciliation, **pkw,
        )

    # Build Dashboard LAST so it can reference positions on the other sheets.
    _build_dashboard(wb, summary, **pkw)

    now = datetime.now()
    monthly_dir = REPORT_OUTPUT_DIR / now.strftime("%Y-%m")
    monthly_dir.mkdir(parents=True, exist_ok=True)
    filename = monthly_dir / f"hr_report_{now.strftime('%Y%m%d_%H%M%S')}.xlsx"
    wb.save(filename)

    print(f"Report saved: {filename}")
