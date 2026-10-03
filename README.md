# MA-Harness

A complete orchestration and memory system for teams of coding agents.

## Installation

### Easy personal-project install

After cloning this repository, point the installer at an existing Git project
that has at least one commit. Keep the install root outside both repositories:

```bash
sh install.sh \
  --project /absolute/path/to/your-project \
  --install-root "$HOME/.local/share/ma-harness/my-project"
```

On PowerShell:

```powershell
./install.ps1 `
  --project C:\absolute\path\to\your-project `
  --install-root C:\Users\you\AppData\Local\ma-harness\my-project
```

The command creates an isolated virtual environment, stages a separate operational harness root,
configures local memory with Atlas/EverOS disabled, validates the full setup plan, and calls the one
public `harness setup` boundary. Source installation needs package-index access for the minimum build
prerequisites `setuptools>=68` and `wheel`. Advanced offline environments can repeat
`--build-tool-path` with local setuptools/wheel import roots.

Setup intentionally adds provider hook payloads and `.harness-runtime/` beneath your project. It
preserves/merges supported provider configuration, but is not globally transactional: a late I/O
failure is recorded in `setup-journal.json` as `PARTIAL_SETUP` and may require manual inspection.
Use `--no-setup` to install without changing the target project. The installer never edits `.git`,
`.gitignore`, or other Git metadata; add `.harness-runtime/` to your own ignore policy if desired.

The first `lane launch` in each active epoch automatically opens the live visualizer in a separate
terminal window before provider work starts. Later lanes reuse that epoch decision, so they do not
open duplicate windows. To run a session without automatic visualization, put `--no-visualizer` on
that epoch's first `lane launch`; the opt-out remains in force for the epoch and resets when a new
epoch begins.

Automatic opening is best-effort. A headless host, missing terminal emulator, or rejected desktop
launch never turns a successful lane launch into a failure: ordinary CLI output prints a warning and
the JSON result includes a `visualizer` object. The viewer receives no provider or harness-control
credentials and continues to read the authoritative runtime records every refresh rather than keeping
a parallel state model. The one exception is an explicit `--no-visualizer`: if that opt-out cannot be
stored durably, the CLI refuses to start the provider rather than silently weakening the request.

The install result also prints the exact manual visualizer command. Use it from the reported harness
root whenever automatic opening is unavailable or when you want an additional viewer:

```bash
cd "$HOME/.local/share/ma-harness/my-project/harness"
../venv/bin/orchestrator-harness view
```

Use `view --once --no-color` for a noninteractive snapshot; press `q` in the live viewer to close it.
Re-running the same install command is a
no-op when all owned bytes match. Shut down an OPEN runtime before installing changed source. Drifted
or unowned files are never force-overwritten; inspect them and choose a new install root if needed.
Updates retain a private rollback copy of the prior venv and harness until setup succeeds. If setup
may have changed the target, the current success receipt is withheld and the rollback copy plus
`setup-journal.json` are retained for manual inspection rather than guessing at a rollback.

An install is bound to the selected checkout path so an independent clone cannot silently reuse its
scope. After intentionally moving the checkout (or selecting a linked worktree), repeat the command
with `--relink-project`; the installer preserves the random project identity and rewrites the bound
configuration. It refuses an unexplained path change and will not replace a live independent binding.

### Distribution details

The release is a coordinated pair of distributions. `memory-harness==0.1.0` owns the
`memory_harness` package; `portable-orchestrator-harness==2.0.0` owns the harness and watcher packages
and depends on that exact memory release. Installing the wheels provides the Python packages and CLI,
but an operational run also needs the staged `adapters/`, `super-cache/`, and configuration created by
the easy installer (or equivalent manual setup). The `atlas` and `everos` extras forward to the
corresponding optional memory dependencies.

### External qualification readiness

The following command performs only read-only, presence-only checks. It never contacts services,
executes a provider, consumes a live nonce, or turns readiness into PASS evidence:

```bash
python scripts/qualification_preflight.py --json
```

An installed copy also contains this read-only tool at the `qualification_preflight` command array
printed by the installer, so the source clone is not needed merely to inspect prerequisite readiness.
Actually executing a qualification action does require the candidate checkout named by
`--product-root`; every emitted pytest selector and working directory is bound to that absolute root.

The report covers the 15 outstanding external items and maps all 19 ledger coordinates currently marked
`UNPROVED-PREREQUISITE`. Rows without a real native qualification node remain `design_gap`. The pinned
GitHub Actions workflow can exercise supported Python and native OS coordinates after it is pushed and
run; an unexecuted workflow is not evidence. Learned routing, online learning, and benchmark/performance
claims remain deliberately deferred.

For a live row to become `local_preconditions_ready`, pass the candidate checkout with
`--product-root`, its exact archive with `--artifact`, the qualifying candidate/artifact receipt with
`--verification-receipt`, a shaped one-use nonce file with `--authorization-file`, and the real
`--live-namespace`, `--live-call-budget 37`, `--live-cost-budget-usd 1.90`, and
`--live-timeout 1800`. The configured driver, provider authorization, and service prerequisites must
also be present. Hard-egress and nested-topology rows additionally require trusted, artifact-bound
JSON proof files (`memory-harness-hard-egress-proof/v1` and
`memory-harness-nested-topology-proof/v1`). Each proof must bind both the artifact SHA-256 and the
validated verification receipt's `integrity` value, plus the current OS/platform/architecture and the
resolved executable identity of the provider whose matching authorization is present. The preflight
reads only shapes, hashes, schemas, and presence; it never prints nonce/credential values or executes
the driver.

## Canonical scope and evidence

[`harness/docs/product/FULL_PRODUCT_SPEC.md`](harness/docs/product/FULL_PRODUCT_SPEC.md)
is the reconciled reader for the complete product. It distinguishes the narrow
completed MVP, the full current fixed-strategy product, and deliberately
deferred learned-selection and benchmark candidates. It also records the
closed source manifest, every audited gap (G01–G13), and every binding release
obligation (T01–T29), including native/live/platform coordinates that remain
explicitly unproved rather than being inferred from local mocks.

### Orchestrator Harness

- **Fresh epoch per campaign** — ROOT splits the work into parallel lanes.
- **Isolated lanes** — each lane gets its own Git branch, worktree, task card,
  and provider session, so workers never interfere.
- **Super-cache** — stages the shared tools, hooks, skills, and provider config
  every worker needs.
- **Persistent monitor** — tracks processes, results, leases, and lane health.
- **Read-only terminal visualizer** — shows Recall, Vet, Plan, Pack, Work,
  Review, and Learn progress for every lane without mutating runtime state.
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

## Testing

The complete categorized suite has one entrypoint:

```bash
python scripts/run_tests.py all
```

A release gate can bind its result to the exact candidate and artifact bytes:

```bash
python scripts/run_tests.py all \
  --receipt /path/outside/the/checkout/verification-receipt.json \
  --artifact product-memory-harness-20260926-01.zip
```

The complete `all` selection and at least one declared artifact are required
for a qualifying release receipt. Skipped tests remain explicit unproved
coordinates; a successful available-scope run with skips is labeled
`QUALIFYING-WITH-UNPROVED-COORDINATES`, not an unqualified pass.

See [`TESTING.md`](TESTING.md) for what each category checks, focused commands,
prerequisite and skip semantics, and the exact manifest coverage contract.
