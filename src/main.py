"""Entry point for the monthly HR reporting pipeline.

Flow:
1. Load three Odoo / BioTime exports from data/.
2. Validate each input; warnings are logged, blocking errors stop the run.
3. Compute attendance metrics (status classification, payroll, risk).
4. Emit the Excel report and the Claude Markdown brief.

Outputs are written under REPORT_OUTPUT_DIR/YYYY-MM/. Logs land in
logs/pipeline.log.
"""
import logging

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


def _log_validation(report):
    logger.info(f"Validation [{report.label}]: {report.row_count} rows")
    for warning in report.warnings:
        logger.warning(f"Validation [{report.label}]: {warning}")


def main():
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
            f"high_risk={summary.get('high_risk_employees', 0)}"
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
