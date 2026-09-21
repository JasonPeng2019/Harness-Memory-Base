"""Deterministic local Atlas-query and representation fixtures.

This module is a fixture adapter only: it never contacts a live service and
provider authentication is accepted only to be explicitly excluded from the
query/representation payload.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from .experience import ExperienceRecord
from .privacy import PrivacyPolicy, sanitize_payload


@dataclass(frozen=True)
class AtlasQuery:
    text: str
    objective_id: str
    route: str
    filters: tuple[tuple[str, str], ...] = ()

    def to_record(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "objective_id": self.objective_id,
            "route": self.route,
            "filters": [list(item) for item in self.filters],
        }

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "AtlasQuery":
        return cls(
            text=str(record["text"]),
            objective_id=str(record["objective_id"]),
            route=str(record["route"]),
            filters=tuple((str(key), str(value)) for key, value in record.get("filters", [])),
        )


@dataclass(frozen=True)
class AtlasDocument:
    document_id: str
    objective_id: str
    route: str
    text: str
    fingerprint: str

    def to_record(self) -> dict[str, Any]:
        return {
            "document_id": self.document_id,
            "objective_id": self.objective_id,
            "route": self.route,
            "text": self.text,
            "fingerprint": self.fingerprint,
        }


def build_atlas_query(
    text: str,
    *,
    objective_id: str,
    route: str,
    privacy_policy: PrivacyPolicy | None = None,
    credentials: Mapping[str, Any] | None = None,
    filters: Mapping[str, str] | None = None,
) -> AtlasQuery:
    """Build a sanitized query. Credentials are never copied into the payload."""

    policy = privacy_policy or PrivacyPolicy()
    sanitized_text = sanitize_payload(text, policy)
    if not isinstance(sanitized_text, str):
        raise ValueError("Atlas query text must be a string")
    normalized_filters = tuple(
        (str(key), str(value)) for key, value in sorted((filters or {}).items())
    )
    return AtlasQuery(
        text=sanitized_text,
        objective_id=objective_id,
        route=route,
        filters=normalized_filters,
    )


def build_representation(
    experience: ExperienceRecord, privacy_policy: PrivacyPolicy | None = None
) -> AtlasDocument:
    policy = privacy_policy or PrivacyPolicy()
    sanitized = sanitize_payload(experience.raw_content, policy)
    if not isinstance(sanitized, str):
        raise ValueError("representation text must be a string")
    fingerprint = _deterministic_fingerprint(sanitized)
    return AtlasDocument(
        document_id=experience.record_id,
        objective_id=experience.objective_id,
        route=experience.route,
        text=sanitized,
        fingerprint=fingerprint,
    )


def _deterministic_fingerprint(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _tokens(text: str) -> set[str]:
    return {item for item in re.findall(r"[a-z0-9_]+", text.lower()) if len(item) > 2}


class LocalAtlasFixture:
    """A deterministic in-memory fixture, not a live Atlas client."""

    def __init__(self, documents: Iterable[AtlasDocument]) -> None:
        self.documents = tuple(documents)

    def search(self, query: AtlasQuery) -> list[AtlasDocument]:
        query_tokens = _tokens(query.text)
        scored: list[tuple[int, str, AtlasDocument]] = []
        for document in self.documents:
            if query.objective_id and document.objective_id != query.objective_id:
                continue
            if query.route and document.route != query.route:
                continue
            score = len(query_tokens & _tokens(document.text))
            if score:
                scored.append((score, document.document_id, document))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [item[2] for item in scored]


__all__ = [
    "AtlasQuery",
    "AtlasDocument",
    "LocalAtlasFixture",
    "build_atlas_query",
    "build_representation",
]
