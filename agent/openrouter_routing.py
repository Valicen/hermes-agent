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
   with the band and a PRICE CEILING: every model in OpenRouter's live
   catalogue priced above ``price_ceiling`` ($/M prompt or completion) is
   passed as ``excluded_models`` — Opus, Fable, GPT-5.5, the *-pro tiers,
   o1/o3-pro, whatever appears tomorrow — until the tier reaches
   ``ceiling_lifted_at``. Name globs in ``excluded_models`` are added on top.
   ``provider.ignore`` goes on every OpenRouter model (the Bedrock endpoint
   bills the full prefix with zero cache reads).
5. Escalates one tier automatically after N API errors / empty responses in
   a session (capped at ``escalation.max_auto_tier``), so a model that is
   failing us gets replaced by a stronger band without human intervention.

Loosening the belt (all documented in docs/routing-policy.md):
  * ``/tier high|xhigh|max`` in any chat — this session only (``max`` lifts the
    price ceiling; automation stops at ``xhigh``).
  * ``/tier auto`` — keep the ceiling but let the router pick freely under it.
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
    # Price ceiling ($ per million tokens). Models above EITHER number are
    # excluded from the Auto Router's choice until the tier reaches
    # ``ceiling_lifted_at``. 3/15 keeps sonnet-5 ($2/$10), gpt-5.6-sol/terra
    # ($2/$10-12) and kimi-k3 ($3/$15) and drops opus-5 ($5/$25), fable-5
    # ($10/$50), gpt-5.5 ($5/$30), every *-pro and o1 tier.
    "price_ceiling": {"prompt": 3.0, "completion": 15.0},
    "ceiling_lifted_at": "max",
    # Extra name globs excluded on top of the price ceiling (always).
    "excluded_models": [],
    # Used ONLY when the catalogue cannot be fetched and no cache exists.
    "fallback_excluded_models": [
        "anthropic/claude-opus*", "anthropic/claude-fable*", "openai/gpt-5.5*",
        "openai/*-pro", "openai/o1*", "openai/o3-pro", "openai/gpt-4",
        "openai/gpt-4-turbo*", "sakana/fugu-ultra",
    ],
    "allowed_models": [],
    # Providers to try FIRST for whatever model the router picks (OpenRouter
    # provider.order with allow_fallbacks). Rationale (2026-08-29 experiment):
    # prompt caching is per provider; first-party/caching providers returned
    # cached_tokens on the second call (Z.AI, Anthropic via Google/Bedrock,
    # OpenInference for deepseek-flash) while some cheap resellers (Io Net,
    # DigitalOcean) returned zero — and cached reads are ~90% of our tokens,
    # so a caching provider beats a 20% cheaper non-caching one 5-8x.
    "prefer_providers": [
        "anthropic", "openai", "azure", "google-vertex", "google-ai-studio",
        "z-ai", "deepseek", "moonshotai", "x-ai", "mistral",
        "openinference", "novita", "siliconflow",
    ],
    # Endpoints never used. Empty by default: the 2026-08-29 experiment showed
    # amazon-bedrock caches Anthropic prompts like any other endpoint (the
    # earlier "zero cache reads" reading came from unresolved openrouter/auto
    # accounting rows, not from billing).
    "ignore_providers": [],
    # OpenRouter provider.data_collection: "deny" excludes endpoints that may
    # retain prompts/completions non-transiently (David, 2026-08-29). Live
    # check: every band and every model we use still had endpoints under deny.
    "data_collection": "deny",
    "catalog_ttl_hours": 24,
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
# OpenRouter catalogue → price-derived exclusion list
# ---------------------------------------------------------------------------

CATALOG_URL = "https://openrouter.ai/api/v1/models"
CATALOG_CACHE_NAME = "routing-policy.models-cache.json"
_catalog_lock = __import__("threading").Lock()
_excl_cache: Dict[str, Any] = {"key": None, "ids": None}


def catalog_cache_path() -> Path:
    override = os.environ.get("HERMES_ROUTING_MODELS_CACHE")
    if override:
        return Path(override).expanduser()
    return fleet_root() / CATALOG_CACHE_NAME


def _fetch_catalog(timeout: float = 6.0) -> Optional[List[Dict[str, Any]]]:
    """Public endpoint, no key. Returns the model list or None on any failure."""
    try:
        import json
        import urllib.request
        req = urllib.request.Request(CATALOG_URL, headers={"User-Agent": "talaria-routing-policy/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
        models = data.get("data") if isinstance(data, dict) else None
        return models if isinstance(models, list) and models else None
    except Exception as exc:
        logger.warning("routing policy: catalogue fetch failed (%s)", exc)
        return None


def _write_catalog_cache(path: Path, models: List[Dict[str, Any]]) -> None:
    try:
        import json
        slim = [{"id": m.get("id"), "pricing": {
            "prompt": (m.get("pricing") or {}).get("prompt"),
            "completion": (m.get("pricing") or {}).get("completion")}} for m in models if m.get("id")]
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"fetched_at": time.time(), "data": slim}), encoding="utf-8")
        tmp.replace(path)
    except Exception as exc:
        logger.debug("routing policy: cannot write catalogue cache (%s)", exc)


