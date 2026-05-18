# HR_REPORTING_RULES_MASTER

## 1. Reporting Period
- Monthly report period is defined by the user.
- All calculations must be limited to the report period only.
- However, Odoo leaves must be included if they overlap the report period, even if they start before or end after it.

## 2. Data Sources
### Odoo 19 Enterprise
Official source for:
- Approved leaves
- Approved permissions
- HR approvals
- Official justification records

### BioTime Cloud
Official source for:
- Actual punches
- Check-in
- Check-out
- Actual work hours
- Shift records
- Attendance raw status

## 3. Priority Order
Daily status must be resolved in this order:

1. Approved Leave
2. Approved Permission
3. Actual Attendance
4. Attendance Exceptions
5. Unjustified Absence

Odoo Approved Leave overrides BioTime Absence.

## 4. Leave Expansion Rule
Every approved leave record must be expanded into daily coverage.

Include any leave record where:
Leave Start <= Report End
AND
Leave End >= Report Start

Each working day within the overlap must be classified as Approved Leave unless there is a deliberate exception that requires manual review.

## 5. Permission Rule
If an approved permission overlaps late arrival or early departure, classify the relevant portion as Approved Permission, not a violation.

## 6. Late Rule
A 15-minute grace period is allowed after shift start.

Late Minutes =
MAX(0, Check-in - Shift Start)

## 7. Early Leave Rule
Early leave is calculated when Check-out is earlier than Shift End and is not covered by approved permission.

## 8. Missing Punch Rule
Missing Punch is only valid when attendance data is finalized and one of Check-in or Check-out is missing.

Do not classify current-day or unstable prior-day records as Missing Punch.

## 9. Attendance Finalization Rule
Current day:
- Pending Day Closure
- Excluded from final KPIs

Previous day:
If many employees have missing check-out or incomplete work hours:
- Pending Attendance Sync
- Exclude unstable records from final KPIs

## 10. Overtime Rule
Overtime =
Actual Worked Hours - Scheduled Shift Hours

If Work Hours is zero or missing, do not count overtime, even if BioTime has an OT value. Classify as Overtime Without Work Hours.

### 10.1 Payroll Multiplier (current behavior: 1.5x active)
**Overtime is reported in two parallel forms: the raw physical duration AND a payroll-adjusted (payable) duration. A global `1.5x` payroll multiplier is applied to every minute of overtime AFTER classification. The multiplier is the single config constant `OVERTIME_PAY_MULTIPLIER` and can be set per environment.**

Rule:
```
overtime_payable_minutes = round_half_up(overtime_minutes * OVERTIME_PAY_MULTIPLIER)
```

Defaults:
- `OVERTIME_PAY_MULTIPLIER = 1.5` (set to `1.0` to disable the premium without touching code).
- Rounding: half-up to the nearest minute (Python's built-in `round()` uses banker's rounding; we use half-up for payroll predictability).
- Examples: `2:00` raw → `3:00` payable; `1:30` raw → `2:15` payable.

Implications for downstream consumers:
- **Raw fields are NEVER mutated.** `overtime_minutes`, `total_overtime_hours`, and `Total Over Time (Hours)` continue to mean the **physical worked duration beyond the scheduled shift**, exactly as in every prior report.
- **Payable fields are new and parallel:** `overtime_payable_minutes`, `overtime_payable_hours`, `total_overtime_payable_minutes`, `total_overtime_payable_hours`, and `overtime_multiplier`. HR/payroll should use the payable totals; auditors can reconcile against raw using the multiplier.
- The multiplier is applied **once**, in a centralized payroll-adjustment layer (`_apply_overtime_payroll_adjustment` in `src/metrics_calculator.py`), AFTER every overtime classifier (standard and `TOTAL_SPAN_MINUS_8H`) has produced raw `overtime_minutes`. Policy branches never need to know about the multiplier.
- The `LATE_MINUTE_COST` / `estimated_deduction` / `deduction_capped` figures still apply to **lateness only**, and never to overtime.

For the full architecture, field schema, rounding rules, and per-employee override extension path, see README §14.

## 11. Abnormal Attendance
If Check-in occurs after Shift End:
- Classify as Abnormal Attendance
- Calculate actual work hours if both punches exist
- Do not treat as normal Present

## 12. Consecutive Absence
If an employee has 3 or more consecutive unjustified absence days:
- Flag as Consecutive Absence
- Add to Critical Attendance Cases

## 13. Status Mapping
Allowed final statuses:
- Present
- Late
- Early Leave
- Approved Leave
- Approved Permission
- Unjustified Absence
- Pending Verification
- Missing Punch
- Abnormal Attendance
- Shift Conflict
- Overtime Without Work Hours
- Present Without Punches
- Pending Day Closure
- Pending Attendance Sync

## 14. KPI Rules
Approved Leave days:
- Do not count as absence
- Do not count as missing punch
- Do not count as attendance failure

Pending records:
- Exclude from final KPI totals
- Show in Data Quality & Synchronization Notes

## 15. Manual Review
Any unclear or conflicting record must be placed in Exceptions & Manual Review with a reason.
