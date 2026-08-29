"""OpenRouter routing policy — cage ``openrouter/auto`` per session, escalate on failure.

Talaria (Valicen fork) feature, 2026-08-29. Background and the numbers that
motivated it live in ``docs/routing-policy.md``; the short version:

* Every Valicen profile runs ``openrouter/auto``. The Auto Router picks a
  *model* per request from "what the OpenRouter community spends on for this
  task type"; it is not a price optimiser. Over 30 days it sent 10.6% of
  requests to Claude Opus, which was 59% of the bill.
* Provider competition (sales, price-weighted load balancing) happens *per
  model*, one layer below — it is preserved by everything this module does.
* OpenRouter accepts per-request constraints on auto (``plugins:
  [{"id": "auto-router", "cost_tier", "allowed_models", "excluded_models"}]``)
  and provider preferences (``provider: {"ignore": [...]}``). Characterised
  live 2026-08-29: ``cost_tier`` low→deepseek-v4-flash, medium→glm-5.2,
  high→claude-sonnet-5, xhigh→kimi-k3/gpt-5.6-sol, max→claude-opus-5;
  ``excluded_models``/``allowed_models``/``provider.ignore`` all honoured;
  ``provider.max_price`` is applied AFTER the model choice and fails the
  request (404 "No endpoints found") instead of downgrading — so it is NOT
  used here.

What this module does, per API call on an OpenRouter route:

1. Loads the fleet policy file ``<fleet root>/routing-policy.yaml`` (hot —
   mtime-cached, no restart needed) and overlays the current profile's
   section.
2. Classifies the *session* once (cron / kanban / subagent / interactive) and
   maps it to a tier. The tier is sticky for the session: flipping models
   between turns of one conversation destroys the prompt cache, and the
   cached prefix is ~90% of our tokens, so per-turn flipping would cost more
   than it saves.
3. Resolves the effective tier:  ``/tier`` session override  >  env
   ``HERMES_ROUTING_TIER``  >  session class default + automatic escalation.
4. Emits ``plugins[auto-router]`` (only when the model is ``openrouter/auto``)
   and ``provider.ignore`` (for every OpenRouter model — the Bedrock endpoint
   bills the full prefix with zero cache reads).
5. Escalates one tier automatically after N API errors / empty responses in
   a session (capped at ``escalation.max_auto_tier``), so a model that is
   failing us gets replaced by a stronger band without human intervention.

Loosening the belt (all documented in docs/routing-policy.md):
  * ``/tier high|xhigh|max`` in any chat — this session only (``max`` lifts the
    Opus cage; automation stops at ``xhigh``).
  * ``/tier auto`` — keep the cage but let the router pick freely inside it.
  * edit ``routing-policy.yaml`` (fleet-wide or per profile) — live.
  * ``enabled: false`` in the file, or env ``HERMES_ROUTING_POLICY=off`` —
    pure ``openrouter/auto`` as before.
"""
from __future__ import annotations

import copy
import fnmatch
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

TIERS: Tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")
SESSION_CLASSES: Tuple[str, ...] = ("cron", "kanban", "subagent", "interactive")
POLICY_FILENAME = "routing-policy.yaml"

# Baseline when the fleet file is missing or a key is absent. Mirrors the
# 2026-08-29 decision: keep market routing, exclude Opus unless explicitly
# asked, never let Anthropic traffic land on the no-cache Bedrock endpoint.
DEFAULT_POLICY: Dict[str, Any] = {
    "enabled": True,
    "excluded_models": ["anthropic/claude-opus*"],
    "allowed_models": [],
    "ignore_providers": ["amazon-bedrock"],
    "cage_lifted_at": "max",
    "tiers": {
        "cron": "low",
        "kanban": "medium",
        "subagent": "low",
        "interactive": "high",
    },
    "escalation": {
        "auto": True,
        "failures": 2,
        "max_auto_tier": "xhigh",
    },
    "profiles": {},
}

_AUTO_MODELS = frozenset({"openrouter/auto", "auto"})

# ---------------------------------------------------------------------------
# Policy file
# ---------------------------------------------------------------------------

_cache: Dict[str, Any] = {"path": None, "mtime": None, "policy": None}


