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

### 10.1 Payroll Multiplier (current behavior)
**Overtime is reported as actual overtime duration only. This pipeline does not apply a 1.5x payroll/pay-rate multiplier. Any payroll multiplier must be handled separately outside this report unless explicitly added in a future release.**

Implications for downstream consumers:
- `overtime_minutes`, `total_overtime_hours`, and `Total Over Time (Hours)` are the **physical worked durations beyond the scheduled shift**, not amounts of payable overtime.
- Premium-rate overtime pay (e.g. 1.5x) must be calculated in payroll using these duration fields as inputs.
- The `LATE_MINUTE_COST` / `estimated_deduction` / `deduction_capped` figures apply to **lateness only**, and never to overtime.

If a future release adds a payroll multiplier, the agreed extension point is documented in README §14 (config constant + derived aggregate field; the per-row duration must remain unchanged).

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
