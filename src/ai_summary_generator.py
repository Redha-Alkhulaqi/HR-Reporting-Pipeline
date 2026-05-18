"""Write the Claude-facing monthly HR Markdown input.

Sections (in order):
   1. Executive Summary           (highlights, concerns, risks,
                                   recommendations, action plan)
   2. Summary KPIs
   3. Employee Count Reconciliation  (auditable taxonomy; why our number
                                      can differ from Odoo / BioTime)
   4. Data Quality                 (data_quality_score + audit signals)
   5. Attendance Status Breakdown
   6. Excused vs Unexcused Analysis
   7. Daily Trend
   8. Top Late Employees          (full Employee Summary, includes
                                   risk_score / risk_reason / deductions)
   9. HR Audit Flags              (employees flagged for chronic late,
                                   missing checkouts, excessive excuses,
                                   no schedule, anomalies)
  10. Employee ID Alias Mapping    (old BioTime IDs remapped to current
                                    Odoo IDs; only shown when at least
                                    one active alias exists)
  11. Break Analysis              (INFORMATIONAL only -- does NOT affect
                                   any KPI above or below)
  12. Excluded Employees          (policy exclusions from KPIs while
                                   operational data stays visible)
  13. Early Leave Analysis        (counts, minutes, top early-leave employees)
  14. Early Leave Anomalies       (rows above the implausibility threshold)
  15. Overtime Analysis           (counts, hours, top overtime employees)
  16. Department Summary          (only when department data is present)
  17. Approved Excuse Records
  18. Missing Punch Analysis
  19. Employees Missing Working Schedule
  20. Late Attendance Records
  21. Business Logic Notes
  22. Instructions for Claude

Files are written to REPORT_OUTPUT_DIR/YYYY-MM/claude_hr_report_input.md
so each month is naturally archived.
"""
from datetime import datetime

import pandas as pd

from config import MAX_MONTHLY_DEDUCTION, REPORT_OUTPUT_DIR


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
- Risk scoring is COMPOUND: late_count, total unexcused minutes,
  missing check-outs, and repeated excuses each contribute capped
  points. risk_level bands the resulting risk_score.
- estimated_deduction = total_late_minutes * LATE_MINUTE_COST.
  deduction_capped = min(estimated_deduction, MAX_MONTHLY_DEDUCTION).
- attendance_status values: Late, On Time, Approved Excuse, Leave,
  Missing Schedule. late_cases and total_late_minutes count only Late
  rows; excused minutes never flow into total_late_minutes.
"""

_INSTRUCTIONS = """## Instructions for Claude

Use the Executive Summary above as the lead, then expand each section
with detail drawn from the tables below. The final report should
include:

1. Executive Summary
2. Key Attendance KPIs
3. Late Arrival Analysis (with department breakdown if available)
4. Approved Excuse vs Unexcused Late Analysis
5. Leave and Permission Patterns
6. Daily Attendance Trend
7. Missing Punch and Missing Schedule Manual Review Items
8. Employee Attendance Risks (cite risk_score and risk_reason)
9. Payroll Impact (use estimated_deduction / deduction_capped totals)
10. HR Recommendations
11. Action Plan for Next Month

Tone: professional, concise, suitable for HR management.
"""


def _build_executive_summary(metrics):
    total_emp = metrics["total_employees"]
    late = metrics["late_cases"]
    late_min = metrics["total_late_minutes"]
    excused = metrics["excused_delay_minutes"]
    missing_co = metrics["missing_check_out_cases"]
    missing_sch = metrics["missing_schedule_cases"]
    leave = metrics["leave_cases"]
    excuse_cases = metrics["approved_excuse_cases"]
    high_risk = metrics.get("high_risk_employees", 0)
    dep = metrics.get("total_estimated_deduction", 0.0)
    dep_capped = metrics.get("total_deduction_capped", 0.0)
    dq_score = metrics.get("data_quality_score", "n/a")

    employee_summary = metrics.get("employee_summary")
    if employee_summary is not None and not employee_summary.empty:
        top = employee_summary.iloc[0]
        top_late_line = (
            f"**{top['First Name']}** "
            f"({int(top['total_late_minutes'])} unexcused min, "
            f"risk score {int(top['risk_score'])}, {top['risk_level']})"
        )
    else:
        top_late_line = "_none_"

    hours = late_min // 60
    minutes = late_min % 60

    return f"""## Executive Summary

### Executive Highlights
- Workforce covered: **{total_emp}** employees.
- Data Quality Score: **{dq_score} / 100** (see Data Quality section for
  the underlying penalties).
- **{late}** late day(s) totaling **{late_min}** unexcused minutes
  ({hours}h {minutes}m).
