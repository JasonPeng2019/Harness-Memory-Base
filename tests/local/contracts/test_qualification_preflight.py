"""Closed, non-effectful external-qualification readiness contract."""

from __future__ import annotations

import json
import hashlib
import os
import platform
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import qualification_preflight as preflight
from scripts import verification_receipt


PRODUCT = Path(__file__).resolve().parents[3]
EXPECTED_KEYS = {
    "live-everos",
    "live-atlas",
    "remote-snapshot-restore",
    "native-provider-apc",
    "hard-egress",
    "nested-harness",
    "native-macos",
    "native-windows",
    "windows-job-junction-launcher",
    "cpython-3.11",
    "cpython-3.13",
    "cpython-3.14",
    "qwen-0.21.10",
    "enhanced-memory-claude-code",
    "enhanced-memory-qwen-code",
}

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


def _candidate_repository(root: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Preflight Test"], cwd=root, check=True)
    subprocess.run(
        ["git", "config", "user.email", "preflight@example.invalid"], cwd=root, check=True
    )
    (root / "candidate.txt").write_text("candidate\n", encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests/suite_manifest.json").write_bytes(
        (PRODUCT / "tests/suite_manifest.json").read_bytes()
    )
    for selector, *_ in preflight._LIVE_ACTIONS.values():
        source = root / selector.split("::", 1)[0]
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("# qualification selector fixture\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "candidate"], cwd=root, check=True)


def _qualifying_receipt(candidate: Path, artifact: Path, target: Path) -> None:
    candidate_manifest = verification_receipt.candidate_manifest(candidate)
    artifact_manifest = verification_receipt.artifact_manifest([artifact])
    categories = json.loads(
        (candidate / "tests/suite_manifest.json").read_text(encoding="utf-8")
    )["all"]
    document = {
        "schema": verification_receipt.SCHEMA,
        "candidate": {
            "root": str(candidate.resolve()),
            "pre": candidate_manifest,
            "post": candidate_manifest,
            "stable": True,
        },
        "artifacts": {
            "pre": artifact_manifest,
            "post": artifact_manifest,
            "stable": True,
        },
        "selection": {
            "requested": categories,
            "available": categories,
            "complete": True,
        },
        "categories": [{
            "name": name,
            "framework": "pytest",
            "returncode": 0,
            "structured_output_valid": True,
            "result": {
                "framework": "pytest", "tests": 1, "testcases": 1,
                "passed": 1, "failures": 0, "errors": 0, "skipped": 0,
                "skip_reasons": [],
            },
        } for name in categories],
        "fail_fast_omissions": [],
        "interrupted": False,
        "internal_error": None,
        "environment": verification_receipt.environment_metadata(cwd=candidate, environment={}),
    }
    verification_receipt.write_atomic(target, verification_receipt.finalize(document))


def _proof(
    path: Path,
    schema: str,
    artifact: Path,
    receipt: Path,
    checks: dict[str, bool],
    *,
    provider: str = "codex",
    provider_executable: Path,
) -> None:
    receipt_document = verification_receipt.validate_receipt(
        receipt, require_qualifying=True,
    )
    path.write_text(json.dumps({
        "schema": schema,
        "artifact_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
        "verification_receipt_integrity": receipt_document["integrity"],
        "provider": provider,
        "provider_executable": str(provider_executable.resolve()),
        "host": {
            "os": os.name,
            "platform": sys.platform,
            "architecture": platform.machine(),
        },
        "outcome": "PASS",
        "checks": checks,
    }), encoding="utf-8")


def _ready_inputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    _candidate_repository(candidate)
    artifact = tmp_path / "candidate.zip"
    artifact.write_bytes(b"candidate artifact")
    receipt = tmp_path / "verification-receipt.json"
    _qualifying_receipt(candidate, artifact, receipt)
    nonce_file = tmp_path / "one-use.nonce"
    nonce_file.write_text("a" * 64 + "\n", encoding="ascii")
    driver = tmp_path / "driver"
    driver.write_text("#!/bin/sh\nexit 99\n", encoding="ascii")
    driver.chmod(0o700)
    provider_root = tmp_path / "providers"
    provider_root.mkdir()
    providers: dict[str, Path] = {}
    for name in ("codex", "claude", "qwen"):
        executable = provider_root / name
        executable.write_text("#!/bin/sh\nexit 99\n", encoding="ascii")
        executable.chmod(0o700)
        providers[name] = executable
    hard_egress = tmp_path / "hard-egress.json"
    _proof(hard_egress, preflight.HARD_EGRESS_PROOF_SCHEMA, artifact, receipt, {
        "deny_by_default": True,
        "provider_allowlist_only": True,
        "undeclared_egress_blocked": True,
    }, provider_executable=providers["codex"])
    nested = tmp_path / "nested-topology.json"
    _proof(nested, preflight.NESTED_TOPOLOGY_PROOF_SCHEMA, artifact, receipt, {
        "single_controller_owner": True,
        "child_runtime_isolated": True,
        "descendants_reaped": True,
    }, provider_executable=providers["codex"])
    monkeypatch.setattr(preflight, "_packages", lambda: frozenset({
        "everos", "pymongo", "langchain_mongodb",
    }))
    return {
        "product_root": candidate,
        "environ": {
            "MEMORY_HARNESS_LIVE_QUALIFICATION_DRIVER": str(driver),
            "MEMORY_HARNESS_ATLAS_URI": "atlas-secret",
            "MEMORY_HARNESS_EVEROS_ENDPOINT": "everos-secret",
            "MEMORY_HARNESS_EVEROS_TOKEN": "everos-token",
            "OPENAI_API_KEY": "provider-secret",
        },
        "executable_finder": lambda name: str(providers[name]) if name in providers else None,
        "artifact": artifact,
        "verification_receipt_path": receipt,
        "authorization_file": nonce_file,
        "namespace": "mhq-abcdefgh",
        "call_budget": 37,
        "cost_budget_usd": 1.90,
        "timeout_seconds": 1800,
        "hard_egress_proof": hard_egress,
        "nested_topology_proof": nested,
    }


def _unproved_ledger_pairs() -> set[tuple[str, str]]:
    ledger = json.loads(
        (PRODUCT / "harness/docs/product/VERIFICATION_LEDGER.json").read_text(
            encoding="utf-8"
        )
    )
    return {
        (row["requirement_id"], row["evidence_class"])
        for row in ledger["coordinates"]
        if row["status"] == "UNPROVED-PREREQUISITE"
    }


def test_inventory_is_closed_and_maps_every_unproved_ledger_coordinate() -> None:
    assert set(preflight.INVENTORY) == EXPECTED_KEYS
    mapped = {
        pair
        for item in preflight.INVENTORY.values()
        for pair in item.ledger_coordinates
    }
    assert mapped == _unproved_ledger_pairs()


def test_design_gaps_never_offer_a_qualification_command() -> None:
    report = preflight.build_report(product_root=PRODUCT, environ={}, executable_finder=lambda _: None)
    by_key = {row["key"]: row for row in report["coordinates"]}
    for key in (
        "qwen-0.21.10",
        "enhanced-memory-claude-code",
        "enhanced-memory-qwen-code",
    ):
        row = by_key[key]
        assert row["status"] == "blocked"
        assert row["reason"] == "design_gap"
        assert row["next_argv"] is None
        assert row["missing_qualification_node"]
        assert row["design_next_step"]
    assert by_key["qwen-0.21.10"]["diagnostic_argv"]
    assert by_key["qwen-0.21.10"]["diagnostic_evidence"] == "CONTRACT_ONLY"


def test_aggregate_readiness_is_action_specific_and_never_equals_evidence() -> None:
    report = preflight.build_report(product_root=PRODUCT, environ={}, executable_finder=lambda _: None)
    by_key = {row["key"]: row for row in report["coordinates"]}
    enhanced = by_key["live-everos"]
    assert enhanced["status"] == "blocked"
    assert [action["id"] for action in enhanced["actions"]] == ["live-enhanced"]
    assert {
        "atlas_packages",
        "atlas_uri",
        "everos_package",
        "everos_endpoint",
        "everos_token",
        "native_provider",
    } <= set(enhanced["actions"][0]["missing"])
    provider = by_key["native-provider-apc"]
    assert [action["id"] for action in provider["actions"]] == [
        "native-baseline",
        "live-enhanced",
        "apc-binding-a",
        "apc-binding-b",
        "native-role-usage",
    ]
    assert all(action["status"] in {"local_preconditions_ready", "blocked"} for action in provider["actions"])
    assert report["qualification_evidence_earned"] is False


def test_action_evidence_classes_do_not_downgrade_platform_coordinates() -> None:
    report = preflight.build_report(
        product_root=PRODUCT, environ={}, executable_finder=lambda _: None
    )
    by_key = {row["key"]: row for row in report["coordinates"]}
    assert by_key["native-macos"]["actions"][0]["evidence_class"] == "NATIVE-MACOS"
    assert by_key["native-windows"]["actions"][0]["evidence_class"] == "NATIVE-WINDOWS"
    assert (
        by_key["windows-job-junction-launcher"]["actions"][0]["evidence_class"]
        == "NATIVE-WINDOWS"
    )
    for version in ("3.11", "3.13", "3.14"):
        assert (
            by_key[f"cpython-{version}"]["actions"][0]["evidence_class"]
            == f"LOCAL-CPYTHON-{version}"
        )


def test_cli_emits_no_credential_or_nonce_values() -> None:
    secrets = {
        "MEMORY_HARNESS_ATLAS_URI": "mongodb+srv://user:atlas-secret@example.invalid",
        "MEMORY_HARNESS_EVEROS_ENDPOINT": "https://everos.invalid/secret-path",
        "MEMORY_HARNESS_EVEROS_TOKEN": "everos-secret-token",
        "OPENAI_API_KEY": "provider-secret-key",
        "MEMORY_HARNESS_LIVE_QUALIFICATION_DRIVER": "/not/executable/driver",
    }
    environment = dict(os.environ)
    environment.update(secrets)
    completed = subprocess.run(
        [sys.executable, str(PRODUCT / "scripts/qualification_preflight.py"), "--json"],
        cwd=PRODUCT,
        env=environment,
        text=True,
        capture_output=True,
        timeout=20,
    )
    assert completed.returncode == 0, completed.stderr
    document = json.loads(completed.stdout)
    assert document["schema"] == preflight.SCHEMA
    rendered = completed.stdout
    for secret in secrets.values():
        assert secret not in rendered
    assert "<REDACTED_NONCE>" in rendered
    assert {row["key"] for row in document["coordinates"]} == EXPECTED_KEYS


def test_live_readiness_requires_exact_candidate_bound_inputs_and_can_be_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs = _ready_inputs(tmp_path, monkeypatch)
    report = preflight.build_report(**inputs)
    by_key = {row["key"]: row for row in report["coordinates"]}
    for key in (
        "live-everos", "live-atlas", "remote-snapshot-restore",
        "native-provider-apc", "hard-egress", "nested-harness",
    ):
        assert by_key[key]["status"] == "local_preconditions_ready", by_key[key]
    argv = by_key["live-everos"]["actions"][0]["next_argv"]
    assert Path(argv[3].split("::", 1)[0]).is_absolute()
    assert by_key["live-everos"]["actions"][0]["cwd"] == str(
        inputs["product_root"]
    )
    assert argv[argv.index("--live-namespace") + 1] == "mhq-abcdefgh"
    assert argv[argv.index("--live-call-budget") + 1] == "37"
    assert argv[argv.index("--live-cost-budget-usd") + 1] == "1.90"
    assert argv[argv.index("--live-timeout") + 1] == "1800"
    assert report["qualification_evidence_earned"] is False
    rendered = json.dumps(report, sort_keys=True)
    for secret in inputs["environ"].values():
        assert secret not in rendered
    assert "a" * 64 not in rendered


@pytest.mark.skipif(os.name == "nt", reason="POSIX selector-symlink fixture")
def test_selector_symlink_escape_is_blocked_without_an_executable_argv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs = _ready_inputs(tmp_path, monkeypatch)
    candidate = inputs["product_root"]
    assert isinstance(candidate, Path)
    selector = preflight._LIVE_ACTIONS["live-enhanced"][0]
    source = candidate / selector.split("::", 1)[0]
    external = tmp_path / "outside-qualification.py"
    external.write_text("raise AssertionError('must not execute')\n", encoding="utf-8")
    source.unlink()
    source.symlink_to(external)

    report = preflight.build_report(**inputs)
    action = {
        row["key"]: row for row in report["coordinates"]
    }["live-everos"]["actions"][0]

    assert action["status"] == "blocked"
    assert "qualification_source:tests/live/qualification/test_current_pin_lifecycle.py" in action["missing"]
    assert action["next_argv"] is None
    assert str(external) not in json.dumps(report)


@pytest.mark.parametrize(
    ("override", "missing"),
    (
        ({"verification_receipt_path": None}, "qualifying_verification_receipt"),
        ({"namespace": "qualification"}, "disposable_namespace"),
        ({"call_budget": 38}, "aggregate_call_budget:37"),
        ({"cost_budget_usd": 1.91}, "aggregate_cost_budget_usd:1.90"),
        ({"timeout_seconds": 1799}, "aggregate_timeout_seconds:1800"),
    ),
)
def test_live_readiness_fails_closed_for_near_miss_authority_and_budget_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    override: dict[str, object], missing: str,
) -> None:
    inputs = _ready_inputs(tmp_path, monkeypatch)
    inputs.update(override)
    report = preflight.build_report(**inputs)
    action = {row["key"]: row for row in report["coordinates"]}["live-everos"]["actions"][0]
    assert action["status"] == "blocked"
    assert missing in action["missing"]


def test_receipt_must_bind_the_selected_artifact_and_current_clean_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs = _ready_inputs(tmp_path, monkeypatch)
    other = tmp_path / "other.zip"
    other.write_bytes(b"other artifact")
    inputs["artifact"] = other
    report = preflight.build_report(**inputs)
    missing = {row["key"]: row for row in report["coordinates"]}["live-atlas"]["actions"][0]["missing"]
    assert "qualifying_verification_receipt" in missing
    candidate = inputs["product_root"]
    assert isinstance(candidate, Path)
    (candidate / "candidate.txt").write_text("dirty\n", encoding="utf-8")
    report = preflight.build_report(**inputs)
    missing = {row["key"]: row for row in report["coordinates"]}["live-atlas"]["actions"][0]["missing"]
    assert "clean_candidate" in missing
    assert "qualifying_verification_receipt" in missing


def test_receipt_must_cover_the_candidate_declared_complete_suite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs = _ready_inputs(tmp_path, monkeypatch)
    receipt_path = inputs["verification_receipt_path"]
    assert isinstance(receipt_path, Path)
    document = verification_receipt.validate_receipt(
        receipt_path, require_qualifying=True,
    )
    document.pop("integrity")
    document["selection"] = {
        "requested": ["contracts"], "available": ["contracts"], "complete": True,
    }
    document["categories"] = [document["categories"][0]]
    verification_receipt.write_atomic(
        receipt_path, verification_receipt.finalize(document),
    )

    report = preflight.build_report(**inputs)
    action = {
        row["key"]: row for row in report["coordinates"]
    }["live-everos"]["actions"][0]
    assert "qualifying_verification_receipt" in action["missing"]


def test_provider_executable_and_authorization_must_form_a_supported_pair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs = _ready_inputs(tmp_path, monkeypatch)
    inputs["executable_finder"] = lambda name: "/qualified/codex" if name == "codex" else None
    environment = dict(inputs["environ"])
    environment.pop("OPENAI_API_KEY")
    environment["ANTHROPIC_API_KEY"] = "mismatched-provider-secret"
    inputs["environ"] = environment

    report = preflight.build_report(**inputs)
    action = {
        row["key"]: row for row in report["coordinates"]
    }["native-provider-apc"]["actions"][0]
    assert action["status"] == "blocked"
    assert "provider_authorization_pair" in action["missing"]

@pytest.mark.parametrize("contents", ("A" * 64, "a" * 63, "a" * 65, "not-hex"))
def test_nonce_file_requires_lowercase_256_bit_shape_without_disclosure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, contents: str,
) -> None:
    inputs = _ready_inputs(tmp_path, monkeypatch)
    nonce = inputs["authorization_file"]
    assert isinstance(nonce, Path)
    nonce.write_text(contents, encoding="ascii")
    report = preflight.build_report(**inputs)
    action = {row["key"]: row for row in report["coordinates"]}["live-everos"]["actions"][0]
    assert "one_use_authorization_nonce_file" in action["missing"]
    assert contents not in json.dumps(report)


