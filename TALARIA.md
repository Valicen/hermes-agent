# Talaria — Valicen's carried changes on top of upstream Hermes

This branch (`talaria`) is upstream `NousResearch/hermes-agent` plus a SMALL
stack of Valicen commits, rebased onto upstream by `talaria sync`
(`~/argus/ops/talaria.py`). This file is the ledger a future conflict
resolver reads first. Policy (David, 2026-08-28): **"better stays"** — when
upstream reworks an area we touched, take upstream and drop or re-express
our commit, UNLESS the commit is Valicen-specific and upstream still lacks
it. Every entry below states the drop condition, so the decision is
mechanical.

| # | Commit (subject) | Why we carry it | Files | Drop when upstream… |
|---|---|---|---|---|
| 1 | agent: auto-compaction on local endpoints + routed-model/token accounting | zeus llama.cpp (local, fixed window) must auto-compact even when `compression.enabled` is false; served-model + cache/reasoning token accounting feeds Argus spend views | agent/agent_init.py, agent_runtime_helpers.py, chat_completion_helpers.py, conversation_loop.py | auto-compacts on local endpoints by itself AND records the routed/served model per turn |
| 2 | mcp: schema-cache TTL fix (our PR #97410 closed as duplicate of upstream **#92621**, the more complete fix — drop the TTL hunk when #92621 merges), lazy tool loading, OAuth loopback relay | mcp 2.x SDK writes `ttl_ms=0` → upstream treats as instant expiry → every lookup MISS (CHG-23); OAuth refresh-token preservation; headless loopback relay | tools/mcp_schema_cache.py, mcp_tool.py, tool_search.py, mcp_oauth.py | `_entry_expired` (or equivalent) treats non-positive ttl as "no hint"; refresh token survives reauth; loopback works headless |
| 3 | desktop: served-model chip (commit subject says "timestamps" — misnamed) | Shows which model actually served a turn (openrouter/auto routes per call) in the desktop app, paired with #6 | apps/desktop/src/** (15 files, incl. i18n `turnModel`) | desktop shows the served model per turn natively |
| 4 | cron: post-persistence completion events | Consumers get a durable report path after `save_job_output` instead of racing it (Argus/n8n notice pipelines depend on it) | cron/scheduler.py, tests/cron/test_completion_event.py | emits a completion event after persistence |
| 5 | cli: plugin-toolset validation race fix (desktop-entry half DROPPED 2026-09-05 — upstream now resolves the venv interpreter itself) | `validate_toolset` raced background plugin discovery ("unknown toolset"); exempts keys the non-blocking plugin helper knows | cli.py | validation consults persisted plugin keys during discovery |
| 6 | tui: report the model that actually served the turn | `_last_served_model` in session.info payload → Argus/desktop served-model display | tui_gateway/server.py | session.info carries the served model |
| 7 | code-skew: fingerprint the checked-out TREE, not the commit | Branch flips with identical trees are not stale code (5h picker outage 2026-08-27) — **PR NousResearch#97407** | gateway/code_skew.py, tests/test_code_skew.py | compares tree content. Related upstream: #66999 (reload on real drift) — complementary |
| 8+9 | kanban notifier: deliver for profiles that cannot host the platform (passive-only) | Worker-profile subs on a platform only the root gateway hosts were unclaimable forever — **PR NousResearch#97408** | gateway/kanban_watchers.py, tests/gateway/test_kanban_notifier.py | delivery falls back to a platform-hosting gateway (or subscribe-time validation rejects). Related upstream: #81143 (transport-owner stamping), #76514/#76483 (CLI stamping) — they reduce the need but do not replace this |
| 10 | kanban: clean exit with a completion comment routes to review, not retry | A worker that posts its result as a comment then exits without `kanban_complete` was retried until the budget blocked a DONE task (t_a5199403). Now: run outcome `unconfirmed_completion`, task → `review` via `request_review` (wakes the subscriber) — **PR NousResearch#97409** | hermes_cli/kanban_db.py (`detect_crashed_workers`), tests/hermes_cli/test_kanban_unconfirmed_completion.py | treats a comment-bearing clean exit as a review handoff instead of a protocol violation. Prior art upstream: #50288 (auto-completes to done with heuristics; we route to review) |
| 11 | dashboard: `kanban.changed` global event (board movement) | Lets Argus (any client) refresh on change instead of polling the board every 60s; read-only sqlite probe of task_events per board, heartbeats folded out — **PR candidate** | tui_gateway/server.py (`_kanban_sig`, `_kanban_changed_payload`, `_CHANGE_WATCHES`), tests/tui_gateway/test_kanban_change_watch.py | broadcasts kanban task events to dashboard clients |
| 12 | OpenRouter routing policy: price ceiling on `openrouter/auto` (catalogue-derived exclusions), cost band per session class, `/tier` session override, auto-escalation on failures | 30-day audit 2026-08-29: auto sent 10.6% of requests to Opus = 59% of $371, all via the no-cache Bedrock endpoint. Fleet file `~/.hermes/routing-policy.yaml` (hot-reloaded); every catalogue model above `price_ceiling` ($3/$15 per M — Opus, Fable, GPT-5.5, *-pro, o1…) goes out as `plugins[auto-router].excluded_models` until `/tier max`; `cost_tier` band + `provider.ignore` per request; classes cron/kanban/subagent/interactive; steps up one band after N failures — see docs/routing-policy.md. **Valicen-specific, not for upstream** | agent/openrouter_routing.py, agent/conversation_loop.py (3 hooks), hermes_cli/commands.py (`tier`), cli.py, hermes_cli/cli_commands_mixin.py, hermes_cli/cli_agent_setup_mixin.py, gateway/{run,session_state,slash_commands}.py, tests/agent/test_openrouter_routing.py, docs/routing-policy.md | ships per-request Auto Router constraints or a session tier command natively — re-express on top, drop the overlap |
| 14 | routing: per-cron-job / per-kanban-task band pins + routing-ledger.jsonl | Hooks for the nightly loops (~/argus/ops/loops): `cron_jobs.<id>` / `kanban_tasks.<id>` in routing-policy.yaml, resolved before the class default; every decision change appended to `~/.hermes/logs/routing-ledger.jsonl`. Valicen-specific | agent/openrouter_routing.py (`work_unit`, `_write_ledger`), tests/agent/test_openrouter_routing.py | n/a (part of row 12) |
| 15 | kanban: block-loop breaker parks in triage → auto-specifier/decomposer must wait for a human | The breaker routes a re-blocking card to `triage` "for a human-in-the-loop decision", but `triage` is the column the auto-specifier and auto-decomposer sweep: an LLM re-promoted the card within one tick and the worker re-blocked it (t_a169ceb8: 25 cycles / 101 Director calls / ~3M input tokens in 25 min, 2026-09-03). Now: `kanban_db.loop_parked()` (latest `block_loop_detected` with no non-automation comment since) makes `list_triage_ids()`/`specify_task()`/`decompose_task()` skip the card; the breaker posts a `loop-breaker` comment as the visible marker; a human comment (e.g. `--author david`) releases it — **PR candidate** | hermes_cli/kanban_db.py, hermes_cli/kanban_specify.py, hermes_cli/kanban_decompose.py, tests/hermes_cli/test_kanban_specify.py | …guards the specifier/decomposer against loop-parked cards (or stops routing loop-breaks to `triage`) |
| 16 | tui_gateway: process-level orphan kanban notifier — deliver tui subscriptions for idle (no-socket) sessions via a headless resumed CLI turn | The per-session poller only runs while a session's socket is open; Argus opens a socket per send, so a task settling between turns never reached the requesting chat (t_5733e873 and every Chief-of-Staff tui sub since 08-24: cursor stuck at creation). Now a per-gateway thread claims events for THIS profile's idle tui sessions and runs `hermes -p <profile> chat --resume <session> -Q -q <notice>` (same store the gateway reads); failures rewind the cursor; live sessions untouched; `HERMES_TUI_ORPHAN_NOTIFY=0` disables — **PR candidate** | tui_gateway/server.py, tui_gateway/entry.py, tests/tui_gateway/test_orphan_kanban_notifier.py | …delivers platform=tui notify subs without a live socket (or wakes the owning session headlessly) |

## Sync 2026-09-05 (CHG-20260905) — where the rows live after upstream's Sep-2026 decomposition

Upstream split its god-files on 2026-09-02/03 (kept alive by ONE compat-shim commit, 2776813df3,
"revert on the announced date"). Every carried hunk was re-expressed on the new shape; a future
resolver should look here first, not in the files the commit subjects name:

| Row | Now lives in |
|---|---|
| 1 | `agent/agent_init.py` (`_parse_compression_config` local-endpoint override), `agent/agent_runtime_helpers.py` (auto-router envelope cache), `agent/chat_completion_helpers.py` (reasoning-mandatory recovery + `display_metadata.served_model`), **`agent/turn_usage.py`** (`_last_served_model`, routed log line, priced at the served model) |
| 2 | `tools/mcp_schema_cache.py`, `tools/mcp_tool.py` (ttl hint loop), `tools/mcp_oauth.py` (refresh-token preservation), `tools/tool_search.py` (empty-args) |
| 4 | `cron/scheduler.py`: `_RunDelivery.completion_event_error`, hook inside `_save_compose_deliver`, folded into `_finish_completed_run` |
| 6 | **`tui_gateway/prompt_turn.py`** (message.complete payload) |
| 7 | `gateway/code_skew.py` (`_tree_fingerprint`, now under `noninteractive_git_env()` — GHSA-7x36-8jrh-v4pw class) |
| 8+9 | **`gateway/kanban_watchers_notifier.py`** (`_profile_hosts_platform`, `_Collector._claim_fallback_subs`, passive-only downgrade in `_KanbanNotification.__init__`, fallback adapter in `deliver`) |
| 10 | **`hermes_cli/kanban_db_dispatch.py`** (`_DeadWorker.unconfirmed_completion`, `_completion_comment_for_run`, `_CrashSweep.review_candidates`, request_review handoff in `detect_crashed_workers`) |
| 11 | **`tui_gateway/change_watcher.py`** (`_kanban_sig`, `_kanban_changed_payload`, `_CHANGE_WATCHES["kanban.changed"]`) |
| 12/14 | `agent/openrouter_routing.py` (ours), hooks in **`agent/turn_api_request.py`** / `turn_api_error.py` / `turn_empty_response.py`; `/tier` in `hermes_cli/cli_commands_mixin.py` + **`gateway/slash_commands_model.py`** (auto-dispatched by name); session plumbing in `gateway/run_config_loaders.py`, `gateway/run_turn_runner.py`, `gateway/session_state.py`; dashboard `config.set key=tier` in **`tui_gateway/methods_config_set.py`** (`_set_tier`), `_routing_tier_label` + `_make_agent` pin in `tui_gateway/server.py`, /new clearing in `tui_gateway/agent_callbacks.py` |
| 15 | `hermes_cli/kanban_db.py` (`loop_parked`, breaker comment inside `block_task`), `kanban_specify.py`, `kanban_decompose.py` |
| 16 | `tui_gateway/server.py` (block appended before the split-module imports), started from `entry.py`, `ws.py`, `hermes_cli/main.py` |

Tool renames upstream (todo→todo_list, cronjob→cronjob_manage, process→process_manage) are
alias-mapped in `model_tools._LEGACY_TOOL_ALIASES` / `_LEGACY_TOOLSET_MAP`; profile configs were
left on the legacy names deliberately (aliases hold; row 13 normalizes its own list).

## Upstream PRs open

#97407 skew guard · #97408 notify fallback · #97409 unconfirmed completion → review. (#97410 schema-cache TTL closed 2026-08-28 as a duplicate of #92621 — watch that one instead.) When one merges, the next `talaria sync` will report the corresponding commit as already-upstream: skip it and delete its row.

## Conflict playbook

1. `talaria status` first: the conflict forecast names overlapping files.
2. Rebase happens in `/tmp/talaria-sync-worktree`; the live tree is untouched
   until tests pass there.
3. On a conflict in a file above: read the "Drop when" column. If upstream now
   satisfies it → `git rebase --skip` that commit and delete its row here. If
   not → keep our hunk minimal on top of upstream's new shape; prefer
   upstream's structure, re-express our intent.
4. Add/add conflicts at the end of test files: keep both blocks (upstream first).
5. `talaria sync --from-worktree` to validate, test, move, restart, push.
6. Keep this table honest in the same commit as any change to the stack.

Sync history: see `~/argus/ops/logs/talaria-sync.log` and the
`talaria-pre-sync-*` tags (rollback points).

## Commit identity for upstream PRs

Upstream's `Contributor Attribution Check` fails any PR whose commits carry an
email with no mapping in `contributors/emails/`. Earlier commits on this branch
were authored `david@devon.localdomain` (git had no identity set on devon);
PRs #97407/#97408/#97409 carry a mapping file for that email. Since 2026-08-28
devon's global git identity is the GitHub noreply address
(`289773754+valicen-davidsaunders@users.noreply.github.com`), which upstream
auto-resolves with no mapping file. Never rewrite authorship on already-opened PRs.

### Upstream review of #97408 (2026-08-28, andrexibiza)

Two P1s: (1) `.env` absence is not authoritative — upstream `_getenv()` also reads
`secret_scope`/`os.environ`; (2) fallback selects a platform, not an exact bot
identity, and `_collect()` counted `_profile_adapters`-only platforms as hosted
while the send site uses `self.adapters`. Decision: keep the fallback in Talaria
(our fleet: one bot per platform, tokens in `.env`), fix (2)'s send-boundary bug
locally (`fallback_platforms` = `self.adapters` only; test
`test_no_fallback_claim_when_platform_hosted_only_by_secondary_profile`), and
park #97408 as draft to restack on #81143/#76514 once they land.
