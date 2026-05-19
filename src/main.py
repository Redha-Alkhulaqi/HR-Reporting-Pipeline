"""Entry point for the monthly HR reporting pipeline.

Flow:
1. Load three Odoo / BioTime exports from data/.
2. (Optional) filter attendance to a reporting period from CLI args.
3. Validate each input; warnings are logged, blocking errors stop the run.
4. Compute attendance metrics (status classification, payroll, risk,
   employee master, data quality score).
5. Emit the Excel report and the Claude Markdown brief.

CLI:
  python src/main.py                          # whole attendance file
  python src/main.py --month 2026-05          # one calendar month
  python src/main.py --from 2026-05-01 --to 2026-05-15   # custom range

Outputs land under REPORT_OUTPUT_DIR/YYYY-MM/. Logs go to
logs/pipeline.log.
"""
import argparse
import calendar
import logging

import pandas as pd

from ai_summary_generator import generate_ai_input_file
from config import HIDE_EXCLUDED_EMPLOYEES_FROM_REPORT, LOG_LEVEL, PROJECT_ROOT
from data_loader import (
    apply_employee_id_aliases,
    load_attendance_file,
    load_employee_id_aliases_file,
    load_employee_weekly_off_file,
    load_excluded_employees_file,
    load_overtime_policy_overrides_file,
    load_time_off_file,
    load_working_schedule_file,
)
from excel_exporter import export_report
from manual_punch_corrections import (
    apply_manual_punch_corrections,
    load_manual_punch_corrections_file,
)
from metrics_calculator import calculate_metrics, filter_inputs_for_report
from report_generator import generate_report
from validators import (
    ValidationError,
    validate_attendance,
    validate_schedules,
    validate_time_off,
)


logs_dir = PROJECT_ROOT / "logs"
logs_dir.mkdir(exist_ok=True)

logger = logging.getLogger("hr_pipeline")
logger.setLevel(LOG_LEVEL)

file_handler = logging.FileHandler(logs_dir / "pipeline.log", encoding="utf-8")
file_handler.setLevel(LOG_LEVEL)

formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
file_handler.setFormatter(formatter)

if not logger.handlers:
    logger.addHandler(file_handler)


def _parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Run the monthly HR reporting pipeline."
    )
    p.add_argument(
        "--month",
        help="YYYY-MM. Convenience: sets --from / --to to that month's bounds.",
    )
    p.add_argument(
        "--from", dest="from_date",
        help="YYYY-MM-DD inclusive lower bound for the attendance period.",
    )
    p.add_argument(
        "--to", dest="to_date",
        help="YYYY-MM-DD inclusive upper bound for the attendance period.",
    )
    args = p.parse_args(argv)

    if args.month:
        if args.from_date or args.to_date:
            p.error("--month is mutually exclusive with --from / --to")
        try:
            year, month = (int(x) for x in args.month.split("-"))
        except ValueError:
            p.error("--month must be YYYY-MM (e.g. 2026-05)")
        last_day = calendar.monthrange(year, month)[1]
        args.from_date = f"{args.month}-01"
        args.to_date = f"{args.month}-{last_day:02d}"
    return args


def _filter_period(df, from_date, to_date):
    """Restrict attendance rows to [from_date, to_date] inclusive."""
    if from_date is None and to_date is None:
        return df
    dates = pd.to_datetime(df["Date"], errors="coerce")
    mask = pd.Series(True, index=df.index)
    if from_date:
        mask &= dates >= pd.to_datetime(from_date)
    if to_date:
        mask &= dates <= pd.to_datetime(to_date)
    return df[mask].copy()


def _log_validation(report):
    logger.info(f"Validation [{report.label}]: {report.row_count} rows")
    for warning in report.warnings:
        logger.warning(f"Validation [{report.label}]: {warning}")


