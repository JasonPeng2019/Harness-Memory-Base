from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from orchestrator_harness import memory_admin
from memory_harness import atlas, config, contracts, privacy, procedures, store
from memory_harness.snapshot import SnapshotService


class _UnusedAdapter:
    metric = "cosine"


class _AtlasCollection:
    def __init__(self) -> None:
        self.documents: dict[str, dict] = {}

    def find_one(self, query: dict) -> dict | None:
        document = self.documents.get(query["_id"])
        return dict(document) if document is not None else None

    def insert_one(self, document: dict) -> object:
        if document["_id"] in self.documents:
            raise RuntimeError("duplicate id")
        self.documents[document["_id"]] = dict(document)
        return object()

    def replace_one(self, query: dict, document: dict, *, upsert: bool) -> object:
        existing = self.documents.get(query["_id"])
        matched = existing is not None and all(
            existing.get(key) == value for key, value in query.items()
        )
        if matched:
            self.documents[document["_id"]] = dict(document)
        return type("ReplaceResult", (), {"matched_count": int(matched)})()


class _AdminAtlasAdapter(atlas.AtlasProcedureAdapter):
    def __init__(self) -> None:
        super().__init__(
            collection=_AtlasCollection(),
            vector_store=object(),
            location={
                "database": "admin_test",
                "collection": "trusted_procedures",
                "index": "procedure_vector",
            },
        )
        self.publication_writes = 0
        self.lose_publication_ack = False
        self.before_publication_write = None

    def write_publication(self, publication: dict) -> dict:
        self.publication_writes += 1
        if self.before_publication_write is not None:
            self.before_publication_write()
        result = super().write_publication(publication)
        if self.lose_publication_ack:
            self.lose_publication_ack = False
            raise atlas.AtlasProcedureAmbiguityError("synthetic lost acknowledgement")
        return result


class _BarrierMemoryStore(store.MemoryStore):
    """Force competing admins past their read preflight before durable CAS."""

    def __init__(self, path: Path, barrier: threading.Barrier) -> None:
        super().__init__(path)
        self._test_barrier = barrier

    def replace_plan_disposition(self, disposition: dict, *, expected_content_hash: str) -> dict:
        self._test_barrier.wait(timeout=5)
        return super().replace_plan_disposition(
            disposition, expected_content_hash=expected_content_hash
        )


class MemoryAdministrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.state = store.MemoryStore(self.root / "state" / "memory.sqlite3")
        self.state.initialize()
        self.scope = {
            "application": "harness",
            "project": "product-a",
            "namespace": "admin-test",
            "owner": "root-agent",
        }

        def authorize(action: str, scope: dict[str, str], path: Path, credential: object) -> bool:
            return credential == "operator" and scope == self.scope and action in {
                "export", "inspect", "restore",
            }

        self.snapshots = SnapshotService(
            authorizer=authorize,
            dependency_verifier=lambda capability, reference, scope: (
                scope == self.scope and capability == "experience" and reference == "ready-ref"
            ),
        )
        self.admin = memory_admin.MemoryAdmin(
            memory_admin.RootAdminContext(
                store=self.state,
                scope=self.scope,
                trusted_issuers=("ROOT",),
                privacy_policy=privacy.PrivacyPolicy(known_secrets=("admin-secret-value",)),
                snapshots=self.snapshots,
            )
        )

    def tearDown(self) -> None:
        self.state.close()
        self.temporary.cleanup()

    def _pending(self, *, content: object | None = None) -> tuple[dict, dict]:
        plan = contracts.make_plan(
            plan_id="candidate-plan",
            objective_id="objective-1",
            route="ordinary",
            state="candidate",
            content=content or {"steps": ["inspect admin-secret-value", "verify"]},
            created_at="2026-10-01T00:00:00Z",
        )
        disposition = contracts.make_plan_disposition(
            decision_id="decision-1",
            objective_id=plan["objective_id"],
            route=plan["route"],
            branch="candidate_review",
            reason="ROOT review is required",
            preserved_plan=plan,
            created_at="2026-10-01T00:00:01Z",
        )
        return self.state.record_plan_disposition(disposition), plan

    @staticmethod
    def _plan_expectations(disposition: dict, plan: dict) -> dict:
        return {
            "disposition_id": disposition["disposition_id"],
            "expected_disposition_digest": disposition["content_hash"],
            "expected_decision_id": disposition["decision_id"],
            "expected_objective_id": disposition["objective_id"],
            "expected_plan_id": plan["plan_id"],
            "expected_plan_digest": plan["content_hash"],
        }

    def _procedure(self, *, origin: str = "curated") -> dict:
        if origin == "generated":
            candidate = contracts.make_generated_skill_candidate(
                scope=self.scope, skill_id="generated-lock-repair",
                content="Inspect the lock and preserve evidence.",
                source_cases=[{
                    "case_id": "case-1", "case_receipt_id": "case-receipt-1",
                    "trajectory_id": "trajectory-1", "review_receipt_id": "review-1",
                    "review_receipt_digest": "review-digest-1",
                }],
                created_at="2026-10-01T00:00:00Z",
            )
            self.state.record_generated_skill_candidate(candidate)
            source_approval = contracts.make_skill_approval(
                approval_id="generated-source-approval", candidate=candidate, issuer="ROOT",
                recipients=(self.scope["owner"],), approved_at="2026-10-01T00:00:01Z",
                authority_evidence={"policy_id": "trusted-root/v1", "subject": "root-operator"},
            )
            self.state.record_skill_approval(source_approval)
            return procedures.TrustedProcedureService(
                self.state, trusted_issuers={"ROOT"}
            ).procedure_from_generated_skill(
                candidate_id=candidate["candidate_id"],
                skill_approval_id=source_approval["approval_id"],
                logical_name="repair-lock-generated",
                references=[{"id": "guide://lock", "content": "Preserve lock state."}],
                predicates={
                    "applicability": {}, "conflicts": {},
                    "capabilities": {}, "routes": {},
                },
            )
        return contracts.make_procedure_revision(
            logical_name="repair-lock-curated",
            origin="curated",
            origin_scope=self.scope,
            body="Inspect the lock and preserve evidence.",
            references=[{"id": "guide://lock", "content": "Preserve lock state."}],
            predicates={
                "applicability": {},
                "conflicts": {},
                "capabilities": {},
                "routes": {},
            },
            source={"kind": "curated_authoring", "provenance_ref": "source://curated/1"},
            created_at="2026-10-01T00:00:02Z",
        )

    def _approve(self, procedure: dict, *, approval_id: str = "approval-1") -> tuple[dict, dict]:
        result = self.admin.approve_procedure(
            procedure=procedure,
            expected_logical_id=procedure["logical_id"],
            expected_revision_id=procedure["revision_id"],
            expected_revision_digest=procedure["content_hash"],
            expected_origin=procedure["origin"],
            approval_id=approval_id,
            issuer="ROOT",
            recipients=[self.scope],
            authority_evidence={"policy_id": "trusted-root/v1", "subject": "root-operator"},
            approved_at="2026-10-01T00:00:03Z",
        )
        approval = self.state.get_procedure_approval(result["evidence"]["approval_id"])
        return result, approval

    def test_inspect_pending_plan_returns_sanitized_review_material_and_exact_identity(self) -> None:
        disposition, plan = self._pending()
        result = self.admin.inspect_pending_plan(
            disposition_id=disposition["disposition_id"],
            expected_disposition_digest=disposition["content_hash"],
            expected_decision_id="decision-1",
            expected_objective_id="objective-1",
        )
        self.assertEqual("pending_review", result["status"])
        self.assertEqual(plan["plan_id"], result["evidence"]["plan_id"])
        self.assertEqual(plan["content_hash"], result["evidence"]["plan_digest"])
        self.assertNotIn("admin-secret-value", repr(result))
        self.assertIn("[REDACTED]", repr(result["evidence"]["review_content"]))
        self.assertIn("correct", result["next_step"])

    def test_correction_then_acceptance_rejects_copied_or_stale_exact_identities(self) -> None:
        disposition, plan = self._pending(content={"steps": ["draft"]})
        exact = self._plan_expectations(disposition, plan)
        with self.assertRaises(memory_admin.MemoryAdminError) as copied:
            self.admin.correct_plan(
                **exact,
                corrected_plan_id=plan["plan_id"],
                corrected_content={"steps": ["changed under copied identity"]},
            )
        self.assertEqual("copied_identity", copied.exception.code)
        corrected = self.admin.correct_plan(
            **exact,
            corrected_plan_id="corrected-plan",
            corrected_content={"steps": ["inspect", "repair", "verify"]},
        )
        self.assertEqual("pending_review", corrected["status"])
        revised = self.state.get_plan_disposition(disposition["disposition_id"])
        revised_plan = revised["preserved_plan"]
        self.assertEqual("corrected-plan", revised_plan["plan_id"])
        self.assertEqual(plan["plan_id"], revised_plan["supersedes"])

        with self.assertRaises(memory_admin.MemoryAdminError) as stale:
            self.admin.correct_plan(
                **exact,
                corrected_plan_id="copied-plan",
                corrected_content={"steps": ["copied"]},
            )
        self.assertEqual("stale_identity", stale.exception.code)
        self.assertEqual("rejected", stale.exception.as_dict()["status"])

        accepted = self.admin.accept_plan(
            **self._plan_expectations(revised, revised_plan),
        )
        self.assertEqual("accepted", accepted["status"])
        self.assertEqual("ROOT", accepted["evidence"]["accepted_by"])
        self.assertEqual("corrected-plan", accepted["evidence"]["plan_id"])
        accepted_disposition = self.state.get_plan_disposition(disposition["disposition_id"])
        self.assertEqual(
            accepted["evidence"]["accepted_plan"],
            accepted_disposition["root_acceptance"]["accepted_plan"],
        )
        self.assertEqual(
            revised_plan["content_hash"],
            accepted_disposition["root_acceptance"]["source_plan_digest"],
        )
        with self.assertRaises(memory_admin.MemoryAdminError) as replay:
            self.admin.accept_plan(**self._plan_expectations(revised, revised_plan))
        self.assertEqual("stale_identity", replay.exception.code)

    def test_plan_with_configured_secret_can_be_inspected_redacted_but_not_accepted(self) -> None:
        disposition, plan = self._pending()
        with self.assertRaises(memory_admin.MemoryAdminError) as rejected:
            self.admin.accept_plan(**self._plan_expectations(disposition, plan))
        self.assertEqual("privacy_violation", rejected.exception.code)
        self.assertNotIn("admin-secret-value", repr(rejected.exception.as_dict()))
        self.assertIsNone(
            self.state.get_plan_disposition(disposition["disposition_id"])["root_acceptance"]
        )

    def test_propose_and_reject_create_distinct_valid_pending_plan_and_fresh_fallback(self) -> None:
        disposition, plan = self._pending(content={"steps": ["draft"]})
        proposed = self.admin.propose_plan(
            **self._plan_expectations(disposition, plan),
            proposed_plan_id="root-proposal-2",
            proposed_content={"steps": ["new draft"]},
        )
        self.assertEqual("pending_review", proposed["status"])
        revised = self.state.get_plan_disposition(disposition["disposition_id"])
        revised_plan = revised["preserved_plan"]
        self.assertEqual("root-proposal-2", revised_plan["plan_id"])
        self.assertEqual("candidate", revised_plan["state"])

        rejected = self.admin.reject_plan(
            **self._plan_expectations(revised, revised_plan),
            reason="missing rollback proof",
            fresh_plan_id="fresh-after-rejection",
        )
        self.assertEqual("rejected", rejected["status"])
        fresh_disposition = self.state.get_plan_disposition(disposition["disposition_id"])
        contracts.validate_plan(fresh_disposition["fresh"], expected_state="fresh")
        self.assertEqual("fresh-after-rejection", fresh_disposition["fresh"]["plan_id"])
        self.assertEqual(revised_plan["plan_id"], fresh_disposition["fresh"]["supersedes"])

    def test_store_plan_disposition_compare_and_swap_rejects_stale_digest_without_mutation(self) -> None:
        disposition, _plan = self._pending(content={"steps": ["draft"]})
        replacement = dict(disposition)
        replacement["reason"] = "new exact ROOT review state"
        replacement["content_hash"] = contracts.content_hash(replacement)
        with self.assertRaises(store.PlanDispositionConflictError):
            self.state.replace_plan_disposition(
                replacement, expected_content_hash="stale-disposition-digest"
            )
        self.assertEqual(
            disposition,
            self.state.get_plan_disposition(disposition["disposition_id"]),
        )

    def test_two_connections_that_pass_preflight_have_exactly_one_plan_correction_winner(self) -> None:
        disposition, plan = self._pending(content={"steps": ["draft"]})
        database = self.state.path
        barrier = threading.Barrier(2)

        def compete(suffix: str) -> tuple[str, dict]:
            connection = _BarrierMemoryStore(database, barrier)
            connection.initialize()
            snapshots = SnapshotService(authorizer=lambda *_: True)
            admin = memory_admin.MemoryAdmin(memory_admin.RootAdminContext(
                store=connection,
                scope=self.scope,
                trusted_issuers=("ROOT",),
                privacy_policy=privacy.PrivacyPolicy(),
                snapshots=snapshots,
            ))
            try:
                result = admin.correct_plan(
                    **self._plan_expectations(disposition, plan),
                    corrected_plan_id=f"corrected-{suffix}",
                    corrected_content={"steps": [f"winner candidate {suffix}"]},
                )
                return "winner", result
            except memory_admin.MemoryAdminError as exc:
                return "loser", exc.as_dict()
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(compete, ("a", "b")))
        winners = [record for outcome, record in results if outcome == "winner"]
        losers = [record for outcome, record in results if outcome == "loser"]
        self.assertEqual(1, len(winners), results)
        self.assertEqual(1, len(losers), results)
        self.assertEqual("rejected", losers[0]["status"])
        self.assertEqual("stale_identity", losers[0]["evidence"]["code"])
        durable = self.state.get_plan_disposition(disposition["disposition_id"])
        self.assertEqual(winners[0]["evidence"]["plan_id"], durable["preserved_plan"]["plan_id"])
        self.assertEqual(winners[0]["evidence"]["plan_digest"], durable["preserved_plan"]["content_hash"])

    def test_procedure_approval_and_designation_preserve_generated_origin(self) -> None:
        procedure = self._procedure(origin="generated")
        approved, approval = self._approve(procedure)
        self.assertEqual("generated", approved["evidence"]["origin"])
        durable = self.state.get_procedure_revision(procedure["revision_id"])
        self.assertEqual("generated", durable["origin"])

        partition = {
            "scope": "project",
            "application": self.scope["application"],
            "project": self.scope["project"],
            "namespace": self.scope["namespace"],
            "recipients": [self.scope],
        }
        designated = self.admin.designate_procedure(
            logical_id=procedure["logical_id"],
            revision_id=procedure["revision_id"],
            expected_revision_digest=procedure["content_hash"],
            approval_id=approval["approval_id"],
            expected_approval_digest=approval["content_hash"],
            expected_origin="generated",
            partition=partition,
            issuer="ROOT",
        )
        self.assertEqual("designated", designated["status"])
        self.assertEqual("generated", designated["evidence"]["origin"])
        current = self.state.get_current_procedure_designation(procedure["logical_id"], partition)
        self.assertEqual(designated["evidence"]["designation_id"], current["designation_id"])

    def test_representation_creation_is_canonical_bound_and_privacy_guarded(self) -> None:
        procedure = self._procedure()
        self._approve(procedure)
        limits = config.PreparationLimits()
        vector = [0.0] * limits.representation_dimensions
        vector[0] = 1.0

        represented = self.admin.create_procedure_representation(
            logical_id=procedure["logical_id"],
            revision_id=procedure["revision_id"],
            expected_revision_digest=procedure["content_hash"],
            expected_origin=procedure["origin"],
            search_text="inspect repair verify",
            vector=vector,
        )
        durable = self.state.get_procedure_representation(
            represented["evidence"]["representation_id"]
        )
        self.assertEqual(limits.representation_identity, {
            key: durable[key]
            for key in ("model", "dimensions", "metric", "sanitizer_version")
        })
        self.assertEqual(procedure["revision_id"], durable["revision_id"])

        with self.assertRaises(memory_admin.MemoryAdminError) as secret:
            self.admin.create_procedure_representation(
                logical_id=procedure["logical_id"],
                revision_id=procedure["revision_id"],
                expected_revision_digest=procedure["content_hash"],
                expected_origin=procedure["origin"],
                search_text="inspect admin-secret-value",
                vector=vector,
            )
        self.assertEqual("privacy_violation", secret.exception.code)

        with self.assertRaises(memory_admin.MemoryAdminError) as malformed:
            self.admin.create_procedure_representation(
                logical_id=procedure["logical_id"],
                revision_id=procedure["revision_id"],
                expected_revision_digest=procedure["content_hash"],
                expected_origin=procedure["origin"],
                search_text="inspect repair verify",
                vector=[1.0],
            )
        self.assertEqual("invalid_representation", malformed.exception.code)

    def test_procedure_and_effect_scope_are_exact_without_trusted_sharing_policy(self) -> None:
        foreign_scope = dict(self.scope, project="product-b", owner="another-root")
        foreign = contracts.make_procedure_revision(
            logical_name="foreign-lock-repair", origin="curated",
            origin_scope=foreign_scope, body="Inspect the foreign lock.",
            references=[], predicates={
                "applicability": {}, "conflicts": {}, "capabilities": {}, "routes": {},
            },
            source={"kind": "curated_authoring", "provenance_ref": "foreign://1"},
        )
        with self.assertRaises(memory_admin.MemoryAdminError) as foreign_origin:
            self.admin.approve_procedure(
                procedure=foreign, expected_logical_id=foreign["logical_id"],
                expected_revision_id=foreign["revision_id"],
                expected_revision_digest=foreign["content_hash"], expected_origin="curated",
                approval_id="foreign-approval", issuer="ROOT", recipients=[foreign_scope],
                authority_evidence={"policy_id": "trusted-root/v1"},
            )
        self.assertEqual("scope_denied", foreign_origin.exception.code)

        local = self._procedure()
        with self.assertRaises(memory_admin.MemoryAdminError) as foreign_recipient:
            self.admin.approve_procedure(
                procedure=local, expected_logical_id=local["logical_id"],
                expected_revision_id=local["revision_id"],
                expected_revision_digest=local["content_hash"], expected_origin="curated",
                approval_id="foreign-recipient", issuer="ROOT", recipients=[foreign_scope],
                authority_evidence={"policy_id": "trusted-root/v1"},
            )
        self.assertEqual("scope_denied", foreign_recipient.exception.code)

        operation, _ = self.state.create_external_effect_operation(
            kind="procedure_publication", scope_key="foreign", source_id=foreign["revision_id"],
            source_record=foreign, payload={"publication": "foreign"},
            captured_config=config.MemoryConfig(), current_config=config.MemoryConfig(),
        )
        with self.assertRaises(memory_admin.MemoryAdminError) as foreign_effect:
            self.admin.list_pending_effects()
        self.assertEqual("scope_denied", foreign_effect.exception.code)
        self.assertEqual("pending", self.state.get_effect_operation(operation["operation_id"])["status"])

    def test_shared_partition_requires_and_uses_context_bound_sharing_policy(self) -> None:
        procedure = self._procedure()
        _, approval = self._approve(procedure)
        shared_partition = {
            "scope": "shared",
            "application": self.scope["application"],
            "project": self.scope["project"],
            "namespace": self.scope["namespace"],
            "recipients": [self.scope],
        }
        with self.assertRaises(memory_admin.MemoryAdminError) as denied:
            self.admin.designate_procedure(
                logical_id=procedure["logical_id"], revision_id=procedure["revision_id"],
                expected_revision_digest=procedure["content_hash"],
                approval_id=approval["approval_id"],
                expected_approval_digest=approval["content_hash"], expected_origin="curated",
                partition=shared_partition, issuer="ROOT",
            )
        self.assertEqual("scope_denied", denied.exception.code)

        calls: list[tuple[str, dict, dict]] = []
        def allow_sharing(action: str, source: dict, target: dict) -> bool:
            calls.append((action, source, target))
            return source == self.scope and target == self.scope

        sharing_admin = memory_admin.MemoryAdmin(memory_admin.RootAdminContext(
            store=self.state, scope=self.scope, trusted_issuers=("ROOT",),
            privacy_policy=privacy.PrivacyPolicy(), snapshots=self.snapshots,
            sharing_policy=allow_sharing,
        ))
        result = sharing_admin.designate_procedure(
            logical_id=procedure["logical_id"], revision_id=procedure["revision_id"],
            expected_revision_digest=procedure["content_hash"],
            approval_id=approval["approval_id"],
            expected_approval_digest=approval["content_hash"], expected_origin="curated",
            partition=shared_partition, issuer="ROOT",
        )
        self.assertEqual("designated", result["status"])
        self.assertTrue(calls)

    def test_governed_publish_records_intent_and_lost_ack_retry_never_duplicates_write(self) -> None:
        procedure = self._procedure()
        _, approval = self._approve(procedure)
        partition = {
            "scope": "project",
            "application": self.scope["application"],
            "project": self.scope["project"],
            "namespace": self.scope["namespace"],
            "recipients": [self.scope],
        }
        designated = self.admin.designate_procedure(
            logical_id=procedure["logical_id"],
            revision_id=procedure["revision_id"],
            expected_revision_digest=procedure["content_hash"],
            approval_id=approval["approval_id"],
            expected_approval_digest=approval["content_hash"],
            expected_origin="curated",
            partition=partition,
            issuer="ROOT",
        )
        designation = self.state.get_procedure_designation(designated["evidence"]["designation_id"])
        representation = contracts.make_procedure_representation(
            procedure=procedure,
            model="test-embedding/v1",
            dimensions=2,
            metric="cosine",
            sanitizer_version="test/v1",
            search_text="repair lock",
            vector=[0.25, 0.75],
            created_at="2026-10-01T00:00:04Z",
        )
        self.state.record_procedure_representation(representation)
        adapter = _AdminAtlasAdapter()
        rich = config.configuration_record(config.resolve_config())
        claimant = {
            "schema": "external-effect-claimant/v1",
            "native_invocation_id": "admin-publish-1",
            "pid": 4242,
            "process_created_at": "created-admin-publish-1",
        }
        claimant["content_hash"] = contracts.content_hash(claimant)
        atlas_scope = {
            **adapter.location,
            "namespace": partition["namespace"],
            "recipients": partition["recipients"],
        }
        arguments = {
            "logical_id": procedure["logical_id"],
            "revision_id": procedure["revision_id"],
            "expected_revision_digest": procedure["content_hash"],
            "approval_id": approval["approval_id"],
            "expected_approval_digest": approval["content_hash"],
            "representation_id": representation["representation_id"],
            "expected_representation_digest": representation["content_hash"],
            "designation_id": designation["designation_id"],
            "expected_designation_digest": designation["content_hash"],
            "adapter": adapter,
            "captured_config": rich,
            "current_config": rich,
            "atlas_scope": atlas_scope,
            "claimant": claimant,
            "network_resolution": config.resolve_network_mode("normal"),
        }
        intent_observations: list[tuple[int, str]] = []
        def observe_intent() -> None:
            effects = [
                effect for effect in self.state.list_effect_operations()
                if effect["kind"] == "procedure_publication"
            ]
            intent_observations.append((len(effects), effects[0]["status"]))
        adapter.before_publication_write = observe_intent
        adapter.lose_publication_ack = True
        with self.assertRaises(memory_admin.MemoryAdminError) as ambiguous:
            self.admin.publish_procedure(**arguments)
        self.assertEqual("remote_ambiguous", ambiguous.exception.code)
        self.assertEqual(1, adapter.publication_writes)
        self.assertEqual([(1, "in_flight")], intent_observations)
        effects = [
            effect for effect in self.state.list_effect_operations()
            if effect["kind"] == "procedure_publication"
        ]
        self.assertEqual(1, len(effects))
        self.assertEqual("uncertain", effects[0]["status"])

        result = self.admin.publish_procedure(**arguments)
        self.assertEqual("published", result["status"])
        self.assertEqual(1, adapter.publication_writes)
        self.assertEqual(
            "confirmed", self.state.get_effect_operation(effects[0]["operation_id"])["status"]
        )

        legacy = dict(arguments, captured_config={
            key: value for key, value in rich.items()
            if key not in {"feature_states", "configuration_identity"}
        })
        with self.assertRaises(memory_admin.MemoryAdminError) as invalid_configuration:
            self.admin.publish_procedure(**legacy)
        self.assertEqual("invalid_configuration", invalid_configuration.exception.code)
        self.assertEqual(1, adapter.publication_writes)

        with self.assertRaises(memory_admin.MemoryAdminError) as stale:
            self.admin.publish_procedure(
                **dict(arguments, expected_designation_digest="copied-old-digest")
            )
        self.assertEqual("stale_identity", stale.exception.code)

    def test_withdraw_and_revoke_are_local_fail_closed_operations_when_network_is_restricted(self) -> None:
        procedure = self._procedure()
        _, approval = self._approve(procedure)
        partition = {
            "scope": "project",
            "application": self.scope["application"],
            "project": self.scope["project"],
            "namespace": self.scope["namespace"],
            "recipients": [self.scope],
        }
        designated = self.admin.designate_procedure(
            logical_id=procedure["logical_id"], revision_id=procedure["revision_id"],
            expected_revision_digest=procedure["content_hash"], approval_id=approval["approval_id"],
            expected_approval_digest=approval["content_hash"], expected_origin="curated",
            partition=partition, issuer="ROOT",
        )
        designation = self.state.get_procedure_designation(designated["evidence"]["designation_id"])
        restricted = config.resolve_network_mode("restricted_local")
        withdrawn = self.admin.withdraw_procedure(
            logical_id=procedure["logical_id"], partition=partition,
            expected_designation_id=designation["designation_id"],
            expected_revision_id=procedure["revision_id"],
            expected_generation=designation["generation"],
            expected_designation_digest=designation["content_hash"],
            issuer="ROOT", adapter=_UnusedAdapter(), network_resolution=restricted,
        )
        self.assertEqual("withdrawn_pending_remote", withdrawn["status"])
        revoked = self.admin.revoke_procedure(
            revision_id=procedure["revision_id"],
            expected_revision_digest=procedure["content_hash"],
            expected_origin="curated", issuer="ROOT", reason="superseded",
            adapter=_UnusedAdapter(), network_resolution=restricted,
        )
        self.assertEqual("revoked_pending_remote", revoked["status"])
        self.assertTrue(self.state.is_procedure_revoked(procedure["revision_id"]))

    def test_pending_effect_listing_and_exact_reconciliation_do_not_expose_payloads(self) -> None:
        procedure = self._procedure()
        operation, _ = self.state.create_external_effect_operation(
            kind="procedure_publication",
            scope_key="recipient-1",
            source_id=procedure["revision_id"],
            source_record=procedure,
            payload={"secret": "admin-secret-value", "publication": "exact"},
            captured_config=config.MemoryConfig(),
            current_config=config.MemoryConfig(),
        )
        claimant = {
            "schema": "external-effect-claimant/v1",
            "native_invocation_id": "native-admin-1",
            "pid": 1234,
            "process_created_at": "created-1",
        }
        claimant["content_hash"] = contracts.content_hash(claimant)
        claimed = self.state.claim_effect_operation(
            operation["operation_id"], current_config=config.MemoryConfig(), claimant=claimant,
        )
        listed = self.admin.list_pending_effects()
        self.assertEqual("pending_effects", listed["status"])
        self.assertEqual([operation["operation_id"]], [item["operation_id"] for item in listed["evidence"]["effects"]])
        self.assertNotIn("payload_record", repr(listed))
        self.assertNotIn("admin-secret-value", repr(listed))

        evidence = {key: claimed[key] for key in (
            "operation_id", "kind", "scope_key", "source_digest", "payload_digest",
            "configuration_digest",
        )}
        evidence["adapter_proof"] = {"remote_id": "remote-1", "digest": "exact"}
        reconciled = self.admin.reconcile_effect(
            operation_id=claimed["operation_id"],
            expected_status="in_flight",
            expected_version=claimed["version"],
            expected_kind=claimed["kind"],
            expected_source_digest=claimed["source_digest"],
            evidence=evidence,
            result="acknowledged",
            claim_id=claimed["active_claim_id"],
        )
        self.assertEqual("reconciled", reconciled["status"])
        self.assertEqual("confirmed", reconciled["evidence"]["effect_status"])
        with self.assertRaises(memory_admin.MemoryAdminError) as stale:
            self.admin.reconcile_effect(
                operation_id=claimed["operation_id"], expected_status="in_flight",
                expected_version=claimed["version"], expected_kind=claimed["kind"],
                expected_source_digest=claimed["source_digest"], evidence=evidence,
                result="acknowledged", claim_id=claimed["active_claim_id"],
            )
        self.assertEqual("stale_identity", stale.exception.code)

    def test_snapshot_export_readiness_and_import_round_trip(self) -> None:
        artifact = self.root / "snapshot"
        exported = self.admin.export_snapshot(
            artifact=artifact,
            credential="operator",
            dependencies=[{"capability": "experience", "reference": "ready-ref"}],
        )
        self.assertEqual("exported", exported["status"])
        snapshot_digest = exported["evidence"]["snapshot_digest"]
        readiness = self.admin.snapshot_readiness(
            artifact=artifact,
            expected_snapshot_digest=snapshot_digest,
            credential="operator",
        )
        self.assertEqual("ready", readiness["status"])
        self.assertTrue(readiness["evidence"]["capabilities"]["experience"])
        self.assertNotIn(str(self.state.path), repr(readiness))

        target = store.MemoryStore(self.root / "restored" / "memory.sqlite3")
        try:
            restored = self.admin.import_snapshot(
                artifact=artifact,
                target=target,
                credential="operator",
                expected_snapshot_digest=snapshot_digest,
                required_capabilities=("local", "experience"),
            )
            self.assertEqual("imported", restored["status"])
            self.assertTrue(restored["evidence"]["capabilities"]["local"])
            self.assertTrue(restored["evidence"]["capabilities"]["experience"])
            target.initialize()
            self.assertEqual([], target.list_effect_operations())
        finally:
            target.close()

    def test_snapshot_readiness_revalidates_database_bytes_not_only_manifest(self) -> None:
        for damage in ("delete", "tamper"):
            with self.subTest(damage=damage):
                artifact = self.root / f"snapshot-{damage}"
                exported = self.admin.export_snapshot(
                    artifact=artifact, credential="operator"
                )
                database = artifact / "state.sqlite3"
                if damage == "delete":
                    database.unlink()
                else:
                    with database.open("ab") as handle:
                        handle.write(b"tampered")
                with self.assertRaises(memory_admin.MemoryAdminError) as invalid:
                    self.admin.snapshot_readiness(
                        artifact=artifact,
                        expected_snapshot_digest=exported["evidence"]["snapshot_digest"],
                        credential="operator",
                    )
                self.assertEqual("invalid_snapshot", invalid.exception.code)

    def test_snapshot_readiness_revalidates_schema_dependencies_and_pending_facts(self) -> None:
        for damage in ("schema", "dependencies", "pending"):
            with self.subTest(damage=damage):
                artifact = self.root / f"snapshot-structure-{damage}"
                exported = self.admin.export_snapshot(
                    artifact=artifact, credential="operator"
                )
                manifest_path = artifact / "manifest.json"
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                if damage == "schema":
                    connection = sqlite3.connect(artifact / "state.sqlite3")
                    try:
                        # Force the synthetic schema mutation into the main
                        # file rather than leaving it in a WAL sidecar that a
                        # deliberately immutable inspection must ignore.
                        connection.execute("PRAGMA journal_mode=DELETE")
                        connection.execute("CREATE TABLE injected_state(value TEXT)")
                        connection.commit()
                    finally:
                        connection.close()
                    manifest["database_sha256"] = hashlib.sha256(
                        (artifact / "state.sqlite3").read_bytes()
                    ).hexdigest()
                elif damage == "dependencies":
                    manifest["dependencies"] = [
                        {"capability": "unknown", "reference": "forged", "ready": True}
                    ]
                else:
                    manifest["pending_effects"] = {"forged-operation": "pending"}
                manifest["content_hash"] = contracts.content_hash(manifest)
                manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                with self.assertRaises(memory_admin.MemoryAdminError) as invalid:
                    self.admin.snapshot_readiness(
                        artifact=artifact,
                        expected_snapshot_digest=manifest["content_hash"],
                        credential="operator",
                    )
                self.assertEqual("invalid_snapshot", invalid.exception.code)
                self.assertNotEqual(
                    exported["evidence"]["snapshot_digest"], manifest["content_hash"]
                )

    def test_context_rejects_noncanonical_scope_or_uninitialized_store(self) -> None:
        extra = dict(self.scope, worker_selected_store="/tmp/forged")
        with self.assertRaises(memory_admin.MemoryAdminError) as invalid_scope:
            memory_admin.RootAdminContext(
                store=self.state, scope=extra, trusted_issuers=("ROOT",),
                privacy_policy=privacy.PrivacyPolicy(), snapshots=self.snapshots,
            )
        self.assertEqual("invalid_context", invalid_scope.exception.code)

        closed = store.MemoryStore(self.root / "closed.sqlite3")
        with self.assertRaises(memory_admin.MemoryAdminError) as uninitialized:
            memory_admin.RootAdminContext(
                store=closed, scope=self.scope, trusted_issuers=("ROOT",),
                privacy_policy=privacy.PrivacyPolicy(), snapshots=self.snapshots,
            )
        self.assertEqual("invalid_context", uninitialized.exception.code)


if __name__ == "__main__":
    unittest.main()
