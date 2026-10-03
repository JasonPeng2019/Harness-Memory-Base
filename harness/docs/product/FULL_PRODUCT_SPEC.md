# Full Product Specification and Release Evidence Ledger

**Status:** reconciled reader, not a superseding specification
**Input baseline:** integrated product commit `59f93195784d140c37845fe007a5632c3fff263b`
**Parent source baseline:** `cfae52c2290740edc2ef33a1254c31ee8f5798cd`
**Audit date:** 2026-10-02

## 1. How to read this document

This document makes the full product legible in one place. It reconciles three layers that had
previously been easy to confuse:

1. **Narrow completed MVP.** The two-hour plan proved a coherent local Standard path, accepted-plan
   handoff, bounded context, dispatch, outcome persistence, packaging, and broad regression safety.
   It was an implementation slice, not a statement that the rest of the current product disappeared.
2. **Full current-phase product.** The integrated v65 Feature specification and binding v67
   Implementation contract require fixed Standard, Problem-focused, and Deeper search; durable
   experience/procedure/template memory; reviewed APC; trust/publication/revocation; privacy and
   network modes; outcomes and native usage; feature switches; snapshot/isolation; and native
   product-harness qualification.
3. **Future candidates.** Learned selection, training/update machinery, scored B0–B3 comparisons,
   benchmark cohorts, and leaderboard/submission work remain deliberately deferred.

Normative conflicts are resolved in this order: direct operator request; integrated v65 Feature
requirements; binding v67 clauses; accepted source and tests; integrated v72 and Concepts; component
contracts; the completed MVP as evidence; then archived and parent candidates as historical intent.
A summary here never weakens a fail-closed rule or silently makes a future candidate current.

## 2. Product in one sentence

The product is a durable, trust-aware memory and plan-reuse layer for the native multi-agent coding
harness: ROOT prepares an exact objective, searches eligible evidence and procedures within a bounded
strategy, optionally drafts a near-match plan through a separately bound harness child, accepts an
exact plan and final context, dispatches through the existing harness, and records immutable linked
outcomes, effects, and usage without granting workers control-plane authority.

## 3. Boundaries and owners

| Boundary | Product rule |
|---|---|
| ROOT | Owns objective truth, current plan, route corrections, review, acceptance, and final outcome acceptance. Memory is advisory. |
| Workers/reviewers | Receive bounded worker-specific context. They cannot approve procedures, mutate policy/trust, designate current revisions, or receive control credentials. |
| Native harness | Remains the only launcher, queue/lease owner, process reconciler, correction path, and cleanup owner. The memory layer does not replace it with a scheduler. |
| Memory domain | Owns schemas, identities, local durability, search eligibility, context construction, external-effect journals, outcome links, usage aggregation, and snapshots. |
| EverOS | Optional experience/skill storage reached through a narrow adapter; unavailability stays visible. It is not silently replaced. |
| Atlas | Optional scoped shared procedure/template and hybrid-search backend. Remote acceptance and freshness are explicit; restricted-local mode makes no Atlas task call. |
| APC child | A real native-harness child selected only by the run's explicit `apc_adaptation_binding`. It proposes; ROOT separately accepts or rejects. There is no inherited/default model or direct API fallback. |
| Watcher | Diagnostic-only reader/alerter. It never evaluates, schedules, repairs, acknowledges for ROOT, or operates the harness. |
| Visualizer | Read-only terminal projection over public status. It never mutates lanes, queues, processes, or product state. |

The normal product must run without the outer development-test harness. The nested heavyweight
product-test ROOT and prescribed development model matrix are qualification topology, not deployment
defaults.

## 4. Information model and exact identities

The model distinguishes facts that are often wrongly collapsed:

- **Objective/task card:** exact owner, task, execution base, route, and current-plan identity.
- **Plan:** immutable content with `proposed` or exact ROOT-`accepted` state. A candidate or historical
  template is never execution authority.
- **Decision:** the resolved configuration, strategy, searched sources, selected evidence/templates,
  omissions, budgets, and lineage for one objective.
- **Case:** reviewed trajectory/evidence, including failures and disproved hypotheses. Instruction-like
  case text remains evidence, not approved guidance.
- **Procedure/skill:** immutable revision plus origin, scope, compatibility, approval, designation,
  publication, withdrawal/revocation, and projection evidence. Generated origin never becomes curated.
- **Plan template:** reusable fixed structure, invariants, verification intent, permitted edit surfaces,
  versions, compatibility, and search projection. It is not an accepted current plan.
- **Final context and envelope:** bounded mandatory truth plus optional eligible material, exact omissions,
  privacy provenance, accepted-plan binding, lane/run/worktree/base/checkpoint, and content digest.
- **Dispatch operation:** durable intent before spawn, then exact native acknowledgement or visible
  ambiguity. Lost acknowledgement cannot cause blind duplicate dispatch.
- **Terminal outcome:** exact decision/plan/task/run/review/acceptance evidence. Quality comes from review;
  acceptance cannot convert FAIL/BLOCKED/UNKNOWN into PASS.
- **Effect operation:** intent/ack/reconciliation record for publication, ingestion, telemetry, and other
  externally ambiguous side effects.
