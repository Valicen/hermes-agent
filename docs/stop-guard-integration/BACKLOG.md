# t_34e7aa4f integration status

- Implemented: approved dda0ca6d source promoted without code edits or conflicts into fresh branch fix/exact-run-stop-t34e7aa4f from 5afe34715d.
- Added: self-contained 10-case run408/run412 regression. Baseline 7 failed/3 passed; integrated stop-guard selection 27 passed/0 failed before commit.
- Scope: complete approved row-37 chain included, including checkpoint/preflight/recovery/reclaim/notifier/loop integration. No from-scratch repair, extraction or unrelated notifier-route repair.
- Required next: exact-head canonical focused and lifecycle/dispatch suites; independent same-card ITO Director review. Receipts accompany Kanban handoff.
- Production: NOT DEPLOYED. Live talaria is 5afe34715d. Separate explicit David approval required for any live merge/deploy/restart. No CHG record because no infra/config/runtime change. No external push in this local-only task.
- Known upstream baseline caveat: notifier route-denial failure in test_kanban_routed_transport.py was independently reproduced on 5afe34715d by source review; separate approved fix 54059296b7 is NOT included here. Broader integration must preserve that separation.
- Canonical Argus/vault files untouched; this scoped FLOWS/BACKLOG is isolated to avoid foreign documentation hotspots.
