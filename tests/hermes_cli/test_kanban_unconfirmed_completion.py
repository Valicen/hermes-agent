"""A worker that exits cleanly AFTER posting its result as a comment is not a
protocol violation: the work is on the card, only ``kanban_complete`` was
skipped. Retrying burned a whole budget redoing finished work and then
auto-blocked a done task (Valicen t_a5199403, 2026-08-25). Route to review.
"""
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB (mirrors test_kanban_db)."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kbc.init_db()
    return home


def _exited_status(code: int) -> int:
    return code << 8


def _dead_clean_exit_with_comment(conn, monkeypatch, *, comment: bool):
    import hermes_cli.kanban_db as _kb
    monkeypatch.setattr(kbd, "_pid_alive", lambda _pid: False)
    monkeypatch.setattr(_kb, "_pid_alive", lambda _pid: False, raising=False)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    host = _kb._claimer_id().split(":", 1)[0]
    tid = kb.create_task(conn, title="write the report", assignee="ito_it_director")
    kb.claim_task(conn, tid, claimer=f"{host}:w1")
    pid = 71001
    conn.execute("UPDATE tasks SET worker_pid=? WHERE id=?", (pid, tid))
    conn.commit()
    if comment:
        kb.add_comment(conn, tid, author="ito_it_director", body="## RESULTS\nReport written to /tmp/report.md; endpoint confirmed.")
    kbd._record_worker_exit(pid, _exited_status(0))
    return tid


def test_clean_exit_with_completion_comment_goes_to_review(kanban_home, monkeypatch):
    with kbc.connect() as conn:
        tid = _dead_clean_exit_with_comment(conn, monkeypatch, comment=True)
        crashed = kbd.detect_crashed_workers(conn)
        task = kb.get_task(conn, tid)
        assert task.status == "review", f"expected review, got {task.status}"
        assert task.consecutive_failures == 0
        assert not task.last_failure_error
        kinds = [r["kind"] for r in conn.execute("SELECT kind FROM task_events WHERE task_id=? ORDER BY id", (tid,)).fetchall()]
        assert "unconfirmed_completion" in kinds
        assert "review_requested" in kinds
        assert "protocol_violation" not in kinds
        outcomes = [r["outcome"] for r in conn.execute("SELECT outcome FROM task_runs WHERE task_id=?", (tid,)).fetchall()]
        assert "unconfirmed_completion" in outcomes and "crashed" not in outcomes


def test_clean_exit_without_comment_is_still_a_protocol_violation(kanban_home, monkeypatch):
    with kbc.connect() as conn:
        tid = _dead_clean_exit_with_comment(conn, monkeypatch, comment=False)
        kbd.detect_crashed_workers(conn)
        task = kb.get_task(conn, tid)
        assert task.status != "review"
        kinds = [r["kind"] for r in conn.execute("SELECT kind FROM task_events WHERE task_id=? ORDER BY id", (tid,)).fetchall()]
        assert "protocol_violation" in kinds
        assert "unconfirmed_completion" not in kinds
