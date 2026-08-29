"""Talaria: ``config.set key=tier`` — session-scoped OpenRouter routing band in
the TUI/dashboard backend (the path Argus and the desktop use; slash commands
typed into those chats go to the model, so the picker must not depend on them).

Contract:
1. ``config.set key=tier value=<band>`` with a session pins
   ``create_routing_tier_override`` and stamps the live agent; never writes
   config.yaml.
2. ``value=status`` (or empty) reports the effective tier, its source, the
   price-ceiling summary and the band guide.
3. ``value=reset`` drops the pin and any auto-escalation.
4. ``_make_agent`` re-applies the pin on rebuild; ``session.info`` carries
   ``routing_tier``.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import tui_gateway.server as server
from agent import openrouter_routing as rp


@pytest.fixture(autouse=True)
def _policy(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_ROUTING_POLICY_FILE", str(tmp_path / "routing-policy.yaml"))
    cat = tmp_path / "models-cache.json"
    cat.write_text(json.dumps({"fetched_at": 4102444800.0, "data": [
        {"id": "anthropic/claude-opus-5", "pricing": {"prompt": "0.000005", "completion": "0.000025"}},
        {"id": "anthropic/claude-sonnet-5", "pricing": {"prompt": "0.000002", "completion": "0.00001"}},
    ]}), encoding="utf-8")
    monkeypatch.setenv("HERMES_ROUTING_MODELS_CACHE", str(cat))
    monkeypatch.setattr(rp, "_fetch_catalog", lambda timeout=6.0: None)
    monkeypatch.delenv("HERMES_ROUTING_TIER", raising=False)
    monkeypatch.delenv("HERMES_ROUTING_POLICY", raising=False)
    monkeypatch.setattr(rp, "current_profile_name", lambda: "default")
    rp._excl_cache.update({"key": None, "ids": None})
    rp.load_policy(force=True)


def _agent():
    return SimpleNamespace(
        reasoning_config=None, service_tier=None, request_overrides={},
        model="openrouter/auto", provider="openrouter", session_id="sess-key",
        platform="browser", _is_openrouter_url=lambda: True,
    )


def _set(params: dict) -> dict:
    return server._methods["config.set"]("rid-1", params)


def test_status_reports_effective_tier_and_bands():
    session = {"session_key": "k1", "agent": _agent()}
    with patch.dict(server._sessions, {"s1": session}, clear=False), \
            patch.object(server, "_write_config_key") as write_key:
        resp = _set({"key": "tier", "session_id": "s1", "value": "status"})
    assert resp["result"]["value"] == "high"                 # interactive default
    assert "class interactive" in resp["result"]["source"]
    assert resp["result"]["ceiling"]["excluded_count"] == 1  # opus over $3/$15
    assert [b["tier"] for b in resp["result"]["bands"]] == list(rp.TIERS)
    assert resp["result"]["override"] is None
    write_key.assert_not_called()


def test_set_pins_session_and_stamps_agent_without_global_write():
    agent = _agent()
    session = {"session_key": "k1", "agent": agent}
    with patch.dict(server._sessions, {"s1": session}, clear=False), \
            patch.object(server, "_write_config_key") as write_key, \
            patch.object(server, "_persist_live_session_runtime") as persist, \
            patch.object(server, "_emit") as emit:
        resp = _set({"key": "tier", "session_id": "s1", "value": "MAX"})
    assert resp["result"]["value"] == "max"
    assert resp["result"]["override"] == "max"
    assert resp["result"]["ceiling"]["excluded_count"] == 0    # ceiling lifted at max
    assert agent._routing_tier_override == "max"
    assert session["create_routing_tier_override"] == "max"
    write_key.assert_not_called()
    persist.assert_called_once()
    assert emit.call_args[0][0] == "session.info"
    assert emit.call_args[0][2]["routing_tier"] == "max"


def test_reset_drops_pin_and_escalation():
    agent = _agent()
    agent._routing_tier_override = "low"
    agent._routing_escalation = 2
    session = {"session_key": "k1", "agent": agent, "create_routing_tier_override": "low"}
    with patch.dict(server._sessions, {"s1": session}, clear=False), \
            patch.object(server, "_persist_live_session_runtime"), patch.object(server, "_emit"):
        resp = _set({"key": "tier", "session_id": "s1", "value": "reset"})
    assert resp["result"]["value"] == "high" and resp["result"]["override"] is None
    assert agent._routing_tier_override is None and agent._routing_escalation == 0
    assert "create_routing_tier_override" not in session


def test_unknown_value_and_missing_session_are_errors():
    session = {"session_key": "k1", "agent": _agent()}
    with patch.dict(server._sessions, {"s1": session}, clear=False):
        bad = _set({"key": "tier", "session_id": "s1", "value": "turbo"})
    assert "error" in bad and "unknown tier" in bad["error"]["message"]
    nosess = _set({"key": "tier", "value": "high"})
    assert "error" in nosess


def test_status_without_session_reports_profile_policy_default(monkeypatch):
    """New chats (no runtime yet) can ask for the profile's policy default."""
    with patch.object(server, "_resolve_model", return_value="openrouter/auto"):
        resp = _set({"key": "tier", "value": "status", "profile": "default"})
    assert resp["result"]["value"] == "high" and resp["result"]["override"] is None
    assert resp["result"]["profile"] == "default"
    assert "bands" in resp["result"] and "allowed_models" in resp["result"]


def test_pre_build_session_pin_is_reported():
    session = {"session_key": "k2", "agent": None, "create_routing_tier_override": "low"}
    with patch.dict(server._sessions, {"s2": session}, clear=False), \
            patch.object(server, "_resolve_model", return_value="openrouter/auto"):
        resp = _set({"key": "tier", "session_id": "s2", "value": ""})
    assert resp["result"]["value"] == "low" and "override" in resp["result"]["source"]


def test_session_info_carries_routing_tier():
    agent = _agent()
    agent._routing_tier_override = "xhigh"
    assert server._routing_tier_label(agent, {}) == "xhigh"
    assert server._routing_tier_label(_agent(), {}) == "high"
    off = _agent(); off.provider = "anthropic"; off._is_openrouter_url = lambda: False
    assert server._routing_tier_label(off, {}) == ""
