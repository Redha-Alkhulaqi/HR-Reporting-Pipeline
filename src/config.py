from dotenv import load_dotenv
from pathlib import Path
import os

load_dotenv()

# Absolute path to the project root, independent of the current working directory.
PROJECT_ROOT = Path(__file__).resolve().parent.parent

APP_ENV = os.getenv("APP_ENV")
REPORT_OUTPUT_DIR = os.getenv("REPORT_OUTPUT_DIR")
LOG_LEVEL = os.getenv("LOG_LEVEL")