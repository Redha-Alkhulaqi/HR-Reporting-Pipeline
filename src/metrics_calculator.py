from datetime import datetime


SHIFT_START = "08:00"
GRACE_MINUTES = 15  # Rule 6: grace period after shift start before an arrival counts as late.


def _delay_minutes(punch_time, shift_start):
    check_in = datetime.strptime(punch_time, "%H:%M:%S")
    return int((check_in - shift_start).total_seconds() / 60)


def calculate_metrics(df):
    shift_start = datetime.strptime(SHIFT_START, "%H:%M")

    check_ins = df[df["Punch State"] == "Check In"]

    # First check-in per employee per day. Punch Time is a zero-padded
    # HH:MM:SS string, so a lexical min is also the chronological earliest.
    daily = (
        check_ins.groupby(["Employee ID", "Date"])["Punch Time"]
        .min()
        .reset_index()
        .rename(columns={"Punch Time": "Check In"})
    )

    daily["Delay Minutes"] = daily["Check In"].apply(
        lambda t: _delay_minutes(t, shift_start)
    )
    # Grace period is a threshold: once exceeded, the full delay counts
    # as late minutes, per the Rule 6 formula MAX(0, Check-in - Shift Start).
    daily["is_late"] = daily["Delay Minutes"] > GRACE_MINUTES

    late_employees = daily[daily["is_late"] == True]

    employee_summary = (
        late_employees.groupby("Employee ID")
        .agg(
            late_count=("is_late", "sum"),
            total_late_minutes=("Delay Minutes", "sum"),
            avg_late_minutes=("Delay Minutes", "mean"),
        )
        .reset_index()
    )

    employee_summary = employee_summary.sort_values(
        by="total_late_minutes",
        ascending=False
    )

    summary = {
        "total_employees": df["Employee ID"].nunique(),
        "late_cases": int(daily["is_late"].sum()),
        "total_late_minutes": int(
            daily.loc[daily["is_late"], "Delay Minutes"].sum()
        ),
        "employee_summary": employee_summary,
    }

    

    return summary, daily
