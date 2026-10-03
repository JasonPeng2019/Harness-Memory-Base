"""Trusted ROOT-side configuration for the composed memory product.

The file is always derived as ``<harness-root>/memory-product-config.json``;
no worker/task input selects it.  Its identity contains references and
availability facts, never credential or known-secret values.
"""

from __future__ import annotations

import hashlib
import hmac
import importlib.util
import json
import os
import re
import secrets as secrets_module
import stat
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Mapping

from .core import canonical_json, sha256_hex

MEMORY_PRODUCT_CONFIG_SCHEMA = "memory-product-config/v1"
SECRET_ROTATION_STATE_SCHEMA = "memory-product-secret-state/v1"
CONFIG_FILE_NAME = "memory-product-config.json"
SECRET_DIRECTORY_NAME = "secrets"
SECRET_KEY_FILE_NAME = ".memory-product-hmac.key"
SECRET_STATE_FILE_NAME = ".memory-product-secret-state.json"

SCOPE_FIELDS = frozenset({"application", "namespace", "project", "owner"})
SERVICE_NAMES = (
    "local_experience",
    "local_procedures",
    "everos",
    "atlas",
)
_TOP_LEVEL_FIELDS = frozenset({
    "schema",
    "application",
    "namespace",
    "project",
    "owner",
    "store_root",
    "policy_generation",
    "known_secret_files",
    "services",
})
_SERVICE_FIELDS = {
    "local_experience": frozenset({"enabled"}),
    "local_procedures": frozenset({"enabled"}),
    "everos": frozenset({"enabled", "credential_env"}),
    "atlas": frozenset({
        "enabled", "credential_env", "database", "collection", "index"
    }),
}
_ENV_REFERENCE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_LOCATION_VALUE = re.compile(r"^[A-Za-z0-9_-]+$")


class MemoryProductConfigError(ValueError):
    """Trusted product configuration is malformed or has drifted."""


@dataclass(frozen=True)
class ServiceConfig:
    name: str
    enabled: bool
    available: bool
    reason: str
    credential_env: str | None = None
    database: str | None = None
    collection: str | None = None
    index: str | None = None

    def identity_record(self) -> dict[str, Any]:
        """Return only nonsecret configured facts and resolved availability."""

        return {
            "enabled": self.enabled,
            "available": self.available,
            "reason": self.reason,
            "credential_env": self.credential_env,
            "database": self.database,
            "collection": self.collection,
            "index": self.index,
        }


@dataclass(frozen=True)
class MemoryProductConfig:
    harness_root: Path
    config_path: Path
    application: str
    namespace: str
    project: str
    owner: str
    store_root: Path
    policy_generation: str
    known_secret_files: tuple[Path, ...]
    services: Mapping[str, ServiceConfig]
    config_digest: str

    @property
    def scope(self) -> dict[str, str]:
        return {
            "application": self.application,
            "namespace": self.namespace,
            "project": self.project,
            "owner": self.owner,
        }

    @property
    def secrets_root(self) -> Path:
        return self.store_root / SECRET_DIRECTORY_NAME

    @property
    def credential_environment_references(self) -> tuple[str, ...]:
        return tuple(sorted(
            service.credential_env
            for service in self.services.values()
            if service.credential_env is not None
        ))

    def identity_record(self) -> dict[str, Any]:
        return {
            "schema": MEMORY_PRODUCT_CONFIG_SCHEMA,
            **self.scope,
            "store_root": str(self.store_root),
            "policy_generation": self.policy_generation,
            "known_secret_files": [str(path) for path in self.known_secret_files],
            "services": {
                name: self.services[name].identity_record()
                for name in SERVICE_NAMES
            },
        }


@dataclass(frozen=True)
class SecretRotationState:
    policy_generation: str
    config_digest: str
    secret_hmac: str

    def record(self) -> dict[str, str]:
        return {
            "schema": SECRET_ROTATION_STATE_SCHEMA,
            "policy_generation": self.policy_generation,
            "config_digest": self.config_digest,
            "secret_hmac": self.secret_hmac,
        }


def _nonempty_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise MemoryProductConfigError(f"{field} must be a canonical nonempty string")
    if any(ord(character) < 32 for character in value):
        raise MemoryProductConfigError(f"{field} contains control characters")
    return value


def _is_reparse(path: Path) -> bool:
    try:
        attributes = path.lstat().st_file_attributes
    except (AttributeError, OSError):
        return False
    flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & flag)


def _assert_no_link_components(path: Path, field: str) -> None:
    for component in (path, *path.parents):
        if component.is_symlink() or _is_reparse(component):
            raise MemoryProductConfigError(
                f"{field} must not contain a symbolic link or reparse point: {component}"
            )