def test_egress_and_topology_require_schema_and_artifact_bound_proofs_not_env_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs = _ready_inputs(tmp_path, monkeypatch)
    environment = dict(inputs["environ"])
    environment.update({
        "MEMORY_HARNESS_HARD_EGRESS_EVIDENCE": "yes",
        "MEMORY_HARNESS_NESTED_TOPOLOGY_EVIDENCE": "yes",
    })
    inputs["environ"] = environment
    inputs["hard_egress_proof"] = None
    inputs["nested_topology_proof"] = None
    report = preflight.build_report(**inputs)
    by_key = {row["key"]: row for row in report["coordinates"]}
    assert "artifact_bound_hard_egress_proof" in by_key["hard-egress"]["actions"][0]["missing"]
    assert "artifact_bound_nested_topology_proof" in by_key["nested-harness"]["actions"][0]["missing"]

    wrong_artifact = tmp_path / "wrong.zip"
    wrong_artifact.write_bytes(b"wrong")
    hard = tmp_path / "wrong-hard.json"
    receipt = inputs["verification_receipt_path"]
    assert isinstance(receipt, Path)
    finder = inputs["executable_finder"]
    assert callable(finder)
    codex = Path(finder("codex"))
    _proof(hard, preflight.HARD_EGRESS_PROOF_SCHEMA, wrong_artifact, receipt, {
        "deny_by_default": True,
        "provider_allowlist_only": True,
        "undeclared_egress_blocked": True,
    }, provider_executable=codex)
    nested = tmp_path / "wrong-nested.json"
    _proof(nested, preflight.NESTED_TOPOLOGY_PROOF_SCHEMA, wrong_artifact, receipt, {
        "single_controller_owner": True,
        "child_runtime_isolated": True,
        "descendants_reaped": True,
    }, provider_executable=codex)
    inputs["hard_egress_proof"] = hard
    inputs["nested_topology_proof"] = nested
    report = preflight.build_report(**inputs)
    by_key = {row["key"]: row for row in report["coordinates"]}
    assert "artifact_bound_hard_egress_proof" in by_key["hard-egress"]["actions"][0]["missing"]
    assert "artifact_bound_nested_topology_proof" in by_key["nested-harness"]["actions"][0]["missing"]