- Approved excuses absorbed **{excused}** minutes of delay across
  **{excuse_cases}** day(s).
- **{leave}** day(s) classified as Leave.
- Top late employee: {top_late_line}.

### Top Concerns
- **{high_risk}** employee(s) flagged as High Risk by the compound
  scoring (late frequency, unexcused minutes, missing check-outs,
  repeated excuses).
- **{missing_co}** day(s) recorded a Check In but no Check Out --
  resolve with operations before finalizing payroll.
- **{missing_sch}** day(s) belong to employees missing from the Odoo
  resources export; lateness could not be computed for them.

### Operational Risks
- Repeated lateness is concentrated in the top names of the Top Late
  Employees table; their cumulative impact dominates the payroll
  estimate.
- Missing check-outs may indicate device sync gaps or unfinalized days;
  surface to BioTime / floor managers and re-run when stabilized.

### HR Recommendations
- Hold one-to-one conversations with High Risk employees and discuss
  underlying causes (commute, shift fit, recurring excuses).
- Ensure every active employee has a Working Time assigned in Odoo so
  the Missing Schedule count drops to zero next month.
- Reconcile the {missing_co} Missing Check-Out day(s) before payroll cut.

### Payroll Impact
- Estimated payroll deduction (uncapped): **{dep:.2f}**.
- Estimated payroll deduction (after the **{MAX_MONTHLY_DEDUCTION:.0f}**
  per-employee monthly cap): **{dep_capped:.2f}**.

