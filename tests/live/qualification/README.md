# Live qualification driver contract

These tests are skipped unless an operator authorizes the exact clean candidate. They never treat
ambient credentials as authorization.

A complete run needs:

1. a clean committed product tree;
2. an absolute candidate artifact path;
3. an absolute, current qualifying verification receipt outside the candidate tree that binds the
   complete suite, that exact candidate, and that exact artifact;
4. an executable driver in `MEMORY_HARNESS_LIVE_QUALIFICATION_DRIVER`;
5. a disposable `mhq-*` namespace;
6. a random 64-character lowercase hexadecimal nonce and an absolute, single-link regular file
   containing exactly that nonce. No symlink, hardlink, junction, reparse-point, or linked parent is
   accepted; the fixture atomically claims the pathname and destroys the bearer value once;
7. aggregate call, USD-cost, and wall-time budgets. The complete current matrix allocates 37 calls
   and USD 1.90; larger limits are not a reason for a driver to exceed any request's smaller cap.

Example selector (values are illustrative, not reusable):

```text
pytest tests/live/qualification \
  --authorize-live-qualification I_AUTHORIZE_ONE_DISPOSABLE_RUN \
  --live-authorization-nonce <64-lowercase-hex> \
  --live-authorization-file /absolute/path/to/one-use.nonce \
  --live-candidate-artifact /absolute/path/to/candidate.zip \
  --live-verification-receipt /absolute/path/to/qualifying-verification-receipt.json \
  --live-namespace mhq-<disposable-id> \
  --live-call-budget 37 --live-cost-budget-usd 1.90 --live-timeout 1800
```

The driver reads one JSON request from stdin and writes one JSON receipt to stdout. Requests use
`memory-harness-live-qualification/v2`; receipts use
`memory-harness-live-qualification-receipt/v1`. Every receipt must repeat the exact `candidate`
(commit, tree, artifact digest, and qualifying-verification-receipt integrity plus exact byte digest)
and namespace, and report nonnegative `calls` and `cost_usd` within the request. The fixture revalidates the candidate,
artifact, and qualifying receipt immediately before and after every driver invocation. Preflight must
return `{"preflight":{"ready":true}}` with zero service calls/cost.
Every test scenario gets its own namespace and a reserved cleanup call. The fixture invokes a separate
`cleanup` request in `finally`, including after driver failure; cleanup must return exactly:

```json
{"attempted": true, "complete": true, "remaining": []}
```

A skip, timeout, over-budget receipt, identity mismatch, malformed receipt, or incomplete cleanup earns
no LIVE/NATIVE credit. Driver orchestration and credential handling remain environment-owned and are not
part of the deterministic product test suite.
