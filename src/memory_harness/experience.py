"""Deterministic reviewed-experience fixtures used by the Stage-A slice."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from . import privacy as privacy_module
from .privacy import PrivacyPolicy

SYNTHETIC_SECRET = "synthetic-secret-alpha-1234567890"


@dataclass(frozen=True)
class ExperienceRecord:
    record_id: str
    objective_id: str
    route: str
    status: str
    raw_content: str
    evidence_ref: str
    reviewed_by: str = "ROOT"


_EXAMPLES = (
    ExperienceRecord(
        record_id="experience-regression-001",
        objective_id="objective-regression",
        route="ordinary",
        status="reviewed_success",
        raw_content=(
            "A regression failed after the parser change. The discriminating check was "
            "tests/test_parser.py. The repair preserved the public API. "
            f"Synthetic fixture credential {SYNTHETIC_SECRET} must never leave raw evidence."
        ),
        evidence_ref="review://fixture/regression-001",
    ),
    ExperienceRecord(
        record_id="experience-interface-002",
        objective_id="objective-interface",
        route="ordinary",
        status="reviewed_failure",
        raw_content=(
            "An interface change failed because a consumer was not updated. "
            "The missing consumer was in the release adapter."
        ),
        evidence_ref="review://fixture/interface-002",
    ),
    ExperienceRecord(
        record_id="experience-hypothesis-003",
        objective_id="objective-hypothesis",
        route="problem_focused",
        status="disproved_hypothesis",
        raw_content=(
            "The timeout was not caused by the network adapter; the local lock was held "
            "by the previous worker. This disproved the network-first hypothesis."
        ),
        evidence_ref="review://fixture/hypothesis-003",
    ),
)


def load_experience_examples(policy: PrivacyPolicy | None = None) -> tuple[ExperienceRecord, ...]:
    """Return immutable synthetic reviewed-experience fixtures.

    ``policy`` is accepted for interface symmetry, but raw authoritative evidence
    is intentionally never rewritten here.
    """

    return _EXAMPLES


def derived_optional_content(
    record: ExperienceRecord, policy: PrivacyPolicy | None = None
) -> dict[str, Any]:
    selected_policy = policy or PrivacyPolicy(known_secrets=(SYNTHETIC_SECRET,))
    sanitized = privacy_module.sanitize_payload(record.raw_content, selected_policy)
    return {
        "id": record.record_id,
        "kind": "experience",
        "status": record.status,
        "content": sanitized,
        "evidence_ref": record.evidence_ref,
    }


def is_reviewed(record: Mapping[str, Any]) -> bool:
    return record.get("status") in {
        "reviewed_success",
        "reviewed_failure",
        "disproved_hypothesis",
    }


__all__ = ["ExperienceRecord", "load_experience_examples", "derived_optional_content", "is_reviewed"]
