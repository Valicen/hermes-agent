"""Talaria: OpenRouter routing policy (agent/openrouter_routing.py)."""
from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from agent import openrouter_routing as rp


CATALOG = {"fetched_at": 4102444800.0, "data": [   # far-future fetched_at = always fresh
    {"id": "anthropic/claude-opus-5", "pricing": {"prompt": "0.000005", "completion": "0.000025"}},
    {"id": "anthropic/claude-fable-5", "pricing": {"prompt": "0.00001", "completion": "0.00005"}},
    {"id": "openai/gpt-5.5", "pricing": {"prompt": "0.000005", "completion": "0.00003"}},
    {"id": "openai/o1-pro", "pricing": {"prompt": "0.00015", "completion": "0.0006"}},
    {"id": "anthropic/claude-sonnet-5", "pricing": {"prompt": "0.000002", "completion": "0.00001"}},
    {"id": "moonshotai/kimi-k3", "pricing": {"prompt": "0.000003", "completion": "0.000015"}},
    {"id": "deepseek/deepseek-v4-flash", "pricing": {"prompt": "0.00000008", "completion": "0.00000017"}},
    {"id": "openrouter/auto", "pricing": {"prompt": "-1", "completion": "-1"}},
]}
OVER_DEFAULT_CEILING = ["anthropic/claude-fable-5", "anthropic/claude-opus-5", "openai/gpt-5.5", "openai/o1-pro"]


@pytest.fixture
def policy_file(tmp_path, monkeypatch):
    import json
    path = tmp_path / "routing-policy.yaml"
    monkeypatch.setenv("HERMES_ROUTING_POLICY_FILE", str(path))
    cat = tmp_path / "models-cache.json"
    cat.write_text(json.dumps(CATALOG), encoding="utf-8")
    monkeypatch.setenv("HERMES_ROUTING_MODELS_CACHE", str(cat))
    monkeypatch.setattr(rp, "_fetch_catalog", lambda timeout=6.0: None)   # never hit the network
    rp._excl_cache.update({"key": None, "ids": None})
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
    assert plug["excluded_models"] == OVER_DEFAULT_CEILING   # price ceiling, not a name list
    assert "ignore" not in kw["extra_body"]["provider"]                # bedrock caches fine (2026-08-29 test)
    assert kw["extra_body"]["provider"]["data_collection"] == "deny"     # privacy default
    assert kw["extra_body"]["provider"]["order"][:2] == ["anthropic", "openai"]   # caching providers first
    assert kw["extra_body"]["provider"]["allow_fallbacks"] is True


def test_prefer_providers_never_overrides_an_explicit_order(policy_file):
    kw = rp.apply_routing_policy(_agent(), _kwargs(extra_body={"provider": {"order": ["together"], "allow_fallbacks": False}}))
    assert kw["extra_body"]["provider"]["order"] == ["together"]
    assert kw["extra_body"]["provider"]["allow_fallbacks"] is False
    policy_file("prefer_providers: []\n")
    kw = rp.apply_routing_policy(_agent(), _kwargs())
    assert "order" not in kw["extra_body"]["provider"]


def test_data_collection_is_configurable_and_policy_wins_on_merge(policy_file):
    policy_file("data_collection: allow\n")
    kw = rp.apply_routing_policy(_agent(), _kwargs(extra_body={"provider": {"data_collection": "deny", "order": ["x"]}}))
    assert kw["extra_body"]["provider"]["data_collection"] == "allow"
    assert kw["extra_body"]["provider"]["order"] == ["x"]
    policy_file("data_collection: null\nignore_providers: []\nprefer_providers: []\n")
    kw = rp.apply_routing_policy(_agent(), _kwargs())
    assert "provider" not in kw["extra_body"]


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


def test_tier_override_wins_and_max_lifts_ceiling(policy_file):
    agent = _agent()
    agent._routing_tier_override = "max"
    plug = rp.apply_routing_policy(agent, _kwargs())["extra_body"]["plugins"][0]
    assert plug["cost_tier"] == "max"
    assert "excluded_models" not in plug              # ceiling lifted at max
    agent._routing_tier_override = "xhigh"
    plug = rp.apply_routing_policy(agent, _kwargs())["extra_body"]["plugins"][0]
    assert plug["excluded_models"] == OVER_DEFAULT_CEILING


