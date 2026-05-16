"""Write the Claude-facing monthly HR Markdown input.

Sections (in order):
   1. Summary KPIs
   2. Attendance Status Breakdown
   3. Excused vs Unexcused Analysis
   4. Daily Trend
   5. Top Late Employees
   6. Department Summary           (only when department data is present)
   7. Approved Excuse Records
   8. Missing Punch Analysis
   9. Employees Missing Working Schedule
  10. Late Attendance Records
  11. Business Logic Notes
  12. Instructions for Claude
"""
import pandas as pd

from config import REPORT_OUTPUT_DIR


_BUSINESS_LOGIC_NOTES = """## Business Logic Notes

- Lateness is computed against each employee's shift start, parsed from
  the Odoo resource.resource Working Time label (first HH:MM AM/PM token).
- A 15-minute grace period applies (GRACE_MINUTES). A day counts as Late
  only when the UNEXCUSED portion of the delay exceeds 15 minutes.
- Approved EXCUSES (e.g. استأذان) are PARTIAL hourly permissions. They
  reduce the delay by the overlap between (Shift Start -> Check In) and
  (Excuse Start -> Excuse End). Only the residual unexcused minutes
  count toward Late. If the excuse fully covers the lateness, the day
  is classified as Approved Excuse instead of Late.
- Approved LEAVES (Annual / Sick / etc.) whose window covers the
  check-in time classify the day as Leave and bypass lateness entirely.
  Leave wins over Excuse when both apply on the same day.
- Employees absent from the Odoo resources export have no shift, so
  their lateness cannot be computed. They appear as Missing Schedule
  and require manual review.
- Missing Punch flags days that recorded a Check In but no Check Out.
  Per HR_REPORTING_RULES_MASTER rule 8, treat these as Missing Punch
  only after attendance is finalized.
- attendance_status values: Late, On Time, Approved Excuse, Leave,
  Missing Schedule. late_cases and total_late_minutes count only Late
  rows; excused minutes never flow into total_late_minutes.
"""

_INSTRUCTIONS = """## Instructions for Claude

Please generate a professional monthly HR report based on the data above.

The report should include:
1. Executive Summary
2. Key Attendance KPIs
3. Late Arrival Analysis
4. Approved Excuse vs Unexcused Late Analysis
5. Leave and Permission Patterns
6. Department Comparison (if department data is provided)
7. Daily Attendance Trend
8. Missing Punch and Missing Schedule Manual Review Items
9. Employee Attendance Risks
10. HR Recommendations
11. Action Plan for Next Month

Tone: professional, concise, suitable for HR management.
"""


def _write_section(f, title, df, empty_message, head=None):
    f.write(f"\n\n## {title}\n")
    if df is None or df.empty:
        f.write(empty_message + "\n")
        return
    table = df.head(head) if head else df
    f.write(table.to_markdown(index=False))


def generate_ai_input_file(metrics, attendance_daily):
    REPORT_OUTPUT_DIR.mkdir(exist_ok=True)
    file_path = REPORT_OUTPUT_DIR / "claude_hr_report_input.md"

    daily = attendance_daily
    late_rows = daily[daily["attendance_status"] == "Late"]
    excuse_rows = daily[daily["attendance_status"] == "Approved Excuse"]
    missing_schedule_rows = (
        daily[daily["attendance_status"] == "Missing Schedule"][
            ["Employee ID", "First Name"]
        ]
        .drop_duplicates()
        .sort_values("First Name")
    )

    with open(file_path, "w", encoding="utf-8") as f:
        f.write("# Monthly HR Attendance Report Input\n\n")

        f.write("## Summary KPIs\n")
        for key, value in metrics.items():
            # Skip DataFrames (rendered in their own sections) and Nones.
            if isinstance(value, pd.DataFrame) or value is None:
                continue
            f.write(f"- {key}: {value}\n")

        _write_section(
            f, "Attendance Status Breakdown",
            metrics.get("status_summary"), "No status data.",
        )
        _write_section(
            f, "Excused vs Unexcused Analysis",
            metrics.get("excused_vs_unexcused"), "No delay data.",
        )
        _write_section(
            f, "Daily Trend",
            metrics.get("daily_trend"), "No trend data.",
        )
        _write_section(
            f, "Top Late Employees",
            metrics.get("employee_summary"), "No late employees found.",
            head=20,
        )

        # Department Summary — only when source data exposed a department col.
        dept_summary = metrics.get("department_summary")
        f.write("\n\n## Department Summary\n")
        if dept_summary is None or dept_summary.empty:
            f.write("Department column not available in the source data.\n")
        else:
            f.write(dept_summary.to_markdown(index=False))

        f.write("\n\n## Approved Excuse Records\n")
        if excuse_rows.empty:
            f.write("No approved-excuse-covered days found.\n")
        else:
            f.write(
                "These days had a partial approved excuse (e.g. استأذان) that "
                "covered enough of the delay to keep the day under the late "
                "threshold.\n\n"
            )
            f.write(excuse_rows.to_markdown(index=False))

        # Missing Punch Analysis
        missing_punches = metrics.get("missing_punch_summary")
        f.write("\n\n## Missing Punch Analysis\n")
        if missing_punches is None or missing_punches.empty:
            f.write("No days with a missing Check Out punch.\n")
        else:
            f.write(
                f"{len(missing_punches)} day(s) recorded a Check In but no "
                "Check Out punch. Per HR_REPORTING_RULES_MASTER rule 8, "
                "treat as Missing Punch only after attendance is finalized.\n\n"
            )
            f.write(missing_punches.to_markdown(index=False))

        f.write("\n\n## Employees Missing Working Schedule\n")
        if missing_schedule_rows.empty:
            f.write("All employees have an assigned working schedule.\n")
        else:
            f.write(
                "These employees are not present in the Odoo resources file, "
                "so their lateness could not be computed. Manual review "
                "required.\n\n"
            )
            f.write(missing_schedule_rows.to_markdown(index=False))

        f.write("\n\n## Late Attendance Records\n")
        if late_rows.empty:
            f.write("No late attendance records found.\n")
        else:
            f.write(late_rows.to_markdown(index=False))

        f.write("\n\n" + _BUSINESS_LOGIC_NOTES)
        f.write("\n" + _INSTRUCTIONS)

    print(f"Claude input file saved: {file_path}")