def fleet_root() -> Path:
    """The directory shared by every profile: ``~/.hermes`` even when
    ``HERMES_HOME`` points at ``~/.hermes/profiles/<name>``."""
    try:
        from hermes_constants import get_hermes_home
        home = Path(get_hermes_home())
    except Exception:
        home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
    if home.parent.name == "profiles":
        return home.parent.parent
    return home


def policy_path() -> Path:
    override = os.environ.get("HERMES_ROUTING_POLICY_FILE")
    if override:
        return Path(override).expanduser()
    return fleet_root() / POLICY_FILENAME


def _deep_merge(base: Dict[str, Any], over: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _read_policy_file(path: Path) -> Dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        import yaml
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return data if isinstance(data, dict) else {}
    except Exception as exc:  # malformed file must never break inference
        logger.warning("routing policy: cannot read %s (%s) — using defaults", path, exc)
        return {}


def load_policy(force: bool = False) -> Dict[str, Any]:
    """Fleet policy = DEFAULT_POLICY overlaid with the file. Cached on mtime."""
    path = policy_path()
    try:
        mtime = path.stat().st_mtime_ns if path.exists() else None
    except OSError:
        mtime = None
    if (
        not force
        and _cache["policy"] is not None
        and _cache["path"] == str(path)
        and _cache["mtime"] == mtime
    ):
        return _cache["policy"]
    policy = _deep_merge(DEFAULT_POLICY, _read_policy_file(path))
    _cache.update({"path": str(path), "mtime": mtime, "policy": policy})
    return policy


def current_profile_name() -> str:
    try:
        from hermes_cli.profiles import get_active_profile_name
        return get_active_profile_name()
    except Exception:
        return "default"


def effective_policy(profile: Optional[str] = None) -> Dict[str, Any]:
    """Fleet policy with the profile's ``profiles.<name>`` section overlaid."""
    policy = load_policy()
    name = profile or current_profile_name()
    per_profile = (policy.get("profiles") or {}).get(name) or {}
    if per_profile:
        policy = _deep_merge(policy, {k: v for k, v in per_profile.items() if k != "profiles"})
    return policy


# ---------------------------------------------------------------------------
# Tiers
# ---------------------------------------------------------------------------

def tier_index(tier: Optional[str]) -> int:
    try:
        return TIERS.index(str(tier).strip().lower())
    except (ValueError, AttributeError):
        return -1


def normalize_tier(value: Any) -> Optional[str]:
    """``'HIGH'`` → ``'high'``; ``'auto'``/``''``/None → ``'auto'``; junk → None."""
    if value is None:
        return "auto"
    text = str(value).strip().lower()
    if text in ("", "auto", "none", "off"):
        return "auto"
    return text if text in TIERS else None


def step_tier(tier: str, steps: int, cap: Optional[str] = None) -> str:
    idx = max(0, tier_index(tier))
    idx = min(len(TIERS) - 1, idx + max(0, steps))
    if cap is not None and tier_index(cap) >= 0:
        idx = min(idx, tier_index(cap))
    return TIERS[idx]


# ---------------------------------------------------------------------------
# Session classification and per-agent state
# ---------------------------------------------------------------------------

def classify_session(agent: Any) -> str:
    """cron / kanban / subagent / interactive — decided once per agent."""
    cached = getattr(agent, "_routing_session_class", None)
    if cached in SESSION_CLASSES:
        return cached
    platform = str(getattr(agent, "platform", "") or "").strip().lower()
    if os.environ.get("HERMES_KANBAN_TASK"):
        klass = "kanban"
    elif platform == "cron":
        klass = "cron"
    elif platform == "subagent":
        klass = "subagent"
    else:
        klass = "interactive"
        try:
            from agent.delegation_context import is_delegated_child_context
            if is_delegated_child_context():
                klass = "subagent"
        except Exception:
            pass
    try:
        agent._routing_session_class = klass
    except Exception:
        pass
    return klass


def resolve_tier(agent: Any, policy: Dict[str, Any]) -> Tuple[Optional[str], str]:
    """Return ``(tier, source)``. ``tier`` is None when the router should pick
    freely inside the cage (``auto``)."""
    override = normalize_tier(getattr(agent, "_routing_tier_override", None)) \
        if getattr(agent, "_routing_tier_override", None) is not None else None
    if override is not None:
        return (None if override == "auto" else override), "session override (/tier)"
    env_tier = os.environ.get("HERMES_ROUTING_TIER")
    if env_tier:
        norm = normalize_tier(env_tier)
        if norm is not None:
            return (None if norm == "auto" else norm), "env HERMES_ROUTING_TIER"
    klass = classify_session(agent)
    base = normalize_tier((policy.get("tiers") or {}).get(klass))
    if base is None or base == "auto":
        tier = None
        source = f"class {klass} → auto"
    else:
        tier = base
        source = f"class {klass}"
    steps = int(getattr(agent, "_routing_escalation", 0) or 0)
    if steps and tier is not None:
        cap = (policy.get("escalation") or {}).get("max_auto_tier")
        escalated = step_tier(tier, steps, cap=cap)
        if escalated != tier:
            source += f" +{steps} escalation → {escalated}"
            tier = escalated
    return tier, source


def _cage_active(tier: Optional[str], policy: Dict[str, Any]) -> bool:
    """The exclusion list applies unless the tier reached ``cage_lifted_at``."""
    lift_at = policy.get("cage_lifted_at")
    if tier is None or tier_index(lift_at) < 0:
        return True
    return tier_index(tier) < tier_index(lift_at)


# ---------------------------------------------------------------------------
# Request shaping
# ---------------------------------------------------------------------------

def _is_openrouter(agent: Any) -> bool:
    try:
        if callable(getattr(agent, "_is_openrouter_url", None)) and agent._is_openrouter_url():
            return True
    except Exception:
        pass
    return str(getattr(agent, "provider", "") or "").lower() == "openrouter"


def policy_enabled(policy: Dict[str, Any]) -> bool:
    if str(os.environ.get("HERMES_ROUTING_POLICY", "")).strip().lower() in ("off", "0", "false", "disabled"):
        return False
    return bool(policy.get("enabled", True))


def routing_extra_body(agent: Any, model: Optional[str] = None,
                       policy: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """Compute the extra_body fragment for this call, or None when not applicable."""
    if not _is_openrouter(agent):
        return None
    policy = policy or effective_policy()
    if not policy_enabled(policy):
        return None
    model_name = str(model or getattr(agent, "model", "") or "").strip().lower()
    tier, source = resolve_tier(agent, policy)
    fragment: Dict[str, Any] = {}

    if model_name in _AUTO_MODELS:
        plugin: Dict[str, Any] = {"id": "auto-router"}
        if tier is not None:
            plugin["cost_tier"] = tier
        allowed = [str(x) for x in (policy.get("allowed_models") or []) if str(x).strip()]
        if allowed:
            plugin["allowed_models"] = allowed
        excluded = [str(x) for x in (policy.get("excluded_models") or []) if str(x).strip()]
        if excluded and _cage_active(tier, policy):
            plugin["excluded_models"] = excluded
        fragment["plugins"] = [plugin]

    ignore = [str(x) for x in (policy.get("ignore_providers") or []) if str(x).strip()]
    if ignore:
        fragment["provider"] = {"ignore": ignore}

    if not fragment:
        return None
    fragment["_routing_meta"] = {"tier": tier, "source": source,
                                 "class": classify_session(agent)}
    return fragment


def apply_routing_policy(agent: Any, api_kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """Merge the routing fragment into ``api_kwargs['extra_body']`` in place.

    Merge rules: ``plugins`` — replace any existing ``auto-router`` entry, keep
    others; ``provider`` — union the ``ignore`` list with existing preferences
    (existing ``order``/``only``/``sort`` are left alone). Never raises.
    """
    try:
        fragment = routing_extra_body(agent, api_kwargs.get("model"))
    except Exception as exc:  # policy must never break a turn
        logger.warning("routing policy: skipped (%s)", exc)
        return api_kwargs
    if not fragment:
        return api_kwargs
    meta = fragment.pop("_routing_meta", {})
    extra = dict(api_kwargs.get("extra_body") or {})

    if "plugins" in fragment:
        existing = [p for p in (extra.get("plugins") or [])
                    if not (isinstance(p, dict) and p.get("id") == "auto-router")]
        extra["plugins"] = existing + fragment["plugins"]
    if "provider" in fragment:
        prov = dict(extra.get("provider") or {})
        merged_ignore = list(prov.get("ignore") or [])
        for slug in fragment["provider"]["ignore"]:
            if slug not in merged_ignore:
                merged_ignore.append(slug)
        prov["ignore"] = merged_ignore
        extra["provider"] = prov
    api_kwargs["extra_body"] = extra

    snapshot = {
        "tier": meta.get("tier"), "source": meta.get("source"),
        "class": meta.get("class"),
        "excluded": list((fragment.get("plugins") or [{}])[0].get("excluded_models") or []),
        "ignore": list(fragment.get("provider", {}).get("ignore") or []),
    }
    try:
        if getattr(agent, "_routing_last", None) != snapshot:
            logger.info(
                "routing policy: class=%s tier=%s (%s) excluded=%s ignore=%s model=%s",
                snapshot["class"], snapshot["tier"] or "auto", snapshot["source"],
                ",".join(snapshot["excluded"]) or "-", ",".join(snapshot["ignore"]) or "-",
                api_kwargs.get("model"),
            )
        agent._routing_last = snapshot
    except Exception:
        pass
    return api_kwargs


# ---------------------------------------------------------------------------
# Automatic escalation
# ---------------------------------------------------------------------------

def note_failure(agent: Any, reason: str) -> Optional[str]:
    """Record an API error / empty response. After ``escalation.failures`` of
    them in one session, step the tier up by one (capped). Returns the new
    tier when an escalation happened, else None."""
    try:
        policy = effective_policy()
        if not policy_enabled(policy) or not _is_openrouter(agent):
            return None
        esc = policy.get("escalation") or {}
        if not esc.get("auto", True):
            return None
        threshold = max(1, int(esc.get("failures", 2) or 2))
        count = int(getattr(agent, "_routing_failures", 0) or 0) + 1
        agent._routing_failures = count
        if count < threshold:
            return None
        agent._routing_failures = 0
        before, _ = resolve_tier(agent, policy)
        if before is None:
            # 'auto' sessions have no band to step; give them one.
            before = normalize_tier((policy.get("tiers") or {}).get(classify_session(agent))) or "medium"
            if before == "auto":
                before = "medium"
        cap = esc.get("max_auto_tier")
        if tier_index(before) >= tier_index(cap) >= 0:
            return None
        agent._routing_escalation = int(getattr(agent, "_routing_escalation", 0) or 0) + 1
        after, _ = resolve_tier(agent, policy)
        if after == before:
            agent._routing_escalation -= 1
            return None
        logger.warning(
            "routing policy: %d %s failures in session — escalating tier %s → %s (cap %s)",
            threshold, reason, before, after, cap,
        )
        agent._routing_last_escalation = {"from": before, "to": after, "reason": reason,
                                          "at": time.time()}
        return after
    except Exception as exc:
        logger.debug("routing policy: note_failure skipped (%s)", exc)
        return None


# ---------------------------------------------------------------------------
# Human-facing status
# ---------------------------------------------------------------------------

def describe(agent: Any) -> str:
    policy = effective_policy()
    if not policy_enabled(policy):
        return "Routing policy: OFF (pure openrouter/auto)."
    if not _is_openrouter(agent):
        return "Routing policy: not applicable (this session is not on OpenRouter)."
    tier, source = resolve_tier(agent, policy)
    klass = classify_session(agent)
    cage = policy.get("excluded_models") or []
    lines = [
        f"Routing policy: tier {tier or 'auto'} ({source}); session class {klass}.",
        f"Cage: {', '.join(cage) if cage else 'none'}"
        + (f" — lifted at {policy.get('cage_lifted_at')}" if cage else "")
        + (" [LIFTED for this tier]" if cage and not _cage_active(tier, policy) else ""),
        f"Ignored providers: {', '.join(policy.get('ignore_providers') or []) or 'none'}.",
    ]
    esc = policy.get("escalation") or {}
    lines.append(
        f"Auto-escalation: {'on' if esc.get('auto', True) else 'off'} "
        f"after {esc.get('failures', 2)} failures, cap {esc.get('max_auto_tier')}; "
        f"steps so far {int(getattr(agent, '_routing_escalation', 0) or 0)}."
    )
    lines.append("Change: /tier low|medium|high|xhigh|max|auto (this session) · /tier reset · file: "
                 + str(policy_path()))
    return "\n".join(lines)