- **Native usage:** one started invocation and zero or more source receipts, with requested/resolved/native
  binding, category, stage/window, optional parent adaptation link, measure coverage, and no double count.
- **Snapshot:** integrity-checked, namespace-bound product state plus dependencies, pending operations,
  configuration, and restoration/reconciliation status.

Every write is identity-bound and idempotent only for identical content. Conflicting replay is an
explicit error. Owner/project/app/namespace, objective, plan, decision, task, run, route, base,
checkpoint, content digest, and provider binding are checked where relevant.

## 5. End-to-end lifecycle

1. **Resolve configuration and authority.** Capture feature switches, fixed strategy, network mode,
   backends, privacy policy, budgets, provider bindings, owner/namespace, and current checkpoint.
2. **Capture exact current state.** ROOT provides the objective/task card and current plan. Missing
   mandatory truth blocks; it is never searched or invented.
3. **Bounded preparation.** Standard searches once; Problem-focused derives a sanitized real-problem
   query and falls back when none exists; Deeper performs bounded additional stages. Local-only Deeper
   keeps remote calls suppressed. Fair stage/kind budgets include embedding, fetch, score, APC queue,
   launch, correction, collection, and validation time.
4. **Eligibility and selection.** Search may use lexical/semantic evidence, but exact reads and live or
   frozen freshness validate scope, trust, current designation, revocation, compatibility, conflicts,
   and recipient authorization before use. Duplicate storage cannot boost relevance.
5. **Plan branch.** Direct fill, explicit APC near-match, or fresh ROOT planning. APC may edit only
   permitted surfaces and cannot change fixed steps, invariants, verification intent, authoritative
   facts, ordering, or mandatory plan meaning. Invalid/unavailable APC falls back to fresh planning.
6. **ROOT review.** Candidate identity and corrections are durable. Only exact accepted content proceeds.
   A route correction permanently invalidates old preparation and preserves spent cost.
7. **Finalization.** Construct one bounded context, omitting optional unsafe/oversized pieces rather than
   mandatory truth, and bind it to the accepted plan, task, lane/run, worktree, base, checkpoint, and
   privacy provenance.
8. **Dispatch.** Persist intent before native spawn, record exact requested/resolved binding and native
   acknowledgement, and reconcile ambiguous ownership rather than retrying blindly.
9. **Execution and review.** Workers run in the existing harness. Completion review and ROOT acceptance
   remain distinct. Correction/resume retains exact lineage and never resets budgets without bound.
   The controller signs every attempt with a per-run memory-only capability and publishes the exact
   journal seal only after the provider/helper boundary is gone. This applies equally to valid results
   and cleanup-proven terminal no-result runs; cleanup-unproven runs never receive review authority.
10. **Settlement.** Atomically fix the terminal outcome; then idempotently settle enabled ingestion,
    procedure/template evidence, telemetry, and usage receipts. A retry completes missing side effects
    but never changes quality or invokes deferred learning.
11. **Recovery and cleanup.** Reconcile pending/ambiguous operations, restore only integrity-checked
    namespace-bound snapshots, and retire exact process/worktree/resource identities. Unrelated roots
    are never signaled, waited, deleted, or contaminated.

## 6. Capability surface

| Capability | Current required behavior | Degradation/failure behavior |
|---|---|---|
| Experience memory | Preserve reviewed success/failure, original evidence, recent findings until searchable representation is confirmed, and durable references. | Unlinked history is visibly unverified; unavailable store is reported under its configured policy. |
| Case/skill generation | Generate only from linked reviewed evidence; keep source provenance and trust state. | Missing/ambiguous/unreviewed source IDs block approval; helpers are not auto-installed/executed. |
| Procedure library | Immutable revisions, explicit approval and partition designation, publication receipts, scope/compatibility, revocation and withdrawal. | Approval alone never supersedes; stale publish/designation replay is fenced; cached shared copies cannot bypass freshness. |
| Unified search | Comparable sanitized query/fingerprint/metric, fair store/kind opportunity, deterministic specificity/ties, exact eligibility after retrieval. | Malformed vector/predicate, unknown hard requirement, conflict, revocation, or unavailable required freshness rejects. |
| Fixed strategies | Standard, Problem-focused with no-problem fallback, bounded Deeper, and local-only Deeper. | Unknown budget masks expensive stages; reserve and enclosing deadlines cannot be bypassed. |
| Templates/APC | Five seed families, lower-ranked candidates considered, direct fill/near-match/fresh branches, explicit child binding and receipts. | Invalid/no binding/timeout/forbidden edit becomes fresh planning, never a silent direct API or inherited lightweight model. |
| Context/dispatch | Mandatory truth intact, optional content sanitized/omittable, exact final envelope, intent-before-spawn. | Draft/stale/wrong-bound context blocks; lost acknowledgement becomes ambiguous ownership, not duplicate launch. |
| Outcomes/effects | Exact terminal evidence, idempotent replay, explicit conflicts, retryable enabled side effects. | Missing evidence stays unobserved; genuine terminal UNKNOWN is distinct; child completion is not parent acceptance. |
| Usage/accounting | Count every actual CLI invocation once, retain native counters and binding, link APC support once, expose completeness per measure/category. | Missing/late receipts stay incomplete; conflicting binding/receipt fails closed; outer development costs remain distinct. |
| Privacy/network | Protect authoritative source, sanitize/omit derived content, scrub workers, enforce recipient and egress policy, support restricted-local. | Prohibited mandatory secrets block without rewriting truth; outage/call suppression remains visible; auth stays in transport. |
| Switches | Resolve effective service-side behavior, dependencies, transitions, and pending writes for current features. | Disabled means no call/write, not filtered output; illegal dependencies and deferred selector switches reject. |
| Snapshot/isolation | Integrity, required dependencies, pending state, namespace binding/remap, configuration and accounting metadata. | Corruption/collision/unsafe remap blocks; unrelated namespaces and outer harness state remain unchanged. |
| Operator/observability | Explain strategy, sources, omissions, fallback, trust/freshness, bindings, budgets, outcomes, usage completeness, processes, and cleanup. | Diagnostics cannot mutate or claim success; unsupported/live-unproved coordinates remain explicit. |
| Packaging | Source tree, wheel, sdist, editable install, portable ZIP, assets and CLIs work away from repository cwd. | Missing interpreter/platform/service coordinate is unproved, never inferred from a contract test. |

