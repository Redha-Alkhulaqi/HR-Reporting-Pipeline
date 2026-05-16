"""Write the Claude-facing monthly HR Markdown input.

Sections (in order):
  1. Summary KPIs
  2. Attendance Status Breakdown
  3. Excused vs Unexcused Analysis
  4. Top Late Employees
  5. Approved Excuse Records
  6. Employees Missing Working Schedule
  7. Late Attendance Records
  8. Business Logic Notes
  9. Instructions for Claude
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
6. Employee Attendance Risks
7. Manual Review Items (Missing Schedule)
8. HR Recommendations
9. Action Plan for Next Month

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
            # Skip DataFrames; they get their own sections below.
            if isinstance(value, pd.DataFrame):
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
            f, "Top Late Employees",
            metrics.get("employee_summary"), "No late employees found.",
            head=20,
        )

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