def load_catalog(ttl_hours: float = 24.0) -> Tuple[Optional[List[Dict[str, Any]]], Optional[float]]:
    """Cached catalogue. Fresh → cache; stale → cache now + background refresh;
    missing → synchronous fetch (bounded). Returns (models, fetched_at)."""
    import json
    import threading
    path = catalog_cache_path()
    cached, fetched_at = None, None
    try:
        if path.is_file():
            blob = json.loads(path.read_text(encoding="utf-8"))
            cached, fetched_at = blob.get("data"), float(blob.get("fetched_at") or 0)
    except Exception:
        cached, fetched_at = None, None
    fresh = cached is not None and fetched_at and (time.time() - fetched_at) < ttl_hours * 3600
    if fresh:
        return cached, fetched_at
    if cached is not None:
        # Serve stale, refresh once in the background.
        if _catalog_lock.acquire(blocking=False):
            def _refresh():
                try:
                    models = _fetch_catalog()
                    if models:
                        _write_catalog_cache(path, models)
                finally:
                    _catalog_lock.release()
            threading.Thread(target=_refresh, name="routing-catalog-refresh", daemon=True).start()
        return cached, fetched_at
    with _catalog_lock:
        models = _fetch_catalog()
        if models:
            _write_catalog_cache(path, models)
            return models, time.time()
    return None, None


def _price(m: Dict[str, Any], key: str) -> float:
    try:
        return float((m.get("pricing") or {}).get(key) or 0.0) * 1e6
    except (TypeError, ValueError):
        return 0.0


def models_over_ceiling(policy: Dict[str, Any]) -> Tuple[List[str], str]:
    """IDs priced above the ceiling, and a short provenance string.

    Falls back to ``fallback_excluded_models`` (name globs) when no catalogue
    is available at all, so the ceiling degrades to the old name-based cage
    instead of to nothing.
    """
    ceiling = policy.get("price_ceiling") or {}
    try:
        max_prompt = float(ceiling.get("prompt")) if ceiling.get("prompt") is not None else None
        max_completion = float(ceiling.get("completion")) if ceiling.get("completion") is not None else None
    except (TypeError, ValueError):
        max_prompt = max_completion = None
    if max_prompt is None and max_completion is None:
        return [], "no price ceiling"
    ttl = float(policy.get("catalog_ttl_hours") or 24)
    models, fetched_at = load_catalog(ttl)
    if not models:
        fb = [str(x) for x in (policy.get("fallback_excluded_models") or [])]
        return fb, "catalogue unavailable — name fallback"
    key = (max_prompt, max_completion, fetched_at)
    if _excl_cache["key"] == key and _excl_cache["ids"] is not None:
        return list(_excl_cache["ids"]), f"catalogue {time.strftime('%Y-%m-%d %H:%M', time.localtime(fetched_at or 0))}"
    ids = sorted({
        str(m["id"]) for m in models if m.get("id") and (
            (max_prompt is not None and _price(m, "prompt") > max_prompt)
            or (max_completion is not None and _price(m, "completion") > max_completion))
    })
    _excl_cache.update({"key": key, "ids": ids})
    return list(ids), f"catalogue {time.strftime('%Y-%m-%d %H:%M', time.localtime(fetched_at or 0))}"


# ---------------------------------------------------------------------------
# Tiers
# ---------------------------------------------------------------------------

# What each band bought on one representative prompt, 2026-08-29 (docs/routing-policy.md).
TIER_GUIDE: Dict[str, str] = {
    "low":    "cheapest capable models  (~$0.1–0.3/M in)   e.g. deepseek-v4-flash, gpt-5.6-luna, gemini-3.7-flash",
    "medium": "mid-price workhorses     (~$0.5–1.5/M in)   e.g. glm-5.2, deepseek-v4-pro",
    "high":   "frontier, non-premium    (~$2/M in)         e.g. claude-sonnet-5, gpt-5.6-sol/terra",
    "xhigh":  "strongest under ceiling  (~$2–3/M in)       e.g. kimi-k3, gpt-5.6-sol",
    "max":    "no band, ceiling lifted  ($5–10+/M in)      e.g. claude-opus-5, claude-fable-5, gpt-5.5",
}

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


def _ceiling_active(tier: Optional[str], policy: Dict[str, Any]) -> bool:
    """The price ceiling applies unless the tier reached ``ceiling_lifted_at``."""
    lift_at = policy.get("ceiling_lifted_at", policy.get("cage_lifted_at"))
    if tier is None or tier_index(lift_at) < 0:
        return True
    return tier_index(tier) < tier_index(lift_at)


_cage_active = _ceiling_active  # backwards-compatible alias


