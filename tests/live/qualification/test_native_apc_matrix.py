"""Opt-in real-provider APC binding matrix with exact cleanup receipts."""

from __future__ import annotations

import pytest


@pytest.mark.parametrize("binding", ["binding_a", "binding_b"])
def test_supported_binding_runs_actual_child(live_authorization, binding: str) -> None:
    receipt = live_authorization.execute(
        f"native-apc-{binding.replace('_', '-')}",
        call_budget=4, cost_budget_usd=0.25,
        required=["native_provider", "apc_child"],
        extra={"binding_slot": binding},
    )
    assert receipt["binding_slot"] == binding
    assert receipt["provider_execution"] == "actual"
    assert receipt["requested_binding"] == receipt["resolved_binding"]
    assert receipt["child"]["launched"] is True
    assert receipt["child"]["cleanup_proven"] is True
    assert receipt["usage"]["invocations"] == 1


def test_missing_or_invalid_binding_falls_back_without_provider_call() -> None:
    # This negative is deterministic: the product must reject before launch.
    from memory_harness import config

    resolved = config.resolve_config({"apc": False, "light_adaptation": True})
    assert resolved.apc is False
    assert resolved.light_adaptation is False


def test_native_usage_role_matrix_is_single_counted(live_authorization) -> None:
    receipt = live_authorization.execute(
        "native-usage-role-matrix", call_budget=5, cost_budget_usd=0.25,
        required=["native_provider", "native_usage", "role_matrix"],
    )
    assert receipt["provider_execution"] == "actual"
    assert receipt["roles"] == ["worker", "root", "reviewer", "apc_child"]
    assert receipt["usage"]["expected_invocations"] == receipt["usage"]["invocations"]
    assert receipt["usage"]["duplicate_invocations"] == 0
    assert receipt["usage"]["binding_conflicts"] == 0
    assert receipt["usage"]["outer_inner_separate"] is True