### Action Plan for Next Month
1. Schedule one-to-ones with High Risk employees within the first week.
2. Close out Missing Check-Out cases and finalize the prior month.
3. Add the Missing Schedule employees to Odoo resources.
4. Re-run this pipeline mid-month for an early-warning check.
"""


def _write_section(f, title, df, empty_message, head=None):
    f.write(f"\n\n## {title}\n")
    if df is None or df.empty:
        f.write(empty_message + "\n")
        return
    table = df.head(head) if head else df
    f.write(table.to_markdown(index=False))


def _write_data_quality(f, metrics):
    """Render the Data Quality section.

    Shows the composite score and the audit counters that drove it, so
    HR can see exactly which signal hurt the score.
    """
    score = metrics.get("data_quality_score", "n/a")
    rows = [
        ("Missing Schedule Cases", metrics.get("missing_schedule_cases", 0)),
        ("Missing Check-Out Cases", metrics.get("missing_check_out_cases", 0)),
        ("Orphan Attendance Records", metrics.get("orphan_attendance_records", 0)),
        ("Unscheduled Active Employees", metrics.get("unscheduled_active_employees", 0)),
        ("Duplicate Employee Names", metrics.get("duplicate_employee_names", 0)),
        ("Missing Employee IDs", metrics.get("missing_employee_ids", 0)),
        ("Invalid Punches", metrics.get("invalid_punches_count", 0)),
    ]
    f.write(f"\n\n## Data Quality\n")
    f.write(f"**Data Quality Score: {score} / 100.** Higher is cleaner.\n\n")
    f.write("| Signal | Count |\n|---|---:|\n")
    for label, value in rows:
        f.write(f"| {label} | {value} |\n")


def _write_hr_audit_flags(f, metrics):
    """Render the HR Audit Flags section: only employees with a non-empty
    audit_flags string. Each flag is explained in the legend below."""
    master = metrics.get("employee_master")
    f.write("\n\n## HR Audit Flags\n")
    f.write(
        "Flag legend: `chronic_lateness` (>=5 late days), "
        "`repeated_missing_checkouts` (>=5), `excessive_excuses` (>=4), "
        "`no_assigned_schedule` (active without Odoo schedule), "
        "`attendance_anomaly` (single delay >= 240 min, suggests wrong-shift).\n\n"
    )
    if master is None or master.empty:
        f.write("No employee master available.\n")
        return
    flagged = master[master["audit_flags"].astype(bool)]
    if flagged.empty:
        f.write("No employees flagged this period.\n")
        return
    cols = [
        "Employee ID", "First Name", "Status Consistency",
        "late_count", "missing_checkout_count", "excuse_count",
        "audit_flags",
    ]
    f.write(flagged[cols].to_markdown(index=False))


def _write_employee_count_reconciliation(f, metrics):
    """Render the Employee Count Reconciliation section.

    Explains why our headline employee number can diverge from Odoo /
    BioTime and surfaces the reconciliation table built by
    metrics_calculator.
    """
    reporting_population = metrics.get(
        "reporting_population", metrics.get("total_employees", 0)
    )
    table = metrics.get("employee_reconciliation")

    f.write("\n\n## Employee Count Reconciliation\n")
    f.write(
        f"The headline employee count in this report is **Reporting "
        f"Population = {reporting_population}**. This number may differ "
        "from other employee dashboards for these reasons:\n\n"
    )
    f.write(
        "- **Odoo Employees screen** counts every Odoo-configured "
        "employee (active + leave + recently created), regardless of "
        "whether they punched. It can be larger than this report when "
        "employees took the full month off, and smaller when the "
        "attendance file still contains inactive / decommissioned IDs.\n"
    )
    f.write(
        "- **BioTime dashboard** typically counts only currently active "
        "terminal users; an export covering a closed period can include "
        "more IDs than the live BioTime count.\n"
    )
    f.write(
        "- **This pipeline** uses **Employees With Check-ins** as the "
        "Reporting Population: the set of employees who actually punched "
        "in during the exported period. That is the auditable number for "
        "monthly HR reporting.\n\n"
    )
    if table is None or table.empty:
        f.write("Reconciliation table not available.\n")
    else:
        f.write(table.to_markdown(index=False))


def generate_ai_input_file(metrics, attendance_daily):
    now = datetime.now()
    monthly_dir = REPORT_OUTPUT_DIR / now.strftime("%Y-%m")
    monthly_dir.mkdir(parents=True, exist_ok=True)
    file_path = monthly_dir / "claude_hr_report_input.md"

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

        f.write(_build_executive_summary(metrics))

        f.write("\n## Summary KPIs\n")
        for key, value in metrics.items():
            # Skip DataFrames (rendered in their own sections) and Nones.
            if isinstance(value, pd.DataFrame) or value is None:
                continue
            f.write(f"- {key}: {value}\n")

        _write_employee_count_reconciliation(f, metrics)
        _write_data_quality(f, metrics)

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
        )

        _write_hr_audit_flags(f, metrics)

        alias_audit = metrics.get("alias_audit")
        if alias_audit is not None and not alias_audit.empty:
            f.write("\n\n## Employee ID Alias Mapping\n")
            f.write(
                "Some employees previously held more than one BioTime "
                "Employee ID because old fingerprint devices used old "
                "IDs and newer devices use new ones. Old IDs were "
                "decommissioned in Odoo, but historical punches still "
                "carry them. The pipeline remaps these to the current "
                "Employee ID immediately after loading attendance, so "
                "the rest of the report (lateness, overtime, "
                "reconciliation, absence, etc.) sees a unified ID.\n\n"
            )
            f.write(
                f"- Active aliases configured: **{len(alias_audit)}**\n"
                f"- Attendance rows remapped: "
                f"**{int(alias_audit['records_mapped'].sum())}**\n\n"
            )
            f.write(alias_audit.to_markdown(index=False))

        f.write("\n\n## Break Analysis\n")
        break_count = metrics.get("total_break_count", 0)
        break_min = metrics.get("total_break_minutes", 0)
        emp_breaks = metrics.get("employees_with_breaks", 0)
        incomplete_brk = metrics.get("incomplete_break_records", 0)
        f.write(
            f"- Total break count: **{break_count}**\n"
            f"- Total break minutes: **{break_min}** "
            f"({break_min // 60}h {break_min % 60}m)\n"
            f"- Employees with breaks: **{emp_breaks}**\n"
            f"- Incomplete break records: **{incomplete_brk}**\n\n"
        )
        break_summary = metrics.get("break_summary")
        if break_summary is not None and not break_summary.empty:
            f.write("### Per-employee Break Summary (top 20 by minutes)\n")
            f.write(break_summary.head(20).to_markdown(index=False))
        else:
            f.write("_No break punches recorded this period._\n")
        f.write(
            "\n\n_Notes:_ Break analytics is **informational only**. "
            "Breaks do NOT affect lateness, overtime, early leave, "
            "payroll deduction, risk scoring, attendance_status, or "
            "the data quality score. Incomplete records (a Break Out "
            "without a Break In, or vice versa) are surfaced so HR can "
            "follow up on terminal sync issues.\n"
        )

        # The Excluded Employees section is rendered only when the
        # filtered report still surfaces some exclusion data. When
        # HIDE_EXCLUDED_EMPLOYEES_FROM_REPORT is on the metrics dict
        # arrives empty and the whole section is skipped.
        excluded_summary = metrics.get("excluded_employees_summary")
        if excluded_summary is not None and not excluded_summary.empty:
            f.write("\n\n## Excluded Employees\n")
            f.write(
                "Excluded employees remain visible in the raw operational "
                "data (Daily Attendance, Daily Trend) so HR can still see "
                "what they did, but their rows are dropped from the "
                "management KPIs (late_cases, overtime_cases, payroll "
                "deduction, risk scoring) according to the per-employee "
                "exclusion flags. Exclusions are policy decisions sourced "
                "from `data/excluded_employees.xlsx`.\n\n"
            )
            f.write(excluded_summary.to_markdown(index=False))

        f.write("\n\n## Early Leave Analysis\n")
        el_cases = metrics.get("early_leave_cases", 0)
        el_min = metrics.get("total_early_leave_minutes", 0)
        el_emps = metrics.get("employees_with_early_leave", 0)
        f.write(
            f"- Early Leave cases: **{el_cases}**\n"
            f"- Total early-leave minutes: **{el_min}** "
            f"({el_min // 60}h {el_min % 60}m)\n"
            f"- Employees with early leave: **{el_emps}**\n\n"
        )
        top_el = metrics.get("top_early_leave_employees")
        if top_el is not None and not top_el.empty:
            f.write("### Top Early Leave Employees\n")
            f.write(top_el.head(20).to_markdown(index=False))
        else:
            f.write("_No early-leave days recorded this period._\n")
        f.write(
            "\n\n_Notes:_ A day counts as Early Leave when the gap "
            "between the Check Out and the shift's Shift End exceeds "
            "the configured grace window. Days with no Check Out or "
            "no shift assignment are excluded.\n"
        )

        f.write("\n\n## Early Leave Anomalies\n")
        anomaly_count = metrics.get("early_leave_anomaly_cases", 0)
        anomaly_rows = daily[
            daily.get("early_leave_anomaly", False) == True  # noqa: E712
        ]
        f.write(
            f"**{anomaly_count}** day(s) recorded an implausibly large "
            "early-leave value (above the configured threshold). These "
            "rows are KEPT in every total -- they are flagged here for "
            "HR review because the underlying cause is usually a "
            "missing Check Out, a wrong shift assignment, a device "
            "sync issue, or a partial attendance record rather than a "
            "genuine early departure.\n\n"
        )
        if anomaly_rows.empty:
            f.write("_No anomalous early-leave rows this period._\n")
        else:
            cols = [
                "Employee ID", "First Name", "Date",
                "Check In", "Check Out",
                "matched_shift_label",
                "early_leave_minutes", "early_leave_anomaly_reason",
            ]
            available = [c for c in cols if c in anomaly_rows.columns]
            f.write(anomaly_rows[available].to_markdown(index=False))

        f.write("\n\n## Overtime Analysis\n")
        ot_cases = metrics.get("overtime_cases", 0)
        ot_hours = metrics.get("total_overtime_hours", 0)
        ot_emps = metrics.get("employees_with_overtime", 0)
        ot_avg = metrics.get("avg_overtime_minutes", 0)
        ot_mult = metrics.get("overtime_multiplier", 1.0)
        ot_payable_hours = metrics.get("total_overtime_payable_hours", 0)
        ot_payable_min = metrics.get("total_overtime_payable_minutes", 0)
        f.write(
            f"- Overtime cases: **{ot_cases}**\n"
            f"- Total overtime (raw duration): **{ot_hours} hours** "
            f"({metrics.get('total_overtime_minutes', 0)} minutes)\n"
            f"- Overtime payroll multiplier: **{ot_mult:g}x**\n"
            f"- Total overtime (payable, after multiplier): "
            f"**{ot_payable_hours} hours** ({ot_payable_min} minutes)\n"
            f"- Employees with overtime: **{ot_emps}**\n"
            f"- Average overtime per case (raw): **{ot_avg} minutes**\n\n"
        )
        top_overtime = metrics.get("top_overtime_employees")
        if top_overtime is not None and not top_overtime.empty:
            f.write("### Top Overtime Employees\n")
            f.write(top_overtime.head(20).to_markdown(index=False))
        else:
            f.write("_No overtime recorded this period._\n")
        f.write(
            "\n\n_Notes:_ Overtime is the time between Shift End and the "
            "actual Check Out, beyond the configured grace period. Rows "
            "with a missing Check Out or a missing Working Time cannot "
            "contribute to overtime. Accuracy depends on the Odoo "
            "Working Time labels reflecting each employee's true shift.\n"
            "\n"
            f"> **Payroll note:** Overtime is reported as **both** the raw "
            f"physical duration (`overtime_minutes` / `total_overtime_hours`) "
            f"and a payroll-adjusted duration (`overtime_payable_minutes` / "
            f"`total_overtime_payable_hours`). The current payroll multiplier "
            f"is **{ot_mult:g}x** (config: `OVERTIME_PAY_MULTIPLIER`). Set "
            f"it to `1.0` to disable the premium without touching code. "
            f"Per-row rounding: half-up to the nearest minute (so 1:30 "
            f"raw -> 2:15 payable at 1.5x).\n"
        )

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
