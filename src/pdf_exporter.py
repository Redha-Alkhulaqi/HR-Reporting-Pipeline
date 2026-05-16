"""Placeholder for the future PDF export channel.

The pipeline currently writes Excel and Markdown via excel_exporter and
ai_summary_generator. PDF is intentionally deferred -- it needs a
renderer choice (reportlab, weasyprint, headless LibreOffice), styling
work, and Arabic font / RTL handling. This stub fixes the public
interface ahead of that work so wiring it into main is a one-line
change later.
"""
from datetime import datetime

from config import REPORT_OUTPUT_DIR


def export_pdf(summary, daily):
    """Render the monthly HR report to PDF. Not implemented yet."""
    raise NotImplementedError(
        "PDF export is not implemented yet. See FINAL_PROJECT_STATUS.md "
        "for the roadmap entry."
    )


def planned_output_path(now=None):
    """Return where the PDF will land once export_pdf is implemented."""
    now = now or datetime.now()
    monthly_dir = REPORT_OUTPUT_DIR / now.strftime("%Y-%m")
    return monthly_dir / f"hr_report_{now.strftime('%Y%m%d_%H%M%S')}.pdf"
