# Exact-run stop guard characterization — t_34e7aa4f / run416

2026-09-28, before source integration. Scope: isolated worktree only; the live Argus FLOWS.md is intentionally unchanged.

Baseline: 5afe34715d9d7469311c0088442777ee88d40891.
Canonical runner: scripts/run_tests.sh -j 1 tests/agent/test_kanban_stop_run408_sequence.py -s --tb=short, bounded sibling systemd unit stopguard-416-red2.
Observed: 7 failed, 3 passed, exit 1. Receipt: ../red-corrected.txt relative to integration worktree.

- Real tool dispatch of successful review returned ok=true/status=review/run_id=408 but the old guard failed to recognize its terminal result.
- On a synthetic SQLite board seeded with run408, real review handoff closes run408; real claim_review_task then claims run412 with source_status=review. The baseline incorrectly nudges the old implementer despite its terminal run and successor ownership.
- Successful changes_requested and checkpoint pause result contracts fail terminal recognition. These two baseline probes test parser contracts; actual checkpoint tool is absent at baseline. Full approved-source tests will exercise real checkpoint pause after integration.
- Refused complete/block calls incorrectly suppress unfinished-work detection; refused review/rework/checkpoint calls retain a nudge (3 passing cases).
- An absent board is falsely described as running. The candidate must read without creating it and state unknown ownership conservatively.
- Initial test fixture requested an uninstalled reviewer name, causing two setup-path failures. Corrected to the isolated default profile and reran before accepting red evidence; red.txt is superseded by red-corrected.txt.

Integration decision: preserve the complete already-approved source chain ending dda0ca6d665b48407cfe94858009d5c3a6bfcded. It descends directly from the unchanged live release. All row-37 checkpoint/recovery/preflight/notifier/loop changes are included deliberately so actual pause and recovery tests remain meaningful without a new implementation. This is NOT a stop-guard-only patch or production rollout. No approval code, credentials, services, canonical vault records or historical live incident rows are changed by this task.
