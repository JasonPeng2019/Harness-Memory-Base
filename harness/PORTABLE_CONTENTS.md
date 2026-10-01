# Portable contents

Snapshot date: 2026-10-01.

## Included

- Complete production Python code from `orchestrator_harness/`.
- The read-only terminal visualizer and its public `view` command.
- Complete shared `harness_common/` process-identity code.
- Complete production Python code and schema from `harness_watcher_implementation/`.
- Harness and watcher unit/integration tests that are self-contained in this portable tree.
- Example configuration, setup instructions, specifications, attention-logging documentation, and
  testing guides.
- A disposable coding integration fixture with two worktree lanes, one contended named resource,
  stale/valid result paths, a merge lane, native event acknowledgement, and cleanup.
- One static harness configuration fixture required by a copied unit test.
- Build metadata at `harness/pyproject.toml` plus the executable live-matrix
  examples required by wheel and source distributions.

## Deliberately excluded

- `multi-agent-logs/`, `harness_watcher/<epoch>/`, and `fresh-experiments/`.
- Canary state directories, generated JSONL, snapshots, pending notifications, caches, and
  `test_results/`.
- Historical sprint plans, reviews, checkpoints, handoffs, verdicts, and repair prompts.
- The AI watcher-subagent relay model and all collaboration-notification machinery.
- Obsolete owner/watcher wrappers and their repository-history-only test.
- External provider service code and experiment artifacts; they are observed targets, not harness
  or watcher dependencies.

The portable runtime generates new state only under paths selected in local configuration. The
release archive is generated from version-controlled product files, so ignored runtime evidence,
build products, and caches are not copied into it.
