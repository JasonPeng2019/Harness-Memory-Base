from __future__ import annotations

import importlib.util
import json
import math
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from memory_harness import atlas, config, privacy, procedures, store

LIVE_TEST = ROOT / "tests" / "live" / "atlas" / "test_live_trusted_procedures.py"
SPEC = importlib.util.spec_from_file_location("live_atlas_fixture", LIVE_TEST)
assert SPEC and SPEC.loader
fixture = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fixture)


class _IndexAdapter:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def create_vector_search_index(self, **kwargs: object) -> None:
        self.calls.append(kwargs)


class _Collection:
    def __init__(self) -> None:
        self.documents: dict[str, dict] = {}
        self.reads: list[str] = []

    def find_one(self, selector: dict) -> dict | None:
        identifier = selector["_id"]
        self.reads.append(identifier)
        value = self.documents.get(identifier)
        return dict(value) if value is not None else None

    def insert_one(self, document: dict) -> object:
        if document["_id"] in self.documents:
            raise ValueError("duplicate")
        self.documents[document["_id"]] = dict(document)
        return object()

    def replace_one(self, selector: dict, document: dict, *, upsert: bool = False) -> object:
        current = self.documents.get(selector["_id"])
        matched = bool(current and all(current.get(k) == v for k, v in selector.items()))
        if matched:
            self.documents[selector["_id"]] = dict(document)
        return type("WriteResult", (), {"matched_count": int(matched)})()


class _Document:
    def __init__(self, identifier: str) -> None:
        self.id = identifier
        self.metadata = {"_id": identifier}


class _VectorStore:
    def __init__(self, collection: _Collection) -> None:
        self.collection = collection
        self.calls: list[dict] = []

    def similarity_search_with_score(self, query: str, **kwargs: object) -> list[tuple[_Document, float]]:
        self.calls.append({"query": query, **kwargs})
        selector = kwargs["pre_filter"]
        return [
            (_Document(identifier), 0.9)
            for identifier, value in self.collection.documents.items()
            if value.get("document_kind") == selector["document_kind"]
            and value.get("application") == selector["application"]
            and value.get("namespace") == selector["namespace"]
        ]


class _OwnedCollection:
    def __init__(self) -> None:
        self.drops = 0

    def drop(self) -> None:
        self.drops += 1


class AtlasLiveRepresentationTests(unittest.TestCase):
    def test_product_identity_vector_and_single_index_without_network(self) -> None:
        adapter = _IndexAdapter()
        with mock.patch.object(socket.socket, "connect", side_effect=AssertionError("network call")):
            embedding = fixture.DeterministicEmbeddings()
            document_vector = embedding.embed_documents(["parser lock recovery"])[0]
            query_vector = embedding.embed_query("parser lock recovery")
            fixture.create_fixture_index(adapter)

        self.assertEqual(query_vector, document_vector)
        self.assertEqual(config.REPRESENTATION_DIMENSIONS, len(query_vector))
        self.assertTrue(all(math.isfinite(value) for value in query_vector))
        self.assertEqual(query_vector, embedding.embed_query("parser lock recovery"))
        self.assertNotEqual(query_vector, embedding.embed_query("unrelated procedure text"))
        self.assertEqual({
            "model": "local-token-overlap/v1", "dimensions": 512,
            "metric": "cosine", "sanitizer_version": "v1",
        }, fixture.representation_identity())
        self.assertEqual([{
            "dimensions": 512,
            "filter_fields": ["document_kind", "application", "namespace"],
            "wait_until_complete": 120.0,
        }], adapter.calls)

    def test_incompatible_fixture_is_rejected_before_index_mutation(self) -> None:
        scope = {
            "application": "memory-harness-live-test", "project": "step18-synthetic",
            "namespace": "step18-live-offline", "owner": "root-live-test",
        }
        valid = fixture.make_fixture(scope, "1c2850372db24777bf0a2e72dfd341c4", retained=True)
        incompatible = {**valid, "representation": {**valid["representation"], "dimensions": 3}}
        adapter = _IndexAdapter()
        with self.assertRaises(ValueError):
            fixture.create_fixture_index(adapter, fixtures=(valid, incompatible))
        self.assertEqual([], adapter.calls)


