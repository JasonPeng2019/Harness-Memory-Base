"""The enhanced parent fixes quality only from its exact native terminal chain."""

from __future__ import annotations

import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from memory_harness import config, context, contracts, runtime, store


class TerminalOutcomeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.state = store.MemoryStore(self.root / "state.sqlite3")
        self.state.initialize()
        self.runtime = runtime.MemoryRuntime(self.state)
        self.prepare_case()

    def prepare_case(self, *, plan_id: str = "plan-1", run_id: str = "run-1",
                     lane_id: str = "lane-1", supersedes: str | None = None) -> None:
        self.run_id, self.lane_id = run_id, lane_id
        self.plan = contracts.make_plan(
            plan_id=plan_id, objective_id="objective-1", route="ordinary",
            state="accepted", accepted_by="ROOT", content={"steps": ["verify", plan_id]},
            supersedes=supersedes,
        )
        self.card = contracts.make_task_card(
            task="Verify the change", base_commit="base-1",
            memory_handoff=contracts.make_memory_handoff(
                objective_id="objective-1", route="ordinary", plan=self.plan,
                checkpoint="checkpoint-1",
            ),
        )
        self.configuration = {"strategy": "standard"}
        self.decision = contracts.make_decision(
            self.card, self.plan, configuration=self.configuration,
        )
        self.state.record_decision(self.decision)
        self.final = context.finalize_context(
            task_card=self.card, plan=self.plan,
            decision_id=self.decision["decision_id"], lane_id=lane_id, run_id=run_id,
            worktree_path=str(self.root / "worktree"), base_commit="base-1",
            strategy="standard", configuration=self.configuration,
            checkpoint="checkpoint-1", execution_role="worker",
            invocation_target="harness:worker", recipient="worker:lane-1",
            mandatory_content=[
                {"id": "task", "kind": "task", "content": self.card["task"]},
                {"id": "accepted-plan", "kind": "accepted-plan", "content": self.plan["content"]},
                {"id": "base", "kind": "base", "content": "base-1"},
                {"id": "route", "kind": "route", "content": "ordinary"},
                {"id": "checkpoint", "kind": "checkpoint", "content": "checkpoint-1"},
                {"id": "security", "kind": "security", "content": context.ROLE_SEPARATION},
            ],
            limits=config.resolve_limits({"context_char_limit": 4000}),
        )
        self.state.record_final_context(
            self.final.context, envelope_digest=self.final.envelope["content_hash"],
        )

    def tearDown(self) -> None:
        self.state.close()
        self.temporary.cleanup()

    def observe(self) -> dict:
        self.runtime.record_dispatch_intent(self.final.envelope)
        receipt = {
            "invocation_id": "controller:7:now", "pid": 7, "creation_time": "now",
            "task_card_digest": self.card["content_hash"],
            "decision_id": self.decision["decision_id"],
            "plan_id": self.plan["plan_id"], "plan_digest": self.plan["content_hash"],
            "envelope_digest": self.final.envelope["content_hash"],
            "lane_id": self.lane_id, "run_id": self.run_id, "base_commit": "base-1",
            "route": "ordinary", "configuration_digest": self.decision["configuration_digest"],
            "context_id": self.final.context["context_id"],
            "context_digest": self.final.context["content_hash"],
        }
        self.runtime.record_observed_dispatch(self.final.envelope, receipt)
        return receipt

    def bundle(self, *, status: str = "PASS") -> dict:
        operation = self.state.get_operation(contracts.make_operation(
            kind="dispatch", envelope=self.final.envelope,
        )["operation_id"])
        native_operation = {key: operation[key] for key in (
            "operation_id", "decision_id", "envelope_digest", "run_id", "kind",
            "status", "observed_invocation", "created_at", "updated_at",
        )}
        result = {
            "schema": "result/v1", "lane_id": self.lane_id, "run_id": self.run_id,
            "outcome": status,
        }
        result["content_hash"] = contracts.content_hash(result)
        review = {
            "schema": "completion-review/v1", "lane_id": self.lane_id, "run_id": self.run_id,
            "review_outcome": status, "task_card_id": self.card["content_hash"],
            "task_card_hash": self.card["content_hash"], "result_id": self.run_id,
            "result_hash": result["content_hash"], "commit": "commit-1",
            "reviewed_at": "2026-09-26T00:00:00Z",
        }
        review["content_hash"] = contracts.content_hash(review)
        acceptance = {
            "schema": "orchestrator-acceptance/v1", "lane_id": self.lane_id, "run_id": self.run_id,
            "approval": "ACCEPTED" if status == "PASS" else "REJECTED", "accepted_by": "ROOT",
            "review_ref": review["content_hash"], "task_card_id": self.card["content_hash"],
            "task_card_hash": self.card["content_hash"], "result_id": self.run_id,
            "result_hash": result["content_hash"], "commit": "commit-1",
            "decided_at": "2026-09-26T00:00:00Z",
        }
        acceptance["content_hash"] = contracts.content_hash(acceptance)
        bundle = {
            "schema": "native-terminal-evidence/v1", "epoch_id": "epoch-1",
            "lane_id": self.lane_id, "run_id": self.run_id, "task_card": self.card,
            "accepted_plan": self.plan, "objective_id": "objective-1",
            "decision_id": self.decision["decision_id"], "decision": self.decision,
            "final_context": self.final.context,
            "dispatch": {
                "operation": native_operation,
                "operation_digest": contracts.sha256_hex(native_operation),
                "envelope_digest": self.final.envelope["content_hash"],
                "envelope": self.final.envelope,
                "observed_invocation": operation["observed_invocation"],
            },
            "configuration": self.configuration,
            "configuration_digest": contracts.sha256_hex(self.configuration),
            "result": result, "review": review, "acceptance": acceptance,
            "terminal_proof": None,
        }
        bundle["content_hash"] = contracts.content_hash(bundle)
        return bundle

    def test_exact_observed_native_chain_fixes_pass(self) -> None:
        self.observe()
        outcome = self.runtime.record_terminal_outcome(self.bundle())
        self.assertEqual("PASS", outcome["status"])
        self.assertEqual("ACCEPTED", outcome["acceptance_status"])
        self.assertFalse(outcome["exceptional_acceptance"])

    def test_quality_statuses_and_exceptional_acceptance_stay_distinct(self) -> None:
        for status in ("PASS", "FAIL", "BLOCKED"):
            with self.subTest(status=status):
                self.prepare_case(plan_id=f"plan-{status}", run_id=f"run-{status}",
                                  lane_id=f"lane-{status}")
                self.observe()
                evidence = self.bundle(status=status)
                if status != "PASS":
                    evidence["acceptance"]["approval"] = "ACCEPTED"
                    evidence["acceptance"]["force_accept_reason"] = "ROOT exception"
                    evidence["acceptance"]["content_hash"] = contracts.content_hash(evidence["acceptance"])
                    evidence["content_hash"] = contracts.content_hash(evidence)
                fixed = self.runtime.record_terminal_outcome(evidence)
                self.assertEqual(status, fixed["status"])
                self.assertEqual(status != "PASS", fixed["exceptional_acceptance"])
                self.assertEqual("ACCEPTED", fixed["acceptance_status"])
                self.assertNotIn("effect_status", fixed)
                self.assertNotIn("usage_status", fixed)

    def test_terminal_unknown_requires_no_result_proof_and_exception(self) -> None:
        self.observe()
        evidence = self.bundle()
        proof = {
            "schema": "controller-status/v1", "lane_id": "lane-1", "run_id": "run-1",
            "controller_state": "exited", "provider_state": {"state": "exited"},
            "result_state": "absent", "recorded_status": "provider_exited_no_result",
            "cleanup_proven": True,
        }
        evidence["result"] = None
        evidence["terminal_proof"] = proof
        evidence["review"].update({
            "review_outcome": "UNKNOWN", "result_id": None, "result_hash": None,
            "terminal_proof_digest": contracts.sha256_hex(proof),
        })
        evidence["review"]["content_hash"] = contracts.content_hash(evidence["review"])
        evidence["acceptance"].update({
            "result_id": None, "result_hash": None,
            "review_ref": evidence["review"]["content_hash"],
            "terminal_proof_digest": contracts.sha256_hex(proof),
            "force_accept_reason": "No result after provider exited",
        })
        evidence["acceptance"]["content_hash"] = contracts.content_hash(evidence["acceptance"])
        evidence["content_hash"] = contracts.content_hash(evidence)
        invalid = deepcopy(evidence)
        invalid["terminal_proof"] = None
        invalid["content_hash"] = contracts.content_hash(invalid)
        with self.assertRaises(ValueError):
            self.runtime.record_terminal_outcome(invalid)
        fixed = self.runtime.record_terminal_outcome(evidence)
        self.assertEqual("UNKNOWN", fixed["status"])
        self.assertTrue(fixed["exceptional_acceptance"])

    def test_missing_wrong_and_apc_evidence_never_fix(self) -> None:
        with self.assertRaises(ValueError):
            self.runtime.record_terminal_outcome(None)
        with self.assertRaises(ValueError):
            self.runtime.record_terminal_outcome({"schema": "result/v1", "outcome": "PASS"})
        self.runtime.record_dispatch_intent(self.final.envelope)
        pending = self.bundle()
        with self.assertRaises(ValueError):
            self.runtime.record_terminal_outcome(pending)
        self.runtime.record_observed_dispatch(self.final.envelope, self.native_receipt())
        valid = self.bundle()
        for field in ("review", "acceptance", "result", "dispatch"):
            with self.subTest(field=field):
                invalid = deepcopy(valid)
                invalid[field] = None
                invalid["content_hash"] = contracts.content_hash(invalid)
                with self.assertRaises(ValueError):
                    self.runtime.record_terminal_outcome(invalid)
        for field in ("run_id", "decision_id", "objective_id", "accepted_plan", "task_card"):
            with self.subTest(field=field):
                invalid = deepcopy(valid)
                invalid[field] = "wrong"
                invalid["content_hash"] = contracts.content_hash(invalid)
                with self.assertRaises(ValueError):
                    self.runtime.record_terminal_outcome(invalid)
        with self.assertRaises(store.StoreError):
            self.state.get_outcome(self.decision["decision_id"])

    def test_changed_native_observation_cannot_replace_durable_dispatch(self) -> None:
        self.observe()
        evidence = self.bundle()
        operation_id = evidence["dispatch"]["operation"]["operation_id"]
        durable = self.state.get_operation(operation_id)
        forged = deepcopy(evidence)
        observed = forged["dispatch"]["observed_invocation"]
        observed["pid"] = 8
        observed["invocation_id"] = "controller:8:now"
        forged["dispatch"]["operation"]["observed_invocation"] = observed
        forged["dispatch"]["operation_digest"] = contracts.sha256_hex(forged["dispatch"]["operation"])
        forged["content_hash"] = contracts.content_hash(forged)
        with self.assertRaises(store.OperationConflictError):
            self.runtime.record_terminal_outcome(forged)
        self.assertEqual(durable, self.state.get_operation(operation_id))
        with self.assertRaises(store.StoreError):
            self.state.get_outcome(self.decision["decision_id"])

    def native_receipt(self) -> dict:
        return {
            "invocation_id": "controller:7:now", "pid": 7, "creation_time": "now",
            "task_card_digest": self.card["content_hash"],
            "decision_id": self.decision["decision_id"],
            "plan_id": self.plan["plan_id"], "plan_digest": self.plan["content_hash"],
            "envelope_digest": self.final.envelope["content_hash"],
            "lane_id": self.lane_id, "run_id": self.run_id, "base_commit": "base-1",
            "route": "ordinary", "configuration_digest": self.decision["configuration_digest"],
            "context_id": self.final.context["context_id"],
            "context_digest": self.final.context["content_hash"],
        }

    def test_crash_before_after_and_conflict_isolation(self) -> None:
        self.observe()
        evidence = self.bundle()
        self.state.close()
        self.state = store.MemoryStore(self.root / "state.sqlite3")
        self.state.initialize()
        self.runtime = runtime.MemoryRuntime(self.state)
        with self.assertRaises(store.StoreError):
            self.state.get_outcome(self.decision["decision_id"])
        fixed = self.runtime.record_terminal_outcome(evidence)
        self.state.close()
        self.state = store.MemoryStore(self.root / "state.sqlite3")
        self.state.initialize()
        self.runtime = runtime.MemoryRuntime(self.state)
        self.assertEqual(fixed, self.runtime.record_terminal_outcome(evidence))
        second = deepcopy(evidence)
        second["review"]["review_outcome"] = "FAIL"
        second["review"]["content_hash"] = contracts.content_hash(second["review"])
        second["acceptance"]["review_ref"] = second["review"]["content_hash"]
        second["acceptance"]["approval"] = "REJECTED"
        second["acceptance"]["content_hash"] = contracts.content_hash(second["acceptance"])
        second["content_hash"] = contracts.content_hash(second)
        original_operation = self.state.get_operation(evidence["dispatch"]["operation"]["operation_id"])
        with self.assertRaises(store.OutcomeConflictError):
            self.runtime.record_terminal_outcome(second)
        self.assertEqual(fixed, self.state.get_outcome(fixed["decision_id"]))
        self.assertEqual(original_operation, self.state.get_operation(original_operation["operation_id"]))
        self.prepare_case(plan_id="unrelated", run_id="run-2", lane_id="lane-2")
        self.observe()
        other = self.runtime.record_terminal_outcome(self.bundle(status="BLOCKED"))
        self.assertEqual("BLOCKED", other["status"])
        self.assertEqual(fixed, self.state.get_outcome(fixed["decision_id"]))

    def test_explicit_plan_supersession_retains_predecessor(self) -> None:
        self.observe()
        old = self.runtime.record_terminal_outcome(self.bundle())
        self.prepare_case(plan_id="corrected-plan", run_id="run-2", lane_id="lane-2",
                          supersedes="plan-1")
        self.observe()
        corrected = self.runtime.record_terminal_outcome(
            self.bundle(status="FAIL"), supersedes_outcome_id=old["outcome_id"],
        )
        self.assertEqual(old["outcome_id"], corrected["supersedes_outcome_id"])
        self.assertEqual("PASS", self.state.get_outcome(old["decision_id"])["status"])

    def test_supersession_requires_explicit_accepted_plan_link(self) -> None:
        self.observe()
        old = self.runtime.record_terminal_outcome(self.bundle())
        self.prepare_case(plan_id="unlinked-plan", run_id="run-2", lane_id="lane-2")
        self.observe()
        with self.assertRaises(store.OutcomeConflictError):
            self.runtime.record_terminal_outcome(
                self.bundle(), supersedes_outcome_id=old["outcome_id"],
            )
        with self.assertRaises(store.StoreError):
            self.state.get_outcome(self.decision["decision_id"])

    def test_generic_caller_cannot_bypass_finalized_guard(self) -> None:
        self.observe()
        with self.assertRaisesRegex(store.OperationConflictError, "native terminal evidence"):
            self.runtime.record_outcome(
                decision_id=self.decision["decision_id"], plan_id=self.plan["plan_id"],
                plan_digest=self.plan["content_hash"], status="PASS",
                evidence_digest="caller-supplied", linked_run_id=self.run_id,
            )


if __name__ == "__main__":
    unittest.main()