def _absolute_plain_path(raw: Any, field: str, *, kind: str) -> Path:
    if not isinstance(raw, str) or not raw:
        raise MemoryProductConfigError(f"{field} must be an absolute plain path string")
    path = Path(raw)
    if not path.is_absolute() or os.path.normpath(raw) != raw:
        raise MemoryProductConfigError(f"{field} must be an absolute plain path")
    _assert_no_link_components(path, field)
    if kind == "file" and (not path.exists() or not path.is_file()):
        raise MemoryProductConfigError(f"{field} must identify an existing regular file")
    if kind == "directory" and (not path.exists() or not path.is_dir()):
        raise MemoryProductConfigError(f"{field} must identify an existing directory")
    return path


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return path != parent


def _validate_secret_file(raw: Any, *, store_root: Path) -> Path:
    path = _absolute_plain_path(raw, "known_secret_files entry", kind="file")
    secrets_root = store_root / SECRET_DIRECTORY_NAME
    if not _is_within(path, secrets_root):
        raise MemoryProductConfigError(
            "known secret files must be beneath store_root/secrets"
        )
    _assert_no_link_components(secrets_root, "store_root/secrets")
    mode = path.stat().st_mode
    if os.name == "posix" and mode & 0o077:
        raise MemoryProductConfigError(
            f"known secret file must have private POSIX permissions: {path}"
        )
    return path


def _default_prerequisite_probe(name: str) -> bool:
    if name in {"local_experience", "local_procedures"}:
        return True
    if name == "everos":
        return importlib.util.find_spec("everos") is not None
    if name == "atlas":
        return (
            importlib.util.find_spec("pymongo") is not None
            and importlib.util.find_spec("langchain_mongodb") is not None
        )
    return False


def _service(
    name: str,
    raw: Any,
    *,
    environ: Mapping[str, str],
    prerequisite_probe: Callable[[str], bool],
) -> ServiceConfig:
    if not isinstance(raw, Mapping):
        raise MemoryProductConfigError(f"service {name!r} must be an object")
    unknown = sorted(set(raw) - _SERVICE_FIELDS[name])
    if unknown:
        raise MemoryProductConfigError(
            f"service {name!r} has unknown key {unknown[0]!r}"
        )
    if "enabled" not in raw or not isinstance(raw["enabled"], bool):
        raise MemoryProductConfigError(f"service {name!r} enabled must be boolean")
    enabled = raw["enabled"]
    credential_env = raw.get("credential_env")
    if credential_env is not None:
        if not isinstance(credential_env, str) or not _ENV_REFERENCE.fullmatch(
            credential_env
        ):
            raise MemoryProductConfigError(
                f"service {name!r} credential_env must name an environment variable"
            )
    location: dict[str, str | None] = {
        key: raw.get(key) for key in ("database", "collection", "index")
    }
    for key, value in location.items():
        if value is not None and (
            not isinstance(value, str) or not _LOCATION_VALUE.fullmatch(value)
        ):
            raise MemoryProductConfigError(
                f"service {name!r} {key} must be a plain identifier"
            )
    if name == "atlas" and enabled:
        missing = [key for key in ("credential_env", "database", "collection", "index")
                   if raw.get(key) is None]
        if missing:
            raise MemoryProductConfigError(
                "enabled Atlas service is missing " + ", ".join(missing)
            )

    if not enabled:
        available = False
        reason = "disabled by trusted product configuration"
    elif credential_env is not None and not environ.get(credential_env):
        available = False
        reason = f"credential environment reference {credential_env!r} is unavailable"
    elif name in {"everos", "atlas"} and not prerequisite_probe(name):
        available = False
        label = "EverOS" if name == "everos" else "Atlas" if name == "atlas" else name
        reason = f"{label} prerequisite is unavailable"
    else:
        available = True
        reason = "enabled prerequisites available"
    if enabled and not available:
        raise MemoryProductConfigError(reason)
    return ServiceConfig(
        name=name,
        enabled=enabled,
        available=available,
        reason=reason,
        credential_env=credential_env,
        database=location["database"],
        collection=location["collection"],
        index=location["index"],
    )


