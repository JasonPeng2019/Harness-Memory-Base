"""Bounded optional-memory search over the accepted store adapters.

One logical preparation owns one absolute deadline.  Every enabled store gets
a bounded, independent chance to start inside its own sub-budget; a slow or
malformed store is isolated and valid completed results from another store are
preserved.  Candidates are normalized and gated *before* ranking, and one
logical revision is delivered once no matter how many stores saw it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Sequence

from . import contracts
from .config import PreparationLimits
from .privacy import PrivacyPolicy, safe_query_payload


class SearchError(RuntimeError):
    """A bounded search invariant was violated."""


class UnknownKindError(SearchError):
    """A store declared a candidate kind the coordinator does not own."""


@dataclass(frozen=True)
class SearchStore:
    """One bounded, enabled optional store adapter."""

    store_id: str
    kind: str
    query: Callable[[Mapping[str, Any]], Iterable[Mapping[str, Any]]]
    scope: Mapping[str, Any] | None = None
    freshness: str = "live"
    specificity: str = "project"

    def __post_init__(self) -> None:
        if not isinstance(self.store_id, str) or not self.store_id.strip():
            raise SearchError("store_id must be a nonempty string")
        if self.kind not in contracts.CANDIDATE_KINDS:
            raise UnknownKindError(f"unknown candidate kind: {self.kind!r}")
        if not callable(self.query):
            raise SearchError("store query must be callable")


@dataclass(frozen=True)
class SearchResult:
    outcome: str
    candidates: list[dict[str, Any]]
    attempts: list[dict[str, Any]]
    rounds: int
    delivered: list[str]

    @property
    def delivered_candidates(self) -> list[dict[str, Any]]:
        return [item for item in self.candidates if item["disposition"] == "selected"]


_SPECIFICITY_ORDER = {"private": 0, "local": 1, "project": 2, "shared": 3}
_KIND_ORDER = {"procedure": 0, "historical_evidence": 1, "template": 2}


class BoundedSearch:
    """Run one bounded, fair, gated, and deduplicated optional search."""

    def __init__(
        self,
        *,
        limits: PreparationLimits | None = None,
        privacy_policy: PrivacyPolicy | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.limits = limits or PreparationLimits()
        self.privacy_policy = privacy_policy or PrivacyPolicy()
        self.clock = clock or time.monotonic

    def run(
        self,
        *,
        objective: Mapping[str, Any],
        stores: Sequence[SearchStore],
        route: str,
        stage_seconds: float,
        rounds: int,
    ) -> SearchResult:
        if not isinstance(objective, Mapping):
            raise SearchError("objective representation must be an object")
        if route not in contracts.ROUTES:
            raise SearchError(f"unknown route: {route!r}")
        if stage_seconds < 0:
            raise SearchError("stage allowance must not be negative")
        enabled_kinds = {store.kind for store in stores}
        capacity = {
            kind: self.limits.capacity_for(kind) for kind in enabled_kinds
        }
        used = {kind: 0 for kind in enabled_kinds}
        attempts: list[dict[str, Any]] = []
        collected: list[dict[str, Any]] = []
        completed_rounds = 0
        # Each enabled store is scheduled a bounded slice from the enclosing
        # stage allowance.  One slow store consumes only its own scheduled
        # slice, so it can never remove another store's chance to start.
        scheduled = 0.0
        for round_index in range(max(1, int(rounds))):
            if scheduled >= float(stage_seconds):
                break
            completed_rounds += 1
            for store in stores:
                slice_budget = min(self.limits.store_seconds, float(stage_seconds) - scheduled)
                entry = self._attempt(
                    store,
                    objective=objective,
                    route=route,
                    slice_budget=slice_budget,
                    attempt_limit=self._attempt_limit(capacity[store.kind]),
                )
                scheduled += slice_budget
                if entry["status"] == "completed":
                    used[store.kind] += entry.get("accepted", 0)
                if entry["status"] == "completed":
                    accepted = entry.pop("candidates")
                    for raw in accepted:
                        collected.append(raw)
                    entry["candidates"] = len(accepted)
                attempts.append(entry)
        ranked = self._normalize_and_rank(collected, objective=objective, route=route)
        delivered = []
        delivered_ids: list[str] = []
        per_kind_count: dict[str, int] = {kind: 0 for kind in enabled_kinds}
        finalized: list[dict[str, Any]] = []
        for candidate in ranked:
            kind = candidate["kind"]
            if candidate["disposition"] != "eligible":
                finalized.append(candidate)
                continue
            if per_kind_count.get(kind, 0) >= capacity.get(kind, 0):
                candidate = self._dispositioned(
                    candidate, "rejected", "capacity for this candidate kind is exhausted"
                )
                finalized.append(candidate)
                continue
            per_kind_count[kind] = per_kind_count.get(kind, 0) + 1
            selected = self._dispositioned(candidate, "selected", "selected within its kind capacity")
            delivered.append(selected)
            delivered_ids.append(selected["candidate_id"])
            finalized.append(selected)
        for selected in delivered:
            for index, candidate in enumerate(finalized):
                if candidate["candidate_id"] == selected["candidate_id"]:
                    finalized[index] = self._dispositioned(
                        candidate, "selected", "selected within its kind capacity", delivered=True
                    )
        delivered = [item for item in finalized if item["disposition"] == "selected"]
        outcome = "optional_memory" if delivered else "no_optional_memory"
        return SearchResult(
            outcome=outcome,
            candidates=finalized,
            attempts=attempts,
            rounds=completed_rounds,
            delivered=delivered_ids,
        )

    # -- attempt scheduling -------------------------------------------------

    def _attempt(
        self,
        store: SearchStore,
        *,
        objective: Mapping[str, Any],
        route: str,
        slice_budget: float,
        attempt_limit: int,
    ) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "store_id": store.store_id,
            "kind": store.kind,
            "status": "unattempted-by-budget",
            "candidates": 0,
            "accepted": 0,
            "reason": None,
        }
        if slice_budget < self.limits.minimum_optional_slice_seconds:
            entry["reason"] = "no remaining time inside the enclosing stage bound"
            return entry
        query = safe_query_payload(
            {
                "representation": {key: objective[key] for key in ("model", "dimensions", "metric", "sanitizer_version")},
                "tokens": list(objective.get("tokens", []))[:32],
                "route": route,
            },
            self.privacy_policy,
        )
        entry["status"] = "attempted"
        started = self.clock()
        try:
            produced = store.query(query)
            raw_items = list(produced) if produced is not None else []
        except Exception as exc:  # isolated store failure
            entry["status"] = "invalid" if isinstance(exc, (TypeError, ValueError)) else "unavailable"
            entry["reason"] = f"{type(exc).__name__}"
            entry["elapsed_seconds"] = self.clock() - started
            return entry
        elapsed = self.clock() - started
        entry["elapsed_seconds"] = elapsed
        if elapsed > slice_budget:
            # Late work stays attributable but can never enter this packet.
            entry["status"] = "timed-out"
            entry["reason"] = "store exceeded its bounded sub-budget"
            return entry
        accepted: list[dict[str, Any]] = []
        for raw in raw_items:
            record = self._normalize(store, raw, objective=objective, route=route)
            if record is None:
                continue
            accepted.append(record)
            if len(accepted) >= attempt_limit:
                break
        entry["status"] = "completed"
        entry["accepted"] = len(accepted)
        entry["candidates"] = accepted
        return entry

    @staticmethod
    def _attempt_limit(capacity: int) -> int:
        # A store may report more candidates than its delivery capacity so a
        # lower-ranked eligible candidate stays visible; the returned set is
        # still bounded, and only the selection stage applies capacity.
        return max(2, int(capacity) * 4 + 4)

    # -- normalization and gating ------------------------------------------

    def _normalize(
        self,
        store: SearchStore,
        raw: Any,
        *,
        objective: Mapping[str, Any],
        route: str,
    ) -> dict[str, Any] | None:
        if not isinstance(raw, Mapping):
            return None
        required = ("kind", "logical_id", "revision_id", "payload", "scope", "representation")
        if any(key not in raw for key in required):
            return None
        if raw["kind"] != store.kind:
            return None
        try:
            payload = dict(raw["payload"])
        except (TypeError, ValueError):
            return None
        scope = raw["scope"]
        if store.scope is not None and dict(scope) != dict(store.scope):
            return None
        if raw.get("approval_status") not in (None, "approved"):
            return None
        if raw.get("designation") not in (None, "current"):
            return None
        if raw.get("revoked") is True or raw.get("withdrawn") is True:
            return None
        if raw.get("predicates_ok") is False:
            return None
        candidate_routes = raw.get("routes")
        if candidate_routes is not None and route not in tuple(candidate_routes):
            return None
        declared_digest = raw.get("payload_digest")
        payload_digest = contracts.sha256_hex(payload)
        if declared_digest is not None and declared_digest != payload_digest:
            return None
        representation = raw.get("representation")
        from .templates import representations_comparable

        comparable = representations_comparable(objective, representation)
        score = float(raw.get("score", 0.0)) if isinstance(raw.get("score", 0.0), (int, float)) else 0.0
        if not comparable:
            score = 0.0
        freshness = str(raw.get("freshness", store.freshness))
        if freshness not in {"live", "frozen"}:
            return None
        return {
            "kind": store.kind,
            "logical_id": str(raw["logical_id"]),
            "revision_id": str(raw["revision_id"]),
            "origin": str(raw.get("origin", store.store_id)),
            "source_id": str(raw.get("source_id", store.store_id)),
            "payload": payload,
            "payload_digest": payload_digest,
            "scope": dict(scope) if isinstance(scope, Mapping) else None,
            "freshness": freshness,
            "representation": dict(representation),
            "comparable": comparable,
            "score": score,
            "specificity": str(raw.get("specificity", store.specificity)),
            "provenance": [
                {
                    "store_id": store.store_id,
                    "origin": str(raw.get("origin", store.store_id)),
                    "specificity": str(raw.get("specificity", store.specificity)),
                }
            ],
            "reason": "eligible" if comparable else "incomparable representation",
        }

    def _normalize_and_rank(
        self,
        collected: list[dict[str, Any]],
        *,
        objective: Mapping[str, Any],
        route: str,
    ) -> list[dict[str, Any]]:
        deduplicated: dict[tuple[str, str, str], dict[str, Any]] = {}
        order: list[tuple[str, str, str]] = []
        for candidate in collected:
            key = (candidate["kind"], candidate["logical_id"], candidate["revision_id"])
            prior = deduplicated.get(key)
            if prior is None:
                deduplicated[key] = candidate
                order.append(key)
                continue
            merged_provenance = list(prior["provenance"])
            for entry in candidate["provenance"]:
                if entry not in merged_provenance:
                    merged_provenance.append(entry)
            prior["provenance"] = merged_provenance
            # One logical revision never receives a relevance bonus for being
            # visible in several stores; the maximum observed score stands.
            if candidate["score"] > prior["score"]:
                prior["score"] = candidate["score"]
                prior["source_id"] = candidate["source_id"]
        ranked = [deduplicated[key] for key in order]
        ranked.sort(
            key=lambda item: (
                not item["comparable"],
                -item["score"],
                _KIND_ORDER.get(item["kind"], 9),
                _SPECIFICITY_ORDER.get(item["specificity"], 9),
                item["logical_id"],
                item["revision_id"],
            )
        )
        records: list[dict[str, Any]] = []
        for item in ranked:
            disposition = "eligible" if item["comparable"] else "rejected"
            records.append(
                contracts.make_candidate(
                    kind=item["kind"],
                    logical_id=item["logical_id"],
                    revision_id=item["revision_id"],
                    origin=item["origin"],
                    source_id=item["source_id"],
                    payload_digest=item["payload_digest"],
                    payload=item["payload"],
                    scope=item["scope"],
                    provenance=item["provenance"],
                    freshness=item["freshness"],
                    representation=item["representation"],
                    score=item["score"],
                    comparable=item["comparable"],
                    disposition=disposition,
                    reasons=[item["reason"]] if not item["comparable"] else [],
                )
            )
        return records

    @staticmethod
    def _dispositioned(
        candidate: Mapping[str, Any],
        disposition: str,
        reason: str,
        *,
        delivered: bool = False,
    ) -> dict[str, Any]:
        revised = dict(candidate)
        revised["disposition"] = disposition
        reasons = list(revised.get("reasons", []))
        if reason not in reasons:
            reasons.append(reason)
        revised["reasons"] = reasons
        revised["content_hash"] = contracts.content_hash(revised)
        return revised
