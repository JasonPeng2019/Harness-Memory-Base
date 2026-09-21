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


__all__ = [
    "MemoryStore", "StoreError", "OperationConflictError", "OutcomeConflictError",
]