def test_tier_auto_keeps_ceiling_without_band(policy_file):
    agent = _agent()
    agent._routing_tier_override = "auto"
    plug = rp.apply_routing_policy(agent, _kwargs())["extra_body"]["plugins"][0]
    assert "cost_tier" not in plug
    assert plug["excluded_models"] == OVER_DEFAULT_CEILING


def test_env_tier_beats_class_default(policy_file, monkeypatch):
    monkeypatch.setenv("HERMES_ROUTING_TIER", "xhigh")
    plug = rp.apply_routing_policy(_agent(platform="cron"), _kwargs())["extra_body"]["plugins"][0]
    assert plug["cost_tier"] == "xhigh"


def test_pinned_model_gets_provider_ignore_but_no_auto_router_plugin(policy_file):
    kw = rp.apply_routing_policy(_agent(model="anthropic/claude-sonnet-5"),
                                 _kwargs(model="anthropic/claude-sonnet-5"))
    assert "plugins" not in kw["extra_body"]
    assert kw["extra_body"]["provider"]["data_collection"] == "deny"


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
        "price_ceiling: {prompt: null, completion: null}\n"
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
    assert kw["extra_body"]["provider"] == {"order": ["anthropic"], "ignore": ["groq"], "data_collection": "deny"}


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
    assert "excluded_models" in rp.apply_routing_policy(agent, _kwargs())["extra_body"]["plugins"][0]   # ceiling still on at xhigh


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


def test_describe_mentions_tier_ceiling_and_file(policy_file):
    text = rp.describe(_agent(platform="cron"))
    assert "tier: low" in text and "routing-policy.yaml" in text
    assert "Price ceiling: $3.0/M in" in text and "4 models excluded" in text and "z-ai" in text
    assert "claude-opus-5" in text


def test_price_ceiling_is_configurable_and_name_globs_add_on_top(policy_file):
    policy_file("price_ceiling: {prompt: 2.5, completion: 20}\nexcluded_models: ['perplexity/*']\n")
    plug = rp.apply_routing_policy(_agent(), _kwargs())["extra_body"]["plugins"][0]
    # kimi-k3 ($3 in) now over the prompt ceiling; sonnet-5 ($2) stays
    assert plug["excluded_models"] == OVER_DEFAULT_CEILING[:2] + ["moonshotai/kimi-k3"] + OVER_DEFAULT_CEILING[2:] + ["perplexity/*"] \
        or set(plug["excluded_models"]) == set(OVER_DEFAULT_CEILING + ["moonshotai/kimi-k3", "perplexity/*"])
    assert "anthropic/claude-sonnet-5" not in plug["excluded_models"]


def test_no_catalogue_falls_back_to_name_globs(policy_file, monkeypatch, tmp_path, caplog):
    monkeypatch.setenv("HERMES_ROUTING_MODELS_CACHE", str(tmp_path / "missing.json"))
    rp._excl_cache.update({"key": None, "ids": None})
    plug = rp.apply_routing_policy(_agent(), _kwargs())["extra_body"]["plugins"][0]
    assert "anthropic/claude-opus*" in plug["excluded_models"]
    assert "anthropic/claude-fable*" in plug["excluded_models"]


def test_stale_catalogue_is_served_and_refreshed_in_background(policy_file, monkeypatch, tmp_path):
    import json, threading
    cat = tmp_path / "models-cache.json"
    stale = dict(CATALOG); stale["fetched_at"] = 1.0
    cat.write_text(json.dumps(stale), encoding="utf-8")
    rp._excl_cache.update({"key": None, "ids": None})
    fetched = threading.Event()
    def fake_fetch(timeout=6.0):
        fetched.set()
        return [{"id": "anthropic/claude-opus-5", "pricing": {"prompt": "0.000005", "completion": "0.000025"}}]
    monkeypatch.setattr(rp, "_fetch_catalog", fake_fetch)
    plug = rp.apply_routing_policy(_agent(), _kwargs())["extra_body"]["plugins"][0]
    assert plug["excluded_models"] == OVER_DEFAULT_CEILING            # stale served immediately
    assert fetched.wait(5)
    for _ in range(50):
        if json.loads(cat.read_text())["fetched_at"] > 1.0: break
        __import__("time").sleep(0.05)
    assert json.loads(cat.read_text())["fetched_at"] > 1.0             # refreshed on disk
