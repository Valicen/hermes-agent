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
| 5 | cli/desktop-entry: plugin-toolset validation race + venv-symlink desktop entry | `validate_toolset` raced background plugin discovery ("unknown toolset"); desktop entry Exec= survives venv symlinks (ops/upstream-hermes-desktop-entry-venv-symlink.md) | cli.py, hermes_cli/linux_desktop_entry.py | validation consults persisted plugin keys during discovery; Exec resolves the interpreter |
| 6 | tui: report the model that actually served the turn | `_last_served_model` in session.info payload → Argus/desktop served-model display | tui_gateway/server.py | session.info carries the served model |
| 7 | code-skew: fingerprint the checked-out TREE, not the commit | Branch flips with identical trees are not stale code (5h picker outage 2026-08-27) — **PR NousResearch#97407** | gateway/code_skew.py, tests/test_code_skew.py | compares tree content. Related upstream: #66999 (reload on real drift) — complementary |
| 8+9 | kanban notifier: deliver for profiles that cannot host the platform (passive-only) | Worker-profile subs on a platform only the root gateway hosts were unclaimable forever — **PR NousResearch#97408** | gateway/kanban_watchers.py, tests/gateway/test_kanban_notifier.py | delivery falls back to a platform-hosting gateway (or subscribe-time validation rejects). Related upstream: #81143 (transport-owner stamping), #76514/#76483 (CLI stamping) — they reduce the need but do not replace this |
| 10 | kanban: clean exit with a completion comment routes to review, not retry | A worker that posts its result as a comment then exits without `kanban_complete` was retried until the budget blocked a DONE task (t_a5199403). Now: run outcome `unconfirmed_completion`, task → `review` via `request_review` (wakes the subscriber) — **PR NousResearch#97409** | hermes_cli/kanban_db.py (`detect_crashed_workers`), tests/hermes_cli/test_kanban_unconfirmed_completion.py | treats a comment-bearing clean exit as a review handoff instead of a protocol violation. Prior art upstream: #50288 (auto-completes to done with heuristics; we route to review) |
| 11 | dashboard: `kanban.changed` global event (board movement) | Lets Argus (any client) refresh on change instead of polling the board every 60s; read-only sqlite probe of task_events per board, heartbeats folded out — **PR candidate** | tui_gateway/server.py (`_kanban_sig`, `_kanban_changed_payload`, `_CHANGE_WATCHES`), tests/tui_gateway/test_kanban_change_watch.py | broadcasts kanban task events to dashboard clients |

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
