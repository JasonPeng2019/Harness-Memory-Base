"""BEHAVIOR-03: only an accepted plan dispatches once with exact safe context."""

from __future__ import annotations

import sys
import tempfile
import unittest
from copy import deepcopy
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
            "checkpoint": "checkpoint-1",
            "execution_role": "worker",
            "invocation_target": "harness:worker",
            "recipient": "worker:lane-1",
            "mandatory_content": [
                {"id": "task", "kind": "task", "content": self.card["task"]},
                {"id": "accepted-plan", "kind": "accepted-plan", "content": self.accepted["content"]},
                {"id": "base", "kind": "base", "content": "base-1"},
                {"id": "route", "kind": "route", "content": "ordinary"},
                {"id": "checkpoint", "kind": "checkpoint", "content": "checkpoint-1"},
                {"id": "security", "kind": "security", "content": context.ROLE_SEPARATION},
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
        self.assertEqual("checkpoint-1", finalized.context["checkpoint"])
        self.assertEqual("worker", finalized.context["execution_role"])
        self.assertEqual("harness:worker", finalized.envelope["invocation_target"])
        self.assertEqual("worker:lane-1", finalized.envelope["recipient"])
        self.assertEqual(self.accepted["revision"], finalized.context["plan_revision"])
        self.assertEqual(finalized.context["context_id"], finalized.envelope["final_context_id"])
        self.assertEqual(
            context.ROLE_SEPARATION["control_plane"],
            finalized.context["role_separation"]["control_plane"],
        )
        self.assertFalse(finalized.context["role_separation"]["may_approve"])
        self.assertFalse(finalized.context["role_separation"]["may_execute_parent"])
        context.validate_final_context(
            finalized.context, envelope=finalized.envelope, task_card=self.card,
            plan=self.accepted, lane_id="lane-1", run_id="run-1",
            base_commit="base-1", worktree_path=str(self.worktree),
        )

    def test_exact_mandatory_state_and_canonical_destination_are_required(self) -> None:
        mandatory = self._finalize().envelope["mandatory_content"]
        for identifier in ("task", "accepted-plan", "base", "route", "checkpoint", "security"):
            with self.subTest(missing=identifier):
                with self.assertRaisesRegex(ValueError, identifier.replace("-", ".")):
                    self._finalize(mandatory_content=[item for item in mandatory if item["id"] != identifier])
            with self.subTest(substituted=identifier):
                changed = deepcopy(mandatory)
                next(item for item in changed if item["id"] == identifier)["content"] = "substituted"
                with self.assertRaisesRegex(ValueError, identifier.replace("-", ".")):
                    self._finalize(mandatory_content=changed)
        for field in ("checkpoint", "execution_role", "invocation_target", "recipient"):
            with self.subTest(field=field):
                with self.assertRaisesRegex(ValueError, field.replace("_", " ")):
                    self._finalize(**{field: "  value  "})

    def test_complete_optional_trace_binds_provenance_content_and_omission_reason(self) -> None:
        finalized = self._finalize(optional_items=[
            {"id": "packed", "kind": "memory", "origin": "everos", "revision_id": "r1", "content": "short"},
            {"id": "omitted", "kind": "memory", "origin": "atlas", "revision_id": "r2", "content": "x" * 5000},
        ])
        trace = finalized.context["delivery_trace"]
        self.assertEqual(["packed", "omitted"], [item["id"] for item in trace["selected"]])
        self.assertEqual(["packed"], [item["id"] for item in trace["packed"]])
        self.assertEqual(trace["packed"], trace["context_delivered"])
        self.assertEqual("exceeds the optional allowance", trace["omitted"][0]["reason"])
        self.assertEqual(trace, finalized.envelope["delivery_trace"])
        for item in trace["selected"]:
            self.assertEqual(64, len(item["provenance_digest"]))
            self.assertEqual(64, len(item["content_digest"]))
        changed = self._finalize(optional_items=[
            {"id": "packed", "kind": "memory", "origin": "everos", "revision_id": "r2", "content": "short"},
            {"id": "omitted", "kind": "memory", "origin": "atlas", "revision_id": "r2", "content": "x" * 5000},
        ])
        self.assertNotEqual(finalized.context["context_id"], changed.context["context_id"])
        changed_content = self._finalize(optional_items=[
            {"id": "packed", "kind": "memory", "origin": "everos", "revision_id": "r1", "content": "different"},
            {"id": "omitted", "kind": "memory", "origin": "atlas", "revision_id": "r2", "content": "x" * 5000},
        ])
        self.assertNotEqual(finalized.context["integrity"], changed_content.context["integrity"])
        changed_reason = self._finalize(optional_items=[
            {"id": "packed", "kind": "memory", "origin": "everos", "revision_id": "r1", "content": "short"},
            {"id": "omitted", "kind": "memory", "origin": "atlas", "revision_id": "r2", "content": "x" * 5000},
        ], freshness_check=lambda item: item["id"] != "omitted")
        self.assertNotEqual(finalized.context["context_id"], changed_reason.context["context_id"])

    def test_destination_changes_create_distinct_ready_contexts(self) -> None:
        original = self._finalize()
        for field, value in (("invocation_target", "harness:other"), ("recipient", "worker:other")):
            with self.subTest(field=field):
                changed = self._finalize(**{field: value})
                self.assertNotEqual(original.context["context_id"], changed.context["context_id"])
        changed_mandatory = deepcopy(original.envelope["mandatory_content"])
        next(item for item in changed_mandatory if item["id"] == "checkpoint")["content"] = "checkpoint-2"
        changed_checkpoint = self._finalize(checkpoint="checkpoint-2", mandatory_content=changed_mandatory)
        self.assertNotEqual(original.context["context_id"], changed_checkpoint.context["context_id"])

    def test_plan_affecting_selection_flag_changes_trace_identity(self) -> None:
        item = {"id": "memory-1", "origin": "everos", "content": "same content"}
        ordinary = self._finalize(optional_items=[{**item, "plan_affecting": False}], freshness_check=lambda item: True)
        affecting = self._finalize(optional_items=[{**item, "plan_affecting": True}], freshness_check=lambda item: True)
        self.assertNotEqual(ordinary.context["context_id"], affecting.context["context_id"])

    def test_secret_optional_provenance_never_enters_trace(self) -> None:
        policy = privacy.PrivacyPolicy(known_secrets=("super-secret-value",))
        with self.assertRaises(context.OptionalItemError):
            self._finalize(optional_items=[{
                "id": "memory-1", "origin": "super-secret-value", "content": "note",
            }], privacy_policy=policy)

    def test_finalized_envelope_cannot_downgrade_to_generic_validation(self) -> None:
        finalized = self._finalize()
        self.assertEqual(contracts.FINAL_ENVELOPE_SCHEMA, finalized.envelope["schema"])
        stripped = deepcopy(finalized.envelope)
        for field in (
            "final_context", "final_context_id", "final_context_integrity", "task",
            "plan_revision", "accepted_by", "checkpoint", "execution_role",
            "invocation_target", "recipient", "delivery_trace",
        ):
            stripped.pop(field)
        stripped["content_hash"] = contracts.content_hash(stripped)
        with self.assertRaises(ValueError):
            contracts.validate_envelope(
                stripped, task_card=self.card, plan=self.accepted,
                lane_id="lane-1", run_id="run-1", base_commit="base-1",
                worktree_path=str(self.worktree),
            )
        stripped["schema"] = contracts.ENVELOPE_SCHEMA
        stripped["content_hash"] = contracts.content_hash(stripped)
        with self.assertRaises(ValueError):
            contracts.validate_envelope(
                stripped, task_card=self.card, plan=self.accepted,
                lane_id="lane-1", run_id="run-1", base_commit="base-1",
                worktree_path=str(self.worktree), require_final_context=True,
            )

    def test_rehashed_record_and_envelope_tampering_fails_validation(self) -> None:
        finalized = self._finalize(optional_items=[{"id": "a", "content": "note"}])
        for field, value in (
            ("integrity", "0" * 64), ("context_id", "0" * 64),
            ("recipient", "worker:other"), ("invocation_target", "harness:other"),
        ):
            changed = deepcopy(finalized.context)
            changed[field] = value
            changed["content_hash"] = contracts.content_hash(changed)
            with self.subTest(field=field), self.assertRaises(ValueError):
                contracts.validate_finalized_context(changed)
        for field in ("mandatory_content", "optional_content", "delivery_trace"):
            changed = deepcopy(finalized.envelope)
            if field == "delivery_trace":
                changed[field]["context_delivered"] = []
            else:
                changed[field][0]["content"] = "changed"
            changed["content_hash"] = contracts.content_hash(changed)
            with self.subTest(field=field), self.assertRaises((ValueError, context.ContextError)):
                context.validate_final_context(
                    finalized.context, envelope=changed, task_card=self.card,
                    plan=self.accepted, lane_id="lane-1", run_id="run-1",
                    base_commit="base-1", worktree_path=str(self.worktree),
                )

    def test_preparation_rejects_missing_security_before_final_context_persistence(self) -> None:
        service = preparation.PreparationService(store=self.memory_store, limits=self.limits)
        mandatory = [item for item in self._finalize().envelope["mandatory_content"] if item["id"] != "security"]
        with self.assertRaisesRegex(ValueError, "security"):
            service.prepare(
                task_card=self.card, plan=self.accepted, objective_id="objective-1",
                lane_id="lane-1", run_id="run-1", worktree_path=str(self.worktree),
                base_commit="base-1", checkpoint="checkpoint-1",
                execution_role="worker", invocation_target="harness:worker",
                recipient="worker:lane-1", mandatory_content=mandatory, finalize=True,
            )
        self.assertIsNone(self.memory_store.get_final_context_for_decision(self.decision_id))

    def test_envelope_rejects_rehashed_domain_binding_substitution(self) -> None:
        finalized = self._finalize()
        changed = deepcopy(finalized.envelope)
        changed["recipient"] = "worker:other"
        changed["content_hash"] = contracts.content_hash(changed)
        with self.assertRaisesRegex(ValueError, "recipient"):
            contracts.validate_envelope(
                changed, task_card=self.card, plan=self.accepted,
                lane_id="lane-1", run_id="run-1", base_commit="base-1",
                worktree_path=str(self.worktree),
            )

    def test_persisted_context_id_never_overwrites_a_different_record(self) -> None:
        finalized = self._finalize()
        first = self.memory_store.record_final_context(
            finalized.context, envelope_digest=finalized.envelope["content_hash"]
        )
        changed = deepcopy(finalized.context)
        changed["created_at"] = "2030-01-01T00:00:00Z"
        changed["content_hash"] = contracts.content_hash(changed)
        self.assertEqual(first["context_id"], changed["context_id"])
        with self.assertRaises(store.StoreError):
            self.memory_store.record_final_context(changed, envelope_digest="different-envelope")
        self.assertEqual(first, self.memory_store.get_final_context(first["context_id"]))

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
        mandatory = self._finalize().envelope["mandatory_content"]
        with self.assertRaises(context.MandatoryOverflowError):
            self._finalize(
                mandatory_content=[*mandatory, {"id": "required-constraints", "content": "x" * 8000}]
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
            checkpoint="checkpoint-1",
            execution_role="worker",
            invocation_target="harness:worker",
            recipient="worker:lane-1",
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
            checkpoint="checkpoint-1",
            execution_role="worker",
            invocation_target="harness:worker",
            recipient="worker:lane-1",
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
                checkpoint="checkpoint-1",
                execution_role="worker",
                invocation_target="harness:worker",
                recipient="worker:lane-1",
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
                checkpoint="checkpoint-1",
                execution_role="worker",
                invocation_target="harness:worker",
                recipient="worker:lane-1",
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
                checkpoint="checkpoint-1",
                execution_role="worker",
                invocation_target="harness:worker",
                recipient="worker:lane-1",
                launcher=lambda envelope: self.fail("invalid target must not launch"),
            )

    def test_dispatch_finalized_rejects_absent_context_and_stripped_generic_envelope(self) -> None:
        finalized = self._finalize()
        stripped = deepcopy(finalized.envelope)
        for field in (
            "final_context", "final_context_id", "final_context_integrity", "task",
            "plan_revision", "accepted_by", "checkpoint", "execution_role",
            "invocation_target", "recipient", "delivery_trace",
        ):
            stripped.pop(field)
        stripped["schema"] = contracts.ENVELOPE_SCHEMA
        stripped["content_hash"] = contracts.content_hash(stripped)
        contracts.validate_envelope(
            stripped, task_card=self.card, plan=self.accepted, lane_id="lane-1",
            run_id="run-1", worktree_path=str(self.worktree), base_commit="base-1",
        )
        memory_runtime = runtime.MemoryRuntime(self.memory_store)
        calls: list[object] = []
        for envelope, record in (
            (finalized.envelope, None), (stripped, None), (stripped, finalized.context),
        ):
            with self.subTest(schema=envelope["schema"], context=record is not None):
                with self.assertRaises(runtime.RuntimeError):
                    memory_runtime.dispatch_finalized(
                        envelope=envelope, context=record, task_card=self.card,
                        plan=self.accepted, lane_id="lane-1", run_id="run-1",
                        worktree_path=str(self.worktree), base_commit="base-1",
                        checkpoint="checkpoint-1", execution_role="worker",
                        invocation_target="harness:worker", recipient="worker:lane-1",
                        launcher=lambda value: calls.append(value) or {"invocation_id": "unexpected"},
                    )
                operation = contracts.make_operation(kind="dispatch", envelope=envelope)
                with self.assertRaises(store.StoreError):
                    self.memory_store.get_operation(operation["operation_id"])
        self.assertEqual([], calls)

    def test_dispatch_finalized_requires_each_actual_destination_field(self) -> None:
        finalized = self._finalize()
        memory_runtime = runtime.MemoryRuntime(self.memory_store)
        calls: list[object] = []
        actual = {
            "checkpoint": "checkpoint-1", "execution_role": "worker",
            "invocation_target": "harness:worker", "recipient": "worker:lane-1",
        }
        for field in actual:
            for value in (None, "", "wrong-value"):
                with self.subTest(field=field, value=value):
                    with self.assertRaises(runtime.RuntimeError):
                        memory_runtime.dispatch_finalized(
                            envelope=finalized.envelope, context=finalized.context,
                            task_card=self.card, plan=self.accepted,
                            lane_id="lane-1", run_id="run-1",
                            worktree_path=str(self.worktree), base_commit="base-1",
                            launcher=lambda value: calls.append(value) or {"invocation_id": "unexpected"},
                            **(actual | {field: value}),
                        )
            with self.subTest(field=field, omitted=True):
                with self.assertRaises(runtime.RuntimeError):
                    memory_runtime.dispatch_finalized(
                        envelope=finalized.envelope, context=finalized.context,
                        task_card=self.card, plan=self.accepted,
                        lane_id="lane-1", run_id="run-1",
                        worktree_path=str(self.worktree), base_commit="base-1",
                        launcher=lambda value: calls.append(value) or {"invocation_id": "unexpected"},
                        **{key: value for key, value in actual.items() if key != field},
                    )
        self.assertEqual([], calls)
        operation = contracts.make_operation(kind="dispatch", envelope=finalized.envelope)
        with self.assertRaises(store.StoreError):
            self.memory_store.get_operation(operation["operation_id"])

    def test_selected_candidate_source_provenance_changes_ready_identity(self) -> None:
        payload = {"summary": "identical rendered content"}
        candidates = []
        for source_id in ("source-a", "source-b"):
            candidate = contracts.make_candidate(
                kind="historical_evidence", logical_id="case-1", revision_id="r1",
                origin="everos", source_id=source_id,
                payload_digest=contracts.sha256_hex(payload), payload=payload,
                scope={"project": "p"}, provenance=[{"store_id": "everos"}],
                freshness="frozen", disposition="selected",
            )
            candidate["approval_id"] = "approval-1"
            candidate["plan_affecting"] = False
            candidate["content_hash"] = contracts.content_hash(candidate)
            candidates.append(candidate)
        service = preparation.PreparationService(limits=self.limits)
        mandatory = self._finalize().envelope["mandatory_content"]
        def finalize_candidate(candidate):
            outcome = preparation.PreparationOutcome(
                mode="planning", decision=self.decision, preparation=None,
                trace={"candidates": [candidate]}, disposition=None, plan=self.accepted,
            )
            return service._finalize(
                outcome=outcome, task_card=self.card, plan=self.accepted,
                lane_id="lane-1", run_id="run-1", worktree_path=str(self.worktree),
                base_commit="base-1", checkpoint="checkpoint-1",
                execution_role="worker", invocation_target="harness:worker",
                recipient="worker:lane-1", mandatory_content=mandatory,
                optional_items=(), freshness_check=None,
            )
        finalized = [finalize_candidate(candidate) for candidate in candidates]
        for candidate, prepared in zip(candidates, finalized):
            selected = prepared.context["delivery_trace"]["selected"][0]
            self.assertEqual(candidate["source_id"], selected["provenance"]["source_id"])
            self.assertEqual("approval-1", selected["provenance"]["approval_id"])
            self.assertEqual("frozen", selected["provenance"]["freshness"])
            self.assertEqual(candidate["scope"], selected["provenance"]["scope"])
            self.assertEqual(candidate["provenance"], selected["provenance"]["provenance"])
            self.assertNotIn("payload", selected["provenance"])
        self.assertEqual(finalized[0].envelope["optional_content"], finalized[1].envelope["optional_content"])
        self.assertNotEqual(finalized[0].context["context_id"], finalized[1].context["context_id"])
        for field, value in (
            ("scope", {"project": "other"}), ("approval_id", "approval-2"),
            ("freshness", "live"), ("provenance", [{"store_id": "other"}]),
            ("plan_affecting", True),
        ):
            with self.subTest(field=field):
                changed = deepcopy(candidates[0])
                changed[field] = value
                changed["content_hash"] = contracts.content_hash(changed)
                revised = finalize_candidate(changed)
                self.assertEqual(finalized[0].envelope["optional_content"], revised.envelope["optional_content"])
                self.assertNotEqual(finalized[0].context["context_id"], revised.context["context_id"])

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
