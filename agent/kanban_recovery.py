"""Worker-side recovery snapshots; never execute a proposed lifecycle action."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import subprocess
import time

from agent.delegation_context import is_dispatcher_owned_worker_context

log = logging.getLogger(__name__)


def worker_identity():
    if not is_dispatcher_owned_worker_context():
        return None
    tid, rid = os.environ.get("HERMES_KANBAN_TASK"), os.environ.get("HERMES_KANBAN_RUN_ID")
    if not tid or not rid or not rid.isdecimal():
        return None
    return tid, int(rid)


def snapshot(previous=None):
    data = dict((previous or {}).get("checkpoint") or {})
    defaults = {
        "commits": [], "dirty_files": [], "evidence": [],
        "current_runtime": "unknown; reconcile before resume",
        "intended_runtime": "unknown; see task specification",
        "detached_operations": ["unknown; reconcile before resume"],
        "next_command": "unknown; owner must reconcile and choose the next scoped action",
        "rollback": "unknown; do not replay or roll back without inspecting live state",
        "blockers": [], "owner": "task requester/operator",
    }
    for key, value in defaults.items():
        data.setdefault(key, value)
    root = os.environ.get("HERMES_KANBAN_WORKSPACE")
    if root and Path(root).is_dir():
        paths = [Path(root)] + sorted(p for p in Path(root).iterdir() if p.is_dir())[:10]
        commits, dirty = [], []
        for path in paths:
            if not (path / ".git").exists():
                continue
            for args, dest in ((["rev-parse", "HEAD"], commits),
                               (["status", "--short", "--untracked-files=normal"], dirty)):
                try:
                    result = subprocess.run(
                        ["git", "-c", "core.fsmonitor=false", "-C", str(path), *args],
                        capture_output=True, text=True, timeout=3, check=False,
                        env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
                    )
                    dest.append({"path": str(path), "value": result.stdout[:4000], "exit_code": result.returncode})
                except (OSError, subprocess.TimeoutExpired):
                    dest.append({"path": str(path), "value": "unknown; inspection failed"})
        if commits:
            data["commits"], data["dirty_files"] = commits, dirty
    session = os.environ.get("HERMES_SESSION_ID")
    if session and {"session_id": session} not in data["evidence"]:
        data["evidence"] = [*data["evidence"], {"session_id": session}]
    return data


def checkpoint_from_env(*, classification=None, reason=None, pause=False, attention=False, command=None):
    identity = worker_identity()
    if identity is None:
        return None
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli.kanban_recovery import latest_checkpoint, save_checkpoint
    conn = kbc.connect()
    try:
        data = snapshot(latest_checkpoint(conn, identity[0]))
        if command:
            data["next_command"] = command
        if reason:
            data["blockers"] = [reason]
        return save_checkpoint(conn, identity[0], run_id=identity[1], data=data,
                               classification=classification, reason=reason, pause=pause, attention=attention)
    finally:
        conn.close()


def _denied_terminal(messages):
    """Only structured terminal results, not quoted prose from files/web pages."""
    if not messages or messages[-1].get("role") != "tool":
        return None
    start = next((i for i in range(len(messages) - 1, -1, -1)
                  if messages[i].get("role") == "assistant"), len(messages))
    calls = {}
    for msg in messages[start:]:
        if msg.get("role") == "assistant":
            for call in msg.get("tool_calls") or []:
                fn = call.get("function") or {}
                if fn.get("name") == "terminal":
                    calls[call.get("id")] = fn.get("arguments")
        if msg.get("role") != "tool" or msg.get("tool_call_id") not in calls:
            continue
        try:
            result = json.loads(msg.get("content") or "{}")
            args = json.loads(calls[msg["tool_call_id"]] or "{}")
        except (TypeError, ValueError):
            continue
        if isinstance(result, dict) and result.get("status") == "blocked" and result.get("exit_code") == -1:
            # Do not persist the tool's blanket-approval advice. Preserve the exact
            # denied action, not an executable recipe for circumventing the gate.
            return args.get("command") if isinstance(args, dict) else None
    return None


def before_iteration(agent, messages):
    """One automatic entry snapshot, budget warning, and denial stop per run."""
    identity = worker_identity()
    if identity is None or getattr(agent, "_interrupt_requested", False):
        return False
    try:
        from hermes_cli import kanban_db_connect as kbc
        from hermes_cli.kanban_recovery import latest_checkpoint
        with kbc.connect_closing() as conn:
            saved = latest_checkpoint(conn, identity[0])
            if saved and saved["run_id"] == identity[1] and saved["requires_reconciliation"]:
                return True
            task = conn.execute(
                "SELECT t.status, t.current_run_id, t.max_runtime_seconds, r.started_at "
                "FROM tasks t LEFT JOIN task_runs r ON r.id=t.current_run_id WHERE t.id=?",
                (identity[0],)).fetchone()
            if task is None or (task["status"] != "done" and (
                    task["status"] != "running" or task["current_run_id"] != identity[1])):
                agent._kanban_recovery_notice = "This worker no longer owns the running claim. Stopping without further execution; inspect the task's current outcome and owner before any continuation."
                return True
        command = _denied_terminal(messages)
        if command:
            receipt = checkpoint_from_env(
                classification="policy", reason="Terminal operation denied; use the normal operator approval channel, not a retry or alternate interface",
                command=command, pause=True,
            )
            return receipt is not None
        if not getattr(agent, "_recovery_entry_saved", False):
            agent._recovery_entry_saved = bool(checkpoint_from_env())
        budget = getattr(agent, "iteration_budget", None)
        near_turn = (budget is not None and 1 < budget.max_total < 2**63 - 1
                     and budget.used >= min((getattr(agent, "budget_warning_ratio", None) or .9) * budget.max_total,
                                            budget.max_total - 1))
        runtime, started = getattr(agent, "run_budget_seconds", None), getattr(agent, "_run_budget_started_at", None)
        near_time = bool(runtime and started and time.time() - started >= .8 * runtime)
        near_task_time = bool(task and task["max_runtime_seconds"] and task["started_at"]
                              and time.time() - task["started_at"] >= .8 * task["max_runtime_seconds"])
        if (near_turn or near_time or near_task_time) and not getattr(agent, "_recovery_budget_saved", False):
            agent._recovery_budget_saved = bool(checkpoint_from_env(
                classification="budget", reason="Approaching configured budget; save evidence and decompose remaining work before exhaustion",
                attention=True,
            ))
    except Exception:
        log.exception("Could not persist worker recovery checkpoint")
        raise
    return False
