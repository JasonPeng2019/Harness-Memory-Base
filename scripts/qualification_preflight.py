#!/usr/bin/env python3
"""Read-only prerequisite inventory for external product qualification.

This command earns no qualification evidence.  It does not execute provider
CLIs, contact a service, consume authorization, or print environment values.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

try:
    from scripts import verification_receipt
except ModuleNotFoundError:  # Direct ``python scripts/qualification_preflight.py``.
    import verification_receipt  # type: ignore[no-redef]


SCHEMA = "memory-harness-qualification-preflight/v1"
PRODUCT = Path(__file__).resolve().parents[1]
CONFIRMATION = "I_AUTHORIZE_ONE_DISPOSABLE_RUN"
HARD_EGRESS_PROOF_SCHEMA = "memory-harness-hard-egress-proof/v1"
NESTED_TOPOLOGY_PROOF_SCHEMA = "memory-harness-nested-topology-proof/v1"
LIVE_CALL_BUDGET = 37
LIVE_COST_BUDGET_USD = 1.90
LIVE_TIMEOUT_SECONDS = 1800

WINDOWS_JOB_SELECTORS = (
    "harness/orchestrator_harness/tests/test_product_corrections.py::"
    "ProviderBoundaryCorrectionTests::test_windows_preassigned_job_close_kills_suspended_provider_before_resume",
    "harness/orchestrator_harness/tests/test_product_corrections.py::"
    "ProviderBoundaryCorrectionTests::test_windows_job_boundary_cleans_provider_descendant",
    "harness/orchestrator_harness/tests/test_product_corrections.py::"
    "ProviderBoundaryCorrectionTests::test_windows_job_handle_close_kills_members_and_serialized_boundary_recovers",
)
WINDOWS_JUNCTION_SELECTORS = (
    "harness/orchestrator_harness/tests/test_setup_cache_recovery.py::"
    "SetupCacheRecoveryTests::test_root_payload_junction_blocks_preflight_and_install_recheck",
    "harness/orchestrator_harness/tests/test_setup_cache_recovery.py::"
    "SetupCacheRecoveryTests::test_root_payload_target_junction_blocks_before_setup_writes",
    "harness/orchestrator_harness/tests/test_setup_cache_recovery.py::"
    "SetupCacheRecoveryTests::test_junction_at_root_or_beneath_cache_blocks_validation_and_dispatch",
    "harness/orchestrator_harness/tests/test_setup_cache_recovery.py::"
    "SetupCacheRecoveryTests::test_workspace_and_runtime_junctions_block_first_setup_without_writes",
    "harness/orchestrator_harness/tests/test_setup_cache_recovery.py::"
    "SetupCacheRecoveryTests::test_workspace_and_runtime_junctions_block_setup_and_both_bootstrap_profiles",
)
WINDOWS_HOST_ASSERTION = (
    "tests/platform/test_native_process_contracts.py::"
    "test_expected_native_host_is_executing"
)


@dataclass(frozen=True)
class InventoryItem:
    key: str
    evidence_class: str
    ledger_coordinates: tuple[tuple[str, str], ...] = ()
    action_ids: tuple[str, ...] = ()
    design_gap: str | None = None
    diagnostic_selector: str | None = None


INVENTORY = {
    item.key: item
    for item in (
        InventoryItem("live-everos", "LIVE", (("G01", "LIVE"), ("T25", "LIVE")), ("live-enhanced",)),
        InventoryItem("live-atlas", "LIVE", (("G01", "LIVE"), ("T25", "LIVE")), ("live-enhanced",)),
        InventoryItem("remote-snapshot-restore", "LIVE", (("G05", "LIVE"),), ("remote-restore",)),
        InventoryItem(
            "native-provider-apc", "NATIVE",
            (("G02", "NATIVE"), ("T01", "NATIVE"), ("T12", "NATIVE"),
             ("T22", "NATIVE"), ("T25", "NATIVE"), ("T26", "NATIVE"),
             ("T28", "NATIVE")),
            ("native-baseline", "live-enhanced", "apc-binding-a", "apc-binding-b", "native-role-usage"),
        ),
        InventoryItem("hard-egress", "LIVE", (("G06", "LIVE"), ("T18", "LIVE"), ("T24", "LIVE")), ("hard-egress",)),
        InventoryItem("nested-harness", "NATIVE", (("T27", "NATIVE"),), ("nested-harness",)),
        InventoryItem("native-macos", "NATIVE-MACOS", (("G07", "NATIVE-MACOS"),), ("native-macos",)),
        InventoryItem("native-windows", "NATIVE-WINDOWS", (("G07", "NATIVE-WINDOWS"),), ("native-windows",)),
        InventoryItem("windows-job-junction-launcher", "NATIVE-WINDOWS", action_ids=("windows-details",)),
        InventoryItem("cpython-3.11", "LOCAL-CPYTHON-3.11", (("G10", "LOCAL-CPYTHON-3.11"),), ("cpython-3.11",)),
        InventoryItem("cpython-3.13", "LOCAL-CPYTHON-3.13", (("G10", "LOCAL-CPYTHON-3.13"),), ("cpython-3.13",)),
        InventoryItem("cpython-3.14", "LOCAL-CPYTHON-3.14", (("G10", "LOCAL-CPYTHON-3.14"),), ("cpython-3.14",)),
        InventoryItem(
            "qwen-0.21.10", "NATIVE", design_gap="native Qwen 0.21.10 version-observation node",
            diagnostic_selector=(
                "harness/orchestrator_harness/tests/test_provider_network_payload.py::"
                "ProviderNetworkPayloadTests::test_qwen_cli_argument_and_version_gate"
            ),
        ),
        InventoryItem(
            "enhanced-memory-claude-code", "NATIVE",
            design_gap="Claude Code provider-filesystem-isolation qualification node",
        ),
        InventoryItem(
            "enhanced-memory-qwen-code", "NATIVE",
            design_gap="Qwen Code provider-filesystem-isolation qualification node",
        ),
    )
}


@dataclass(frozen=True)
class ProbeContext:
    product_root: Path
    environ: Mapping[str, str]
    find: Callable[[str], str | None]
    artifact: Path | None
    verification_receipt_path: Path | None
    authorization_file: Path | None
    namespace: str | None
    call_budget: int | None
    cost_budget_usd: float | None
    timeout_seconds: int | None
    hard_egress_proof: Path | None
    nested_topology_proof: Path | None
    clean_candidate: bool
    packages: frozenset[str]

    @property
    def provider_executables(self) -> dict[str, Path]:
        found: dict[str, Path] = {}
        for name in ("codex", "claude", "qwen"):
            raw = self.find(name)
            if not raw:
                continue
            path = Path(raw)
            try:
                resolved = path.resolve(strict=True)
            except (OSError, RuntimeError):
                continue
            if path.is_absolute() and resolved.is_file() and os.access(resolved, os.X_OK):
                found[name] = resolved
        return found

    @property
    def provider_present(self) -> bool:
        return bool(self.provider_executables)

    @property
    def live_driver_present(self) -> bool:
        raw = self.environ.get("MEMORY_HARNESS_LIVE_QUALIFICATION_DRIVER", "")
        return bool(raw and Path(raw).is_absolute() and Path(raw).is_file() and os.access(raw, os.X_OK))


def _git_clean(product_root: Path) -> bool:
    try:
        completed = subprocess.run(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"],
            cwd=product_root, text=True, capture_output=True, timeout=10, check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return not completed.stdout.strip()


def _packages() -> frozenset[str]:
    return frozenset(
        name for name in ("everos", "pymongo", "langchain_mongodb")
        if importlib.util.find_spec(name) is not None
    )


def _artifact_sha256(artifact: Path | None) -> str | None:
    if artifact is None or not artifact.is_absolute() or artifact.is_symlink() or not artifact.is_file():
        return None
    try:
        return hashlib.sha256(artifact.read_bytes()).hexdigest()
    except OSError:
        return None


def _receipt_binds_candidate_and_artifact(ctx: ProbeContext) -> bool:
    receipt_path = ctx.verification_receipt_path
    artifact = ctx.artifact
    artifact_digest = _artifact_sha256(artifact)
    if (
        receipt_path is None
        or not receipt_path.is_absolute()
        or receipt_path.is_symlink()
        or not receipt_path.is_file()
        or artifact is None
        or artifact_digest is None
    ):
        return False
    try:
        document = verification_receipt.validate_receipt(
            receipt_path, require_qualifying=True,
        )
        candidate = document["candidate"]
        if Path(candidate["root"]).resolve() != ctx.product_root:
            return False
        suite = json.loads(
            (ctx.product_root / "tests/suite_manifest.json").read_text(encoding="utf-8")
        )
        required_categories = suite.get("all")
        selection = document.get("selection", {})
        if (
            not isinstance(required_categories, list)
            or not required_categories
            or selection.get("requested") != required_categories
            or selection.get("available") != required_categories
            or [item.get("name") for item in document.get("categories", [])]
            != required_categories
        ):
            return False
        artifact_files = document["artifacts"]["post"]["files"]
        matches = [
            item for item in artifact_files
            if Path(item.get("path", "")).resolve() == artifact.resolve()
        ]
        return len(matches) == 1 and matches[0] == {
            "path": str(artifact.resolve()),
            "kind": "file",
            "size": artifact.stat().st_size,
            "sha256": artifact_digest,
        }
    except (
        OSError, KeyError, TypeError, ValueError, json.JSONDecodeError,
        subprocess.SubprocessError, verification_receipt.ReceiptError,
    ):
        return False


def _nonce_file_has_expected_shape(path: Path | None) -> bool:
    if path is None or not path.is_absolute() or path.is_symlink() or not path.is_file():
        return False
    try:
        if path.stat().st_size > 256:
            return False
        value = path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError):
        return False
    return re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _proof_is_bound(
    path: Path | None, *, schema: str, ctx: ProbeContext,
    required_checks: frozenset[str],
) -> bool:
    digest = _artifact_sha256(ctx.artifact)
    if (
        path is None or not path.is_absolute() or path.is_symlink()
        or not path.is_file() or digest is None
    ):
        return False
    try:
        if path.stat().st_size > 1_000_000:
            return False
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    try:
        receipt = (
            verification_receipt.validate_receipt(
                ctx.verification_receipt_path, require_qualifying=True,
            )
            if ctx.verification_receipt_path is not None else None
        )
    except (
        OSError, KeyError, TypeError, ValueError, json.JSONDecodeError,
        subprocess.SubprocessError, verification_receipt.ReceiptError,
    ):
        receipt = None
    if not isinstance(document, dict) or set(document) != {
        "schema", "artifact_sha256", "verification_receipt_integrity",
        "provider", "provider_executable", "host", "outcome", "checks",
    }:
        return False
    checks = document.get("checks")
    provider = document.get("provider")
    provider_authorizations = {
        "codex": "OPENAI_API_KEY",
        "claude": "ANTHROPIC_API_KEY",
        "qwen": "QWEN_API_KEY",
    }
    executable = (
        ctx.provider_executables.get(provider)
        if isinstance(provider, str) else None
    )
    expected_host = {
        "os": os.name,
        "platform": sys.platform,
        "architecture": platform.machine(),
    }
    return (
        document.get("schema") == schema
        and document.get("artifact_sha256") == digest
        and isinstance(receipt, dict)
        and document.get("verification_receipt_integrity") == receipt.get("integrity")
        and executable is not None
        and document.get("provider_executable") == str(executable)
        and document.get("host") == expected_host
        and bool(ctx.environ.get(provider_authorizations.get(provider, "")))
        and document.get("outcome") == "PASS"
        and isinstance(checks, dict)
        and set(checks) == required_checks
        and all(value is True for value in checks.values())
    )


def _common_live_missing(ctx: ProbeContext) -> list[str]:
    missing: list[str] = []
    if _artifact_sha256(ctx.artifact) is None:
        missing.append("clean_candidate_artifact")
    if not ctx.clean_candidate:
        missing.append("clean_candidate")
    if not _receipt_binds_candidate_and_artifact(ctx):
        missing.append("qualifying_verification_receipt")
    if not ctx.live_driver_present:
        missing.append("live_driver")
    if not _nonce_file_has_expected_shape(ctx.authorization_file):
        missing.append("one_use_authorization_nonce_file")
    if not isinstance(ctx.namespace, str) or re.fullmatch(r"mhq-[a-z0-9-]{8,64}", ctx.namespace) is None:
        missing.append("disposable_namespace")
    if not isinstance(ctx.call_budget, int) or isinstance(ctx.call_budget, bool) or ctx.call_budget != LIVE_CALL_BUDGET:
        missing.append(f"aggregate_call_budget:{LIVE_CALL_BUDGET}")
    if (
        not isinstance(ctx.cost_budget_usd, (int, float))
        or isinstance(ctx.cost_budget_usd, bool)
        or float(ctx.cost_budget_usd) != LIVE_COST_BUDGET_USD
    ):
        missing.append(f"aggregate_cost_budget_usd:{LIVE_COST_BUDGET_USD:.2f}")
    if not isinstance(ctx.timeout_seconds, int) or isinstance(ctx.timeout_seconds, bool) or ctx.timeout_seconds != LIVE_TIMEOUT_SECONDS:
        missing.append(f"aggregate_timeout_seconds:{LIVE_TIMEOUT_SECONDS}")
    return missing


def _service_missing(ctx: ProbeContext) -> list[str]:
    missing: list[str] = []
    if not {"pymongo", "langchain_mongodb"} <= ctx.packages:
        missing.append("atlas_packages")
    if not ctx.environ.get("MEMORY_HARNESS_ATLAS_URI"):
        missing.append("atlas_uri")
    if "everos" not in ctx.packages:
        missing.append("everos_package")
    if not ctx.environ.get("MEMORY_HARNESS_EVEROS_ENDPOINT"):
        missing.append("everos_endpoint")
    if not ctx.environ.get("MEMORY_HARNESS_EVEROS_TOKEN"):
        missing.append("everos_token")
    return missing


def _provider_missing(ctx: ProbeContext) -> list[str]:
    missing: list[str] = []
    providers = set(ctx.provider_executables)
    authorizations = {
        name for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "QWEN_API_KEY")
        if ctx.environ.get(name)
    }
    if not providers:
        missing.append("native_provider")
    if not authorizations:
        missing.append("provider_authorization")
    pairs = {
        ("codex", "OPENAI_API_KEY"),
        ("claude", "ANTHROPIC_API_KEY"),
        ("qwen", "QWEN_API_KEY"),
    }
    if not any(provider in providers and authorization in authorizations for provider, authorization in pairs):
        missing.append("provider_authorization_pair")
    return missing


def _resolved_selector(selector: str, product_root: Path) -> str:
    path, separator, node = selector.partition("::")
    relative = Path(path)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("qualification selector must be candidate-relative")
    # Deliberately do not resolve the target: resolving a selector symlink can
    # turn a candidate-looking argv into an external-code argv.  Readiness is
    # awarded only after _plain_qualification_source validates every component.
    candidate_path = product_root.resolve() / relative
    return str(candidate_path) + (separator + node if separator else "")


def _plain_qualification_source(selector: str, product_root: Path) -> bool:
    """Require a regular selector file below root with no link/reparse component."""

    raw = selector.split("::", 1)[0]
    relative = Path(raw)
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        return False
    try:
        root = product_root.resolve(strict=True)
        root.relative_to(root)
        current = root
        for part in relative.parts:
            current = current / part
            metadata = current.lstat()
            junction_probe = getattr(current, "is_junction", None)
            if stat.S_ISLNK(metadata.st_mode) or bool(
                junction_probe is not None and junction_probe()
            ):
                return False
        if not stat.S_ISREG(current.lstat().st_mode):
            return False
        current.resolve(strict=True).relative_to(root)
    except (OSError, RuntimeError, ValueError):
        return False
    return True


def _live_argv(selector: str, ctx: ProbeContext) -> list[str]:
    return [
        sys.executable, "-m", "pytest", _resolved_selector(selector, ctx.product_root),
        "--authorize-live-qualification", CONFIRMATION,
        "--live-authorization-nonce", "<REDACTED_NONCE>",
        "--live-authorization-file", "<ABSOLUTE_ONE_USE_NONCE_FILE>",
        "--live-candidate-artifact", (
            str(ctx.artifact) if ctx.artifact is not None
            else "<ABSOLUTE_CANDIDATE_ARTIFACT>"
        ),
        "--live-namespace", ctx.namespace or "mhq-<DISPOSABLE_ID>",
        "--live-call-budget", str(LIVE_CALL_BUDGET),
        "--live-cost-budget-usd", f"{LIVE_COST_BUDGET_USD:.2f}",
        "--live-timeout", str(LIVE_TIMEOUT_SECONDS),
    ]


_LIVE_ACTIONS = {
    "live-enhanced": (
        "tests/live/qualification/test_current_pin_lifecycle.py::test_current_pin_enhanced_lifecycle",
        8, 0.50, "services+provider",
    ),
    "native-baseline": (
        "tests/live/qualification/test_current_pin_lifecycle.py::test_current_pin_baseline_lifecycle",
        4, 0.10, "provider",
    ),
    "remote-restore": (
        "tests/live/qualification/test_current_pin_lifecycle.py::test_remote_snapshot_restore",
        4, 0.25, "services",
    ),
    "hard-egress": (
        "tests/live/qualification/test_current_pin_lifecycle.py::test_hard_egress_and_content_privacy",
        4, 0.15, "provider+hard-egress",
    ),
    "nested-harness": (
        "tests/live/qualification/test_current_pin_lifecycle.py::test_nested_harness_ownership_topology",
        4, 0.15, "provider+nested",
    ),
    "apc-binding-a": (
        "tests/live/qualification/test_native_apc_matrix.py::test_supported_binding_runs_actual_child[binding_a]",
        4, 0.25, "provider",
    ),
    "apc-binding-b": (
        "tests/live/qualification/test_native_apc_matrix.py::test_supported_binding_runs_actual_child[binding_b]",
        4, 0.25, "provider",
    ),
    "native-role-usage": (
        "tests/live/qualification/test_native_apc_matrix.py::test_native_usage_role_matrix_is_single_counted",
        5, 0.25, "provider",
    ),
}


def _live_action(action_id: str, ctx: ProbeContext) -> dict[str, object]:
    selector, calls, cost, needs = _LIVE_ACTIONS[action_id]
    missing = _common_live_missing(ctx)
    selector_path = selector.split("::", 1)[0]
    source_ready = _plain_qualification_source(selector, ctx.product_root)
    if not source_ready:
        missing.append(f"qualification_source:{selector_path}")
    if "services" in needs:
        missing.extend(_service_missing(ctx))
    if "provider" in needs:
        missing.extend(_provider_missing(ctx))
    if "hard-egress" in needs and not _proof_is_bound(
        ctx.hard_egress_proof,
        schema=HARD_EGRESS_PROOF_SCHEMA,
        ctx=ctx,
        required_checks=frozenset({
            "deny_by_default", "provider_allowlist_only", "undeclared_egress_blocked",
        }),
    ):
        missing.append("artifact_bound_hard_egress_proof")
    if "nested" in needs and not _proof_is_bound(
        ctx.nested_topology_proof,
        schema=NESTED_TOPOLOGY_PROOF_SCHEMA,
        ctx=ctx,
        required_checks=frozenset({
            "single_controller_owner", "child_runtime_isolated", "descendants_reaped",
        }),
    ):
        missing.append("artifact_bound_nested_topology_proof")
    missing = sorted(set(missing))
    return {
        "id": action_id,
        "selector": selector,
        "evidence_class": "LIVE" if action_id in {"live-enhanced", "remote-restore", "hard-egress"} else "NATIVE",
        "scenario_call_budget": calls,
        "scenario_cost_budget_usd": cost,
        "status": "blocked" if missing else "local_preconditions_ready",
        "missing": missing,
        "next_argv": _live_argv(selector, ctx) if source_ready else None,
        "cwd": str(ctx.product_root),
    }


def _local_action(action_id: str, ctx: ProbeContext) -> dict[str, object]:
    actual = "win32" if os.name == "nt" else sys.platform
    selectors: tuple[str, ...]
    if action_id == "native-macos":
        target = "darwin"
        selector = "tests/platform/test_native_process_contracts.py::test_native_host_spawn_identity_and_exact_reap[darwin]"
        selectors = (selector,)
        ready = actual == target
        command = [sys.executable, "-m", "pytest", _resolved_selector(selector, ctx.product_root)]
        evidence_class = "NATIVE-MACOS"
    elif action_id == "native-windows":
        target = "win32"
        selector = "tests/platform/test_native_process_contracts.py::test_native_host_spawn_identity_and_exact_reap[win32]"
        selectors = (selector,)
        ready = actual == target
        command = [sys.executable, "-m", "pytest", _resolved_selector(selector, ctx.product_root)]
        evidence_class = "NATIVE-WINDOWS"
    elif action_id == "windows-details":
        target = "win32"
        selector = WINDOWS_HOST_ASSERTION
        selectors = (WINDOWS_HOST_ASSERTION, *WINDOWS_JOB_SELECTORS, *WINDOWS_JUNCTION_SELECTORS)
        ready = actual == target and any(ctx.find(name) is not None for name in ("powershell", "pwsh"))
        command = [
            sys.executable, "-m", "pytest", "-q",
            *(
                _resolved_selector(item, ctx.product_root)
                for item in (
                    WINDOWS_HOST_ASSERTION, *WINDOWS_JOB_SELECTORS,
                    *WINDOWS_JUNCTION_SELECTORS,
                )
            ),
        ]
        evidence_class = "NATIVE-WINDOWS"
    else:
        version = action_id.removeprefix("cpython-")
        target = f"python{version}"
        selector = f"tests/distribution/test_install_matrix.py::test_declared_release_interpreter_coordinate[{version}]"
        selectors = (selector,)
        ready = ctx.find(target) is not None
        command = [target, "-m", "pytest", _resolved_selector(selector, ctx.product_root)]
        evidence_class = f"LOCAL-CPYTHON-{version}"
    missing: list[str] = []
    if not ready:
        missing.append(f"required_host_or_executable:{target}")
    missing_sources = sorted({
        selector.split("::", 1)[0]
        for selector in selectors
        if not _plain_qualification_source(selector, ctx.product_root)
    })
    if missing_sources:
        missing.extend(f"qualification_source:{path}" for path in missing_sources)
    result: dict[str, object] = {
        "id": action_id,
        "selector": selector,
        "evidence_class": evidence_class,
        "status": "local_preconditions_ready" if ready and not missing_sources else "blocked",
        "missing": missing,
        "next_argv": command if not missing_sources else None,
        "cwd": str(ctx.product_root),
    }
    if action_id == "windows-details":
        result.update({
            "host_assertion_selector": WINDOWS_HOST_ASSERTION,
            "selectors": [*WINDOWS_JOB_SELECTORS, *WINDOWS_JUNCTION_SELECTORS],
            "job_object_selectors": list(WINDOWS_JOB_SELECTORS),
            "junction_selectors": list(WINDOWS_JUNCTION_SELECTORS),
        })
    return result


def build_report(
    *,
    product_root: Path = PRODUCT,
    environ: Mapping[str, str] | None = None,
    executable_finder: Callable[[str], str | None] = shutil.which,
    artifact: Path | None = None,
    verification_receipt_path: Path | None = None,
    authorization_file: Path | None = None,
    namespace: str | None = None,
    call_budget: int | None = None,
    cost_budget_usd: float | None = None,
    timeout_seconds: int | None = None,
    hard_egress_proof: Path | None = None,
    nested_topology_proof: Path | None = None,
) -> dict[str, object]:
    """Return presence-only local readiness; never perform external work."""

    selected_environment = dict(os.environ if environ is None else environ)
    context = ProbeContext(
        product_root=product_root.resolve(),
        environ=selected_environment,
        find=executable_finder,
        artifact=artifact,
        verification_receipt_path=verification_receipt_path,
        authorization_file=authorization_file,
        namespace=namespace,
        call_budget=call_budget,
        cost_budget_usd=cost_budget_usd,
        timeout_seconds=timeout_seconds,
        hard_egress_proof=hard_egress_proof,
        nested_topology_proof=nested_topology_proof,
        clean_candidate=_git_clean(product_root),
        packages=_packages(),
    )
    rows: list[dict[str, object]] = []
    for item in INVENTORY.values():
        base: dict[str, object] = {
            "key": item.key,
            "evidence_class": item.evidence_class,
            "ledger_coordinates": [list(pair) for pair in item.ledger_coordinates],
        }
        if item.design_gap is not None:
            diagnostic_source_ready = (
                _plain_qualification_source(item.diagnostic_selector, context.product_root)
                if item.diagnostic_selector else False
            )
            base.update({
                "status": "blocked",
                "reason": "design_gap",
                "actions": [],
                "next_argv": None,
                "missing_qualification_node": item.design_gap,
                "design_next_step": f"design and run {item.design_gap} without weakening the current gate",
                "diagnostic_argv": (
                    [
                        sys.executable, "-m", "pytest",
                        _resolved_selector(item.diagnostic_selector, context.product_root),
                    ]
                    if item.diagnostic_selector and diagnostic_source_ready else None
                ),
                "diagnostic_cwd": str(context.product_root) if item.diagnostic_selector else None,
                "diagnostic_evidence": "CONTRACT_ONLY" if item.diagnostic_selector else None,
            })
        else:
            actions = [
                _live_action(action_id, context)
                if action_id in _LIVE_ACTIONS else _local_action(action_id, context)
                for action_id in item.action_ids
            ]
            ready = bool(actions) and all(action["status"] == "local_preconditions_ready" for action in actions)
            base.update({
                "status": "local_preconditions_ready" if ready else "blocked",
                "reason": "all_actions_ready" if ready else "missing_local_prerequisites",
                "actions": actions,
                "next_argv": actions[0]["next_argv"] if len(actions) == 1 else None,
                "missing_qualification_node": None,
                "design_next_step": None,
                "diagnostic_argv": None,
                "diagnostic_evidence": None,
            })
        rows.append(base)
    return {
        "schema": SCHEMA,
        "qualification_evidence_earned": False,
        "meaning": "local_preconditions_ready means attemptable, never PASS",
        "host": {
            "os": "windows" if os.name == "nt" else platform.system().lower(),
            "python": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        },
        "coordinates": rows,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit the versioned JSON report")
    parser.add_argument(
        "--product-root", type=Path, default=PRODUCT,
        help="candidate source checkout whose cleanliness and receipt binding are checked",
    )
    parser.add_argument("--artifact", type=Path)
    parser.add_argument("--verification-receipt", type=Path)
    parser.add_argument("--authorization-file", type=Path)
    parser.add_argument("--live-namespace")
    parser.add_argument("--live-call-budget", type=int)
    parser.add_argument("--live-cost-budget-usd", type=float)
    parser.add_argument("--live-timeout", type=int)
    parser.add_argument("--hard-egress-proof", type=Path)
    parser.add_argument("--nested-topology-proof", type=Path)
    args = parser.parse_args(argv)
    report = build_report(
        product_root=args.product_root,
        artifact=args.artifact,
        verification_receipt_path=args.verification_receipt,
        authorization_file=args.authorization_file,
        namespace=args.live_namespace,
        call_budget=args.live_call_budget,
        cost_budget_usd=args.live_cost_budget_usd,
        timeout_seconds=args.live_timeout,
        hard_egress_proof=args.hard_egress_proof,
        nested_topology_proof=args.nested_topology_proof,
    )
    if args.json:
        print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    else:
        for row in report["coordinates"]:
            print(f"{row['key']}: {row['status']} ({row['evidence_class']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
