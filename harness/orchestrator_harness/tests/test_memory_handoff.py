from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from orchestrator_harness import memory_handoff
from memory_harness import contracts, runtime


def _task_card() -> tuple[dict, dict]:
    plan = contracts.make_plan(
        plan_id="plan-1",
        objective_id="objective-1",
        route="ordinary",
        state="accepted",
        content={"steps": ["inspect", "implement", "verify"]},
        accepted_by="ROOT",
    )
    handoff = contracts.make_memory_handoff(
        objective_id="objective-1",
        route="ordinary",
        plan=plan,
    )
    card = contracts.make_task_card(
        task="Fix the regression and verify it",
        base_commit="base-1",
        branch="lane/memory",
        memory_handoff=handoff,
    )
    return card, plan


class MemoryHandoffSeamTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.worktree = self.root / "worktree"
        self.worktree.mkdir()
        self.card, self.plan = _task_card()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_legacy_task_card_has_no_memory_envelope(self) -> None:
        legacy = contracts.make_task_card(
            task="ordinary task",
            base_commit="base-1",
        )
        self.assertIsNone(memory_handoff.validate_task_card(legacy))
        self.assertIsNone(
            memory_handoff.prepare_bootstrap_envelope(
                task_card=legacy,
                lane_id="lane-legacy",
                run_id="run-legacy",
                worktree_path=self.worktree,
                base_commit="base-1",
            )
        )
        self.assertIsNone(memory_handoff.load_envelope(self.worktree))

    def test_bootstrap_and_resume_use_equivalent_envelope_validation(self) -> None:
        bootstrap_envelope = memory_handoff.prepare_bootstrap_envelope(
            task_card=self.card,
            lane_id="lane-1",
            run_id="run-1",
            worktree_path=self.worktree,
            base_commit="base-1",
        )
        self.assertIsNotNone(bootstrap_envelope)
        self.assertEqual("run-1", bootstrap_envelope["run_id"])
        memory_handoff.validate_envelope_for_launch(
            envelope=bootstrap_envelope,
            task_card=self.card,
            lane_id="lane-1",
            run_id="run-1",
            worktree_path=self.worktree,
            base_commit="base-1",
        )

        resume_envelope = memory_handoff.prepare_resume_envelope(
            task_card=self.card,
            lane_id="lane-1",
            run_id="run-2",
            worktree_path=self.worktree,
            base_commit="base-1",
        )
        self.assertEqual("run-2", resume_envelope["run_id"])
        memory_handoff.validate_envelope_for_launch(
            envelope=resume_envelope,
            task_card=self.card,
            lane_id="lane-1",
            run_id="run-2",
            worktree_path=self.worktree,
            base_commit="base-1",
        )
        self.assertEqual(
            bootstrap_envelope["decision_id"], resume_envelope["decision_id"]
        )

    def test_candidate_plan_continues_without_parent_envelope(self) -> None:
        candidate = contracts.make_plan(
            plan_id="candidate-plan",
            objective_id="objective-1",
            route="ordinary",
            state="candidate",
            content={"steps": ["draft"]},
        )
        candidate_card = contracts.make_task_card(
            task="Fix the regression",
            base_commit="base-1",
            memory_handoff=contracts.make_memory_handoff(
                objective_id="objective-1",
                route="ordinary",
                plan=candidate,
            ),
        )
        self.assertIsNone(
            memory_handoff.prepare_bootstrap_envelope(
                task_card=candidate_card,
                lane_id="lane-candidate",
                run_id="run-candidate",
                worktree_path=self.worktree,
                base_commit="base-1",
            )
        )
        self.assertIsNone(memory_handoff.load_envelope(self.worktree))

    def test_changed_task_or_base_is_rejected_at_launch(self) -> None:
        envelope = memory_handoff.prepare_bootstrap_envelope(
            task_card=self.card,
            lane_id="lane-1",
            run_id="run-1",
            worktree_path=self.worktree,
            base_commit="base-1",
        )
        changed_card = contracts.make_task_card(
            task="different task",
            base_commit="base-1",
            memory_handoff=self.card["memory_handoff"],
        )
        with self.assertRaisesRegex(memory_handoff.MemoryHandoffError, "task card"):
            memory_handoff.validate_envelope_for_launch(
                envelope=envelope,
                task_card=changed_card,
                lane_id="lane-1",
                run_id="run-1",
                worktree_path=self.worktree,
                base_commit="base-1",
            )
        with self.assertRaisesRegex(memory_handoff.MemoryHandoffError, "base"):
            memory_handoff.validate_envelope_for_launch(
                envelope=envelope,
                task_card=self.card,
                lane_id="lane-1",
                run_id="run-1",
                worktree_path=self.worktree,
                base_commit="base-2",
            )

    def test_dispatch_is_idempotent_and_lost_ack_blocks_duplicate(self) -> None:
        envelope = memory_handoff.prepare_bootstrap_envelope(
            task_card=self.card,
            lane_id="lane-1",
            run_id="run-1",
            worktree_path=self.worktree,
            base_commit="base-1",
        )
        observed = {"invocation_id": "controller-1", "pid": 123, "creation_time": "now"}
        delivered = memory_handoff.dispatch(
            worktree_path=self.worktree,
            envelope=envelope,
            launcher=lambda record: observed,
        )
        self.assertEqual("delivered", delivered["status"])
        replay = memory_handoff.dispatch(
            worktree_path=self.worktree,
            envelope=envelope,
            launcher=lambda record: self.fail("duplicate launcher call"),
        )
        self.assertEqual(delivered["operation_id"], replay["operation_id"])

        ambiguous_worktree = self.root / "ambiguous"
        ambiguous_worktree.mkdir()
        ambiguous_envelope = memory_handoff.prepare_bootstrap_envelope(
            task_card=self.card,
            lane_id="lane-2",
            run_id="run-2",
            worktree_path=ambiguous_worktree,
            base_commit="base-1",
        )
        calls: list[object] = []

        def launcher(record: dict) -> None:
            calls.append(record)
            return None

        with self.assertRaisesRegex(runtime.DispatchAmbiguityError, "ambiguous"):
            memory_handoff.dispatch(
                worktree_path=ambiguous_worktree,
                envelope=ambiguous_envelope,
                launcher=launcher,
            )
        with self.assertRaisesRegex(runtime.DispatchAmbiguityError, "ambiguous"):
            memory_handoff.dispatch(
                worktree_path=ambiguous_worktree,
                envelope=ambiguous_envelope,
                launcher=launcher,
            )
        self.assertEqual(1, len(calls))

    def test_optional_omission_preserves_plan_and_updates_delivery_trace(self) -> None:
        card_with_optional = contracts.make_task_card(
            task="Fix the regression",
            base_commit="base-1",
            memory_handoff=contracts.make_memory_handoff(
                objective_id="objective-1",
                route="ordinary",
                plan=self.plan,
            ),
        )
        # Add one optional item by preparing through the runtime contract.
        from memory_harness import store as memory_store_module

        store_path, _ = memory_handoff.memory_paths(self.worktree)
        memory_store = memory_store_module.MemoryStore(store_path)
        memory_store.initialize()
        try:
            memory_runtime = runtime.MemoryRuntime(memory_store)
            prepared = memory_runtime.prepare(
                plan=self.plan,
                task_card=card_with_optional,
                lane_id="lane-1",
                run_id="run-1",
                worktree_path=self.worktree,
                base_commit="base-1",
                optional_content=[
                    {"id": "history", "kind": "experience", "content": "prior failure"}
                ],
            )
        finally:
            memory_store.close()
        envelope = prepared.envelope
        revised = memory_handoff.omit_optional_content(
            worktree_path=self.worktree,
            envelope=envelope,
            item_id="history",
        )
        self.assertEqual([], revised["optional_content"])
        self.assertIn("history", revised["delivery"]["omitted"])
        self.assertEqual(envelope["plan_id"], revised["plan_id"])
        self.assertEqual(envelope["plan_digest"], revised["plan_digest"])


if __name__ == "__main__":
    unittest.main()