def test_environment_proof_must_bind_the_qualifying_receipt_integrity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs = _ready_inputs(tmp_path, monkeypatch)
    proof = inputs["hard_egress_proof"]
    assert isinstance(proof, Path)
    document = json.loads(proof.read_text(encoding="utf-8"))
    document["verification_receipt_integrity"] = "0" * 64
    proof.write_text(json.dumps(document), encoding="utf-8")

    report = preflight.build_report(**inputs)
    action = {
        row["key"]: row for row in report["coordinates"]
    }["hard-egress"]["actions"][0]
    assert action["status"] == "blocked"
    assert "artifact_bound_hard_egress_proof" in action["missing"]


def test_windows_detail_action_enumerates_exact_native_nodes() -> None:
    report = preflight.build_report(
        product_root=PRODUCT, environ={}, executable_finder=lambda _: None,
    )
    action = {
        row["key"]: row for row in report["coordinates"]
    }["windows-job-junction-launcher"]["actions"][0]
    assert tuple(action["job_object_selectors"]) == WINDOWS_JOB_SELECTORS
    assert tuple(action["junction_selectors"]) == WINDOWS_JUNCTION_SELECTORS
    assert tuple(action["selectors"]) == WINDOWS_JOB_SELECTORS + WINDOWS_JUNCTION_SELECTORS
    assert action["host_assertion_selector"] == (
        "tests/platform/test_native_process_contracts.py::"
        "test_expected_native_host_is_executing"
    )
    argv = action["next_argv"]
    for selector in (action["host_assertion_selector"], *action["selectors"]):
        assert preflight._resolved_selector(selector, PRODUCT) in argv
    assert action["cwd"] == str(PRODUCT)


def test_windows_workflow_runs_every_detail_node_and_always_uploads_junit() -> None:
    workflow = (PRODUCT / ".github/workflows/qualification.yml").read_text(encoding="utf-8")
    host_assertion = (
        "tests/platform/test_native_process_contracts.py::"
        "test_expected_native_host_is_executing"
    )
    for selector in (host_assertion, *WINDOWS_JOB_SELECTORS, *WINDOWS_JUNCTION_SELECTORS):
        assert selector in workflow
    assert "assert os.name == 'nt' and sys.platform == 'win32'" in workflow
    assert "--junitxml=native-windows-details.xml" in workflow
    assert "name: native-windows-details-evidence" in workflow
    assert "if: always()" in workflow
    assert "path: native-windows-details.xml" in workflow
