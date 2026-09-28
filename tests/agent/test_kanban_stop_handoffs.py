"""Stop guard consults real run ownership, not attempted or historical handoffs."""
import json

import pytest

from agent.kanban_stop import build_kanban_stop_nudge, session_called_kanban_terminal
from hermes_cli import kanban_db as kb
from tools.registry import registry
import tools.kanban_tools
from tests.hermes_cli.test_kanban_recovery import worker, data


def messages(name, result=None):
    rows = [{"role": "assistant", "tool_calls": [{"id": "handoff", "function": {
        "name": name, "arguments": "{}"}}]}]
    if result is not None:
        rows.append({"role": "tool", "tool_call_id": "handoff", "content": json.dumps(result)})
    return rows


@pytest.mark.parametrize("name,status", [
    ("kanban_complete", "done"), ("kanban_block", "blocked"),
    ("kanban_request_review", "review"), ("kanban_request_changes", "ready"),
    ("kanban_checkpoint", "blocked"),
])
def test_only_confirmed_terminal_results_suppress_fallback(worker, monkeypatch, name, status):
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID")
    for result in (None, {"ok": False}, {"error": "refused"}, {"ok": True, "status": "running"},
                   {"ok": True, "status": status, "task_id": "different-task"}):
        assert not session_called_kanban_terminal(messages(name, result))
        assert build_kanban_stop_nudge(messages=messages(name, result))
    good = {"ok": True, "status": status, "checkpoint": {"requires_reconciliation": True}}
    assert session_called_kanban_terminal(messages(name, good))
    assert build_kanban_stop_nudge(messages=messages(name, good)) is None
    assert not session_called_kanban_terminal(messages("kanban_checkpoint", {"ok": True, "status": "blocked"}))


@pytest.mark.parametrize("outcome", ["review", "changes_requested", "pause", "complete", "cancel"])
def test_actual_handoff_ends_nudge_even_with_successor_and_no_history(worker, monkeypatch, outcome):
    conn, tid, rid = worker
    assert build_kanban_stop_nudge(messages=[])
    if outcome == "changes_requested":
        assert kb.request_review(conn, tid, summary="fixture", reviewer="reviewer", expected_run_id=rid)
        assert kb.claim_review_task(conn, tid)
        rid = kb.get_task(conn, tid).current_run_id
        monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(rid))
        name, args = "kanban_request_changes", {"reason": "fixture changes"}
    elif outcome == "review":
        name, args = "kanban_request_review", {"summary": "fixture reviewed evidence"}
    elif outcome == "pause":
        name, args = "kanban_checkpoint", {"checkpoint": data(), "pause": True,
                                         "classification": "budget", "reason": "fixture limit"}
    elif outcome == "complete":
        name, args = "kanban_complete", {"summary": "fixture verified"}
    else:
        name, args = "kanban_block", {"reason": "operator cancelled"}
    result = json.loads(registry.dispatch(name, args))
    assert result.get("ok"), result
    assert build_kanban_stop_nudge(messages=messages(name, result)) is None
    assert build_kanban_stop_nudge(messages=[]) is None
    if outcome in {"review", "changes_requested"}:
        claim = kb.claim_review_task if outcome == "review" else kb.claim_task
        assert claim(conn, tid)
        successor = kb.get_task(conn, tid).current_run_id
        assert successor != rid
        assert build_kanban_stop_nudge(messages=[]) is None
        # New run must not accept a prior run's successful transcript as its handoff.
        monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(successor))
        assert build_kanban_stop_nudge(messages=messages(name, result))


def test_refused_real_review_and_checkpoint_leave_owned_work_unfinished(worker):
    conn, tid, rid = worker
    for name, args in [("kanban_request_review", {"summary": ""}),
                       ("kanban_request_changes", {"reason": "not a review run"}),
                       ("kanban_checkpoint", {"checkpoint": {}})]:
        result = json.loads(registry.dispatch(name, args))
        assert result.get("error"), result
        assert build_kanban_stop_nudge(messages=messages(name, result))
    assert kb.get_task(conn, tid).current_run_id == rid


def test_missing_board_does_not_get_created_or_assert_running(worker, monkeypatch, tmp_path):
    path = tmp_path / "absent.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(path))
    nudge = build_kanban_stop_nudge(messages=[])
    assert nudge and "Read the board" in nudge and "is still `running`" not in nudge
    assert not path.exists()
