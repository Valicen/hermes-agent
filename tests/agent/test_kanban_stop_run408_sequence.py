"""Incident-shaped stop guard regression; only synthetic SQLite boards are touched."""
import json
from pathlib import Path

import pytest

from agent.kanban_stop import build_kanban_stop_nudge, session_called_kanban_terminal
from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from tools.registry import registry
import tools.kanban_tools  # register real lifecycle handlers


def transcript(name, result):
    return [
        {"role": "assistant", "tool_calls": [{"id": "handoff", "function": {
            "name": name, "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "handoff", "content": json.dumps(result)},
    ]


@pytest.fixture
def incident_board(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(home / "board.db"))
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(tmp_path / "work"))
    for key in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_KANBAN_STOP_NUDGE"):
        monkeypatch.delenv(key, raising=False)
    kb.init_db()
    conn = kbc.connect()
    # Synthetic AUTOINCREMENT seeds preserve the incident's literal run IDs.
    conn.execute("INSERT INTO sqlite_sequence(name, seq) VALUES ('task_runs', 407)")
    conn.commit()
    tid = kb.create_task(conn, title="synthetic run408 handoff", assignee="worker")
    assert kb.claim_task(conn, tid)
    assert kb.get_task(conn, tid).current_run_id == 408
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "408")
    yield conn, tid
    conn.close()


def review_handoff(conn, tid):
    result = json.loads(registry.dispatch("kanban_request_review", {
        "summary": "synthetic verified implementation", "reviewer": "default"}))
    assert result.get("ok"), result
    row = conn.execute("SELECT outcome, ended_at FROM task_runs WHERE id=408").fetchone()
    assert row["outcome"] == "review_requested" and row["ended_at"] is not None
    return transcript("kanban_request_review", result)


def test_run408_successful_review_suppresses_nudge(incident_board):
    conn, tid = incident_board
    history = review_handoff(conn, tid)
    assert session_called_kanban_terminal(history)
    assert build_kanban_stop_nudge(messages=history) is None


def test_run412_claim_never_reactivates_run408(incident_board, monkeypatch):
    conn, tid = incident_board
    history = review_handoff(conn, tid)
    conn.execute("UPDATE sqlite_sequence SET seq=411 WHERE name='task_runs'")
    conn.commit()
    assert kb.claim_review_task(conn, tid)
    assert kb.get_task(conn, tid).current_run_id == 412
    claimed = conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind='claimed' AND run_id=412",
        (tid,),
    ).fetchone()
    assert json.loads(claimed["payload"])["source_status"] == "review"
    before = conn.total_changes
    assert build_kanban_stop_nudge(messages=history) is None
    assert build_kanban_stop_nudge(messages=[]) is None
    assert conn.total_changes == before
    assert kb.get_task(conn, tid).current_run_id == 412
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "412")
    # The successor cannot reuse an old successful transcript to skip its handoff.
    assert build_kanban_stop_nudge(messages=history)
    print("synthetic run408=review_requested; run412=running/source_status=review; stale nudge suppressed; successor nudge retained")


@pytest.mark.parametrize("name,status", [
    ("kanban_request_changes", "ready"), ("kanban_checkpoint", "blocked"),
])
def test_successful_changes_and_pause_results(incident_board, monkeypatch, name, status):
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID")
    result = {"ok": True, "status": status, "checkpoint": {"requires_reconciliation": True}}
    assert session_called_kanban_terminal(transcript(name, result))
    assert build_kanban_stop_nudge(messages=transcript(name, result)) is None


@pytest.mark.parametrize("name", ["kanban_complete", "kanban_block", "kanban_request_review",
                                 "kanban_request_changes", "kanban_checkpoint"])
def test_failed_calls_do_not_finish_owned_work(incident_board, name):
    history = transcript(name, {"ok": False, "error": "synthetic refusal"})
    assert not session_called_kanban_terminal(history)
    assert build_kanban_stop_nudge(messages=history)


def test_unknown_board_never_asserts_running(incident_board, monkeypatch, tmp_path):
    missing = tmp_path / "missing.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(missing))
    nudge = build_kanban_stop_nudge(messages=[])
    assert nudge and "is still `running`" not in nudge
    assert not missing.exists()
