"""Fixed native outcomes retain exact, independently recoverable effect work."""

from __future__ import annotations

import sys
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from memory_harness import config, context, contracts, runtime, store
from tests.local.contracts import test_terminal_outcome as fixture_module


class LocalEffectStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.case = fixture_module.TerminalOutcomeTests(methodName="test_exact_observed_native_chain_fixes_pass")
        self.case.setUp()

    def tearDown(self) -> None:
        self.case.tearDown()

    def test_fixed_outcome_retains_waiting_intents_atomically(self) -> None:
        case = self.case
        case.observe()
        outcome = case.runtime.record_terminal_outcome(case.bundle())
        effects = case.state.list_effect_operations(outcome["outcome_id"])
        self.assertEqual(
            {"review_receipt", "recent_evidence", "experience_ingestion", "generated_skill_creation"},
            {effect["kind"] for effect in effects},
        )
        self.assertTrue(all(effect["status"] == "waiting_source" for effect in effects))
        self.assertTrue(all(effect["source_record"] is None and effect["payload_record"] is None for effect in effects))
        self.assertEqual(effects, case.state.list_effect_operations(outcome["outcome_id"], actionable_only=True))
        self.assertEqual(outcome, case.runtime.record_terminal_outcome(case.bundle()))
        self.assertEqual(effects, case.state.list_effect_operations(outcome["outcome_id"]))

    def test_no_outcome_means_no_intents(self) -> None:
        self.case.observe()
        self.assertEqual([], self.case.state.list_effect_operations())
        self.case.state.close()
        self.case.state = store.MemoryStore(self.case.root / "state.sqlite3")
        self.case.state.initialize()
        self.assertEqual([], self.case.state.list_effect_operations())

    def _review(self, outcome: dict) -> tuple[dict, dict]:
        case = self.case
        outcome = contracts.make_outcome(
            decision_id=outcome["decision_id"], plan_id=outcome["plan_id"],
            plan_digest=outcome["plan_digest"], status=outcome["status"],
            evidence_digest=outcome["evidence_digest"], linked_run_id=outcome["linked_run_id"],
            task_card_digest=outcome["task_card_digest"], objective_id=outcome["objective_id"],
            observed_at=outcome["observed_at"],
        )
        receipt = contracts.make_review_receipt(
            review_id="review-1", outcome=outcome, decision=case.decision,
            task_card=case.card, plan=case.plan, reviewed_by="ROOT",
            evidence_refs=("review://run-1",), raw_evidence="ROOT reviewed exact run evidence.",
        )
        case.state.record_review_receipt(receipt)
        trajectory = contracts.make_reviewed_trajectory(
            task_card=case.card, plan=case.plan, decision=case.decision,
            outcome=outcome, review_receipt=receipt,
            scope={"application": "harness", "project": "product",
                   "namespace": "isolated", "owner": "root-agent"},
        )
        case.state.record_reviewed_trajectory(trajectory)
        return receipt, trajectory

    def test_review_evidence_survives_outage_and_payload_readiness(self) -> None:
        case = self.case
        case.observe()
        outcome = case.runtime.record_terminal_outcome(case.bundle())
        receipt, trajectory = self._review(outcome)
        effects = {effect["kind"]: effect for effect in case.state.list_effect_operations(outcome["outcome_id"])}
        self.assertEqual("confirmed", effects["review_receipt"]["status"])
        self.assertEqual(receipt, effects["review_receipt"]["source_record"])
        self.assertEqual("confirmed", effects["recent_evidence"]["status"])
        self.assertEqual(trajectory, effects["recent_evidence"]["source_record"])
        self.assertEqual("waiting_payload", effects["experience_ingestion"]["status"])
        self.assertEqual("waiting_payload", effects["generated_skill_creation"]["status"])
        self.assertIsNone(effects["experience_ingestion"]["payload_record"])
        self.assertEqual(2, len(case.state.list_effect_operations(outcome["outcome_id"], actionable_only=True)))
        operation = case.state.bind_effect_payload(
            effects["experience_ingestion"]["operation_id"], {"exact": "adapter payload"},
        )
        self.assertEqual("pending", operation["status"])
        self.assertEqual({"exact": "adapter payload"}, operation["payload_record"])

    def test_reopen_claim_uncertainty_confirmation_and_conflict(self) -> None:
        case = self.case
        case.observe()
        outcome = case.runtime.record_terminal_outcome(case.bundle())
        self._review(outcome)
        operation_id = contracts.effect_operation_id(outcome["outcome_id"], "experience_ingestion")
        case.state.bind_effect_payload(operation_id, {"payload": "exact"})
        other = store.MemoryStore(case.root / "state.sqlite3")
        other.initialize()
        try:
            claimed = case.state.claim_effect_operation(operation_id, current_config=config.MemoryConfig())
            self.assertEqual("in_flight", claimed["status"])
            with self.assertRaises(store.OperationConflictError):
                other.claim_effect_operation(operation_id, current_config=config.MemoryConfig())
            uncertain = other.mark_effect_uncertain(operation_id, "acknowledgement lost")
            self.assertEqual("uncertain", uncertain["status"])
            with self.assertRaises(store.OperationConflictError):
                case.state.claim_effect_operation(operation_id, current_config=config.MemoryConfig())
            confirmed = case.state.confirm_effect_operation(operation_id, {"receipt": "remote-1"})
            self.assertEqual("confirmed", confirmed["status"])
            self.assertEqual([], [e for e in case.state.list_effect_operations(actionable_only=True)
                                  if e["operation_id"] == operation_id])
            with self.assertRaises(store.OperationConflictError):
                other.bind_effect_payload(operation_id, {"payload": "different"})
            with self.assertRaises(store.OperationConflictError):
                other.confirm_effect_operation(operation_id, {"receipt": "remote-2"})
            case.state.close()
            case.state = store.MemoryStore(case.root / "state.sqlite3")
            case.state.initialize()
            self.assertEqual("confirmed", case.state.get_effect_operation(operation_id)["status"])
        finally:
            other.close()

    def test_current_off_blocks_claim_without_erasing_local_evidence(self) -> None:
        case = self.case
        case.observe()
        outcome = case.runtime.record_terminal_outcome(case.bundle())
        self._review(outcome)
        operation_id = contracts.effect_operation_id(outcome["outcome_id"], "experience_ingestion")
        case.state.bind_effect_payload(operation_id, {"payload": "exact"})
        with self.assertRaisesRegex(store.OperationConflictError, "off"):
            case.state.claim_effect_operation(
                operation_id, current_config=config.resolve_config({"experience_write": False}),
            )
        self.assertEqual("pending", case.state.get_effect_operation(operation_id)["status"])
        self.assertEqual("confirmed", case.state.get_effect_operation(
            contracts.effect_operation_id(outcome["outcome_id"], "recent_evidence"))["status"])
        generation_id = contracts.effect_operation_id(outcome["outcome_id"], "generated_skill_creation")
        case.state.bind_effect_payload(generation_id, {"candidate": "exact proposed content"})
        with self.assertRaisesRegex(store.OperationConflictError, "off"):
            case.state.claim_effect_operation(
                generation_id,
                current_config=config.resolve_config({"generated_skill_creation": False}),
            )
        self.assertEqual("pending", case.state.get_effect_operation(generation_id)["status"])

    def test_wrong_source_and_payload_identity_are_fenced(self) -> None:
        case = self.case
        case.observe()
        outcome = case.runtime.record_terminal_outcome(case.bundle())
        _, trajectory = self._review(outcome)
        operation_id = contracts.effect_operation_id(outcome["outcome_id"], "experience_ingestion")
        forged = dict(trajectory, outcome_id="wrong-outcome")
        with self.assertRaises(store.OperationConflictError):
            case.state.bind_effect_source(operation_id, trajectory["trajectory_id"], forged)
        case.state.bind_effect_payload(operation_id, {"payload": "exact"})
        with self.assertRaises(store.OperationConflictError):
            case.state.bind_effect_payload(operation_id, {"payload": "different"})

    def test_restart_after_fixation_recovers_only_missing_work(self) -> None:
        case = self.case
        case.observe()
        outcome = case.runtime.record_terminal_outcome(case.bundle())
        case.state.close()
        case.state = store.MemoryStore(case.root / "state.sqlite3")
        case.state.initialize()
        self.assertEqual("PASS", case.state.get_outcome(case.decision["decision_id"])["status"])
        self.assertEqual(4, len(case.state.list_effect_operations(outcome["outcome_id"], actionable_only=True)))
        self._review(outcome)
        self.assertEqual(2, len(case.state.list_effect_operations(outcome["outcome_id"], actionable_only=True)))

    def _recapture(self, configuration: dict) -> None:
        case = self.case
        case.configuration = configuration
        case.decision = contracts.make_decision(case.card, case.plan, configuration=case.configuration)
        case.state.record_decision(case.decision)
        case.final = context.finalize_context(
            task_card=case.card, plan=case.plan, decision_id=case.decision["decision_id"],
            lane_id=case.lane_id, run_id=case.run_id,
            worktree_path=str(case.root / "worktree"), base_commit="base-1",
            strategy="standard", configuration=case.configuration,
            checkpoint="checkpoint-1", execution_role="worker",
            invocation_target="harness:worker", recipient="worker:lane-1",
            mandatory_content=case.final.envelope["mandatory_content"],
            limits=config.resolve_limits({"context_char_limit": 4000}),
        )
        case.state.record_final_context(
            case.final.context, envelope_digest=case.final.envelope["content_hash"],
        )

    def test_captured_write_off_keeps_local_intents_only(self) -> None:
        case = self.case
        self._recapture({"strategy": "standard", "experience_write": False,
                         "generated_skill_creation": True})
        case.observe()
        outcome = case.runtime.record_terminal_outcome(case.bundle())
        effects = case.state.list_effect_operations(outcome["outcome_id"])
        self.assertEqual({"review_receipt", "recent_evidence"}, {effect["kind"] for effect in effects})
        self.assertTrue(all(effect["configuration_digest"] == case.decision["configuration_digest"]
                            for effect in effects))
        self._review(outcome)
        self.assertEqual([], case.state.list_effect_operations(outcome["outcome_id"], actionable_only=True))

    def test_captured_generation_off_retains_ingestion_intent(self) -> None:
        case = self.case
        self._recapture({"strategy": "standard", "experience_write": True,
                         "generated_skill_creation": False})
        case.observe()
        outcome = case.runtime.record_terminal_outcome(case.bundle())
        self.assertEqual(
            {"review_receipt", "recent_evidence", "experience_ingestion"},
            {effect["kind"] for effect in case.state.list_effect_operations(outcome["outcome_id"])},
        )

    def test_generic_legacy_outcome_does_not_gain_effect_operations(self) -> None:
        case = self.case
        legacy = store.MemoryStore(case.root / "legacy.sqlite3")
        legacy.initialize()
        try:
            legacy.record_decision(case.decision)
            outcome = contracts.make_outcome(
                decision_id=case.decision["decision_id"], plan_id=case.plan["plan_id"],
                plan_digest=case.plan["content_hash"], status="PASS",
                evidence_digest="legacy-evidence", linked_run_id="legacy-run",
                task_card_digest=case.card["content_hash"], objective_id=case.plan["objective_id"],
            )
            legacy.record_outcome(outcome)
            self.assertEqual([], legacy.list_effect_operations())
            legacy.close()
            legacy.initialize()
            self.assertEqual([], legacy.list_effect_operations())
        finally:
            legacy.close()

    def test_old_native_outcome_gets_intents_on_migration(self) -> None:
        case = self.case
        case.observe()
        outcome = case.runtime.record_terminal_outcome(case.bundle())
        case.state.connection.execute("DELETE FROM effect_operations")
        case.state.connection.commit()
        case.state.close()
        case.state = store.MemoryStore(case.root / "state.sqlite3")
        case.state.initialize()
        self.assertEqual(4, len(case.state.list_effect_operations(outcome["outcome_id"])))
        self.assertEqual("PASS", case.state.get_outcome(case.decision["decision_id"])["status"])

    def test_existing_ingestion_identity_blocks_fresh_submission(self) -> None:
        case = self.case
        case.observe()
        outcome = case.runtime.record_terminal_outcome(case.bundle())
        _, trajectory = self._review(outcome)
        payload = {"payload": "exact"}
        operation_id = contracts.effect_operation_id(outcome["outcome_id"], "experience_ingestion")
        case.state.bind_effect_payload(operation_id, payload)
        ingestion = contracts.make_experience_ingestion(
            trajectory=trajectory, destination="everos", session_id="session-1",
            payload_digest=contracts.sha256_hex(payload),
        )
        persisted, created = case.state.create_experience_ingestion(ingestion)
        self.assertTrue(created)
        self.assertEqual(ingestion["ingestion_id"], persisted["ingestion_id"])
        effect = case.state.get_effect_operation(operation_id)
        self.assertEqual(ingestion["ingestion_id"], effect["adapter_operation_id"])
        with self.assertRaisesRegex(store.OperationConflictError, "reconciliation"):
            case.state.claim_effect_operation(operation_id, current_config=config.MemoryConfig())

    def test_simultaneous_connections_claim_only_once(self) -> None:
        case = self.case
        case.observe()
        outcome = case.runtime.record_terminal_outcome(case.bundle())
        self._review(outcome)
        operation_id = contracts.effect_operation_id(outcome["outcome_id"], "generated_skill_creation")
        case.state.bind_effect_payload(operation_id, {"candidate": "exact"})
        barrier = threading.Barrier(2)

        def claim() -> str:
            peer = store.MemoryStore(case.root / "state.sqlite3")
            peer.initialize()
            try:
                barrier.wait(timeout=5)
                try:
                    peer.claim_effect_operation(operation_id, current_config=config.MemoryConfig())
                    return "claimed"
                except store.OperationConflictError:
                    return "fenced"
            finally:
                peer.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: claim(), range(2)))
        self.assertEqual(["claimed", "fenced"], sorted(results))
        self.assertEqual("in_flight", case.state.get_effect_operation(operation_id)["status"])


if __name__ == "__main__":
    unittest.main()
