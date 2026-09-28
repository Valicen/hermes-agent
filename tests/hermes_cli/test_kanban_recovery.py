"""Real isolated board/approval/recovery paths for run386/run390 failure classes."""
import json
import logging
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc, kanban_db_dispatch as kbd
from hermes_cli.kanban_recovery import latest_checkpoint, save_checkpoint
from agent.kanban_recovery import before_iteration, snapshot
from agent.turn_finalizer import _record_kanban_budget_exhausted
from tools.registry import registry
import tools.kanban_tools  # registers the actual model-facing handlers


@pytest.fixture
def worker(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(home / "board.db"))
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setenv("HERMES_SINGLE_QUERY_SESSION", "1")
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(tmp_path / "work"))
    for key in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_YOLO_MODE", "HERMES_GATEWAY_SESSION"):
        monkeypatch.delenv(key, raising=False)
    (home / "config.yaml").write_text("approvals:\n  mode: manual\n  single_query_mode: deny\n")
    kb.init_db()
    conn = kbc.connect()
    tid = kb.create_task(conn, title="isolated recovery", assignee="worker")
    kb.claim_task(conn, tid)
    rid = kb.get_task(conn, tid).current_run_id
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(rid))
    yield conn, tid, rid
    conn.close()


def data():
    return {**snapshot(), "owner": "operator", "next_command": "inspect fixture postconditions",
            "detached_operations": [], "evidence": ["fixture receipt"], "rollback": "preserve fixture state"}


def reconcile(cp):
    return {"checkpoint_event_id": cp["event_id"], "observed_at": time.time(), "operator": "fixture-operator",
            "evidence": "fixture state readback", "runtime_observed": "inactive",
            "detached_observed": "none", "postconditions": "inactive verified", "next_action": "continue tests",
            "authorization_provenance": "same original task authorization", "in_flight": False,
            "postconditions_passed": True}


def test_preflight_in_worker_context_never_grants_permission(worker):
    conn, tid, rid = worker
    op = {"command": "systemctl --user stop recovery-fixture.service", "action": "stop",
          "units": ["recovery-fixture.service"], "environment": "isolated-fixture",
          "authorization_provenance": "existing task authorization", "required_capabilities": ["lifecycle"],
          "dependencies": ["fixture only"]}
    out = json.loads(registry.dispatch("kanban_checkpoint", {"checkpoint": data(), "operations": [op]}))
    assert out["ok"], out
    report = out["checkpoint"]["checkpoint"]["preflight"][0]
    assert report["flagged"] and report["readiness"] == "unknown"
    assert report["interactive_approval_channel"] == "absent" and not report["permission_granted"]
    assert kb.get_task(conn, tid).status == "blocked"
    assert not kb.unblock_task(conn, tid)
    assert kb.unblock_task(conn, tid, recovery=reconcile(out["checkpoint"]))
    assert kb.get_task(conn, tid).status == "ready"


@pytest.mark.parametrize("classification", ["operator_only", "auth", "policy", "transient", "budget"])
def test_checkpoint_pause_is_atomic_deduplicated_and_fenced(worker, classification):
    conn, tid, rid = worker
    out = save_checkpoint(conn, tid, run_id=rid, data=data(), classification=classification,
                          reason="missing credential" if classification == "auth" else "fixture blocker", pause=True)
    assert out and kb.get_task(conn, tid).status == "blocked"
    count = len(kb.list_events(conn, tid))
    assert save_checkpoint(conn, tid, run_id=rid, data=data(), classification=classification,
                           reason="same", pause=True) is None
    assert len(kb.list_events(conn, tid)) == count
    assert not kb.unblock_task(conn, tid, recovery={**reconcile(out), "in_flight": True})
    assert not kb.unblock_task(conn, tid, recovery={**reconcile(out), "observed_at": 0})
    assert not kb.unblock_task(conn, tid, recovery={**reconcile(out), "checkpoint_event_id": -1})
    assert kb.unblock_task(conn, tid, recovery=reconcile(out))
    kb.claim_task(conn, tid)
    new_rid = kb.get_task(conn, tid).current_run_id
    assert new_rid != rid
    assert save_checkpoint(conn, tid, run_id=rid, data=data()) is None
    assert before_iteration(SimpleNamespace(), [])
    _record_kanban_budget_exhausted(tid, 150, 150, logging.getLogger(__name__))
    assert kb.get_task(conn, tid).current_run_id == new_rid
    assert kb.complete_task(conn, tid, summary="fixture verified", expected_run_id=new_rid)
    assert kb.get_task(conn, tid).status == "done"


