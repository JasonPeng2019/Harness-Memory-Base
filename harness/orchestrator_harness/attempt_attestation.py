"""Controller-only attestation for provider-attempt journals.

The provider and controller run as the same operating-system account on some
supported hosts, so a pathname outside the worktree is not an authentication
boundary.  The controller instead keeps a fresh HMAC key only in its Python
process while providers run.  Once the complete provider/helper boundary is
proven gone, it publishes the key and the exact ordered row hashes for that
run.  A later run uses a different key and run-bound attestation.

This is evidence integrity against a provider child, not a claim that files
are protected from the human account that owns the harness.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

from .core import canonical_json, content_hash, iso_utc


ATTEMPT_SCHEMA = "controller-attempt/v3"
JOURNAL_HEADER = {"schema": "controller-attempts/v2"}
ATTESTATION_SCHEMA = "controller-attempt-attestation/v1"


def attestation_path(attempts_path: str | Path, run_id: str) -> Path:
    """Return the per-run seal path without exposing the in-memory key early."""

    journal = Path(attempts_path)
    run_slot = hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:16]
    return journal.with_name(f"{journal.name}.{run_slot}.attestation.json")


def key_id(key: bytes) -> str:
    return hashlib.sha256(key).hexdigest()


def _signature_payload(record: Mapping[str, Any]) -> bytes:
    return canonical_json({
        name: value for name, value in record.items()
        if name not in {"content_hash", "controller_hmac_sha256"}
    })


def sign_attempt(record: Mapping[str, Any], key: bytes) -> dict[str, Any]:
    """Bind an attempt row to a controller-memory-only signing capability."""

    if len(key) < 32:
        raise ValueError("controller attempt key is too short")
    signed = dict(record)
    signed["schema"] = ATTEMPT_SCHEMA
    signed["controller_key_id"] = key_id(key)
    signed.pop("content_hash", None)
    signed.pop("controller_hmac_sha256", None)
    signed["controller_hmac_sha256"] = hmac.new(
        key, _signature_payload(signed), hashlib.sha256,
    ).hexdigest()
    signed["content_hash"] = content_hash(signed)
    return signed


def validate_signed_attempt(record: Mapping[str, Any], key: bytes) -> bool:
    """Return whether one row is content-exact and signed by ``key``."""

    if not isinstance(record, dict) or record.get("schema") != ATTEMPT_SCHEMA:
        return False
    signature = record.get("controller_hmac_sha256")
    if not isinstance(signature, str) or not re.fullmatch(r"[0-9a-f]{64}", signature):
        return False
    if record.get("controller_key_id") != key_id(key):
        return False
    if record.get("content_hash") != content_hash(dict(record)):
        return False
    expected = hmac.new(key, _signature_payload(record), hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature, expected)


def make_attestation(
    *, lane_id: str, run_id: str, key: bytes,
    row_content_hashes: Sequence[str],
) -> dict[str, Any]:
    """Publish verification material only after the provider boundary is gone."""

    record: dict[str, Any] = {
        "schema": ATTESTATION_SCHEMA,
        "lane_id": lane_id,
        "run_id": run_id,
        "controller_key_id": key_id(key),
        "verification_key": key.hex(),
        "row_content_hashes": list(row_content_hashes),
        "row_count": len(row_content_hashes),
        "provider_boundary_closed": True,
        "closed_at": iso_utc(),
    }
    record["content_hash"] = content_hash(record)
    return record


def verification_key(
    record: Mapping[str, Any], *, lane_id: str, run_id: str,
) -> bytes:
    """Validate a closed-run attestation and return its verification key."""

    if not isinstance(record, dict) or record.get("schema") != ATTESTATION_SCHEMA:
        raise ValueError("controller attempt attestation schema mismatch")
    if record.get("content_hash") != content_hash(dict(record)):
        raise ValueError("controller attempt attestation hash mismatch")
    if record.get("lane_id") != lane_id or record.get("run_id") != run_id:
        raise ValueError("controller attempt attestation ownership mismatch")
    if record.get("provider_boundary_closed") is not True:
        raise ValueError("controller attempt attestation is not closed")
    hashes = record.get("row_content_hashes")
    if (
        not isinstance(hashes, list)
        or record.get("row_count") != len(hashes)
        or any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)
               for value in hashes)
    ):
        raise ValueError("controller attempt attestation row index is malformed")
    raw_key = record.get("verification_key")
    if not isinstance(raw_key, str) or not re.fullmatch(r"[0-9a-f]{64,}", raw_key):
        raise ValueError("controller attempt attestation key is malformed")
    try:
        key = bytes.fromhex(raw_key)
    except ValueError as exc:
        raise ValueError("controller attempt attestation key is malformed") from exc
    if len(key) < 32 or record.get("controller_key_id") != key_id(key):
        raise ValueError("controller attempt attestation key identity mismatch")
    return key
