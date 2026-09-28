"""Turn-end guard for kanban workers, which must persist a lifecycle handoff.
Some models narrate the next step and stop with no tool calls;
Hermes treats that as a clean exit → ``rc=0`` → dispatcher ``protocol_violation``.
Policy-only: return a bounded synthetic nudge so the loop continues instead of exiting.
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
from typing import Any, Iterable, Optional

from agent.delegation_context import is_dispatcher_owned_worker_context


_TERMINAL_KANBAN_TOOLS = frozenset({
    "kanban_complete", "kanban_block", "kanban_request_review",
    "kanban_request_changes", "kanban_checkpoint",
})

_DEFAULT_MAX_ATTEMPTS = 2


def kanban_stop_nudge_enabled() -> bool:
    """On when ``HERMES_KANBAN_TASK`` is set for the dispatcher-owned worker, unless
    ``HERMES_KANBAN_STOP_NUDGE`` disables it. In-process delegate_task children and cron runs
    inherit the env var but own no board task and carry no kanban toolset."""
    if (os.environ.get("HERMES_KANBAN_STOP_NUDGE") or "").strip().lower() in {"0", "false", "no", "off"}:
        return False
    return bool((os.environ.get("HERMES_KANBAN_TASK") or "").strip()) and is_dispatcher_owned_worker_context()


def _tool_call_name(tc: Any) -> str:
    """Tool name from a dict or object tool call (``function.name`` first, then ``name``)."""
    if isinstance(tc, dict):
        fn = tc.get("function")
        return str((fn.get("name") if isinstance(fn, dict) else tc.get("name")) or "")
    fn = getattr(tc, "function", None)
    return str((getattr(fn, "name", "") if fn is not None else getattr(tc, "name", "")) or "")


def session_called_kanban_terminal(messages: Iterable[dict] | None) -> bool:
    """Successful terminal results only; an attempted/refused call is not a handoff."""
    calls = {}
    tid = os.environ.get("HERMES_KANBAN_TASK", "")
    for msg in filter(lambda m: isinstance(m, dict), messages or ()):
        if msg.get("role") == "assistant":
            for tc in msg.get("tool_calls") or []:
                if isinstance(tc, dict):
                    calls[tc.get("id")] = _tool_call_name(tc)
        if msg.get("role") != "tool":
            continue
        name = calls.get(msg.get("tool_call_id")) or msg.get("name")
        if name not in _TERMINAL_KANBAN_TOOLS:
            continue
        try:
            result = json.loads(msg.get("content", ""))
        except (TypeError, ValueError):
            continue
        if (not isinstance(result, dict) or result.get("ok") is not True
                or result.get("error") or (result.get("task_id") and result["task_id"] != tid)):
            continue
        if name == "kanban_checkpoint":
            cp = result.get("checkpoint")
            if not isinstance(cp, dict) or cp.get("requires_reconciliation") is not True:
                continue
        if result.get("status") in {"done", "blocked", "review", "ready", "todo", "triage"}:
            return True
    return False


def _owns_open_run(task_id: str) -> Optional[bool]:
    """Read only: never initialize/repair a board from a turn-end advisory.

    False includes a closed run or successor ownership, even if the task is running
    again. None means unknown, not permission to transition somebody else's run.
    """
    raw = os.environ.get("HERMES_KANBAN_RUN_ID")
    if not raw:
        return None
    try:
        from hermes_cli.kanban_db import kanban_db_path
        from hermes_cli.sqlite_safe_read import connect_tracked
        run_id = int(raw)
        uri = kanban_db_path().resolve().as_uri() + "?mode=ro"
        with contextlib.closing(connect_tracked(uri, uri=True, timeout=1)) as conn:
            row = conn.execute(
                "SELECT t.status, t.current_run_id, r.ended_at, r.id FROM tasks t "
                "LEFT JOIN task_runs r ON r.id=? AND r.task_id=t.id WHERE t.id=?",
                (run_id, task_id),
            ).fetchone()
        if row is None or row[3] is None:
            return None
        return row[0] == "running" and row[1] == run_id and row[2] is None
    except (OSError, ValueError, sqlite3.Error):
        return None


def build_kanban_stop_nudge(
    *,
    messages: Iterable[dict] | None = None,
    attempts: int = 0,
    max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
    task_id: Optional[str] = None,
) -> Optional[str]:
    """Synthetic follow-up when a kanban worker exits without a terminal tool; ``None`` when
    the guard should not fire (not a worker, closed/lost run, successful handoff, nudge cap)."""
    if (
        not kanban_stop_nudge_enabled()
        or attempts >= max_attempts
    ):
        return None

    tid = (task_id or os.environ.get("HERMES_KANBAN_TASK") or "").strip() or "this task"
    owns_run = _owns_open_run(tid)
    if owns_run is False or (owns_run is None and session_called_kanban_terminal(messages)):
        return None
    state = (f"Task `{tid}` is still `running` under your exact run. " if owns_run else
             f"Task `{tid}` has no verified terminal handoff in this context. "
             "Read the board and your exact run first; do not transition a successor's task. ")
    return (
        "[System: You are a Hermes kanban worker. A plain-text reply is NOT a "
        "terminal state for the board.\n\n"
        f"{state}Ending an owned running task without a board tool "
        "causes a protocol violation (clean exit with no "
        "successful completion, block, review handoff or checkpoint pause).\n\n"
        "Do this immediately in your next response — do not narrate intent:\n"
        "1. Confirm exact run ownership. If your run is closed or replaced, preserve that handoff and stop.\n"
        "2. Only if you still own the open run: call `kanban_complete(summary=..., artifacts=[...])` "
        "when done, `kanban_request_review(summary=...)` for independent review, "
        "`kanban_request_changes(reason=...)` for review rework, or `kanban_block(reason=...)` "
        "if blocked. A successful checkpoint pause is also terminal.\n\n"
        "Never end a turn with only a promise of future action. Repeated "
        "protocol violations will block this task and require manual intervention.]"
    )


__all__ = ["build_kanban_stop_nudge", "kanban_stop_nudge_enabled", "session_called_kanban_terminal"]
