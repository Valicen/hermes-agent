"""Talaria: OpenRouter routing policy (agent/openrouter_routing.py)."""
from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from agent import openrouter_routing as rp


@pytest.fixture
def policy_file(tmp_path, monkeypatch):
    path = tmp_path / "routing-policy.yaml"
    monkeypatch.setenv("HERMES_ROUTING_POLICY_FILE", str(path))
    monkeypatch.delenv("HERMES_ROUTING_TIER", raising=False)
    monkeypatch.delenv("HERMES_ROUTING_POLICY", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setattr(rp, "current_profile_name", lambda: "default")
    rp.load_policy(force=True)

    def write(text: str):
        path.write_text(text, encoding="utf-8")
        rp.load_policy(force=True)
        return path

    return write


def _agent(platform="cli", model="openrouter/auto", provider="openrouter"):
    return SimpleNamespace(platform=platform, model=model, provider=provider,
                           _is_openrouter_url=lambda: provider == "openrouter")


def _kwargs(model="openrouter/auto", extra_body=None):
    kw = {"model": model, "messages": []}
    if extra_body is not None:
        kw["extra_body"] = extra_body
    return kw


def test_defaults_cage_opus_and_ignore_bedrock(policy_file):
    kw = rp.apply_routing_policy(_agent(), _kwargs())
    plug = kw["extra_body"]["plugins"][0]
    assert plug["id"] == "auto-router"
    assert plug["cost_tier"] == "high"                       # interactive default
    assert plug["excluded_models"] == ["anthropic/claude-opus*"]
    assert kw["extra_body"]["provider"]["ignore"] == ["amazon-bedrock"]


@pytest.mark.parametrize("platform,env,expected", [
    ("cron", None, "low"),
    ("cli", "t-123", "medium"),          # kanban worker (HERMES_KANBAN_TASK)
    ("subagent", None, "low"),
    ("telegram", None, "high"),
])
def test_session_class_maps_to_tier(policy_file, monkeypatch, platform, env, expected):
    if env:
        monkeypatch.setenv("HERMES_KANBAN_TASK", env)
    kw = rp.apply_routing_policy(_agent(platform=platform), _kwargs())
    assert kw["extra_body"]["plugins"][0]["cost_tier"] == expected


def test_session_class_is_sticky(policy_file, monkeypatch):
    agent = _agent(platform="cron")
    assert rp.classify_session(agent) == "cron"
    agent.platform = "telegram"                     # later mutation is ignored
    assert rp.classify_session(agent) == "cron"


def test_tier_override_wins_and_max_lifts_cage(policy_file):
    agent = _agent()
    agent._routing_tier_override = "max"
    plug = rp.apply_routing_policy(agent, _kwargs())["extra_body"]["plugins"][0]
    assert plug["cost_tier"] == "max"
    assert "excluded_models" not in plug              # cage lifted at max
    agent._routing_tier_override = "xhigh"
    plug = rp.apply_routing_policy(agent, _kwargs())["extra_body"]["plugins"][0]
    assert plug["excluded_models"] == ["anthropic/claude-opus*"]


def test_tier_auto_keeps_cage_without_band(policy_file):
    agent = _agent()
    agent._routing_tier_override = "auto"
    plug = rp.apply_routing_policy(agent, _kwargs())["extra_body"]["plugins"][0]
    assert "cost_tier" not in plug
    assert plug["excluded_models"] == ["anthropic/claude-opus*"]


def test_env_tier_beats_class_default(policy_file, monkeypatch):
    monkeypatch.setenv("HERMES_ROUTING_TIER", "xhigh")
    plug = rp.apply_routing_policy(_agent(platform="cron"), _kwargs())["extra_body"]["plugins"][0]
    assert plug["cost_tier"] == "xhigh"


def test_pinned_model_gets_provider_ignore_but_no_auto_router_plugin(policy_file):
    kw = rp.apply_routing_policy(_agent(model="anthropic/claude-sonnet-5"),
                                 _kwargs(model="anthropic/claude-sonnet-5"))
    assert "plugins" not in kw["extra_body"]
    assert kw["extra_body"]["provider"]["ignore"] == ["amazon-bedrock"]


def test_non_openrouter_route_untouched(policy_file):
    agent = _agent(provider="anthropic")
    kw = rp.apply_routing_policy(agent, _kwargs(model="claude-sonnet-5"))
    assert "extra_body" not in kw


def test_disabled_by_file_and_by_env(policy_file, monkeypatch):
    policy_file("enabled: false\n")
    assert "extra_body" not in rp.apply_routing_policy(_agent(), _kwargs())
    policy_file("enabled: true\n")
    monkeypatch.setenv("HERMES_ROUTING_POLICY", "off")
    assert "extra_body" not in rp.apply_routing_policy(_agent(), _kwargs())


def test_file_overrides_and_profile_section(policy_file, monkeypatch):
    policy_file(
        "tiers:\n  interactive: medium\n"
        "excluded_models: []\n"
        "profiles:\n  ito_it_director:\n    tiers:\n      interactive: xhigh\n"
        "    ignore_providers: [amazon-bedrock, groq]\n"
    )
    plug = rp.apply_routing_policy(_agent(), _kwargs())["extra_body"]["plugins"][0]
    assert plug["cost_tier"] == "medium" and "excluded_models" not in plug
    monkeypatch.setattr(rp, "current_profile_name", lambda: "ito_it_director")
    kw = rp.apply_routing_policy(_agent(), _kwargs())
    assert kw["extra_body"]["plugins"][0]["cost_tier"] == "xhigh"
    assert kw["extra_body"]["provider"]["ignore"] == ["amazon-bedrock", "groq"]


def test_hot_reload_on_mtime_change(policy_file):
    path = policy_file("tiers:\n  interactive: low\n")
    assert rp.apply_routing_policy(_agent(), _kwargs())["extra_body"]["plugins"][0]["cost_tier"] == "low"
    import os, time
    path.write_text("tiers:\n  interactive: xhigh\n", encoding="utf-8")
    os.utime(path, (time.time() + 5, time.time() + 5))
    assert rp.apply_routing_policy(_agent(), _kwargs())["extra_body"]["plugins"][0]["cost_tier"] == "xhigh"


def test_malformed_file_falls_back_to_defaults(policy_file, caplog):
    policy_file("tiers: [not, a, mapping\n")
    with caplog.at_level(logging.WARNING):
        plug = rp.apply_routing_policy(_agent(), _kwargs())["extra_body"]["plugins"][0]
    assert plug["cost_tier"] == "high"


def test_merge_keeps_existing_plugins_and_provider_prefs(policy_file):
    existing = {"plugins": [{"id": "web"}, {"id": "auto-router", "cost_tier": "low"}],
                "provider": {"order": ["anthropic"], "ignore": ["groq"]}}
    kw = rp.apply_routing_policy(_agent(), _kwargs(extra_body=existing))
    ids = [p["id"] for p in kw["extra_body"]["plugins"]]
    assert ids == ["web", "auto-router"]                 # ours replaced the stale one
    assert kw["extra_body"]["plugins"][1]["cost_tier"] == "high"
    assert kw["extra_body"]["provider"] == {"order": ["anthropic"], "ignore": ["groq", "amazon-bedrock"]}


def test_auto_escalation_steps_one_band_after_threshold(policy_file):
    agent = _agent(platform="cron")                       # low
    assert rp.note_failure(agent, "api_error") is None    # 1 of 2
    assert rp.note_failure(agent, "api_error") == "medium"
    assert rp.apply_routing_policy(agent, _kwargs())["extra_body"]["plugins"][0]["cost_tier"] == "medium"
    assert agent._routing_failures == 0                   # counter reset
    rp.note_failure(agent, "empty_response"); assert rp.note_failure(agent, "empty_response") == "high"
    rp.note_failure(agent, "api_error"); assert rp.note_failure(agent, "api_error") == "xhigh"
    rp.note_failure(agent, "api_error"); assert rp.note_failure(agent, "api_error") is None   # capped at xhigh
    assert rp.apply_routing_policy(agent, _kwargs())["extra_body"]["plugins"][0]["cost_tier"] == "xhigh"
    assert "excluded_models" in rp.apply_routing_policy(agent, _kwargs())["extra_body"]["plugins"][0]


def test_auto_escalation_can_be_disabled(policy_file):
    policy_file("escalation:\n  auto: false\n")
    agent = _agent()
    for _ in range(5):
        assert rp.note_failure(agent, "api_error") is None


def test_explicit_override_is_not_escalated(policy_file):
    agent = _agent()
    agent._routing_tier_override = "low"
    rp.note_failure(agent, "api_error"); rp.note_failure(agent, "api_error")
    assert rp.apply_routing_policy(agent, _kwargs())["extra_body"]["plugins"][0]["cost_tier"] == "low"


def test_describe_mentions_tier_and_file(policy_file):
    text = rp.describe(_agent(platform="cron"))
    assert "tier low" in text and "routing-policy.yaml" in text
    assert "amazon-bedrock" in text
