"""Joined preparation, finalization, dispatch, and outcome coverage for fixed modes."""

from __future__ import annotations

from pathlib import Path

import pytest

from memory_harness import config, context, contracts, preparation, runtime, search, store, templates


def _mandatory(card: dict, plan: dict, checkpoint: str) -> list[dict]:
    return [
        {"id": "task", "kind": "task", "content": card["task"]},
        {"id": "accepted-plan", "kind": "accepted-plan", "content": plan["content"]},
        {"id": "base", "kind": "base", "content": card["base_commit"]},
        {"id": "route", "kind": "route", "content": plan["route"]},
        {"id": "checkpoint", "kind": "checkpoint", "content": checkpoint},
        {"id": "security", "kind": "security", "content": context.ROLE_SEPARATION},
    ]


def _candidate(limits: config.PreparationLimits) -> dict:
    payload = {"summary": "reviewed parser failure and verified repair"}
    return {
        "kind": "historical_evidence", "logical_id": "case-fixed", "revision_id": "r1",
        "payload": payload, "payload_digest": contracts.sha256_hex(payload),
        "scope": {"app": "harness", "project": "product", "namespace": "reviewed", "owner": "ROOT"},
        "origin": "local", "score": 0.7, "freshness": "live",
        "representation": templates.representation_identity(limits=limits) | {
            "tokens": ["parser", "failure", "repair"], "route": "ordinary",
        },
    }


