"""Shared durable boundary for source-specific external effects."""

from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from memory_harness import config, contracts, store
from tests.local.contracts import test_terminal_outcome as fixture_module


class ExternalReconciliationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.state = store.MemoryStore(Path(self.directory.name) / "effects.sqlite3")
        self.state.initialize()
        self.source = {"schema": "trusted-source/v1", "source_id": "source-1", "value": "approved"}
        self.source["content_hash"] = contracts.content_hash(self.source)

    def tearDown(self) -> None:
        self.state.close()
        self.directory.cleanup()

    def _create(self, **changes: object) -> dict:
        inputs = {
            "kind": "procedure_publication", "scope_key": "recipient-1",
            "source_id": "source-1", "source_record": self.source,
            "payload": {"publication": "exact"},
            "captured_config": config.MemoryConfig(),
            "current_config": config.MemoryConfig(),
        }
        inputs.update(changes)
        return self.state.create_external_effect_operation(**inputs)[0]

    @staticmethod
    def _evidence(operation: dict, **changes: object) -> dict:
        evidence = {key: operation[key] for key in (
            "operation_id", "kind", "scope_key", "source_digest", "payload_digest",
            "configuration_digest",
        )}
        evidence["adapter_proof"] = {"remote_id": "remote-1", "digest": "exact"}
        evidence.update(changes)
        return evidence

    def test_lost_ack_requires_exact_reconciliation_before_retry(self) -> None:
        intent = self._create()
        _, created = self.state.create_external_effect_operation(
            kind="procedure_publication", scope_key="recipient-1", source_id="source-1",
            source_record=self.source, payload={"publication": "exact"},
            captured_config=config.MemoryConfig(), current_config=config.MemoryConfig(),
        )
        self.assertFalse(created)
        self.assertEqual("pending", intent["status"])
        claimed = self.state.claim_effect_operation(intent["operation_id"], current_config=config.MemoryConfig())
        self.assertEqual("in_flight", claimed["status"])
        uncertain = self.state.mark_effect_uncertain(intent["operation_id"], "acknowledgement lost")
        self.assertEqual("uncertain", uncertain["status"])
        with self.assertRaises(store.OperationConflictError):
            self.state.claim_effect_operation(intent["operation_id"], current_config=config.MemoryConfig())
        self.assertEqual("uncertain", self.state.get_effect_operation(intent["operation_id"])["status"])

    def test_exact_replay_and_conflicts_survive_restart(self) -> None:
        intent = self._create()
        self.state.close()
        self.state = store.MemoryStore(Path(self.directory.name) / "effects.sqlite3")
        self.state.initialize()
        replay = self._create()
        self.assertEqual(intent, replay)
        with self.assertRaisesRegex(store.OperationConflictError, "replay"):
            self._create(payload={"publication": "different"})
        with self.assertRaisesRegex(store.OperationConflictError, "replay"):
            self._create(captured_config=config.resolve_config({"experience_read": False}))
        with self.assertRaisesRegex(contracts.ContractError, "source ID"):
            self._create(source_id="new-identity")
        self.assertNotEqual(intent["operation_id"], self._create(scope_key="recipient-2")["operation_id"])

    def test_same_source_record_cannot_gain_second_identity(self) -> None:
        source = dict(self.source, alternate_id="alternate")
        source["content_hash"] = contracts.content_hash(source)
        original = self._create(source_record=source)
        self.state.claim_effect_operation(original["operation_id"], current_config=config.MemoryConfig())
        self.state.confirm_effect_operation(original["operation_id"], self._evidence(original))
        with self.assertRaisesRegex(store.OperationConflictError, "source already"):
            self._create(source_id="alternate", source_record=source)

    def test_accepted_effect_table_migrates_for_source_owned_rows(self) -> None:
        case = fixture_module.TerminalOutcomeTests(methodName="test_exact_observed_native_chain_fixes_pass")
        case.setUp()
        try:
            case.observe()
            outcome = case.runtime.record_terminal_outcome(case.bundle())
            before = case.state.list_effect_operations(outcome["outcome_id"])
            self.assertEqual(4, len(before))
            connection = case.state.connection
            schema = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='effect_operations'"
            ).fetchone()["sql"]
            old_schema = (schema.replace("effect_operations (", "effect_operations_old (", 1)
                          .replace("outcome_id TEXT REFERENCES", "outcome_id TEXT NOT NULL REFERENCES", 1)
                          .replace("decision_id TEXT,", "decision_id TEXT NOT NULL,", 1))
            columns = ", ".join(row["name"] for row in connection.execute(
                "PRAGMA table_info(effect_operations)"
            ).fetchall() if row["name"] != "reconciliation")
            with connection:
                connection.execute(old_schema.replace("        reconciliation TEXT,\n", ""))
                connection.execute(
                    f"INSERT INTO effect_operations_old ({columns}) "
                    f"SELECT {columns} FROM effect_operations"
                )
                connection.execute("DROP TABLE effect_operations")
                connection.execute("ALTER TABLE effect_operations_old RENAME TO effect_operations")
            case.state.close()
            case.state = store.MemoryStore(case.root / "state.sqlite3")
            case.state.initialize()
            self.assertEqual(before, case.state.list_effect_operations(outcome["outcome_id"]))
        finally:
            case.tearDown()

    def test_fault_readback_and_original_identity(self) -> None:
        intent = self._create()
        calls: list[str] = []

        def submit() -> None:
            self.state.claim_effect_operation(intent["operation_id"], current_config=config.MemoryConfig())
            calls.append(intent["operation_id"])
            self.state.mark_effect_uncertain(intent["operation_id"], "response lost")

        submit()
        with self.assertRaises(store.OperationConflictError):
            submit()
        self.assertEqual([intent["operation_id"]], calls)
        with self.assertRaisesRegex(store.OperationConflictError, "exact"):
            self.state.reconcile_external_effect_operation(
                intent["operation_id"], evidence=self._evidence(intent, payload_digest="wrong"),
                result="acknowledged",
            )
        acknowledged = self.state.reconcile_external_effect_operation(
            intent["operation_id"], evidence=self._evidence(intent), result="acknowledged",
        )
        self.assertEqual("confirmed", acknowledged["status"])
        self.assertEqual(acknowledged, self.state.reconcile_external_effect_operation(
            intent["operation_id"], evidence=self._evidence(intent), result="acknowledged",
        ))
        with self.assertRaises(store.OperationConflictError):
            self.state.claim_effect_operation(intent["operation_id"], current_config=config.MemoryConfig())

    def test_exact_absence_or_original_idempotency_authorizes_one_retry(self) -> None:
        for result, extra in (("absent", {}),
                              ("idempotent", {"idempotency_key": "original"})):
            with self.subTest(result=result):
                intent = self._create(scope_key=result)
                identity = intent["operation_id"]
                self.state.claim_effect_operation(identity, current_config=config.MemoryConfig())
                self.state.mark_effect_uncertain(identity, "response lost")
                evidence = self._evidence(intent, **extra)
                with self.assertRaises(store.OperationConflictError):
                    self.state.reconcile_external_effect_operation(
                        identity, evidence=evidence, result=result,
                    )
                evidence.update(readback_complete=True, idempotency_key=identity)
                pending = self.state.reconcile_external_effect_operation(
                    identity, evidence=evidence, result=result,
                )
                self.assertEqual("pending", pending["status"])
                self.assertIsNone(pending["acknowledgement"])
                self.assertEqual(evidence, pending["reconciliation"])
                if result == "absent":
                    self.assertEqual("confirmed", self.state.confirm_effect_operation(
                        identity, self._evidence(intent),
                    )["status"])
                else:
                    self.assertEqual(identity, self.state.claim_effect_operation(
                        identity, current_config=config.MemoryConfig(),
                    )["operation_id"])

    def test_feature_off_blocks_new_and_pending_and_shows_in_flight(self) -> None:
        off = config.resolve_config({"shared_publication": False})
        with self.assertRaisesRegex(store.OperationConflictError, "off"):
            self._create(current_config=off)
        pending = self._create()
        self.assertEqual("off", self.state.external_effect_off_state(
            pending["operation_id"], current_config=off,
        ))
        with self.assertRaisesRegex(store.OperationConflictError, "off"):
            self.state.claim_effect_operation(pending["operation_id"], current_config=off)
        self.state.claim_effect_operation(pending["operation_id"], current_config=config.MemoryConfig())
        self.assertEqual("pending_off", self.state.external_effect_off_state(
            pending["operation_id"], current_config=off,
        ))
        self.state.mark_effect_uncertain(pending["operation_id"], "response lost")
        self.assertEqual("pending_off", self.state.external_effect_off_state(
            pending["operation_id"], current_config=off,
        ))
        self.state.reconcile_external_effect_operation(
            pending["operation_id"], evidence=self._evidence(pending), result="acknowledged",
        )
        self.assertEqual("off", self.state.external_effect_off_state(
            pending["operation_id"], current_config=off,
        ))

    def test_two_connections_serialize_one_claim_and_leave_other_scope_free(self) -> None:
        intent = self._create()
        other = self._create(scope_key="recipient-2")
        barrier = threading.Barrier(2)

        def claim() -> str:
            peer = store.MemoryStore(Path(self.directory.name) / "effects.sqlite3")
            peer.initialize()
            try:
                barrier.wait(timeout=5)
                try:
                    peer.claim_effect_operation(intent["operation_id"], current_config=config.MemoryConfig())
                    return "claimed"
                except store.OperationConflictError:
                    return "fenced"
            finally:
                peer.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: claim(), range(2)))
        self.assertEqual(["claimed", "fenced"], sorted(results))
        self.assertEqual("in_flight", self.state.claim_effect_operation(
            other["operation_id"], current_config=config.MemoryConfig(),
        )["status"])


if __name__ == "__main__":
    unittest.main()