def load_memory_product_config(
    harness_root: str | os.PathLike[str],
    *,
    environ: Mapping[str, str] | None = None,
    prerequisite_probe: Callable[[str], bool] | None = None,
) -> MemoryProductConfig:
    """Load the closed trusted config derived from one harness root."""

    root = Path(harness_root).absolute()
    path = root / CONFIG_FILE_NAME
    _absolute_plain_path(str(path), "memory product config", kind="file")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MemoryProductConfigError(f"memory product config is unreadable: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise MemoryProductConfigError("memory product config must be an object")
    unknown = sorted(set(raw) - _TOP_LEVEL_FIELDS)
    missing = sorted(_TOP_LEVEL_FIELDS - set(raw))
    if unknown:
        raise MemoryProductConfigError(
            f"memory product config has unknown key {unknown[0]!r}"
        )
    if missing:
        raise MemoryProductConfigError(
            f"memory product config is missing key {missing[0]!r}"
        )
    if raw["schema"] != MEMORY_PRODUCT_CONFIG_SCHEMA:
        raise MemoryProductConfigError(
            f"memory product config schema must be {MEMORY_PRODUCT_CONFIG_SCHEMA!r}"
        )
    scope = {field: _nonempty_string(raw[field], field) for field in SCOPE_FIELDS}
    store_root = _absolute_plain_path(raw["store_root"], "store_root", kind="directory")
    policy_generation = _nonempty_string(raw["policy_generation"], "policy_generation")
    secret_values = raw["known_secret_files"]
    if not isinstance(secret_values, list):
        raise MemoryProductConfigError("known_secret_files must be a list")
    secret_files = tuple(
        _validate_secret_file(value, store_root=store_root) for value in secret_values
    )
    if len(set(secret_files)) != len(secret_files):
        raise MemoryProductConfigError("known_secret_files must not contain duplicates")
    services_raw = raw["services"]
    if not isinstance(services_raw, Mapping) or set(services_raw) != set(SERVICE_NAMES):
        raise MemoryProductConfigError(
            "services must contain exactly local_experience, local_procedures, everos, and atlas"
        )
    environment = os.environ if environ is None else environ
    probe = prerequisite_probe or _default_prerequisite_probe
    services = {
        name: _service(
            name, services_raw[name], environ=environment, prerequisite_probe=probe
        )
        for name in SERVICE_NAMES
    }
    provisional = MemoryProductConfig(
        harness_root=root,
        config_path=path,
        application=scope["application"],
        namespace=scope["namespace"],
        project=scope["project"],
        owner=scope["owner"],
        store_root=store_root,
        policy_generation=policy_generation,
        known_secret_files=secret_files,
        services=MappingProxyType(services),
        config_digest="",
    )
    return replace(
        provisional,
        config_digest=sha256_hex(provisional.identity_record()),
    )


def assert_exact_scope(
    configuration: MemoryProductConfig, scope: Mapping[str, Any]
) -> dict[str, str]:
    """Reject any missing, additional, or different task-selected scope."""

    if not isinstance(scope, Mapping) or set(scope) != SCOPE_FIELDS:
        raise MemoryProductConfigError("scope must contain exactly four configured fields")
    normalized = {
        field: _nonempty_string(scope[field], f"scope.{field}") for field in SCOPE_FIELDS
    }
    if normalized != configuration.scope:
        raise MemoryProductConfigError("scope does not match the exact configured scope")
    return normalized


def read_known_secrets(
    configuration: MemoryProductConfig, *, verify_rotation: bool = True
) -> tuple[str, ...]:
    """Read ROOT-only secret contents for privacy-policy construction."""

    if verify_rotation:
        verify_secret_rotation_state(configuration)
    values: list[str] = []
    for path in configuration.known_secret_files:
        _validate_secret_file(str(path), store_root=configuration.store_root)
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise MemoryProductConfigError("known secret file is unreadable") from exc
        value = content.strip()
        if not value:
            raise MemoryProductConfigError("known secret file must not be empty")
        values.append(value)
    return tuple(values)


def _private_file(path: Path, field: str) -> None:
    _assert_no_link_components(path, field)
    if not path.is_file():
        raise MemoryProductConfigError(f"{field} is missing")
    if os.name == "posix" and path.stat().st_mode & 0o077:
        raise MemoryProductConfigError(f"{field} must have private POSIX permissions")


def _atomic_private_write(path: Path, data: bytes) -> None:
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{secrets_module.token_hex(8)}.tmp"
    )
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if os.name == "posix":
            path.chmod(0o600)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _key_path(configuration: MemoryProductConfig) -> Path:
    return configuration.store_root / SECRET_KEY_FILE_NAME


def _state_path(configuration: MemoryProductConfig) -> Path:
    return configuration.store_root / SECRET_STATE_FILE_NAME