## 7. Trust, privacy, and network invariants

- Eligibility requires exact immutable content, origin, authorized scope/recipient, compatible environment,
  approval where required, current designation, non-revocation, non-conflict, and fresh validation.
- Approval and designation are different operations. Shared current changes are not claimed before durable
  remote acceptance. Rollback is a new authorized designation, never implicit history selection.
- Revocation blocks all tracked active partitions before managed global completion; withdrawal is narrower.
  Neither erases already delivered exposure, and both fence stale in-flight replay.
- Mandatory authoritative truth is never redacted into a different fact. Unsafe mandatory content blocks.
  Optional derived content is deterministically sanitized or omitted; its digest/projection changes.
- Provider credentials belong only to provider transport. Worker prompts, query/embedding/APC payloads,
  logs, telemetry, cases, skills, templates, and shared publication must not expose control credentials or
  prohibited source content.
- `restricted-local` means no Atlas task call or undeclared external tool/egress. Other claimed modes must
  report the actual launched tools and outages.

## 8. Feature switches and modes

The current fixed selector accepts explicit Standard, Problem-focused, and Deeper choices without policy
files. `PRODUCT_FEATURE_SPEC_v65.md` Section 15 is the single behavioral definition of the current
ordinary-task-path switches. The complete switch surface is exactly:

| Current switch | Disabled behavior |
|---|---|
| Experience read | No optional local historical-case or experience-derived-skill retrieval. |
| Experience write | No new product-owned long-term experience writes; eligible pending non-safety writes pause. |
| Generated-skill creation | No new product-triggered skill generation from current or queued experience. |
| Generated-skill use | No generated skill delivery from local or remote sources. |
| Shared publication | No new non-safety publish, promote, or designate commits, including pending retries. |
| Atlas shared retrieval | No task-time remote procedure reads; local procedures remain available. |
| Plan-template memory | No execution-path template retrieval or APC. |
| APC | No automatic template-to-plan conversion. |
| Light adaptation | No adaptation stage; direct fill and fresh planning remain available. |
| Deeper | The Deeper strategy is ineligible. |
| Learned strategy selection | **Reserved and unavailable in this release**; requests fail as not implemented. |

Service configuration and availability, privacy/network modes, snapshot administration,
telemetry/accounting, and outcome ingestion are policies or operations—not additional feature switches.
Their gates may restrict an enabled request but never silently turn a disabled switch on. All-off is a
real baseline: no implicit memory search/context/write and no implicit curated material.
Transitioning/pending writes are reported truthfully; disabling a service suppresses the call at the
service boundary.

**Learned selector** enablement, provisional training, policy load/inference, updates, replay, and learner
persistence are deferred and must return not-implemented without importing learner machinery. Neutral
strategy, outcome, and usage facts are retained for a future separately authorized phase.

## 9. Verification topology and evidence meanings

Development can use deterministic local unit/contract/integration tests. Native product qualification is
stricter: an outer harness launches a heavyweight isolated product-test ROOT; that ROOT launches real
workers and the separately configured APC child through the candidate harness; evidence retains outer and
inner identities, exact candidate build, requested/resolved roles, real receipts, product review,
acceptance, usage, and cleanup. Mock responses cannot establish native coverage.

- `LOCAL`: real deterministic local implementation/process/storage evidence.
- `CONTRACT`: an external/provider/foreign-OS seam is stubbed or simulated.
- `NATIVE`: actual advertised provider CLI or target operating system.
- `LIVE`: actual configured remote Atlas/EverOS/service behavior.

CONTRACT evidence never counts as NATIVE or LIVE evidence. No ambient credential or environment variable
authorizes a live run. Live qualification additionally needs a clean committed tree, an exact candidate
artifact digest, an atomically consumed 256-bit authorization nonce, exact disposable namespace, driver
preflight, aggregate (not per-test-reset) invocation/cost/time budgets, candidate identity on every request
and receipt, and independently receipted `finally` cleanup for every scenario namespace.
The exact selector and JSON driver protocol are documented in
`tests/live/qualification/README.md`.