@pytest.mark.parametrize(
    ("strategy", "failure_context", "atlas_enabled", "expected_rounds"),
    [
        ("standard", None, True, 1),
        ("problem_focused", "parser raises IndexError after malformed input", True, 1),
        ("deeper", None, True, 3),
        ("deeper", None, False, 3),
    ],
)
def test_fixed_strategy_runs_joined_lifecycle(
    tmp_path: Path, strategy: str, failure_context: str | None,
    atlas_enabled: bool, expected_rounds: int,
) -> None:
    state = store.MemoryStore(tmp_path / "memory.sqlite3")
    state.initialize()
    try:
        limits = config.resolve_limits({
            "default_deadline_seconds": 300.0, "execution_reserve_seconds": 60.0,
            "deeper_rounds": 3,
        })
        selected_config = config.resolve_config({
            "strategy": strategy, "atlas_shared_retrieval": atlas_enabled,
        })
        service = preparation.PreparationService(
            store=state, config=selected_config, limits=limits,
        )
        objective = f"objective-{strategy}-{atlas_enabled}"
        plan = contracts.make_plan(
            plan_id=f"plan-{strategy}-{atlas_enabled}", objective_id=objective,
            route="ordinary", state="accepted", accepted_by="ROOT",
            content={"steps": ["inspect", "repair", "verify"]},
        )
        checkpoint = f"checkpoint-{strategy}-{atlas_enabled}"
        card = contracts.make_task_card(
            task="Repair the parser failure and verify it", base_commit="base-fixed",
            memory_handoff=contracts.make_memory_handoff(
                objective_id=objective, route="ordinary", plan=plan,
                checkpoint=checkpoint,
            ),
        )
        query_calls: list[dict] = []
        source = search.SearchStore(
            store_id="local-reviewed", kind="historical_evidence",
            query=lambda query: query_calls.append(query) or [_candidate(limits)],
        )
        result = service.prepare(
            task_card=card, plan=plan, objective_id=objective, route="ordinary",
            stores=[source], request={
                "strategy": strategy, "atlas_shared_retrieval": atlas_enabled,
            }, failure_context=failure_context, finalize=True,
            lane_id="lane-fixed", run_id="run-fixed", worktree_path=str(tmp_path),
            base_commit="base-fixed", checkpoint=checkpoint,
            execution_role="worker", invocation_target="harness:worker",
            recipient="worker:lane-fixed", mandatory_content=_mandatory(card, plan, checkpoint),
        )
        assert result.dispatchable
        assert result.preparation["strategy"] == strategy
        assert result.trace["rounds"] == expected_rounds
        assert len(query_calls) == expected_rounds + 1  # search rounds plus final live recheck
        assert result.context["decision_id"] == result.decision["decision_id"]
        assert result.context["strategy"] == strategy
        if not atlas_enabled:
            assert result.decision["configuration"]["atlas_shared_retrieval"] is False

        product = runtime.MemoryRuntime(state, config=selected_config)
        product.record_dispatch_intent(result.envelope)
        native = {
            "invocation_id": "controller:17:created", "pid": 17,
            "creation_time": "created", "task_card_digest": card["content_hash"],
            "decision_id": result.decision["decision_id"], "plan_id": plan["plan_id"],
            "plan_digest": plan["content_hash"],
            "envelope_digest": result.envelope["content_hash"],
            "lane_id": "lane-fixed", "run_id": "run-fixed", "base_commit": "base-fixed",
            "route": "ordinary",
            "configuration_digest": result.decision["configuration_digest"],
            "context_id": result.context["context_id"],
            "context_digest": result.context["content_hash"],
        }
        operation = product.record_observed_dispatch(result.envelope, native)
        durable_operation = {key: operation[key] for key in (
            "operation_id", "decision_id", "envelope_digest", "run_id", "kind",
            "status", "observed_invocation", "created_at", "updated_at",
        )}
        terminal_result = {
            "schema": "result/v1", "lane_id": "lane-fixed", "run_id": "run-fixed",
            "outcome": "PASS",
        }
        terminal_result["content_hash"] = contracts.content_hash(terminal_result)
        review = {
            "schema": "completion-review/v1", "lane_id": "lane-fixed",
            "run_id": "run-fixed", "review_outcome": "PASS",
            "task_card_id": card["content_hash"], "task_card_hash": card["content_hash"],
            "result_id": "run-fixed", "result_hash": terminal_result["content_hash"],
            "commit": "commit-fixed", "reviewed_at": "2026-10-02T00:00:00Z",
        }
        review["content_hash"] = contracts.content_hash(review)
        acceptance = {
            "schema": "orchestrator-acceptance/v1", "lane_id": "lane-fixed",
            "run_id": "run-fixed", "approval": "ACCEPTED", "accepted_by": "ROOT",
            "review_ref": review["content_hash"], "task_card_id": card["content_hash"],
            "task_card_hash": card["content_hash"], "result_id": "run-fixed",
            "result_hash": terminal_result["content_hash"], "commit": "commit-fixed",
            "decided_at": review["reviewed_at"],
        }
        acceptance["content_hash"] = contracts.content_hash(acceptance)
        evidence = {
            "schema": "native-terminal-evidence/v1", "epoch_id": "epoch-fixed",
            "lane_id": "lane-fixed", "run_id": "run-fixed", "task_card": card,
            "accepted_plan": plan, "objective_id": objective,
            "decision_id": result.decision["decision_id"], "decision": result.decision,
            "final_context": result.context,
            "dispatch": {
                "operation": durable_operation,
                "operation_digest": contracts.sha256_hex(durable_operation),
                "envelope_digest": result.envelope["content_hash"],
                "envelope": result.envelope, "observed_invocation": native,
            },
            "configuration": result.decision["configuration"],
            "configuration_digest": result.decision["configuration_digest"],
            "result": terminal_result, "review": review, "acceptance": acceptance,
            "terminal_proof": None,
        }
        evidence["content_hash"] = contracts.content_hash(evidence)
        outcome = product.record_terminal_outcome(evidence)
        assert operation["status"] == "delivered"
        assert outcome["status"] == "PASS"
        assert state.get_final_context_for_decision(result.decision["decision_id"]) == result.context
        assert state.get_outcome(result.decision["decision_id"]) == outcome
    finally:
        state.close()


def test_all_off_bypasses_every_memory_service_and_learned_mode_fails_before_state(
    tmp_path: Path,
) -> None:
    state = store.MemoryStore(tmp_path / "memory.sqlite3")
    state.initialize()
    try:
        plan = contracts.make_plan(
            plan_id="plan-off", objective_id="objective-off", route="ordinary",
            state="candidate", content={"steps": ["ordinary work"]},
        )
        card = contracts.make_task_card(task="ordinary work", base_commit="base")
        calls: list[dict] = []
        source = search.SearchStore(
            store_id="must-not-run", kind="historical_evidence",
            query=lambda query: calls.append(query) or (_ for _ in ()).throw(
                AssertionError("all-off queried memory")
            ),
        )
        result = preparation.PreparationService(
            store=state, config=config.all_off(),
        ).prepare(
            task_card=card, plan=plan, objective_id="objective-off",
            route="ordinary", stores=[source],
        )
        assert result.mode == "inherited"
        assert result.decision is None and result.envelope is None
        assert calls == []
        assert state.connection.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 0
        with pytest.raises(config.DeferredCapabilityError, match="deferred/not implemented"):
            config.resolve_config({"strategy": "learned", "policy_load": True})
        assert state.connection.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 0
    finally:
        state.close()