def _load_key(configuration: MemoryProductConfig, *, create: bool) -> bytes:
    path = _key_path(configuration)
    if not path.exists():
        if not create:
            raise MemoryProductConfigError("secret rotation HMAC key is missing")
        _atomic_private_write(path, secrets_module.token_bytes(32))
    _private_file(path, "secret rotation HMAC key")
    try:
        key = path.read_bytes()
    except OSError as exc:
        raise MemoryProductConfigError("secret rotation HMAC key is unreadable") from exc
    if len(key) < 32:
        raise MemoryProductConfigError("secret rotation HMAC key is invalid")
    return key


def _secret_hmac(configuration: MemoryProductConfig, key: bytes) -> str:
    digest = hmac.new(key, digestmod=hashlib.sha256)
    digest.update(canonical_json({
        "schema": "memory-product-secret-set/v1",
        "policy_generation": configuration.policy_generation,
        "paths": [str(path) for path in configuration.known_secret_files],
    }))
    for path in configuration.known_secret_files:
        _validate_secret_file(str(path), store_root=configuration.store_root)
        content = path.read_bytes()
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def _load_state(configuration: MemoryProductConfig) -> SecretRotationState:
    path = _state_path(configuration)
    _private_file(path, "secret rotation state")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MemoryProductConfigError("secret rotation state is unreadable") from exc
    fields = {"schema", "policy_generation", "config_digest", "secret_hmac"}
    if not isinstance(raw, Mapping) or set(raw) != fields:
        raise MemoryProductConfigError("secret rotation state is not a closed record")
    if raw["schema"] != SECRET_ROTATION_STATE_SCHEMA:
        raise MemoryProductConfigError("secret rotation state schema mismatch")
    return SecretRotationState(
        policy_generation=_nonempty_string(raw["policy_generation"], "policy_generation"),
        config_digest=_nonempty_string(raw["config_digest"], "config_digest"),
        secret_hmac=_nonempty_string(raw["secret_hmac"], "secret_hmac"),
    )


def initialize_secret_rotation_state(
    configuration: MemoryProductConfig,
) -> SecretRotationState:
    """Create the operator-only key/state once, or verify an existing state."""

    if _state_path(configuration).exists():
        return verify_secret_rotation_state(configuration)
    key = _load_key(configuration, create=True)
    state = SecretRotationState(
        policy_generation=configuration.policy_generation,
        config_digest=configuration.config_digest,
        secret_hmac=_secret_hmac(configuration, key),
    )
    _atomic_private_write(_state_path(configuration), canonical_json(state.record()) + b"\n")
    return state


def verify_secret_rotation_state(
    configuration: MemoryProductConfig,
) -> SecretRotationState:
    """Fail closed on policy, configuration, key, or secret-content drift."""

    state = _load_state(configuration)
    key = _load_key(configuration, create=False)
    current_hmac = _secret_hmac(configuration, key)
    if state.policy_generation != configuration.policy_generation:
        raise MemoryProductConfigError("secret rotation policy_generation changed")
    if state.config_digest != configuration.config_digest:
        raise MemoryProductConfigError("secret rotation configuration identity changed")
    if not hmac.compare_digest(state.secret_hmac, current_hmac):
        raise MemoryProductConfigError(
            "secret rotation detected without a new policy_generation"
        )
    return state


def record_secret_rotation_state(
    configuration: MemoryProductConfig,
) -> SecretRotationState:
    """Record an explicit rotation, requiring a new policy generation."""

    path = _state_path(configuration)
    if not path.exists():
        return initialize_secret_rotation_state(configuration)
    prior = _load_state(configuration)
    key = _load_key(configuration, create=False)
    current_hmac = _secret_hmac(configuration, key)
    if (
        prior.policy_generation == configuration.policy_generation
        and not hmac.compare_digest(prior.secret_hmac, current_hmac)
    ):
        raise MemoryProductConfigError(
            "secret content changed without a new policy_generation"
        )
    state = SecretRotationState(
        policy_generation=configuration.policy_generation,
        config_digest=configuration.config_digest,
        secret_hmac=current_hmac,
    )
    _atomic_private_write(path, canonical_json(state.record()) + b"\n")
    return state


__all__ = [
    "CONFIG_FILE_NAME",
    "MEMORY_PRODUCT_CONFIG_SCHEMA",
    "MemoryProductConfig",
    "MemoryProductConfigError",
    "SECRET_ROTATION_STATE_SCHEMA",
    "SERVICE_NAMES",
    "SecretRotationState",
    "ServiceConfig",
    "assert_exact_scope",
    "initialize_secret_rotation_state",
    "load_memory_product_config",
    "read_known_secrets",
    "record_secret_rotation_state",
    "verify_secret_rotation_state",
]
