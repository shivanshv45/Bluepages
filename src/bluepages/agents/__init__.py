"""Fan-out (Layer 5).

One diff, N department reports. Each department agent receives only the findings
already routed to it and writes them up in its own vocabulary; clearance and
schedule impact are further consumers of the same diff.
"""

from bluepages.agents.consumers import (
    ClearanceFlag,
    ClearanceReport,
    FinanceLine,
    FinanceReport,
    ScheduleImpact,
    ScheduleReport,
    clearance_report,
    finance_report,
    schedule_report,
)
from bluepages.agents.departments import (
    SPECS,
    DepartmentNote,
    DepartmentReport,
    DepartmentSpec,
    FanOut,
    Report,
    fan_out,
    write_report,
)

__all__ = [
    "SPECS",
    "ClearanceFlag",
    "ClearanceReport",
    "DepartmentNote",
    "DepartmentReport",
    "DepartmentSpec",
    "FanOut",
    "FinanceLine",
    "FinanceReport",
    "Report",
    "ScheduleImpact",
    "ScheduleReport",
    "clearance_report",
    "fan_out",
    "finance_report",
    "schedule_report",
    "write_report",
]
