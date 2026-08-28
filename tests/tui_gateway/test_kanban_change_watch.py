"""kanban.changed: the dashboard broadcasts board movement (task_events) so
clients (Argus) can refresh on change instead of polling (Talaria #4)."""
import os

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from tui_gateway import change_watcher as srv


def test_kanban_sig_and_payload_follow_task_events(tmp_path, monkeypatch):
    db_path = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kbc.init_db()
    # Point the watcher at ONLY this board.
    monkeypatch.setattr(srv, "_kanban_boards_db_paths", lambda: [("default", db_path)])
    srv._kanban_last_event_ids.clear()

    sig0 = srv._kanban_sig()
    assert sig0 is not None

    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="watch me", assignee="worker")
    finally:
        conn.close()
    sig1 = srv._kanban_sig()
    assert sig1 != sig0

    payload = srv._kanban_changed_payload()
    kinds = [(e["task_id"], e["kind"]) for e in payload["events"]]
    assert (tid, "created") in kinds
    assert all(e["board"] == "default" for e in payload["events"])
    # Cursor advanced: a second read without new events is empty.
    assert srv._kanban_changed_payload()["events"] == []

    assert "kanban.changed" in srv._CHANGE_WATCHES
