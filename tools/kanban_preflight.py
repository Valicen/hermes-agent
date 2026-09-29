"""Non-mutating readiness report. Never call an approval gate or execute input."""
from __future__ import annotations


def inspect_operations(operations):
    from tools.approval_detection import detect_dangerous_command
    from tools.approval_floors import _command_matches_permanent_allowlist, _match_user_deny_rule
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
        # Talaria row 43 (2026-09-29): a flagged command that the profile's permanent
        # command_allowlist already pre-approves (and no approvals.deny glob blocks) is
        # NOT an operator handoff — the worker runs it itself, unattended contexts included.
        # The gate that executes the command still applies; this only stops the worker
        # from parking the card on David for something it is allowed to do.
        denied = _match_user_deny_rule(op["command"])
        preapproved = (not dangerous or _command_matches_permanent_allowlist(op["command"])) and not denied
        if denied:
            readiness, next_action = "denied", (
                f"Do not run: approvals.deny rule {denied!r} blocks this command in this profile "
                "(production guard). Hand off to an operator or choose a permitted path.")
        elif preapproved:
            readiness, next_action = "pre-approved", (
                "Run it now in this worker as ONE plain terminal call (no ; && | chaining, no path prefix "
                "other than the allowlisted form): the command is pre-approved by command_allowlist "
                "or not flagged. Record postconditions in the next checkpoint.")
        else:
            readiness, next_action = "unknown", None
        reports.append({**op, "flagged": dangerous, "pattern": pattern, "description": description,
                        "readiness": readiness, "permission_granted": preapproved,
                        "deny_rule": denied or "",
                        "interactive_approval_channel": "absent" if unattended else "unverified",
                        "next_action": next_action or "Operator verifies scoped executable permission and dependencies in the normal approval-capable context; then records postconditions. Do not repeat project authorization or bypass a tool denial."})
    return reports