## 10. Narrow MVP, integrated candidate, and deferred future

### Narrow completed MVP

The active MVP established the Standard local path, exact accepted-plan precedence, privacy-bounded
context, native dispatch linkage, durable terminal outcome, local/EverOS seam coverage, packaging, and
regression safety. Later integration also repaired finalization/checkpoint, resume, identity clearing,
usage parsing, distribution, visualizer, and watcher behavior. Those facts are evidence, not a reason to
mark broader current requirements optional.

### Full current-phase product

Current scope is the complete fixed-strategy memory/procedure/template/APC product described above,
including trust/publication/revocation, privacy/network, switches, usage, snapshots/isolation, and the
T01–T29 verification obligations. Optional mechanisms and unavailable environments may be conditional,
but core requirements cannot be skipped merely by leaving them disabled.

### Deferred candidates

Benchmark execution, benchmark task/subset/runners, scored B0–B3 comparisons, learned selection,
training/update/provisional policies, benchmark-host cohorts, and performance-improvement claims are not
current release work. Their configuration requests must fail clearly and no automatic post-closeout job
may schedule them.

## 11. Frozen source manifest

The manifest below is the closed source set used for this reconciliation. Parent candidate files and
integrated copies intentionally both appear because their v65/v72 bytes differ. Differences are retained
as provenance; the integrated copies win under the precedence rule.

| Input (relative to parent) | SHA-256 | Class / disposition |
|---|---|---|
| `goal.md` | `9fc8b254a363bb9406b7505521164161186ea4e9555fd2b18824e011631ec122` | operator goal; scope intent |
| `.plans/memory-backed-harness/PLAN.md` | `3d648b49ec93ff232cbcf25417227cb7456ed2856433ea7190db750372498ae7` | completed narrow-MVP execution record, not product authority |
| `.plans/memory-backed-harness/NORMAL_OPERATION_ACCEPTANCE.md` | `0087c7f14182a9187ba423c0cd25d7331c8b82a8d8a283c6ed7de23d753f176e` | operational MVP evidence |
| `.plans/memory-backed-harness/KNOWN_ISSUES.md` | `fa38741e103cf968acdf69a1addd69bd57b38b092e0dc45e9fb1356089bd76bc` | historical limitation ledger; reconcile stale claims |
| `.plans/archive/2026-09-21-r8-plan/PLAN.md` | `1bf10bfeb535e73ae9356cd5a0631167a02bfdd59035251b9aedd18e1ca8bfe8` | archived candidate/history |
| `.plans/archive/2026-09-21-tier4-plan/memory-backed-harness-formal/plan-workflow.md` | `481ac99b3fc2adad946297927dd49aabec82e0f499527123945efd6b5268206e` | archived workflow candidate/history |
| `.plans/archive/2026-09-24-pre-remaining-replan/PLAN.md` | `66db5d958c87331512ea3786c965312eeafb61033454b6e9c0df68d851b0c109` | archived broad candidate/history |
| `.plans/archive/2026-09-24-pre-remaining-replan/specification/SPEC.md` | `5a2c40372f70e5f720462037814622f9c8271c1e379ce19c10d0cbfb752d6980` | explanatory acceptance model |
| `.plans/archive/2026-09-24-pre-remaining-replan/specification/behaviors/BEHAVIOR-01-bounded-preparation-selects-eligible-memory.md` | `e6b52cb976e7b5a2df4e217849607f1d8010ec370f3797c037ce10b201c8ba49` | executable-behavior candidate/history |
| `.plans/archive/2026-09-24-pre-remaining-replan/specification/behaviors/BEHAVIOR-02-reuse-produces-a-root-reviewable-plan.md` | `9947e9f675204d8577ba7cc3b093748a39baf89f9c66f99ea1dabf7bbb9ffa35` | executable-behavior candidate/history |
| `.plans/archive/2026-09-24-pre-remaining-replan/specification/behaviors/BEHAVIOR-03-accepted-plan-dispatches-with-safe-context.md` | `e892ac4112a26a03a4f33f62ee717ca9c16249391be5c270f2d20b0c48b4a702` | executable-behavior candidate/history |
| `.plans/archive/2026-09-24-pre-remaining-replan/specification/behaviors/BEHAVIOR-04-terminal-work-remains-durable-and-accounted.md` | `c0c4acc26f92c16b0ca7d82f75037eaaf7197f7a13df4c04248d8f35158ac71a` | executable-behavior candidate/history |
| `.plans/archive/2026-09-24-pre-remaining-replan/specification/behaviors/BEHAVIOR-05-operators-control-isolated-recoverable-runs.md` | `cf5c521455e6fef404de1006ac59dc08702d34e5abaff904bfaf02a6a25ae31f` | executable-behavior candidate/history |
| `.plans/archive/2026-09-24-pre-remaining-replan/specification/behaviors/BEHAVIOR-06-integrated-candidate-proves-the-real-product-path.md` | `3df5e578217dabcfe44809b62d05cff36b1a87cd77ed49fd7f79f210c0a9b791` | executable-behavior candidate/history |
| `new_harness_memory_docs/PRODUCT_FEATURE_SPEC_v65.md` | `fabf3665e74695d17857c8f99998c6fad38376ba3dd838f275a6afb310a22cbe` | parent candidate; retain differences explicitly |
| `new_harness_memory_docs/PRODUCT_IMPLEMENTATION_SPEC_v67.md` | `98b6cb55eb1057390e64aee1fbbbbb38ccbfa39da5486bf68f60731c7ee81730` | binding implementation/verification source |
| `new_harness_memory_docs/PRODUCT_SPEC_DETAILED_v72.md` | `dcd6e884f6f16b2284a3793293ed7c64b2af784fe01e91d97da56fd37e49ad55` | explanatory/source-observation candidate |
| `new_harness_memory_docs/PRODUCT_CONCEPTS.md` | `fbea351dc325ea72dc80410cf3fef03196c9dc3e34eceb6070a4dca3d6ffa033` | parent explanatory glossary candidate |
| `product/harness/docs/product/PRODUCT_FEATURE_SPEC_v65.md` | `64c3c8a6e9df5cef09b69f4b56d965995e354a133afbc8da3f88830920f622ce` | canonical integrated Feature behavior |
| `product/harness/docs/product/PRODUCT_IMPLEMENTATION_SPEC_v67.md` | `98b6cb55eb1057390e64aee1fbbbbb38ccbfa39da5486bf68f60731c7ee81730` | canonical binding verification |
| `product/harness/docs/product/PRODUCT_SPEC_DETAILED_v72.md` | `c9e12bf461998d70b272fb24336408541c3e7d4669c280ee2939d92199b56f39` | integrated explanatory/source ledger |
| `product/harness/docs/product/PRODUCT_CONCEPTS.md` | `ca4ca03a3f4750f00e4c9fcb5c6f54915c9c520a0a95084e8485d6d138874fbf` | explanatory glossary |
| `product/harness/orchestrator_harness/SPEC.md` | `c1b93417ceaf5fdec71ab707425a9072e92b28e17668d0d9a78d4f8de6464bd7` | operational harness contract |
| `product/harness/orchestrator_harness/PROVIDER_NETWORK_PAYLOAD.md` | `428f8f87ba89247ec75454b684850a6121424a97d9e182acf4f26111ac016af2` | operational privacy/network contract |
| `product/harness/harness_watcher_implementation/SPEC.md` | `9b388d7da2ebd620d064deaf4fd01c1a7f8ce35bf4650802c76a512ea49470a9` | diagnostic watcher contract |
| `product/harness/README.md` | `8f6889918cdfc6239b77416c4ea039d5179152099a11a0cd0adb3b1d4f8234bb` | accepted visualizer/operator contract |
| `product/harness/QUICK_START.md` | `578c296fc52fe59299b9640c59de38b0b938592c585eaed7f75b6d9a7ee7cd43` | accepted visualizer launch/interaction contract |
| `product/harness/REUSE_MANIFEST.md` | `7d7538bbb993f8d49a18fdb8731d50fdee64df903ece255cf714323374a0e770` | accepted component/reuse inventory |

