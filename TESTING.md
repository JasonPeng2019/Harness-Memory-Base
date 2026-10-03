# Test suite guide

The project has one categorized test entrypoint:

```bash
python scripts/run_tests.py all
```

The command above is written relative to the product root. The runner itself is working-directory
independent when invoked by its path: it resolves the product root from its own location, uses the
current Python interpreter, supplies absolute `src` and `harness` import roots, and runs every declared
category once. It preserves the native frameworks: product tests use pytest; orchestrator harness and
watcher tests use unittest discovery.

The child environment deliberately removes ambient `PYTEST_ADDOPTS` and `PYTEST_PLUGINS`. Host-level
pytest options or injected plugins therefore cannot add selectors, turn a category into collect-only,
or smuggle command-line authorization into the live session; the runner's manifest and argv remain the
collection authority. Ordinary inherited environment (including explicit product prerequisites) remains
available to the tests themselves.

The source of truth is [`tests/suite_manifest.json`](tests/suite_manifest.json). A contract test
independently scans all supported test roots and fails if
a module is missing, assigned twice, or placed outside the declared roots.

## Categories

| Category | Modules | What it examines | Special requirements |
|---|---:|---|---|
| `contracts` | dynamic | Record schemas, configuration, runtime dispatch, terminal outcomes, deferred-feature boundaries, qualification readiness, live authorization gates, full-product specification integrity, and this suite manifest/runner. | Deterministic local. |
| `memory` | 19 | Experience ingestion, trajectory review, trust, generated skills, procedure revisions, revocation/withdrawal, Atlas/EverOS adapters, and idempotent local/external effects. | Optional adapters are exercised through their local contracts unless explicitly configured. |
| `planning` | 15 | Bounded preparation, local/Atlas/EverOS search adapters, plan reuse, templates and APC, final context, provider binding, correction lineage, deadlines, and captured configuration. | Deterministic local/contract evidence. |
| `lifecycle` | 4 | Coherent MVP paths and joined Standard, Problem-focused, Deeper, local-only, and all-off lifecycles through accepted outcome settlement. | Optional service coordinates skip or use local substitutes according to their contracts. |
| `privacy` | 2 | Redaction identity, protected-source preservation, prompt/query/payload scrubbing, recipient authorization, transport-only credentials, and restricted-local call suppression. | No live provider authorization is introduced. |
| `recovery` | 3 | Snapshot consistency, digest/collision/isolation rules, restore, concurrency and retry pressure, abrupt failures, atomic publication, and unrelated-resource preservation. | Uses bounded local processes and temporary stores. |
| `usage` | 2 | Native provider usage parsing, binding, completeness, cumulative versus incremental receipts, deduplication, replay, late settlement, and immutable quality outcomes. | Deterministic native-event fixtures. |
| `platform` | 1 | Exact process identity, termination, reaping, PID-reuse safety, and platform-specific qualification. | Runs the native-host coordinate; foreign OS coordinates skip explicitly. |
| `distribution` | dynamic | Easy installer, checked-ZIP byte parity, deterministic complete-product ZIP coverage, extracted-archive offline builds, coordinated two-wheel/two-sdist/editable installation, non-overlapping package ownership, exact dependency/extras, an accepted-plan lifecycle through the installed public harness boundary, CLI smoke, and supported CPython coordinates. | Missing interpreters skip explicitly; a stale checked ZIP fails until deliberately regenerated after source stabilization. |
| `live` | 3 | Current-pin Atlas, EverOS, native-provider, remote snapshot, privacy/egress, APC, baseline, and nested-harness qualification. | One indivisible pytest session. Without the separate one-use authorization, candidate artifact, disposable namespace, budgets, driver, and credentials, nodes skip before setup. |
| `harness` | 50 | Controller/lane lifecycle, queues, leases, hooks, review and acceptance, attempt attestation, process cleanup, isolation, packaging, visualizer behavior, and all v2 acceptance maps. | Native unittest discovery under `harness/`. Platform/provider prerequisites may skip. |
| `watcher` | 5 | Diagnostic ingestion, attention records, CLI validation, start/stop/status, both supported working directories, burst handling, abrupt restart, and exact PID reaping. | Native unittest discovery; evaluator remains disabled. |

The module counts describe the current manifest. They are explanatory only—the executable coverage
contract discovers the files dynamically instead of trusting these numbers.

