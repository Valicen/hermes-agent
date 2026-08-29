# OpenRouter routing policy (Talaria)

*Valicen fork feature, 2026-08-29. Code: `agent/openrouter_routing.py`; policy
file: `~/.hermes/routing-policy.yaml` (one file for the whole fleet); session
command: `/tier`; tests: `tests/agent/test_openrouter_routing.py`.*

## The problem it solves

Every Valicen profile runs `openrouter/auto`. Over the 30 days to 2026-08-28
the OpenRouter bill was **$371**, of which **$277 (76%) was Claude Opus** — a
model nobody chose. Three facts explain it:

1. **`openrouter/auto` is a model chooser, not a price optimiser.** OpenRouter
   documents it as routing on "the wisdom of the market": a lightweight
   classifier assigns one of ~30 task types and the router picks the model the
   community *spends the most on* for that type over a trailing 7-day window.
   Agentic/engineering-shaped prompts → Opus. 10.6% of our requests, 59% of
   the dollars.
2. **Provider competition happens one layer down and is untouched by this.**
   For a given model, OpenRouter's default is price-weighted load balancing
   across the providers that serve it ("lowest-cost candidates, weighted by
   inverse square of price"). Sales, 75%-off providers, new entrants — all of
   that still applies to whatever model the router picks.
3. **Caching is per provider, and the price sort ignores it.** Our shape is
   ~76k tokens of prefix per call and ~360 tokens of output, with a 90%
   cache-hit rate when caching works; losing the cache is a 5–8× cost
   multiplier, far larger than any provider discount. Measured 2026-08-29
   (two identical calls, `cached_tokens` on the second): Z.AI, Anthropic via
   Google *and* via Bedrock, OpenInference/Novita/SiliconFlow (DeepSeek) all
   cache; Io Net and DigitalOcean (cheap resellers the price sort likes)
   return nothing. *Correction:* the first draft of this doc blamed
   `amazon-bedrock` for "zero cache reads" — that came from unresolved
   `openrouter/auto` accounting rows in the local DB, not from billing;
   Bedrock caches normally and is no longer ignored.

## What OpenRouter lets us control per request (characterised live 2026-08-29)

| Request field | Effect | Verified |
|---|---|---|
| `plugins: [{id: "auto-router", cost_tier: <band>}]` | Auto Router picks inside a cost band. **A band, not a ceiling**: models cheaper than the band are excluded too. | low→`deepseek-v4-flash`, medium→`glm-5.2`, high→`claude-sonnet-5`, xhigh→`kimi-k3`/`gpt-5.6-sol`, max→`claude-opus-5` (same prompt) |
| `plugins[auto-router].excluded_models` (globs) | Router never picks these | `anthropic/claude-opus*` excluded at xhigh → `gpt-5.6-sol` |
| `plugins[auto-router].allowed_models` (globs) | Router only picks these | `anthropic/*` → `claude-sonnet-5` |
| `provider: {order: [...], allow_fallbacks: true}` | Try these providers first for whatever model is chosen | first-party-first order + auto medium → glm-5.2@Z.AI, cached on call 2; low → deepseek-flash@OpenInference, cached |
| `provider: {ignore: [...]}` | Endpoint never used, for any model | works with auto; **default empty** (see correction above) |
| `provider: {data_collection: "deny"}` | Excludes endpoints that may retain prompts/completions | every band and every model we use still served (sonnet-5→Google, glm-5.2→Makora, deepseek-flash→Reka, kimi-k3→Together); **on by default since 2026-08-29** |
| `provider: {max_price: {...}}` | Applied **after** the model choice; if the chosen model's endpoints all exceed it the request **fails** (`404 No endpoints found that satisfy the max price`) | **Not used** — it fails instead of degrading |

## What the policy does

On every API call that goes to OpenRouter (`agent/conversation_loop.py`,
right after `_build_api_kwargs`, before request middleware):

1. **Load** `routing-policy.yaml` from the fleet root (`~/.hermes`, even when
   `HERMES_HOME` is a profile directory). Cached on mtime — edits apply on the
   next call, no restart. A malformed file logs a warning and falls back to
   the built-in defaults; the policy can never break a turn.
2. **Classify the session once** — `cron` (platform `cron`), `kanban`
   (`HERMES_KANBAN_TASK` set), `subagent` (delegated child), otherwise
   `interactive` (desktop, dashboard, Telegram, CLI). The class is cached on
   the agent object and never re-evaluated.
3. **Resolve the tier**, highest priority first:
   `/tier` session override → env `HERMES_ROUTING_TIER` → class default from
   the file (+ automatic escalation steps).
4. **Emit** `plugins[auto-router]` (only when the model is `openrouter/auto`;
   a pinned model gets no plugin) with `cost_tier`, `allowed_models`, the
   **price-ceiling exclusion list** (see below; unless the tier reached
   `ceiling_lifted_at`), and `provider.order` (caching providers first),
   `provider.data_collection`, `provider.ignore` for *every* OpenRouter model.
   An explicit provider order already on the request (config.yaml
   `provider_routing`, `/model --provider`) is left alone.
   Existing `plugins` / `provider` entries in the request are merged, not
   clobbered.
5. **Log** one INFO line per session (and again whenever the decision
   changes): `routing policy: class=kanban tier=medium (class kanban)
   excluded=31 models over price ceiling ignore=amazon-bedrock model=openrouter/auto`.

### The price ceiling (why not a list of names)

A name list ("exclude Opus") is too specific — Claude Fable 5 is $10/$50,
GPT-5.5 is $5/$30, the `*-pro` and `o1` tiers go up to $150/$600, and new
premium models appear monthly. So the guard is a **price**:
`price_ceiling: {prompt: 3.0, completion: 15.0}` ($ per million tokens).
On each call the module takes OpenRouter's public catalogue
(`/api/v1/models`, cached in `~/.hermes/routing-policy.models-cache.json`,
refreshed in the background every `catalog_ttl_hours`) and passes every
model priced above either number as `excluded_models`. Today that is 31
models. 3/15 keeps sonnet-5 ($2/$10), gpt-5.6-sol/terra ($2/$10–12) and
kimi-k3 ($3/$15); it drops opus-5 ($5/$25), fable-5 ($10/$50), gpt-5.5
($5/$30) and everything above. Verified live: `/tier max` *with* the
ceiling still yields kimi-k3; with the ceiling lifted, opus-5.

If the catalogue has never been fetched and cannot be (offline), the module
falls back to `fallback_excluded_models` name globs (opus, fable, gpt-5.5,
*-pro, o1*, …) and logs a warning — it degrades to the old name cage, never
to "no cage". `excluded_models` name globs are applied on top, always, even
at `max`.

### What the bands mean

`/tier` prints this table. Bands are OpenRouter's, characterised on one
representative prompt on 2026-08-29; the *input* price is what matters for
our prefix-heavy shape.

| Band | Roughly | Examples |
|---|---|---|
| `low` | cheapest capable, ~$0.1–0.3/M in | deepseek-v4-flash, gpt-5.6-luna, gemini-3.7-flash |
| `medium` | mid-price workhorses, ~$0.5–1.5/M in | glm-5.2, deepseek-v4-pro |
| `high` | frontier, non-premium, ~$2/M in | claude-sonnet-5, gpt-5.6-sol/terra |
| `xhigh` | strongest under the ceiling, ~$2–3/M in | kimi-k3, gpt-5.6-sol |
| `max` | no band **and the ceiling is lifted**, $5–10+/M in | claude-opus-5, claude-fable-5, gpt-5.5 |

The name "tier" is OpenRouter's (`cost_tier`); it is a *price/quality band*,
not reasoning effort (`/reasoning` is a separate command). If the vocabulary
grates, the command is one `CommandDef` entry and can be aliased.

### Why the tier is per *session*, not per turn

The original wish was "the exactly appropriate model per turn". We
deliberately did not do that: 90% of our tokens are cached prefix, and
OpenRouter's cache is per model+provider. A session that flips models
between a fresh user ask and its tool-result continuations re-bills its
whole prefix at every flip. Per-session bands keep the cache warm; the
per-turn intelligence stays inside the band with the Auto Router.

## Where you set it: pickers, not slash commands

Chats hosted by the dashboard (Argus, the desktop app) send everything —
including a typed `/tier` — to the model as a prompt; the gateway slash
dispatcher only sees Telegram/Discord/etc. and the CLI. So the dashboard
exposes the tier exactly like the model and fast mode: **`config.set
key=tier value=<band|auto|reset|status>`** (session-scoped; the pin lives in
`create_routing_tier_override` and `_make_agent` re-applies it on lazy
builds and `/new`). `status` returns the effective tier, its source, the
price-ceiling summary and the band guide; `session.info` carries
`routing_tier`. Argus renders this as a **Tier dropdown next to the chat
model** (label shows the effective band; "policy default" = whatever the
file says for an interactive session; `max` is spelled out as "ceiling
lifted — premium models"). `/tier` remains for CLI and messaging gateways.

## Automatic escalation (the "ideally automatic" part)

`note_failure()` is called from the conversation loop on every API-error
retry and every empty-response retry. After `escalation.failures` of them in
one session (default 2) the session steps **up one band** and the counter
resets; it can keep stepping until `escalation.max_auto_tier` (default
`xhigh` — automation never reaches Opus). The step is logged at WARNING:
`routing policy: 2 api_error failures in session — escalating tier low →
medium (cap xhigh)`. Explicit `/tier` overrides are never escalated. The
kanban dead-handoff watchdog and Argus night watch both read the journal, so
repeated escalations surface as receipts without any new plumbing.

## Loosening the belt

Ordered from fastest/most local to most global. All are reversible.

| When you want… | Do this | Scope | Takes effect |
|---|---|---|---|
| A stronger model for *this* conversation | `/tier high`, `/tier xhigh` | this chat session (cleared by `/new`, `/tier reset`) | next call |
| Premium models (Opus, Fable, GPT-5.5 …) for this conversation | `/tier max` — lifts the price ceiling | this session | next call |
| The router to choose freely under the ceiling | `/tier auto` | this session | next call |
| Back to policy default | `/tier reset` | this session | next call |
| See what applies right now | `/tier` or `/tier status` | — | — |
| A whole class stronger (e.g. all kanban workers) | edit `tiers.kanban: high` in `routing-policy.yaml` | fleet | next call, every profile |
| One profile stronger | `profiles.<name>.tiers.interactive: xhigh` in the file | that profile | next call |
| Opus/GPT-5.5 allowed but not Fable/pro tiers | `price_ceiling.prompt: 6` in the file | fleet (or per profile) | next call |
| No ceiling at all | `price_ceiling: {prompt: null, completion: null}` | fleet / profile | next call |
| Back to OpenRouter's full provider pool (privacy off) | `data_collection: allow` | fleet / profile | next call |
| Pure price-weighted provider choice (sales first, caching be damned) | `prefer_providers: []` | fleet / profile | next call |
| Automation allowed to reach premium models | `escalation.max_auto_tier: max` | fleet / profile | next call |
| One process pinned regardless of file | `HERMES_ROUTING_TIER=xhigh` in that profile's `.env` | that profile's gateway | restart that gateway |
| Everything off, exactly the old behaviour | `enabled: false` in the file, **or** `HERMES_ROUTING_POLICY=off` in a profile's `.env` | fleet / one profile | next call / restart |

Tightening is the same table in reverse; the defaults shipped in the file
are the 2026-08-29 decision: cron/subagent `low`, kanban `medium`,
interactive `high`, premium models only via `/tier max`, Bedrock never.

## Observability

* `journalctl --user -u 'hermes-gateway*' | grep 'routing policy'` — one line
  per session decision, WARNING lines for escalations.
* `/tier` in any chat prints the effective tier, its source, the cage state,
  the ignore list, escalation settings and steps taken.
* OpenRouter activity (`ops/argus-nightly-selftest.py::_openrouter_activity`)
  shows the model actually served; the night watch's spend tripwire remains
  the backstop.

## Failure modes and what happens

| Situation | Behaviour |
|---|---|
| Policy file missing | built-in defaults (same as the shipped file) |
| Catalogue stale | served as-is, refreshed once in a background thread |
| Catalogue missing and unreachable | name-glob fallback list + WARNING |
| Policy file malformed | WARNING + defaults; inference continues |
| Any exception inside the policy | WARNING `routing policy: skipped (...)`; the request goes out unmodified |
| Model is not `openrouter/auto` | no auto-router plugin; `provider.ignore` still applied |
| Route is not OpenRouter (zeus, Anthropic direct) | untouched |
| Band has no available endpoint for the prompt | OpenRouter picks another model in the band; it does not fail (unlike `max_price`) |

## Upstream posture

Valicen-specific by construction (fleet file layout, our tier defaults), so
it is carried in Talaria (ledger row 12) rather than proposed upstream. If
upstream ever ships per-request Auto Router constraints or a session tier
command, re-express this on top of it and drop the overlapping parts.
