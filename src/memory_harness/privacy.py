"""Known-secret and control-credential guards for derived product payloads.

The authoritative task source is never rewritten.  Mandatory content fails
closed when it contains a configured control credential; optional content is
sanitized or omitted by the caller.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Mapping

REDACTION_MARKER = "[REDACTED]"


class PrivacyError(ValueError):
    """Base class for privacy-boundary failures."""


class MandatorySecretError(PrivacyError):
    """A prohibited secret appeared in mandatory worker content."""


@dataclass(frozen=True)
class PrivacyPolicy:
    known_secrets: tuple[str, ...] = ()
    forbidden_environment_keys: tuple[str, ...] = (
        "MEMORY_HARNESS_CONTROL_TOKEN",
        "MEMORY_HARNESS_POLICY_TOKEN",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
    )

    def detect(self, value: Any) -> list[str]:
        return _find_known_secrets(value, self.known_secrets)


def _find_known_secrets(value: Any, known_secrets: tuple[str, ...]) -> list[str]:
    if not known_secrets:
        return []
    findings: list[str] = []
    serialized = _stringify(value)
    for secret in known_secrets:
        if secret and secret in serialized:
            findings.append(secret)
    return findings


def _stringify(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=False)
    except TypeError:
        return str(value)


def sanitize_text(text: str, policy: PrivacyPolicy) -> str:
    sanitized = text
    for secret in policy.known_secrets:
        if secret:
            sanitized = sanitized.replace(secret, REDACTION_MARKER)
    return sanitized


def sanitize_payload(value: Any, policy: PrivacyPolicy) -> Any:
    """Recursively sanitize known secret values while preserving JSON shape."""

    if isinstance(value, str):
        return sanitize_text(value, policy)
    if isinstance(value, Mapping):
        return {str(key): sanitize_payload(item, policy) for key, item in value.items()}
    if isinstance(value, list):
        return [sanitize_payload(item, policy) for item in value]
    if isinstance(value, tuple):
        return tuple(sanitize_payload(item, policy) for item in value)
    return value


def detect_secrets(value: Any, policy: PrivacyPolicy) -> list[str]:
    return policy.detect(value)


def guard_mandatory(value: Any, policy: PrivacyPolicy) -> None:
    findings = detect_secrets(value, policy)
    if findings:
        joined = ", ".join(sorted(findings))
        raise MandatorySecretError(f"mandatory content contains a prohibited secret: {joined}")


def sanitize_optional(value: Any, policy: PrivacyPolicy) -> Any:
    return sanitize_payload(value, policy)


def worker_environment(
    environment: Mapping[str, str], policy: PrivacyPolicy | None = None
) -> dict[str, str]:
    selected_policy = policy or PrivacyPolicy()
    result: dict[str, str] = {}
    for key, value in environment.items():
        if not isinstance(key, str) or key.upper() in {
            item.upper() for item in selected_policy.forbidden_environment_keys
        }:
            continue
        sanitized_value = sanitize_payload(value, selected_policy)
        if isinstance(sanitized_value, str):
            result[key] = sanitized_value
    return result


def worker_prompt(
    task_text: str,
    plan_content: Mapping[str, Any] | Any,
    *,
    optional_content: Any = None,
    privacy_policy: PrivacyPolicy | None = None,
) -> str:
    policy = privacy_policy or PrivacyPolicy()
    guard_mandatory(task_text, policy)
    mandatory = sanitize_payload(plan_content, policy)
    parts = ["## Task", str(task_text), "## Accepted plan", _render(mandatory)]
    if optional_content is not None:
        sanitized_optional = sanitize_payload(optional_content, policy)
        parts.extend(["## Optional historical evidence", _render(sanitized_optional)])
    parts.append("## Execution boundary")
    parts.append("Do not approve, publish, revoke, or mutate product policy.")
    return "\n\n".join(parts) + "\n"


def _render(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2)


def safe_query_payload(value: Any, policy: PrivacyPolicy | None = None) -> Any:
    selected_policy = policy or PrivacyPolicy()
    return sanitize_payload(value, selected_policy)


__all__ = [
    "PrivacyError",
    "MandatorySecretError",
    "PrivacyPolicy",
    "REDACTION_MARKER",
    "detect_secrets",
    "guard_mandatory",
    "safe_query_payload",
    "sanitize_optional",
    "sanitize_payload",
    "sanitize_text",
    "worker_environment",
    "worker_prompt",
]