def main(argv=None):
    args = _parse_args(argv)
    logger.info("Pipeline started")
    print("Starting HR Reporting Pipeline...")

    try:
        df = load_attendance_file(PROJECT_ROOT / "data/attendance_raw.xlsx")

        # Capture the attendance file's native date range BEFORE any
        # transformation. This is the authoritative reporting period
        # when the user does not pass --from/--to: it ignores stray
        # dates that manual punch corrections may inject (e.g. an HR
        # form filled in with a typo year/month).
        raw_dates = pd.to_datetime(df["Date"], errors="coerce").dropna()
        inferred_period_start = (
            raw_dates.min().normalize() if not raw_dates.empty else None
        )
        inferred_period_end = (
            raw_dates.max().normalize() if not raw_dates.empty else None
        )

        # Apply temporary manual (camera-verified) forgotten-punch
        # corrections BEFORE alias remapping so any historical IDs in
        # the HR form get unified by the alias step alongside biometric
        # rows. Anything not approved+camera ends up in
        # rejected_corrections for Exceptions & Manual Review.
        manual_corrections_df = load_manual_punch_corrections_file(
            PROJECT_ROOT / "data/manual_forgotten_punches.xlsx"
        )
        df, rejected_corrections = apply_manual_punch_corrections(
            df, manual_corrections_df
        )
        applied_n = int(df["is_manual_correction"].sum())
        if applied_n or not rejected_corrections.empty:
            logger.info(
                f"Manual punch corrections: applied={applied_n} "
                f"rejected={len(rejected_corrections)}"
            )
        for _, rej in rejected_corrections.iterrows():
            raw_date = rej.get("date")
            try:
                date_str = pd.to_datetime(raw_date).strftime("%Y-%m-%d")
            except (ValueError, TypeError):
                date_str = "" if pd.isna(raw_date) else str(raw_date)
            logger.warning(
                "Rejected correction: "
                f"employee={rej.get('employee_code')} "
                f"date={date_str} "
                f"type={rej.get('punch_type')} "
                f"reason={rej.get('rejection_reason')}"
            )

        schedules_df = load_working_schedule_file(
            PROJECT_ROOT / "data/Resources (resource.resource).xlsx"
        )
        time_off_df = load_time_off_file(
            PROJECT_ROOT
            / "data/Time Off Custom - Simplified Duration Calculation (hr.leave).xlsx"
        )
        excluded_df = load_excluded_employees_file(
            PROJECT_ROOT / "data/excluded_employees.xlsx"
        )
        aliases_df = load_employee_id_aliases_file(
            PROJECT_ROOT / "data/employee_id_aliases.xlsx"
        )
        weekly_off_df = load_employee_weekly_off_file(
            PROJECT_ROOT / "data/employee_weekly_off.xlsx"
        )
        overtime_policy_overrides_df = load_overtime_policy_overrides_file(
            PROJECT_ROOT / "data/overtime_policy_overrides.xlsx"
        )
        logger.info(
            f"Input files loaded: attendance={len(df)} schedules={len(schedules_df)} "
            f"time_off={len(time_off_df) if time_off_df is not None else 0} "
            f"exclusions={len(excluded_df)} aliases={len(aliases_df)} "
            f"weekly_off_overrides={len(weekly_off_df)} "
            f"overtime_policy_overrides={len(overtime_policy_overrides_df)}"
        )

        # Apply alias mapping IMMEDIATELY after loading so every
        # downstream step (validation, daily aggregation, reconciliation,
        # absence detection, late/overtime/early leave/break) sees the
        # unified Employee ID.
        df, alias_audit, alias_warnings = apply_employee_id_aliases(
            df, aliases_df, schedules_df
        )
        for w in alias_warnings:
            logger.warning(f"Alias mapping: {w}")
        if not alias_audit.empty:
            logger.info(
                f"Alias mapping: {len(alias_audit)} active alias(es), "
                f"{int(alias_audit['records_mapped'].sum())} attendance rows remapped"
            )

        if args.from_date or args.to_date:
            before = len(df)
            df = _filter_period(df, args.from_date, args.to_date)
            logger.info(
                f"Period filter [{args.from_date or '-'} .. {args.to_date or '-'}]: "
                f"{before} -> {len(df)} attendance rows"
            )

        # Authoritative reporting period: CLI bounds win over the
        # inferred attendance-file range. Used by the absence calendar
        # so out-of-window manual corrections cannot inflate absences.
        period_start = (
            pd.to_datetime(args.from_date).normalize()
            if args.from_date else inferred_period_start
        )
        period_end = (
            pd.to_datetime(args.to_date).normalize()
            if args.to_date else inferred_period_end
        )
        logger.info(
            f"Reporting period: "
            f"{period_start.strftime('%Y-%m-%d') if period_start is not None else '-'}"
            f" .. "
            f"{period_end.strftime('%Y-%m-%d') if period_end is not None else '-'}"
        )

        _log_validation(validate_attendance(df))
        _log_validation(validate_schedules(schedules_df))
        _log_validation(validate_time_off(time_off_df))

        summary, daily = calculate_metrics(
            df, schedules_df, time_off_df,
            excluded_df=excluded_df, alias_audit=alias_audit,
            period_start=period_start, period_end=period_end,
            weekly_off_df=weekly_off_df,
            overtime_policy_overrides_df=overtime_policy_overrides_df,
        )
        for w in summary.get("overtime_policy_override_warnings", []):
            logger.warning(f"Overtime policy overrides: {w}")
        ot_override_n = summary.get("overtime_policy_override_count", 0)
        if ot_override_n:
            logger.info(
                f"Overtime policy overrides active for "
                f"{ot_override_n} employee(s)."
            )
        logger.info(
            f"Metrics computed: daily_rows={len(daily)} "
            f"late={summary['late_cases']} "
            f"excuse={summary['approved_excuse_cases']} "
            f"leave={summary['leave_cases']} "
            f"missing_schedule={summary['missing_schedule_cases']} "
            f"missing_checkout={summary['missing_check_out_cases']} "
            f"high_risk={summary.get('high_risk_employees', 0)} "
            f"excluded={summary.get('excluded_employee_count', 0)} "
            f"data_quality_score={summary.get('data_quality_score', 'n/a')}"
        )
        for warn in summary.get("absence_audit_breaks", []):
            logger.warning(f"Absence audit reconciliation break: {warn}")

        # Build the REPORT view -- by default we hide excluded employees
        # from every exported sheet and from the Claude markdown. The
        # internal `summary`/`daily` above keep them for audit.
        report_summary, report_daily = summary, daily
        report_punches, report_schedules = df, schedules_df
        if HIDE_EXCLUDED_EMPLOYEES_FROM_REPORT and excluded_df is not None and not excluded_df.empty:
            r_df, r_sched, r_tof, hidden = filter_inputs_for_report(
                df, schedules_df, time_off_df, excluded_df
            )
            if hidden > 0:
                report_summary, report_daily = calculate_metrics(
                    r_df, r_sched, r_tof,
                    excluded_df=None, alias_audit=alias_audit,
                    period_start=period_start, period_end=period_end,
                    weekly_off_df=weekly_off_df,
                    overtime_policy_overrides_df=overtime_policy_overrides_df,
                )
                report_punches, report_schedules = r_df, r_sched
            logger.info(f"Excluded employees hidden from report: {hidden}")

        # Surface manual-correction rejections in the exported workbook.
        report_summary["rejected_punch_corrections"] = rejected_corrections

        generate_report(report_summary)
        logger.info("HR report generated")

        export_report(
            report_summary, report_daily,
            raw_punches=report_punches, schedules_df=report_schedules,
        )
        logger.info("Excel report exported")

        generate_ai_input_file(report_summary, report_daily)
        logger.info("AI input file generated")

    except ValidationError as exc:
        logger.error(f"Validation failed: {exc}")
        print(f"Pipeline failed validation: {exc}")
        raise
    except Exception:
        logger.exception("Pipeline failed")
        print("Pipeline failed. See logs/pipeline.log for details.")
        raise

    logger.info("Pipeline completed successfully")
    print("Pipeline completed successfully.")


if __name__ == "__main__":
    main()
