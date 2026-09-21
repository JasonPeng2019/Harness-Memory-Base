"""Small standard-library SQLite store for Stage-A decisions and operations."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Mapping

from . import contracts


class StoreError(RuntimeError):
    """The durable store could not be initialized or updated."""


class OutcomeConflictError(StoreError):
    """A different terminal outcome already exists for this decision."""


class OperationConflictError(StoreError):
    """An operation replay conflicts with its durable state."""


class TrajectoryConflictError(StoreError):
    """A reviewed trajectory replay conflicts with immutable local evidence."""


class ExperienceConflictError(StoreError):
    """An extraction receipt, case, candidate, or approval conflicts with evidence."""


class ProcedureConflictError(StoreError):
    """Trusted procedure evidence or its immutable source binding conflicts."""


class ProcedureDesignationConflictError(ProcedureConflictError):
    """A delayed designation or withdrawal lost its conditional generation race."""


_SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS decisions (
        decision_id TEXT PRIMARY KEY,
        task_card_digest TEXT NOT NULL,
        objective_id TEXT NOT NULL,
        route TEXT NOT NULL,
        plan_id TEXT NOT NULL,
        plan_state TEXT NOT NULL,
        plan_digest TEXT NOT NULL,
        strategy TEXT NOT NULL,
        configuration TEXT NOT NULL,
        configuration_digest TEXT NOT NULL,
        state TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS operations (
        operation_id TEXT PRIMARY KEY,
        decision_id TEXT NOT NULL REFERENCES decisions(decision_id),
        envelope_digest TEXT NOT NULL,
        run_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        status TEXT NOT NULL,
        observed_invocation TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS outcomes (
        outcome_id TEXT PRIMARY KEY,
        decision_id TEXT NOT NULL UNIQUE REFERENCES decisions(decision_id),
        task_card_digest TEXT NOT NULL,
        objective_id TEXT NOT NULL,
        plan_id TEXT NOT NULL,
        plan_digest TEXT NOT NULL,
        status TEXT NOT NULL,
        evidence_digest TEXT NOT NULL,
        linked_run_id TEXT NOT NULL,
        observed_at TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS review_receipts (
        review_receipt_id TEXT PRIMARY KEY,
        outcome_id TEXT NOT NULL UNIQUE REFERENCES outcomes(outcome_id),
        review_id TEXT NOT NULL,
        decision_id TEXT NOT NULL,
        task_card_digest TEXT NOT NULL,
        task_text TEXT NOT NULL,
        objective_id TEXT NOT NULL,
        run_id TEXT NOT NULL,
        plan_id TEXT NOT NULL,
        plan_digest TEXT NOT NULL,
        route TEXT NOT NULL,
        reviewed_by TEXT NOT NULL,
        state TEXT NOT NULL,
        evidence_refs TEXT NOT NULL,
        protected_source_refs TEXT NOT NULL,
        raw_evidence TEXT NOT NULL,
        failed_hypotheses TEXT NOT NULL,
        reviewed_at TEXT NOT NULL,
        content_hash TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS reviewed_trajectories (
        trajectory_id TEXT PRIMARY KEY,
        outcome_id TEXT NOT NULL UNIQUE REFERENCES outcomes(outcome_id),
        scope_digest TEXT NOT NULL,
        scope TEXT NOT NULL,
        task_card_digest TEXT NOT NULL,
        task_text TEXT NOT NULL,
        objective_id TEXT NOT NULL,
        decision_id TEXT NOT NULL,
        run_id TEXT NOT NULL,
        accepted_plan_id TEXT NOT NULL,
        accepted_plan_digest TEXT NOT NULL,
        route TEXT NOT NULL,
        status TEXT NOT NULL,
        review_receipt_id TEXT NOT NULL,
        review_id TEXT NOT NULL,
        review_receipt_digest TEXT NOT NULL,
        review_state TEXT NOT NULL,
        reviewed_by TEXT NOT NULL,
        reviewed_at TEXT NOT NULL,
        evidence_refs TEXT NOT NULL,
        protected_source_refs TEXT NOT NULL,
        failed_hypotheses TEXT NOT NULL,
        raw_evidence TEXT NOT NULL,
        evidence_digest TEXT NOT NULL,
        recorded_at TEXT NOT NULL,
        content_hash TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS experience_ingestions (
        ingestion_id TEXT PRIMARY KEY,
        trajectory_id TEXT NOT NULL UNIQUE REFERENCES reviewed_trajectories(trajectory_id),
        scope_digest TEXT NOT NULL,
        scope TEXT NOT NULL,
        destination TEXT NOT NULL,
        session_id TEXT NOT NULL,
        payload_digest TEXT NOT NULL,
        status TEXT NOT NULL,
        case_ids TEXT NOT NULL,
        error TEXT,
        version INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS experience_case_receipts (
        case_receipt_id TEXT PRIMARY KEY,
        case_id TEXT NOT NULL,
        trajectory_id TEXT NOT NULL REFERENCES reviewed_trajectories(trajectory_id),
        ingestion_id TEXT NOT NULL REFERENCES experience_ingestions(ingestion_id),
        scope_digest TEXT NOT NULL,
        scope TEXT NOT NULL,
        review_receipt_id TEXT NOT NULL,
        review_receipt_digest TEXT NOT NULL,
        source_case TEXT NOT NULL,
        source_case_digest TEXT NOT NULL,
        created_at TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        UNIQUE(case_id, scope_digest)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS generated_skill_candidates (
        candidate_id TEXT PRIMARY KEY,
        skill_id TEXT NOT NULL,
        origin TEXT NOT NULL,
        state TEXT NOT NULL,
        scope_digest TEXT NOT NULL,
        scope TEXT NOT NULL,
        content TEXT NOT NULL,
        content_digest TEXT NOT NULL,
        source_cases TEXT NOT NULL,
        metadata TEXT NOT NULL,
        created_at TEXT NOT NULL,
        content_hash TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS generated_skill_approvals (
        approval_id TEXT PRIMARY KEY,
        candidate_id TEXT NOT NULL REFERENCES generated_skill_candidates(candidate_id),
        skill_id TEXT NOT NULL,
        origin TEXT NOT NULL,
        scope_digest TEXT NOT NULL,
        scope TEXT NOT NULL,
        content_digest TEXT NOT NULL,
        issuer TEXT NOT NULL,
        recipients TEXT NOT NULL,
        source_cases TEXT NOT NULL,
        approved_at TEXT NOT NULL,
        authority_evidence TEXT NOT NULL,
        content_hash TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS procedure_revisions (
        revision_id TEXT PRIMARY KEY,
        logical_id TEXT NOT NULL,
        origin_scope_digest TEXT NOT NULL,
        record TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS procedure_approvals (
        approval_id TEXT PRIMARY KEY,
        revision_id TEXT NOT NULL REFERENCES procedure_revisions(revision_id),
        logical_id TEXT NOT NULL,
        record TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS procedure_representations (
        representation_id TEXT PRIMARY KEY,
        revision_id TEXT NOT NULL REFERENCES procedure_revisions(revision_id),
        logical_id TEXT NOT NULL,
        record TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS procedure_designations (
        designation_id TEXT PRIMARY KEY,
        logical_id TEXT NOT NULL,
        revision_id TEXT NOT NULL REFERENCES procedure_revisions(revision_id),
        partition_id TEXT NOT NULL,
        generation INTEGER NOT NULL,
        record TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(logical_id, partition_id, generation)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS procedure_withdrawals (
        withdrawal_id TEXT PRIMARY KEY,
        logical_id TEXT NOT NULL,
        revision_id TEXT NOT NULL,
        partition_id TEXT NOT NULL,
        generation INTEGER NOT NULL,
        record TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(logical_id, partition_id, generation)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS procedure_current_designations (
        logical_id TEXT NOT NULL,
        partition_id TEXT NOT NULL,
        current_id TEXT NOT NULL,
        revision_id TEXT NOT NULL,
        generation INTEGER NOT NULL,
        state TEXT NOT NULL,
        record TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY(logical_id, partition_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS procedure_publications (
        publication_id TEXT PRIMARY KEY,
        logical_id TEXT NOT NULL,
        revision_id TEXT NOT NULL REFERENCES procedure_revisions(revision_id),
        partition_id TEXT NOT NULL,
        designation_id TEXT NOT NULL,
        status TEXT NOT NULL,
        version INTEGER NOT NULL,
        record TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS procedure_revocations (
        revocation_id TEXT PRIMARY KEY,
        logical_id TEXT NOT NULL,
        revision_id TEXT NOT NULL UNIQUE REFERENCES procedure_revisions(revision_id),
        record TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS procedure_remote_operations (
        operation_id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        logical_id TEXT NOT NULL,
        revision_id TEXT NOT NULL,
        partition_id TEXT,
        payload_id TEXT NOT NULL,
        status TEXT NOT NULL,
        version INTEGER NOT NULL,
        record TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS procedure_exposures (
        exposure_id TEXT PRIMARY KEY,
        publication_id TEXT NOT NULL REFERENCES procedure_publications(publication_id),
        revision_id TEXT NOT NULL,
        record TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        delivered_at TEXT NOT NULL
    )
    """,
]


