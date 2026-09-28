# Delegated execution recovery — candidate runbook

Status: isolated candidate; not deployed. Task t_0760634b. No executor, privilege
broker, approval bypass or policy change is included.

## Contract

Project authorization, tool consent and execution capability are separate facts.
`kanban_checkpoint` never executes its `operations`. It records a detector-only
report in the actual worker approval context. An absent interactive channel is
reported as absent; all executable readiness stays **unknown**, never approved.
Unknown lifecycle readiness pauses before deployment for the existing operator
approval channel. Having a credential or reaching a service is not permission.
A fresh operator receipt does not grant permission to retry a denied command.

A worker checkpoint requires `commits`, `dirty_files`, `evidence`,
`detached_operations`, `blockers` (lists), and `current_runtime`,
`intended_runtime`, `next_command`, `rollback`, `owner` (nonblank text).
Use explicit unknowns rather than inventing successful checks. Evidence should
be attachment/commit/test references, never secrets or raw transcript dumps.

The automatic entry checkpoint captures only bounded read-only Git HEAD/status
metadata for the workspace and up to ten immediate child repositories. It does
not inspect arbitrary service state, credentials, file contents, or detached
processes. Unknown runtime/detached state remains a required operator check.
Workers must enrich the checkpoint before risky or long-running work.

## Runtime paths

- An early lifecycle preflight takes exact command, environment, action, units,
  authorization provenance, required capabilities and dependencies. It saves the
  report and pauses; it does not make destructive probes or infer readiness.
- `pause=true` is a run-fenced atomic snapshot and blocked transition. Classes:
  operator_only, auth (missing credentials/access), policy (tool denial),
  transient, budget, worker_death. Credential contents are never required.
- A structured blocked terminal result causes a checkpoint and stops the next
  model iteration. Old transcript denials and file/web quotations do not do so.
  Approval detection/gates themselves are unchanged.
- Entry + near-budget snapshots are independent of the model obeying a notice.
  Near-limit thresholds reuse 90% of finite turns (or configured warning ratio)
  and 80% of runtime. One attention event per agent run uses existing durable
  notification subscriptions. Existing prompt-cache/role order is preserved.
- At iteration exhaustion the exact run pauses, not a generic spawn failure.
  A stale finalizer cannot consume a newer claim. The dispatcher pauses workers
  with saved checkpoints after death/runtime expiry. Legacy workers without
  checkpoints retain the existing bounded retry behavior.
- No new task/daemon is automatically spawned. The requester either bounds the
  remaining work or decomposes it with existing parent/idempotency mechanisms.
  No indefinite budget reset or automatic retry chain exists here.
- Durable event delivery retains existing send/wake retry checkpoints. Exactly
  one acknowledged event is delivered on repeated ticks; like the existing
  notifier, transport acceptance followed by a crash before acknowledgement can
  remain at-least-once. Do not promise network-level exactly-once delivery.

## Operator reconciliation

`kanban_show` includes the saved checkpoint in worker_context. A recovery-blocked
card refuses ordinary unblock. Through the existing orchestrator-only
`kanban_unblock`, supply `recovery` with:

- checkpoint_event_id: the exact most recent checkpoint event;
- observed_at: Unix seconds, within the last 15 minutes, not in the future;
- operator, evidence, runtime_observed, detached_observed, postconditions,
  next_action, authorization_provenance: nonblank receipt strings;
- in_flight: false; postconditions_passed: true.

The receipt is a redacted operator attestation, not an independent service probe
and not executable consent. The operator must actually perform/read back the
checks before providing it. It is persisted atomically with unblock. Missing,
stale, mismatched, or in-flight receipts leave the task blocked. Parent gating
and review provenance survive resume. New worker must reconcile again before
executing its first action; never repeat an operation already performed by the
operator. Do not use a direct status edit/reclaim as a substitute for recovery.

The generic CLI/dashboard unblock controls cannot supply this new receipt yet:
use the orchestrator tool. Their existing false-result behavior is fail-closed.
The operator can inspect full checkpoint detail through kanban_show. No new Argus
control is shipped; existing blocked reason/attention and task details are used.

## Verification and rollout

Run `scripts/run_tests.sh` under a bounded systemd user sibling with isolated
homes. New tests exercise real SQLite, registry handlers, approval classifier/
gate, actual short-lived child exit, watchdog resolution and durable notifier
with a synthetic transport. No real service lifecycle or provider call is used.

Known baseline failure to track separately: routed notifier wrong-owner/anchor
negative test fails on untouched 5afe34715d as well as this candidate (task
 t_d18774f2). Do not hide it or label the entire repository suite green.

Production rollout remains a separate reviewed deployment gate. No dashboard,
gateway, nonproduction service, approval setting, credentials or routing changed
in this task. Rollback before rollout: leave the candidate unmerged. After a
future rollout: reverting code will not delete durable checkpoint events, but
old code does not enforce the new reconciliation gate; assess that before rollback.

## Procedure
1. Read the task, claim/run id, saved checkpoint and operator comments; reconcile
   current Git/runtime and detached work before writing or resuming.
2. Before long lifecycle work, record the exact scoped preflight in the actual
   worker. Keep existing authorization provenance; do not ask for the same
   project decision again, and never substitute it for tool consent.
3. On denial or a missing capability, save complete recovery state and pause once.
   Use the normal approval-capable operator channel, not helpers or alternate
   interfaces. For unknowns, stop safely and identify the owner and exact action.
4. Before unblocking, verify postconditions and no in-flight detached action;
   attach evidence and send the fresh exact-checkpoint reconciliation receipt.
5. Confirm the new task status and claimed run id before reporting work issued.
   Continue only missing work, recheck tool consent, retain cancellation/review
   changes and bound the remaining budget. Report completion only with receipts.
