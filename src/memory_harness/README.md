# memory_harness

`memory_harness` is the Stage-A Python package for deterministic memory- and
plan-reuse contracts.  It provides:

- canonical task, plan, decision, dispatch, operation, and outcome records;
- an integrity-bound parent execution envelope;
- durable SQLite decision, operation, and outcome storage;
- privacy guards for control credentials and known synthetic secrets;
- fixed Standard/Problem-focused/Deeper configuration with an explicit
  `deferred/not implemented` boundary for learned selection;
- versioned local templates with direct-fill and fresh-plan fallback;
- a thin APC request/result contract that requires an explicit
  `apc_adaptation_binding`;
- deterministic reviewed-experience and Atlas fixture adapters.

The package intentionally does **not** provide a second launcher, scheduler,
review system, evidence ledger, secret manager, learned selector, or benchmark
runner.

## Optional EverOS reviewed-experience adapter

The EverOS adapter is an optional Python 3.12+ dependency.  It uses only the
public `memorize`, `search`, `SearchRequest`, and `MemoryRoot` imports, while
the local SQLite store remains authoritative for ROOT review receipts,
provenance, uncertainty, and approval evidence.

From a source checkout containing `harness/vendor/everos`, create a clean
Python 3.12 environment and install both packages.  These commands do not add
the vendor source tree to `PYTHONPATH` or `sys.path`:

```powershell
py -3.12 -m venv .venv-everos
& .\.venv-everos\Scripts\python -m pip install --upgrade pip
& .\.venv-everos\Scripts\python -m pip install -e harness/vendor/everos
& .\.venv-everos\Scripts\python -m pip install -e ".[everos]"
```

On POSIX, use `python3.12 -m venv .venv-everos` and
`.venv-everos/bin/python` in place of the PowerShell executable path.  The
package metadata pins the compatible `everos==1.3.1` optional extra; the
editable vendored install above supplies that distribution locally.

Configure the scoped root before loading the public surface, then bind the
adapter to that captured root.  A missing, mismatched, or later-rebound
`EVEROS_ROOT` is rejected rather than treated as a namespace fallback:

```python
import os
from pathlib import Path

from memory_harness.experience import (
    EverOSAdapter,
    ExperienceScope,
    load_vendored_everos_public_surface,
)

scope = ExperienceScope("harness", "product", "isolated", "root-agent")
root = EverOSAdapter.memory_root_for_scope(Path(".memory") / "everos", scope)
os.environ["EVEROS_ROOT"] = str(root)
surface = load_vendored_everos_public_surface(memory_root=root)
adapter = EverOSAdapter(scope=scope, base_root=Path(".memory") / "everos", surface=surface)
```

An EverOS root is process-bound.  Once a public surface is loaded, do not
change or unset `EVEROS_ROOT`: payload, memorize, and search operations fail
closed if the current public `MemoryRoot` differs from the captured root, and a
second different root is rejected in the same process.  To use another root,
start a fresh Python process, configure that root before import, then load its
surface.  The dependency-light local suite honestly skips the real EverOS
integration when this optional installation is absent.

Generated EverOS skills remain proposed historical candidates.  Approval is
deny-by-default: `ReviewedExperienceService` requires a configured
`TrustedApprovalVerifier` to authenticate the exact durable candidate, issuer,
scope, and requested recipient set and return `VerifiedApproval` evidence.
The durable approval retains that evidence and preserves the candidate's
`generated` origin; approval does not publish, designate, inject, or execute
the content.

## Optional Atlas trusted-procedure adapter

Install `memory-harness[atlas]` only in a ROOT-controlled environment that is
authorized to use Atlas.  `AtlasProcedureAdapter` uses the public
`MongoDBAtlasVectorSearch` surface solely to discover stable publication IDs;
it exact-reads complete records and lifecycle controls from the supplied
PyMongo collection before any delivery.  Private/local partitions are never
published remotely, and the adapter's configured Atlas metric must match each
published representation.