The table freezes the input bytes at the baseline, including the original historical
`.plans/memory-backed-harness/KNOWN_ISSUES.md`. Reconciliation deliberately produced a distinct current
output at that same path with SHA-256
`c69582dccbd5540b58114e69e570613b74ca8152dfe6a844a7a392f74fbd4b83`; it is not substituted into the
input manifest. Machine-readable coordinate evidence is in
[`VERIFICATION_LEDGER.json`](VERIFICATION_LEDGER.json). Each entry gives an exact executable node,
evidence class, prerequisite, positive oracle, negative oracle, and one canonical `EXISTING-SUFFICIENT`, `NEW-LOCAL-PASS`, or
`UNPROVED-PREREQUISITE` status. The coordinate sets below are machine-checked for exact parity with that ledger.

## 12. Frozen 2026-10-02 gap ledger

Every row has `closure=CLOSED` when each declared coordinate is either evidenced or explicitly unproved.
`UNPROVED-PREREQUISITE` is an honest open qualification coordinate, not a passing result.

| Gap | Closure, coordinate status, executable evidence, and remaining prerequisite |
|---|---|
| G01 | closure=CLOSED; coordinates={LIVE=`UNPROVED-PREREQUISITE`} — `tests/live/qualification/test_current_pin_lifecycle.py` requires separately authorized disposable Atlas, EverOS, and provider resources at the exact candidate pin. |
| G02 | closure=CLOSED; coordinates={CONTRACT=`EXISTING-SUFFICIENT`; NATIVE=`UNPROVED-PREREQUISITE`} — `tests/local/preparation/test_step04_native_apc_child.py`; `tests/live/qualification/test_native_apc_matrix.py` requires authorized real provider CLIs. |
| G03 | closure=CLOSED; coordinates={LOCAL=`NEW-LOCAL-PASS`} — `tests/local/usage/test_harness_usage_reconciliation.py` covers attempt-to-ledger receipt conversion, replay, cumulative observations, incomplete/late/conflict behavior, and immutable quality; `harness/orchestrator_harness/tests/test_step08_native_review_evidence.py` runs a cleanup-proven failed controller through accepted UNKNOWN review and durable native-usage settlement. |
| G04 | closure=CLOSED; coordinates={LOCAL=`NEW-LOCAL-PASS`} — `tests/local/integration/test_fixed_strategy_lifecycle.py` joins Standard, Problem-focused, Deeper, local-only, and all-off lifecycle behavior. |
| G05 | closure=CLOSED; coordinates={LOCAL=`NEW-LOCAL-PASS`; LIVE=`UNPROVED-PREREQUISITE`} — `tests/local/recovery/test_snapshot_qualification.py`; `tests/live/qualification/test_current_pin_lifecycle.py` exposes the separately authorized real remote-restore selector. |
| G06 | closure=CLOSED; coordinates={CONTRACT=`NEW-LOCAL-PASS`; LOCAL=`NEW-LOCAL-PASS`; LIVE=`UNPROVED-PREREQUISITE`} — `tests/local/privacy/test_provider_payload_qualification.py` proves local child payload/scrubbing and restricted zero-call; real egress needs authorization. |
| G07 | closure=CLOSED; coordinates={NATIVE-LINUX=`NEW-LOCAL-PASS`; NATIVE-MACOS=`UNPROVED-PREREQUISITE`; NATIVE-WINDOWS=`UNPROVED-PREREQUISITE`} — `tests/platform/test_native_process_contracts.py` executes locally and exact-skips foreign hosts. |
| G08 | closure=CLOSED; coordinates={CONTRACT-WINDOWS=`NEW-LOCAL-PASS`; LOCAL-POSIX=`NEW-LOCAL-PASS`} — `harness/orchestrator_harness/tests/test_view_interactive.py` covers PTY/key/resize/restore/malformed/changing/large state. |
| G09 | closure=CLOSED; coordinates={LOCAL=`NEW-LOCAL-PASS`} — `harness/harness_watcher_implementation/tests/test_watcher_operational.py` and `harness/harness_watcher_implementation/tests/test_watcher_smoke.py` cover both cwds, cycles, burst, abrupt stop, and stale identity. |
| G10 | closure=CLOSED; coordinates={LOCAL-CPYTHON-3.12=`NEW-LOCAL-PASS`; LOCAL-CPYTHON-3.11=`UNPROVED-PREREQUISITE`; LOCAL-CPYTHON-3.13=`UNPROVED-PREREQUISITE`; LOCAL-CPYTHON-3.14=`UNPROVED-PREREQUISITE`} — `tests/distribution/test_install_matrix.py` plus `scripts/build_portable_archive.py`; unavailable interpreters remain unqualified. |
| G11 | closure=CLOSED; coordinates={LOCAL=`NEW-LOCAL-PASS`} — `tests/local/stress/test_runtime_fault_matrix.py` covers bounded concurrent replay, pressure, abrupt child death, atomicity, and unrelated sentinels. |
| G12 | closure=CLOSED; coordinates={LOCAL=`NEW-LOCAL-PASS`} — `tests/local/contracts/test_deferred_scope.py` proves learner and benchmark absence/fail-closed requests. |
| G13 | closure=CLOSED; coordinates={LOCAL=`NEW-LOCAL-PASS`} — `tests/local/contracts/test_full_product_spec.py` freezes sources, precedence, scope layers, G01–G13, and T01–T29; parent limitation docs are reconciled. |