def test_denial_after_checkpoint_uses_real_gate_and_preserves_progress(worker):
    from tools.approval import check_dangerous_command
    conn, tid, rid = worker
    saved = data()
    saved["commits"] = ["fixture-commit"]
    save_checkpoint(conn, tid, run_id=rid, data=saved)
    command = "systemctl --user stop recovery-fixture.service"
    result = check_dangerous_command(command, "local")
    assert result["approved"] is False
    # Terminal's actual envelope, containing the real gate outcome; never run the command.
    messages = [{"role": "assistant", "tool_calls": [{"id": "deny", "function": {
        "name": "terminal", "arguments": json.dumps({"command": command})}}]},
        {"role": "tool", "tool_call_id": "deny", "content": json.dumps({
            "status": "blocked", "exit_code": -1, "error": result["message"],
            "pattern_key": result["pattern_key"]})}]
    assert before_iteration(SimpleNamespace(), messages)
    cp = latest_checkpoint(conn, tid)
    assert cp["checkpoint"]["commits"] == ["fixture-commit"]
    assert cp["checkpoint"]["next_command"] == command
    assert kb.get_task(conn, tid).status == "blocked"
    assert "Saved recovery checkpoint" in kb.build_worker_context(conn, tid)


@pytest.mark.parametrize("budget_type", ["turn", "runtime"])
def test_near_budget_saves_once_then_exhaustion_pauses(worker, budget_type):
    conn, tid, rid = worker
    agent = SimpleNamespace(iteration_budget=SimpleNamespace(used=135 if budget_type == "turn" else 1, max_total=150),
                            run_budget_seconds=100 if budget_type == "runtime" else None,
                            _run_budget_started_at=time.time() - 90)
    assert not before_iteration(agent, [])
    assert not before_iteration(agent, [])
    assert len([e for e in kb.list_events(conn, tid) if e.kind == "checkpoint_attention"]) == 1
    assert kb.get_task(conn, tid).status == "running"
    _record_kanban_budget_exhausted(tid, 150, 150, logging.getLogger(__name__))
    cp = latest_checkpoint(conn, tid)
    assert cp["classification"] == "budget" and cp["requires_reconciliation"]
    assert kb.get_task(conn, tid).status == "blocked"
    assert not [e for e in kb.list_events(conn, tid) if e.kind == "gave_up"]


def test_cancelled_worker_cannot_checkpoint_or_consume_budget(worker):
    conn, tid, rid = worker
    kb.block_task(conn, tid, reason="operator cancelled", expected_run_id=rid)
    count = len(kb.list_events(conn, tid))
    assert not before_iteration(SimpleNamespace(_interrupt_requested=True), [])
    _record_kanban_budget_exhausted(tid, 150, 150, logging.getLogger(__name__))
    assert len(kb.list_events(conn, tid)) == count


@pytest.mark.parametrize("killed", [False, True])
def test_worker_death_preserves_checkpoint_and_does_not_respawn(worker, killed):
    conn, tid, rid = worker
    save_checkpoint(conn, tid, run_id=rid, data=data())
    process = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()" if killed else "pass"], stdin=subprocess.PIPE)
    kbd._set_worker_pid(conn, tid, process.pid)
    if killed:
        process.kill()
    process.wait(timeout=10)
    process.stdin.close()
    assert tid in kbd.detect_crashed_workers(conn)
    assert kb.get_task(conn, tid).status == "blocked"
    assert latest_checkpoint(conn, tid)["classification"] == "worker_death"
    assert kb.claim_task(conn, tid) is None
    assert not kb.unblock_task(conn, tid)


def test_git_snapshot_reads_commit_and_dirty_paths_only(worker, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                    "commit", "--allow-empty", "-m", "fixture"], check=True, capture_output=True)
    (repo / "work.txt").write_text("uncommitted fixture")
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(repo))
    cp = snapshot()
    assert cp["commits"][0]["exit_code"] == 0
    assert "work.txt" in cp["dirty_files"][0]["value"]
    assert "uncommitted fixture" not in json.dumps(cp)


def test_runtime_watchdog_stops_at_checkpoint_without_retry(worker):
    conn, tid, rid = worker
    save_checkpoint(conn, tid, run_id=rid, data=data())
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    kbd._set_worker_pid(conn, tid, process.pid)
    process.wait(timeout=10)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET max_runtime_seconds=1 WHERE id=?", (tid,))
        conn.execute("UPDATE task_runs SET started_at=? WHERE id=?", (int(time.time()) - 10, rid))
    assert tid in kbd.enforce_max_runtime(conn, signal_fn=lambda *_: None)
    assert kb.get_task(conn, tid).status == "blocked"
    assert latest_checkpoint(conn, tid)["classification"] == "budget"
    assert not kb.unblock_task(conn, tid)


