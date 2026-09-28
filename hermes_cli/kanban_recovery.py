"""Durable, run-fenced recovery handoffs. No command execution or consent grants.

Events are the ledger (no second store/daemon). Existing notification subscriptions
own delivery. Reconciliation is an operator attestation, never executable permission.
"""
from __future__ import annotations

import json
import time

from hermes_cli import kanban_db as kb

CLASSES = frozenset({"operator_only", "auth", "policy", "transient", "budget", "worker_death"})
FIELDS = ("commits", "dirty_files", "evidence", "current_runtime", "intended_runtime",
          "detached_operations", "next_command", "rollback", "blockers", "owner")


def latest_checkpoint(conn, task_id):
    row = conn.execute(
        "SELECT id, run_id, payload FROM task_events WHERE task_id = ? "
        "AND kind IN ('checkpoint_saved', 'checkpoint_attention') ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if row is None:
        return None
    return {"event_id": row["id"], "run_id": row["run_id"], **json.loads(row["payload"])}


def _clean(value):
    # JSON validation also prevents arbitrary object serialization into durable state.
    text = json.dumps(kb.redact_review_value(value), sort_keys=True, ensure_ascii=False)
    if len(text.encode()) > 32768:
        raise ValueError("checkpoint exceeds 32 KiB; attach evidence and supply references")
    return json.loads(text)


def validate_checkpoint(data):
    if not isinstance(data, dict):
        raise ValueError("checkpoint must be an object")
    missing = [key for key in FIELDS if key not in data or data[key] is None]
    if missing:
        raise ValueError("checkpoint missing fields: " + ", ".join(missing))
    for key in ("current_runtime", "intended_runtime", "next_command", "rollback", "owner"):
        if not isinstance(data[key], str) or not data[key].strip():
            raise ValueError(f"checkpoint {key} must be nonblank text (unknown is acceptable)")
    for key in ("commits", "dirty_files", "evidence", "detached_operations", "blockers"):
        if not isinstance(data[key], list):
            raise ValueError(f"checkpoint {key} must be a list; unknown must be explicit")
    return _clean(data)


def save_checkpoint(conn, task_id, *, run_id, data, classification=None, reason=None,
                    pause=False, attention=False):
    """Atomic snapshot + optional safe stop, only for the current open running claim.

    Duplicate calls return the existing event; a stale/cancelled/reviewed worker cannot
    add progress, consume another run's budget or undo an operator's transition.
    """
    data = validate_checkpoint(data)
    if classification is not None and classification not in CLASSES:
        raise ValueError("unknown recovery classification")
    if pause and (not classification or not reason):
        raise ValueError("a pause requires classification and actionable reason")
    payload = _clean({"checkpoint": data, "classification": classification,
                      "reason": reason, "requires_reconciliation": bool(pause)})
    with kb.write_txn(conn):
        row = conn.execute(
            "SELECT t.status, t.current_run_id, r.ended_at FROM tasks t "
            "LEFT JOIN task_runs r ON r.id = t.current_run_id WHERE t.id = ?", (task_id,),
        ).fetchone()
        if (run_id is None or row is None or row["status"] != "running"
                or row["current_run_id"] != run_id or row["ended_at"] is not None):
            return None
        previous = latest_checkpoint(conn, task_id)
        if previous and previous["run_id"] == run_id and all(
            previous.get(k) == v for k, v in payload.items()
        ):
            return previous
        kind = "checkpoint_attention" if attention and not pause else "checkpoint_saved"
        kb._append_event(conn, task_id, kind, payload, run_id=run_id)
        event_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        if pause:
            source_status = kb._retry_status_for_run(conn, task_id)
            block_kind = {"transient": "transient", "budget": "needs_input"}.get(str(classification), "capability")
            summary = (f"{classification}: owner={data['owner']}; progress saved in checkpoint {event_id}; "
                       f"{reason}; next={data['next_command']}; "
                       "reconcile live runtime and detached actions before resuming")
            conn.execute(
                "UPDATE tasks SET status='blocked', block_kind=?, claim_lock=NULL, "
                "claim_expires=NULL, worker_pid=NULL, worker_started_at=NULL, "
                "last_failure_error=? WHERE id=?", (block_kind, summary[:500], task_id),
            )
            kb._end_run(conn, task_id, outcome="blocked", status="blocked", summary=summary,
                        metadata={"recovery": payload, "checkpoint_event_id": event_id})
            kb._append_event(conn, task_id, "blocked", {
                "reason": summary, "kind": block_kind, "classification": classification,
                "checkpoint_event_id": event_id, "source_status": source_status,
            }, run_id=run_id)
        return {"event_id": event_id, "run_id": run_id, **payload}


def checkpoint_after_worker_loss(conn, task_id, classification, reason):
    """Dispatcher transaction only, after it has established worker loss/timeout.

    Retain the last checkpoint verbatim; never infer live state from dispatcher
    reachability. Legacy workers without a checkpoint retain bounded retries.
    """
    previous = latest_checkpoint(conn, task_id)
    if not previous or previous["run_id"] != kb._current_run_id(conn, task_id):
        return False
    data = previous["checkpoint"]
    payload = {"checkpoint": data, "classification": classification, "reason": reason,
               "requires_reconciliation": True}
    kb._append_event(conn, task_id, "checkpoint_saved", payload, run_id=previous["run_id"])
    conn.execute("UPDATE tasks SET block_kind='capability', last_failure_error=? WHERE id=?",
                 (f"{classification}: {reason}; progress saved; owner={data['owner']}; reconcile detached operations", task_id))
    return True


def pending_recovery(conn, task_id):
    latest = latest_checkpoint(conn, task_id)
    if not latest or not latest.get("requires_reconciliation"):
        return None
    resolved = conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind='recovery_reconciled' AND id>? LIMIT 1",
                            (task_id, latest["event_id"])).fetchone()
    return None if resolved else latest


def reconcile_before_unblock(conn, task_id, receipt):
    """Inside unblock's transaction. Validate a fresh receipt bound to the exact stop.

    This is evidence/coordination, NOT an approval token. Neither this function nor
    the receipt author executes lifecycle commands. Tool gates still apply on resume.
    """
    latest = pending_recovery(conn, task_id)
    if not latest:
        return True
    if not isinstance(receipt, dict) or receipt.get("checkpoint_event_id") != latest["event_id"]:
        return False
    fields = ("operator", "evidence", "runtime_observed", "detached_observed",
              "postconditions", "next_action", "authorization_provenance")
    if any(not isinstance(receipt.get(k), str) or not receipt[k].strip() for k in fields):
        return False
    stamp = receipt.get("observed_at")
    if isinstance(stamp, bool) or not isinstance(stamp, (int, float)) or not 0 <= time.time() - stamp <= 900:
        return False
    if receipt.get("in_flight") is not False or receipt.get("postconditions_passed") is not True:
        return False
    kb._append_event(conn, task_id, "recovery_reconciled", _clean(receipt))
    return True


def recovery_context(conn, task_id):
    checkpoint = latest_checkpoint(conn, task_id)
    if not checkpoint:
        return ""
    return ("\n## Saved recovery checkpoint (not execution permission)\n"
            + json.dumps(checkpoint, ensure_ascii=False, indent=2)
            + "\nReconcile live state, claims and detached actions before continuing. "
              "Do not replay a completed action or retry a denied action via another interface.\n")