## 13. Binding T01–T29 release ledger

This is a coverage index, not a replacement for v67 §15.2. A row can close locally while retaining an
explicit unproved NATIVE/LIVE coordinate.

| Test | Closure, status, and concrete evidence |
|---|---|
| T01 | closure=CLOSED; coordinates={LOCAL=`NEW-LOCAL-PASS`; NATIVE=`UNPROVED-PREREQUISITE`} — `tests/local/mvp/test_coherent_memory_path.py`, `tests/local/integration/test_fixed_strategy_lifecycle.py`, `tests/distribution/test_install_matrix.py`; the separately authorized native baseline selector is `tests/live/qualification/test_current_pin_lifecycle.py::test_current_pin_baseline_lifecycle`. |
| T02 | closure=CLOSED; coordinates={LOCAL=`EXISTING-SUFFICIENT`} — `tests/local/contracts/test_step04_handoff_plan_state.py`, `tests/local/contracts/test_contracts.py` reject stale/wrong task, objective, route, base, or plan identity. |
| T03 | closure=CLOSED; coordinates={LOCAL=`EXISTING-SUFFICIENT`} — `tests/local/preparation/test_final_context_dispatch.py`, `harness/orchestrator_harness/tests/test_launch_lifecycle.py` separate finalized, dispatched, and ambiguous launch state. |
| T04 | closure=CLOSED; coordinates={LOCAL=`EXISTING-SUFFICIENT`} — `tests/local/experience/test_reviewed_trajectory.py`, `tests/local/experience/test_ingestion_and_trust.py` preserve referenced history and prevent evidence from becoming instructions. |
| T05 | closure=CLOSED; coordinates={LOCAL=`EXISTING-SUFFICIENT`} — `tests/local/experience/test_generated_trust.py`, `tests/local/experience/test_step04_generated_skill_scope_lookup.py` enforce exact provenance, review, curated selection, and no helper execution. |
| T06 | closure=CLOSED; coordinates={LOCAL=`EXISTING-SUFFICIENT`} — `tests/local/experience/test_generated_trust.py`, `tests/local/privacy/test_privacy.py`, `tests/local/preparation/test_step04_procedure_revision_selection.py` enforce revisions, coherent compact payloads, and digest meaning. |
| T07 | closure=CLOSED; coordinates={LOCAL=`EXISTING-SUFFICIENT`} — `tests/local/contracts/test_contracts.py`, `tests/local/experience/test_step04_generated_skill_scope_lookup.py` cover owner/project/app/namespace and recipient identity. |
| T08 | closure=CLOSED; coordinates={LOCAL=`EXISTING-SUFFICIENT`} — `tests/local/preparation/test_step04_procedure_revision_selection.py`, `tests/local/procedures/test_atlas_external_reconciliation.py` distinguish approval/designation and fence stale acknowledgement. |
| T09 | closure=CLOSED; coordinates={LOCAL=`NEW-LOCAL-PASS`} — `tests/local/experience/test_generated_trust.py`, `tests/local/effects/test_external_reconciliation.py`, `tests/local/experience/test_everos_external_reconciliation.py`, `tests/local/procedures/test_atlas_external_reconciliation.py` enforce revocation/withdrawal replay fences. |
| T10 | closure=CLOSED; coordinates={LOCAL=`EXISTING-SUFFICIENT`} — `tests/local/preparation/test_reuse_planning.py`, `tests/local/preparation/test_step04_procedure_revision_selection.py` cover eligibility, predicates, conflicts, duplicates, specificity, and ties. |
| T11 | closure=CLOSED; coordinates={LOCAL=`EXISTING-SUFFICIENT`} — `tests/local/preparation/test_final_context_dispatch.py`, `tests/local/preparation/test_step04_procedure_revision_selection.py` cover live/frozen freshness and post-search revocation. |
| T12 | closure=CLOSED; coordinates={CONTRACT=`EXISTING-SUFFICIENT`; LOCAL=`EXISTING-SUFFICIENT`; NATIVE=`UNPROVED-PREREQUISITE`} — `tests/local/preparation/test_step04_native_apc_child.py`, `tests/local/preparation/test_step04_template_shortlist_coherence.py`, `tests/live/qualification/test_native_apc_matrix.py`. |
| T13 | closure=CLOSED; coordinates={LOCAL=`EXISTING-SUFFICIENT`} — `tests/local/preparation/test_step04_local_search_adapters.py`, `tests/local/preparation/test_step04_atlas_search_adapters.py`, `tests/local/privacy/test_privacy.py` enforce representation consistency and projection revision. |
| T14 | closure=CLOSED; coordinates={LOCAL=`EXISTING-SUFFICIENT`} — `harness/orchestrator_harness/tests/test_product_corrections.py` covers route correction, stale preparation, lineage, and spent budget. |
| T15 | closure=CLOSED; coordinates={LOCAL=`NEW-LOCAL-PASS`} — `tests/local/preparation/test_bounded_preparation.py`, `tests/local/preparation/test_step04_search_deadline.py`, `tests/local/integration/test_fixed_strategy_lifecycle.py` cover every fixed strategy and enclosing budgets. |
| T16 | closure=CLOSED; coordinates={LOCAL=`NEW-LOCAL-PASS`} — `tests/local/contracts/test_config.py`, `tests/local/integration/test_fixed_strategy_lifecycle.py`, `tests/local/contracts/test_deferred_scope.py` cover switches, dependencies, suppression, transitions, and deferred requests. |
| T17 | closure=CLOSED; coordinates={CONTRACT=`NEW-LOCAL-PASS`; LOCAL=`NEW-LOCAL-PASS`} — `harness/orchestrator_harness/tests/test_provider_network_payload.py`, `tests/local/privacy/test_provider_payload_qualification.py` constrain credentials and authority. |
| T18 | closure=CLOSED; coordinates={CONTRACT=`NEW-LOCAL-PASS`; LOCAL=`NEW-LOCAL-PASS`; LIVE=`UNPROVED-PREREQUISITE`} — `tests/local/privacy/test_privacy.py`, `tests/local/privacy/test_provider_payload_qualification.py` cover protected data through derived/provider surfaces. |
| T19 | closure=CLOSED; coordinates={LOCAL=`NEW-LOCAL-PASS`} — `tests/local/contracts/test_terminal_outcome.py`, `tests/local/usage/test_harness_usage_reconciliation.py` cover exact outcomes, replay/conflict, and post-outcome usage settlement. |
| T20 | closure=CLOSED; coordinates={LOCAL=`NEW-LOCAL-PASS`} — `tests/local/effects/test_atomic_experience_settlement.py`, `tests/local/effects/test_local_effect_state.py`, `tests/local/recovery/test_snapshot_qualification.py`, `tests/local/stress/test_runtime_fault_matrix.py` cover crash and concurrency boundaries. |
| T21 | closure=CLOSED; coordinates={LOCAL=`NEW-LOCAL-PASS`} — `tests/local/integration/test_fixed_strategy_lifecycle.py`, `tests/local/contracts/test_deferred_scope.py` prove fixed strategies without learner code and rejection of learner operations. |
| T22 | closure=CLOSED; coordinates={LOCAL=`NEW-LOCAL-PASS`; NATIVE=`UNPROVED-PREREQUISITE`} — `tests/local/usage/test_native_usage.py`, `tests/local/usage/test_harness_usage_reconciliation.py`, `tests/live/qualification/test_native_apc_matrix.py` cover completeness, binding, and single counting. |
| T23 | closure=CLOSED; coordinates={LOCAL=`NEW-LOCAL-PASS`} — `tests/local/recovery/test_snapshot_restore.py`, `tests/local/recovery/test_snapshot_qualification.py` cover integrity, dependencies, pending state, collision, and namespace isolation. |
| T24 | closure=CLOSED; coordinates={CONTRACT=`NEW-LOCAL-PASS`; LOCAL=`NEW-LOCAL-PASS`; LIVE=`UNPROVED-PREREQUISITE`} — `harness/orchestrator_harness/tests/test_provider_network_payload.py`, `tests/local/atlas/test_live_fixture_control.py`, `tests/local/privacy/test_provider_payload_qualification.py`. |
| T25 | closure=CLOSED; coordinates={LOCAL=`NEW-LOCAL-PASS`; LIVE=`UNPROVED-PREREQUISITE`; NATIVE=`UNPROVED-PREREQUISITE`} — `harness/orchestrator_harness/tests/test_step04_lifecycle_integration.py`, `harness/orchestrator_harness/tests/v2_acceptance/test_claim_map.py`, `harness/orchestrator_harness/tests/v2_acceptance/test_scenario_map.py`, `tests/live/qualification/test_current_pin_lifecycle.py`. |
| T26 | closure=CLOSED; coordinates={CONTRACT=`EXISTING-SUFFICIENT`; NATIVE=`UNPROVED-PREREQUISITE`} — `tests/local/preparation/test_step04_native_apc_child.py`, `tests/live/qualification/test_native_apc_matrix.py` cover explicit binding and fail-closed fallback. |
| T27 | closure=CLOSED; coordinates={LOCAL=`NEW-LOCAL-PASS`; NATIVE=`UNPROVED-PREREQUISITE`} — `harness/orchestrator_harness/tests/test_real_agent_isolation.py`, `harness/orchestrator_harness/tests/v2_acceptance/test_gap_map.py`, `tests/local/stress/test_runtime_fault_matrix.py` cover owned-tree separation. |
| T28 | closure=CLOSED; coordinates={CONTRACT=`EXISTING-SUFFICIENT`; NATIVE=`UNPROVED-PREREQUISITE`} — `harness/orchestrator_harness/tests/test_native_attempt_receipts.py`, `harness/orchestrator_harness/tests/test_root_provider_hooks.py`, `tests/live/qualification/test_native_apc_matrix.py`, `tests/live/qualification/test_current_pin_lifecycle.py`. |
| T29 | closure=CLOSED; coordinates={LOCAL=`NEW-LOCAL-PASS`} — `tests/local/contracts/test_deferred_scope.py`, `tests/local/contracts/test_full_product_spec.py` reject benchmark work and automatic scheduling. |

