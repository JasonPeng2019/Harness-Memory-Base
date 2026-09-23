"""STEP-04: local trusted procedures as a real bounded SearchStore input.

These tests pin the narrow slice that turns the already accepted local
trusted-procedure lifecycle into real ``search.SearchStore`` inputs for
``preparation.PreparationService``:

- a current, exactly approved, recipient- and predicate-compatible local
  curated/builtin procedure supplies bounded guidance, and a generated-origin
  revision needs both its retained Step-02 candidate/skill/source-case/review
  provenance and its own explicit Step-03 procedure approval and current
  designation; raw local skill presence and a Step-02 generated-skill approval
  alone are never delivery approval;
- stale, revoked, altered, cross-scope, unreviewed-source, and incomparable
  local records fail closed without suppressing an unrelated eligible
  procedure;
- the read-only exact-record lookup runs on the bounded worker thread without
  sharing or weakening the accepted thread-affine store handle;
- the source-specific preparation gate suppresses the generated-origin source
  query when ``generated_skill_use`` is off while curated local procedures stay
  available, keeps local guidance available when Atlas shared retrieval is off
  or the network mode is ``restricted_local``, and never begins the Atlas call;
- one logical revision visible locally and in Atlas deduplicates under the
  existing search rules without a ranking bonus or a second delivery.
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from memory_harness import (
    atlas,
    atlas_adapters,
    config,
    contracts,
    local_procedure_adapters,
    preparation,
    privacy,
    procedures,
    search,
    store,
    templates,
)

ROOT_REPLAN = {
    "requested_by": "ROOT",
    "reason": "the tests exercise the explicit ROOT replan path",
}

CURATED_STORE_ID = "local-curated-procedures"
GENERATED_STORE_ID = "local-generated-procedures"


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.value = float(start)

    def __call__(self) -> float:
        return self.value


class RecordingMemoryStore(store.MemoryStore):
    """Record the exact thread that performs the local procedure lookup."""

    def __init__(self, path) -> None:
        super().__init__(path)
        self.procedure_reads: list[int] = []

    def read_local_current_procedures(self, partition, *, receiver=None):
        self.procedure_reads.append(threading.get_ident())
        return super().read_local_current_procedures(partition, receiver=receiver)


class _WriteResult:
    def __init__(self, matched_count: int) -> None:
        self.matched_count = matched_count


class _Collection:
    """Deterministic exact-read collection double (no live Atlas service)."""

    def __init__(self) -> None:
        self.documents: dict[str, dict] = {}
        self.exact_reads: list[str] = []

    def find_one(self, filter: dict) -> dict | None:
        identifier = filter.get("_id")
        if isinstance(identifier, str):
            self.exact_reads.append(identifier)
            document = self.documents.get(identifier)
            return dict(document) if document is not None else None
        return None

    def insert_one(self, document: dict) -> object:
        identifier = document["_id"]
        if identifier in self.documents:
            raise RuntimeError("duplicate key")
        self.documents[identifier] = dict(document)
        return object()

    def replace_one(self, filter: dict, replacement: dict, upsert: bool = False) -> _WriteResult:
        identifier = filter.get("_id")
        current = self.documents.get(identifier)
        if current is None:
            return _WriteResult(0)
        if any(current.get(key) != value for key, value in filter.items()):
            return _WriteResult(0)
        self.documents[identifier] = dict(replacement)
        return _WriteResult(1)


class _Document:
    def __init__(self, publication_id: str) -> None:
        self.id = publication_id
        self.metadata = {"_id": publication_id}


class _VectorStore:
    """Deterministic Vector Search double; records every discovery call."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.publication_ids: list[str] = []

    def similarity_search_with_score(
        self, query: str, **kwargs: object
    ) -> list[tuple[_Document, float]]:
        self.calls.append({"query": query, **kwargs})
        return [(_Document(publication_id), 0.99) for publication_id in self.publication_ids]


