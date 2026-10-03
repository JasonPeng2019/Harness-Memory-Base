from __future__ import annotations

import contextlib
import io
import json
import unittest
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType
from unittest.mock import patch

from memory_harness import atlas, config, contracts, procedures, store, templates

from orchestrator_harness import (
    memory_commands,
    operator_launch,
    product_composition,
    resume,
)
from orchestrator_harness.core import content_hash, read_json
from orchestrator_harness.memory_product_config import load_memory_product_config
from orchestrator_harness.product_composition import central_store_path
from orchestrator_harness.tests.support import configure_local_memory_product, write_json
from orchestrator_harness.bootstrap import _remove_owned_tree
from orchestrator_harness.tests.test_step04_launch_boundary import (
    LaunchBoundaryFixture,
    candidate_card,
)


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


class _OperatorAtlasAdapter(atlas.AtlasProcedureAdapter):
    def __init__(self) -> None:
        super().__init__(
            collection=_AtlasCollection(),
            vector_store=object(),
            location={
                "database": "operator_test",
                "collection": "trusted_procedures",
                "index": "procedure_vector",
            },
        )


class MemoryOperatorCLITests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = LaunchBoundaryFixture()
        self.addCleanup(self._cleanup_fixture)
        self.secret = "operator-cli-secret-value"
        configured_scope = {
            "application": "test-application",
            "namespace": "test-namespace",
            "project": "test-project",
            # Deliberately differs from the former hard-coded CLI issuer.
            "owner": "configured-root-owner",
        }
        self.scope = configure_local_memory_product(
            self.fixture.harness,
            store_root=self.fixture.root / "operator-memory",
            scope=configured_scope,
            known_secrets=(self.secret,),
        )

    def _cleanup_fixture(self) -> None:
        _remove_owned_tree(self.fixture.root, timeout_seconds=5.0)
        self.fixture.close()

    @property
    def product(self):
        return load_memory_product_config(self.fixture.harness)

    @property
    def expected_config(self) -> list[str]:
        configured = self.product
        return [
            "--expected-config-digest", configured.config_digest,
            "--expected-policy-generation", configured.policy_generation,
        ]

    def dispatch(self, argv: list[str]) -> dict:
        with (
            patch.object(memory_commands, "find_harness_root", return_value=self.fixture.harness),
            patch.object(
                memory_commands,
                "find_active_lane",
                side_effect=self.fixture._read_active_lane,
            ),
            patch.object(resume, "find_harness_root", return_value=self.fixture.harness),
            patch.object(
                resume, "find_active_lane", side_effect=self.fixture._read_active_lane
            ),
        ):
            parsed = operator_launch._build_parser().parse_args(argv)
            return operator_launch._dispatch(parsed)

    def central(self) -> store.MemoryStore:
        memory = store.MemoryStore(central_store_path(self.product))
        memory.initialize()
        return memory

    @staticmethod
    def one_disposition(memory: store.MemoryStore) -> dict:
        assert memory.connection is not None
        row = memory.connection.execute(
            "SELECT record FROM plan_dispositions"
        ).fetchone()
        assert row is not None
        return json.loads(row[0])

    @staticmethod
    def pending_plan(disposition: dict) -> dict:
        field = {
            "candidate_review": "preserved_plan",
            "direct_fill": "proposal",
            "apc_proposal": "proposal",
            "fresh": "fresh",
        }[disposition["branch"]]
        return disposition[field]

    def plan_argv(
        self, action: str, lane: dict, disposition: dict, plan: dict, extra: list[str]
    ) -> list[str]:
        return [
            "memory", "plan", action,
            "--lane-id", lane["lane_id"],
            "--expected-run-id", lane["run_id"],
            "--expected-task-card-digest", read_json(
                Path(lane["worktree_path"]) / ".agent-workspace" / "task-card.json"
            )["content_hash"],
            "--disposition-id", disposition["disposition_id"],
            "--expected-disposition-digest", disposition["content_hash"],
            "--expected-decision-id", disposition["decision_id"],
            "--expected-objective-id", disposition["objective_id"],
            "--expected-plan-id", plan["plan_id"],
            "--expected-plan-digest", plan["content_hash"],
            *extra,
        ]

    def make_procedure(self, *, origin: str, suffix: str) -> dict:
        if origin == "curated":
            return contracts.make_procedure_revision(
                logical_name=f"repair-{suffix}",
                origin="curated",
                origin_scope=self.scope,
                body="Inspect, repair, and verify.",
                references=[],
                predicates={
                    "applicability": {}, "conflicts": {},
                    "capabilities": {}, "routes": {},
                },
                source={
                    "kind": "curated_authoring",
                    "provenance_ref": f"source://{suffix}",
                },
            )
        memory = self.central()
        try:
            candidate = contracts.make_generated_skill_candidate(
                scope=self.scope,
                skill_id=f"generated-{suffix}",
                content="Inspect, repair, and verify.",
                source_cases=[{
                    "case_id": f"case-{suffix}",
                    "case_receipt_id": f"case-receipt-{suffix}",
                    "trajectory_id": f"trajectory-{suffix}",
                    "review_receipt_id": f"review-{suffix}",
                    "review_receipt_digest": f"review-digest-{suffix}",
                }],
            )
            memory.record_generated_skill_candidate(candidate)
            source_approval = contracts.make_skill_approval(
                approval_id=f"source-approval-{suffix}",
                candidate=candidate,
                issuer=self.scope["owner"],
                recipients=(self.scope["owner"],),
                authority_evidence={"policy_id": "local-root-authority/v1"},
                approved_at="2026-10-02T00:00:00Z",
            )
            memory.record_skill_approval(source_approval)
            return procedures.TrustedProcedureService(
                memory, trusted_issuers={self.scope["owner"]}
            ).procedure_from_generated_skill(
                candidate_id=candidate["candidate_id"],
                skill_approval_id=source_approval["approval_id"],
                logical_name=f"repair-generated-{suffix}",
                references=[],
                predicates={
                    "applicability": {}, "conflicts": {},
                    "capabilities": {}, "routes": {},
                },
            )
        finally:
            memory.close()

    def approve_represent_and_designate(
        self, procedure: dict, suffix: str
    ) -> tuple[dict, dict, dict]:
        approved = self.dispatch([
            "memory", "procedure", "approve", *self.expected_config,
            "--procedure-json", json.dumps(procedure),
            "--expected-logical-id", procedure["logical_id"],
            "--expected-revision-id", procedure["revision_id"],
            "--expected-revision-digest", procedure["content_hash"],
            "--expected-origin", procedure["origin"],
            "--approval-id", f"approval-{suffix}",
        ])
        self.assertTrue(approved["ok"], approved)
        represented = self.dispatch([
            "memory", "procedure", "represent", *self.expected_config,
            "--logical-id", procedure["logical_id"],
            "--revision-id", procedure["revision_id"],
            "--expected-revision-digest", procedure["content_hash"],
            "--expected-origin", procedure["origin"],
            "--search-text", "inspect repair verify",
        ])
        self.assertTrue(represented["ok"], represented)
        designated = self.dispatch([
            "memory", "procedure", "designate", *self.expected_config,
            "--logical-id", procedure["logical_id"],
            "--revision-id", procedure["revision_id"],
            "--expected-revision-digest", procedure["content_hash"],
            "--approval-id", approved["evidence"]["approval_id"],
            "--expected-approval-digest", approved["evidence"]["approval_digest"],
            "--expected-origin", procedure["origin"],
        ])
        self.assertTrue(designated["ok"], designated)
        return approved, represented, designated

    def test_parser_json_contract_and_no_authority_path_options(self) -> None:
        parsed = operator_launch._build_parser().parse_args(
            ["memory", "config", "status"]
        )
        self.assertEqual(("memory", "config", "status"), (
            parsed.command, parsed.memory_command, parsed.config_command
        ))
        for forbidden in ("--store-path", "--config-path", "--worktree"):
            with self.subTest(forbidden=forbidden), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    operator_launch._build_parser().parse_args(
                        ["memory", "config", "status", forbidden, "/tmp/forged"]
                    )
        output = io.StringIO()
        with (
            patch.object(memory_commands, "find_harness_root", return_value=self.fixture.harness),
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(0, operator_launch.main(["--json", "memory", "config", "status"]))
        emitted = json.loads(output.getvalue())
        self.assertEqual("memory-admin-result/v1", emitted["schema"])
        self.assertNotIn(self.secret, output.getvalue())
        self.assertNotIn(str(self.product.store_root), output.getvalue())

    def test_pending_corrected_accepted_becomes_dispatchable_and_stale_loses(self) -> None:
        card, original_plan = candidate_card(objective="operator-cli-plan")
        card["memory_scope"] = dict(self.scope)
        card["content_hash"] = content_hash(card)
        pending, worktree = self.fixture.run_bootstrap(lane_id="memory-cli", card=card)
        self.assertFalse(pending["ok"], pending)
        lane = self.fixture.lane_record("memory-cli")
        memory = self.fixture.open_store(worktree)
        disposition = self.one_disposition(memory)
        memory.close()

        inspected = self.dispatch([
            "memory", "plan", "inspect",
            "--lane-id", lane["lane_id"],
            "--expected-run-id", lane["run_id"],
            "--expected-task-card-digest", read_json(
                Path(lane["worktree_path"]) / ".agent-workspace" / "task-card.json"
            )["content_hash"],
            "--disposition-id", disposition["disposition_id"],
            "--expected-disposition-digest", disposition["content_hash"],
            "--expected-decision-id", disposition["decision_id"],
            "--expected-objective-id", disposition["objective_id"],
        ])
        self.assertEqual("pending_review", inspected["status"])
        proposed = self.dispatch(self.plan_argv(
            "propose", lane, disposition, original_plan,
            ["--new-plan-id", "operator-proposal", "--content-json", '{"steps":["draft","verify"]}'],
        ))
        self.assertEqual("pending_review", proposed["status"])
        stale = self.dispatch(self.plan_argv(
            "correct", lane, disposition, original_plan,
            ["--new-plan-id", "stale-copy", "--content-json", '{"steps":["wrong"]}'],
        ))
        self.assertFalse(stale["ok"])
        self.assertEqual("stale_identity", stale["evidence"]["code"])

        memory = self.fixture.open_store(worktree)
        proposed_disposition = memory.get_plan_disposition(disposition["disposition_id"])
        proposed_plan = self.pending_plan(proposed_disposition)
        memory.close()
        corrected = self.dispatch(self.plan_argv(
            "correct", lane, proposed_disposition, proposed_plan,
            ["--new-plan-id", "operator-corrected", "--content-json", '{"steps":["fix","verify"]}'],
        ))
        self.assertEqual("pending_review", corrected["status"])

        memory = self.fixture.open_store(worktree)
        revised = memory.get_plan_disposition(disposition["disposition_id"])
        revised_plan = self.pending_plan(revised)
        memory.close()
        accepted = self.dispatch(self.plan_argv(
            "accept", lane, revised, revised_plan, ["--checkpoint", "root-checkpoint-1"]
        ))
        self.assertTrue(accepted["ok"], accepted)
        self.assertEqual("accepted_dispatchable", accepted["status"])
        durable_lane = self.fixture.lane_record("memory-cli")
        self.assertTrue(durable_lane["dispatchable"])
        self.assertEqual("execution_accepted", durable_lane["memory_plan_state"])
        durable_card = read_json(worktree / ".agent-workspace" / "task-card.json")
        self.assertEqual("root-checkpoint-1", durable_card["memory_handoff"]["checkpoint"])
        self.assertEqual(
            accepted["evidence"]["plan_digest"],
            durable_card["memory_handoff"]["plan"]["content_hash"],
        )

    def test_rejection_keeps_lane_nondispatchable(self) -> None:
        card, plan = candidate_card(objective="operator-cli-reject")
        card["memory_scope"] = dict(self.scope)
        card["content_hash"] = content_hash(card)
        _, worktree = self.fixture.run_bootstrap(lane_id="memory-reject", card=card)
        lane = self.fixture.lane_record("memory-reject")
        memory = self.fixture.open_store(worktree)
        disposition = self.one_disposition(memory)
        memory.close()
        rejected = self.dispatch(self.plan_argv(
            "reject", lane, disposition, plan,
            ["--reason", "rollback evidence is missing", "--fresh-plan-id", "fresh-root-plan"],
        ))
        self.assertTrue(rejected["ok"], rejected)
        self.assertEqual("rejected", rejected["status"])
        self.assertFalse(self.fixture.lane_record("memory-reject")["dispatchable"])
        durable = read_json(worktree / ".agent-workspace" / "task-card.json")
        self.assertEqual("candidate_review", durable["memory_handoff"]["plan_state"])
        self.assertEqual("fresh-root-plan", durable["memory_handoff"]["plan"]["plan_id"])

    def test_curated_and_generated_governance_and_truthful_remote_unavailable(self) -> None:
        curated = self.make_procedure(origin="curated", suffix="curated")
        approved, represented, designated = self.approve_represent_and_designate(
            curated, "curated"
        )
        self.assertEqual(self.scope["owner"], approved["evidence"]["issuer"])
        self.assertEqual(
            curated["revision_id"], represented["evidence"]["revision_id"]
        )
        published = self.dispatch([
            "memory", "procedure", "publish", *self.expected_config,
            "--logical-id", curated["logical_id"],
            "--revision-id", curated["revision_id"],
            "--expected-revision-digest", curated["content_hash"],
            "--approval-id", approved["evidence"]["approval_id"],
            "--expected-approval-digest", approved["evidence"]["approval_digest"],
            "--representation-id", represented["evidence"]["representation_id"],
            "--expected-representation-digest", represented["evidence"]["representation_digest"],
            "--designation-id", designated["evidence"]["designation_id"],
            "--expected-designation-digest", designated["evidence"]["designation_digest"],
        ])
        self.assertFalse(published["ok"])
        self.assertEqual("atlas_unavailable", published["evidence"]["code"])

        withdrawn = self.dispatch([
            "memory", "procedure", "withdraw", *self.expected_config,
            "--logical-id", curated["logical_id"],
            "--expected-designation-id", designated["evidence"]["designation_id"],
            "--expected-revision-id", curated["revision_id"],
            "--expected-generation", str(designated["evidence"]["generation"]),
            "--expected-designation-digest", designated["evidence"]["designation_digest"],
        ])
        self.assertIn(withdrawn["status"], {"withdrawn", "withdrawn_pending_remote"})
        revoked = self.dispatch([
            "memory", "procedure", "revoke", *self.expected_config,
            "--revision-id", curated["revision_id"],
            "--expected-revision-digest", curated["content_hash"],
            "--expected-origin", "curated", "--reason", "superseded",
        ])
        self.assertIn(revoked["status"], {"revoked", "revoked_pending_remote"})

        generated = self.make_procedure(origin="generated", suffix="generated")
        generated_approved, generated_represented, generated_designated = self.approve_represent_and_designate(
            generated, "generated"
        )
        self.assertEqual("generated", generated_approved["evidence"]["origin"])
        self.assertEqual(
            generated["revision_id"], generated_represented["evidence"]["revision_id"]
        )
        self.assertEqual("generated", generated_designated["evidence"]["origin"])

    def test_cli_governed_procedure_is_searchable_by_normal_composition(self) -> None:
        procedure = self.make_procedure(origin="curated", suffix="searchable")
        approved, represented, designated = self.approve_represent_and_designate(
            procedure, "searchable"
        )
        self.assertEqual(self.scope["owner"], approved["evidence"]["issuer"])

        card, _ = candidate_card(objective="searchable-procedure")
        card["memory_scope"] = dict(self.scope)
        card["content_hash"] = content_hash(card)
        with product_composition.compose_product(
            harness_root=self.fixture.harness,
            task_card=card,
            route="ordinary",
        ) as composed:
            self.assertEqual((self.scope["owner"],), tuple(composed.procedure_service.trusted_issuers))
            curated_store = next(
                item
                for item in composed.search_stores
                if item.store_id == "local-curated-procedures"
            )
            objective = templates.objective_representation(
                "inspect repair verify", route="ordinary"
            )
            payload = {
                "representation": {
                    key: objective[key]
                    for key in ("model", "dimensions", "metric", "sanitizer_version")
                },
                "tokens": objective["tokens"],
                "route": "ordinary",
            }
            candidates = list(curated_store.query(payload))

        self.assertEqual([procedure["revision_id"]], [item["revision_id"] for item in candidates])
        self.assertEqual(
            represented["evidence"]["representation_id"],
            candidates[0]["representation"]["representation_id"],
        )
        self.assertEqual(
            designated["evidence"]["designation_id"],
            candidates[0]["payload"]["provenance"]["designation_id"],
        )

    def test_cli_full_available_atlas_lifecycle_uses_created_representation(self) -> None:
        root, harness_configuration, configured = self._configuration_tuple()
        services = dict(configured.services)
        services["atlas"] = replace(
            services["atlas"],
            enabled=True,
            available=True,
            reason="enabled prerequisites available",
            credential_env="OPERATOR_TEST_ATLAS_URI",
            database="operator_test",
            collection="trusted_procedures",
            index="procedure_vector",
        )
        available = replace(configured, services=MappingProxyType(services))
        adapter = _OperatorAtlasAdapter()

        with (
            patch.object(
                memory_commands,
                "_configuration",
                return_value=(root, harness_configuration, available),
            ),
            patch.object(product_composition, "_atlas_adapter", return_value=adapter),
        ):
            for origin in ("curated", "generated"):
                with self.subTest(origin=origin):
                    suffix = f"remote-lifecycle-{origin}"
                    procedure = self.make_procedure(origin=origin, suffix=suffix)
                    approved, represented, designated = self.approve_represent_and_designate(
                        procedure, suffix
                    )
                    published = self.dispatch([
                        "memory", "procedure", "publish", *self.expected_config,
                        "--logical-id", procedure["logical_id"],
                        "--revision-id", procedure["revision_id"],
                        "--expected-revision-digest", procedure["content_hash"],
                        "--approval-id", approved["evidence"]["approval_id"],
                        "--expected-approval-digest", approved["evidence"]["approval_digest"],
                        "--representation-id", represented["evidence"]["representation_id"],
                        "--expected-representation-digest", represented["evidence"]["representation_digest"],
                        "--designation-id", designated["evidence"]["designation_id"],
                        "--expected-designation-digest", designated["evidence"]["designation_digest"],
                    ])
                    self.assertTrue(published["ok"], published)
                    self.assertEqual("published", published["status"])

                    withdrawn = self.dispatch([
                        "memory", "procedure", "withdraw", *self.expected_config,
                        "--logical-id", procedure["logical_id"],
                        "--expected-designation-id", designated["evidence"]["designation_id"],
                        "--expected-revision-id", procedure["revision_id"],
                        "--expected-generation", str(designated["evidence"]["generation"]),
                        "--expected-designation-digest", designated["evidence"]["designation_digest"],
                    ])
                    self.assertTrue(withdrawn["ok"], withdrawn)
                    self.assertEqual("withdrawn", withdrawn["status"])

                    revoked = self.dispatch([
                        "memory", "procedure", "revoke", *self.expected_config,
                        "--revision-id", procedure["revision_id"],
                        "--expected-revision-digest", procedure["content_hash"],
                        "--expected-origin", origin,
                        "--reason", "superseded",
                    ])
                    self.assertTrue(revoked["ok"], revoked)
                    self.assertEqual("revoked", revoked["status"])

    def _configuration_tuple(self):
        with patch.object(
            memory_commands, "find_harness_root", return_value=self.fixture.harness
        ):
            return memory_commands._configuration()

    def test_pending_effect_list_and_exact_reconcile(self) -> None:
        procedure = self.make_procedure(origin="curated", suffix="effect")
        memory = self.central()
        try:
            operation, _ = memory.create_external_effect_operation(
                kind="procedure_publication",
                scope_key="project",
                source_id=procedure["revision_id"],
                source_record=procedure,
                payload={"publication": "exact"},
                captured_config=config.MemoryConfig(),
                current_config=config.MemoryConfig(),
            )
            claimant = {
                "schema": "external-effect-claimant/v1",
                "native_invocation_id": "memory-operator-test",
                "pid": 1234,
                "process_created_at": "created-memory-operator-test",
            }
            claimant["content_hash"] = contracts.content_hash(claimant)
            claimed = memory.claim_effect_operation(
                operation["operation_id"],
                current_config=config.MemoryConfig(),
                claimant=claimant,
            )
        finally:
            memory.close()
        listed = self.dispatch(["memory", "effects", "list"])
        self.assertTrue(listed["ok"], listed)
        self.assertEqual(1, listed["evidence"]["count"])
        self.assertNotIn("payload_record", repr(listed))
        evidence = {key: claimed[key] for key in (
            "operation_id", "kind", "scope_key", "source_digest",
            "payload_digest", "configuration_digest",
        )}
        evidence["adapter_proof"] = {"remote_id": "exact-remote-result"}
        reconciled = self.dispatch([
            "memory", "effects", "reconcile", *self.expected_config,
            "--operation-id", claimed["operation_id"],
            "--expected-status", claimed["status"],
            "--expected-version", str(claimed["version"]),
            "--expected-kind", claimed["kind"],
            "--expected-source-digest", claimed["source_digest"],
            "--evidence-json", json.dumps(evidence),
            "--result", "acknowledged",
            "--claim-id", claimed["active_claim_id"],
        ])
        self.assertTrue(reconciled["ok"], reconciled)
        self.assertEqual("confirmed", reconciled["evidence"]["effect_status"])

    def test_snapshot_round_trip_readiness_and_tamper(self) -> None:
        exported = self.dispatch([
            "memory", "snapshot", "export", *self.expected_config,
            "--snapshot-id", "operator-snapshot-1",
        ])
        self.assertTrue(exported["ok"], exported)
        digest = exported["evidence"]["snapshot_digest"]
        readiness = self.dispatch([
            "memory", "snapshot", "readiness", *self.expected_config,
            "--snapshot-id", "operator-snapshot-1",
            "--expected-snapshot-digest", digest,
        ])
        self.assertEqual("ready", readiness["status"])
        imported = self.dispatch([
            "memory", "snapshot", "import", *self.expected_config,
            "--snapshot-id", "operator-snapshot-1",
            "--expected-snapshot-digest", digest,
            "--restore-id", "operator-restore-1",
        ])
        self.assertTrue(imported["ok"], imported)

        database = self.product.store_root / "snapshots" / "operator-snapshot-1" / "state.sqlite3"
        with database.open("ab") as handle:
            handle.write(b"tampered")
        tampered = self.dispatch([
            "memory", "snapshot", "readiness", *self.expected_config,
            "--snapshot-id", "operator-snapshot-1",
            "--expected-snapshot-digest", digest,
        ])
        self.assertFalse(tampered["ok"])
        self.assertEqual("invalid_snapshot", tampered["evidence"]["code"])

    def test_secret_state_init_rotation_and_stale_generation(self) -> None:
        configured = self.product
        (configured.store_root / ".memory-product-secret-state.json").unlink()
        (configured.store_root / ".memory-product-hmac.key").unlink()
        attention = self.dispatch(["memory", "config", "status"])
        self.assertEqual("attention_required", attention["status"])
        initialized = self.dispatch([
            "memory", "config", "init-secret-state", *self.expected_config
        ])
        self.assertTrue(initialized["ok"], initialized)

        config_path = self.fixture.harness / "memory-product-config.json"
        record = read_json(config_path)
        record["policy_generation"] = "test-generation-2"
        write_json(config_path, record)
        current = load_memory_product_config(self.fixture.harness)
        rotated = self.dispatch([
            "memory", "config", "rotate-secret-state",
            "--expected-config-digest", current.config_digest,
            "--expected-policy-generation", current.policy_generation,
            "--expected-prior-policy-generation", "test-generation-1",
        ])
        self.assertTrue(rotated["ok"], rotated)
        stale = self.dispatch([
            "memory", "config", "rotate-secret-state",
            "--expected-config-digest", current.config_digest,
            "--expected-policy-generation", current.policy_generation,
            "--expected-prior-policy-generation", "copied-generation",
        ])
        self.assertFalse(stale["ok"])
        self.assertEqual("stale_identity", stale["evidence"]["code"])


if __name__ == "__main__":
    unittest.main()
