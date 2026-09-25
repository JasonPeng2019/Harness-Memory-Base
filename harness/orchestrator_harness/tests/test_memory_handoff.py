from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from orchestrator_harness import bootstrap, memory_handoff
from memory_harness import contracts, runtime, store


def _task_card(configuration: dict | None = None) -> tuple[dict, dict]:
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
        configuration=configuration,
    )
    card = contracts.make_task_card(
        task="Fix the regression and verify it",
        base_commit="base-1",
        branch="lane/memory",
        memory_handoff=handoff,
    )
    return card, plan


def _finalized_lane1_fixture(card: dict, plan: dict, worktree: Path) -> SimpleNamespace:
    """Deterministic STEP-06-1 record shape until the lane-1 join is available."""

    configuration = {"strategy": "standard"}
    decision_id = contracts.make_decision(card, plan)["decision_id"]
    mandatory = [
        {"id": "task", "kind": "task", "content": card["task"]},
        {"id": "accepted-plan", "kind": "accepted-plan", "content": plan["content"]},
        {"id": "base", "kind": "repository-base", "content": card["base_commit"]},
        {"id": "route", "kind": "route", "content": plan["route"]},
        {"id": "security", "kind": "trusted-security", "content": {"worker_environment": "scrubbed"}},
    ]
    optional = [
        {"id": "case-1", "kind": "experience", "origin": "reviewed", "content": "prior failure"}
    ]
    omitted = ["case-2"]
    envelope = contracts.make_envelope(
        task_card=card,
        plan=plan,
        decision_id=decision_id,
        lane_id="lane-1",
        run_id="run-1",
        worktree_path=str(worktree),
        base_commit=card["base_commit"],
        mandatory_content=mandatory,
        optional_content=optional,
        omitted_content=omitted,
        configuration=configuration,
    )
    context = contracts.make_finalized_context(
        lane_id="lane-1",
        run_id="run-1",
        decision_id=decision_id,
        task_card_digest=card["content_hash"],
        objective_id=plan["objective_id"],
        route=plan["route"],
        plan_id=plan["plan_id"],
        plan_digest=plan["content_hash"],
        base_commit=card["base_commit"],
        worktree_path=str(worktree),
        strategy="standard",
        configuration=configuration,
        mandatory_items=mandatory,
        optional_items=optional,
        omitted=omitted,
        role_separation={"experience": "evidence", "procedure": "approved-guidance"},
        freshness={"state": "current"},
        context_limit=10000,
    )
    context.update(
        plan_revision=plan["revision"],
        plan_integrity=plan["content_hash"],
        execution_role="worker",
        invocation_target="orchestrator_harness.controller",
        recipient="worker:lane-1",
        recipient_authorization={
            "recipient": "worker:lane-1",
            "authorized": True,
            "sanitized": True,
        },
        rendered_context={"mandatory": mandatory, "optional": optional},
        delivery_trace={
            "selected": ["case-1", "case-2"],
            "packed": ["case-1"],
            "omitted": omitted,
            "delivered": ["case-1"],
        },
    )
    context["integrity"] = contracts.sha256_hex(
        {key: value for key, value in context.items() if key not in {"integrity", "content_hash"}}
    )
    context["context_id"] = contracts.sha256_hex(
        {"decision_id": decision_id, "integrity": context["integrity"]}
    )
    context["content_hash"] = contracts.content_hash(context)
    return SimpleNamespace(envelope=envelope, context=context)


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

    def test_all_off_bypasses_memory_and_learned_mode_fails_before_state(self) -> None:
        all_off_card, _ = _task_card({"all_features": False})
        self.assertIsNone(
            memory_handoff.prepare_bootstrap_envelope(
                task_card=all_off_card,
                lane_id="lane-off",
                run_id="run-off",
                worktree_path=self.worktree,
                base_commit="base-1",
            )
        )
        store_path, envelope_path = memory_handoff.memory_paths(self.worktree)
        self.assertFalse(store_path.exists())
        self.assertFalse(envelope_path.exists())

        learned_card, _ = _task_card({"strategy": "learned"})
        with self.assertRaisesRegex(memory_handoff.MemoryHandoffError, "deferred"):
            memory_handoff.prepare_bootstrap_envelope(
                task_card=learned_card,
                lane_id="lane-learned",
                run_id="run-learned",
                worktree_path=self.worktree,
                base_commit="base-1",
            )
        self.assertFalse(store_path.exists())

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

    def test_bootstrap_rejects_a_final_context_for_another_base_before_writing(self) -> None:
        decision_id = contracts.make_decision(self.card, self.plan)["decision_id"]
        mandatory = [
            {"id": "task", "kind": "task", "content": self.card["task"]},
            {"id": "accepted-plan", "kind": "accepted-plan", "content": self.plan["content"]},
        ]
        configuration = {"strategy": "standard"}
        envelope = contracts.make_envelope(
            task_card=self.card,
            plan=self.plan,
            decision_id=decision_id,
            lane_id="lane-1",
            run_id="run-1",
            worktree_path=str(self.worktree),
            base_commit="base-1",
            mandatory_content=mandatory,
            configuration=configuration,
        )
        context = contracts.make_finalized_context(
            lane_id="lane-1",
            run_id="run-1",
            decision_id=decision_id,
            task_card_digest=self.card["content_hash"],
            objective_id=self.plan["objective_id"],
            route=self.plan["route"],
            plan_id=self.plan["plan_id"],
            plan_digest=self.plan["content_hash"],
            base_commit="base-2",
            worktree_path=str(self.worktree),
            strategy="standard",
            configuration=configuration,
            mandatory_items=mandatory,
            optional_items=[],
            omitted=[],
            role_separation={},
            freshness={},
            context_limit=10000,
        )
        with patch.object(
            memory_handoff,
            "_prepare_memory_outcome",
            return_value=SimpleNamespace(envelope=envelope, context=context),
        ):
            with self.assertRaisesRegex(memory_handoff.MemoryHandoffError, "finalized context"):
                memory_handoff.prepare_bootstrap_envelope(
                    task_card=self.card,
                    lane_id="lane-1",
                    run_id="run-1",
                    worktree_path=self.worktree,
                    base_commit="base-1",
                )
        self.assertIsNone(memory_handoff.load_envelope(self.worktree))

    def test_absent_and_proposed_plans_remain_in_root_planning_or_review(self) -> None:
        for state in ("absent", "proposed", "candidate"):
            with self.subTest(state=state):
                plan = None if state == "absent" else contracts.make_plan(
                    plan_id=f"{state}-plan",
                    objective_id="objective-1",
                    route="ordinary",
                    state=state,
                    content={"steps": ["draft"]},
                )
                card = contracts.make_task_card(
                    task="Plan and review the regression",
                    base_commit="base-1",
                    memory_handoff=contracts.make_memory_handoff(
                        objective_id="objective-1", route="ordinary", plan=plan
                    ),
                )
                worktree = self.root / state
                worktree.mkdir()
                prepared = memory_handoff.prepare_lane_memory(
                    task_card=card,
                    lane_id=f"lane-{state}",
                    run_id=f"run-{state}",
                    worktree_path=worktree,
                    base_commit="base-1",
                )
                self.assertEqual(
                    "absent" if state == "absent" else "candidate_review",
                    prepared.state,
                )
                self.assertTrue(prepared.pending_plan)
                self.assertIsNone(prepared.envelope)
                self.assertIsNone(memory_handoff.load_envelope(worktree))

    def test_accepted_fixture_carries_exact_finalized_meaning_without_launch_claim(self) -> None:
        outcome = _finalized_lane1_fixture(self.card, self.plan, self.worktree)
        with patch.object(memory_handoff, "_prepare_memory_outcome", return_value=outcome):
            envelope = memory_handoff.prepare_bootstrap_envelope(
                task_card=self.card,
                lane_id="lane-1",
                run_id="run-1",
                worktree_path=self.worktree,
                base_commit="base-1",
            )
        self.assertEqual(self.card["task"], envelope["task"])
        self.assertEqual(self.plan["revision"], envelope["plan_revision"])
        self.assertEqual(self.plan["content_hash"], envelope["plan_integrity"])
        self.assertEqual(outcome.context["integrity"], envelope["context_integrity"])
        self.assertEqual(outcome.context["context_id"], envelope["context_id"])
        for field in ("execution_role", "invocation_target", "recipient", "recipient_authorization", "rendered_context", "delivery_trace"):
            self.assertEqual(outcome.context[field], envelope[field], field)
        self.assertEqual(["case-1", "case-2"], envelope["delivery_trace"]["selected"])
        self.assertEqual(["case-1"], envelope["delivery_trace"]["packed"])
        self.assertEqual(["case-2"], envelope["delivery_trace"]["omitted"])
        self.assertEqual(["case-1"], envelope["delivery_trace"]["delivered"])
        self.assertNotIn("observed_invocation", envelope)
        self.assertEqual(envelope, memory_handoff.load_envelope(self.worktree))
        prompt_context = "\n".join(bootstrap._render_memory_context(self.worktree))
        self.assertIn("base-1", prompt_context)
        self.assertIn("ordinary", prompt_context)
        self.assertIn("scrubbed", prompt_context)
        self.assertIn("prior failure", prompt_context)

    def test_resume_rejects_an_unreadable_prior_handoff(self) -> None:
        memory_handoff.prepare_bootstrap_envelope(
            task_card=self.card,
            lane_id="lane-1",
            run_id="run-1",
            worktree_path=self.worktree,
            base_commit="base-1",
        )
        _, envelope_path = memory_handoff.memory_paths(self.worktree)
        envelope_path.write_text("{unreadable", encoding="utf-8")
        with self.assertRaisesRegex(memory_handoff.MemoryHandoffError, "cannot read"):
            memory_handoff.prepare_resume_envelope(
                task_card=self.card,
                lane_id="lane-1",
                run_id="run-2",
                worktree_path=self.worktree,
                base_commit="base-1",
            )
        self.assertEqual("{unreadable", envelope_path.read_text(encoding="utf-8"))

    def test_enriched_handoff_rejects_revocation_and_wrong_execution_recipient(self) -> None:
        for change, expected in (
            (lambda context: context["freshness"].update(state="revoked"), "stale or revoked"),
            (lambda context: context.update(execution_role="reviewer"), "execution role"),
            (lambda context: context.update(invocation_target="another.controller"), "invocation target"),
            (lambda context: context.update(recipient="worker:another-lane"), "recipient does not match"),
            (
                lambda context: context["recipient_authorization"].update(sanitized=False),
                "sanitized recipient authorization",
            ),
        ):
            with self.subTest(expected=expected):
                outcome = _finalized_lane1_fixture(self.card, self.plan, self.worktree)
                change(outcome.context)
                outcome.context["content_hash"] = contracts.content_hash(outcome.context)
                with patch.object(memory_handoff, "_prepare_memory_outcome", return_value=outcome):
                    with self.assertRaisesRegex(memory_handoff.MemoryHandoffError, expected):
                        memory_handoff.prepare_bootstrap_envelope(
                            task_card=self.card,
                            lane_id="lane-1",
                            run_id="run-1",
                            worktree_path=self.worktree,
                            base_commit="base-1",
                        )
                self.assertIsNone(memory_handoff.load_envelope(self.worktree))

    def test_enriched_handoff_preserves_mandatory_security(self) -> None:
        outcome = _finalized_lane1_fixture(self.card, self.plan, self.worktree)
        mandatory = [
            item for item in outcome.envelope["mandatory_content"] if item["id"] != "security"
        ]
        outcome.envelope["mandatory_content"] = mandatory
        outcome.envelope["mandatory_digest"] = contracts.sha256_hex(mandatory)
        outcome.envelope["delivery"]["mandatory"] = [item["id"] for item in mandatory]
        outcome.envelope["content_hash"] = contracts.content_hash(outcome.envelope)
        outcome.context["mandatory_items"] = [item["id"] for item in mandatory]
        outcome.context["mandatory_digest"] = contracts.sha256_hex(mandatory)
        outcome.context["rendered_context"]["mandatory"] = mandatory
        outcome.context["content_hash"] = contracts.content_hash(outcome.context)
        with patch.object(memory_handoff, "_prepare_memory_outcome", return_value=outcome):
            with self.assertRaisesRegex(memory_handoff.MemoryHandoffError, "mandatory security"):
                memory_handoff.prepare_bootstrap_envelope(
                    task_card=self.card,
                    lane_id="lane-1",
                    run_id="run-1",
                    worktree_path=self.worktree,
                    base_commit="base-1",
                )
        self.assertIsNone(memory_handoff.load_envelope(self.worktree))

    def test_enriched_handoff_rejects_duplicate_mandatory_identity(self) -> None:
        outcome = _finalized_lane1_fixture(self.card, self.plan, self.worktree)
        mandatory = [
            {"id": "task", "kind": "task", "content": "untrusted duplicate"},
            *outcome.envelope["mandatory_content"],
        ]
        outcome.envelope["mandatory_content"] = mandatory
        outcome.envelope["mandatory_digest"] = contracts.sha256_hex(mandatory)
        outcome.envelope["delivery"]["mandatory"] = [item["id"] for item in mandatory]
        outcome.envelope["content_hash"] = contracts.content_hash(outcome.envelope)
        outcome.context["mandatory_items"] = [item["id"] for item in mandatory]
        outcome.context["mandatory_digest"] = contracts.sha256_hex(mandatory)
        outcome.context["rendered_context"]["mandatory"] = mandatory
        outcome.context["content_hash"] = contracts.content_hash(outcome.context)
        with patch.object(memory_handoff, "_prepare_memory_outcome", return_value=outcome):
            with self.assertRaisesRegex(memory_handoff.MemoryHandoffError, "duplicate"):
                memory_handoff.prepare_bootstrap_envelope(
                    task_card=self.card,
                    lane_id="lane-1",
                    run_id="run-1",
                    worktree_path=self.worktree,
                    base_commit="base-1",
                )
        self.assertIsNone(memory_handoff.load_envelope(self.worktree))

    def test_mandatory_overflow_remains_an_explicit_failure(self) -> None:
        from memory_harness.context import MandatoryOverflowError

        with patch.object(
            memory_handoff,
            "_prepare_memory_outcome",
            side_effect=MandatoryOverflowError("mandatory task and plan overflow"),
        ):
            with self.assertRaisesRegex(memory_handoff.MemoryHandoffError, "mandatory task and plan overflow"):
                memory_handoff.prepare_bootstrap_envelope(
                    task_card=self.card,
                    lane_id="lane-1",
                    run_id="run-1",
                    worktree_path=self.worktree,
                    base_commit="base-1",
                )
        self.assertIsNone(memory_handoff.load_envelope(self.worktree))

    def test_enriched_handoff_detects_changed_payload_after_finalization(self) -> None:
        outcome = _finalized_lane1_fixture(self.card, self.plan, self.worktree)
        with patch.object(memory_handoff, "_prepare_memory_outcome", return_value=outcome):
            envelope = memory_handoff.prepare_bootstrap_envelope(
                task_card=self.card,
                lane_id="lane-1",
                run_id="run-1",
                worktree_path=self.worktree,
                base_commit="base-1",
            )
        changed = dict(envelope)
        changed["rendered_context"] = {"mandatory": [], "optional": []}
        changed["content_hash"] = contracts.content_hash(changed)
        with self.assertRaisesRegex(memory_handoff.MemoryHandoffError, "rendered_context"):
            memory_handoff.validate_final_context_for_launch(
                context=outcome.context,
                envelope=changed,
                task_card=self.card,
                lane_id="lane-1",
                run_id="run-1",
                worktree_path=self.worktree,
                base_commit="base-1",
            )
        changed_context = json.loads(json.dumps(outcome.context))
        changed_context["integrity"] = "changed-integrity"
        changed_context["content_hash"] = contracts.content_hash(changed_context)
        with self.assertRaisesRegex(memory_handoff.MemoryHandoffError, "context_integrity"):
            memory_handoff.validate_final_context_for_launch(
                context=changed_context,
                envelope=envelope,
                task_card=self.card,
                lane_id="lane-1",
                run_id="run-1",
                worktree_path=self.worktree,
                base_commit="base-1",
            )

    def test_enriched_handoff_rejects_delivery_trace_that_omits_rendered_item(self) -> None:
        outcome = _finalized_lane1_fixture(self.card, self.plan, self.worktree)
        outcome.context["delivery_trace"]["delivered"] = []
        outcome.context["content_hash"] = contracts.content_hash(outcome.context)
        with patch.object(memory_handoff, "_prepare_memory_outcome", return_value=outcome):
            with self.assertRaisesRegex(memory_handoff.MemoryHandoffError, "delivered trace"):
                memory_handoff.prepare_bootstrap_envelope(
                    task_card=self.card,
                    lane_id="lane-1",
                    run_id="run-1",
                    worktree_path=self.worktree,
                    base_commit="base-1",
                )
        self.assertIsNone(memory_handoff.load_envelope(self.worktree))

    def test_enriched_handoff_rejects_packed_item_also_marked_omitted(self) -> None:
        outcome = _finalized_lane1_fixture(self.card, self.plan, self.worktree)
        outcome.envelope["delivery"]["omitted"] = ["case-1", "case-2"]
        outcome.envelope["content_hash"] = contracts.content_hash(outcome.envelope)
        outcome.context["omitted"] = ["case-1", "case-2"]
        outcome.context["delivery_trace"]["omitted"] = ["case-1", "case-2"]
        outcome.context["content_hash"] = contracts.content_hash(outcome.context)
        with patch.object(memory_handoff, "_prepare_memory_outcome", return_value=outcome):
            with self.assertRaisesRegex(memory_handoff.MemoryHandoffError, "packed and omitted"):
                memory_handoff.prepare_bootstrap_envelope(
                    task_card=self.card,
                    lane_id="lane-1",
                    run_id="run-1",
                    worktree_path=self.worktree,
                    base_commit="base-1",
                )
        self.assertIsNone(memory_handoff.load_envelope(self.worktree))

    def test_resume_checks_the_durable_enriched_context_fixture(self) -> None:
        outcome = _finalized_lane1_fixture(self.card, self.plan, self.worktree)
        with patch.object(memory_handoff, "_prepare_memory_outcome", return_value=outcome):
            envelope = memory_handoff.prepare_bootstrap_envelope(
                task_card=self.card,
                lane_id="lane-1",
                run_id="run-1",
                worktree_path=self.worktree,
                base_commit="base-1",
            )
        store_path, _ = memory_handoff.memory_paths(self.worktree)
        memory_store = store.MemoryStore(store_path)
        memory_store.initialize()
        try:
            memory_store.record_final_context(
                outcome.context, envelope_digest=envelope["content_hash"]
            )
        finally:
            memory_store.close()
        memory_handoff.validate_resume_handoff(
            task_card=self.card,
            lane_id="lane-1",
            prior_run_id="run-1",
            worktree_path=self.worktree,
            base_commit="base-1",
        )

        changed_context = json.loads(json.dumps(outcome.context))
        changed_context["rendered_context"]["optional"][0]["content"] = "changed after finalization"
        changed_context["content_hash"] = contracts.content_hash(changed_context)
        memory_store = store.MemoryStore(store_path)
        memory_store.initialize()
        try:
            memory_store.record_final_context(
                changed_context, envelope_digest=envelope["content_hash"]
            )
        finally:
            memory_store.close()
        with self.assertRaisesRegex(memory_handoff.MemoryHandoffError, "rendered context"):
            memory_handoff.validate_resume_handoff(
                task_card=self.card,
                lane_id="lane-1",
                prior_run_id="run-1",
                worktree_path=self.worktree,
                base_commit="base-1",
            )

    def test_resume_rejects_revised_plan_and_changed_prior_payload(self) -> None:
        envelope = memory_handoff.prepare_bootstrap_envelope(
            task_card=self.card,
            lane_id="lane-1",
            run_id="run-1",
            worktree_path=self.worktree,
            base_commit="base-1",
        )
        revised = contracts.revise_plan(
            self.plan, new_plan_id="plan-2", new_content={"steps": ["revised"]}
        )
        changed_card = contracts.make_task_card(
            task=self.card["task"],
            base_commit="base-1",
            branch="lane/memory",
            memory_handoff=contracts.make_memory_handoff(
                objective_id="objective-1", route="ordinary", plan=revised
            ),
        )
        with self.assertRaisesRegex(memory_handoff.MemoryHandoffError, "task card"):
            memory_handoff.prepare_resume_envelope(
                task_card=changed_card,
                lane_id="lane-1",
                run_id="run-2",
                worktree_path=self.worktree,
                base_commit="base-1",
            )
        self.assertEqual(envelope, memory_handoff.load_envelope(self.worktree))

        all_off_card, _ = _task_card({"all_features": False})
        with self.assertRaisesRegex(memory_handoff.MemoryHandoffError, "non-dispatchable"):
            memory_handoff.prepare_resume_envelope(
                task_card=all_off_card,
                lane_id="lane-1",
                run_id="run-2",
                worktree_path=self.worktree,
                base_commit="base-1",
            )
        self.assertEqual(envelope, memory_handoff.load_envelope(self.worktree))

        changed = json.loads(json.dumps(envelope))
        changed["mandatory_content"][0]["content"] = "different task payload"
        changed["mandatory_digest"] = contracts.sha256_hex(changed["mandatory_content"])
        changed["content_hash"] = contracts.content_hash(changed)
        _, envelope_path = memory_handoff.memory_paths(self.worktree)
        envelope_path.write_text(json.dumps(changed), encoding="utf-8")
        with self.assertRaisesRegex(memory_handoff.MemoryHandoffError, "finalized context"):
            memory_handoff.prepare_resume_envelope(
                task_card=self.card,
                lane_id="lane-1",
                run_id="run-2",
                worktree_path=self.worktree,
                base_commit="base-1",
            )
        self.assertEqual(changed, memory_handoff.load_envelope(self.worktree))

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

    def test_split_launch_intent_cannot_overwrite_ambiguous_dispatch(self) -> None:
        envelope = memory_handoff.prepare_bootstrap_envelope(
            task_card=self.card,
            lane_id="lane-1",
            run_id="run-1",
            worktree_path=self.worktree,
            base_commit="base-1",
        )
        memory_handoff.record_ambiguous_dispatch(
            worktree_path=self.worktree,
            envelope=envelope,
        )
        with self.assertRaisesRegex(memory_handoff.MemoryHandoffError, "ambiguous"):
            memory_handoff.record_dispatch_intent(
                worktree_path=self.worktree,
                envelope=envelope,
            )

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
