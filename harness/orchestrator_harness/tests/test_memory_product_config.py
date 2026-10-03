from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from orchestrator_harness.config import (
    ResourceManifest,
    compute_config_identity,
    load_config,
)
from orchestrator_harness.memory_product_config import (
    MemoryProductConfigError,
    assert_exact_scope,
    initialize_secret_rotation_state,
    load_memory_product_config,
    read_known_secrets,
    record_secret_rotation_state,
    verify_secret_rotation_state,
)
from orchestrator_harness.epochs import EpochError, open_epoch


class MemoryProductConfigurationTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.store_root = self.root / "operator-memory"
        self.secrets = self.store_root / "secrets"
        self.secrets.mkdir(parents=True)
        self.secret = self.secrets / "content-secrets.txt"
        self.secret.write_text("alpha-secret\n", encoding="utf-8")
        if os.name == "posix":
            self.secret.chmod(0o600)
        self._write_json(
            self.root / "harness-config.json",
            {"root_workspace": str(self.workspace), "managed_coordination": "enabled"},
        )

    @staticmethod
    def _write_json(path: Path, value: object) -> None:
        path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")

    def _record(self, **changes: object) -> dict[str, object]:
        value: dict[str, object] = {
            "schema": "memory-product-config/v1",
            "application": "coding-agents",
            "namespace": "team-a",
            "project": "orchestrator",
            "owner": "ROOT",
            "store_root": str(self.store_root),
            "policy_generation": "policy-1",
            "known_secret_files": [str(self.secret)],
            "services": {
                "local_experience": {"enabled": True},
                "local_procedures": {"enabled": True},
                "everos": {"enabled": False},
                "atlas": {"enabled": False},
            },
        }
        value.update(changes)
        return value

    def _load(self, record: dict[str, object] | None = None, **kwargs: object):
        self._write_json(
            self.root / "memory-product-config.json", record or self._record()
        )
        return load_memory_product_config(self.root, **kwargs)

    def test_closed_record_binds_exact_scope_and_service_availability(self) -> None:
        config = self._load()
        self.assertEqual(
            {
                "application": "coding-agents",
                "namespace": "team-a",
                "project": "orchestrator",
                "owner": "ROOT",
            },
            config.scope,
        )
        self.assertTrue(config.services["local_experience"].available)
        self.assertFalse(config.services["atlas"].available)
        self.assertEqual(64, len(config.config_digest))
        self.assertEqual(config.scope, assert_exact_scope(config, dict(config.scope)))
        with self.assertRaisesRegex(MemoryProductConfigError, "scope"):
            assert_exact_scope(config, {**config.scope, "owner": "worker"})
        with self.assertRaisesRegex(MemoryProductConfigError, "exact"):
            assert_exact_scope(config, {**config.scope, "extra": "value"})

    def test_unknown_top_level_or_service_fields_fail_closed(self) -> None:
        with self.assertRaisesRegex(MemoryProductConfigError, "unknown key"):
            self._load(self._record(unexpected=True))
        for service, key in (
            ("local_experience", "credential_env"),
            ("everos", "database"),
            ("atlas", "raw_credential"),
        ):
            record = self._record()
            services = dict(record["services"])  # type: ignore[arg-type]
            services[service] = {"enabled": False, key: "not-allowed"}
            record["services"] = services
            with self.subTest(service=service, key=key), self.assertRaisesRegex(
                MemoryProductConfigError, "unknown key"
            ):
                self._load(record)

    def test_atlas_accepts_only_credential_environment_reference(self) -> None:
        record = self._record()
        services = dict(record["services"])  # type: ignore[arg-type]
        services["atlas"] = {
            "enabled": True,
            "credential_env": "TEST_ATLAS_URI",
            "database": "memory_product",
            "collection": "trusted_procedures",
            "index": "procedure_vectors",
        }
        record["services"] = services
        config = self._load(
            record,
            environ={"TEST_ATLAS_URI": "mongodb://user:literal-secret@example.invalid"},
            prerequisite_probe=lambda name: name == "atlas",
        )
        atlas = config.services["atlas"]
        self.assertTrue(atlas.available)
        self.assertEqual("TEST_ATLAS_URI", atlas.credential_env)
        serialized = json.dumps(config.identity_record(), sort_keys=True)
        self.assertIn("TEST_ATLAS_URI", serialized)
        self.assertNotIn("literal-secret", serialized)
        self.assertNotIn("mongodb://", serialized)

        with self.assertRaisesRegex(MemoryProductConfigError, "environment reference"):
            self._load(
                record,
                environ={},
                prerequisite_probe=lambda name: name == "atlas",
            )

    def test_enabled_unavailable_service_fails_before_product_effects(self) -> None:
        record = self._record()
        services = dict(record["services"])  # type: ignore[arg-type]
        services["everos"] = {"enabled": True}
        record["services"] = services
        with self.assertRaisesRegex(MemoryProductConfigError, "EverOS.*unavailable"):
            self._load(record, prerequisite_probe=lambda _name: False)

    def test_secret_paths_must_be_plain_private_files_beneath_secret_root(self) -> None:
        with self.assertRaisesRegex(MemoryProductConfigError, "absolute plain"):
            self._load(self._record(store_root="relative-memory"))

        outside = self.root / "outside.txt"
        outside.write_text("outside", encoding="utf-8")
        if os.name == "posix":
            outside.chmod(0o600)
        with self.assertRaisesRegex(MemoryProductConfigError, "store_root/secrets"):
            self._load(self._record(known_secret_files=[str(outside)]))

        link = self.secrets / "linked.txt"
        try:
            link.symlink_to(self.secret)
        except (OSError, NotImplementedError):
            pass
        else:
            with self.assertRaisesRegex(MemoryProductConfigError, "link|reparse"):
                self._load(self._record(known_secret_files=[str(link)]))

        if os.name == "posix":
            self.secret.chmod(0o644)
            with self.assertRaisesRegex(MemoryProductConfigError, "private"):
                self._load()

    def test_secret_rotation_hmac_detects_silent_change_without_serializing_secret(self) -> None:
        config = self._load()
        state = initialize_secret_rotation_state(config)
        self.assertEqual(state, verify_secret_rotation_state(config))
        self.assertEqual(("alpha-secret",), read_known_secrets(config))
        persisted = (self.store_root / ".memory-product-secret-state.json").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("alpha-secret", persisted)

        self.secret.write_text("rotated-secret\n", encoding="utf-8")
        if os.name == "posix":
            self.secret.chmod(0o600)
        reloaded_same_policy = self._load()
        self.assertEqual(config.config_digest, reloaded_same_policy.config_digest)
        self.assertNotIn("rotated-secret", json.dumps(reloaded_same_policy.identity_record()))
        with self.assertRaisesRegex(MemoryProductConfigError, "rotation"):
            verify_secret_rotation_state(reloaded_same_policy)
        with self.assertRaisesRegex(MemoryProductConfigError, "rotation"):
            read_known_secrets(reloaded_same_policy)
        with self.assertRaisesRegex(MemoryProductConfigError, "policy_generation"):
            record_secret_rotation_state(reloaded_same_policy)

        rotated = self._load(self._record(policy_generation="policy-2"))
        record_secret_rotation_state(rotated)
        self.assertEqual("policy-2", verify_secret_rotation_state(rotated).policy_generation)

    def test_config_digest_participates_in_harness_epoch_identity(self) -> None:
        first_product = self._load()
        first_harness = load_config(self.root)
        self.assertEqual(first_product.config_digest, first_harness.memory_product_config_identity)
        manifest = ResourceManifest()
        first_identity = compute_config_identity(first_harness, manifest)

        second_product = self._load(self._record(policy_generation="policy-2"))
        second_harness = load_config(self.root)
        self.assertEqual(
            second_product.config_digest, second_harness.memory_product_config_identity
        )
        self.assertNotEqual(
            first_identity, compute_config_identity(second_harness, manifest)
        )

    def test_active_epoch_rejects_memory_product_configuration_drift(self) -> None:
        self._load()
        first = load_config(self.root)
        manifest = ResourceManifest()
        open_epoch(first.runtime_root, first, manifest)

        self._load(self._record(policy_generation="policy-2"))
        changed = load_config(self.root)
        with self.assertRaisesRegex(EpochError, "configuration changed"):
            open_epoch(changed.runtime_root, changed, manifest)


if __name__ == "__main__":
    unittest.main()
