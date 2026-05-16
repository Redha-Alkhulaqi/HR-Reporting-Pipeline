import pandas as pd


def generate_report(data):
    print("Generating HR report...")

    # Print only scalar KPIs. DataFrames are rendered in their own
    # destinations (Excel sheets and the Claude markdown sections).
    for key, value in data.items():
        if isinstance(value, pd.DataFrame) or value is None:
            continue
        print(f"{key}: {value}")
