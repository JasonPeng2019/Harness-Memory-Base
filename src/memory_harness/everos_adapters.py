"""EverOS case SearchStore adapter over the accepted reviewed-experience service.

This module is the narrow STEP-04 integration seam that turns the already
accepted EverOS case receipts and ``ReviewedExperienceService`` durable-join
path into one real ``search.SearchStore`` input for
``preparation.PreparationService``, separate from the accepted local
recent/unrepresented evidence store.

A remote case is discovery, never authority.  The store consumes only the
already sanitized bounded-search query it is handed (the exact representation
identity, at most 32 sanitized tokens, and the route of this preparation), asks
``EverOSAdapter.search_case_candidates`` for this exact scope's case
candidates through the accepted public search surface, and then requires the
accepted service to rejoin every hit to its durable confirmed ingestion, case
receipt, reviewed trajectory, and review receipt in the same scope before a
sanitized historical-evidence candidate is derived.  A foreign, malformed,
unconfirmed, altered, unreviewed, or missing receipt omits that one hit and can
never discard an unrelated eligible candidate, and no remote case ever becomes
procedural guidance or a plan.

The store declares ``requires_network=True`` because the case query performs a
real shared/remote task-path call, so the existing preparation gate suppresses
the actual call when ``experience_read`` is disabled or the network mode is
``restricted_local`` while every eligible local store keeps its chance.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any, Mapping

from . import config, contracts, experience, search, templates

_IDENTITY_KEYS = ("model", "dimensions", "metric", "sanitizer_version")
_EVIDENCE_KIND = "historical_evidence"
_EVIDENCE_AUTHORITY = "reviewed_historical_evidence"
_EVEROS_STORE_ID = "everos-reviewed-cases"
_MAX_TOKENS = 32


class EverOSAdapterError(ValueError):
    """The EverOS case-store factory received unusable explicit inputs."""


def make_everos_case_search_store(
    *,
    experience_service: Any,
    adapter: Any,
    scope: experience.ExperienceScope,
    limits: config.PreparationLimits | None = None,
    top_k: int = 100,
) -> search.SearchStore:
    """Return the one smallest EverOS case store for one exact scope.

    ``experience_service`` is the accepted ``ReviewedExperienceService`` whose
    ``confirmed_case_evidence`` rejoins one remote case to its durable
    confirmed ingestion, case receipt, reviewed trajectory, and review receipt;
    ``adapter`` is the accepted ``EverOSAdapter`` (or a deterministic double of
    its public scoped case-query surface) already bound to the same scope;
    ``scope`` is that exact four-part scope; ``limits`` is the central
    preparation limit set whose representation identity this store declares;
    and ``top_k`` bounds the one keyword query.
    """

    if not callable(getattr(experience_service, "confirmed_case_evidence", None)):
        raise EverOSAdapterError(
            "the EverOS case store needs a service exposing confirmed_case_evidence"
        )
    if not callable(getattr(adapter, "search_case_candidates", None)):
        raise EverOSAdapterError(
            "the EverOS case store needs an adapter exposing the scoped case query"
        )
    if not callable(getattr(adapter, "validate_case_scope", None)):
        raise EverOSAdapterError(
            "the EverOS case store needs an adapter exposing its case scope check"
        )
    if not isinstance(scope, experience.ExperienceScope):
        raise EverOSAdapterError(
            "the EverOS case store needs one explicit ExperienceScope"
        )
    if getattr(adapter, "scope", None) != scope:
        raise EverOSAdapterError(
            "the EverOS adapter is bound to a different experience scope"
        )
    if limits is not None and not isinstance(limits, config.PreparationLimits):
        raise EverOSAdapterError("preparation limits must be a PreparationLimits value")
    if isinstance(top_k, bool) or not isinstance(top_k, int) or top_k <= 0:
        raise EverOSAdapterError("the EverOS case query bound must be a positive integer")
    resolved_limits = limits or config.PreparationLimits()
    scope_record = scope.to_record()
    return search.SearchStore(
        store_id=_EVEROS_STORE_ID,
        kind=_EVIDENCE_KIND,
        query=lambda payload: _case_evidence_candidates(
            experience_service,
            adapter,
            scope,
            scope_record,
            resolved_limits,
            int(top_k),
            payload,
        ),
        scope=scope_record,
        freshness="live",
        specificity="project",
        requires_network=True,
    )


# --- the bounded query -----------------------------------------------------

def _tokens(value: str) -> tuple[str, ...]:
    return tuple(sorted(set(re.findall(r"[a-z0-9_]+", value.casefold()))))


def _objective(payload: Any) -> dict[str, Any] | None:
    """Read only the already sanitized bounded-search query of one attempt."""

    if not isinstance(payload, Mapping):
        return None
    representation = payload.get("representation")
    route = payload.get("route")
    if not isinstance(representation, Mapping) or route not in contracts.ROUTES:
        return None
    identity: dict[str, Any] = {}
    for key in _IDENTITY_KEYS:
        if key not in representation:
            return None
        identity[key] = representation[key]
    raw_tokens = payload.get("tokens")
    if not isinstance(raw_tokens, (list, tuple)):
        return None
    tokens = [
        token
        for token in list(raw_tokens)[:_MAX_TOKENS]
        if isinstance(token, str) and token.strip()
    ]
    if not tokens:
        return None
    return {**identity, "route": route, "tokens": tokens}


def _await(coroutine: Any) -> Any:
    """Run the accepted public async search from the bounded worker thread."""

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine)
    raise EverOSAdapterError(
        "the EverOS case query cannot run inside a running event loop"
    )


def _case_evidence_candidates(
    service: Any,
    adapter: Any,
    scope: experience.ExperienceScope,
    scope_record: Mapping[str, str],
    limits: config.PreparationLimits,
    top_k: int,
    payload: Any,
) -> list[dict[str, Any]]:
    """Query this scope's case candidates and convert only exact durable joins."""

    objective = _objective(payload)
    if objective is None:
        return []
    central = templates.representation_identity(limits=limits)
    if any(objective.get(key) != central.get(key) for key in _IDENTITY_KEYS):
        # The store's central identity and the bounded query disagree: no exact
        # comparison can be established, so no remote call begins at all.
        return []
    adapter.assert_scope(scope_record)
    source_cases = _await(
        adapter.search_case_candidates(query=" ".join(objective["tokens"]), top_k=top_k)
    )
    candidates: list[dict[str, Any]] = []
    for source_case in _materialize(source_cases):
        candidate = _evidence_candidate(
            service,
            adapter,
            scope,
            scope_record,
            objective,
            central,
            source_case,
        )
        if candidate is not None:
            candidates.append(candidate)
    return candidates


