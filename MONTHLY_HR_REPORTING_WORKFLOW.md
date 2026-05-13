# MONTHLY_HR_REPORTING_WORKFLOW

## Goal
Generate a monthly HR attendance report with consistent business rules and minimal rework.

## Monthly Inputs
1. BioTime Cloud export for the report period.
2. Odoo approved leaves and permissions export.
3. The fixed Excel report template.
4. HR_REPORTING_RULES_MASTER.

## Workflow
### Step 1 — Data Intake
- Load BioTime data.
- Load Odoo leaves and permissions.
- Confirm the reporting period.

### Step 2 — Data Quality Check
- Check missing employee IDs.
- Check duplicate rows.
- Check incomplete current or prior-day attendance.
- Identify days with possible sync delay.

### Step 3 — Leave Reconciliation
- Extract all approved leaves overlapping the report period.
- Expand leave date ranges into daily leave coverage.
- Match daily attendance rows against leave coverage.
- Correct absence/missing-punch classifications where leave exists.

### Step 4 — Permission Reconciliation
- Match approved permissions to late arrival or early departure.
- Correct violation classification where permission exists.

### Step 5 — Attendance Classification
Apply priority order:
Approved Leave → Approved Permission → Actual Attendance → Exceptions → Unjustified Absence.

### Step 6 — Calculations
- Late minutes
- Early leave minutes
- Work hours
- Overtime
- Attendance percentage
- Compliance percentage
- Department-level KPIs

### Step 7 — Exceptions
Create a separate manual-review list:
- Missing punch
- Present without punches
- Overtime without work hours
- Consecutive absence
- Abnormal attendance
- Shift conflict
- Pending verification
- Data sync issues

### Step 8 — Final Excel Output
Fill the Excel template:
- Executive Summary
- KPI Dashboard
- Employee Daily Details
- Attendance Summary
- Overtime Analysis
- Leaves & Permissions
- Exceptions & Manual Review
- Consecutive Absence Cases
- Department Analysis
- HR Recommendations
- Business Rules Reference

### Step 9 — HR Review
HR reviews:
- Pending verification
- Leave mismatches
- High-risk employees
- Critical attendance cases

### Step 10 — Final Export
Export Excel and optionally PDF for management.
