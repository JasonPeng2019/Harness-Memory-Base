"""Known-secret and control-credential guards for derived product payloads.

The authoritative task source is never rewritten.  Mandatory content fails
closed when it contains a configured control credential; optional content is
sanitized or omitted by the caller.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Iterator, Mapping

REDACTION_MARKER = "[REDACTED]"
_MAX_JSON_SCALAR_CHARS = 65536
_JSON_STRING = re.compile(r'"(?:\\.|[^"\\])*"')


class PrivacyError(ValueError):
    """Base class for privacy-boundary failures."""


class MandatorySecretError(PrivacyError):
    """A prohibited secret appeared in mandatory worker content."""


class RemotePayloadPrivacyError(PrivacyError):
    """A reusable remote payload contains a configured secret or sensitive value."""


class RecipientAuthorizationError(PrivacyError):
    """The requested worker destination is not authorized for final context."""


@dataclass(frozen=True)
class PrivacyPolicy:
    known_secrets: tuple[str, ...] = ()
    forbidden_environment_keys: tuple[str, ...] = (
        "MEMORY_HARNESS_CONTROL_TOKEN",
        "MEMORY_HARNESS_POLICY_TOKEN",
    )

    def detect(self, value: Any) -> list[str]:
        return detect_secrets(value, self)


def _find_known_secrets(value: Any, known_secrets: tuple[str, ...]) -> bool:
    if isinstance(value, Mapping):
        return any(_find_known_secrets(key, known_secrets)
                   or _find_known_secrets(item, known_secrets)
                   for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return any(_find_known_secrets(item, known_secrets) for item in value)
    return isinstance(value, str) and _text_has_known_secret(value, known_secrets)


def _secret_forms(secret: str) -> tuple[str, ...]:
    if not secret:
        return ()
    # A string can arrive after an earlier JSON serialization. Match the
    # escaped scalar as well as the original, without decoding arbitrary text.
    forms = {secret}
    for ascii_only in (False, True):
        escaped = json.dumps(secret, ensure_ascii=ascii_only)[1:-1]
        forms.add(escaped)
        forms.add(escaped.replace("/", "\\/"))
    return tuple(sorted(forms, key=len, reverse=True))


def _json_secret_spans(text: str, known_secrets: tuple[str, ...]) -> list[tuple[int, int]]:
    """Locate one-level JSON string scalars that decode to a known secret."""
    if not known_secrets or "\\u" not in text.lower():
        return []
    spans: list[tuple[int, int]] = []
    for match in _JSON_STRING.finditer(text):
        scalar = match.group()
        if "\\u" not in scalar.lower():
            continue
        if len(scalar) > _MAX_JSON_SCALAR_CHARS:
            # A caller cannot use an oversized escaped scalar to evade a
            # configured-secret check or make us decode unbounded input.
            spans.append(match.span())
            continue
        try:
            decoded = json.loads(scalar)
        except (ValueError, TypeError):
            continue
        if any(secret and secret in decoded for secret in known_secrets):
            spans.append(match.span())
    return spans


def _decoded_escaped_json_fields(text: str) -> Iterator[str | None]:
    """Inspect one-level Unicode-escaped JSON field names; None fails closed."""
    if "\\u" not in text.lower():
        return
    for match in _JSON_STRING.finditer(text):
        scalar = match.group()
        if "\\u" not in scalar.lower():
            continue
        following = match.end()
        while following < len(text) and text[following] in " \t\r\n":
            following += 1
        if following == len(text) or text[following] != ":":
            continue
        if len(scalar) > _MAX_JSON_SCALAR_CHARS:
            yield None
            continue
        try:
            yield json.loads(scalar)
        except (ValueError, TypeError):
            continue


def _serialized_structure_flags(text: str, policy: PrivacyPolicy) -> tuple[bool, bool]:
    """Classify one bounded JSON object/list layer, including decoded keys."""
    stripped = text.strip()
    if stripped.startswith(("{", "[")) and stripped.endswith(("}", "]")):
        if len(stripped) > _MAX_JSON_SCALAR_CHARS:
            return True, True  # A plausible oversized JSON payload fails closed.
        try:
            decoded = json.loads(stripped)
        except (ValueError, RecursionError):
            pass
        else:
            return _classify_payload(decoded, policy, decode_serialized=False)
    # Preserve the previous guard for escaped field assignments in non-JSON
    # text; do not decode another layer inside an otherwise valid JSON value.
    fields = tuple(_decoded_escaped_json_fields(text))
    return (
        any(field is None or _protected_key(field, policy) for field in fields),
        any(field is None or _authority_key(field) for field in fields),
    )


def _unsafe_serialized_structure(text: str, policy: PrivacyPolicy) -> bool:
    return any(_serialized_structure_flags(text, policy))


def _contains_unsafe_serialized_structure(value: Any, policy: PrivacyPolicy) -> bool:
    if isinstance(value, str):
        return _unsafe_serialized_structure(value, policy)
    if isinstance(value, Mapping):
        return any(_contains_unsafe_serialized_structure(item, policy) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_unsafe_serialized_structure(item, policy) for item in value)
    return False


def _text_has_known_secret(text: str, known_secrets: tuple[str, ...]) -> bool:
    return (any(form in text for secret in known_secrets for form in _secret_forms(secret))
            or bool(_json_secret_spans(text, known_secrets)))


def _normalized_key(key: str) -> str:
    separated = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", key)
    separated = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", separated)
    return re.sub(r"[^a-z0-9]+", "_", separated.lower()).strip("_")


_RAW_CREDENTIAL_FIELDS = frozenset({
    "api_key", "access_token", "password", "client_secret", "secret_key",
    "credential", "credentials", "credential_value", "raw_task_credential",
    "task_credential_value", "token", "secret", "authorization", "auth_token",
    "bearer", "private_key",
})
_API_KEY_TOKEN = re.compile(r"(?:^|_)api_?key(?:_|$)")
_CREDENTIAL_ASSIGNMENT = re.compile(
    r"\b(?:api[_ -]?key|access[_ -]?token|password|client[_ -]?secret|"
    r"token|secret|authorization|bearer|private[_ -]?key|"
    r"credential(?:_value)?|(?:control|policy)[_ -]?(?:token|secret|credential))"
    r"\b[\"']?\s*[:=]\s*[\"']?\S+", re.IGNORECASE,
)
_FIELD_ASSIGNMENT = re.compile(r"\b([A-Za-z][A-Za-z0-9_.-]*)[\"']?\s*[:=]\s*[\"']?\S+")
_WORKER_RECIPIENT = re.compile(r"worker:[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_CONTROL_DESTINATIONS = frozenset({
    "root", "manager", "controller", "control", "policy", "admin", "operator",
    "approve", "approval", "publish", "publication", "revoke", "revocation",
})


def _control_key(key: str, policy: PrivacyPolicy) -> bool:
    normalized = _normalized_key(key)
    if normalized in {_normalized_key(item) for item in policy.forbidden_environment_keys}:
        return True
    parts = set(normalized.split("_"))
    return bool(
        parts & {"root", "manager", "control", "controller", "policy", "controlplane",
                 "approve", "approval", "publish", "publication", "revoke",
                 "revocation", "admin", "operator"}
        and parts & {"token", "secret", "credential", "credentials", "key", "password"}
    )


def _protected_key(key: str, policy: PrivacyPolicy) -> bool:
    normalized = _normalized_key(key)
    folded = normalized.replace("_", "")
    return (_control_key(key, policy) or normalized in _RAW_CREDENTIAL_FIELDS
            or bool(_API_KEY_TOKEN.search(normalized))
            or normalized.endswith(("_api_key", "_access_token", "_auth_token",
                                    "_token", "_password", "_client_secret",
                                    "_secret_key", "_private_key", "_credential",
                                    "_credentials"))
            or any(folded.endswith(alias) for alias in (
                "apikey", "accesstoken", "authtoken", "clientsecret",
                "secretkey", "privatekey", "credentialvalue",
            )))


_AUTHORITY_ACTIONS = frozenset({
    "approve", "approval", "publish", "publication", "revoke", "revocation",
    "policy_mutation", "mutate_policy", "policy_write", "write_policy",
})
_CONTROL_ROLES = frozenset({"root", "manager", "controller", "control", "admin", "operator"})


def _authority_key(key: str) -> bool:
    normalized = _normalized_key(key)
    if normalized == "shared_publication":
        # Existing resolved feature gate, not a worker mutation grant.
        return False
    role_field = normalized
    for suffix in ("_enabled", "_allowed"):
        if role_field.endswith(suffix):
            role_field = role_field[:-len(suffix)]
            break
    if role_field.endswith("_role") and role_field[:-5] in _CONTROL_ROLES:
        return True
    def action_claim(name: str) -> bool:
        return any(name == action or name in (f"{action}_enabled", f"{action}_allowed")
                   for action in _AUTHORITY_ACTIONS)
    if action_claim(normalized):
        return True
    prefix, _, action = normalized.partition("_")
    return (prefix in {"may", "can", "allow", "allowed", "tool", "tools", "grant", "enable"}
            and action_claim(action))


def has_worker_authority(value: Any) -> bool:
    """Find control claims in worker configuration, not task prose or evidence."""
    return _classify_payload(value, PrivacyPolicy())[1]


def _classify_payload(
    value: Any, policy: PrivacyPolicy, *, decode_serialized: bool = True,
) -> tuple[bool, bool]:
    """Return credential and worker-authority flags from the same representation.

    Decoding is limited to one JSON object/list string of at most 65536 chars.
    A decoded string value is inspected as text, never parsed a second time.
    """
    if isinstance(value, Mapping):
        credential = authority = False
        for key, item in value.items():
            if isinstance(key, str):
                credential |= _protected_key(key, policy) or _text_has_known_secret(key, policy.known_secrets)
                authority |= _authority_key(key) or (
                    _normalized_key(key) in {"role", "execution_role"}
                    and isinstance(item, str) and item.lower() in _CONTROL_ROLES
                )
            else:
                credential |= _classify_payload(key, policy, decode_serialized=False)[0]
            child_credential, child_authority = _classify_payload(
                item, policy, decode_serialized=decode_serialized,
            )
            credential |= child_credential
            authority |= child_authority
        return credential, authority
    if isinstance(value, (list, tuple)):
        flags = (_classify_payload(item, policy, decode_serialized=decode_serialized)
                 for item in value)
        return _combine_flags(flags)
    if isinstance(value, str):
        credential = _text_has_known_secret(value, policy.known_secrets) or bool(
            _CREDENTIAL_ASSIGNMENT.search(value)
            or any(_protected_key(match.group(1), policy) for match in _FIELD_ASSIGNMENT.finditer(value))
            or any(re.search(r"(?<![A-Za-z0-9_])" + re.escape(key) + r"(?![A-Za-z0-9_])", value, re.IGNORECASE)
                   for key in policy.forbidden_environment_keys if key)
        )
        authority = any(_authority_key(match.group(1)) for match in _FIELD_ASSIGNMENT.finditer(value))
        if decode_serialized:
            nested_credential, nested_authority = _serialized_structure_flags(value, policy)
            credential |= nested_credential
            authority |= nested_authority
        return credential, authority
    return False, False


def _combine_flags(flags: Iterator[tuple[bool, bool]]) -> tuple[bool, bool]:
    credential = authority = False
    for child_credential, child_authority in flags:
        credential |= child_credential
        authority |= child_authority
    return credential, authority


def guard_worker_payload(value: Any, policy: PrivacyPolicy | None = None) -> None:
    """Reject untrusted worker-bound content with value-free diagnostics."""
    if any(_classify_payload(value, policy or PrivacyPolicy())):
        raise MandatorySecretError("worker payload contains a prohibited credential or control authority")


def guard_worker_authority(value: Any) -> None:
    if has_worker_authority(value):
        raise MandatorySecretError("worker payload contains prohibited control authority")


def _protected_text(value: str, policy: PrivacyPolicy) -> bool:
    if _unsafe_serialized_structure(value, policy) or _CREDENTIAL_ASSIGNMENT.search(value) or any(
        _protected_key(match.group(1), policy) for match in _FIELD_ASSIGNMENT.finditer(value)
    ):
        return True
    return any(
        re.search(r"(?<![A-Za-z0-9_])" + re.escape(key) + r"(?![A-Za-z0-9_])", value, re.IGNORECASE)
        for key in policy.forbidden_environment_keys if key
    )


def _has_protected_field(value: Any, policy: PrivacyPolicy) -> bool:
    if isinstance(value, Mapping):
        return any(
            (isinstance(key, str) and _protected_key(key, policy))
            or _has_protected_field(item, policy)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_has_protected_field(item, policy) for item in value)
    return isinstance(value, str) and _protected_text(value, policy)


def _stringify(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=False)
    except TypeError:
        return str(value)


def sanitize_text(text: str, policy: PrivacyPolicy) -> str:
    if _unsafe_serialized_structure(text, policy):
        return REDACTION_MARKER
    sanitized = text
    spans = _json_secret_spans(sanitized, policy.known_secrets)
    for start, end in reversed(spans):
        sanitized = sanitized[:start] + json.dumps(REDACTION_MARKER) + sanitized[end:]
    for secret in policy.known_secrets:
        for form in _secret_forms(secret):
            sanitized = sanitized.replace(form, REDACTION_MARKER)
    return sanitized


def sanitize_payload(value: Any, policy: PrivacyPolicy) -> Any:
    """Sanitize known values and omit secret or credential-bearing field names."""

    if isinstance(value, str):
        return sanitize_text(value, policy)
    if isinstance(value, Mapping):
        return {
            str(key): sanitize_payload(item, policy) for key, item in value.items()
            if not _find_known_secrets(key, policy.known_secrets)
            and not (isinstance(key, str) and (_protected_key(key, policy)
                                              or (_authority_key(key) and key != "approval")))
        }
    if isinstance(value, list):
        return [sanitize_payload(item, policy) for item in value]
    if isinstance(value, tuple):
        return tuple(sanitize_payload(item, policy) for item in value)
    return value


def detect_secrets(value: Any, policy: PrivacyPolicy) -> list[str]:
    return ["prohibited credential"] if _classify_payload(value, policy)[0] else []


def guard_mandatory(value: Any, policy: PrivacyPolicy) -> None:
    if detect_secrets(value, policy):
        raise MandatorySecretError("mandatory content contains a prohibited credential")


def authorize_recipient(recipient: Any, policy: PrivacyPolicy | None = None) -> str:
    """Permit one canonical worker destination, never a product control actor."""

    selected_policy = policy or PrivacyPolicy()
    if (not isinstance(recipient, str) or len(recipient) > 128
            or not _WORKER_RECIPIENT.fullmatch(recipient)
            or any(part.lower() in _CONTROL_DESTINATIONS
                   for part in re.split(r"[._-]", recipient.removeprefix("worker:")))
            or detect_secrets(recipient, selected_policy)):
        raise RecipientAuthorizationError("recipient is not an authorized worker destination")
    return recipient


def sanitize_optional(value: Any, policy: PrivacyPolicy) -> Any:
    return sanitize_payload(value, policy)


def guard_remote_payload(
    value: Any, policy: PrivacyPolicy | None = None, *, worker_bound: bool = False,
) -> None:
    """Reject remote egress rather than silently mutate approved meaning.

    Optional historical evidence can be redacted before it is approved.  Once
    a procedure revision is approved, replacing sensitive text under the same
    content identity would break the trust binding, so publication must stop
    until an explicitly sanitized/reapproved revision exists.
    """

    selected_policy = policy or PrivacyPolicy()
    credential, authority = _classify_payload(value, selected_policy)
    if credential or (worker_bound and authority):
        raise RemotePayloadPrivacyError(
            "remote procedure payload contains a prohibited secret or sensitive value"
        )


def guard_worker_remote_payload(value: Any, policy: PrivacyPolicy | None = None) -> None:
    """Lane-4 preflight for each worker-bound body/search text before write."""
    guard_remote_payload(value, policy, worker_bound=True)


def worker_environment(
    environment: Mapping[str, str], policy: PrivacyPolicy | None = None
) -> dict[str, str]:
    selected_policy = policy or PrivacyPolicy()
    result: dict[str, str] = {}
    for key, value in environment.items():
        if (not isinstance(key, str) or _control_key(key, selected_policy)
                or _find_known_secrets(key, selected_policy.known_secrets)
                or (_protected_key(key, selected_policy) and key != "OPENAI_API_KEY")
                or _authority_key(key)
                or _contains_unsafe_serialized_structure(value, selected_policy)
                or (key != "OPENAI_API_KEY" and has_worker_authority(value))
                or (_normalized_key(key) in {"role", "execution_role"}
                    and isinstance(value, str) and value.lower() in _CONTROL_ROLES)):
            continue
        if key == "OPENAI_API_KEY" and isinstance(value, str):
            # This named provider transport is the sole credential value the
            # worker inherits; it is never copied into a context or trace.
            result[key] = value
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
    guard_mandatory({"task": task_text, "plan": plan_content}, policy)
    guard_worker_authority({"task": task_text, "plan": plan_content})
    parts = ["## Task", str(task_text), "## Accepted plan", _render(plan_content)]
    if optional_content is not None:
        if not detect_secrets(optional_content, policy) and not has_worker_authority(optional_content):
            parts.extend(["## Optional historical evidence", _render(optional_content)])
    parts.append("## Execution boundary")
    parts.append("Do not approve, publish, revoke, or mutate product policy.")
    return "\n\n".join(parts) + "\n"


def _render(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2)


def safe_query_payload(value: Any, policy: PrivacyPolicy | None = None) -> Any:
    selected_policy = policy or PrivacyPolicy()
    if _contains_unsafe_serialized_structure(value, selected_policy) or has_worker_authority(value):
        raise RemotePayloadPrivacyError("remote query contains a prohibited credential or authority field")
    safe = sanitize_payload(value, selected_policy)
    if detect_secrets(safe, selected_policy):
        raise RemotePayloadPrivacyError("remote query contains a prohibited credential")
    return safe


__all__ = [
    "PrivacyError",
    "MandatorySecretError",
    "RemotePayloadPrivacyError",
    "RecipientAuthorizationError",
    "PrivacyPolicy",
    "REDACTION_MARKER",
    "detect_secrets",
    "authorize_recipient",
    "guard_mandatory",
    "guard_worker_authority",
    "guard_worker_payload",
    "guard_worker_remote_payload",
    "has_worker_authority",
    "guard_remote_payload",
    "safe_query_payload",
    "sanitize_optional",
    "sanitize_payload",
    "sanitize_text",
    "worker_environment",
    "worker_prompt",
]
