"""A real AIAgent loop must halt at a saved recovery pause before any model call."""
import json

from tests.hermes_cli.test_kanban_recovery import worker, data
from tests.agent.test_iteration_budget_warning import _agent
from tools.registry import registry


def test_saved_pause_halts_real_loop_without_model_request(worker, tmp_path, monkeypatch):
    from unittest.mock import Mock

    agent = _agent(tmp_path, monkeypatch, "null")
    try:
        response = json.loads(registry.dispatch("kanban_checkpoint", {
            "checkpoint": data(), "classification": "auth", "reason": "fixture credential unavailable", "pause": True,
        }))
        assert response["ok"], response
        # The external provider is the only fake: no paid calls or real credentials.
        call = Mock(side_effect=AssertionError("recovery pause must precede provider call"))
        monkeypatch.setattr(agent.client.chat.completions, "create", call)
        result = agent.run_conversation("continue fixture only")
        assert "Work paused" in result["final_response"]
        call.assert_not_called()
    finally:
        agent._session_db.close()