class MemoryStore:
    """A minimal durable store with explicit outcome conflict semantics."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.connection: sqlite3.Connection | None = None

    def __enter__(self) -> "MemoryStore":
        self.initialize()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def initialize(self) -> None:
        if self.connection is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.connection = sqlite3.connect(str(self.path), timeout=30.0)
            self.connection.row_factory = sqlite3.Row
            self.connection.execute("PRAGMA foreign_keys=ON")
            self.connection.execute("PRAGMA journal_mode=WAL")
            with self.connection:
                for statement in _SCHEMA:
                    self.connection.execute(statement)
                self._ensure_column("operations", "envelope_digest", "TEXT")
                self._ensure_column("operations", "run_id", "TEXT")
                self._ensure_column("outcomes", "task_card_digest", "TEXT")
                self._ensure_column("outcomes", "objective_id", "TEXT")
                self._ensure_column("decisions", "configuration", "TEXT")
                self._ensure_column("decisions", "configuration_digest", "TEXT")
                self._ensure_column(
                    "generated_skill_candidates",
                    "state",
                    "TEXT NOT NULL DEFAULT 'proposed'",
                )
                self._ensure_column(
                    "experience_ingestions",
                    "version",
                    "INTEGER NOT NULL DEFAULT 0",
                )
                self._ensure_column(
                    "generated_skill_approvals",
                    "authority_evidence",
                    "TEXT",
                )
                self._ensure_column("review_receipts", "task_text", "TEXT")
                self._ensure_column("review_receipts", "route", "TEXT")
        except sqlite3.Error as exc:
            if self.connection is not None:
                self.connection.close()
                self.connection = None
            raise StoreError(f"cannot initialize memory store {self.path}: {exc}") from exc

    def _ensure_column(self, table: str, column: str, declaration: str) -> None:
        connection = self._require_connection()
        columns = {
            str(row["name"])
            for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in columns:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

    def close(self) -> None:
        if self.connection is not None:
            self.connection.close()
            self.connection = None

    def _require_connection(self) -> sqlite3.Connection:
        if self.connection is None:
            raise StoreError("memory store is not initialized")
        return self.connection

    def record_decision(self, decision: Mapping[str, Any]) -> dict[str, Any]:
        contracts.validate_decision(decision)
        connection = self._require_connection()
        values = (
            decision["decision_id"],
            decision["task_card_digest"],
            decision["objective_id"],
            decision["route"],
            decision["plan_id"],
            decision["plan_state"],
            decision["plan_digest"],
            decision["strategy"],
            json.dumps(decision["configuration"], sort_keys=True),
            decision["configuration_digest"],
            decision["state"],
            decision["created_at"],
            decision["created_at"],
        )
        with connection:
            connection.execute(
                """
                INSERT INTO decisions (
                    decision_id, task_card_digest, objective_id, route, plan_id,
                    plan_state, plan_digest, strategy, configuration,
                    configuration_digest, state, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(decision_id) DO UPDATE SET
                    task_card_digest=excluded.task_card_digest,
                    objective_id=excluded.objective_id,
                    route=excluded.route,
                    plan_id=excluded.plan_id,
                    plan_state=excluded.plan_state,
                    plan_digest=excluded.plan_digest,
                    strategy=excluded.strategy,
                    configuration=excluded.configuration,
                    configuration_digest=excluded.configuration_digest,
                    state=excluded.state,
                    updated_at=excluded.updated_at
                """,
                values,
            )
        return self.get_decision(str(decision["decision_id"]))

    def get_decision(self, decision_id: str) -> dict[str, Any]:
        connection = self._require_connection()
        row = connection.execute(
            "SELECT * FROM decisions WHERE decision_id = ?", (decision_id,)
        ).fetchone()
        if row is None:
            raise StoreError(f"decision not found: {decision_id}")
        result = dict(row)
        if result.get("configuration"):
            result["configuration"] = json.loads(result["configuration"])
        return result

    def record_operation(self, operation: Mapping[str, Any]) -> dict[str, Any]:
        contracts.validate_operation(operation)
        connection = self._require_connection()
        operation_id = str(operation["operation_id"])
        existing_row = connection.execute(
            "SELECT * FROM operations WHERE operation_id = ?", (operation_id,)
        ).fetchone()
        if existing_row is not None:
            existing = self._operation_from_row(existing_row)
            for field in ("decision_id", "kind", "envelope_digest", "run_id"):
                if existing[field] != operation[field]:
                    raise OperationConflictError(
                        f"operation identity conflict for {operation_id}: {field} differs"
                    )
            if (
                existing["status"] == operation["status"]
                and existing["observed_invocation"] == operation.get("observed_invocation")
            ):
                return existing
            allowed = {
                ("pending", "ambiguous"),
                ("pending", "delivered"),
                ("ambiguous", "delivered"),
            }
            if (existing["status"], operation["status"]) not in allowed:
                raise OperationConflictError(
                    f"operation {operation_id} cannot transition from "
                    f"{existing['status']!r} to {operation['status']!r}"
                )
        observed = operation.get("observed_invocation")
        observed_json = json.dumps(observed, sort_keys=True) if observed is not None else None
        values = (
            operation["operation_id"],
            operation["decision_id"],
            operation["envelope_digest"],
            operation["run_id"],
            operation["kind"],
            operation["status"],
            observed_json,
            operation["created_at"],
            operation["created_at"],
        )
        with connection:
            connection.execute(
                """
                INSERT INTO operations (
                    operation_id, decision_id, envelope_digest, run_id,
                    kind, status, observed_invocation,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(operation_id) DO UPDATE SET
                    status=excluded.status,
                    observed_invocation=excluded.observed_invocation,
                    updated_at=excluded.updated_at
                """,
                values,
            )
        return self.get_operation(str(operation["operation_id"]))

    def create_operation(self, operation: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
        """Create an intent exactly once and report whether this call created it."""

        contracts.validate_operation(operation)
        connection = self._require_connection()
        observed = operation.get("observed_invocation")
        observed_json = json.dumps(observed, sort_keys=True) if observed is not None else None
        with connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO operations (
                    operation_id, decision_id, envelope_digest, run_id,
                    kind, status, observed_invocation, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    operation["operation_id"], operation["decision_id"],
                    operation["envelope_digest"], operation["run_id"],
                    operation["kind"], operation["status"], observed_json,
                    operation["created_at"], operation["created_at"],
                ),
            )
        return self.get_operation(str(operation["operation_id"])), cursor.rowcount == 1

    def get_operation(self, operation_id: str) -> dict[str, Any]:
        connection = self._require_connection()
        row = connection.execute(
            "SELECT * FROM operations WHERE operation_id = ?", (operation_id,)
        ).fetchone()
        if row is None:
            raise StoreError(f"operation not found: {operation_id}")
        return self._operation_from_row(row)

    @staticmethod
    def _operation_from_row(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        if result["observed_invocation"] is not None:
            result["observed_invocation"] = json.loads(result["observed_invocation"])
        return result

    def list_operations(self, decision_id: str) -> list[dict[str, Any]]:
        connection = self._require_connection()
        rows = connection.execute(
            "SELECT * FROM operations WHERE decision_id = ? ORDER BY created_at, operation_id",
            (decision_id,),
        ).fetchall()
        results: list[dict[str, Any]] = []
        for row in rows:
            results.append(self._operation_from_row(row))
        return results

    def record_outcome(self, outcome: Mapping[str, Any]) -> dict[str, Any]:
        contracts.validate_outcome(outcome)
        connection = self._require_connection()
        decision_id = str(outcome["decision_id"])
        existing_row = connection.execute(
            "SELECT * FROM outcomes WHERE decision_id = ?", (decision_id,)
        ).fetchone()
        if existing_row is not None:
            existing = dict(existing_row)
            if existing["outcome_id"] == outcome["outcome_id"]:
                return existing
            raise OutcomeConflictError(
                "terminal outcome conflict for decision "
                f"{decision_id}: existing {existing['outcome_id']}, incoming {outcome['outcome_id']}"
            )
        values = (
            outcome["outcome_id"],
            decision_id,
            outcome["task_card_digest"],
            outcome["objective_id"],
            outcome["plan_id"],
            outcome["plan_digest"],
            outcome["status"],
            outcome["evidence_digest"],
            outcome["linked_run_id"],
            outcome["observed_at"],
            outcome["observed_at"],
        )
        with connection:
            connection.execute(
                """
                INSERT INTO outcomes (
                    outcome_id, decision_id, task_card_digest, objective_id,
                    plan_id, plan_digest, status,
                    evidence_digest, linked_run_id, observed_at, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                values,
            )
        return self.get_outcome(decision_id)

    def get_outcome(self, decision_id: str) -> dict[str, Any]:
        connection = self._require_connection()
        row = connection.execute(
            "SELECT * FROM outcomes WHERE decision_id = ?", (decision_id,)
        ).fetchone()
        if row is None:
            raise StoreError(f"outcome not found for decision: {decision_id}")
        return dict(row)

    def record_review_receipt(self, review_receipt: Mapping[str, Any]) -> dict[str, Any]:
        """Persist the exact ROOT receipt that authorizes a later trajectory.

        This is durable retention of the existing review receipt, not a second
        review workflow.  A trajectory may only copy its protected narrative
        and failed hypotheses from this immutable local source.
        """

        contracts.validate_review_receipt(review_receipt)
        connection = self._require_connection()
        durable_outcome = connection.execute(
            "SELECT * FROM outcomes WHERE outcome_id = ?",
            (review_receipt["outcome_id"],),
        ).fetchone()
        if durable_outcome is None:
            raise StoreError(
                "review receipt outcome is not durable: "
                f"{review_receipt['outcome_id']}"
            )
        outcome = dict(durable_outcome)
        expected_outcome_fields = {
            "decision_id": review_receipt["decision_id"],
            "task_card_digest": review_receipt["task_card_digest"],
            "objective_id": review_receipt["objective_id"],
            "plan_id": review_receipt["plan_id"],
            "plan_digest": review_receipt["plan_digest"],
            "linked_run_id": review_receipt["run_id"],
            "observed_at": review_receipt["reviewed_at"],
        }
        for field, expected in expected_outcome_fields.items():
            if outcome[field] != expected:
                raise TrajectoryConflictError(
                    "review receipt does not match durable outcome "
                    f"for {field}"
                )
        receipt_id = str(review_receipt["review_receipt_id"])
        existing = connection.execute(
            "SELECT * FROM review_receipts WHERE review_receipt_id = ?",
            (receipt_id,),
        ).fetchone()
        if existing is not None:
            restored = self._review_receipt_from_row(existing)
            if restored["content_hash"] != review_receipt["content_hash"]:
                raise TrajectoryConflictError(
                    f"review receipt conflict for {receipt_id}"
                )
            return restored
        linked = connection.execute(
            "SELECT review_receipt_id FROM review_receipts WHERE outcome_id = ?",
            (review_receipt["outcome_id"],),
        ).fetchone()
        if linked is not None:
            raise TrajectoryConflictError(
                "terminal outcome already has a different durable review receipt: "
                f"{review_receipt['outcome_id']}"
            )
        with connection:
            connection.execute(
                """
                INSERT INTO review_receipts (
                    review_receipt_id, outcome_id, review_id, decision_id,
                    task_card_digest, task_text, objective_id, run_id, plan_id, plan_digest,
                    route,
                    reviewed_by, state, evidence_refs, protected_source_refs,
                    raw_evidence, failed_hypotheses, reviewed_at, content_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    review_receipt["review_receipt_id"],
                    review_receipt["outcome_id"],
                    review_receipt["review_id"],
                    review_receipt["decision_id"],
                    review_receipt["task_card_digest"],
                    review_receipt["task_text"],
                    review_receipt["objective_id"],
                    review_receipt["run_id"],
                    review_receipt["plan_id"],
                    review_receipt["plan_digest"],
                    review_receipt["route"],
                    review_receipt["reviewed_by"],
                    review_receipt["state"],
                    json.dumps(review_receipt["evidence_refs"], sort_keys=True),
                    json.dumps(review_receipt["protected_source_refs"], sort_keys=True),
                    review_receipt["raw_evidence"],
                    json.dumps(review_receipt["failed_hypotheses"], sort_keys=False),
                    review_receipt["reviewed_at"],
                    review_receipt["content_hash"],
                ),
            )
        return self.get_review_receipt(receipt_id)

    def get_review_receipt(self, review_receipt_id: str) -> dict[str, Any]:
        connection = self._require_connection()
        row = connection.execute(
            "SELECT * FROM review_receipts WHERE review_receipt_id = ?",
            (review_receipt_id,),
        ).fetchone()
        if row is None:
            raise StoreError(f"review receipt not found: {review_receipt_id}")
        return self._review_receipt_from_row(row)

    @staticmethod
    def _review_receipt_from_row(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        for field in ("evidence_refs", "protected_source_refs", "failed_hypotheses"):
            result[field] = json.loads(result[field])
        result["schema"] = contracts.REVIEW_RECEIPT_SCHEMA
        contracts.validate_review_receipt(result)
        return result

    def _durable_review_receipt_for_trajectory(
        self, trajectory: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Return the full receipt and reject flattened narrative substitution."""

        connection = self._require_connection()
        row = connection.execute(
            "SELECT * FROM review_receipts WHERE review_receipt_id = ?",
            (trajectory["review_receipt_id"],),
        ).fetchone()
        if row is None:
            raise TrajectoryConflictError(
                "reviewed trajectory requires a durable review receipt"
            )
        receipt = self._review_receipt_from_row(row)
        expected_fields = {
            "outcome_id": trajectory["outcome_id"],
            "decision_id": trajectory["decision_id"],
            "task_card_digest": trajectory["task_card_digest"],
            "task_text": trajectory["task_text"],
            "objective_id": trajectory["objective_id"],
            "run_id": trajectory["run_id"],
            "plan_id": trajectory["accepted_plan_id"],
            "plan_digest": trajectory["accepted_plan_digest"],
            "route": trajectory["route"],
            "review_id": trajectory["review_id"],
            "content_hash": trajectory["review_receipt_digest"],
            "state": trajectory["review_state"],
            "reviewed_by": trajectory["reviewed_by"],
            "reviewed_at": trajectory["reviewed_at"],
            "evidence_refs": trajectory["evidence_refs"],
            "protected_source_refs": trajectory["protected_source_refs"],
            "raw_evidence": trajectory["raw_evidence"],
            "failed_hypotheses": trajectory["failed_hypotheses"],
        }
        for field, expected in expected_fields.items():
            if receipt[field] != expected:
                raise TrajectoryConflictError(
                    "reviewed trajectory does not match durable review receipt "
                    f"for {field}"
                )
        return receipt

    def record_reviewed_trajectory(
        self, trajectory: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Persist immutable reviewed evidence before any optional extraction."""

        contracts.validate_reviewed_trajectory(trajectory)
        connection = self._require_connection()
        persisted_outcome = connection.execute(
            "SELECT * FROM outcomes WHERE outcome_id = ?",
            (trajectory["outcome_id"],),
        ).fetchone()
        if persisted_outcome is None:
            raise StoreError(
                "reviewed trajectory outcome is not durable: "
                f"{trajectory['outcome_id']}"
            )
        outcome = dict(persisted_outcome)
        expected_outcome_fields = {
            "decision_id": trajectory["decision_id"],
            "task_card_digest": trajectory["task_card_digest"],
            "objective_id": trajectory["objective_id"],
            "plan_id": trajectory["accepted_plan_id"],
            "plan_digest": trajectory["accepted_plan_digest"],
            "evidence_digest": trajectory["evidence_digest"],
            "linked_run_id": trajectory["run_id"],
            "observed_at": trajectory["recorded_at"],
        }
        for field, expected in expected_outcome_fields.items():
            if outcome[field] != expected:
                raise TrajectoryConflictError(
                    "reviewed trajectory does not match durable outcome "
                    f"for {field}"
                )
        expected_status = {
            "PASS": "reviewed_success",
            "FAIL": "reviewed_failure",
            "BLOCKED": "reviewed_failure",
        }.get(outcome["status"])
        if expected_status != trajectory["status"]:
            raise TrajectoryConflictError(
                "reviewed trajectory status does not match durable outcome"
            )
        self._durable_review_receipt_for_trajectory(trajectory)
        trajectory_id = str(trajectory["trajectory_id"])
        existing = connection.execute(
            "SELECT * FROM reviewed_trajectories WHERE trajectory_id = ?",
            (trajectory_id,),
        ).fetchone()
        if existing is not None:
            restored = self._trajectory_from_row(existing)
            if restored["content_hash"] != trajectory["content_hash"]:
                raise TrajectoryConflictError(
                    f"reviewed trajectory conflict for {trajectory_id}"
                )
            return restored
        linked = connection.execute(
            "SELECT trajectory_id FROM reviewed_trajectories WHERE outcome_id = ?",
            (trajectory["outcome_id"],),
        ).fetchone()
        if linked is not None:
            raise TrajectoryConflictError(
                "terminal outcome already has a different reviewed trajectory: "
                f"{trajectory['outcome_id']}"
            )
        values = (
            trajectory["trajectory_id"],
            trajectory["outcome_id"],
            trajectory["scope_digest"],
            json.dumps(trajectory["scope"], sort_keys=True),
            trajectory["task_card_digest"],
            trajectory["task_text"],
            trajectory["objective_id"],
            trajectory["decision_id"],
            trajectory["run_id"],
            trajectory["accepted_plan_id"],
            trajectory["accepted_plan_digest"],
            trajectory["route"],
            trajectory["status"],
            trajectory["review_receipt_id"],
            trajectory["review_id"],
            trajectory["review_receipt_digest"],
            trajectory["review_state"],
            trajectory["reviewed_by"],
            trajectory["reviewed_at"],
            json.dumps(trajectory["evidence_refs"], sort_keys=True),
            json.dumps(trajectory["protected_source_refs"], sort_keys=True),
            json.dumps(trajectory["failed_hypotheses"], sort_keys=False),
            trajectory["raw_evidence"],
            trajectory["evidence_digest"],
            trajectory["recorded_at"],
            trajectory["content_hash"],
        )
        with connection:
            connection.execute(
                """
                INSERT INTO reviewed_trajectories (
                    trajectory_id, outcome_id, scope_digest, scope,
                    task_card_digest, task_text, objective_id, decision_id, run_id,
                    accepted_plan_id, accepted_plan_digest, route, status,
                    review_receipt_id, review_id, review_receipt_digest, review_state,
                    reviewed_by, reviewed_at, evidence_refs,
                    protected_source_refs, failed_hypotheses, raw_evidence,
                    evidence_digest, recorded_at, content_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                values,
            )
        return self.get_reviewed_trajectory(trajectory_id)

    def get_reviewed_trajectory(self, trajectory_id: str) -> dict[str, Any]:
        connection = self._require_connection()
        row = connection.execute(
            "SELECT * FROM reviewed_trajectories WHERE trajectory_id = ?",
            (trajectory_id,),
        ).fetchone()
        if row is None:
            raise StoreError(f"reviewed trajectory not found: {trajectory_id}")
        trajectory = self._trajectory_from_row(row)
        self._durable_review_receipt_for_trajectory(trajectory)
        return trajectory

    def list_reviewed_trajectories(
        self, scope: Mapping[str, Any]
    ) -> list[dict[str, Any]]:
        connection = self._require_connection()
        expected_scope = contracts.normalize_experience_scope(scope)
        scope_digest = contracts.sha256_hex(expected_scope)
        rows = connection.execute(
            """
            SELECT * FROM reviewed_trajectories
            WHERE scope_digest = ?
            ORDER BY recorded_at, trajectory_id
            """,
            (scope_digest,),
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            trajectory = self._trajectory_from_row(row)
            self._durable_review_receipt_for_trajectory(trajectory)
            if trajectory["scope"] == expected_scope:
                result.append(trajectory)
        return result

    @staticmethod
    def _trajectory_from_row(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        for field in (
            "scope",
            "evidence_refs",
            "protected_source_refs",
            "failed_hypotheses",
        ):
            result[field] = json.loads(result[field])
        result["schema"] = contracts.REVIEWED_TRAJECTORY_SCHEMA
        contracts.validate_reviewed_trajectory(result)
        return result

    def list_recent_reviewed_trajectories(
        self, scope: Mapping[str, Any]
    ) -> list[dict[str, Any]]:
        """Return only local evidence whose EverOS representation is unconfirmed."""

        connection = self._require_connection()
        expected_scope = contracts.normalize_experience_scope(scope)
        scope_digest = contracts.sha256_hex(expected_scope)
        rows = connection.execute(
            """
            SELECT trajectory.* FROM reviewed_trajectories AS trajectory
            WHERE trajectory.scope_digest = ?
              AND NOT EXISTS (
                  SELECT 1 FROM experience_ingestions AS ingestion
                  WHERE ingestion.trajectory_id = trajectory.trajectory_id
                    AND ingestion.status = 'confirmed'
              )
            ORDER BY trajectory.recorded_at, trajectory.trajectory_id
            """,
            (scope_digest,),
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            trajectory = self._trajectory_from_row(row)
            self._durable_review_receipt_for_trajectory(trajectory)
            if trajectory["scope"] == expected_scope:
                result.append(trajectory)
        return result

    def create_experience_ingestion(
        self, ingestion: Mapping[str, Any]
    ) -> tuple[dict[str, Any], bool]:
        """Persist a representation-write intent exactly once before calling EverOS."""

        contracts.validate_experience_ingestion(ingestion)
        connection = self._require_connection()
        with connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO experience_ingestions (
                    ingestion_id, trajectory_id, scope_digest, scope, destination,
                    session_id, payload_digest, status, case_ids, error,
                    version, created_at, content_hash, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ingestion["ingestion_id"],
                    ingestion["trajectory_id"],
                    ingestion["scope_digest"],
                    json.dumps(ingestion["scope"], sort_keys=True),
                    ingestion["destination"],
                    ingestion["session_id"],
                    ingestion["payload_digest"],
                    ingestion["status"],
                    json.dumps(ingestion["case_ids"], sort_keys=True),
                    ingestion.get("error"),
                    ingestion["version"],
                    ingestion["created_at"],
                    ingestion["content_hash"],
                    ingestion["created_at"],
                ),
            )
        persisted = self.get_experience_ingestion(str(ingestion["ingestion_id"]))
        if cursor.rowcount == 0:
            for field in (
                "trajectory_id",
                "scope_digest",
                "destination",
                "session_id",
                "payload_digest",
            ):
                if persisted[field] != ingestion[field]:
                    raise ExperienceConflictError(
                        "experience ingestion identity conflict for "
                        f"{ingestion['ingestion_id']}: {field} differs"
                    )
        return persisted, cursor.rowcount == 1

    def get_experience_ingestion(self, ingestion_id: str) -> dict[str, Any]:
        connection = self._require_connection()
        row = connection.execute(
            "SELECT * FROM experience_ingestions WHERE ingestion_id = ?",
            (ingestion_id,),
        ).fetchone()
        if row is None:
            raise StoreError(f"experience ingestion not found: {ingestion_id}")
        return self._ingestion_from_row(row)

    def get_experience_ingestion_for_trajectory(
        self, trajectory_id: str
    ) -> dict[str, Any] | None:
        connection = self._require_connection()
        row = connection.execute(
            "SELECT * FROM experience_ingestions WHERE trajectory_id = ?",
            (trajectory_id,),
        ).fetchone()
        return self._ingestion_from_row(row) if row is not None else None

    def update_experience_ingestion(
        self,
        ingestion_id: str,
        *,
        status: str,
        expected_version: int,
        case_ids: list[str] | None = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        """Advance a local operation without converting uncertainty into success."""

        existing = self.get_experience_ingestion(ingestion_id)
        if (
            not isinstance(expected_version, int)
            or isinstance(expected_version, bool)
            or expected_version < 0
        ):
            raise ExperienceConflictError("experience ingestion version must be nonnegative")
        if existing["version"] != expected_version:
            raise ExperienceConflictError(
                f"stale experience ingestion update for {ingestion_id}"
            )
        allowed = {
            "pending": {"pending", "uncertain", "confirmed", "blocked"},
            "uncertain": {"uncertain", "confirmed", "blocked"},
            "blocked": {"blocked"},
            "confirmed": {"confirmed"},
        }
        if status not in allowed.get(existing["status"], set()):
            raise ExperienceConflictError(
                f"experience ingestion {ingestion_id} cannot transition from "
                f"{existing['status']!r} to {status!r}"
            )
        updated = dict(existing)
        updated["status"] = status
        if case_ids is not None:
            updated["case_ids"] = list(case_ids)
        updated["error"] = error
        updated["version"] = expected_version + 1
        updated["content_hash"] = contracts.content_hash(updated)
        contracts.validate_experience_ingestion(updated)
        connection = self._require_connection()
        with connection:
            cursor = connection.execute(
                """
                UPDATE experience_ingestions
                SET status = ?, case_ids = ?, error = ?, version = ?, content_hash = ?, updated_at = ?
                WHERE ingestion_id = ? AND version = ?
                """,
                (
                    updated["status"],
                    json.dumps(updated["case_ids"], sort_keys=True),
                    updated["error"],
                    updated["version"],
                    updated["content_hash"],
                    contracts.utc_now(),
                    ingestion_id,
                    expected_version,
                ),
            )
            if cursor.rowcount != 1:
                raise ExperienceConflictError(
                    f"stale experience ingestion update for {ingestion_id}"
                )
        return self.get_experience_ingestion(ingestion_id)

    @staticmethod
    def _ingestion_from_row(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["scope"] = json.loads(result["scope"])
        result["case_ids"] = json.loads(result["case_ids"])
        result["schema"] = contracts.EXPERIENCE_INGESTION_SCHEMA
        result.pop("updated_at", None)
        contracts.validate_experience_ingestion(result)
        return result

    def confirm_experience_ingestion(
        self,
        ingestion_id: str,
        case_receipts: list[Mapping[str, Any]],
        *,
        expected_version: int,
    ) -> dict[str, Any]:
        """Atomically retain exact case receipts and mark their representation confirmed."""

        existing = self.get_experience_ingestion(ingestion_id)
        if (
            not isinstance(expected_version, int)
            or isinstance(expected_version, bool)
            or expected_version < 0
        ):
            raise ExperienceConflictError("experience ingestion version must be nonnegative")
        if existing["version"] != expected_version:
            raise ExperienceConflictError(
                f"stale experience ingestion confirmation for {ingestion_id}"
            )
        if existing["status"] not in {"pending", "uncertain", "confirmed"}:
            raise ExperienceConflictError(
                f"blocked experience ingestion {ingestion_id} cannot be confirmed"
            )
        if not case_receipts:
            raise ExperienceConflictError("confirmed experience ingestion requires case receipts")
        normalized = [dict(receipt) for receipt in case_receipts]
        case_ids: list[str] = []
        for receipt in normalized:
            contracts.validate_case_receipt(receipt)
            if receipt["ingestion_id"] != ingestion_id:
                raise ExperienceConflictError("case receipt has a different ingestion id")
            if receipt["trajectory_id"] != existing["trajectory_id"]:
                raise ExperienceConflictError("case receipt has a different trajectory")
            if receipt["scope_digest"] != existing["scope_digest"]:
                raise ExperienceConflictError("case receipt has a different scope")
            if receipt["scope"] != existing["scope"]:
                raise ExperienceConflictError("case receipt has a different exact scope")
            case_ids.append(str(receipt["case_id"]))
        if len(case_ids) != len(set(case_ids)):
            raise ExperienceConflictError("case receipt ids must be unique")

        connection = self._require_connection()
        if existing["status"] == "confirmed":
            if sorted(case_ids) != existing["case_ids"]:
                raise ExperienceConflictError(
                    "confirmed experience ingestion cannot gain or lose case receipts"
                )
            for receipt in normalized:
                prior = connection.execute(
                    """
                    SELECT * FROM experience_case_receipts
                    WHERE case_id = ? AND scope_digest = ?
                    """,
                    (receipt["case_id"], receipt["scope_digest"]),
                ).fetchone()
                if (
                    prior is None
                    or self._case_receipt_from_row(prior)["content_hash"]
                    != receipt["content_hash"]
                ):
                    raise ExperienceConflictError(
                        "confirmed experience ingestion receipt changed for "
                        f"{receipt['case_id']}"
                    )
            return existing
        with connection:
            for receipt in normalized:
                prior = connection.execute(
                    """
                    SELECT * FROM experience_case_receipts
                    WHERE case_id = ? AND scope_digest = ?
                    """,
                    (receipt["case_id"], receipt["scope_digest"]),
                ).fetchone()
                if prior is not None:
                    restored = self._case_receipt_from_row(prior)
                    if restored["content_hash"] != receipt["content_hash"]:
                        raise ExperienceConflictError(
                            "case receipt conflict for case " f"{receipt['case_id']}"
                        )
                    continue
                connection.execute(
                    """
                    INSERT INTO experience_case_receipts (
                        case_receipt_id, case_id, trajectory_id, ingestion_id,
                        scope_digest, scope, review_receipt_id, review_receipt_digest,
                        source_case,
                        source_case_digest, created_at, content_hash
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        receipt["case_receipt_id"],
                        receipt["case_id"],
                        receipt["trajectory_id"],
                        receipt["ingestion_id"],
                        receipt["scope_digest"],
                        json.dumps(receipt["scope"], sort_keys=True),
                        receipt["review_receipt_id"],
                        receipt["review_receipt_digest"],
                        json.dumps(receipt["source_case"], sort_keys=True),
                        receipt["source_case_digest"],
                        receipt["created_at"],
                        receipt["content_hash"],
                    ),
                )
            updated = dict(existing)
            updated["status"] = "confirmed"
            updated["case_ids"] = sorted(case_ids)
            updated["error"] = None
            updated["version"] = expected_version + 1
            updated["content_hash"] = contracts.content_hash(updated)
            contracts.validate_experience_ingestion(updated)
            cursor = connection.execute(
                """
                UPDATE experience_ingestions
                SET status = ?, case_ids = ?, error = NULL, version = ?, content_hash = ?, updated_at = ?
                WHERE ingestion_id = ? AND version = ?
                """,
                (
                    updated["status"],
                    json.dumps(updated["case_ids"], sort_keys=True),
                    updated["version"],
                    updated["content_hash"],
                    contracts.utc_now(),
                    ingestion_id,
                    expected_version,
                ),
            )
            if cursor.rowcount != 1:
                raise ExperienceConflictError(
                    f"stale experience ingestion confirmation for {ingestion_id}"
                )
        return self.get_experience_ingestion(ingestion_id)

    def get_case_receipt(
        self, scope: Mapping[str, Any], case_id: str
    ) -> dict[str, Any]:
        connection = self._require_connection()
        expected_scope = contracts.normalize_experience_scope(scope)
        row = connection.execute(
            """
            SELECT * FROM experience_case_receipts
            WHERE case_id = ? AND scope_digest = ?
            """,
            (case_id, contracts.sha256_hex(expected_scope)),
        ).fetchone()
        if row is None:
            raise StoreError(f"case receipt not found for {case_id}")
        result = self._case_receipt_from_row(row)
        if result["scope"] != expected_scope:
            raise StoreError(f"case receipt is outside the requested scope: {case_id}")
        return result

    @staticmethod
    def _case_receipt_from_row(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["scope"] = json.loads(result["scope"])
        result["source_case"] = json.loads(result["source_case"])
        result["schema"] = contracts.CASE_RECEIPT_SCHEMA
        contracts.validate_case_receipt(result)
        return result

    def record_generated_skill_candidate(
        self, candidate: Mapping[str, Any]
    ) -> dict[str, Any]:
        contracts.validate_generated_skill_candidate(candidate)
        connection = self._require_connection()
        candidate_id = str(candidate["candidate_id"])
        existing = connection.execute(
            "SELECT * FROM generated_skill_candidates WHERE candidate_id = ?",
            (candidate_id,),
        ).fetchone()
        if existing is not None:
            restored = self._generated_skill_from_row(existing)
            identity_fields = (
                "skill_id",
                "origin",
                "state",
                "scope_digest",
                "scope",
                "content",
                "content_digest",
                "source_cases",
                "metadata",
            )
            if any(restored[field] != candidate[field] for field in identity_fields):
                raise ExperienceConflictError(
                    f"generated skill candidate conflict for {candidate_id}"
                )
            return restored
        with connection:
            connection.execute(
                """
                INSERT INTO generated_skill_candidates (
                    candidate_id, skill_id, origin, state, scope_digest, scope, content,
                    content_digest, source_cases, metadata, created_at, content_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    candidate["candidate_id"],
                    candidate["skill_id"],
                    candidate["origin"],
                    candidate["state"],
                    candidate["scope_digest"],
                    json.dumps(candidate["scope"], sort_keys=True),
                    candidate["content"],
                    candidate["content_digest"],
                    json.dumps(candidate["source_cases"], sort_keys=True),
                    json.dumps(candidate["metadata"], sort_keys=True),
                    candidate["created_at"],
                    candidate["content_hash"],
                ),
            )
        return self.get_generated_skill_candidate(candidate_id)

    def get_generated_skill_candidate(self, candidate_id: str) -> dict[str, Any]:
        connection = self._require_connection()
        row = connection.execute(
            "SELECT * FROM generated_skill_candidates WHERE candidate_id = ?",
            (candidate_id,),
        ).fetchone()
        if row is None:
            raise StoreError(f"generated skill candidate not found: {candidate_id}")
        return self._generated_skill_from_row(row)

    @staticmethod
    def _generated_skill_from_row(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        for field in ("scope", "source_cases", "metadata"):
            result[field] = json.loads(result[field])
        result["schema"] = contracts.GENERATED_SKILL_SCHEMA
        contracts.validate_generated_skill_candidate(result)
        return result

    def record_skill_approval(self, approval: Mapping[str, Any]) -> dict[str, Any]:
        contracts.validate_skill_approval(approval)
        connection = self._require_connection()
        self._durable_candidate_for_approval(approval)
        approval_id = str(approval["approval_id"])
        existing = connection.execute(
            "SELECT * FROM generated_skill_approvals WHERE approval_id = ?",
            (approval_id,),
        ).fetchone()
        if existing is not None:
            restored = self._skill_approval_from_row(existing)
            if restored["content_hash"] != approval["content_hash"]:
                raise ExperienceConflictError(f"skill approval conflict for {approval_id}")
            return restored
        with connection:
            connection.execute(
                """
                INSERT INTO generated_skill_approvals (
                    approval_id, candidate_id, skill_id, origin, scope_digest, scope,
                    content_digest, issuer, recipients, source_cases, approved_at,
                    authority_evidence, content_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    approval["approval_id"],
                    approval["candidate_id"],
                    approval["skill_id"],
                    approval["origin"],
                    approval["scope_digest"],
                    json.dumps(approval["scope"], sort_keys=True),
                    approval["content_digest"],
                    approval["issuer"],
                    json.dumps(approval["recipients"], sort_keys=True),
                    json.dumps(approval["source_cases"], sort_keys=True),
                    approval["approved_at"],
                    json.dumps(approval["authority_evidence"], sort_keys=True),
                    approval["content_hash"],
                ),
            )
        return self.get_skill_approval(approval_id)

    def get_skill_approval(self, approval_id: str) -> dict[str, Any]:
        connection = self._require_connection()
        row = connection.execute(
            "SELECT * FROM generated_skill_approvals WHERE approval_id = ?",
            (approval_id,),
        ).fetchone()
        if row is None:
            raise StoreError(f"skill approval not found: {approval_id}")
        approval = self._skill_approval_from_row(row)
        self._durable_candidate_for_approval(approval)
        return approval

    def _durable_candidate_for_approval(
        self, approval: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Rejoin an approval to its exact candidate before accepting it."""

        try:
            candidate = self.get_generated_skill_candidate(str(approval["candidate_id"]))
        except StoreError as exc:
            raise ExperienceConflictError(
                "skill approval cannot resolve its durable generated candidate"
            ) from exc
        expected_fields = {
            "skill_id": approval["skill_id"],
            "origin": approval["origin"],
            "scope_digest": approval["scope_digest"],
            "scope": approval["scope"],
            "content_digest": approval["content_digest"],
            "source_cases": approval["source_cases"],
        }
        for field, expected in expected_fields.items():
            if candidate[field] != expected:
                raise ExperienceConflictError(
                    "skill approval does not rejoin its exact durable candidate "
                    f"for {field}"
                )
        return candidate

    @staticmethod
    def _skill_approval_from_row(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        for field in ("scope", "recipients", "source_cases"):
            result[field] = json.loads(result[field])
        authority_evidence = result.get("authority_evidence")
        if not isinstance(authority_evidence, str):
            raise ExperienceConflictError(
                "skill approval has no retained authority evidence"
            )
        result["authority_evidence"] = json.loads(authority_evidence)
        result["schema"] = contracts.SKILL_APPROVAL_SCHEMA
        contracts.validate_skill_approval(result)
        return result

    # Trusted-procedure state lives in separate tables so Stage-A decisions,
    # dispatch operations, and outcomes remain readable without migration loss.

    @staticmethod
    def _stored_record(row: sqlite3.Row, validator: Any) -> dict[str, Any]:
        try:
            record = json.loads(str(row["record"]))
        except (TypeError, ValueError) as exc:
            raise ProcedureConflictError("stored procedure record is not valid JSON") from exc
        if not isinstance(record, dict):
            raise ProcedureConflictError("stored procedure record is not an object")
        try:
            validator(record)
        except contracts.ContractError as exc:
            raise ProcedureConflictError("stored procedure record violates its contract") from exc
        return record

    @staticmethod
    def _serialize_record(record: Mapping[str, Any]) -> str:
        return json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    def _validate_durable_procedure_source(
        self, procedure: Mapping[str, Any]
    ) -> None:
        """Rejoin generated provenance to the accepted Step-02 evidence.

        Curated/builtin origins retain their explicit provenance reference in
        the procedure record.  Generated material must additionally resolve
        the durable candidate and its already-verified source approval.
        """

        source = procedure["source"]
        if source.get("kind") != "generated_skill":
            return
        required = (
            "candidate_id",
            "candidate_digest",
            "skill_approval_id",
            "skill_approval_digest",
            "source_cases",
        )
        if any(not source.get(field) for field in required):
            raise ProcedureConflictError("generated procedure source lacks exact approval provenance")
        try:
            candidate = self.get_generated_skill_candidate(str(source["candidate_id"]))
            skill_approval = self.get_skill_approval(str(source["skill_approval_id"]))
        except StoreError as exc:
            raise ProcedureConflictError(
                "generated procedure source cannot resolve durable Step-02 evidence"
            ) from exc
        expected = {
            "candidate_digest": candidate["content_hash"],
            "skill_approval_digest": skill_approval["content_hash"],
            "source_cases": candidate["source_cases"],
        }
        if any(source.get(field) != value for field, value in expected.items()):
            raise ProcedureConflictError("generated procedure source provenance changed or is ambiguous")
        if skill_approval["candidate_id"] != candidate["candidate_id"]:
            raise ProcedureConflictError("generated procedure source approval names another candidate")
        if candidate["content"] != procedure["behavior"]["body"]:
            raise ProcedureConflictError("generated procedure body does not match its approved source candidate")

    def record_procedure_revision(self, procedure: Mapping[str, Any]) -> dict[str, Any]:
        contracts.validate_procedure_revision(procedure)
        self._validate_durable_procedure_source(procedure)
        connection = self._require_connection()
        revision_id = str(procedure["revision_id"])
        existing = connection.execute(
            "SELECT * FROM procedure_revisions WHERE revision_id = ?", (revision_id,)
        ).fetchone()
        if existing is not None:
            restored = self._stored_record(existing, contracts.validate_procedure_revision)
            if restored["content_hash"] != procedure["content_hash"]:
                raise ProcedureConflictError(f"procedure revision conflict for {revision_id}")
            return restored
        with connection:
            connection.execute(
                """
                INSERT INTO procedure_revisions (
                    revision_id, logical_id, origin_scope_digest, record, content_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    procedure["revision_id"],
                    procedure["logical_id"],
                    procedure["origin_scope_digest"],
                    self._serialize_record(procedure),
                    procedure["content_hash"],
                    procedure["created_at"],
                ),
            )
        return self.get_procedure_revision(revision_id)

    def get_procedure_revision(self, revision_id: str) -> dict[str, Any]:
        connection = self._require_connection()
        row = connection.execute(
            "SELECT * FROM procedure_revisions WHERE revision_id = ?", (revision_id,)
        ).fetchone()
        if row is None:
            raise StoreError(f"procedure revision not found: {revision_id}")
        procedure = self._stored_record(row, contracts.validate_procedure_revision)
        self._validate_durable_procedure_source(procedure)
        return procedure

    def record_procedure_approval(self, approval: Mapping[str, Any]) -> dict[str, Any]:
        contracts.validate_procedure_approval(approval)
        procedure = self.get_procedure_revision(str(approval["revision_id"]))
        try:
            contracts.validate_procedure_approval(approval, procedure=procedure)
        except contracts.ContractError as exc:
            raise ProcedureConflictError("procedure approval does not bind durable procedure") from exc
        connection = self._require_connection()
        approval_id = str(approval["approval_id"])
        existing = connection.execute(
            "SELECT * FROM procedure_approvals WHERE approval_id = ?", (approval_id,)
        ).fetchone()
        if existing is not None:
            restored = self._stored_record(existing, contracts.validate_procedure_approval)
            if restored["content_hash"] != approval["content_hash"]:
                raise ProcedureConflictError(f"procedure approval conflict for {approval_id}")
            return restored
        with connection:
            connection.execute(
                """
                INSERT INTO procedure_approvals (
                    approval_id, revision_id, logical_id, record, content_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    approval["approval_id"], approval["revision_id"], approval["logical_id"],
                    self._serialize_record(approval), approval["content_hash"], approval["approved_at"],
                ),
            )
        return self.get_procedure_approval(approval_id)

    def get_procedure_approval(self, approval_id: str) -> dict[str, Any]:
        connection = self._require_connection()
        row = connection.execute(
            "SELECT * FROM procedure_approvals WHERE approval_id = ?", (approval_id,)
        ).fetchone()
        if row is None:
            raise StoreError(f"procedure approval not found: {approval_id}")
        approval = self._stored_record(row, contracts.validate_procedure_approval)
        procedure = self.get_procedure_revision(str(approval["revision_id"]))
        try:
            contracts.validate_procedure_approval(approval, procedure=procedure)
        except contracts.ContractError as exc:
            raise ProcedureConflictError("stored approval does not bind durable procedure") from exc
        return approval

    def record_procedure_representation(
        self, representation: Mapping[str, Any]
    ) -> dict[str, Any]:
        contracts.validate_procedure_representation(representation)
        procedure = self.get_procedure_revision(str(representation["revision_id"]))
        try:
            contracts.validate_procedure_representation(representation, procedure=procedure)
        except contracts.ContractError as exc:
            raise ProcedureConflictError("representation does not bind durable procedure") from exc
        connection = self._require_connection()
        representation_id = str(representation["representation_id"])
        existing = connection.execute(
            "SELECT * FROM procedure_representations WHERE representation_id = ?",
            (representation_id,),
        ).fetchone()
        if existing is not None:
            restored = self._stored_record(existing, contracts.validate_procedure_representation)
            if restored["content_hash"] != representation["content_hash"]:
                raise ProcedureConflictError(
                    f"procedure representation conflict for {representation_id}"
                )
            return restored
        with connection:
            connection.execute(
                """
                INSERT INTO procedure_representations (
                    representation_id, revision_id, logical_id, record, content_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    representation["representation_id"], representation["revision_id"],
                    representation["logical_id"], self._serialize_record(representation),
                    representation["content_hash"], representation["created_at"],
                ),
            )
        return self.get_procedure_representation(representation_id)

    def get_procedure_representation(self, representation_id: str) -> dict[str, Any]:
        connection = self._require_connection()
        row = connection.execute(
            "SELECT * FROM procedure_representations WHERE representation_id = ?",
            (representation_id,),
        ).fetchone()
        if row is None:
            raise StoreError(f"procedure representation not found: {representation_id}")
        representation = self._stored_record(row, contracts.validate_procedure_representation)
        procedure = self.get_procedure_revision(str(representation["revision_id"]))
        try:
            contracts.validate_procedure_representation(representation, procedure=procedure)
        except contracts.ContractError as exc:
            raise ProcedureConflictError("stored representation does not bind durable procedure") from exc
        return representation

    def _current_procedure_from_row(self, row: sqlite3.Row) -> dict[str, Any]:
        try:
            source_record = json.loads(str(row["record"]))
        except (TypeError, ValueError) as exc:
            raise ProcedureConflictError("stored current procedure state is invalid") from exc
        if not isinstance(source_record, dict):
            raise ProcedureConflictError("stored current procedure state is not an object")
        return {
            "logical_id": row["logical_id"],
            "partition_id": row["partition_id"],
            "designation_id": row["current_id"],
            "revision_id": row["revision_id"],
            "generation": row["generation"],
            "state": row["state"],
            "record": source_record,
            "content_hash": row["content_hash"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def get_current_procedure_designation(
        self, logical_id: str, partition: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        normalized = contracts.normalize_procedure_partition(partition)
        partition_id = contracts.procedure_partition_id(normalized)
        connection = self._require_connection()
        row = connection.execute(
            """
            SELECT * FROM procedure_current_designations
            WHERE logical_id = ? AND partition_id = ?
            """,
            (logical_id, partition_id),
        ).fetchone()
        return self._current_procedure_from_row(row) if row is not None else None

    def get_procedure_designation(self, designation_id: str) -> dict[str, Any]:
        connection = self._require_connection()
        row = connection.execute(
            "SELECT * FROM procedure_designations WHERE designation_id = ?", (designation_id,)
        ).fetchone()
        if row is None:
            raise StoreError(f"procedure designation not found: {designation_id}")
        designation = self._stored_record(row, contracts.validate_procedure_designation)
        procedure = self.get_procedure_revision(str(designation["revision_id"]))
        approval = self.get_procedure_approval(str(designation["approval_id"]))
        try:
            contracts.validate_procedure_designation(
                designation, procedure=procedure, approval=approval
            )
        except contracts.ContractError as exc:
            raise ProcedureConflictError("stored designation lacks durable procedure evidence") from exc
        return designation

    def record_procedure_designation(
        self, designation: Mapping[str, Any]
    ) -> dict[str, Any]:
        contracts.validate_procedure_designation(designation)
        procedure = self.get_procedure_revision(str(designation["revision_id"]))
        approval = self.get_procedure_approval(str(designation["approval_id"]))
        try:
            contracts.validate_procedure_designation(
                designation, procedure=procedure, approval=approval
            )
        except contracts.ContractError as exc:
            raise ProcedureConflictError("designation does not bind durable evidence") from exc
        if self.is_procedure_revoked(str(designation["revision_id"])):
            raise ProcedureConflictError("a revoked procedure revision cannot become current")
        connection = self._require_connection()
        designation_id = str(designation["designation_id"])
        with connection:
            existing = connection.execute(
                "SELECT * FROM procedure_designations WHERE designation_id = ?",
                (designation_id,),
            ).fetchone()
            if existing is not None:
                restored = self._stored_record(existing, contracts.validate_procedure_designation)
                if restored["content_hash"] != designation["content_hash"]:
                    raise ProcedureDesignationConflictError(
                        f"procedure designation conflict for {designation_id}"
                    )
                return restored
            current_row = connection.execute(
                """
                SELECT * FROM procedure_current_designations
                WHERE logical_id = ? AND partition_id = ?
                """,
                (designation["logical_id"], designation["partition_id"]),
            ).fetchone()
            generation = int(designation["generation"])
            if current_row is None:
                if generation != 1 or designation["predecessor_generation"] is not None:
                    raise ProcedureDesignationConflictError(
                        "initial designation must begin at generation one"
                    )
                connection.execute(
                    """
                    INSERT INTO procedure_current_designations (
                        logical_id, partition_id, current_id, revision_id, generation, state,
                        record, content_hash, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?, ?)
                    """,
                    (
                        designation["logical_id"], designation["partition_id"],
                        designation["designation_id"], designation["revision_id"], generation,
                        self._serialize_record(designation), designation["content_hash"],
                        designation["created_at"], designation["created_at"],
                    ),
                )
            else:
                current = self._current_procedure_from_row(current_row)
                if generation <= int(current["generation"]):
                    raise ProcedureDesignationConflictError(
                        "delayed procedure designation cannot replace newer current state"
                    )
                if designation["predecessor_generation"] != current["generation"]:
                    raise ProcedureDesignationConflictError(
                        "procedure designation predecessor is not current"
                    )
                cursor = connection.execute(
                    """
                    UPDATE procedure_current_designations
                    SET current_id = ?, revision_id = ?, generation = ?, state = 'active',
                        record = ?, content_hash = ?, updated_at = ?
                    WHERE logical_id = ? AND partition_id = ? AND generation = ?
                    """,
                    (
                        designation["designation_id"], designation["revision_id"], generation,
                        self._serialize_record(designation), designation["content_hash"],
                        designation["created_at"], designation["logical_id"],
                        designation["partition_id"], current["generation"],
                    ),
                )
                if cursor.rowcount != 1:
                    raise ProcedureDesignationConflictError("stale procedure designation update")
            connection.execute(
                """
                INSERT INTO procedure_designations (
                    designation_id, logical_id, revision_id, partition_id, generation,
                    record, content_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    designation["designation_id"], designation["logical_id"], designation["revision_id"],
                    designation["partition_id"], designation["generation"],
                    self._serialize_record(designation), designation["content_hash"], designation["created_at"],
                ),
            )
        return self.get_procedure_designation(designation_id)

    def get_procedure_withdrawal(self, withdrawal_id: str) -> dict[str, Any]:
        connection = self._require_connection()
        row = connection.execute(
            "SELECT * FROM procedure_withdrawals WHERE withdrawal_id = ?", (withdrawal_id,)
        ).fetchone()
        if row is None:
            raise StoreError(f"procedure withdrawal not found: {withdrawal_id}")
        return self._stored_record(row, contracts.validate_procedure_withdrawal)

    def record_procedure_withdrawal(
        self, withdrawal: Mapping[str, Any]
    ) -> dict[str, Any]:
        contracts.validate_procedure_withdrawal(withdrawal)
        connection = self._require_connection()
        withdrawal_id = str(withdrawal["withdrawal_id"])
        with connection:
            existing = connection.execute(
                "SELECT * FROM procedure_withdrawals WHERE withdrawal_id = ?",
                (withdrawal_id,),
            ).fetchone()
            if existing is not None:
                restored = self._stored_record(existing, contracts.validate_procedure_withdrawal)
                if restored["content_hash"] != withdrawal["content_hash"]:
                    raise ProcedureDesignationConflictError(
                        f"procedure withdrawal conflict for {withdrawal_id}"
                    )
                return restored
            current_row = connection.execute(
                """
                SELECT * FROM procedure_current_designations
                WHERE logical_id = ? AND partition_id = ?
                """,
                (withdrawal["logical_id"], withdrawal["partition_id"]),
            ).fetchone()
            if current_row is None:
                raise ProcedureDesignationConflictError("withdrawal has no current designation")
            current = self._current_procedure_from_row(current_row)
            if current["state"] != "active":
                raise ProcedureDesignationConflictError("procedure partition is already withdrawn")
            if (
                current["designation_id"] != withdrawal["predecessor_designation_id"]
                or current["generation"] != withdrawal["predecessor_generation"]
                or current["revision_id"] != withdrawal["revision_id"]
            ):
                raise ProcedureDesignationConflictError(
                    "withdrawal predecessor is no longer the current designation"
                )
            cursor = connection.execute(
                """
                UPDATE procedure_current_designations
                SET current_id = ?, generation = ?, state = 'withdrawn', record = ?,
                    content_hash = ?, updated_at = ?
                WHERE logical_id = ? AND partition_id = ? AND generation = ? AND state = 'active'
                """,
                (
                    withdrawal["withdrawal_id"], withdrawal["generation"],
                    self._serialize_record(withdrawal), withdrawal["content_hash"],
                    withdrawal["created_at"], withdrawal["logical_id"], withdrawal["partition_id"],
                    withdrawal["predecessor_generation"],
                ),
            )
            if cursor.rowcount != 1:
                raise ProcedureDesignationConflictError("stale procedure withdrawal update")
            connection.execute(
                """
                INSERT INTO procedure_withdrawals (
                    withdrawal_id, logical_id, revision_id, partition_id, generation,
                    record, content_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    withdrawal["withdrawal_id"], withdrawal["logical_id"], withdrawal["revision_id"],
                    withdrawal["partition_id"], withdrawal["generation"],
                    self._serialize_record(withdrawal), withdrawal["content_hash"], withdrawal["created_at"],
                ),
            )
            self._fence_publications_in_transaction(
                connection,
                logical_id=str(withdrawal["logical_id"]),
                partition_id=str(withdrawal["partition_id"]),
                status="fenced",
            )
        return self.get_procedure_withdrawal(withdrawal_id)

    def _fence_publications_in_transaction(
        self,
        connection: sqlite3.Connection,
        *,
        logical_id: str,
        partition_id: str | None = None,
        revision_id: str | None = None,
        status: str,
    ) -> None:
        clauses = ["logical_id = ?"]
        values: list[Any] = [logical_id]
        if partition_id is not None:
            clauses.append("partition_id = ?")
            values.append(partition_id)
        if revision_id is not None:
            clauses.append("revision_id = ?")
            values.append(revision_id)
        rows = connection.execute(
            "SELECT * FROM procedure_publications WHERE " + " AND ".join(clauses), values
        ).fetchall()
        for row in rows:
            existing = self._stored_record(row, contracts.validate_procedure_publication)
            if existing["status"] in {"revoked", "withdrawn", "blocked", "revocation_pending"}:
                continue
            updated = contracts.revise_procedure_publication(existing, status=status)
            connection.execute(
                """
                UPDATE procedure_publications
                SET status = ?, version = ?, record = ?, content_hash = ?, updated_at = ?
                WHERE publication_id = ? AND version = ?
                """,
                (
                    updated["status"], updated["version"], self._serialize_record(updated),
                    updated["content_hash"], contracts.utc_now(), updated["publication_id"],
                    existing["version"],
                ),
            )

    def get_procedure_publication(self, publication_id: str) -> dict[str, Any]:
        connection = self._require_connection()
        row = connection.execute(
            "SELECT * FROM procedure_publications WHERE publication_id = ?", (publication_id,)
        ).fetchone()
        if row is None:
            raise StoreError(f"procedure publication not found: {publication_id}")
        return self._stored_record(row, contracts.validate_procedure_publication)

    def list_procedure_publications(
        self,
        *,
        logical_id: str | None = None,
        revision_id: str | None = None,
        partition: Mapping[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        connection = self._require_connection()
        clauses: list[str] = []
        values: list[Any] = []
        if logical_id is not None:
            clauses.append("logical_id = ?")
            values.append(logical_id)
        if revision_id is not None:
            clauses.append("revision_id = ?")
            values.append(revision_id)
        if partition is not None:
            clauses.append("partition_id = ?")
            values.append(contracts.procedure_partition_id(partition))
        query = "SELECT * FROM procedure_publications"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at, publication_id"
        return [
            self._stored_record(row, contracts.validate_procedure_publication)
            for row in connection.execute(query, values).fetchall()
        ]

    def create_procedure_publication(
        self, publication: Mapping[str, Any]
    ) -> tuple[dict[str, Any], bool]:
        """Durably create a publication intent before any remote side effect."""

        contracts.validate_procedure_publication(publication)
        procedure = self.get_procedure_revision(str(publication["revision_id"]))
        approval = self.get_procedure_approval(str(publication["approval"]["approval_id"]))
        representation = self.get_procedure_representation(
            str(publication["representation"]["representation_id"])
        )
        designation = self.get_procedure_designation(
            str(publication["designation"]["designation_id"])
        )
        expected = contracts.make_procedure_publication(
            procedure=procedure,
            approval=approval,
            representation=representation,
            designation=designation,
            created_at=publication["created_at"],
        )
        if expected["payload_digest"] != publication["payload_digest"]:
            raise ProcedureConflictError("publication does not match durable procedure evidence")
        current = self.get_current_procedure_designation(
            str(publication["logical_id"]), publication["partition"]
        )
        if current is None or current["state"] != "active":
            raise ProcedureConflictError("publication partition has no active current designation")
        if (
            current["designation_id"] != publication["designation"]["designation_id"]
            or current["generation"] != publication["designation"]["generation"]
            or current["revision_id"] != publication["revision_id"]
        ):
            raise ProcedureDesignationConflictError(
                "publication designation is no longer current"
            )
        if self.is_procedure_revoked(str(publication["revision_id"])):
            raise ProcedureConflictError("revoked procedure revision cannot be published")
        connection = self._require_connection()
        publication_id = str(publication["publication_id"])
        with connection:
            existing = connection.execute(
                "SELECT * FROM procedure_publications WHERE publication_id = ?",
                (publication_id,),
            ).fetchone()
            if existing is not None:
                restored = self._stored_record(existing, contracts.validate_procedure_publication)
                if restored["payload_digest"] != publication["payload_digest"]:
                    raise ProcedureConflictError(
                        f"procedure publication conflict for {publication_id}"
                    )
                return restored, False
            connection.execute(
                """
                INSERT INTO procedure_publications (
                    publication_id, logical_id, revision_id, partition_id, designation_id,
                    status, version, record, content_hash, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    publication["publication_id"], publication["logical_id"], publication["revision_id"],
                    publication["partition_id"], publication["designation"]["designation_id"],
                    publication["status"], publication["version"], self._serialize_record(publication),
                    publication["content_hash"], publication["created_at"], publication["created_at"],
                ),
            )
        return self.get_procedure_publication(publication_id), True

    def update_procedure_publication(
        self,
        publication_id: str,
        *,
        status: str,
        expected_version: int,
        remote_receipt: Mapping[str, Any] | None = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        existing = self.get_procedure_publication(publication_id)
        if existing["version"] != expected_version:
            raise ProcedureConflictError(f"stale procedure publication update for {publication_id}")
        allowed = {
            "intent": {"ambiguous", "remote_committed", "fenced", "withdrawn", "revoked", "blocked", "revocation_pending"},
            "ambiguous": {"remote_committed", "fenced", "withdrawn", "revoked", "blocked", "revocation_pending"},
            "remote_committed": {"acknowledged", "fenced", "withdrawn", "revoked", "revocation_pending"},
            "acknowledged": {"fenced", "withdrawn", "revoked", "revocation_pending"},
            "fenced": {"withdrawn", "revoked", "revocation_pending"},
            "revocation_pending": {"revoked", "withdrawn"},
            "withdrawn": {"revoked"},
            "revoked": set(),
            "blocked": set(),
        }
        if status == existing["status"] and (
            remote_receipt == existing.get("remote_receipt") and error == existing.get("error")
        ):
            return existing
        if status not in allowed.get(existing["status"], set()):
            raise ProcedureConflictError(
                f"procedure publication {publication_id} cannot transition from "
                f"{existing['status']!r} to {status!r}"
            )
        updated = contracts.revise_procedure_publication(
            existing, status=status, remote_receipt=remote_receipt, error=error
        )
        connection = self._require_connection()
        with connection:
            cursor = connection.execute(
                """
                UPDATE procedure_publications
                SET status = ?, version = ?, record = ?, content_hash = ?, updated_at = ?
                WHERE publication_id = ? AND version = ?
                """,
                (
                    updated["status"], updated["version"], self._serialize_record(updated),
                    updated["content_hash"], contracts.utc_now(), publication_id, expected_version,
                ),
            )
            if cursor.rowcount != 1:
                raise ProcedureConflictError(
                    f"stale procedure publication update for {publication_id}"
                )
        return self.get_procedure_publication(publication_id)

    def get_procedure_revocation(self, revision_id: str) -> dict[str, Any] | None:
        connection = self._require_connection()
        row = connection.execute(
            "SELECT * FROM procedure_revocations WHERE revision_id = ?", (revision_id,)
        ).fetchone()
        return self._stored_record(row, contracts.validate_procedure_revocation) if row is not None else None

    def is_procedure_revoked(self, revision_id: str) -> bool:
        return self.get_procedure_revocation(revision_id) is not None

    def record_procedure_revocation(
        self, revocation: Mapping[str, Any]
    ) -> dict[str, Any]:
        contracts.validate_procedure_revocation(revocation)
        procedure = self.get_procedure_revision(str(revocation["revision_id"]))
        try:
            contracts.validate_procedure_revocation(revocation, procedure=procedure)
        except contracts.ContractError as exc:
            raise ProcedureConflictError("revocation does not bind durable procedure") from exc
        connection = self._require_connection()
        revocation_id = str(revocation["revocation_id"])
        with connection:
            prior_revision = connection.execute(
                "SELECT * FROM procedure_revocations WHERE revision_id = ?",
                (revocation["revision_id"],),
            ).fetchone()
            if prior_revision is not None:
                restored = self._stored_record(prior_revision, contracts.validate_procedure_revocation)
                if restored["content_hash"] != revocation["content_hash"]:
                    raise ProcedureConflictError(
                        "procedure revision already has a different revocation tombstone"
                    )
                return restored
            connection.execute(
                """
                INSERT INTO procedure_revocations (
                    revocation_id, logical_id, revision_id, record, content_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    revocation["revocation_id"], revocation["logical_id"], revocation["revision_id"],
                    self._serialize_record(revocation), revocation["content_hash"], revocation["created_at"],
                ),
            )
            self._fence_publications_in_transaction(
                connection,
                logical_id=str(revocation["logical_id"]),
                revision_id=str(revocation["revision_id"]),
                status="revocation_pending",
            )
        restored = self.get_procedure_revocation(str(revocation["revision_id"]))
        assert restored is not None
        return restored

    def get_procedure_remote_operation(self, operation_id: str) -> dict[str, Any]:
        connection = self._require_connection()
        row = connection.execute(
            "SELECT * FROM procedure_remote_operations WHERE operation_id = ?", (operation_id,)
        ).fetchone()
        if row is None:
            raise StoreError(f"procedure remote operation not found: {operation_id}")
        return self._stored_record(row, contracts.validate_procedure_remote_operation)

    def create_procedure_remote_operation(
        self, operation: Mapping[str, Any]
    ) -> tuple[dict[str, Any], bool]:
        contracts.validate_procedure_remote_operation(operation)
        connection = self._require_connection()
        operation_id = str(operation["operation_id"])
        with connection:
            existing = connection.execute(
                "SELECT * FROM procedure_remote_operations WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            if existing is not None:
                restored = self._stored_record(existing, contracts.validate_procedure_remote_operation)
                immutable_fields = (
                    "kind", "logical_id", "revision_id", "partition_id", "payload_id", "payload_digest",
                )
                if any(restored[field] != operation[field] for field in immutable_fields):
                    raise ProcedureConflictError(
                        f"procedure remote operation conflict for {operation_id}"
                    )
                return restored, False
            connection.execute(
                """
                INSERT INTO procedure_remote_operations (
                    operation_id, kind, logical_id, revision_id, partition_id, payload_id,
                    status, version, record, content_hash, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    operation["operation_id"], operation["kind"], operation["logical_id"],
                    operation["revision_id"], operation["partition_id"], operation["payload_id"],
                    operation["status"], operation["version"], self._serialize_record(operation),
                    operation["content_hash"], operation["created_at"], operation["created_at"],
                ),
            )
        return self.get_procedure_remote_operation(operation_id), True

    def update_procedure_remote_operation(
        self,
        operation_id: str,
        *,
        status: str,
        expected_version: int,
        remote_receipt: Mapping[str, Any] | None = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        existing = self.get_procedure_remote_operation(operation_id)
        if existing["version"] != expected_version:
            raise ProcedureConflictError(f"stale procedure remote operation update for {operation_id}")
        allowed = {
            "intent": {"ambiguous", "remote_committed", "fenced", "withdrawn", "revoked", "blocked"},
            "ambiguous": {"remote_committed", "fenced", "withdrawn", "revoked", "blocked"},
            "remote_committed": {"acknowledged", "fenced", "withdrawn", "revoked"},
            "acknowledged": {"fenced", "withdrawn", "revoked"},
            "fenced": {"withdrawn", "revoked"},
            "withdrawn": {"revoked"},
            "revoked": set(),
            # A stable operation that was blocked locally can be reconciled
            # only by later exact remote evidence, which promotes it through
            # remote_committed before a normal acknowledgement.
            "blocked": {"remote_committed", "fenced", "withdrawn", "revoked"},
            "revocation_pending": {"revoked", "withdrawn"},
        }
        if status == existing["status"] and (
            remote_receipt == existing.get("remote_receipt") and error == existing.get("error")
        ):
            return existing
        if status not in allowed.get(existing["status"], set()):
            raise ProcedureConflictError(
                f"procedure remote operation {operation_id} cannot transition from "
                f"{existing['status']!r} to {status!r}"
            )
        updated = contracts.revise_procedure_remote_operation(
            existing, status=status, remote_receipt=remote_receipt, error=error
        )
        connection = self._require_connection()
        with connection:
            cursor = connection.execute(
                """
                UPDATE procedure_remote_operations
                SET status = ?, version = ?, record = ?, content_hash = ?, updated_at = ?
                WHERE operation_id = ? AND version = ?
                """,
                (
                    updated["status"], updated["version"], self._serialize_record(updated),
                    updated["content_hash"], contracts.utc_now(), operation_id, expected_version,
                ),
            )
            if cursor.rowcount != 1:
                raise ProcedureConflictError(
                    f"stale procedure remote operation update for {operation_id}"
                )
        return self.get_procedure_remote_operation(operation_id)

    def list_procedure_remote_operations(
        self,
        *,
        revision_id: str | None = None,
        payload_id: str | None = None,
    ) -> list[dict[str, Any]]:
        connection = self._require_connection()
        clauses: list[str] = []
        values: list[Any] = []
        if revision_id is not None:
            clauses.append("revision_id = ?")
            values.append(revision_id)
        if payload_id is not None:
            clauses.append("payload_id = ?")
            values.append(payload_id)
        query = "SELECT * FROM procedure_remote_operations"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at, operation_id"
        return [
            self._stored_record(row, contracts.validate_procedure_remote_operation)
            for row in connection.execute(query, values).fetchall()
        ]

    def record_procedure_exposure(self, exposure: Mapping[str, Any]) -> dict[str, Any]:
        contracts.validate_procedure_exposure(exposure)
        publication = self.get_procedure_publication(str(exposure["publication_id"]))
        if (
            publication["logical_id"] != exposure["logical_id"]
            or publication["revision_id"] != exposure["revision_id"]
        ):
            raise ProcedureConflictError("procedure exposure does not bind its publication")
        connection = self._require_connection()
        exposure_id = str(exposure["exposure_id"])
        existing = connection.execute(
            "SELECT * FROM procedure_exposures WHERE exposure_id = ?", (exposure_id,)
        ).fetchone()
        if existing is not None:
            restored = self._stored_record(existing, contracts.validate_procedure_exposure)
            if restored["content_hash"] != exposure["content_hash"]:
                raise ProcedureConflictError(f"procedure exposure conflict for {exposure_id}")
            return restored
        with connection:
            connection.execute(
                """
                INSERT INTO procedure_exposures (
                    exposure_id, publication_id, revision_id, record, content_hash, delivered_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    exposure["exposure_id"], exposure["publication_id"], exposure["revision_id"],
                    self._serialize_record(exposure), exposure["content_hash"], exposure["delivered_at"],
                ),
            )
        return self.get_procedure_exposure(exposure_id)

    def get_procedure_exposure(self, exposure_id: str) -> dict[str, Any]:
        connection = self._require_connection()
        row = connection.execute(
            "SELECT * FROM procedure_exposures WHERE exposure_id = ?", (exposure_id,)
        ).fetchone()
        if row is None:
            raise StoreError(f"procedure exposure not found: {exposure_id}")
        return self._stored_record(row, contracts.validate_procedure_exposure)

    def list_procedure_exposures(self, revision_id: str) -> list[dict[str, Any]]:
        connection = self._require_connection()
        rows = connection.execute(
            """
            SELECT * FROM procedure_exposures
            WHERE revision_id = ?
            ORDER BY delivered_at, exposure_id
            """,
            (revision_id,),
        ).fetchall()
        return [self._stored_record(row, contracts.validate_procedure_exposure) for row in rows]


__all__ = [
    "MemoryStore", "StoreError", "OperationConflictError", "OutcomeConflictError",
    "TrajectoryConflictError", "ExperienceConflictError", "ProcedureConflictError",
    "ProcedureDesignationConflictError",
]
