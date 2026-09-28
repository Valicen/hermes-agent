"""Recovery receipts traverse the real durable notifier with a fixture transport."""
import asyncio
import time

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc, kanban_db_notify as kbn
from hermes_cli.kanban_recovery import save_checkpoint
from agent.kanban_recovery import snapshot
from tests.gateway.test_kanban_notifier_wake_only_ordering import (
    FailingWakeAdapter, RecordingAdapter, _make_runner, _run_one_notifier_tick,
)


def test_recovery_notification_retries_wake_without_duplicate_ping(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "notify.db"))
    kb.init_db()
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="recovery fixture", assignee="worker")
        kb.claim_task(conn, tid)
        rid = kb.get_task(conn, tid).current_run_id
        kbn.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1",
                           chat_type="dm", delivery_mode="notify+wake")
        save_checkpoint(conn, tid, run_id=rid, data=snapshot(), classification="policy",
                        reason="fixture approval denied", pause=True)
    failing = FailingWakeAdapter()
    runner = _make_runner(failing)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))
    assert len(failing.sent) == 1 and len(failing.handled) == 1
    assert "policy" in failing.sent[0]["text"]
    runner._running = True
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))
    assert len(failing.sent) == 1 and len(failing.handled) == 2
    good = RecordingAdapter()
    runner = _make_runner(good)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))
    assert good.sent == [] and len(good.handled) == 1
    runner._running = True
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))
    assert good.sent == [] and len(good.handled) == 1


def test_budget_checkpoint_reaches_wake_only_subscription(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "warning.db"))
    kb.init_db()
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="budget fixture", assignee="worker")
        kb.claim_task(conn, tid)
        rid = kb.get_task(conn, tid).current_run_id
        kbn.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1",
                           chat_type="dm", delivery_mode="wake")
        data = snapshot()
        first = save_checkpoint(conn, tid, run_id=rid, data=data, classification="budget",
                                reason="near budget; progress saved", attention=True)
        again = save_checkpoint(conn, tid, run_id=rid, data=data, classification="budget",
                                reason="near budget; progress saved", attention=True)
        assert first["event_id"] == again["event_id"]
    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))
    assert len(adapter.handled) == 1 and adapter.sent == []
    runner._running = True
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))
    assert len(adapter.handled) == 1


@pytest.mark.parametrize("path", ["heartbeat", "orphan", "ttl"])
def test_reclaim_attention_is_delivered_once(tmp_path, monkeypatch, path):
    from hermes_cli import kanban_db_dispatch as kbd
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "reclaim.db"))
    kb.init_db()
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="reclaim fixture", assignee="worker")
        kb.claim_task(conn, tid)
        rid = kb.get_task(conn, tid).current_run_id
        kbn.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1",
                           chat_type="dm", delivery_mode="notify+wake")
        save_checkpoint(conn, tid, run_id=rid, data={**snapshot(), "owner": "fixture-operator",
                                                   "next_command": "inspect fixture"})
        with kb.write_txn(conn):
            conn.execute("UPDATE task_runs SET started_at=? WHERE id=?", (int(time.time()) - 20000, rid))
            conn.execute("UPDATE tasks SET last_heartbeat_at=NULL, claim_expires=? WHERE id=?",
                         (None if path == "orphan" else int(time.time()) - 1, tid))
        def tick():
            if path == "heartbeat":
                return kbd.detect_stale_running(conn, stale_timeout_seconds=100)
            if path == "orphan":
                return kbd.reconcile_orphaned_running(conn)
            return kb.release_stale_claims(conn)
        assert tick()
        assert not tick()
    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))
    assert len(adapter.sent) == len(adapter.handled) == 1
    assert "fixture-operator" in adapter.sent[0]["text"]
    assert "inspect fixture" in adapter.sent[0]["text"]
    runner._running = True
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))
    assert len(adapter.sent) == len(adapter.handled) == 1
