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
from config import LOG_LEVEL, PROJECT_ROOT
from data_loader import (
    load_attendance_file,
    load_time_off_file,
    load_working_schedule_file,
)
from excel_exporter import export_report
from metrics_calculator import calculate_metrics
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
        schedules_df = load_working_schedule_file(
            PROJECT_ROOT / "data/Resources (resource.resource).xlsx"
        )
        time_off_df = load_time_off_file(
            PROJECT_ROOT
            / "data/Time Off Custom - Simplified Duration Calculation (hr.leave).xlsx"
        )
        logger.info(
            f"Input files loaded: attendance={len(df)} schedules={len(schedules_df)} "
            f"time_off={len(time_off_df) if time_off_df is not None else 0}"
        )

        if args.from_date or args.to_date:
            before = len(df)
            df = _filter_period(df, args.from_date, args.to_date)
            logger.info(
                f"Period filter [{args.from_date or '-'} .. {args.to_date or '-'}]: "
                f"{before} -> {len(df)} attendance rows"
            )

        _log_validation(validate_attendance(df))
        _log_validation(validate_schedules(schedules_df))
        _log_validation(validate_time_off(time_off_df))

        summary, daily = calculate_metrics(df, schedules_df, time_off_df)
        logger.info(
            f"Metrics computed: daily_rows={len(daily)} "
            f"late={summary['late_cases']} "
            f"excuse={summary['approved_excuse_cases']} "
            f"leave={summary['leave_cases']} "
            f"missing_schedule={summary['missing_schedule_cases']} "
            f"missing_checkout={summary['missing_check_out_cases']} "
            f"high_risk={summary.get('high_risk_employees', 0)} "
            f"data_quality_score={summary.get('data_quality_score', 'n/a')}"
        )

        generate_report(summary)
        logger.info("HR report generated")

        export_report(summary, daily)
        logger.info("Excel report exported")

        generate_ai_input_file(summary, daily)
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
