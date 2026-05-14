def generate_report(data):
    print("Generating HR report...")

    for key, value in data.items():
        if key != "employee_summary":
            print(f"{key}: {value}")