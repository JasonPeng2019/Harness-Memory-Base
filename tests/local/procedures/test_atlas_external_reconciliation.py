"""Deterministic exact-read and lost-acknowledgement procedure publication faults."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from memory_harness import atlas, contracts, procedures, store


class _Collection:
    def __init__(self) -> None:
        self.documents: dict[str, dict] = {}
        self.events: list[tuple[str, str]] = []
        self.fail_next_read_for: str | None = None
        self.lose_insert_ack_for: str | None = None

    def find_one(self, query: dict) -> dict | None:
        identifier = query["_id"]
        self.events.append(("read", identifier))
        if self.fail_next_read_for == identifier:
            self.fail_next_read_for = None
            raise RuntimeError("synthetic readback outage")
        document = self.documents.get(identifier)
        return dict(document) if document is not None else None

    def insert_one(self, document: dict) -> object:
        identifier = document["_id"]
        self.events.append(("insert", identifier))
        if identifier in self.documents:
            raise RuntimeError("duplicate id")
        self.documents[identifier] = dict(document)
        if self.lose_insert_ack_for == identifier:
            self.lose_insert_ack_for = None
            raise RuntimeError("synthetic insert acknowledgement loss")
        return object()


class _FaultAdapter(atlas.AtlasProcedureAdapter):
    def __init__(self, *, collection: _Collection) -> None:
        super().__init__(collection=collection, vector_store=object())
        self.publication_writes = 0
        self.lose_ack = False
        self.lose_before_commit = False
        self.fail_before_commit = False
        self.fail_exact_read = False
        self.miss_exact_read = False
        self.miss_state_read = False

    def write_publication(self, publication: dict) -> dict:
        self.publication_writes += 1
        if self.fail_before_commit:
            raise atlas.AtlasProcedureError("synthetic definite pre-commit rejection")
        if self.lose_before_commit:
            self.lose_before_commit = False
            raise atlas.AtlasProcedureAmbiguityError("synthetic lost response before commit")
        document = super().write_publication(publication)
        if self.lose_ack:
            self.lose_ack = False
            raise atlas.AtlasProcedureAmbiguityError("synthetic lost response after commit")
        return document

    def exact_read(self, publication_id: str) -> atlas.AtlasExactProcedureSnapshot | None:
        if self.fail_exact_read:
            self.fail_exact_read = False
            raise atlas.AtlasProcedureError("synthetic exact-read outage")
        if self.miss_exact_read:
            self.miss_exact_read = False
            return None
        snapshot = super().exact_read(publication_id)
        if self.miss_state_read and snapshot is not None:
            self.miss_state_read = False
            return atlas.AtlasExactProcedureSnapshot(
                document=snapshot.document,
                current=snapshot.current,
                publication_state=None,
                revocation=snapshot.revocation,
            )
        return snapshot


class AtlasExternalReconciliationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.memory_store = store.MemoryStore(Path(self.temporary.name) / "memory.sqlite3")
        self.memory_store.initialize()
        self.scope = {
            "application": "harness",
            "project": "product-a",
            "namespace": "exact-reconciliation",
            "owner": "root-agent",
        }
        self.partition = {
            "scope": "project",
            "application": "harness",
            "project": "product-a",
            "namespace": "exact-reconciliation",
            "recipients": [dict(self.scope)],
        }
        self.procedure = contracts.make_procedure_revision(
            logical_name="bounded-recovery",
            origin="curated",
            origin_scope=self.scope,
            body="Inspect the lock before recovery.",
            references=[{"id": "guide://lock", "content": "Preserve lock state."}],
            predicates={
                "applicability": {},
                "conflicts": {},
                "capabilities": {},
                "routes": {},
            },
            source={"kind": "curated_authoring", "provenance_ref": "curation://root/1"},
            created_at="2026-09-21T00:00:00Z",
        )
        self.approval = contracts.make_procedure_approval(
            approval_id="approval-exact",
            procedure=self.procedure,
            issuer="ROOT",
            recipients=[self.scope],
            authority_evidence={"policy_id": "trusted-root/v1", "subject": "root"},
            approved_at="2026-09-21T00:00:01Z",
        )
        self.representation = contracts.make_procedure_representation(
            procedure=self.procedure,
            model="deterministic-test-embedding/v1",
            dimensions=3,
            metric="cosine",
            sanitizer_version="known-secret/v1",
            search_text="lock recovery",
            vector=[0.1, 0.2, 0.3],
            created_at="2026-09-21T00:00:02Z",
        )
        self.service = procedures.TrustedProcedureService(
            self.memory_store, trusted_issuers={"ROOT"}
        )
        self.service.record_approved_revision(self.procedure, self.approval)
        self.service.record_representation(self.representation)
        self.designation = self.service.designate(
            procedure=self.procedure,
            approval=self.approval,
            partition=self.partition,
            issuer="ROOT",
        )
        self.collection = _Collection()
        self.adapter = _FaultAdapter(collection=self.collection)
        self.service.publish_designation(self.designation, self.adapter)

    def tearDown(self) -> None:
        self.memory_store.close()
        self.temporary.cleanup()

    def _publish(self) -> dict:
        return self.service.publish(
            procedure=self.procedure,
            approval=self.approval,
            representation=self.representation,
            designation=self.designation,
            adapter=self.adapter,
        )

    def _publication_and_operation(self) -> tuple[dict, dict]:
        publication = self.memory_store.list_procedure_publications()[0]
        operations = [
            operation
            for operation in self.memory_store.list_procedure_remote_operations(
                payload_id=publication["publication_id"]
            )
            if operation["kind"] == "publication"
        ]
        self.assertEqual(1, len(operations))
        return publication, operations[0]

    def test_success_uses_stable_remote_ids_and_exact_readback(self) -> None:
        publication = self._publish()
        operation = self._publication_and_operation()[1]
        publication_id = publication["publication_id"]
        state_id = atlas.publication_state_document_id(publication_id)
        self.assertEqual(publication_id, self.collection.documents[publication_id]["_id"])
        self.assertEqual(state_id, self.collection.documents[state_id]["_id"])
        self.assertEqual(
            contracts.make_procedure_remote_operation(
                kind="publication",
                logical_id=publication["logical_id"],
                revision_id=publication["revision_id"],
                payload_id=publication_id,
                payload_digest=publication["payload_digest"],
                partition_id=publication["partition_id"],
            )["operation_id"],
            operation["operation_id"],
        )
        self.assertEqual("acknowledged", publication["status"])
        self.assertEqual("acknowledged", operation["status"])
        self.assertEqual(
            {
                "remote_id": publication_id,
                "remote_digest": self.collection.documents[publication_id]["content_hash"],
            },
            operation["remote_receipt"],
        )
        for identifier in (publication_id, state_id):
            insert_at = self.collection.events.index(("insert", identifier))
            self.assertIn(("read", identifier), self.collection.events[insert_at + 1 :])

    def test_lost_ack_after_commit_reconciles_without_a_second_write(self) -> None:
        self.adapter.lose_ack = True
        with self.assertRaises(procedures.ProcedureRemoteAmbiguityError):
            self._publish()
        pending, operation = self._publication_and_operation()
        self.assertEqual("ambiguous", pending["status"])
        self.assertEqual("ambiguous", operation["status"])
        self.assertIsNone(operation["remote_receipt"])

        recovered = self._publish()
        self.assertEqual("acknowledged", recovered["status"])
        self.assertEqual(1, self.adapter.publication_writes)
        self.assertEqual(
            operation["operation_id"], self._publication_and_operation()[1]["operation_id"]
        )

    def test_collection_lost_insert_ack_uses_exact_id_readback(self) -> None:
        publication = contracts.make_procedure_publication(
            procedure=self.procedure,
            approval=self.approval,
            representation=self.representation,
            designation=self.designation,
        )
        publication_id = publication["publication_id"]
        state_id = atlas.publication_state_document_id(publication_id)
        self.collection.lose_insert_ack_for = publication_id

        completed = self._publish()
        self.assertEqual("acknowledged", completed["status"])
        self.assertEqual("acknowledged", self._publication_and_operation()[1]["status"])
        self.assertEqual(1, self.collection.events.count(("insert", publication_id)))
        self.assertEqual(1, self.collection.events.count(("insert", state_id)))
        insert_at = self.collection.events.index(("insert", publication_id))
        self.assertIn(("read", publication_id), self.collection.events[insert_at + 1 :])

    def test_lost_ack_without_exact_evidence_stays_ambiguous_and_does_not_resubmit(self) -> None:
        self.adapter.lose_before_commit = True
        with self.assertRaises(procedures.ProcedureRemoteAmbiguityError):
            self._publish()
        pending, operation = self._publication_and_operation()
        with self.assertRaises(procedures.ProcedureRemoteAmbiguityError):
            self._publish()
        self.assertEqual("ambiguous", self._publication_and_operation()[0]["status"])
        self.assertEqual(
            operation["operation_id"], self._publication_and_operation()[1]["operation_id"]
        )
        self.assertEqual(1, self.adapter.publication_writes)
        self.assertNotIn(pending["publication_id"], self.collection.documents)

    def test_definite_pre_commit_rejection_is_blocked(self) -> None:
        self.adapter.fail_before_commit = True
        with self.assertRaises(procedures.ProcedureError):
            self._publish()
        pending, operation = self._publication_and_operation()
        self.assertEqual("blocked", pending["status"])
        self.assertEqual("blocked", operation["status"])
        self.assertIsNone(operation["remote_receipt"])
        self.assertNotIn(pending["publication_id"], self.collection.documents)
        with self.assertRaises(procedures.ProcedureIneligibleError):
            self._publish()
        self.assertEqual(1, self.adapter.publication_writes)

    def test_pre_write_exact_read_outage_is_blocked_without_submission(self) -> None:
        publication = contracts.make_procedure_publication(
            procedure=self.procedure,
            approval=self.approval,
            representation=self.representation,
            designation=self.designation,
        )
        self.collection.fail_next_read_for = publication["publication_id"]
        with self.assertRaises(procedures.ProcedureError):
            self._publish()
        pending, operation = self._publication_and_operation()
        self.assertEqual("blocked", pending["status"])
        self.assertEqual("blocked", operation["status"])
        self.assertNotIn(publication["publication_id"], self.collection.documents)
        self.assertEqual(1, self.adapter.publication_writes)

    def test_state_id_read_outage_after_publication_insert_is_ambiguous(self) -> None:
        publication = contracts.make_procedure_publication(
            procedure=self.procedure,
            approval=self.approval,
            representation=self.representation,
            designation=self.designation,
        )
        publication_id = publication["publication_id"]
        state_id = atlas.publication_state_document_id(publication_id)
        self.collection.fail_next_read_for = state_id

        with self.assertRaises(procedures.ProcedureRemoteAmbiguityError):
            self._publish()
        pending, operation = self._publication_and_operation()
        self.assertEqual("ambiguous", pending["status"])
        self.assertEqual("ambiguous", operation["status"])
        self.assertIsNone(operation["remote_receipt"])
        self.assertIn(publication_id, self.collection.documents)
        self.assertNotIn(state_id, self.collection.documents)
        with self.assertRaises(procedures.ProcedureRemoteAmbiguityError):
            self._publish()
        self.assertEqual(1, self.adapter.publication_writes)

    def test_exact_read_error_after_write_stays_ambiguous_until_reconciled(self) -> None:
        self.adapter.fail_exact_read = True
        with self.assertRaises(procedures.ProcedureRemoteAmbiguityError):
            self._publish()
        pending, operation = self._publication_and_operation()
        self.assertEqual("ambiguous", pending["status"])
        self.assertEqual("ambiguous", operation["status"])
        self.assertIsNone(operation["remote_receipt"])
        self.assertEqual("acknowledged", self._publish()["status"])
        self.assertEqual(1, self.adapter.publication_writes)

    def test_missing_exact_read_after_write_stays_ambiguous_until_reconciled(self) -> None:
        self.adapter.miss_exact_read = True
        with self.assertRaises(procedures.ProcedureRemoteAmbiguityError):
            self._publish()
        pending, operation = self._publication_and_operation()
        self.assertEqual("ambiguous", pending["status"])
        self.assertEqual("ambiguous", operation["status"])
        self.assertEqual("acknowledged", self._publish()["status"])
        self.assertEqual(1, self.adapter.publication_writes)

    def test_missing_lifecycle_readback_after_write_stays_ambiguous(self) -> None:
        self.adapter.miss_state_read = True
        with self.assertRaises(procedures.ProcedureRemoteAmbiguityError):
            self._publish()
        pending, operation = self._publication_and_operation()
        self.assertEqual("ambiguous", pending["status"])
        self.assertEqual("ambiguous", operation["status"])
        self.assertEqual("acknowledged", self._publish()["status"])
        self.assertEqual(1, self.adapter.publication_writes)

    def test_collection_readback_outage_after_insert_is_ambiguous(self) -> None:
        publication = contracts.make_procedure_publication(
            procedure=self.procedure,
            approval=self.approval,
            representation=self.representation,
            designation=self.designation,
        )
        publication_id = publication["publication_id"]
        original_insert = self.collection.insert_one

        def insert_then_fail_read(document: dict) -> object:
            result = original_insert(document)
            if document["_id"] == publication_id:
                self.collection.fail_next_read_for = publication_id
            return result

        self.collection.insert_one = insert_then_fail_read  # type: ignore[method-assign]
        with self.assertRaises(procedures.ProcedureRemoteAmbiguityError):
            self._publish()
        pending, operation = self._publication_and_operation()
        self.assertEqual("ambiguous", pending["status"])
        self.assertEqual("ambiguous", operation["status"])
        self.assertEqual(1, self.adapter.publication_writes)
        self.assertEqual(publication_id, pending["publication_id"])
        with self.assertRaises(procedures.ProcedureRemoteAmbiguityError):
            self._publish()
        self.assertEqual("ambiguous", self._publication_and_operation()[0]["status"])
        self.assertEqual(1, self.adapter.publication_writes)


if __name__ == "__main__":
    unittest.main()
