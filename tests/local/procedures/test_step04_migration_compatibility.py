"""STEP-04 additive migration: reopen accepted STEP-01..STEP-03 state.

The accepted baseline already owns decisions, dispatches, outcomes, reviews,
reviewed experience, and the trusted-procedure lifecycle. STEP-04 adds
preparation, search-trace, plan-disposition, APC child-operation, and final
context tables. Reopening an accepted database must preserve every existing
record and identity while adding the new tables additively.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from memory_harness import config, contracts, store


_STEP04_TABLES = frozenset(
    {
        "preparations",
        "search_traces",
        "search_candidates",
        "plan_dispositions",
        "apc_child_operations",
        "final_contexts",
    }
)


class Step04MigrationCompatibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary.name) / "accepted-step-03.sqlite3"
        self.card = contracts.make_task_card(
            task="Preserve the accepted STEP-03 record",
            base_commit="accepted-base",
        )
        self.plan = contracts.make_plan(
            plan_id="accepted-plan",
            objective_id="objective-1",
            route="ordinary",
            state="accepted",
            accepted_by="ROOT",
            content={"steps": ["inspect", "verify"]},
        )
        self.decision = contracts.make_decision(
            self.card, self.plan, strategy="standard", configuration={"strategy": "standard"}
        )
        self.envelope = contracts.make_envelope(
            task_card=self.card,
            plan=self.plan,
            decision_id=self.decision["decision_id"],
            lane_id="lane-1",
            run_id="run-1",
            worktree_path="C:/synthetic/accepted",
            base_commit="accepted-base",
            mandatory_content=[{"id": "task", "kind": "task", "content": self.card["task"]}],
        )
        self.operation = contracts.make_operation(kind="dispatch", envelope=self.envelope)
        self.outcome = contracts.make_outcome(
            decision_id=self.decision["decision_id"],
            plan_id=self.plan["plan_id"],
            plan_digest=self.plan["content_hash"],
            status="PASS",
            evidence_digest="accepted-evidence",
            linked_run_id="run-1",
            task_card_digest=self.card["content_hash"],
            objective_id=self.plan["objective_id"],
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write_accepted_records(self, memory_store: store.MemoryStore) -> None:
        memory_store.record_decision(self.decision)
        memory_store.create_operation(self.operation)
        memory_store.record_outcome(self.outcome)

    def test_reopening_an_accepted_database_preserves_records_and_adds_step04(self) -> None:
        accepted = store.MemoryStore(self.database_path)
        accepted.initialize()
        try:
            self._write_accepted_records(accepted)
        finally:
            accepted.close()

        # Emulate the accepted baseline: STEP-04 tables do not exist yet.
        reopened = store.MemoryStore(self.database_path)
        reopened.initialize()
        try:
            assert reopened.connection is not None
            for table in _STEP04_TABLES:
                reopened.connection.execute(f"DROP TABLE {table}")
            reopened.connection.commit()
        finally:
            reopened.close()

        upgraded = store.MemoryStore(self.database_path)
        upgraded.initialize()
        try:
            tables = {
                row["name"]
                for row in upgraded.connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            self.assertTrue(_STEP04_TABLES.issubset(tables))

            self.assertEqual(
                self.decision["content_hash"],
                upgraded.get_decision(self.decision["decision_id"])["content_hash"],
            )
            operations = upgraded.list_operations(self.decision["decision_id"])
            self.assertEqual([self.operation["operation_id"]], [item["operation_id"] for item in operations])
            self.assertEqual(
                self.outcome["outcome_id"],
                upgraded.get_outcome(self.decision["decision_id"])["outcome_id"],
            )

            # The reopened tables accept fresh STEP-04 records unchanged.
            limits = config.resolve_limits({"default_deadline_seconds": 300.0})
            preparation = contracts.make_preparation(
                task_card=self.card,
                decision_id=self.decision["decision_id"],
                objective_id="objective-1",
                route="ordinary",
                plan_id=self.plan["plan_id"],
                plan_digest=self.plan["content_hash"],
                plan_state=self.plan["state"],
                current_plan_state="execution_accepted",
                strategy="standard",
                requested_strategy="standard",
                configuration={"strategy": "standard"},
                network_mode="normal",
                budget_source="trusted_deadline",
                remaining_seconds=300.0,
                execution_reserve_seconds=limits.execution_reserve_seconds,
                stage_allowance_seconds=limits.standard_stage_seconds,
                deadline_monotonic=1300.0,
                spent_seconds=0.0,
            )
            upgraded.record_preparation(preparation)
            self.assertEqual(
                preparation["preparation_id"],
                upgraded.get_preparation(preparation["preparation_id"])["preparation_id"],
            )
        finally:
            upgraded.close()

    def test_legacy_database_without_step04_columns_still_opens(self) -> None:
        accepted = store.MemoryStore(self.database_path)
        accepted.initialize()
        try:
            self._write_accepted_records(accepted)
            # The accepted baseline predates the additive envelope/run columns
            # on operations and the configuration columns on decisions.
            assert accepted.connection is not None
            accepted.connection.execute("ALTER TABLE operations RENAME TO operations_old")
            accepted.connection.execute(
                """
                CREATE TABLE operations (
                    operation_id TEXT PRIMARY KEY,
                    decision_id TEXT NOT NULL REFERENCES decisions(decision_id),
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    observed_invocation TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            accepted.connection.execute(
                """
                INSERT INTO operations (
                    operation_id, decision_id, kind, status, observed_invocation,
                    created_at, updated_at
                )
                SELECT operation_id, decision_id, kind, status, observed_invocation,
                       created_at, updated_at
                FROM operations_old
                """
            )
            accepted.connection.execute("DROP TABLE operations_old")
            accepted.connection.commit()
        finally:
            accepted.close()

        upgraded = store.MemoryStore(self.database_path)
        upgraded.initialize()
        try:
            assert upgraded.connection is not None
            columns = {
                row["name"]
                for row in upgraded.connection.execute("PRAGMA table_info(operations)")
            }
            self.assertIn("envelope_digest", columns)
            self.assertIn("run_id", columns)
        finally:
            upgraded.close()


if __name__ == "__main__":
    unittest.main()
