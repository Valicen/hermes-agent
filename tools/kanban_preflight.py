"""Non-mutating readiness report. Never call an approval gate or execute input."""
from __future__ import annotations


def inspect_operations(operations):
    from tools.approval_detection import detect_dangerous_command
    from tools.approval_context import (
        _is_single_query_approval_context, _is_cron_approval_context,
        _is_unattended_platform_approval_context,
    )
    if not isinstance(operations, list) or not 1 <= len(operations) <= 20:
        raise ValueError("operations must contain 1–20 scoped lifecycle actions")
    unattended = (_is_single_query_approval_context() or _is_cron_approval_context()
                  or _is_unattended_platform_approval_context())
    reports = []
    for op in operations:
        if not isinstance(op, dict):
            raise ValueError("each operation must be an object")
        for key in ("command", "environment", "action", "authorization_provenance"):
            if not isinstance(op.get(key), str) or not op[key].strip():
                raise ValueError(f"operation requires {key}; unknown provenance is not authorization")
        for key in ("units", "required_capabilities", "dependencies"):
            if not isinstance(op.get(key), list):
                raise ValueError(f"operation requires a {key} list")
        dangerous, pattern, description = detect_dangerous_command(op["command"])
        reports.append({**op, "flagged": dangerous, "pattern": pattern, "description": description,
                        "readiness": "unknown", "permission_granted": False,
                        "interactive_approval_channel": "absent" if unattended else "unverified",
                        "next_action": "Operator verifies scoped executable permission and dependencies in the normal approval-capable context; then records postconditions. Do not repeat project authorization or bypass a tool denial."})
    return reports
