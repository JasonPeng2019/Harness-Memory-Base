from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from memory_harness import config, contracts, store

from orchestrator_harness import memory_handoff, product_composition, settlement
from orchestrator_harness.bootstrap import _remove_owned_tree
from orchestrator_harness.core import content_hash
from orchestrator_harness.tests.support import (
    configure_local_memory_product,
)


class OutcomeSettlementTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(_remove_owned_tree, self.root, timeout_seconds=5.0)
        self.harness = self.root / "harness"
        self.harness.mkdir()
        self.scope = configure_local_memory_product(
            self.harness, store_root=self.root / "operator-memory"
        )
        self.worktree = self.root / "worktree"
        self.worktree.mkdir()
        self.plan = contracts.make_plan(
            plan_id="plan-1",
            objective_id="objective-1",
            route="ordinary",
            state="accepted",
            content={"steps": ["implement", "verify"]},
            accepted_by="ROOT",
        )
        self.card = contracts.make_task_card(
            task="repair the parser regression",
            base_commit="base-1",
            memory_handoff=contracts.make_memory_handoff(
                objective_id="objective-1",
                route="ordinary",
                plan=self.plan,
                checkpoint="checkpoint-1",
            ),
        )
        self.decision = contracts.make_decision(
            self.card,
            self.plan,
            configuration=config.configuration_record(config.resolve_config()),
        )
        review = {
            "schema": "completion-review/v1",
            "review_outcome": "PASS",
            "review_summary": "ROOT confirmed the parser repair",
            "reviewed_at": "2026-10-02T00:00:00Z",
            "commit": "commit-1",
            "failed_hypotheses": ["the network caused the parser regression"],
        }
        review["content_hash"] = content_hash(review)
        acceptance = {
            "schema": "orchestrator-acceptance/v1",
            "approval": "ACCEPTED",
        }
        acceptance["content_hash"] = content_hash(acceptance)
        result = {"schema": "result/v1", "outcome": "PASS"}
        result["content_hash"] = content_hash(result)
        self.evidence = {
            "schema": "native-terminal-evidence/v1",
            "run_id": "run-1",
            "objective_id": "objective-1",
            "decision_id": self.decision["decision_id"],
            "task_card": self.card,
            "accepted_plan": self.plan,
            "decision": self.decision,
            "review": review,
            "acceptance": acceptance,
            "result": result,
        }
        self.evidence["content_hash"] = content_hash(self.evidence)
        self.outcome = contracts.make_outcome(
            decision_id=self.decision["decision_id"],
            plan_id=self.plan["plan_id"],
            plan_digest=self.plan["content_hash"],
            status="PASS",
            evidence_digest=self.evidence["content_hash"],
            linked_run_id="run-1",
            task_card_digest=self.card["content_hash"],
            objective_id="objective-1",
            observed_at=review["reviewed_at"],
        )
        (self.worktree / ".agent-workspace").mkdir()
        self.local = store.MemoryStore(
            self.worktree / ".agent-workspace" / "memory-state.sqlite3"
        )
        self.local.initialize()
        self.local.record_decision(self.decision)
        self.local.record_outcome(self.outcome)
        # Production creates these in the same transaction as the exact native
        # outcome.  This focused coordinator fixture starts from the equivalent
        # durable boundary without rebuilding the entire controller transcript.
        with self.local._require_connection():
            self.local._create_local_effect_intents(self.outcome, self.decision)
        self.addCleanup(self.local.close)

    def _configured_case(
        self, *, objective: str, requested: dict, status: str
    ) -> tuple[Path, dict, dict, store.MemoryStore]:
        worktree = self.root / objective
        (worktree / ".agent-workspace").mkdir(parents=True)
        plan = contracts.make_plan(
            plan_id=f"{objective}-plan",
            objective_id=objective,
            route="ordinary",
            state="accepted",
            content={"steps": ["implement", "verify"]},
            accepted_by="ROOT",
        )
        card = contracts.make_task_card(
            task=f"complete {objective}",
            base_commit="base-1",
            memory_handoff=contracts.make_memory_handoff(
                objective_id=objective,
                route="ordinary",
                plan=plan,
                configuration=requested,
                checkpoint="checkpoint-1",
            ),
        )
        decision = contracts.make_decision(
            card,
            plan,
            configuration=config.configuration_record(config.resolve_config(requested)),
        )
        review = {
            "schema": "completion-review/v1",
            "review_outcome": status,
            "review_summary": f"ROOT reviewed {objective}",
            "reviewed_at": "2026-10-02T00:00:00Z",
            "commit": "commit-1",
            "failed_hypotheses": [],
        }
        review["content_hash"] = content_hash(review)
        acceptance = {
            "schema": "orchestrator-acceptance/v1",
            "approval": "ACCEPTED",
        }
        acceptance["content_hash"] = content_hash(acceptance)
        evidence = {
            "schema": "native-terminal-evidence/v1",
            "run_id": f"{objective}-run",
            "objective_id": objective,
            "decision_id": decision["decision_id"],
            "task_card": card,
            "accepted_plan": plan,
            "decision": decision,
            "review": review,
            "acceptance": acceptance,
        }
        if status == "UNKNOWN":
            proof = {"schema": "terminal-proof/v1", "state": "unknown"}
            proof["content_hash"] = content_hash(proof)
            evidence["result"] = None
            evidence["terminal_proof"] = proof
        else:
            result = {"schema": "result/v1", "outcome": status}
            result["content_hash"] = content_hash(result)
            evidence["result"] = result
        evidence["content_hash"] = content_hash(evidence)
        outcome = contracts.make_outcome(
            decision_id=decision["decision_id"],
            plan_id=plan["plan_id"],
            plan_digest=plan["content_hash"],
            status=status,
            evidence_digest=evidence["content_hash"],
            linked_run_id=evidence["run_id"],
            task_card_digest=card["content_hash"],
            objective_id=objective,
            observed_at=review["reviewed_at"],
        )
        local = store.MemoryStore(
            worktree / ".agent-workspace" / "memory-state.sqlite3"
        )
        local.initialize()
        local.record_decision(decision)
        local.record_outcome(outcome)
        with local._require_connection():
            local._create_local_effect_intents(outcome, decision)
        self.addCleanup(local.close)
        return worktree, card, evidence, local

    def test_exact_replay_is_idempotent_and_never_publishes_procedure(self) -> None:
        with product_composition.compose_product(
            harness_root=self.harness,
            task_card=self.card,
            route="ordinary",
        ) as composed:
            first = settlement.settle_accepted_outcome(
                worktree_path=self.worktree,
                evidence=self.evidence,
                local_store=self.local,
                composition=composed,
            )
            second = settlement.settle_accepted_outcome(
                worktree_path=self.worktree,
                evidence=self.evidence,
                local_store=self.local,
                composition=composed,
            )
            self.assertEqual(first, second)
            self.assertEqual("not_configured", first["everos"]["status"])
            self.assertFalse(first["everos"]["called"])
            self.assertFalse(first["generated_skill"]["approved"])
            self.assertFalse(first["generated_skill"]["published"])
            self.assertEqual("confirmed", first["local_effects"]["review_receipt"])
            self.assertEqual("confirmed", first["local_effects"]["recent_evidence"])
            self.assertEqual(
                first["central_trajectory_id"],
                composed.store.get_reviewed_trajectory(
                    first["central_trajectory_id"]
                )["trajectory_id"],
            )
            self.assertEqual(
                0,
                composed.store._require_connection().execute(
                    "SELECT count(*) FROM procedure_publications"
                ).fetchone()[0],
            )

    def test_partial_local_settlement_recovers_when_central_store_returns(self) -> None:
        with product_composition.compose_product(
            harness_root=self.harness,
            task_card=self.card,
            route="ordinary",
        ) as composed:
            composed.store.close()
            with self.assertRaisesRegex(
                settlement.OutcomeSettlementError, "settlement failed"
            ):
                settlement.settle_accepted_outcome(
                    worktree_path=self.worktree,
                    evidence=self.evidence,
                    local_store=self.local,
                    composition=composed,
                )
            self.assertEqual(
                1,
                self.local._require_connection().execute(
                    "SELECT count(*) FROM reviewed_trajectories"
                ).fetchone()[0],
            )
            composed.store.initialize()
            recovered = settlement.settle_accepted_outcome(
                worktree_path=self.worktree,
                evidence=self.evidence,
                local_store=self.local,
                composition=composed,
            )
            self.assertEqual("settled", recovered["status"])
            self.assertEqual(
                1,
                self.local._require_connection().execute(
                    "SELECT count(*) FROM reviewed_trajectories"
                ).fetchone()[0],
            )
            self.assertEqual(
                1,
                composed.store._require_connection().execute(
                    "SELECT count(*) FROM reviewed_trajectories"
                ).fetchone()[0],
            )

    def test_accepted_native_review_invokes_product_closeout(self) -> None:
        runtime = MagicMock()
        runtime.record_terminal_outcome.return_value = dict(self.outcome)
        composition = MagicMock()
        context = MagicMock()
        context.__enter__.return_value = composition
        context.__exit__.return_value = None
        with (
            patch.object(
                memory_handoff,
                "_open_runtime",
                return_value=(self.local, runtime),
            ),
            patch(
                "orchestrator_harness.config.find_harness_root",
                return_value=self.harness,
            ),
            patch.object(product_composition, "configured", return_value=True),
            patch.object(
                product_composition,
                "compose_product",
                return_value=context,
            ) as compose,
            patch.object(settlement, "settle_accepted_outcome") as settle,
        ):
            result = memory_handoff.record_native_review(
                worktree_path=self.worktree,
                evidence=self.evidence,
            )
        self.assertEqual(self.outcome, result)
        compose.assert_called_once_with(
            harness_root=self.harness,
            task_card=self.card,
            route="ordinary",
        )
        settle.assert_called_once_with(
            worktree_path=self.worktree,
            evidence=self.evidence,
            local_store=self.local,
            composition=composition,
        )

    def test_experience_write_off_retains_review_without_searchable_trajectory(self) -> None:
        worktree, card, evidence, local = self._configured_case(
            objective="experience-write-off",
            requested={"experience_write": False},
            status="PASS",
        )
        with product_composition.compose_product(
            harness_root=self.harness,
            task_card=card,
            route="ordinary",
        ) as composed:
            first = settlement.settle_accepted_outcome(
                worktree_path=worktree,
                evidence=evidence,
                local_store=local,
                composition=composed,
            )
            second = settlement.settle_accepted_outcome(
                worktree_path=worktree,
                evidence=evidence,
                local_store=local,
                composition=composed,
            )
            self.assertEqual(first, second)
            self.assertIsNone(first["trajectory_id"])
            self.assertIsNone(first["central_trajectory_id"])
            self.assertEqual("experience_write_disabled", first["trajectory_status"])
            self.assertEqual({"review_receipt": "confirmed"}, first["local_effects"])
            self.assertEqual([], local.list_reviewed_trajectories(self.scope))
            self.assertEqual(
                [], composed.store.list_reviewed_trajectories(self.scope)
            )
            self.assertIsNotNone(
                local.get_review_receipt(first["review_receipt_id"])
            )
            self.assertIsNotNone(
                composed.store.get_review_receipt(
                    first["central_review_receipt_id"]
                )
            )

    def test_generated_skill_off_makes_zero_generation_capable_calls(self) -> None:
        worktree, card, evidence, local = self._configured_case(
            objective="skill-generation-off",
            requested={
                "experience_write": True,
                "generated_skill_creation": False,
            },
            status="PASS",
        )
        with product_composition.compose_product(
            harness_root=self.harness,
            task_card=card,
            route="ordinary",
        ) as composed:
            adapter = MagicMock()
            composed.everos_adapter = adapter
            with patch.object(
                composed.experience_service,
                "extract_trajectory",
                wraps=composed.experience_service.extract_trajectory,
            ) as extract:
                settled = settlement.settle_accepted_outcome(
                    worktree_path=worktree,
                    evidence=evidence,
                    local_store=local,
                    composition=composed,
                )
            extract.assert_not_called()
            adapter.search_skill_candidates.assert_not_called()
            self.assertFalse(settled["everos"]["called"])
            self.assertEqual(
                "generation_capable_backend_suppressed",
                settled["everos"]["status"],
            )
            self.assertEqual("creation_disabled", settled["generated_skill"]["status"])

    def test_accepted_unknown_settles_idempotently_without_trajectory(self) -> None:
        worktree, card, evidence, local = self._configured_case(
            objective="accepted-unknown",
            requested={},
            status="UNKNOWN",
        )
        with product_composition.compose_product(
            harness_root=self.harness,
            task_card=card,
            route="ordinary",
        ) as composed:
            first = settlement.settle_accepted_outcome(
                worktree_path=worktree,
                evidence=evidence,
                local_store=local,
                composition=composed,
            )
            second = settlement.settle_accepted_outcome(
                worktree_path=worktree,
                evidence=evidence,
                local_store=local,
                composition=composed,
            )
            self.assertEqual(first, second)
            self.assertIsNone(first["trajectory_id"])
            self.assertIsNone(first["central_trajectory_id"])
            self.assertEqual("not_applicable_unknown", first["trajectory_status"])
            self.assertEqual({"review_receipt": "confirmed"}, first["local_effects"])
            self.assertEqual([], local.list_reviewed_trajectories(self.scope))
            self.assertEqual(
                [], composed.store.list_reviewed_trajectories(self.scope)
            )
            self.assertIsNotNone(
                local.get_review_receipt(first["review_receipt_id"])
            )


if __name__ == "__main__":
    unittest.main()
