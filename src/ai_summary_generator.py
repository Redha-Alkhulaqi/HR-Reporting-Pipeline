from config import PROJECT_ROOT


def generate_ai_input_file(metrics, attendance_daily):
    output_dir = PROJECT_ROOT / "outputs"
    output_dir.mkdir(exist_ok=True)

    file_path = output_dir / "claude_hr_report_input.md"

    late_employees = attendance_daily[attendance_daily["is_late"] == True]

    with open(file_path, "w", encoding="utf-8") as f:
        f.write("# Monthly HR Attendance Report Input\n\n")

        f.write("## Summary KPIs\n")
        for key, value in metrics.items():
            f.write(f"- {key}: {value}\n")

        f.write("\n## Late Attendance Records\n")
        if late_employees.empty:
            f.write("No late attendance records found.\n")
        else:
            f.write(late_employees.to_markdown(index=False))

        f.write("\n\n## Instructions for Claude\n")
        f.write("""
Please generate a professional monthly HR report based on the attendance data above.

The report should include:
1. Executive Summary
2. Key Attendance KPIs
3. Late Arrival Analysis
4. Missing Punch Analysis
5. Employee Attendance Risks
6. HR Recommendations
7. Action Plan for Next Month

Tone: professional, concise, suitable for HR management.
""")

    print(f"Claude input file saved: {file_path}")