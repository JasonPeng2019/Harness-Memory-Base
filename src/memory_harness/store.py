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
        content_hash TEXT NOT NULL
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
        return self._trajectory_from_row(row)

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
        return [
            trajectory
            for row in rows
            if (trajectory := self._trajectory_from_row(row))["scope"] == expected_scope
        ]

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
        return [
            trajectory
            for row in rows
            if (trajectory := self._trajectory_from_row(row))["scope"] == expected_scope
        ]

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
                    created_at, content_hash, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
        case_ids: list[str] | None = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        """Advance a local operation without converting uncertainty into success."""

        existing = self.get_experience_ingestion(ingestion_id)
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
        updated["content_hash"] = contracts.content_hash(updated)
        contracts.validate_experience_ingestion(updated)
        connection = self._require_connection()
        with connection:
            connection.execute(
                """
                UPDATE experience_ingestions
                SET status = ?, case_ids = ?, error = ?, content_hash = ?, updated_at = ?
                WHERE ingestion_id = ?
                """,
                (
                    updated["status"],
                    json.dumps(updated["case_ids"], sort_keys=True),
                    updated["error"],
                    updated["content_hash"],
                    contracts.utc_now(),
                    ingestion_id,
                ),
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
        self, ingestion_id: str, case_receipts: list[Mapping[str, Any]]
    ) -> dict[str, Any]:
        """Atomically retain exact case receipts and mark their representation confirmed."""

        existing = self.get_experience_ingestion(ingestion_id)
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
            updated["content_hash"] = contracts.content_hash(updated)
            contracts.validate_experience_ingestion(updated)
            connection.execute(
                """
                UPDATE experience_ingestions
                SET status = ?, case_ids = ?, error = NULL, content_hash = ?, updated_at = ?
                WHERE ingestion_id = ?
                """,
                (
                    updated["status"],
                    json.dumps(updated["case_ids"], sort_keys=True),
                    updated["content_hash"],
                    contracts.utc_now(),
                    ingestion_id,
                ),
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
                    content_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
        return self._skill_approval_from_row(row)

    @staticmethod
    def _skill_approval_from_row(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        for field in ("scope", "recipients", "source_cases"):
            result[field] = json.loads(result[field])
        result["schema"] = contracts.SKILL_APPROVAL_SCHEMA
        contracts.validate_skill_approval(result)
        return result


__all__ = [
    "MemoryStore", "StoreError", "OperationConflictError", "OutcomeConflictError",
    "TrajectoryConflictError", "ExperienceConflictError",
]
