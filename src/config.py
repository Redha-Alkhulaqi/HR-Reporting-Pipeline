from dotenv import load_dotenv
from pathlib import Path
import os

load_dotenv()

# Absolute path to the project root, independent of the current working directory.
PROJECT_ROOT = Path(__file__).resolve().parent.parent

APP_ENV = os.getenv("APP_ENV", "development")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
REPORT_OUTPUT_DIR = PROJECT_ROOT / os.getenv("REPORT_OUTPUT_DIR", "outputs")
