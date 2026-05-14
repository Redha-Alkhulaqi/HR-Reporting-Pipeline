import logging

from config import PROJECT_ROOT
from data_loader import load_attendance_file
from attendance_engine import process_attendance
from report_generator import generate_report
from excel_exporter import export_report
from metrics_calculator import calculate_metrics


logs_dir = PROJECT_ROOT / "logs"
logs_dir.mkdir(exist_ok=True)

logger = logging.getLogger("hr_pipeline")
logger.setLevel(logging.INFO)

file_handler = logging.FileHandler(logs_dir / "pipeline.log", encoding="utf-8")
file_handler.setLevel(logging.INFO)

formatter = logging.Formatter(
    "%(asctime)s - %(levelname)s - %(message)s"
)
file_handler.setFormatter(formatter)

if not logger.handlers:
    logger.addHandler(file_handler)


def main():
    logger.info("Pipeline started")

    print("Starting HR Reporting Pipeline...")

    df = load_attendance_file(PROJECT_ROOT / "data/attendance_raw.xlsx")
    logger.info("Attendance file loaded")

    attendance_data = calculate_metrics(df)
    logger.info("Attendance data processed")

    generate_report(attendance_data)
    logger.info("HR report generated")

    export_report(attendance_data)
    logger.info("Excel report exported")

    logger.info("Pipeline completed successfully")
    print("Pipeline completed successfully.")


if __name__ == "__main__":
    main()