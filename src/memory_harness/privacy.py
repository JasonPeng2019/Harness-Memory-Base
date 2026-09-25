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
_UNICODE_ESCAPE = re.compile(r"\\u([0-9a-fA-F]{4})")
_ASSIGNMENT = re.compile(r"(?<![\w.-])([A-Za-z][\w.-]{2,})\s*[:=]", re.MULTILINE)
_CREDENTIAL_KEYS = ("apikey", "apitoken", "accesstoken", "refreshtoken", "password",
                    "passwd", "secret", "credential", "authorization", "bearertoken",
                    "controltoken", "policytoken", "privatekey")
_AUTHORITY_KEYS = ("publishauthority", "publicationauthority", "mutationauthority", "memoryauthority",
                   "controlplaneauthority", "maypublish", "mayapprove", "mayrevoke",
                   "maymutate", "mayexecuteparent")
_SAFE_AUTHORITY = {"none", "excluded", "historical_evidence_only", "evidence_only", "false"}


class PrivacyError(ValueError):
    """Base class for privacy-boundary failures."""


class MandatorySecretError(PrivacyError):
    """A prohibited secret appeared in mandatory worker content."""


class RemotePayloadPrivacyError(PrivacyError):
    """A reusable remote payload contains a configured secret or sensitive value."""


@dataclass(frozen=True)
class PrivacyPolicy:
    known_secrets: tuple[str, ...] = ()
    forbidden_environment_keys: tuple[str, ...] = (
        "MEMORY_HARNESS_CONTROL_TOKEN",
        "MEMORY_HARNESS_POLICY_TOKEN",
    )

    def detect(self, value: Any) -> list[str]:
        return _find_known_secrets(value, self.known_secrets)


def _find_known_secrets(value: Any, known_secrets: tuple[str, ...]) -> list[str]:
    if not known_secrets:
        return []
    findings: list[str] = []
    serialized = _decode_unicode(_stringify(value))
    for secret in known_secrets:
        if secret and secret in serialized:
            findings.append(secret)
    return findings


def _stringify(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=False)
    except TypeError:
        return str(value)


def _decode_unicode(value: str) -> str:
    return _UNICODE_ESCAPE.sub(lambda match: chr(int(match.group(1), 16)), value)


def _key_kind(key: str) -> str | None:
    normalized = re.sub(r"[^a-z0-9]", "", _decode_unicode(key).lower())
    if normalized in {"taskcredentialchannel", "rolelabel", "sharedpublication"}:
        return None
    if any(normalized.endswith(suffix) for suffix in _CREDENTIAL_KEYS):
        return "credential"
    if any(normalized.endswith(suffix) for suffix in _AUTHORITY_KEYS):
        return "authority"
    if normalized == "authority":
        return "authority_value"
    return None


def _task_channel(value: Any) -> bool:
    return (isinstance(value, Mapping) and set(value) == {"scope", "validated", "channel_id"}
            and value.get("scope") == "task_only" and value.get("validated") is True
            and isinstance(value.get("channel_id"), str) and bool(value["channel_id"]))


def worker_bound_finding(value: Any, policy: PrivacyPolicy | None = None) -> str | None:
    """Classify bounded worker-visible meaning without returning sensitive values."""
    selected = policy or PrivacyPolicy()
    seen = 0

    def visit(item: Any, depth: int = 0, coordinator_approval: bool = False) -> str | None:
        nonlocal seen
        seen += 1
        if depth > 24 or seen > 10000:
            return "scan limit"
        if isinstance(item, Mapping):
            if "task_credential_channel" in item and not _task_channel(item["task_credential_channel"]):
                return "invalid task credential channel"
            approved_pair = False
            approved_compact = False
            pair = item.get("content") if isinstance(item.get("content"), Mapping) else item
            if isinstance(pair.get("procedure"), Mapping) and isinstance(pair.get("approval"), Mapping):
                try:
                    from . import contracts
                    contracts.validate_procedure_approval(pair["approval"], procedure=pair["procedure"])
                    approved_pair = True
                    if isinstance(item.get("compact_representation"), Mapping) and isinstance(item.get("compact_approval"), Mapping):
                        contracts.validate_procedure_compact_approval(
                            item["compact_approval"], procedure=pair["procedure"],
                            full_approval=pair["approval"], representation=item["compact_representation"],
                        )
                        approved_compact = True
                except (ValueError, TypeError, KeyError):
                    pass
            for key, child in item.items():
                if not isinstance(key, str):
                    return "non-text key"
                if coordinator_approval and key == "authority_evidence":
                    if _find_known_secrets(child, selected.known_secrets) or _credential_field_finding(child):
                        return "credential in approval evidence"
                    continue
                kind = _key_kind(key)
                if kind == "credential":
                    return "credential field"
                if kind == "authority" and child not in (None, False, "none", "excluded"):
                    return "worker authority field"
                if kind == "authority_value" and child not in (None, False, "none", "excluded") and (
                    not isinstance(child, str) or child.lower() not in _SAFE_AUTHORITY
                ):
                    return "worker authority field"
                finding = visit(child, depth + 1, (approved_pair and key == "approval")
                                or (approved_compact and key == "compact_approval"))
                if finding:
                    return finding
            return None
        if isinstance(item, (list, tuple)):
            for child in item:
                finding = visit(child, depth + 1)
                if finding:
                    return finding
            return None
        if isinstance(item, str):
            decoded = _decode_unicode(item)
            if _find_known_secrets(decoded, selected.known_secrets):
                return "configured secret"
            stripped = decoded.strip()
            if stripped.startswith(("{", "[")):
                try:
                    parsed = json.loads(stripped)
                except (ValueError, TypeError):
                    parsed = None
                if isinstance(parsed, (dict, list)):
                    return visit(parsed, depth + 1)
            for match in _ASSIGNMENT.finditer(decoded):
                kind = _key_kind(match.group(1))
                assigned = decoded[match.end():].splitlines()[0].split(",", 1)[0].split(";", 1)[0].strip().strip("\"' ")
                if kind == "credential" or (kind in {"authority", "authority_value"} and assigned.lower() not in _SAFE_AUTHORITY | {"0", "null"}):
                    return "worker assignment"
        return None

    return visit(value)


