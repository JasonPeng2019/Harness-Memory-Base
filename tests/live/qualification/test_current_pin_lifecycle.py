"""Opt-in current-pin Atlas, EverOS, provider, and remote recovery proof."""

from __future__ import annotations


def test_current_pin_baseline_lifecycle(live_authorization) -> None:
    receipt = live_authorization.execute(
        "native-baseline-lifecycle", call_budget=4, cost_budget_usd=0.10,
        required=["native_provider", "baseline", "workspace_composition"],
    )
    assert receipt["provider_execution"] == "actual"
    assert receipt["baseline"] == {
        "memory_calls": 0, "benchmark_executed": False, "lifecycle": "settled",
    }
    assert receipt["composition"] == {
        "passes": 2,
        "families_preserved": ["harness", "suite"],
        "ownership_conflict_probe": "rejected-before-mutation",
        "interrupted_restore_probe": "restored-before-dispatch",
    }


def test_current_pin_enhanced_lifecycle(live_authorization) -> None:
    receipt = live_authorization.execute(
        "enhanced-lifecycle", call_budget=8, cost_budget_usd=0.50,
        required=["atlas", "everos", "native_provider", "linked_outcome"],
    )
    assert receipt["services"] == {
        "atlas": "actual", "everos": "actual", "native_provider": "actual",
    }
    assert receipt["lifecycle"] == [
        "prepared", "root_accepted", "finalized", "dispatched", "reviewed", "settled",
    ]
    assert receipt["outcome"]["linked"] is True
    assert receipt["usage"]["coverage"] in {"complete", "incomplete"}


def test_remote_snapshot_restore(live_authorization) -> None:
    receipt = live_authorization.execute(
        "remote-snapshot-restore", call_budget=4, cost_budget_usd=0.25,
        required=["atlas", "everos", "snapshot", "restore"],
    )
    assert receipt["services"] == {"atlas": "actual", "everos": "actual"}
    assert receipt["snapshot"]["published"] is True
    assert receipt["snapshot"]["digest_verified"] is True
    assert receipt["restore"]["completed"] is True
    assert receipt["restore"]["namespace_isolated"] is True


def test_hard_egress_and_content_privacy(live_authorization) -> None:
    receipt = live_authorization.execute(
        "hard-egress-content-privacy", call_budget=4, cost_budget_usd=0.15,
        required=["native_provider", "hard_egress", "privacy_probe"],
    )
    assert receipt["provider_execution"] == "actual"
    assert receipt["egress"] == {
        "independently_verified": True,
        "restricted_forbidden_call_count": 0,
    }
    assert receipt["privacy"]["authoritative_source_unchanged"] is True
    assert receipt["privacy"]["secret_absent_from"] == [
        "prompt", "query", "embedding", "adaptation", "telemetry", "logs",
    ]


def test_nested_harness_ownership_topology(live_authorization) -> None:
    receipt = live_authorization.execute(
        "nested-harness-ownership", call_budget=4, cost_budget_usd=0.15,
        required=["native_provider", "nested_harness", "cleanup"],
    )
    assert receipt["provider_execution"] == "actual"
    assert receipt["candidate"]["commit"]
    assert receipt["ownership"] == {
        "outer_inner_disjoint": True,
        "delegation_chain_exact": True,
        "candidate_entrypoint_bound": True,
        "outer_sentinel_unchanged": True,
    }
    assert receipt["inner_cleanup"]["complete"] is True