def test_changes_requested_and_review_phase_are_preserved(worker):
    conn, tid, rid = worker
    assert kb.request_review(conn, tid, summary="fixture implementation", reviewer="reviewer", expected_run_id=rid)
    assert save_checkpoint(conn, tid, run_id=rid, data=data()) is None
    assert kb.claim_review_task(conn, tid) is not None
    review_run = kb.get_task(conn, tid).current_run_id
    cp = save_checkpoint(conn, tid, run_id=review_run, data=data(), classification="operator_only",
                         reason="review dependency", pause=True)
    assert kb.unblock_task(conn, tid, recovery=reconcile(cp))
    assert kb.get_task(conn, tid).status == "review"
    assert kb.claim_review_task(conn, tid) is not None
    review_run = kb.get_task(conn, tid).current_run_id
    assert kb.request_changes(conn, tid, reason="fixture rework", expected_run_id=review_run)
    assert save_checkpoint(conn, tid, run_id=review_run, data=data()) is None
    assert kb.get_task(conn, tid).status == "ready"


def test_historical_denial_and_untrusted_prose_do_not_stop_new_work(worker):
    conn, tid, rid = worker
    content = json.dumps({"status": "blocked", "exit_code": -1})
    history = [{"role": "assistant", "tool_calls": [{"id": "old", "function": {
        "name": "terminal", "arguments": json.dumps({"command": "fixture"})}}]},
        {"role": "tool", "tool_call_id": "old", "content": content},
        {"role": "assistant", "tool_calls": [{"id": "new", "function": {"name": "read_file"}}]},
        {"role": "tool", "tool_call_id": "new", "content": content}]
    assert not before_iteration(SimpleNamespace(), history)
    assert kb.get_task(conn, tid).status == "running"


def test_missing_authorization_is_not_inferred_from_reachability(worker):
    out = json.loads(registry.dispatch("kanban_checkpoint", {"checkpoint": data(), "operations": [{
        "command": "fixture", "environment": "fixture", "action": "stop", "units": ["fixture"],
        "required_capabilities": ["reachable"], "dependencies": [], "authorization_provenance": ""}]}))
    assert "error" in out
    conn, tid, rid = worker
    assert latest_checkpoint(conn, tid) is None


def test_status_edit_cannot_bypass_recovery_claim_fence(worker):
    conn, tid, rid = worker
    save_checkpoint(conn, tid, run_id=rid, data=data(), classification="policy", reason="fixture denied", pause=True)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    assert kbd.check_respawn_guard(conn, tid) == "recovery_reconciliation_required"
    assert kb.claim_task(conn, tid) is None


def test_task_runtime_warning_does_not_require_agent_runtime_configuration(worker):
    conn, tid, rid = worker
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET max_runtime_seconds=100 WHERE id=?", (tid,))
        conn.execute("UPDATE task_runs SET started_at=? WHERE id=?", (int(time.time()) - 85, rid))
    assert not before_iteration(SimpleNamespace(), [])
    assert len([e for e in kb.list_events(conn, tid) if e.kind == "checkpoint_attention"]) == 1
    assert kb.get_task(conn, tid).status == "running"


