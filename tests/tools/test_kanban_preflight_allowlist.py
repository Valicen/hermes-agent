"""Talaria row 43: allowlisted lifecycle commands are pre-approved in the checkpoint preflight."""
from unittest import mock

import pytest

from tools import kanban_preflight


def _op(command):
    return {"command": command, "environment": "devon user david; QA only", "action": "stop QA units",
            "authorization_provenance": "David 2026-09-29", "units": ["argus-qa.service"],
            "required_capabilities": ["user systemd"], "dependencies": []}


STOP = "systemctl --user stop argus-qa.service talaria-qa.service"


def test_allowlisted_flagged_command_is_preapproved():
    with mock.patch("tools.approval_floors._command_matches_permanent_allowlist", return_value=True), \
         mock.patch("tools.approval_floors._match_user_deny_rule", return_value=None):
        [report] = kanban_preflight.inspect_operations([_op(STOP)])
    assert report["flagged"] is True
    assert report["permission_granted"] is True
    assert report["readiness"] == "pre-approved"
    assert "Run it now" in report["next_action"]


def test_flagged_command_without_allowlist_stays_operator_only():
    with mock.patch("tools.approval_floors._command_matches_permanent_allowlist", return_value=False), \
         mock.patch("tools.approval_floors._match_user_deny_rule", return_value=None):
        [report] = kanban_preflight.inspect_operations([_op(STOP)])
    assert report["flagged"] is True
    assert report["permission_granted"] is False
    assert report["readiness"] == "unknown"


def test_deny_rule_wins_over_allowlist():
    with mock.patch("tools.approval_floors._command_matches_permanent_allowlist", return_value=True), \
         mock.patch("tools.approval_floors._match_user_deny_rule", return_value="*systemctl*stop*argus.service*"):
        [report] = kanban_preflight.inspect_operations([_op("systemctl --user stop argus.service")])
    assert report["permission_granted"] is False
    assert report["readiness"] == "denied"
    assert report["deny_rule"]


def test_unflagged_command_is_preapproved():
    with mock.patch("tools.approval_floors._match_user_deny_rule", return_value=None):
        [report] = kanban_preflight.inspect_operations([_op("systemctl --user show argus-qa.service")])
    assert report["flagged"] is False
    assert report["permission_granted"] is True


def test_checkpoint_handler_pauses_only_for_unapproved_operations():
    from tools import kanban_tools
    captured = {}

    class _KB:
        @staticmethod
        def get_task(conn, tid):
            return type("T", (), {"status": "running"})()

    class _Board:
        def __enter__(self):
            return _KB, object()

        def __exit__(self, *a):
            return False

    def _save(conn, tid, *, run_id, data, classification, reason, pause):
        captured.update(classification=classification, reason=reason, pause=pause, data=data)
        return {"event_id": 1}

    common = dict(_worker_guard=lambda *a: "t_1", _worker_run_id=lambda tid: 5, _board=lambda board: _Board())
    with mock.patch.multiple(kanban_tools, **common), \
         mock.patch("hermes_cli.kanban_recovery.save_checkpoint", _save), \
         mock.patch("hermes_cli.kanban_recovery.validate_checkpoint", lambda c: dict(c or {})), \
         mock.patch("tools.kanban_preflight.inspect_operations", return_value=[{"flagged": True, "permission_granted": True}]):
        kanban_tools._handle_checkpoint({"checkpoint": {}, "operations": [_op(STOP)]})
    assert captured["pause"] is False and captured["classification"] is None
    with mock.patch.multiple(kanban_tools, **common), \
         mock.patch("hermes_cli.kanban_recovery.save_checkpoint", _save), \
         mock.patch("hermes_cli.kanban_recovery.validate_checkpoint", lambda c: dict(c or {})), \
         mock.patch("tools.kanban_preflight.inspect_operations", return_value=[{"flagged": True, "permission_granted": False}]):
        kanban_tools._handle_checkpoint({"checkpoint": {}, "operations": [_op(STOP)]})
    assert captured["pause"] is True and captured["classification"] == "operator_only"