def exclusion_list(tier: Optional[str], policy: Dict[str, Any]) -> Tuple[List[str], str]:
    """Price-derived exclusions (while the ceiling is active) + name globs (always)."""
    names = [str(x) for x in (policy.get("excluded_models") or []) if str(x).strip()]
    if not _ceiling_active(tier, policy):
        return names, "ceiling lifted"
    priced, prov = models_over_ceiling(policy)
    merged = list(priced)
    for n in names:
        if n not in merged:
            merged.append(n)
    return merged, prov


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
        excluded, _prov = exclusion_list(tier, policy)
        if excluded:
            plugin["excluded_models"] = excluded
        fragment["plugins"] = [plugin]

    ignore = [str(x) for x in (policy.get("ignore_providers") or []) if str(x).strip()]
    provider_prefs: Dict[str, Any] = {}
    if ignore:
        provider_prefs["ignore"] = ignore
    data_collection = str(policy.get("data_collection") or "").strip().lower()
    if data_collection in ("allow", "deny"):
        provider_prefs["data_collection"] = data_collection
    prefer = [str(x) for x in (policy.get("prefer_providers") or []) if str(x).strip()]
    if prefer:
        provider_prefs["order"] = prefer
        provider_prefs["allow_fallbacks"] = True
    if provider_prefs:
        fragment["provider"] = provider_prefs

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
        if fragment["provider"].get("ignore"):
            merged_ignore = list(prov.get("ignore") or [])
            for slug in fragment["provider"]["ignore"]:
                if slug not in merged_ignore:
                    merged_ignore.append(slug)
            prov["ignore"] = merged_ignore
        if fragment["provider"].get("data_collection"):
            prov["data_collection"] = fragment["provider"]["data_collection"]   # policy wins
        if fragment["provider"].get("order") and not prov.get("order"):
            # An explicit per-request/provider-routing order (config.yaml
            # provider_routing or a /model --provider pin) wins over the
            # policy's caching preference.
            prov["order"] = list(fragment["provider"]["order"])
            prov.setdefault("allow_fallbacks", True)
        extra["provider"] = prov
    api_kwargs["extra_body"] = extra

    snapshot = {
        "tier": meta.get("tier"), "source": meta.get("source"),
        "class": meta.get("class"),
        "excluded": list((fragment.get("plugins") or [{}])[0].get("excluded_models") or []),
        "ignore": list(fragment.get("provider", {}).get("ignore") or []),
        "data_collection": fragment.get("provider", {}).get("data_collection") or "",
        "prefer": list(fragment.get("provider", {}).get("order") or []),
    }
    try:
        if getattr(agent, "_routing_last", None) != snapshot:
            _ex = snapshot["excluded"]
            _ex_text = (f"{len(_ex)} models over price ceiling" if len(_ex) > 4 else ",".join(_ex)) or "-"
            logger.info(
                "routing policy: class=%s tier=%s (%s) excluded=%s ignore=%s data_collection=%s model=%s",
                snapshot["class"], snapshot["tier"] or "auto", snapshot["source"],
                _ex_text, ",".join(snapshot["ignore"]) or "-", snapshot["data_collection"] or "-",
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
    ceiling = policy.get("price_ceiling") or {}
    excluded, prov = exclusion_list(tier, policy)
    lines = [
        f"Routing tier: {tier or 'auto'} ({source}); session class {klass}.",
        "",
        "What the bands mean (per-million-token input price, example models):",
    ]
    for name in TIERS:
        mark = "▶" if name == (tier or "") else " "
        lines.append(f"  {mark} {name:6s} {TIER_GUIDE.get(name, '')}")
    lines.append("")
    if ceiling.get("prompt") is not None or ceiling.get("completion") is not None:
        state = "LIFTED for this tier" if not _ceiling_active(tier, policy) else "active"
        lines.append(
            f"Price ceiling: ${ceiling.get('prompt')}/M in, ${ceiling.get('completion')}/M out — {state}; "
            f"{len(excluded)} models excluded ({prov})"
            + (": " + ", ".join(excluded[:6]) + (" …" if len(excluded) > 6 else "") if excluded else "")
            + f". Lifted at tier {policy.get('ceiling_lifted_at', 'max')}."
        )
    else:
        lines.append(f"Price ceiling: none. Name exclusions: {', '.join(excluded) or 'none'}.")
    lines.append(f"Ignored providers: {', '.join(policy.get('ignore_providers') or []) or 'none'}; "
                 f"data_collection: {policy.get('data_collection') or 'allow (OpenRouter default)'}; "
                 f"preferred (caching) providers first: {', '.join(policy.get('prefer_providers') or []) or 'none'}.")
    esc = policy.get("escalation") or {}
    lines.append(
        f"Auto-escalation: {'on' if esc.get('auto', True) else 'off'} "
        f"after {esc.get('failures', 2)} failures, cap {esc.get('max_auto_tier')}; "
        f"steps so far {int(getattr(agent, '_routing_escalation', 0) or 0)}."
    )
    lines.append("Change: /tier low|medium|high|xhigh|max|auto (this session) · /tier reset · file: "
                 + str(policy_path()))
    return "\n".join(lines)
