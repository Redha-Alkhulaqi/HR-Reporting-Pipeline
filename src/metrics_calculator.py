import re
from datetime import datetime

import pandas as pd


GRACE_MINUTES = 15  # Rule 6: grace period after shift start before an arrival counts as late.


def extract_shift_start(working_time):
    """Pull the first HH:MMAM/PM token from an Odoo Working Time label."""
    match = re.search(r"\((\d{1,2}:\d{2}\s*[AP]M)", str(working_time), re.IGNORECASE)
    if not match:
        return None

    time_text = match.group(1).replace(" ", "").upper()
    return datetime.strptime(time_text, "%I:%M%p").strftime("%H:%M")


def _delay_minutes(punch_time, shift_start):
    check_in = datetime.strptime(punch_time, "%H:%M:%S")
    shift_start_time = datetime.strptime(shift_start, "%H:%M")
    return int((check_in - shift_start_time).total_seconds() / 60)


def classify_risk(minutes):
    if minutes >= 1000:
        return "High Risk"
    if minutes >= 500:
        return "Medium Risk"
    return "Low Risk"


def calculate_metrics(df, schedules_df):
    # Build a name -> shift-start-HH:MM lookup from the Odoo resources file.
    schedules = schedules_df[["Name", "Working Time"]].copy()
    schedules["Shift Start"] = schedules["Working Time"].apply(extract_shift_start)
    name_to_shift = schedules.set_index("Name")["Shift Start"].to_dict()

    check_ins = df[df["Punch State"] == "Check In"]

    # First check-in per employee per day. Punch Time is a zero-padded
    # HH:MM:SS string, so a lexical min is also the chronological earliest.
    daily = (
        check_ins.groupby(["Employee ID", "First Name", "Date"])["Punch Time"]
        .min()
        .reset_index()
        .rename(columns={"Punch Time": "Check In"})
    )

    daily["Shift Start"] = daily["First Name"].map(name_to_shift)
    daily["missing_schedule"] = daily["Shift Start"].isna()

    # Employees not listed in the schedules file have no Shift Start; we
    # cannot compute lateness for them, so leave their delay at 0.
    daily["Delay Minutes"] = daily.apply(
        lambda row: _delay_minutes(row["Check In"], row["Shift Start"])
        if pd.notna(row["Shift Start"])
        else 0,
        axis=1,
    )

    # Grace period is a threshold: once exceeded, the full delay counts
    # as late minutes, per the Rule 6 formula MAX(0, Check-in - Shift Start).
    daily["is_late"] = daily["Delay Minutes"] > GRACE_MINUTES

    late_employees = daily[daily["is_late"]]

    employee_summary = (
        late_employees.groupby("Employee ID")
        .agg(
            late_count=("is_late", "sum"),
            total_late_minutes=("Delay Minutes", "sum"),
            avg_late_minutes=("Delay Minutes", "mean"),
        )
        .reset_index()
        .sort_values(by="total_late_minutes", ascending=False)
    )
    employee_summary["risk_level"] = employee_summary["total_late_minutes"].apply(
        classify_risk
    )

    summary = {
        "total_employees": df["Employee ID"].nunique(),
        "late_cases": int(daily["is_late"].sum()),
        "total_late_minutes": int(daily.loc[daily["is_late"], "Delay Minutes"].sum()),
        "employee_summary": employee_summary,
    }

    return summary, daily