## 14. Release interpretation

A row marked `NEW-LOCAL-PASS` is credited only after its named node passes at the recorded candidate.
A clean clone can reproduce the local evidence without credentials. Native provider, live service, foreign
OS, and absent-interpreter coordinates stay `UNPROVED-PREREQUISITE` until their exact opt-in selectors run
on the real coordinate and retain receipts. No local green suite changes that status.

## 15. Verification recorded for this closure pass

The 2026-10-03 independently audited repair candidate derived from baseline
`316b909e4a9595eedee2e5a532c85036701495fd` recorded:

- root product suite: **804 passed, 18 skipped**;
- harness suite: **660 run: 648 passed, 12 skipped**, including cache-recovery, v2 acceptance, and PTY viewer tests;
- watcher suite: **104 passed** from both the product-root and harness-root working directories;
- unified suite: all **12 categories** passed through `python scripts/run_tests.py all`, totaling
  **1,556 passed and 30 explicitly skipped**, with all **123 discovered test modules** assigned exactly once;
- distribution matrix: deterministic ZIP, wheel, sdist, editable install, and CPython 3.12 passed;
  CPython 3.11/3.13/3.14 skipped because those executables were absent;
- portable archive: **1,520 files**, clean CRC, and exact source parity; the digest is recorded outside
  the archive in the assembly handoff so the archive does not contain a self-referential checksum;
- serial `compileall`, diff whitespace checks, and changed-file secret-pattern checks passed.

The root skips remain explicit prerequisite coordinates: nine separately authorized live/provider/service
selectors, three platform/CI-host selectors, four absent/undeclared interpreter coordinates, and two
optional vendored-EverOS nodes. Ruff, Pyright, and basedpyright were unavailable. No current-pin LIVE
provider, Atlas/EverOS, remote restore, foreign-host, or absent-interpreter claim was made.
