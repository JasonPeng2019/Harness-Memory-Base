"""BEHAVIOR-03: only an accepted plan dispatches once with exact safe context."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from memory_harness import (
    config,
    context,
    contracts,
    preparation,
    privacy,
    runtime,
    store,
)


class FinalContextDispatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.worktree = self.root / "worktree"
        self.worktree.mkdir()
        self.limits = config.resolve_limits(
            {
                "context_char_limit": 4000,
                "default_deadline_seconds": 300.0,
                "execution_reserve_seconds": 60.0,
            }
        )
        self.memory_store = store.MemoryStore(self.root / "memory-state.sqlite3")
        self.memory_store.initialize()
        self.accepted = contracts.make_plan(
            plan_id="accepted-plan",
            objective_id="objective-1",
            route="ordinary",
            state="accepted",
            accepted_by="ROOT",
            content={"steps": ["inspect", "verify"]},
        )
        self.card = contracts.make_task_card(
            task="Repair the regression",
            base_commit="base-1",
            memory_handoff=contracts.make_memory_handoff(
                objective_id="objective-1", route="ordinary", plan=self.accepted
            ),
        )
        # One logical decision owns the finalization; the envelope contract
        # recomputes this exact identity, so the test derives it identically.
        self.configuration = {"strategy": "standard"}
        self.decision = contracts.make_decision(
            self.card, self.accepted, strategy="standard", configuration=self.configuration
        )
        self.decision_id = self.decision["decision_id"]
        self.memory_store.record_decision(self.decision)

    def tearDown(self) -> None:
        self.memory_store.close()
        self.temporary.cleanup()

    def _finalize(self, **overrides):
        arguments = {
            "task_card": self.card,
            "plan": self.accepted,
            "decision_id": self.decision_id,
            "lane_id": "lane-1",
            "run_id": "run-1",
            "worktree_path": str(self.worktree),
            "base_commit": "base-1",
            "strategy": "standard",
            "configuration": dict(self.configuration),
            "mandatory_content": [
                {"id": "task", "kind": "task", "content": self.card["task"]},
                {"id": "accepted-plan", "kind": "accepted-plan", "content": self.accepted["content"]},
            ],
            "limits": self.limits,
        }
        arguments.update(overrides)
        return context.finalize_context(**arguments)

    # -- exact identity -----------------------------------------------------

    def test_proposed_plan_cannot_finalize(self) -> None:
        proposed = contracts.make_plan(
            plan_id="proposed-plan",
            objective_id="objective-1",
            route="ordinary",
            state="proposed",
            content={"steps": ["draft"]},
        )
        with self.assertRaises(contracts.ContractError):
            self._finalize(plan=proposed)

    def test_wrong_base_or_task_binding_fails_before_dispatch(self) -> None:
        with self.assertRaisesRegex(context.ContextError, "base"):
            self._finalize(base_commit="base-2")
        other_card = contracts.make_task_card(
            task="Repair the regression",
            base_commit="base-2",
            memory_handoff=self.card["memory_handoff"],
        )
        with self.assertRaisesRegex(context.ContextError, "base"):
            self._finalize(task_card=other_card)

    def test_finalized_context_binds_the_actual_target(self) -> None:
        finalized = self._finalize(
            optional_items=[
                {
                    "id": "history-1",
                    "kind": "historical_evidence",
                    "origin": "everos",
                    "revision_id": "r1",
                    "content": {"summary": "prior regression"},
                }
            ]
        )
        self.assertEqual("run-1", finalized.envelope["run_id"])
        self.assertEqual("objective-1", finalized.context["objective_id"])
        self.assertEqual(self.accepted["content_hash"], finalized.context["plan_digest"])
        self.assertEqual("base-1", finalized.context["base_commit"])
        self.assertEqual(
            context.ROLE_SEPARATION["control_plane"],
            finalized.context["role_separation"]["control_plane"],
        )
        self.assertFalse(finalized.context["role_separation"]["may_approve"])
        self.assertFalse(finalized.context["role_separation"]["may_execute_parent"])
        context.validate_final_context(
            finalized.context,
            envelope=finalized.envelope,
            task_card=self.card,
            plan=self.accepted,
            lane_id="lane-1",
            run_id="run-1",
            base_commit="base-1",
            worktree_path=str(self.worktree),
        )

    def test_copied_context_fails_against_the_actual_task_card(self) -> None:
        finalized = self._finalize()
        changed = contracts.make_task_card(
            task="Repair a different regression",
            base_commit="base-1",
            memory_handoff=contracts.make_memory_handoff(
                objective_id="objective-1", route="ordinary", plan=self.accepted
            ),
        )
        self.assertNotEqual(changed["content_hash"], self.card["content_hash"])
        with self.assertRaises(context.ContextError):
            context.validate_final_context(
                finalized.context,
                envelope=finalized.envelope,
                task_card=changed,
                plan=self.accepted,
                lane_id="lane-1",
                run_id="run-1",
                base_commit="base-1",
                worktree_path=str(self.worktree),
            )
        with self.assertRaises(context.ContextError):
            context.validate_final_context(
                finalized.context,
                envelope=finalized.envelope,
                task_card=self.card,
                plan=self.accepted,
                lane_id="lane-1",
                run_id="run-2",
                base_commit="base-1",
                worktree_path=str(self.worktree),
            )

    # -- bounded content ----------------------------------------------------

    def test_oversized_mandatory_state_blocks_instead_of_truncating(self) -> None:
        with self.assertRaises(context.MandatoryOverflowError):
            self._finalize(
                mandatory_content=[
                    {"id": "task", "kind": "task", "content": "x" * 8000},
                ]
            )

    def test_optional_overflow_omits_whole_items(self) -> None:
        finalized = self._finalize(
            optional_items=[
                {"id": "small", "kind": "memory", "content": "tiny"},
                {"id": "huge", "kind": "memory", "content": "y" * 5000},
            ]
        )
        packed = [item["id"] for item in finalized.envelope["optional_content"]]
        self.assertEqual(["small"], packed)
        self.assertIn("huge", finalized.envelope["delivery"]["omitted"])
        self.assertEqual(
            self.card["task"], finalized.envelope["mandatory_content"][0]["content"]
        )

    def test_plan_affecting_stale_guidance_returns_to_root(self) -> None:
        with self.assertRaises(context.PlanAffectingFreshnessError):
            self._finalize(
                optional_items=[
                    {
                        "id": "procedure-1",
                        "kind": "procedure",
                        "plan_affecting": True,
                        "content": {"steps": ["approved guidance"]},
                    }
                ],
                freshness_check=lambda item: False,
            )

    def test_stale_non_plan_affecting_item_is_omitted_with_a_new_trace(self) -> None:
        finalized = self._finalize(
            optional_items=[
                {"id": "case-1", "kind": "historical_evidence", "content": {"a": 1}}
            ],
            freshness_check=lambda item: False,
        )
        self.assertEqual([], finalized.envelope["optional_content"])
        self.assertIn("case-1", finalized.envelope["delivery"]["omitted"])

    def test_prohibited_secret_is_omitted_from_optional_content(self) -> None:
        policy = privacy.PrivacyPolicy(known_secrets=("super-secret-value",))
        finalized = self._finalize(
            optional_items=[
                {
                    "id": "leaky",
                    "kind": "memory",
                    "content": {"note": "token super-secret-value"},
                }
            ],
            privacy_policy=policy,
        )
        self.assertEqual([], finalized.envelope["optional_content"])
        self.assertIn("leaky", finalized.envelope["delivery"]["omitted"])
        self.assertNotIn(
            "super-secret-value", contracts.canonical_json(finalized.envelope).decode("utf-8")
        )

    def test_prohibited_mandatory_control_credential_blocks(self) -> None:
        policy = privacy.PrivacyPolicy(known_secrets=("super-secret-value",))
        with self.assertRaises(Exception):
            self._finalize(
                mandatory_content=[
                    {"id": "task", "kind": "task", "content": "token super-secret-value"}
                ],
                privacy_policy=policy,
            )

    # -- durable, at-most-once dispatch ------------------------------------

    def test_dispatch_validates_target_and_launches_once(self) -> None:
        finalized = self._finalize()
        memory_runtime = runtime.MemoryRuntime(self.memory_store)
        calls: list[object] = []

        def launcher(envelope):
            calls.append(envelope)
            return {"invocation_id": "controller-1", "pid": 321, "creation_time": "now"}

        delivered = memory_runtime.dispatch_finalized(
            envelope=finalized.envelope,
            context=finalized.context,
            task_card=self.card,
            plan=self.accepted,
            lane_id="lane-1",
            run_id="run-1",
            worktree_path=str(self.worktree),
            base_commit="base-1",
            launcher=launcher,
        )
        self.assertEqual("delivered", delivered["status"])
        self.assertEqual(1, len(calls))
        replay = memory_runtime.dispatch_finalized(
            envelope=finalized.envelope,
            context=finalized.context,
            task_card=self.card,
            plan=self.accepted,
            lane_id="lane-1",
            run_id="run-1",
            worktree_path=str(self.worktree),
            base_commit="base-1",
            launcher=lambda envelope: self.fail("the same intent launched twice"),
        )
        self.assertEqual(delivered["operation_id"], replay["operation_id"])

    def test_reconciled_dispatch_is_visible_and_blocks_duplicates(self) -> None:
        finalized = self._finalize()
        memory_runtime = runtime.MemoryRuntime(self.memory_store)
        calls: list[object] = []

        def lost_ack(envelope):
            calls.append(envelope)
            return None

        with self.assertRaises(runtime.DispatchAmbiguityError):
            memory_runtime.dispatch_finalized(
                envelope=finalized.envelope,
                context=finalized.context,
                task_card=self.card,
                plan=self.accepted,
                lane_id="lane-1",
                run_id="run-1",
                worktree_path=str(self.worktree),
                base_commit="base-1",
                launcher=lost_ack,
            )
        with self.assertRaises(runtime.DispatchAmbiguityError):
            memory_runtime.dispatch_finalized(
                envelope=finalized.envelope,
                context=finalized.context,
                task_card=self.card,
                plan=self.accepted,
                lane_id="lane-1",
                run_id="run-1",
                worktree_path=str(self.worktree),
                base_commit="base-1",
                launcher=lost_ack,
            )
        self.assertEqual(1, len(calls))
        reconciled = memory_runtime.reconcile_ambiguous_dispatch(
            finalized.envelope,
            {
                "invocation_id": "controller:1:2026-01-01T00:00:00Z",
                "pid": 1,
                "creation_time": "2026-01-01T00:00:00Z",
            },
        )
        self.assertEqual("delivered", reconciled["status"])
        self.assertEqual(1, len(calls))

    def test_dispatch_refuses_a_context_that_does_not_match_the_target(self) -> None:
        finalized = self._finalize()
        memory_runtime = runtime.MemoryRuntime(self.memory_store)
        with self.assertRaisesRegex(runtime.RuntimeError, "does not match"):
            memory_runtime.dispatch_finalized(
                envelope=finalized.envelope,
                context=finalized.context,
                task_card=self.card,
                plan=self.accepted,
                lane_id="lane-1",
                run_id="run-2",
                worktree_path=str(self.worktree),
                base_commit="base-1",
                launcher=lambda envelope: self.fail("invalid target must not launch"),
            )

    # -- all-off and legacy cards stay ordinary ----------------------------

    def test_all_off_finalization_writes_no_envelope(self) -> None:
        service = preparation.PreparationService(
            store=self.memory_store, config=config.all_off(), limits=self.limits
        )
        outcome = service.prepare(
            task_card=self.card,
            plan=self.accepted,
            objective_id="objective-1",
            route="ordinary",
            lane_id="lane-1",
            run_id="run-1",
            worktree_path=str(self.worktree),
            base_commit="base-1",
            finalize=True,
        )
        self.assertEqual("inherited", outcome.mode)
        self.assertIsNone(outcome.envelope)
        self.assertFalse(
            (self.worktree / ".agent-workspace" / "memory-dispatch.json").exists()
        )


if __name__ == "__main__":
    unittest.main()
