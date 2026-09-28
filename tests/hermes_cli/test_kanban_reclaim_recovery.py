"""All reclaim entry points preserve checkpoints, phase and exact-run ownership."""
import subprocess
import sys
import time

import pytest

from hermes_cli import kanban_db as kb, kanban_db_dispatch as kbd
from hermes_cli.kanban_recovery import pending_recovery, save_checkpoint
from tests.hermes_cli.test_kanban_recovery import worker, data, reconcile


def expire(conn, tid, rid, path):
    with kb.write_txn(conn):
        conn.execute("UPDATE task_runs SET started_at=? WHERE id=?", (int(time.time()) - 20000, rid))
        conn.execute("UPDATE tasks SET last_heartbeat_at=NULL, claim_expires=? WHERE id=?",
                     (None if path == "orphan" else int(time.time()) - 1, tid))


def reclaim(conn, path):
    if path == "heartbeat":
        return kbd.detect_stale_running(conn, stale_timeout_seconds=100)
    if path == "orphan":
        return kbd.reconcile_orphaned_running(conn)
    return kb.release_stale_claims(conn)


@pytest.mark.parametrize("path", ["heartbeat", "orphan", "ttl"])
@pytest.mark.parametrize("phase", ["ready", "review"])
@pytest.mark.parametrize("checkpointed", [True, False])
def test_reclaim_resume_contract(worker, path, phase, checkpointed):
    conn, tid, rid = worker
    if phase == "review":
        assert kb.request_review(conn, tid, summary="fixture", reviewer="reviewer", expected_run_id=rid)
        assert kb.claim_review_task(conn, tid)
        rid = kb.get_task(conn, tid).current_run_id
    if checkpointed:
        assert save_checkpoint(conn, tid, run_id=rid, data=data())
    expire(conn, tid, rid, path)
    assert reclaim(conn, path)
    claim = kb.claim_review_task if phase == "review" else kb.claim_task
    if checkpointed:
        assert kb.get_task(conn, tid).status == "blocked"
        cp = pending_recovery(conn, tid)
        assert cp and cp["run_id"] == rid
        assert claim(conn, tid) is None
        assert not kb.unblock_task(conn, tid)
        assert not reclaim(conn, path)
        assert len([e for e in kb.list_events(conn, tid) if e.kind == "blocked"]) == 1
        assert kb.unblock_task(conn, tid, recovery=reconcile(cp))
    else:
        assert pending_recovery(conn, tid) is None
    assert kb.get_task(conn, tid).status == phase
    assert claim(conn, tid)
    assert kb.get_task(conn, tid).current_run_id != rid


@pytest.mark.parametrize("path", ["heartbeat", "orphan", "ttl"])
@pytest.mark.parametrize("transition", ["cancel", "review", "replacement", "changes_requested"])
def test_reclaim_does_not_mutate_a_concurrent_handoff(worker, monkeypatch, path, transition):
    conn, tid, rid = worker
    if transition == "changes_requested":
        assert kb.request_review(conn, tid, summary="fixture", reviewer="reviewer", expected_run_id=rid)
        assert kb.claim_review_task(conn, tid)
        rid = kb.get_task(conn, tid).current_run_id
    assert save_checkpoint(conn, tid, run_id=rid, data=data())
    expire(conn, tid, rid, path)
    # Interleave after selection and before the ownership-checked transaction.
    # No OS signalling: the fixture has no worker, except the orphan's synthetic PID.
    def race(*args, **kwargs):
        if transition == "review":
            assert kb.request_review(conn, tid, summary="verified fixture", reviewer="reviewer", expected_run_id=rid)
            assert kb.claim_review_task(conn, tid)
        elif transition == "changes_requested":
            assert kb.request_changes(conn, tid, reason="fixture rework", expected_run_id=rid)
        else:
            assert kb.block_task(conn, tid, reason="operator cancelled", expected_run_id=rid)
            if transition == "replacement":
                assert kb.unblock_task(conn, tid)
                assert kb.claim_task(conn, tid)
                # Same dispatcher lock and all nullable values; only run identity differs.
                new = kb.get_task(conn, tid).current_run_id
                expire(conn, tid, new, path)
                assert save_checkpoint(conn, tid, run_id=new, data=data())
        race.after = kb.get_task(conn, tid)
        race.events = len(kb.list_events(conn, tid))
        return False if path == "orphan" else {}
    if path == "orphan":
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET worker_pid=99999999 WHERE id=?", (tid,))
        monkeypatch.setattr(kbd, "_worker_alive", race)
    else:
        monkeypatch.setattr(kb, "_terminate_reclaimed_worker", race)
    assert not reclaim(conn, path)
    now = kb.get_task(conn, tid)
    assert (now.status, now.current_run_id) == (race.after.status, race.after.current_run_id)
    assert len(kb.list_events(conn, tid)) == race.events
    assert pending_recovery(conn, tid) is None


@pytest.mark.parametrize("path", ["death", "runtime"])
def test_existing_worker_loss_paths_resume_review_not_implementation(worker, path):
    conn, tid, rid = worker
    assert kb.request_review(conn, tid, summary="fixture", reviewer="reviewer", expected_run_id=rid)
    assert kb.claim_review_task(conn, tid)
    rid = kb.get_task(conn, tid).current_run_id
    assert save_checkpoint(conn, tid, run_id=rid, data=data())
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    kbd._set_worker_pid(conn, tid, process.pid)
    process.wait(timeout=10)
    if path == "runtime":
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET max_runtime_seconds=1 WHERE id=?", (tid,))
            conn.execute("UPDATE task_runs SET started_at=? WHERE id=?", (int(time.time()) - 10, rid))
        assert kbd.enforce_max_runtime(conn, signal_fn=lambda *_: None)
    else:
        assert kbd.detect_crashed_workers(conn)
    cp = pending_recovery(conn, tid)
    assert cp and kb.unblock_task(conn, tid, recovery=reconcile(cp))
    assert kb.get_task(conn, tid).status == "review"
    assert kb.claim_review_task(conn, tid)
