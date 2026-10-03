from __future__ import annotations

import json
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from memory_harness import apc, config, contracts, experience, store

from orchestrator_harness import (
    bootstrap,
    memory_handoff,
    product_composition,
    resume,
    setup,
)
from orchestrator_harness.bootstrap import _remove_owned_tree
from orchestrator_harness.core import content_hash
from orchestrator_harness.tests.support import configure_local_memory_product
from orchestrator_harness.tests.test_step04_launch_boundary import (
    LaunchBoundaryFixture,
    accepted_card,
    candidate_card,
)


class ProductCompositionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = LaunchBoundaryFixture()
        self.addCleanup(self._cleanup)
        self.secret = "composition-secret-123456789"
        self.scope = configure_local_memory_product(
            self.fixture.harness,
            store_root=self.fixture.root / "operator-memory",
            known_secrets=(self.secret,),
        )

    def _cleanup(self) -> None:
        _remove_owned_tree(self.fixture.root, timeout_seconds=5.0)
        self.fixture.close()

    @staticmethod
    def _rehash(card: dict) -> dict:
        card["content_hash"] = content_hash(card)
        return card

    def _seed_reviewed_experience(self, *, task: str) -> None:
        configuration = product_composition.load_memory_product_config(
            self.fixture.harness
        )
        central = store.MemoryStore(
            product_composition.central_store_path(configuration)
        )
        central.initialize()
        try:
            old_card, old_plan = accepted_card(
                task=task,
                objective="historical-objective",
            )
            decision = contracts.make_decision(
                old_card,
                old_plan,
                configuration=config.configuration_record(config.resolve_config()),
            )
            central.record_decision(decision)
            outcome = contracts.make_outcome(
                decision_id=decision["decision_id"],
                plan_id=old_plan["plan_id"],
                plan_digest=old_plan["content_hash"],
                status="PASS",
                evidence_digest="native-proof-1",
                linked_run_id="historical-run",
                task_card_digest=old_card["content_hash"],
                objective_id=old_plan["objective_id"],
            )
            central.record_outcome(outcome)
            receipt = contracts.make_review_receipt(
                review_id="historical-review",
                outcome=outcome,
                decision=decision,
                task_card=old_card,
                plan=old_plan,
                reviewed_by="ROOT",
                evidence_refs=("review://historical",),
                raw_evidence=f"parser regression fixed; {self.secret}",
            )
            with product_composition.compose_product(
                harness_root=self.fixture.harness,
                task_card=old_card,
                route="ordinary",
            ) as composed:
                experience.ReviewedExperienceService(
                    central,
                    privacy_policy=composed.privacy_policy,
                ).capture(
                    task_card=old_card,
                    plan=old_plan,
                    decision=decision,
                    outcome=outcome,
                    review_receipt=receipt,
                    scope=experience.ExperienceScope.from_record(self.scope),
                )
        finally:
            central.close()

    def test_composition_uses_exact_local_scope_and_one_privacy_policy(self) -> None:
        card, _ = accepted_card()
        card["memory_scope"] = dict(self.scope)
        self._rehash(card)
        with product_composition.compose_product(
            harness_root=self.fixture.harness, task_card=card, route="ordinary"
        ) as composed:
            self.assertEqual(
                {
                    "local-reviewed-experience",
                    "local-template-registry",
                    "local-curated-procedures",
                    "local-generated-procedures",
                },
                {item.store_id for item in composed.search_stores},
            )
            self.assertEqual((self.secret,), composed.privacy_policy.known_secrets)
            self.assertIs(composed.privacy_policy, composed.experience_service.privacy_policy)
            self.assertIs(composed.privacy_policy, composed.procedure_service.privacy_policy)
            states = composed.resolved_memory_config.feature_state_by_name
            self.assertTrue(states["experience_read"].effective)
            self.assertTrue(states["generated_skill_use"].effective)
            self.assertFalse(states["atlas_shared_retrieval"].effective)
            self.assertTrue(states["atlas_shared_retrieval"].requested)
            self.assertFalse(states["atlas_shared_retrieval"].available)
            self.assertEqual(
                "stable", states["atlas_shared_retrieval"].transition_state
            )

        card["memory_scope"] = {**self.scope, "project": "foreign"}
        self._rehash(card)
        with self.assertRaisesRegex(
            product_composition.ProductCompositionError, "scope does not match"
        ):
            product_composition.compose_product(
                harness_root=self.fixture.harness, task_card=card, route="ordinary"
            )

    def test_secret_rotation_failure_precedes_epoch_and_worktree_effects(self) -> None:
        configured = product_composition.load_memory_product_config(
            self.fixture.harness
        )
        configured.known_secret_files[0].write_text(
            "unacknowledged-rotation\n", encoding="utf-8"
        )
        configured.known_secret_files[0].chmod(0o600)
        card, _ = accepted_card(objective="rotation-preflight")
        result, worktree = self.fixture.run_bootstrap(
            lane_id="rotation-preflight", card=card
        )
        self.assertFalse(result["ok"], result)
        self.assertIn("rotation", result["summary"])
        self.assertFalse(worktree.exists())
        self.assertFalse(
            (
                self.fixture.runtime
                / "epochs"
                / self.fixture.EPOCH
                / "lanes"
                / "rotation-preflight"
            ).exists()
        )

    def test_configured_secret_in_worker_task_card_fails_before_worktree(self) -> None:
        card, _ = accepted_card(objective="secret-task-preflight")
        card["failure_context"] = f"parser failed with {self.secret}"
        self._rehash(card)
        result, worktree = self.fixture.run_bootstrap(
            lane_id="secret-task-preflight", card=card
        )
        self.assertFalse(result["ok"], result)
        self.assertIn("configured content secret", result["summary"])
        self.assertNotIn(self.secret, result["summary"])
        self.assertFalse(worktree.exists())

    def test_unsupported_provider_is_refused_before_enhanced_product_effects(self) -> None:
        qwen = LaunchBoundaryFixture(include_qwen=True)
        self.addCleanup(qwen.close)
        configure_local_memory_product(
            qwen.harness,
            store_root=qwen.root / "operator-memory",
        )
        card, _ = accepted_card(objective="qwen-enhanced")
        result, worktree = qwen.run_bootstrap(
            lane_id="qwen-enhanced", card=card, provider="qwen-code"
        )
        self.assertFalse(result["ok"], result)
        self.assertEqual(
            bootstrap.BOOTSTRAP_PROVIDER_ISOLATION_UNAVAILABLE,
            result["code"],
        )
        self.assertIn("filesystem isolation unavailable", result["summary"])
        self.assertFalse(worktree.exists())
        self.assertFalse(
            product_composition.central_store_path(
                product_composition.load_memory_product_config(qwen.harness)
            ).exists()
        )

        all_off, _ = accepted_card(
            objective="qwen-all-off", configuration={"all_features": False}
        )
        allowed, allowed_worktree = qwen.run_bootstrap(
            lane_id="qwen-all-off", card=all_off, provider="qwen-code"
        )
        self.assertTrue(allowed["ok"], allowed)
        self.assertTrue(allowed_worktree.exists())

    def test_root_setup_initializes_secret_rotation_witness_once(self) -> None:
        configured = product_composition.load_memory_product_config(
            self.fixture.harness
        )
        key = configured.store_root / ".memory-product-hmac.key"
        state = configured.store_root / ".memory-product-secret-state.json"
        key.unlink()
        state.unlink()
        with (
            patch.object(setup, "find_harness_root", return_value=self.fixture.harness),
            patch.object(setup, "_start_monitor", return_value={"status": "test"}),
        ):
            result = setup.run_setup(overwrite=True)
        self.assertTrue(result["ok"], result)
        self.assertTrue(key.is_file())
        self.assertTrue(state.is_file())
        before = state.read_bytes()
        with (
            patch.object(setup, "find_harness_root", return_value=self.fixture.harness),
            patch.object(setup, "_start_monitor", return_value={"status": "test"}),
        ):
            replay = setup.run_setup(overwrite=True)
        self.assertTrue(replay["ok"], replay)
        self.assertEqual(before, state.read_bytes())

    def test_public_bootstrap_queries_reviewed_central_memory_and_redacts_secret(self) -> None:
        self._seed_reviewed_experience(
            task="regression failure test repair parser"
        )

        card, _ = accepted_card(task="regression failure test repair parser")
        result, worktree = self.fixture.run_bootstrap(lane_id="composed", card=card)
        self.assertTrue(result["ok"], result)
        envelope = memory_handoff.load_envelope(worktree)
        rendered = json.dumps(envelope)
        self.assertNotIn(self.secret, rendered)
        self.assertIn(
            "local-reviewed-experience",
            {
                item["provenance"]["source_id"]
                for item in envelope["delivery_trace"]["selected"]
            },
        )
        isolation = tomllib.loads(
            (worktree / ".codex" / "config.toml").read_text(encoding="utf-8")
        )["permissions"]["worker-isolated"]["filesystem"]
        configured = product_composition.load_memory_product_config(
            self.fixture.harness
        )
        self.assertEqual("deny", isolation[str(configured.store_root)])
        self.assertEqual(
            "deny", isolation[str(configured.known_secret_files[0])]
        )
        for path in (
            worktree / ".agent-workspace" / "task-card.json",
            worktree / ".agent-workspace" / "worker-prompt.md",
            worktree / ".agent-workspace" / "invocation.json",
        ):
            self.assertNotIn(self.secret, path.read_text(encoding="utf-8"))

    def test_public_resume_requeries_authoritative_local_memory(self) -> None:
        task = "regression failure test repair parser"
        self._seed_reviewed_experience(task=task)
        card, _ = accepted_card(task=task)
        prepared, worktree = self.fixture.run_bootstrap(
            lane_id="composed-resume", card=card
        )
        self.assertTrue(prepared["ok"], prepared)
        first = memory_handoff.load_envelope(worktree)
        self.fixture.make_resumable("composed-resume")

        resumed = self.fixture.run_resume(lane_id="composed-resume", card=card)
        self.assertTrue(resumed["ok"], resumed)
        second = memory_handoff.load_envelope(worktree)
        self.assertNotEqual(first["run_id"], second["run_id"])
        self.assertIn(
            "local-reviewed-experience",
            {
                item["provenance"]["source_id"]
                for item in second["delivery_trace"]["selected"]
            },
        )

    def test_pending_plan_activation_finalizes_same_worktree_under_new_run(self) -> None:
        pending_card, candidate = candidate_card(
            task="review then execute the exact candidate",
            objective="activate-candidate",
        )
        prepared, worktree = self.fixture.run_bootstrap(
            lane_id="activate-candidate", card=pending_card
        )
        self.assertEqual(bootstrap.BOOTSTRAP_PLAN_PENDING, prepared["code"])
        before = self.fixture.lane_record("activate-candidate")
        prior_run_id = before["run_id"]

        accepted_plan = contracts.accept_plan(candidate)
        accepted_handoff = contracts.make_memory_handoff(
            objective_id=candidate["objective_id"],
            route=candidate["route"],
            plan=accepted_plan,
            configuration=pending_card["memory_handoff"]["configuration"],
            checkpoint="root-plan-acceptance-checkpoint",
        )
        accepted_card_record = contracts.make_task_card(
            task=pending_card["task"],
            base_commit=pending_card["base_commit"],
            branch=pending_card["branch"],
            memory_handoff=accepted_handoff,
        )
        accepted_path = self.fixture.root / "accepted-plan-task-card.json"
        self.fixture.write_json(accepted_path, accepted_card_record)

        with (
            patch.object(resume, "find_harness_root", return_value=self.fixture.harness),
            patch.object(
                resume,
                "find_active_lane",
                side_effect=self.fixture._read_active_lane,
            ),
        ):
            activated = resume.activate_pending_plan(
                lane_id="activate-candidate",
                accepted_task_card=str(accepted_path),
            )

        self.assertTrue(activated["ok"], activated)
        after = self.fixture.lane_record("activate-candidate")
        self.assertEqual(worktree, Path(after["worktree_path"]))
        self.assertNotEqual(prior_run_id, after["run_id"])
        self.assertEqual("running", after["lifecycle"])
        self.assertEqual("execution_accepted", after["memory_plan_state"])
        self.assertTrue(after["dispatchable"])
        self.assertTrue(after["launch_pending"])
        envelope = memory_handoff.load_envelope(worktree)
        self.assertIsNotNone(envelope)
        self.assertEqual(after["run_id"], envelope["run_id"])
        self.assertEqual(accepted_plan["content_hash"], envelope["plan_digest"])

    def test_pending_plan_activation_rejects_identity_or_execution_ownership(self) -> None:
        pending_card, candidate = candidate_card(objective="activation-guard")
        prepared, worktree = self.fixture.run_bootstrap(
            lane_id="activation-guard", card=pending_card
        )
        self.assertEqual(bootstrap.BOOTSTRAP_PLAN_PENDING, prepared["code"])
        lane = self.fixture.lane_record("activation-guard")
        accepted_plan = contracts.accept_plan(candidate)
        accepted_handoff = contracts.make_memory_handoff(
            objective_id=candidate["objective_id"],
            route=candidate["route"],
            plan=accepted_plan,
            checkpoint="root-plan-acceptance-checkpoint",
        )
        wrong_base = contracts.make_task_card(
            task=pending_card["task"],
            base_commit="other-base",
            branch=pending_card["branch"],
            memory_handoff=accepted_handoff,
        )
        with self.assertRaisesRegex(
            memory_handoff.MemoryHandoffError, "base_commit"
        ):
            resume.validate_pending_plan_activation(
                lane=lane,
                accepted_task_card=wrong_base,
                worktree_path=worktree,
            )

        accepted_card_record = contracts.make_task_card(
            task=pending_card["task"],
            base_commit=pending_card["base_commit"],
            branch=pending_card["branch"],
            memory_handoff=accepted_handoff,
        )
        accepted_path = self.fixture.root / "owned-activation-card.json"
        self.fixture.write_json(accepted_path, accepted_card_record)
        self.fixture.write_json(
            worktree / ".agent-workspace" / "invocation.json",
            {"unexpected": "execution ownership"},
        )
        with (
            patch.object(resume, "find_harness_root", return_value=self.fixture.harness),
            patch.object(
                resume,
                "find_active_lane",
                side_effect=self.fixture._read_active_lane,
            ),
        ):
            refused = resume.activate_pending_plan(
                lane_id="activation-guard",
                accepted_task_card=str(accepted_path),
            )
        self.assertFalse(refused["ok"], refused)
        self.assertIn("invocation or controller ownership", refused["summary"])
        self.assertEqual(lane["run_id"], self.fixture.lane_record("activation-guard")["run_id"])
        self.assertEqual("prepared", self.fixture.lane_record("activation-guard")["lifecycle"])

    def test_public_fixed_strategies_preserve_problem_context_and_deeper(self) -> None:
        for index, (strategy, failure_context) in enumerate(
            (
                ("standard", None),
                ("problem_focused", "parser raises IndexError after retry"),
                ("deeper", None),
            ),
            start=1,
        ):
            with self.subTest(strategy=strategy):
                card, _ = accepted_card(
                    objective=f"strategy-{index}",
                    configuration={"strategy": strategy},
                )
                if failure_context is not None:
                    card["failure_context"] = failure_context
                    self._rehash(card)
                result, worktree = self.fixture.run_bootstrap(
                    lane_id=f"strategy-{index}", card=card
                )
                self.assertTrue(result["ok"], result)
                envelope = memory_handoff.load_envelope(worktree)
                self.assertEqual(strategy, envelope["strategy"])
                if failure_context is not None:
                    # The preparation service falls Problem-focused back to
                    # Standard unless it received a nonempty real failure
                    # context.  Remaining Problem-focused in the public
                    # envelope therefore proves the composed value reached the
                    # defining query without requiring raw query text to leak
                    # into its compact durable trace.
                    self.assertEqual("problem_focused", envelope["strategy"])

    def test_public_direct_fill_and_fresh_fallback_are_reachable(self) -> None:
        direct, _ = candidate_card(task="regression failure test repair")
        direct["root_replan"] = {
            "requested_by": "ROOT",
            "reason": "reuse the exact eligible template",
        }
        direct["template_bindings"] = {
            "failure": "empty parser input",
            "component": "parser",
        }
        self._rehash(direct)
        result, worktree = self.fixture.run_bootstrap(
            lane_id="direct-fill", card=direct
        )
        self.assertEqual("BOOTSTRAP_PLAN_PENDING", result["code"])
        memory_store = store.MemoryStore(memory_handoff.memory_paths(worktree)[0])
        memory_store.initialize()
        try:
            row = memory_store._require_connection().execute(
                "SELECT record FROM plan_dispositions"
            ).fetchone()
            self.assertEqual("direct_fill", json.loads(row[0])["branch"])
        finally:
            memory_store.close()

        fresh, _ = candidate_card(task="entirely unrelated objective")
        fresh["root_replan"] = {
            "requested_by": "ROOT",
            "reason": "try bounded reuse",
        }
        self._rehash(fresh)
        result, worktree = self.fixture.run_bootstrap(lane_id="fresh", card=fresh)
        self.assertEqual("BOOTSTRAP_PLAN_PENDING", result["code"])
        memory_store = store.MemoryStore(memory_handoff.memory_paths(worktree)[0])
        memory_store.initialize()
        try:
            row = memory_store._require_connection().execute(
                "SELECT record FROM plan_dispositions"
            ).fetchone()
            self.assertEqual("fresh", json.loads(row[0])["branch"])
        finally:
            memory_store.close()

    def test_public_near_match_uses_explicit_composed_apc_launcher(self) -> None:
        card, _ = candidate_card(task="regression failure parser")
        card["root_replan"] = {
            "requested_by": "ROOT",
            "reason": "adapt the bounded near match",
        }
        card["apc_adaptation_binding"] = {
            "provider": "codex",
            "model": "test-model",
            "cli": "codex",
            "effort": "low",
            "source": "explicit",
        }
        self._rehash(card)
        calls: list[dict] = []

        def factory(**_kwargs):
            def launch(request):
                calls.append(dict(request))
                proposal = contracts.make_plan(
                    plan_id="apc-proposal-1",
                    objective_id=request["parent_objective_id"],
                    route="ordinary",
                    state="proposed",
                    content={
                        "fixed_steps": list(request["template"]["fixed_steps"]),
                        "bindings": {
                            "failure": "parser failure",
                            "component": "parser",
                        },
                        "verification_intent": request["template"][
                            "verification_intent"
                        ],
                    },
                    source={
                        "template_id": request["template_id"],
                        "template_version": request["template_version"],
                        "branch": "apc_proposal",
                        "apc_request": request["content_hash"],
                    },
                )
                return {
                    "invocation_id": "controller:41:test-apc",
                    "lane_id": "apc-child-test",
                    "run_id": "apc-run-test",
                    "result": apc.make_apc_result(request, proposal),
                    "cleanup": {"cleanup_proven": True},
                }

            return launch

        with patch(
            "memory_harness.harness_child.make_native_apc_launcher",
            side_effect=factory,
        ) as constructed:
            result, worktree = self.fixture.run_bootstrap(
                lane_id="apc", card=card
            )
        self.assertEqual("BOOTSTRAP_PLAN_PENDING", result["code"])
        constructed.assert_called_once()
        self.assertEqual(1, len(calls))
        self.assertEqual(card["apc_adaptation_binding"], calls[0]["binding"])
        memory_store = store.MemoryStore(memory_handoff.memory_paths(worktree)[0])
        memory_store.initialize()
        try:
            row = memory_store._require_connection().execute(
                "SELECT record FROM plan_dispositions"
            ).fetchone()
            self.assertEqual("apc_proposal", json.loads(row[0])["branch"])
        finally:
            memory_store.close()

    def test_unisolated_apc_provider_is_refused_before_worktree(self) -> None:
        card, _ = candidate_card(task="regression failure parser")
        card["root_replan"] = {
            "requested_by": "ROOT",
            "reason": "adapt the bounded near match",
        }
        card["apc_adaptation_binding"] = {
            "provider": "qwen-code",
            "model": "test-model",
            "cli": "qwen",
            "effort": "low",
            "source": "explicit",
        }
        self._rehash(card)
        result, worktree = self.fixture.run_bootstrap(
            lane_id="unisolated-apc", card=card
        )
        self.assertFalse(result["ok"], result)
        self.assertIn("APC provider filesystem isolation unavailable", result["summary"])
        self.assertFalse(worktree.exists())

    def test_all_off_does_not_create_the_central_store(self) -> None:
        card, _ = accepted_card(configuration={"all_features": False})
        result, _ = self.fixture.run_bootstrap(lane_id="all-off", card=card)
        self.assertTrue(result["ok"], result)
        configured = product_composition.load_memory_product_config(
            self.fixture.harness
        )
        self.assertFalse(product_composition.central_store_path(configured).exists())


if __name__ == "__main__":
    unittest.main()