def _materialize(value: Any) -> list[Any]:
    return [] if value is None else list(value)


def _evidence_candidate(
    service: Any,
    adapter: Any,
    scope: experience.ExperienceScope,
    scope_record: Mapping[str, str],
    objective: Mapping[str, Any],
    identity: Mapping[str, Any],
    source_case: Any,
) -> dict[str, Any] | None:
    """Convert one remote hit into one evidence-only candidate, or omit it.

    The adapter's exact scope check, the accepted service's durable rejoin, and
    the central comparable representation all have to hold for this exact hit:
    an unreviewed, unconfirmed, unresolved, altered, cross-scope, malformed, or
    stale remote case simply never becomes a candidate, and no such hit can
    suppress an unrelated one.
    """

    if not isinstance(source_case, Mapping):
        return None
    try:
        adapter.validate_case_scope(source_case)
    except Exception:
        return None
    case_id = source_case.get("id")
    if not isinstance(case_id, str) or not case_id.strip():
        return None
    try:
        projection = service.confirmed_case_evidence(scope, source_case)
    except Exception:
        return None
    if not isinstance(projection, Mapping):
        return None
    if projection.get("kind") != _EVIDENCE_KIND:
        return None
    if projection.get("authority") != _EVIDENCE_AUTHORITY:
        return None
    if not experience.is_reviewed(projection):
        return None
    logical_id = projection.get("id")
    revision_id = projection.get("review_receipt_id")
    content = projection.get("content")
    projection_scope = projection.get("scope")
    if not isinstance(logical_id, str) or not logical_id.strip():
        return None
    if not isinstance(revision_id, str) or not revision_id.strip():
        return None
    if not isinstance(content, str) or not content.strip():
        return None
    if not isinstance(projection_scope, Mapping) or dict(projection_scope) != dict(scope_record):
        return None
    references = projection.get("evidence_refs")
    if not isinstance(references, (list, tuple)) or not references:
        return None
    evidence_refs = [item for item in references if isinstance(item, str) and item]
    if len(evidence_refs) != len(references):
        return None
    evidence_tokens = _tokens(content)
    if not evidence_tokens:
        return None
    representation: dict[str, Any] = dict(identity)
    representation["tokens"] = list(evidence_tokens)
    representation["route"] = str(objective.get("route", "ordinary"))
    representation["declared"] = True
    if not templates.representations_comparable(objective, representation):
        return None
    body = {
        "kind": _EVIDENCE_KIND,
        "authority": _EVIDENCE_AUTHORITY,
        "status": str(projection.get("status")),
        "content": content,
        "evidence_refs": evidence_refs,
        "review_receipt_id": revision_id,
        "scope": dict(scope_record),
        "case_id": case_id,
        "case_receipt_id": str(projection.get("case_receipt_id")),
    }
    return {
        "kind": _EVIDENCE_KIND,
        "logical_id": logical_id,
        "revision_id": revision_id,
        "origin": _EVEROS_STORE_ID,
        "source_id": _EVEROS_STORE_ID,
        "payload": body,
        "payload_digest": contracts.sha256_hex(body),
        "scope": dict(scope_record),
        "representation": representation,
        "score": templates.score_representations(objective, representation),
        "freshness": "live",
    }


__all__ = [
    "EverOSAdapterError",
    "make_everos_case_search_store",
]