def guard_worker_bound(value: Any, policy: PrivacyPolicy | None = None) -> None:
    """Reject worker-bound task/body/search/configuration meaning before use."""
    finding = worker_bound_finding(value, policy)
    if finding:
        raise MandatorySecretError(f"worker-bound content contains prohibited {finding}")


def guard_worker_bound_remote(value: Any, policy: PrivacyPolicy | None = None) -> None:
    """Lane-4 callable: check procedure behavior.body or representation.search_text."""
    if worker_bound_finding(value, policy):
        raise RemotePayloadPrivacyError("remote worker-bound content contains prohibited meaning")


def sanitize_text(text: str, policy: PrivacyPolicy) -> str:
    sanitized = _decode_unicode(text)
    for secret in policy.known_secrets:
        if secret:
            sanitized = sanitized.replace(secret, REDACTION_MARKER)
    return REDACTION_MARKER if worker_bound_finding(sanitized, policy) else sanitized


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
    guard_worker_bound(value, policy)


def sanitize_optional(value: Any, policy: PrivacyPolicy) -> Any:
    return None if worker_bound_finding(value, policy) else value


def guard_remote_payload(value: Any, policy: PrivacyPolicy | None = None) -> None:
    """Reject remote egress rather than silently mutate approved meaning.

    Optional historical evidence can be redacted before it is approved.  Once
    a procedure revision is approved, replacing sensitive text under the same
    content identity would break the trust binding, so publication must stop
    until an explicitly sanitized/reapproved revision exists.
    """

    selected_policy = policy or PrivacyPolicy()
    if detect_secrets(value, selected_policy) or _credential_field_finding(value):
        raise RemotePayloadPrivacyError(
            "remote procedure payload contains a prohibited secret or sensitive value"
        )


def _credential_field_finding(value: Any) -> bool:
    """Scan whole publications for credentials while retaining approved authority."""
    if isinstance(value, Mapping):
        return any((isinstance(key, str) and _key_kind(key) == "credential")
                   or _credential_field_finding(child) for key, child in value.items())
    if isinstance(value, (list, tuple)):
        return any(_credential_field_finding(child) for child in value)
    if isinstance(value, str):
        decoded = _decode_unicode(value)
        try:
            parsed = json.loads(decoded)
        except ValueError:
            parsed = None
        if isinstance(parsed, (dict, list)):
            return _credential_field_finding(parsed)
        return any(_key_kind(match.group(1)) == "credential" for match in _ASSIGNMENT.finditer(decoded))
    return False


def worker_environment(
    environment: Mapping[str, str], policy: PrivacyPolicy | None = None
) -> dict[str, str]:
    selected_policy = policy or PrivacyPolicy()
    result: dict[str, str] = {}
    for key, value in environment.items():
        if not isinstance(key, str) or key.upper() in {
            item.upper() for item in selected_policy.forbidden_environment_keys
        } or (key != "OPENAI_API_KEY" and (
            _key_kind(key) in {"credential", "authority", "authority_value"}
            or (key.upper().startswith("MEMORY_HARNESS_") and any(
                part in key.upper().split("_") for part in ("CONTROL", "POLICY", "PUBLISH", "APPROVAL", "ADMIN", "MANAGER")
            ))
        )):
            continue
        if key == "OPENAI_API_KEY":
            result[key] = value
        elif isinstance(value, str) and not worker_bound_finding(value, selected_policy):
            result[key] = value
    return result


def worker_prompt(
    task_text: str,
    plan_content: Mapping[str, Any] | Any,
    *,
    optional_content: Any = None,
    privacy_policy: PrivacyPolicy | None = None,
) -> str:
    policy = privacy_policy or PrivacyPolicy()
    guard_mandatory({"task": task_text, "plan": plan_content}, policy)
    parts = ["## Task", str(task_text), "## Accepted plan", _render(plan_content)]
    if optional_content is not None:
        selected_optional = (
            [item for item in optional_content if sanitize_optional(item, policy) is not None]
            if isinstance(optional_content, (list, tuple))
            else sanitize_optional(optional_content, policy)
        )
        if selected_optional is not None:
            parts.extend(["## Optional historical evidence", _render(selected_optional)])
    parts.append("## Execution boundary")
    parts.append("Do not approve, publish, revoke, or mutate product policy.")
    return "\n\n".join(parts) + "\n"


def _render(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2)


def safe_query_payload(value: Any, policy: PrivacyPolicy | None = None) -> Any:
    selected_policy = policy or PrivacyPolicy()
    if worker_bound_finding(value, selected_policy):
        raise RemotePayloadPrivacyError("query contains prohibited worker-bound meaning")
    return value


__all__ = [
    "PrivacyError",
    "MandatorySecretError",
    "RemotePayloadPrivacyError",
    "PrivacyPolicy",
    "REDACTION_MARKER",
    "detect_secrets",
    "guard_mandatory",
    "guard_worker_bound",
    "guard_worker_bound_remote",
    "guard_remote_payload",
    "safe_query_payload",
    "sanitize_optional",
    "sanitize_payload",
    "sanitize_text",
    "worker_environment",
    "worker_bound_finding",
    "worker_prompt",
]
