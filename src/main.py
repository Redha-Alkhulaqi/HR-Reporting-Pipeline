import logging

from config import PROJECT_ROOT, LOG_LEVEL
from data_loader import load_attendance_file, load_working_schedule_file
from report_generator import generate_report
from excel_exporter import export_report
from metrics_calculator import calculate_metrics
from ai_summary_generator import generate_ai_input_file


logs_dir = PROJECT_ROOT / "logs"
logs_dir.mkdir(exist_ok=True)

logger = logging.getLogger("hr_pipeline")
logger.setLevel(LOG_LEVEL)

file_handler = logging.FileHandler(logs_dir / "pipeline.log", encoding="utf-8")
file_handler.setLevel(LOG_LEVEL)

formatter = logging.Formatter(
    "%(asctime)s - %(levelname)s - %(message)s"
)
file_handler.setFormatter(formatter)

if not logger.handlers:
    logger.addHandler(file_handler)


def main():
    logger.info("Pipeline started")
    print("Starting HR Reporting Pipeline...")

    try:
        df = load_attendance_file(PROJECT_ROOT / "data/attendance_raw.xlsx")
        schedules_df = load_working_schedule_file(
        PROJECT_ROOT / "data/Resources (resource.resource).xlsx")
        logger.info("Attendance file loaded")

        summary, daily = calculate_metrics(df, schedules_df)
        logger.info("Attendance data processed")

        generate_report(summary)
        logger.info("HR report generated")

        export_report(summary, daily)
        logger.info("Excel report exported")

        generate_ai_input_file(summary, daily)
        logger.info("AI input file generated")
    except Exception:
        logger.exception("Pipeline failed")
        print("Pipeline failed. See logs/pipeline.log for details.")
        raise

    logger.info("Pipeline completed successfully")
    print("Pipeline completed successfully.")


if __name__ == "__main__":
    main()