class AtlasLiveEligibilityControlTests(unittest.TestCase):
    def test_two_distinct_product_records_keep_one_eligible_after_other_revoked(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = store.MemoryStore(Path(directory) / "memory.sqlite3")
            database.initialize()
            try:
                collection = _Collection()
                vector_store = _VectorStore(collection)
                adapter = atlas.AtlasProcedureAdapter(collection=collection, vector_store=vector_store)
                service = procedures.TrustedProcedureService(database, trusted_issuers={"ROOT"})
                scope = {
                    "application": "memory-harness-live-test", "project": "step18-synthetic",
                    "namespace": "step18-live-offline", "owner": "root-live-test",
                }
                with mock.patch.object(socket.socket, "connect", side_effect=AssertionError("network call")):
                    eligible, revoked = fixture.prove_fixture_eligibility(
                        self, service=service, adapter=adapter, scope=scope,
                        run_token="1c2850372db24777bf0a2e72dfd341c4",
                    )
                self.assertNotEqual(eligible["logical_id"], revoked["logical_id"])
                self.assertNotEqual(eligible["revision_id"], revoked["revision_id"])
                self.assertNotEqual(eligible["publication_id"], revoked["publication_id"])
                self.assertNotEqual(eligible["representation"]["search_text"], revoked["representation"]["search_text"])
                self.assertEqual(fixture.representation_identity()["dimensions"], len(eligible["representation"]["vector"]))
                sanitized_body = privacy.sanitize_payload(
                    eligible["procedure"]["behavior"]["body"], privacy.PrivacyPolicy()
                )
                self.assertIn(fixture.ATLAS_MVP_MARKER, sanitized_body)
                self.assertGreaterEqual(len(vector_store.calls), 4)
                self.assertIn(eligible["publication_id"], collection.reads)
                self.assertIn(revoked["publication_id"], collection.reads)
            finally:
                database.close()


class AtlasLiveHandoffControlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "atlas-manifest.json"

    def _valid_manifest(self) -> dict:
        database = store.MemoryStore(Path(self.temporary.name) / "handoff.sqlite3")
        database.initialize()
        try:
            collection = _Collection()
            adapter = atlas.AtlasProcedureAdapter(
                collection=collection, vector_store=_VectorStore(collection)
            )
            service = procedures.TrustedProcedureService(database, trusted_issuers={"ROOT"})
            scope = {
                "application": "memory-harness-live-test", "project": "step18-synthetic",
                "namespace": "step18-live-1c2850372db24777bf0a2e72dfd341c4",
                "owner": "root-live-test",
            }
            retained, _ = fixture.prove_fixture_eligibility(
                self, service=service, adapter=adapter, scope=scope,
                run_token="1c2850372db24777bf0a2e72dfd341c4",
            )
            self.publication = retained
            return fixture.handoff_manifest(
                database="synthetic_live",
                collection="trusted_procedures_1c2850372db24777bf0a2e72dfd341c4",
                index="vector_1c2850372db24777bf0a2e72dfd341c4",
                publication=retained,
                receiver=scope,
                query=fixture.ELIGIBLE_QUERY,
                route=fixture.ROUTE,
                facts=fixture.FACTS,
            )
        finally:
            database.close()

    def test_only_explicit_success_retains_and_writes_complete_nonsecret_manifest(self) -> None:
        manifest = self._valid_manifest()
        expected_fields = {
            "schema", "database", "collection", "index", "namespace", "partition",
            "partition_id", "logical_id", "revision_id", "publication_id", "receiver",
            "query", "route", "facts", "representation", "material_use_marker",
            "cleanup_owner", "cleanup_target",
        }
        self.assertEqual(expected_fields, set(manifest))
        self.assertEqual(fixture.ATLAS_MVP_MARKER, manifest["material_use_marker"])
        self.assertEqual("Master-ROOT", manifest["cleanup_owner"])
        self.assertEqual({"database": manifest["database"], "collection": manifest["collection"]}, manifest["cleanup_target"])
        self.assertEqual(fixture.representation_identity(), manifest["representation"])
        self.assertNotIn("uri", json.dumps(manifest).lower())
        self.assertNotIn("key", json.dumps(manifest).lower())

        owned = _OwnedCollection()
        self.assertEqual(manifest, fixture.run_owned_collection(
            owned, action=lambda: manifest, manifest_path=self.path,
        ))
        self.assertEqual(0, owned.drops)
        self.assertEqual(manifest, json.loads(self.path.read_text(encoding="utf-8")))

    def test_default_assertion_abort_preexisting_path_and_write_failure_drop_exactly_once(self) -> None:
        manifest = self._valid_manifest()
        default = _OwnedCollection()
        self.assertEqual(manifest, fixture.run_owned_collection(
            default, action=lambda: manifest, manifest_path=None,
        ))
        self.assertEqual(1, default.drops)
        self.assertFalse(self.path.exists())

        for error in (AssertionError, KeyboardInterrupt):
            owned = _OwnedCollection()
            with self.subTest(error=error), self.assertRaises(error):
                fixture.run_owned_collection(
                    owned, action=lambda: (_ for _ in ()).throw(error("failed")),
                    manifest_path=self.path,
                )
            self.assertEqual(1, owned.drops)

        self.path.write_text("already owned", encoding="utf-8")
        owned = _OwnedCollection()
        with self.assertRaises(FileExistsError):
            fixture.run_owned_collection(owned, action=lambda: manifest, manifest_path=self.path)
        self.assertEqual(1, owned.drops)
        self.assertEqual("already owned", self.path.read_text(encoding="utf-8"))
        self.path.unlink()

        owned = _OwnedCollection()
        with mock.patch.object(socket.socket, "connect", side_effect=AssertionError("network call")):
            with mock.patch.object(fixture.os, "link", side_effect=OSError("write failed")):
                with self.assertRaises(OSError):
                    fixture.run_owned_collection(owned, action=lambda: manifest, manifest_path=self.path)
        self.assertEqual(1, owned.drops)
        self.assertFalse(self.path.exists())
        self.assertEqual([], list(Path(self.temporary.name).glob("*.tmp")))

    def test_post_link_temporary_unlink_failure_retains_exact_owned_collection(self) -> None:
        manifest = self._valid_manifest()
        owned = _OwnedCollection()
        original_unlink = Path.unlink

        def fail_temporary_unlink(path: Path, *args: object, **kwargs: object) -> None:
            if path.suffix == ".tmp":
                self.assertTrue(self.path.exists(), "failure must occur after destination link")
                raise OSError("temporary cleanup failed")
            original_unlink(path, *args, **kwargs)

        with mock.patch.object(Path, "unlink", fail_temporary_unlink):
            with self.assertRaises(OSError) as raised:
                fixture.run_owned_collection(owned, action=lambda: manifest, manifest_path=self.path)

        self.assertTrue(self.path.exists())
        self.assertEqual(0, owned.drops, f"manifest_exists={self.path.exists()}, collection_drops={owned.drops}")
        self.assertEqual(manifest, json.loads(self.path.read_text(encoding="utf-8")))
        self.assertEqual(self.path, raised.exception.manifest_path)
        self.assertEqual(manifest["cleanup_target"], raised.exception.cleanup_target)

    def test_manifest_rejects_mismatched_owned_identity_and_representation(self) -> None:
        manifest = self._valid_manifest()
        with self.assertRaises(ValueError):
            fixture.handoff_manifest(
                database=manifest["database"], collection="trusted_procedures_wrong",
                index=manifest["index"], publication=self.publication, receiver=manifest["receiver"],
                query=manifest["query"], route=manifest["route"], facts=manifest["facts"],
            )
        with self.assertRaises(ValueError):
            fixture.handoff_manifest(
                database=manifest["database"], collection=manifest["collection"],
                index=manifest["index"], publication=self.publication, receiver=manifest["receiver"],
                query="wrong query", route=manifest["route"], facts=manifest["facts"],
            )
        incompatible = {**self.publication, "representation": {
            **self.publication["representation"], "dimensions": 3,
        }}
        with self.assertRaises(ValueError):
            fixture.handoff_manifest(
                database=manifest["database"], collection=manifest["collection"],
                index=manifest["index"], publication=incompatible, receiver=manifest["receiver"],
                query=manifest["query"], route=manifest["route"], facts=manifest["facts"],
            )


if __name__ == "__main__":
    unittest.main()