## Common commands

List the available categories and their current descriptions:

```bash
python scripts/run_tests.py --list
```

Run one category or several categories in the requested order:

```bash
python scripts/run_tests.py contracts
python scripts/run_tests.py privacy usage recovery
```

Show exact native commands without running them:

```bash
python scripts/run_tests.py --dry-run all
```

Normally the runner continues after a failing category so the summary is complete. To stop before
starting the next category:

```bash
python scripts/run_tests.py --fail-fast all
```

Repeated category names and mixing `all` with a named category are rejected; this prevents accidental
double execution.

## Candidate-bound release receipts

The final local release gate can write a receipt outside the candidate tree and bind one or more
artifacts:

```bash
python scripts/run_tests.py all \
  --receipt /tmp/ma-harness-verification.json \
  --artifact product-memory-harness-20260926-01.zip
python scripts/verification_receipt.py \
  --require-qualifying /tmp/ma-harness-verification.json
```

`--artifact` is repeatable and is valid only with `--receipt`. Before starting any category, the
runner hashes every Git-tracked or nonignored untracked candidate file and every declared artifact.
It hashes them again after the run. Addition, deletion, or byte mutation makes the receipt
`NONQUALIFYING` and the command exits nonzero. The receipt records the Git commit, per-file and
aggregate SHA-256 values, exact category argv, durations, return statuses, interruption and fail-fast
omissions, and structured framework counts. Pytest produces JUnit XML; unittest runs through the
project-owned structured-result runner. Missing or malformed structured output never qualifies.

Environment evidence is intentionally allowlisted to interpreter, host, architecture, working
directory, and presence-only booleans for named optional prerequisites. Values of every named
prerequisite—including Atlas URIs that can embed credentials—and all credential-like environment
variables are redacted from structured skip details. Credential values and the rest of the ambient
environment are not serialized. Receipt publication is atomic, including failed or interrupted runs.

Qualification has deliberately narrow semantics:

- only the complete ordered `all` selection can qualify, and it must declare at least one release
  artifact;
- every category must execute a nonzero number of tests, have at least one substantive passing test,
  produce internally consistent pass/fail/error/skip totals, and exit successfully;
- zero-test, all-skipped, interrupted, malformed-result, fail-fast, mutated-candidate, and
  mutated-artifact runs are `NONQUALIFYING`;
- individual skipped tests are projected into `unproved_coordinates` with their exact category,
  test identity, and reason. When every available executable check passes but skips remain, the
  global status is `QUALIFYING-WITH-UNPROVED-COORDINATES`, never an unqualified pass. It qualifies
  only the available executed scope; every listed skipped coordinate remains unproved.

A named-category receipt may still be useful diagnostic evidence, but is intentionally
`NONQUALIFYING` as a release receipt. The validator recomputes the current candidate and artifact
digests, structured-count invariants, category coverage, unproved projection, qualification reasons,
and receipt integrity.

The deterministic portable artifact is regenerated only after the candidate sources are final:

```bash
python scripts/build_portable_archive.py \
  --output product-memory-harness-20260926-01.zip
```

## Result and prerequisite semantics

- A category passes only when its native child command exits successfully.
- The unified command exits nonzero if any category fails. Interruption exits with status 130.
- Skips remain skips. The runner does not reinterpret a missing interpreter, foreign operating system,
  optional provider, or unavailable external service as a pass.
- `all` invokes the complete `live` tree in one pytest process, preserving its session-scoped one-use
  nonce and aggregate call/cost/time budgets. The runner never creates live authorization.
- An explicitly authorized live campaign should continue to use the documented direct pytest command
  and live qualification flags, because those values are deliberately not inferred or forwarded by
  the safe default aggregator.

## Native framework equivalents

The aggregator is a convenience and coverage gate, not a replacement for focused debugging:

```bash
# All root pytest categories in one traditional invocation
PYTHONPATH=src:harness python -m pytest -q tests

# Orchestrator harness
PYTHONPATH=src:harness python -m unittest discover -q \
  -s harness/orchestrator_harness/tests -t harness

# Diagnostic watcher
PYTHONPATH=src:harness python -m unittest discover -q \
  -s harness/harness_watcher_implementation/tests -t harness
```

Use focused node selectors while developing; use `python scripts/run_tests.py all` for the complete
local release gate.