class Step04LocalProcedureTrustTests(unittest.TestCase):
    """The local trusted-procedure trust path, gates, and duplicate policy."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.clock = FakeClock()
        self.limits = config.resolve_limits(
            {
                "default_deadline_seconds": 300.0,
                "execution_reserve_seconds": 60.0,
                "store_seconds": 8.0,
                "minimum_optional_slice_seconds": 1.0,
            }
        )
        self.policy = privacy.PrivacyPolicy()
        self.memory_store = RecordingMemoryStore(self.root / "memory-state.sqlite3")
        self.memory_store.initialize()
        self.procedure_service = procedures.TrustedProcedureService(
            self.memory_store, trusted_issuers={"ROOT"}, privacy_policy=self.policy
        )
        self.service = preparation.PreparationService(
            store=self.memory_store, limits=self.limits, clock=self.clock
        )
        self.receiver = {
            "application": "harness",
            "project": "product-a",
            "namespace": "local-procedure-trust",
            "owner": "root-agent",
        }
        self.partition = {
            "scope": "project",
            "application": self.receiver["application"],
            "project": self.receiver["project"],
            "namespace": self.receiver["namespace"],
            "recipients": [dict(self.receiver)],
        }
        self.identity = templates.representation_identity(limits=self.limits)
        self.card = contracts.make_task_card(
            task="Fix the parser lock regression in the focused test",
            base_commit="base-1",
        )
        self.candidate = contracts.make_plan(
            plan_id="candidate-plan",
            objective_id="parser-lock-repair",
            route="ordinary",
            state="candidate",
            content={"steps": ["draft"]},
        )
        self.collection = _Collection()
        self.vector_store = _VectorStore()
        self.atlas_adapter = atlas.AtlasProcedureAdapter(
            collection=self.collection, vector_store=self.vector_store
        )

    def tearDown(self) -> None:
        self.memory_store.close()
        self.temporary.cleanup()

    # -- helpers -----------------------------------------------------------

    def _curated(
        self,
        logical_name: str,
        *,
        origin: str = "curated",
        body: str = "Inspect the local lock before network recovery.",
        predicates=None,
        origin_scope=None,
    ) -> dict:
        return contracts.make_procedure_revision(
            logical_name=logical_name,
            origin=origin,
            origin_scope=origin_scope or self.receiver,
            body=body,
            references=[{"id": "guide://lock", "content": "Preserve the lock invariant."}],
            predicates=predicates
            or {
                "applicability": {},
                "conflicts": {},
                "capabilities": {},
                "routes": {
                    "all": [{"field": "route", "operator": "equals", "value": "ordinary"}]
                },
            },
            source={
                "kind": "curated_authoring",
                "provenance_ref": f"curation://root/{logical_name}",
            },
            created_at="2026-09-21T00:00:00Z",
        )

    def _approve(self, procedure: dict, *, approval_id: str, recipients=None) -> dict:
        approval = contracts.make_procedure_approval(
            approval_id=approval_id,
            procedure=procedure,
            issuer="ROOT",
            recipients=recipients or [dict(self.receiver)],
            authority_evidence={"policy_id": "trusted-root/v1", "subject": "root-operator"},
            approved_at="2026-09-21T00:00:01Z",
        )
        return self.procedure_service.record_approved_revision(procedure, approval)

    def _representation(self, procedure: dict, *, search_text: str, identity=None) -> dict:
        selected = identity or self.identity
        return contracts.make_procedure_representation(
            procedure=procedure,
            model=selected["model"],
            dimensions=selected["dimensions"],
            metric=selected["metric"],
            sanitizer_version=selected["sanitizer_version"],
            search_text=search_text,
            vector=[0.01] * int(selected["dimensions"]),
            created_at="2026-09-21T00:00:02Z",
        )

    def _designate(self, procedure: dict, approval: dict, *, partition=None):
        return self.procedure_service.designate(
            procedure=procedure,
            approval=approval,
            partition=partition or self.partition,
            issuer="ROOT",
        )

    def _deliverable_curated(
        self,
        logical_name: str = "curated-parser-lock",
        *,
        search_text: str = "parser lock regression focused inspection recovery",
        predicates=None,
    ) -> tuple[dict, dict, dict, dict]:
        procedure = self._curated(logical_name, predicates=predicates)
        approval = self._approve(procedure, approval_id=f"approval-{logical_name}")
        representation = self._representation(procedure, search_text=search_text)
        self.procedure_service.record_representation(representation)
        designation = self._designate(procedure, approval)
        return procedure, approval, representation, designation

    def _durable_source_case(self, suffix: str) -> dict:
        """Build one genuinely reviewed, durably receipted Step-02 source case.

        Retained generated provenance must rejoin real reviewed experience: a
        durable terminal outcome, review receipt, reviewed trajectory, a
        confirmed experience ingestion, and the exact case receipt of that
        trajectory.  Invented identifiers are never durable evidence.
        """

        plan = contracts.make_plan(
            plan_id=f"generated-source-plan-{suffix}",
            objective_id=f"generated-source-objective-{suffix}",
            route="ordinary",
            state="accepted",
            content={"steps": ["inspect", "verify"]},
            accepted_by="ROOT",
        )
        card = contracts.make_task_card(
            task=f"Review the generated source case {suffix}",
            base_commit="base-1",
            memory_handoff=contracts.make_memory_handoff(
                objective_id=plan["objective_id"], route="ordinary", plan=plan
            ),
        )
        decision = contracts.make_decision(card, plan)
        self.memory_store.record_decision(decision)
        outcome = contracts.make_outcome(
            decision_id=decision["decision_id"],
            plan_id=plan["plan_id"],
            plan_digest=plan["content_hash"],
            status="PASS",
            evidence_digest=f"generated-source-evidence-{suffix}",
            linked_run_id=f"generated-source-run-{suffix}",
            task_card_digest=card["content_hash"],
            objective_id=plan["objective_id"],
        )
        self.memory_store.record_outcome(outcome)
        review = contracts.make_review_receipt(
            review_id=f"generated-source-review-{suffix}",
            outcome=outcome,
            decision=decision,
            task_card=card,
            plan=plan,
            reviewed_by="ROOT",
            evidence_refs=(f"review://generated-source/{suffix}",),
            raw_evidence="The generated source behavior passed its focused check.",
        )
        self.memory_store.record_review_receipt(review)
        trajectory = contracts.make_reviewed_trajectory(
            task_card=card,
            plan=plan,
            decision=decision,
            outcome=outcome,
            review_receipt=review,
            scope=self.receiver,
        )
        self.memory_store.record_reviewed_trajectory(trajectory)
        ingestion = contracts.make_experience_ingestion(
            trajectory=trajectory,
            destination="everos",
            session_id=f"generated-source-session-{suffix}",
            payload_digest=f"generated-source-payload-{suffix}",
        )
        self.memory_store.create_experience_ingestion(ingestion)
        case_receipt = contracts.make_case_receipt(
            trajectory=trajectory,
            ingestion=ingestion,
            source_case={
                "id": f"generated-source-case-{suffix}",
                "session_id": ingestion["session_id"],
                "agent_id": self.receiver["owner"],
                "app_id": self.receiver["application"],
                "project_id": self.receiver["project"],
            },
        )
        self.memory_store.confirm_experience_ingestion(
            ingestion["ingestion_id"], [case_receipt], expected_version=0
        )
        return {
            "case_id": case_receipt["case_id"],
            "case_receipt_id": case_receipt["case_receipt_id"],
            "trajectory_id": case_receipt["trajectory_id"],
            "review_receipt_id": case_receipt["review_receipt_id"],
            "review_receipt_digest": case_receipt["review_receipt_digest"],
        }

    def _generated_provenance(self, suffix: str) -> tuple[dict, dict, dict]:
        candidate = contracts.make_generated_skill_candidate(
            scope=self.receiver,
            skill_id=f"generated-parser-lock-{suffix}",
            content="Inspect the generated parser lock evidence before recovery.",
            source_cases=[self._durable_source_case(suffix)],
            metadata={"source": "synthetic"},
            created_at="2026-09-21T00:00:08Z",
        )
        self.memory_store.record_generated_skill_candidate(candidate)
        source_approval = contracts.make_skill_approval(
            approval_id=f"generated-source-approval-{suffix}",
            candidate=candidate,
            issuer="ROOT",
            recipients=(self.receiver["owner"],),
            approved_at="2026-09-21T00:00:09Z",
            authority_evidence={"policy_id": "step02-test/v1", "subject": "root"},
        )
        self.memory_store.record_skill_approval(source_approval)
        procedure = self.procedure_service.procedure_from_generated_skill(
            candidate_id=candidate["candidate_id"],
            skill_approval_id=source_approval["approval_id"],
            logical_name=f"generated-parser-lock-{suffix}",
            references=[{"id": "guide://generated", "content": "Generated source is historical."}],
            predicates={
                "applicability": {},
                "conflicts": {},
                "capabilities": {},
                "routes": {
                    "all": [{"field": "route", "operator": "equals", "value": "ordinary"}]
                },
            },
        )
        return candidate, source_approval, procedure

    def _deliverable_generated(self, suffix: str = "one") -> tuple[dict, dict, dict]:
        candidate, source_approval, procedure = self._generated_provenance(suffix)
        approval = self._approve(procedure, approval_id=f"generated-approval-{suffix}")
        representation = self._representation(
            procedure,
            search_text="generated parser lock evidence recovery inspection",
        )
        self.procedure_service.record_representation(representation)
        self._designate(procedure, approval)
        return candidate, source_approval, procedure

    def _payload(self, text: str = "parser lock regression focused inspection", *, route: str = "ordinary", identity=None) -> dict:
        objective = templates.objective_representation(text, route=route, limits=self.limits)
        representation = {
            key: objective[key]
            for key in ("model", "dimensions", "metric", "sanitizer_version")
        }
        if identity is not None:
            representation = dict(identity)
        return privacy.safe_query_payload(
            {
                "representation": representation,
                "tokens": list(objective["tokens"])[:32],
                "route": route,
            },
            self.policy,
        )

    def _stores(self, *, receiver=None, facts=None, route: str = "ordinary", memory_store=None):
        return local_procedure_adapters.make_local_procedure_search_stores(
            procedure_service=self.procedure_service,
            memory_store=memory_store or self.memory_store,
            receiver=receiver or self.receiver,
            facts=facts if facts is not None else {"language": "python"},
            route=route,
            limits=self.limits,
        )

    def _by_id(self, stores, store_id: str):
        return next(item for item in stores if item.store_id == store_id)

    def _prepare(self, stores, **overrides):
        arguments = {
            "task_card": self.card,
            "plan": self.candidate,
            "objective_id": "parser-lock-repair",
            "route": "ordinary",
            "stores": list(stores),
            "root_replan": ROOT_REPLAN,
        }
        arguments.update(overrides)
        return self.service.prepare(**arguments)

    @staticmethod
    def _attempts(outcome):
        return {entry["store_id"]: entry for entry in outcome.trace["attempts"]}

    def _procedures(self, outcome):
        return [item for item in outcome.trace["candidates"] if item["kind"] == "procedure"]

    def _selected(self, outcome):
        return [
            item for item in self._procedures(outcome) if item["disposition"] == "selected"
        ]

    def _rejected(self, outcome):
        return {
            item["logical_id"]: item
            for item in self._procedures(outcome)
            if item["disposition"] != "selected"
        }

    def _mutate(self, statement: str, parameters: tuple) -> None:
        writer = sqlite3.connect(str(self.memory_store.path))
        try:
            writer.execute(statement, parameters)
            writer.commit()
        finally:
            writer.close()

    def _atlas_store(self, *, receiver=None, facts=None, route: str = "ordinary"):
        return atlas_adapters.make_atlas_search_store(
            procedure_service=self.procedure_service,
            adapter=self.atlas_adapter,
            receiver=receiver or self.receiver,
            facts=facts if facts is not None else {"language": "python"},
            route=route,
            limits=self.limits,
        )


    # -- the valid current local delivery ----------------------------------

    def test_current_curated_local_procedure_supplies_bounded_guidance(self) -> None:
        procedure, approval, representation, designation = self._deliverable_curated()
        curated = self._by_id(self._stores(), CURATED_STORE_ID)
        self.assertEqual("procedure", curated.kind)
        self.assertEqual("local", curated.specificity)
        self.assertFalse(curated.requires_network)
        self.assertEqual(self.receiver, dict(curated.scope))

        candidates = list(curated.query(self._payload()))
        self.assertEqual(1, len(candidates))
        candidate = candidates[0]
        self.assertEqual(procedure["logical_id"], candidate["logical_id"])
        self.assertEqual(procedure["revision_id"], candidate["revision_id"])
        self.assertEqual(CURATED_STORE_ID, candidate["origin"])
        self.assertEqual(CURATED_STORE_ID, candidate["source_id"])
        self.assertEqual("local", candidate["specificity"])
        self.assertEqual("approved", candidate["approval_status"])
        self.assertEqual("current", candidate["designation"])
        self.assertTrue(candidate["predicates_ok"])
        self.assertEqual(
            contracts.sha256_hex(candidate["payload"]), candidate["payload_digest"]
        )
        body = candidate["payload"]
        self.assertEqual(procedure, body["procedure"])
        self.assertEqual(approval, body["approval"])
        self.assertEqual(designation, body["designation"])
        self.assertEqual(representation["representation_id"], body["provenance"]["representation_id"])
        self.assertEqual(designation["designation_id"], body["provenance"]["designation_id"])
        self.assertEqual("local_trusted_procedure", body["provenance"]["authority"])
        self.assertEqual(CURATED_STORE_ID, body["provenance"]["store_id"])
        self.assertGreater(candidate["score"], 0.0)

        outcome = self._prepare([curated], root_replan=None)
        attempts = self._attempts(outcome)
        self.assertEqual("completed", attempts[CURATED_STORE_ID]["status"])
        selected = self._selected(outcome)
        self.assertEqual(1, len(selected))
        record = selected[0]
        self.assertEqual(procedure["logical_id"], record["logical_id"])
        self.assertEqual(procedure["revision_id"], record["revision_id"])
        self.assertEqual(CURATED_STORE_ID, record["origin"])
        self.assertEqual(
            {CURATED_STORE_ID}, {item["store_id"] for item in record["provenance"]}
        )
        self.assertEqual("optional_memory", outcome.trace["outcome"])
        # The pending candidate plan keeps its exact identity and state.
        self.assertEqual("candidate_review", outcome.disposition["branch"])
        self.assertEqual("candidate", outcome.plan["state"])

    def test_generated_local_procedure_requires_step02_provenance(self) -> None:
        candidate, source_approval, procedure = self._deliverable_generated("two")
        generated = self._by_id(self._stores(), GENERATED_STORE_ID)
        self.assertEqual("generated_local_procedure", generated.source_kind)

        outcome = self._prepare([generated])
        attempts = self._attempts(outcome)
        self.assertEqual("completed", attempts[GENERATED_STORE_ID]["status"])
        selected = self._selected(outcome)
        self.assertEqual(1, len(selected))
        record = selected[0]
        self.assertEqual(procedure["logical_id"], record["logical_id"])
        self.assertEqual(GENERATED_STORE_ID, record["origin"])
        body = record["payload"]
        self.assertEqual(
            {
                "kind": "generated_skill",
                "candidate_id": candidate["candidate_id"],
                "candidate_digest": candidate["content_hash"],
                "skill_approval_id": source_approval["approval_id"],
                "skill_approval_digest": source_approval["content_hash"],
                "source_cases": [dict(item) for item in candidate["source_cases"]],
            },
            body["source"],
        )
        self.assertEqual(
            [dict(item) for item in candidate["source_cases"]],
            body["source_cases"],
        )
        # The generated-origin revision is never served by the curated family.
        curated = self._by_id(self._stores(), CURATED_STORE_ID)
        self.assertEqual([], list(curated.query(self._payload())))

    def test_step02_skill_approval_alone_is_not_delivery_approval(self) -> None:
        candidate, source_approval, procedure = self._generated_provenance("three")
        # Only the durable Step-02 candidate and its skill approval exist: no
        # trusted procedure approval and no current designation.
        generated = self._by_id(self._stores(), GENERATED_STORE_ID)
        self.assertEqual([], list(generated.query(self._payload())))

        outcome = self._prepare([self._by_id(self._stores(), GENERATED_STORE_ID)])
        self.assertEqual([], self._selected(outcome))
        self.assertEqual("no_optional_memory", outcome.trace["outcome"])

        # An unrelated trusted approval still does not authorize this revision.
        other, other_approval, _representation, _designation = self._deliverable_curated(
            "some-other-lock"
        )
        self.assertEqual([], list(generated.query(self._payload())))
        # ... and the unrelated eligible procedure still delivers.
        curated = self._by_id(self._stores(), CURATED_STORE_ID)
        self.assertEqual(
            [other["logical_id"]], [item["logical_id"] for item in curated.query(self._payload())]
        )

    # -- fail-closed eligibility -------------------------------------------

    def test_recipient_scope_mismatch_omits_only_that_procedure(self) -> None:
        eligible, _approval, _representation, _designation = self._deliverable_curated(
            "curated-scope-one"
        )
        other_receiver = dict(self.receiver, owner="another-agent")
        other_partition = {
            "scope": "project",
            "application": other_receiver["application"],
            "project": other_receiver["project"],
            "namespace": other_receiver["namespace"],
            "recipients": [dict(other_receiver)],
        }
        foreign = self._curated("curated-foreign-scope")
        foreign_approval = self._approve(
            foreign, approval_id="approval-foreign", recipients=[dict(other_receiver)]
        )
        self.procedure_service.record_representation(
            self._representation(
                foreign, search_text="parser lock regression focused inspection"
            )
        )
        self._designate(foreign, foreign_approval, partition=other_partition)

        curated = self._by_id(self._stores(), CURATED_STORE_ID)
        self.assertEqual(
            [eligible["logical_id"]],
            [item["logical_id"] for item in curated.query(self._payload())],
        )
        outcome = self._prepare([curated])
        self.assertEqual(
            [eligible["logical_id"]],
            [item["logical_id"] for item in self._selected(outcome)],
        )
        # A candidate omitted before search has no contractual rejection
        # record: it must simply stay absent from the trace and delivery.
        traced = {item["logical_id"] for item in self._procedures(outcome)}
        self.assertNotIn(foreign["logical_id"], traced)

    def test_multi_recipient_project_and_shared_partitions_authorize_receiver(self) -> None:
        teammate = dict(self.receiver, owner="teammate-agent")
        cross_project = {
            "application": self.receiver["application"],
            "project": "product-b",
            "namespace": self.receiver["namespace"],
            "owner": "remote-agent",
        }

        project_procedure = self._curated("curated-multi-project")
        project_approval = self._approve(
            project_procedure,
            approval_id="approval-multi-project",
            recipients=[dict(self.receiver), teammate],
        )
        self.procedure_service.record_representation(
            self._representation(
                project_procedure,
                search_text="parser lock regression focused inspection recovery",
            )
        )
        project_partition = {
            "scope": "project",
            "application": self.receiver["application"],
            "project": self.receiver["project"],
            "namespace": self.receiver["namespace"],
            "recipients": [dict(self.receiver), teammate],
        }
        self._designate(project_procedure, project_approval, partition=project_partition)

        shared_procedure = self._curated("curated-multi-shared")
        shared_approval = self._approve(
            shared_procedure,
            approval_id="approval-multi-shared",
            recipients=[dict(self.receiver), cross_project],
        )
        self.procedure_service.record_representation(
            self._representation(
                shared_procedure,
                search_text="parser lock regression focused inspection recovery",
            )
        )
        shared_partition = {
            "scope": "shared",
            "application": self.receiver["application"],
            "project": self.receiver["project"],
            "namespace": self.receiver["namespace"],
            "recipients": [dict(self.receiver), cross_project],
        }
        self._designate(shared_procedure, shared_approval, partition=shared_partition)

        curated = self._by_id(self._stores(), CURATED_STORE_ID)
        candidates = curated.query(self._payload())
        by_id = {item["logical_id"]: item for item in candidates}
        self.assertIn(project_procedure["logical_id"], by_id)
        self.assertIn(shared_procedure["logical_id"], by_id)
        # A partition that also names other recipients keeps its exact stored
        # identity; the receiver is still named by that durable partition.
        stored_project = by_id[project_procedure["logical_id"]]["payload"]["partition"]
        self.assertEqual(
            contracts.normalize_procedure_partition(project_partition), stored_project
        )
        self.assertIn(
            contracts.canonical_json(dict(self.receiver)).decode("utf-8"),
            {
                contracts.canonical_json(item).decode("utf-8")
                for item in stored_project["recipients"]
            },
        )
        stored_shared = by_id[shared_procedure["logical_id"]]["payload"]["partition"]
        self.assertEqual(
            contracts.normalize_procedure_partition(shared_partition), stored_shared
        )

        outcome = self._prepare([curated])
        self.assertEqual(
            {project_procedure["logical_id"], shared_procedure["logical_id"]},
            {item["logical_id"] for item in self._selected(outcome)},
        )

    def test_predicate_and_conflict_mismatch_fails_closed(self) -> None:
        wrong_route = self._curated(
            "curated-wrong-route",
            predicates={
                "applicability": {},
                "conflicts": {},
                "capabilities": {},
                "routes": {
                    "all": [{"field": "route", "operator": "equals", "value": "deeper"}]
                },
            },
        )
        approval = self._approve(wrong_route, approval_id="approval-wrong-route")
        self.procedure_service.record_representation(
            self._representation(
                wrong_route, search_text="parser lock regression focused inspection"
            )
        )
        self._designate(wrong_route, approval)

        conflicted = self._curated(
            "curated-conflict",
            predicates={
                "applicability": {},
                "conflicts": {
                    "all": [{"field": "language", "operator": "equals", "value": "python"}]
                },
                "capabilities": {},
                "routes": {},
            },
        )
        conflicted_approval = self._approve(conflicted, approval_id="approval-conflict")
        self.procedure_service.record_representation(
            self._representation(
                conflicted, search_text="parser lock regression focused inspection"
            )
        )
        self._designate(conflicted, conflicted_approval)

        eligible, _approval, _representation, _designation = self._deliverable_curated(
            "curated-eligible"
        )
        outcome = self._prepare([self._by_id(self._stores(), CURATED_STORE_ID)])
        self.assertEqual(
            [eligible["logical_id"]],
            [item["logical_id"] for item in self._selected(outcome)],
        )
        traced = {item["logical_id"] for item in self._procedures(outcome)}
        self.assertNotIn(wrong_route["logical_id"], traced)
        self.assertNotIn(conflicted["logical_id"], traced)

    def test_revoked_and_stale_designation_records_fail_closed(self) -> None:
        revoked, _approval, _representation, _designation = self._deliverable_curated(
            "curated-revoked"
        )
        self.memory_store.record_procedure_revocation(
            contracts.make_procedure_revocation(
                procedure=revoked, issuer="ROOT", reason="synthetic local revocation"
            )
        )
        stale = self._curated("curated-stale")
        stale_approval = self._approve(stale, approval_id="approval-stale")
        self.procedure_service.record_representation(
            self._representation(
                stale, search_text="parser lock regression focused inspection"
            )
        )
        self._designate(stale, stale_approval)
        # The durable current row must describe its exact designation; a row
        # that points at another generation is omitted, never delivered.
        self._mutate(
            "UPDATE procedure_current_designations SET generation = generation + 7 "
            "WHERE logical_id = ?",
            (stale["logical_id"],),
        )
        healthy, _approval2, _representation2, _designation2 = self._deliverable_curated(
            "curated-healthy"
        )

        outcome = self._prepare([self._by_id(self._stores(), CURATED_STORE_ID)])
        self.assertEqual(
            [healthy["logical_id"]],
            [item["logical_id"] for item in self._selected(outcome)],
        )
        traced = {item["logical_id"] for item in self._procedures(outcome)}
        self.assertIn(healthy["logical_id"], traced)
        self.assertNotIn(revoked["logical_id"], traced)
        self.assertNotIn(stale["logical_id"], traced)

    def test_missing_or_changed_step02_source_receipt_omits_generated_guidance(self) -> None:
        candidate, source_approval, procedure = self._deliverable_generated("four")
        generated = self._by_id(self._stores(), GENERATED_STORE_ID)
        self.assertEqual(
            [procedure["logical_id"]],
            [item["logical_id"] for item in generated.query(self._payload())],
        )

        # A durable Step-02 skill approval whose stored content no longer
        # matches the retained provenance is never delivered on partial trust.
        self._mutate(
            "UPDATE generated_skill_approvals SET content_hash = ? WHERE approval_id = ?",
            ("tampered-digest", source_approval["approval_id"]),
        )
        self.assertEqual([], list(generated.query(self._payload())))

        # Removing the durable source case receipt is equally decisive.
        self._mutate(
            "DELETE FROM experience_case_receipts WHERE case_id = ?",
            (candidate["source_cases"][0]["case_id"],),
        )
        self.assertEqual([], list(generated.query(self._payload())))
        outcome = self._prepare([generated])
        self.assertEqual([], self._selected(outcome))
        self.assertEqual("no_optional_memory", outcome.trace["outcome"])

    def test_incomparable_representation_is_omitted_but_other_records_deliver(self) -> None:
        other_identity = {
            "model": "other-embedding/v2",
            "dimensions": self.identity["dimensions"],
            "metric": self.identity["metric"],
            "sanitizer_version": self.identity["sanitizer_version"],
        }
        foreign = self._curated("curated-other-identity")
        approval = self._approve(foreign, approval_id="approval-other-identity")
        self.procedure_service.record_representation(
            self._representation(
                foreign,
                search_text="parser lock regression focused inspection",
                identity=other_identity,
            )
        )
        self._designate(foreign, approval)
        eligible, _approval, _representation, _designation = self._deliverable_curated(
            "curated-comparable"
        )

        outcome = self._prepare([self._by_id(self._stores(), CURATED_STORE_ID)])
        self.assertEqual(
            [eligible["logical_id"]],
            [item["logical_id"] for item in self._selected(outcome)],
        )
        # A candidate omitted before search has no contractual rejection
        # record: it must simply stay absent from the trace and delivery.
        traced = {item["logical_id"] for item in self._procedures(outcome)}
        self.assertNotIn(foreign["logical_id"], traced)

    # -- the bounded worker-thread read ------------------------------------

    def test_read_only_lookup_runs_on_the_bounded_worker_thread(self) -> None:
        procedure, _approval, _representation, _designation = self._deliverable_curated(
            "curated-thread"
        )
        reads_before = len(self.memory_store.procedure_reads)
        outcome = self._prepare([self._by_id(self._stores(), CURATED_STORE_ID)])
        self.assertEqual("completed", self._attempts(outcome)[CURATED_STORE_ID]["status"])
        reads = self.memory_store.procedure_reads[reads_before:]
        self.assertTrue(reads, "the local lookup really ran")
        self.assertTrue(
            all(thread != threading.get_ident() for thread in reads),
            "the durable read must run on the bounded worker, not the caller thread",
        )
        self.assertEqual(
            1,
            len(set(reads)),
            "one bounded worker thread serves every local read of the attempt",
        )
        # The accepted shared store handle stays usable after the worker read.
        self.assertEqual(
            procedure["revision_id"],
            self.memory_store.get_procedure_revision(procedure["revision_id"])["revision_id"],
        )

    # -- the source-specific preparation gate -------------------------------

    def test_generated_skill_use_off_suppresses_generated_source_query_only(self) -> None:
        self._deliverable_generated("five")
        eligible, _approval, _representation, _designation = self._deliverable_curated(
            "curated-gate"
        )
        stores = self._stores()
        curated = self._by_id(stores, CURATED_STORE_ID)
        generated = self._by_id(stores, GENERATED_STORE_ID)

        queries: list[str] = []
        original = generated.query

        def recording(payload):
            queries.append("generated")
            return original(payload)

        observed = search.SearchStore(
            store_id=generated.store_id,
            kind=generated.kind,
            query=recording,
            scope=generated.scope,
            freshness=generated.freshness,
            specificity=generated.specificity,
            requires_network=generated.requires_network,
            source_kind=generated.source_kind,
        )

        outcome = self._prepare(
            [curated, observed],
            request={"generated_skill_use": False, "atlas_shared_retrieval": False},
        )
        attempts = self._attempts(outcome)
        self.assertEqual("disabled", attempts[GENERATED_STORE_ID]["status"])
        self.assertEqual("completed", attempts[CURATED_STORE_ID]["status"])
        self.assertEqual([], queries, "the generated-origin source makes no query at all")
        self.assertEqual(
            [eligible["logical_id"]],
            [item["logical_id"] for item in self._selected(outcome)],
        )

    def test_atlas_off_or_restricted_local_keeps_local_delivery_and_no_atlas_call(self) -> None:
        eligible, _approval, _representation, _designation = self._deliverable_curated(
            "curated-local-only"
        )
        curated = self._by_id(self._stores(), CURATED_STORE_ID)
        atlas_store = self._atlas_store()
        calls_before = len(self.vector_store.calls)

        outcome = self._prepare(
            [curated, atlas_store],
            request={"atlas_shared_retrieval": False, "generated_skill_use": False},
            network_mode="restricted_local",
        )
        attempts = self._attempts(outcome)
        self.assertEqual("disabled", attempts["atlas-shared-procedures"]["status"])
        # With atlas_shared_retrieval already off the resolved configuration
        # disables the store first; the accepted reason stays truthful.
        self.assertIn(
            "disables", attempts["atlas-shared-procedures"]["reason"]
        )
        self.assertEqual(calls_before, len(self.vector_store.calls))
        self.assertEqual("completed", attempts[CURATED_STORE_ID]["status"])
        self.assertEqual(
            [eligible["logical_id"]],
            [item["logical_id"] for item in self._selected(outcome)],
        )

        # Even with Atlas enabled, restricted_local still begins no remote call
        # while the curated local procedure keeps working.
        second = self._prepare(
            [self._by_id(self._stores(), CURATED_STORE_ID), self._atlas_store()],
            network_mode="restricted_local",
        )
        second_attempts = self._attempts(second)
        self.assertEqual("disabled", second_attempts["atlas-shared-procedures"]["status"])
        # An enabled Atlas store is suppressed *before* its call by
        # restricted-local mode, and the reason says so.
        self.assertIn("restricted-local", second_attempts["atlas-shared-procedures"]["reason"])
        self.assertEqual(calls_before, len(self.vector_store.calls))
        self.assertTrue(self._selected(second))

    # -- one logical revision across local and Atlas -----------------------

    def test_local_and_atlas_duplicate_revision_delivers_once_without_bonus(self) -> None:
        procedure, approval, representation, designation = self._deliverable_curated(
            "curated-duplicate"
        )
        self.procedure_service.publish_designation(designation, self.atlas_adapter)
        publication = self.procedure_service.publish(
            procedure=procedure,
            approval=approval,
            representation=representation,
            designation=designation,
            adapter=self.atlas_adapter,
        )
        self.vector_store.publication_ids.append(publication["publication_id"])

        curated = self._by_id(self._stores(), CURATED_STORE_ID)
        atlas_store = self._atlas_store()
        self.assertEqual(
            [procedure["logical_id"]],
            [item["logical_id"] for item in atlas_store.query(self._payload())],
        )

        outcome = self._prepare([curated, atlas_store])
        selected = self._selected(outcome)
        self.assertEqual(1, len(selected), "one logical revision is delivered once")
        record = selected[0]
        self.assertEqual(procedure["logical_id"], record["logical_id"])
        self.assertEqual(procedure["revision_id"], record["revision_id"])
        self.assertEqual(
            {CURATED_STORE_ID, "atlas-shared-procedures"},
            {item["store_id"] for item in record["provenance"]},
        )
        # No ranking bonus: the local sighting is the narrower authorized
        # scope, so the accepted dedup policy elects it, and the merged record
        # keeps exactly the local single-sighting selection instead of gaining
        # anything from the remote duplicate.
        local_only = curated.query(self._payload())[0]
        atlas_only = atlas_store.query(self._payload())[0]
        elected = min((local_only, atlas_only), key=search._sighting_rank)
        self.assertEqual("local", elected["specificity"])
        local_single = self._selected(self._prepare([curated]))
        atlas_single = self._selected(self._prepare([atlas_store]))
        self.assertEqual(1, len(local_single))
        self.assertEqual(1, len(atlas_single))
        self.assertEqual(local_single[0]["score"], record["score"])
        self.assertEqual(local_single[0]["payload_digest"], record["payload_digest"])
        self.assertEqual(CURATED_STORE_ID, record["origin"])
        self.assertEqual(CURATED_STORE_ID, record["source_id"])
        self.assertLessEqual(
            record["score"], max(local_single[0]["score"], atlas_single[0]["score"])
        )
        self.assertEqual(
            {CURATED_STORE_ID, "atlas-shared-procedures"},
            {item["store_id"] for item in record["provenance"]},
        )


if __name__ == "__main__":
    unittest.main()
