"""Completion-event handoffs run only after upstream output is durable."""

from pathlib import Path

import cron.scheduler as scheduler


def test_completion_event_runs_after_output_is_saved(monkeypatch, tmp_path):
    calls = []
    output_file = tmp_path / "upstream.md"

    monkeypatch.setattr(
        scheduler,
        "run_job",
        lambda _job, **_kw: (True, "full report", "summary", None),
    )

    def save_output(_job_id, _output):
        calls.append("save")
        output_file.write_text("full report")
        return output_file

    monkeypatch.setattr(scheduler, "save_job_output", save_output)
    monkeypatch.setattr(scheduler, "_deliver_result", lambda *_a, **_kw: None)
    monkeypatch.setattr(scheduler, "mark_job_run", lambda *_a, **_kw: None)
    monkeypatch.setattr(scheduler, "finish_execution", lambda *_a, **_kw: None)
    monkeypatch.setattr(scheduler, "claim_dispatch", lambda _job_id: True)
    monkeypatch.setattr(scheduler, "mark_execution_running", lambda _id: None)

    def event(job, *, output_file, success, **_kw):
        calls.append("event")
        assert job["id"] == "upstream"
        assert success is True
        assert Path(output_file).read_text() == "full report"
        return None

    monkeypatch.setattr(scheduler, "_run_completion_event", event)

    assert scheduler.run_one_job({
        "id": "upstream", "name": "upstream", "execution_id": "e1",
        "completion_event": {"script": "finance_review_email.py"},
    }) is True
    assert calls == ["save", "event"]


def test_completion_event_failure_is_recorded_without_rewriting_upstream(monkeypatch, tmp_path):
    monkeypatch.setattr(
        scheduler, "run_job", lambda _job, **_kw: (True, "report", "summary", None)
    )
    monkeypatch.setattr(scheduler, "save_job_output", lambda *_a: tmp_path / "report.md")
    monkeypatch.setattr(scheduler, "_deliver_result", lambda *_a, **_kw: None)
    marks = []
    monkeypatch.setattr(scheduler, "mark_job_run", lambda *_a, **_kw: marks.append(_a))
    monkeypatch.setattr(scheduler, "finish_execution", lambda *_a, **_kw: None)
    monkeypatch.setattr(scheduler, "claim_dispatch", lambda _job_id: True)
    monkeypatch.setattr(scheduler, "mark_execution_running", lambda _id: None)
    monkeypatch.setattr(scheduler, "_run_completion_event", lambda *_a, **_kw: "email unavailable")

    assert scheduler.run_one_job({
        "id": "upstream", "name": "upstream", "execution_id": "e1",
        "completion_event": {"script": "finance_review_email.py"},
    }) is True
    assert marks[-1][1] is True
