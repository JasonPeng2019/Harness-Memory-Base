from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from orchestrator_harness.bootstrap import _remove_owned_tree


class RetryingTemporaryDirectory:
    """Test-only temporary root with bounded Linux/NFS teardown retries."""

    def __init__(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.name = self._temporary.name

    def __enter__(self) -> str:
        return self.name

    def __exit__(self, *_: object) -> None:
        self.cleanup()

    def cleanup(self) -> None:
        _remove_owned_tree(Path(self.name), timeout_seconds=5.0)
        self._temporary.cleanup()


def write_json(path: Path, value: dict[str, Any]) -> None:
    """Write one deterministic fixture record for product-owned tests."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def configure_local_memory_product(
    harness_root: Path,
    *,
    store_root: Path,
    scope: dict[str, str] | None = None,
    known_secrets: tuple[str, ...] = (),
) -> dict[str, str]:
    """Create one private, local-only trusted product config for tests."""

    from orchestrator_harness.memory_product_config import (
        initialize_secret_rotation_state,
        load_memory_product_config,
    )

    selected = scope or {
        "application": "test-application",
        "namespace": "test-namespace",
        "project": "test-project",
        "owner": "ROOT",
    }
    store_root.mkdir(parents=True, exist_ok=True)
    secrets_root = store_root / "secrets"
    secrets_root.mkdir(exist_ok=True)
    secret_files: list[str] = []
    for index, value in enumerate(known_secrets, start=1):
        path = secrets_root / f"known-{index}.txt"
        path.write_text(value + "\n", encoding="utf-8")
        path.chmod(0o600)
        secret_files.append(str(path))
    write_json(
        harness_root / "memory-product-config.json",
        {
            "schema": "memory-product-config/v1",
            **selected,
            "store_root": str(store_root),
            "policy_generation": "test-generation-1",
            "known_secret_files": secret_files,
            "services": {
                "local_experience": {"enabled": True},
                "local_procedures": {"enabled": True},
                "everos": {"enabled": False, "credential_env": None},
                "atlas": {
                    "enabled": False,
                    "credential_env": None,
                    "database": None,
                    "collection": None,
                    "index": None,
                },
            },
        },
    )
    configuration = load_memory_product_config(harness_root)
    initialize_secret_rotation_state(configuration)
    return dict(selected)
