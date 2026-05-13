# HR Reporting Pipeline

Centralized HR reporting and analytics pipeline covering attendance, payroll, leaves, and workforce insights.

## Repository Layout

```
.
├── src/                                  # Python source code
│   └── main.py
├── docs/                                 # Architecture & business-rules docs
│   └── hr_reporting_pipeline_docs_templates.md
├── CLAUDE_MONTHLY_HR_REPORT_PROMPT.md    # Claude prompt for monthly report
├── HR_REPORTING_RULES_MASTER.md          # Master reporting rules
├── MONTHLY_HR_REPORTING_WORKFLOW.md      # Monthly workflow steps
├── HR_Monthly_Report_Template.xlsx       # Excel template
├── requirements.txt                      # Python dependencies
└── .gitignore
```

## Getting Started

### 1. Prerequisites
- Python 3.10+
- Git

### 2. Setup

```powershell
# Clone the repository
git clone https://github.com/Redha-Alkhulaqi/HR-Reporting-Pipeline.git
cd HR-Reporting-Pipeline

# Create and activate virtual environment
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# Install dependencies
pip install -r requirements.txt
```

### 3. Run

```powershell
python src/main.py
```

## Documentation

See [docs/hr_reporting_pipeline_docs_templates.md](docs/hr_reporting_pipeline_docs_templates.md) for:
- Project roadmap
- System architecture
- Business rules
- Attendance logic
- Development plan

## Monthly Workflow

See [MONTHLY_HR_REPORTING_WORKFLOW.md](MONTHLY_HR_REPORTING_WORKFLOW.md) and [HR_REPORTING_RULES_MASTER.md](HR_REPORTING_RULES_MASTER.md).
