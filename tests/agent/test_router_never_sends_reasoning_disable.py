"""Talaria row 28: a thinking-disable is never sent into openrouter/auto, and a
stream-surfaced reasoning-mandatory error flags the session on classification."""
from types import SimpleNamespace

from agent.chat_completion_helpers import _reasoning_config_for_wire


def _agent(model, cfg, rejected=False):
    return SimpleNamespace(model=model, reasoning_config=cfg, _reasoning_disable_rejected=rejected, _ephemeral_reasoning_off=False)


def test_router_drops_configured_disable():
    assert _reasoning_config_for_wire(_agent("openrouter/auto", {"enabled": False, "effort": "none"})) is None
    assert _reasoning_config_for_wire(_agent("auto", {"enabled": False})) is None


def test_router_keeps_an_enabled_effort():
    cfg = {"enabled": True, "effort": "medium"}
    assert _reasoning_config_for_wire(_agent("openrouter/auto", cfg)) == cfg


def test_pinned_model_keeps_its_disable():
    cfg = {"enabled": False, "effort": "none"}
    assert _reasoning_config_for_wire(_agent("google/gemini-3.8-flash", cfg)) == cfg


def test_rejected_session_drops_disable_for_any_model():
    assert _reasoning_config_for_wire(_agent("google/gemini-3.8-flash", {"enabled": False}, rejected=True)) is None