# Talaria row 38 (2026-09-28): the board shows one human sentence; the machine
# record (classification, owner, checkpoint id, next command) stays on the run
# summary and the event payload. David read the row-37 summary string as the
# block reason and could not tell why the card was blocked.
def test_pause_writes_a_human_reason_and_keeps_the_machine_record(worker):
    from hermes_cli.kanban_recovery import checkpoint_after_worker_loss, human_block_reason
    conn, tid, rid = worker
    out = save_checkpoint(conn, tid, run_id=rid, data=data(), classification="operator_only",
                          reason="the deploy step needs an operator to run it", pause=True)
    task = kb.get_task(conn, tid)
    assert task.last_failure_error == (
        "Paused for an operator's approval: the deploy step needs an operator to run it. "
        "Progress is saved; nothing was lost. Owner: operator.")
    assert "next=" not in task.last_failure_error and "operator_only:" not in task.last_failure_error
    blocked = [e for e in kb.list_events(conn, tid) if e.kind == "blocked"][-1]
    payload = json.loads(blocked.payload) if isinstance(blocked.payload, str) else blocked.payload
    assert payload["reason"] == task.last_failure_error
    assert payload["next_command"] == "inspect fixture postconditions" and payload["owner"] == "operator"
    assert payload["recovery_summary"].startswith(f"operator_only: owner=operator; progress saved in checkpoint {out['event_id']};")
    run = kb.latest_run(conn, tid)
    assert run.summary == payload["recovery_summary"]
    # Every class reads as a sentence, never as a classifier token.
    for cls in ("auth", "policy", "transient", "budget", "worker_death", "operator_only"):
        text = human_block_reason(cls, "fixture reason.")
        assert text.startswith("Paused") and text.endswith("Progress is saved; nothing was lost.")
        assert human_block_reason(cls, "fixture reason.", "unknown") == text
        assert f"{cls}:" not in text
    assert human_block_reason("auth", "") == "Paused for missing access or credentials. Progress is saved; nothing was lost."
    # Dispatcher-side worker loss: same sentence shape on the task row.
    assert kb.unblock_task(conn, tid, recovery=reconcile(out))
    kb.claim_task(conn, tid)
    new_rid = kb.get_task(conn, tid).current_run_id
    save_checkpoint(conn, tid, run_id=new_rid, data=data())
    assert checkpoint_after_worker_loss(conn, tid, "worker_death", "no heartbeat for 10 minutes", notify=True)
    assert kb.get_task(conn, tid).last_failure_error == (
        "Paused because the worker died: no heartbeat for 10 minutes. Progress is saved; nothing was lost. Owner: operator.")


# --- Talaria row 39 ---------------------------------------------------------------------------

def test_worker_session_is_stamped_once_on_first_iteration(worker, monkeypatch):
    from hermes_cli.kanban_recovery import stamp_worker_session
    conn, tid, rid = worker
    monkeypatch.setenv("HERMES_SESSION_ID", "20260928_000000_fixture")
    agent = SimpleNamespace(session_id="20260928_000000_agent")
    assert not before_iteration(agent, [])
    run = next(r for r in kb.list_runs(conn, tid) if r.id == rid)
    assert run.session_id == "20260928_000000_agent"
    # write-once, fenced: a later stamp (or a foreign run id) never overwrites it
    assert not stamp_worker_session(conn, tid, rid, "other")
    assert not stamp_worker_session(conn, tid, rid + 1, "other")
    assert next(r for r in kb.list_runs(conn, tid) if r.id == rid).session_id == "20260928_000000_agent"
    # the model-facing show and the dashboard-facing dataclass both expose it
    shown = json.loads(registry.dispatch("kanban_show", {}))
    assert shown["runs"][-1]["session_id"] == "20260928_000000_agent"


def test_scanner_denial_without_pattern_key_does_not_pause(worker):
    conn, tid, rid = worker
    messages = [{"role": "assistant", "tool_calls": [{"id": "scan", "function": {
        "name": "terminal", "arguments": json.dumps({"command": "node -e 'fixture'"})}}]},
        {"role": "tool", "tool_call_id": "scan", "content": json.dumps({
            "status": "blocked", "exit_code": -1,
            "error": "BLOCKED: Security scan — [HIGH] Inline interpreter with suspicious payload"})}]
    assert not before_iteration(SimpleNamespace(), messages)
    assert kb.get_task(conn, tid).status == "running"
    assert latest_checkpoint(conn, tid) is None or not latest_checkpoint(conn, tid)["requires_reconciliation"]


def test_repeated_same_kind_pause_parks_in_triage_via_loop_breaker(worker):
    conn, tid, rid = worker
    first = save_checkpoint(conn, tid, run_id=rid, data=data(), classification="policy",
                            reason="systemctl stop denied", pause=True)
    task = kb.get_task(conn, tid)
    assert task.status == "blocked" and task.block_kind == "capability" and task.block_recurrences == 1
    assert kb.unblock_task(conn, tid, recovery=reconcile(first))
    kb.claim_task(conn, tid)
    rid2 = kb.get_task(conn, tid).current_run_id
    second = save_checkpoint(conn, tid, run_id=rid2, data=data(), classification="operator_only",
                             reason="lifecycle readiness unknown", pause=True)
    assert second is not None
    task = kb.get_task(conn, tid)
    assert task.status == "triage" and task.block_recurrences == kb.BLOCK_RECURRENCE_LIMIT
    kinds = [e.kind for e in kb.list_events(conn, tid)]
    assert "block_loop_detected" in kinds and "blocked" not in kinds[kinds.index("block_loop_detected"):]
    comments = kb.list_comments(conn, tid)
    assert comments[-1].author == "loop-breaker" and "recovery pauses" in comments[-1].body
    # a different kind after the loop restarts the count, as for block_task
    assert kb.get_task(conn, tid).current_run_id is None
