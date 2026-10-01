# MA-Harness

A complete orchestration and memory system for teams of coding agents.

### Orchestrator Harness

- **Fresh epoch per campaign** — ROOT splits the work into parallel lanes.
- **Isolated lanes** — each lane gets its own Git branch, worktree, task card,
  and provider session, so workers never interfere.
- **Super-cache** — stages the shared tools, hooks, skills, and provider config
  every worker needs.
- **Persistent monitor** — tracks processes, results, leases, and lane health.
- **Live coordination** — workers report progress or request help via queue
  notifications that ROOT answers without restarting sessions.
- **Full result lifecycle** — evidence tied to an exact lane and run, a reviewer
  verdict (pass/fail/blocked) separate from ROOT's accept/reject, plus resume,
  correction, retirement, and identity-exact cleanup.

### Memory feedback loop

- **Trajectories** — each reviewed task is recorded as a trajectory.
- **EverOS** — turns reviewed experience into historical cases and reusable skills.
- **Trust system** — verifies skill origin and promotes approved skills into
  **trusted procedures**: immutable versioned records with permissions,
  applicability rules, current-version status, and revocation.
- **Shared discovery** — trusted procedures publish to **MongoDB Atlas** and are
  found via real Vector Search.
- **Task flow** — a new task searches EverOS (local experience) and Atlas (shared
  procedures), validates every result, builds a safe task-specific plan and worker
  context, launches a real coding worker, and feeds the reviewed outcome back in.
- **Fully optional** — with every memory feature disabled, the harness still
  works normally.

### Benchmark

- **SWE-Marathon v1.1 (ZSTD)** — 47.3% above the official *high* average and 7.3%
  above the official *xhigh* average, using 91% fewer uncached tokens than xhigh
  (11.14× lower), though runs took longer. These are project-reported figures;
  the supporting campaign artifacts are not included here for independent
  verification.

## `memory_harness` package

This repo's core deliverable is `src/memory_harness/` — the Stage-A Python
package implementing the deterministic memory and plan-reuse contracts. Its
guiding principle is **determinism and fail-closed safety**: the same inputs
yield the same decision, side effects are recorded before they happen, and any
capability that cannot be authorized is refused rather than approximated.

- **Canonical records** — task, plan, decision, dispatch, operation, and outcome
  objects with stable content hashes, so identical work is reused, not recomputed.
- **Bounded prepare, one safe dispatch** — a single fault-isolated search before
  any optional call, then at-most-once execution with explicit reconciliation.
- **Durable SQLite store** — the single source of truth for provenance and
  approval evidence, with verifiable snapshot/restore.
- **Privacy guards** — credentials and known secrets are kept out of query
  payloads and persisted records.
- **Gated integrations** — EverOS (reviewed experience) and MongoDB Atlas
  (trusted-procedure vector search) are optional and skip cleanly when absent.
