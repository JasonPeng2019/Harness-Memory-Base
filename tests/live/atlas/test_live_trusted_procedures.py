from __future__ import annotations

import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from memory_harness import atlas, contracts, procedures, store


_URI = os.environ.get("MEMORY_HARNESS_ATLAS_URI")
_DATABASE = os.environ.get("MEMORY_HARNESS_ATLAS_LIVE_DATABASE")
_OPT_IN = os.environ.get("MEMORY_HARNESS_RUN_LIVE_ATLAS") == "1"
_LIVE_READY = bool(_URI and _DATABASE and _OPT_IN)


@unittest.skipUnless(
    _LIVE_READY,
    "set MEMORY_HARNESS_RUN_LIVE_ATLAS=1, MEMORY_HARNESS_ATLAS_URI, and "
    "MEMORY_HARNESS_ATLAS_LIVE_DATABASE to run the owned live Atlas test",
)
class LiveAtlasTrustedProcedureTests(unittest.TestCase):
    """One synthetic namespace/index/data lifecycle, cleaned in ``finally``."""

    def test_vector_discovery_exact_validation_and_revocation(self) -> None:
        try:
            from langchain_core.embeddings import Embeddings
            from pymongo import MongoClient
        except ImportError as exc:  # pragma: no cover - environment gated
            self.skipTest(f"Atlas optional dependency unavailable: {exc.__class__.__name__}")

        class _DeterministicEmbeddings(Embeddings):
            """Disposable local embedding surface for an Atlas vector-index test."""

            @staticmethod
            def _vector(text: str) -> list[float]:
                normalized = text.casefold()
                return [
                    float(normalized.count("parser") + 1),
                    float(normalized.count("lock") + 1),
                    float(normalized.count("recovery") + 1),
                ]

            def embed_documents(self, texts: list[str]) -> list[list[float]]:
                return [self._vector(text) for text in texts]

            def embed_query(self, text: str) -> list[float]:
                return self._vector(text)

        run_token = uuid.uuid4().hex
        namespace = f"step03-live-{run_token}"
        collection_name = f"trusted_procedures_{run_token}"
        index_name = f"vector_{run_token}"
        client = MongoClient(_URI, serverSelectionTimeoutMS=10_000)
        collection = client[_DATABASE][collection_name]
        temporary = tempfile.TemporaryDirectory()
        memory_store = store.MemoryStore(Path(temporary.name) / "memory.sqlite3")
        memory_store.initialize()
        try:
            scope = {
                "application": "memory-harness-live-test",
                "project": "step03-synthetic",
                "namespace": namespace,
                "owner": "root-live-test",
            }
            partition = {
                "scope": "project",
                "application": scope["application"],
                "project": scope["project"],
                "namespace": scope["namespace"],
                "recipients": [scope],
            }
            procedure = contracts.make_procedure_revision(
                logical_name="live-parser-lock-repair",
                origin="curated",
                origin_scope=scope,
                body="Inspect the local parser lock before network recovery.",
                references=[
                    {"id": "live://guide/lock", "content": "Preserve the lock invariant."}
                ],
                predicates={
                    "applicability": {
                        "all": [
                            {"field": "language", "operator": "equals", "value": "python"}
                        ]
                    },
                    "conflicts": {},
                    "capabilities": {},
                    "routes": {
                        "all": [
                            {"field": "route", "operator": "equals", "value": "ordinary"}
                        ]
                    },
                },
                source={"kind": "curated_authoring", "provenance_ref": f"live://{run_token}"},
            )
            approval = contracts.make_procedure_approval(
                approval_id=f"live-approval-{run_token}",
                procedure=procedure,
                issuer="ROOT",
                recipients=[scope],
                authority_evidence={"policy_id": "live-test-root/v1", "subject": "root"},
            )
            representation = contracts.make_procedure_representation(
                procedure=procedure,
                model="live-deterministic-embedding/v1",
                dimensions=3,
                metric="cosine",
                sanitizer_version="live-test/v1",
                search_text="parser lock recovery",
                vector=_DeterministicEmbeddings._vector("parser lock recovery"),
            )
            service = procedures.TrustedProcedureService(
                memory_store, trusted_issuers={"ROOT"}
            )
            service.record_approved_revision(procedure, approval)
            service.record_representation(representation)
            designation = service.designate(
                procedure=procedure,
                approval=approval,
                partition=partition,
                issuer="ROOT",
            )
            adapter = atlas.AtlasProcedureAdapter.from_pymongo_collection(
                collection=collection,
                embedding=_DeterministicEmbeddings(),
                index_name=index_name,
            )
            adapter.create_vector_search_index(
                dimensions=3,
                filter_fields=["document_kind", "application", "namespace"],
                wait_until_complete=120.0,
            )
            service.publish_designation(designation, adapter)
            publication = service.publish(
                procedure=procedure,
                approval=approval,
                representation=representation,
                designation=designation,
                adapter=adapter,
            )
            delivered = service.resolve_atlas(
                "parser lock recovery",
                receiver=scope,
                facts={"language": "python"},
                route="ordinary",
                adapter=adapter,
            )
            self.assertEqual([publication["publication_id"]], [item["publication_id"] for item in delivered])
            exact = adapter.exact_read(publication["publication_id"])
            self.assertIsNotNone(exact)
            self.assertEqual(publication["revision_id"], exact.document["revision_id"])

            revoked = service.revoke(
                procedure=procedure,
                issuer="ROOT",
                reason="synthetic live lifecycle completion",
                adapter=adapter,
            )
            self.assertTrue(revoked["managed_complete"])
            self.assertEqual(
                [],
                service.resolve_atlas(
                    "parser lock recovery",
                    receiver=scope,
                    facts={"language": "python"},
                    route="ordinary",
                    adapter=adapter,
                ),
            )
        finally:
            # The name contains a fresh UUID and is this test's only remote resource.
            try:
                collection.drop()
            finally:
                client.close()
                memory_store.close()
                temporary.cleanup()


if __name__ == "__main__":
    unittest.main()
