from datetime import datetime


SHIFT_START = "08:00"
GRACE_MINUTES = 15  # Rule 6: grace period after shift start before an arrival counts as late.


def calculate_metrics(df):
    shift_start = datetime.strptime(SHIFT_START, "%H:%M")

    check_ins = df[df["Punch State"] == "Check In"]

    # First check-in per employee per day. Punch Time is a zero-padded
    # HH:MM:SS string, so a lexical min is also the chronological earliest.
    first_check_ins = check_ins.groupby(["Employee ID", "Date"])["Punch Time"].min()

    late_cases = 0
    total_late_minutes = 0

    for punch_time in first_check_ins:
        check_in = datetime.strptime(punch_time, "%H:%M:%S")
        delay = (check_in - shift_start).total_seconds() / 60

        # Grace period is a threshold: once exceeded, the full delay counts
        # as late minutes, per the Rule 6 formula MAX(0, Check-in - Shift Start).
        if delay > GRACE_MINUTES:
            late_cases += 1
            total_late_minutes += int(delay)

    return {
        "total_employees": df["Employee ID"].nunique(),
        "late_cases": late_cases,
        "total_late_minutes": total_late_minutes,
    }
