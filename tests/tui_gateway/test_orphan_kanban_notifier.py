"""Tests for the process-level orphan kanban notifier (Talaria #16).

The per-session poller only runs while a TUI/browser session holds a live
socket; Argus opens one per send. These tests cover the delivery half for
sessions with no socket: claim for this profile's idle sessions only, skip
live sessions, deliver via a headless resumed CLI turn, rewind on failure.
"""

from unittest.mock import patch

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as kbn
from tui_gateway import server

KEY = "20260903_170045_758908"


def _subscribed_done_task(*, chat_id: str = KEY, owner: str = "default", summary: str = "logged 3.25h"):
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="Log Delta Star hours", assignee="pmo_project_manager")
        kbn.add_notify_sub(conn, task_id=tid, platform="tui", chat_id=chat_id, notifier_profile=owner)
        assert kb.complete_task(conn, tid, summary=summary)
        return tid
    finally:
        conn.close()


def _cursor(tid: str) -> int:
    conn = kbc.connect()
    try:
        return kbn.list_notify_subs(conn, task_id=tid, include_unowned=True)[0]["last_event_id"]
    finally:
        conn.close()


def _quiet(monkeypatch, *, live=frozenset(), profile="default"):
    monkeypatch.setattr(server, "_current_profile_name", lambda: profile)
    monkeypatch.setattr(server, "_live_tui_session_keys", lambda: set(live))
    server._orphan_notify_inflight.clear()


class TestCollect:
    def test_idle_session_events_are_claimed_once(self, monkeypatch):
        _quiet(monkeypatch)
        tid = _subscribed_done_task()
        before = _cursor(tid)

        pending = server._collect_orphan_kanban_notifications()

        assert list(pending) == [KEY]
        text, sub, old, claimed = pending[KEY][0]
        assert tid in text and "done" in text and "logged 3.25h" in text
        assert sub["task_id"] == tid and sub["board"]
        assert old == before and claimed > old
        assert _cursor(tid) == claimed
        assert server._collect_orphan_kanban_notifications() == {}

    def test_live_session_is_left_to_its_own_poller(self, monkeypatch):
        _quiet(monkeypatch, live={KEY})
        tid = _subscribed_done_task()
        before = _cursor(tid)
        assert server._collect_orphan_kanban_notifications() == {}
        assert _cursor(tid) == before

    def test_every_profiles_subscriptions_are_handled_by_the_session_host(self, monkeypatch):
        # The dashboard hosts browser sessions for all profiles; the sub's own
        # notifier_profile is what names the answering agent.
        _quiet(monkeypatch, profile="default")
        tid = _subscribed_done_task(owner="fin_finance_director")
        pending = server._collect_orphan_kanban_notifications()
        assert list(pending) == [KEY]
        assert pending[KEY][0][1]["notifier_profile"] == "fin_finance_director"
        assert tid in pending[KEY][0][0]

    def test_inflight_session_is_not_reclaimed(self, monkeypatch):
        _quiet(monkeypatch)
        tid = _subscribed_done_task()
        before = _cursor(tid)
        server._orphan_notify_inflight.add(KEY)
        try:
            assert server._collect_orphan_kanban_notifications() == {}
            assert _cursor(tid) == before
        finally:
            server._orphan_notify_inflight.discard(KEY)


class TestDeliver:
    def test_success_runs_a_headless_resumed_turn(self, monkeypatch):
        _quiet(monkeypatch, profile="default")  # host process profile is irrelevant
        tid = _subscribed_done_task(owner="chief-of-staff")
        batch = server._collect_orphan_kanban_notifications()[KEY]
        calls = []

        def fake_run(argv, **kw):
            calls.append((argv, kw))
            class P: returncode = 0; stdout = "ok"; stderr = ""
            return P()

        with patch.object(server.subprocess, "run", fake_run):
            assert server._deliver_orphan_kanban_notification(KEY, batch) is True
        # _resolve_hermes_bin may spawn git first; pick the resumed-turn call.
        argv, kw = next((a, k) for a, k in calls if "--resume" in a)
        assert argv[-9:-1] == ["-p", "chief-of-staff", "chat", "--resume", KEY, "-Q", "--accept-hooks", "-q"]
        assert tid in argv[-1] and "report back" in argv[-1]
        assert kw["env"]["HERMES_KANBAN_NOTIFY_RESUME"] == "1"
        assert kw["timeout"] == server._ORPHAN_KANBAN_TURN_TIMEOUT_S
        # cursor stays advanced: nothing to redeliver
        assert server._collect_orphan_kanban_notifications() == {}

    def test_failure_rewinds_cursor_so_next_tick_retries(self, monkeypatch):
        _quiet(monkeypatch)
        tid = _subscribed_done_task()
        before = _cursor(tid)
        batch = server._collect_orphan_kanban_notifications()[KEY]

        def failing_run(argv, **kw):
            class P: returncode = 1; stdout = ""; stderr = "boom"
            return P()

        with patch.object(server.subprocess, "run", failing_run):
            assert server._deliver_orphan_kanban_notification(KEY, batch) is False
        assert _cursor(tid) == before
        again = server._collect_orphan_kanban_notifications()
        assert list(again) == [KEY] and tid in again[KEY][0][0]

    def test_tick_dispatches_each_session_once(self, monkeypatch):
        _quiet(monkeypatch)
        _subscribed_done_task()
        _subscribed_done_task(chat_id="20260903_100000_aaaaaa")
        delivered = []
        monkeypatch.setattr(server, "_deliver_orphan_kanban_notification", lambda k, b: delivered.append(k) or True)
        assert server._orphan_kanban_notify_tick() == 2
        import time
        for _ in range(50):
            if len(delivered) == 2:
                break
            time.sleep(0.02)
        assert sorted(delivered) == sorted([KEY, "20260903_100000_aaaaaa"])
        assert server._orphan_kanban_notify_tick() == 0
        assert not server._orphan_notify_inflight


class TestStaleEvents:
    def test_events_older_than_max_age_are_claimed_but_not_delivered(self, monkeypatch):
        _quiet(monkeypatch)
        tid = _subscribed_done_task()
        conn = kbc.connect()
        try:
            with kbc.write_txn(conn):
                conn.execute("UPDATE task_events SET created_at = created_at - 3*86400 WHERE task_id = ?", (tid,))
        finally:
            conn.close()
        before = _cursor(tid)
        assert server._collect_orphan_kanban_notifications() == {}
        assert _cursor(tid) > before, "stale events are consumed, not retried forever"
