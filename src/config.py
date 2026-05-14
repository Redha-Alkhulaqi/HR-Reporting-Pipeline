from dotenv import load_dotenv
import os

load_dotenv()

APP_ENV = os.getenv("APP_ENV")
REPORT_OUTPUT_DIR = os.getenv("REPORT_OUTPUT_DIR")
LOG_LEVEL = os.getenv("LOG_LEVEL")