"""Talaria row 42: per-profile kanban_pin from routing-policy.yaml decides a worker's model
when the card carries no explicit override."""
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db_dispatch as kd


def _task(**kw):
    base = dict(id="t_test0001", assignee="fin_financial_analyst", skills=None, model_override=None,
                provider_override=None, reasoning_effort=None, goal_mode=False)
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.fixture
def no_toolsets(monkeypatch):
    monkeypatch.setattr(kd, "_resolve_worker_cli_toolsets", lambda home: [])
    monkeypatch.setattr(kd, "_resolve_hermes_argv", lambda: ["hermes"])


def _policy(monkeypatch, per_profile):
    import agent.openrouter_routing as orr
    monkeypatch.setattr(orr, "effective_policy", lambda profile=None: per_profile.get(profile, {}))


def test_pin_applies_when_card_has_no_override(monkeypatch, no_toolsets):
    _policy(monkeypatch, {"fin_financial_analyst": {"kanban_pin": {"model": "z-ai/glm-5.3-flash", "provider": "openrouter"}}})
    cmd = kd._worker_argv(_task(), "fin_financial_analyst", None)
    assert cmd[cmd.index("-m") + 1] == "z-ai/glm-5.3-flash"
    assert cmd[cmd.index("--provider") + 1] == "openrouter"


def test_explicit_card_override_beats_pin(monkeypatch, no_toolsets):
    _policy(monkeypatch, {"fin_financial_analyst": {"kanban_pin": {"model": "z-ai/glm-5.3-flash", "provider": "openrouter"}}})
    cmd = kd._worker_argv(_task(model_override="openai/gpt-6-luna", provider_override="openrouter"),
                          "fin_financial_analyst", None)
    assert cmd[cmd.index("-m") + 1] == "openai/gpt-6-luna"
    assert cmd.count("-m") == 1


def test_no_pin_means_profile_default(monkeypatch, no_toolsets):
    _policy(monkeypatch, {"fin_financial_analyst": {}})
    cmd = kd._worker_argv(_task(), "fin_financial_analyst", None)
    assert "-m" not in cmd and "--provider" not in cmd


def test_malformed_pin_is_ignored(monkeypatch, no_toolsets):
    _policy(monkeypatch, {"fin_financial_analyst": {"kanban_pin": "z-ai/glm-5.3-flash"}})
    assert "-m" not in kd._worker_argv(_task(), "fin_financial_analyst", None)
    _policy(monkeypatch, {"fin_financial_analyst": {"kanban_pin": {"provider": "openrouter"}}})
    assert "-m" not in kd._worker_argv(_task(), "fin_financial_analyst", None)


def test_policy_failure_never_blocks_dispatch(monkeypatch, no_toolsets):
    import agent.openrouter_routing as orr
    def boom(profile=None):
        raise RuntimeError("policy file unreadable")
    monkeypatch.setattr(orr, "effective_policy", boom)
    cmd = kd._worker_argv(_task(), "fin_financial_analyst", None)
    assert "-m" not in cmd and cmd[-2:] == ["-q", "work kanban task t_test0001"]
