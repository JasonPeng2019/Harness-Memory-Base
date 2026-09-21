from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from memory_harness import atlas, apc, experience, privacy


class PrivacyBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = privacy.PrivacyPolicy(
            known_secrets=("synthetic-secret-alpha-1234567890",),
            forbidden_environment_keys=("MEMORY_HARNESS_CONTROL_TOKEN",),
        )

    def test_mandatory_secret_blocks_without_rewriting_the_raw_source(self) -> None:
        raw_task = "Use synthetic-secret-alpha-1234567890 in the repository."
        with self.assertRaisesRegex(privacy.MandatorySecretError, "synthetic-secret"):
            privacy.guard_mandatory(raw_task, self.policy)
        self.assertIn("synthetic-secret-alpha-1234567890", raw_task)

    def test_optional_content_is_sanitized_and_raw_evidence_is_unchanged(self) -> None:
        raw = "Historical note mentions synthetic-secret-alpha-1234567890."
        sanitized = privacy.sanitize_optional(raw, self.policy)
        self.assertNotIn("synthetic-secret-alpha-1234567890", sanitized)
        self.assertIn("synthetic-secret-alpha-1234567890", raw)

    def test_worker_environment_has_no_control_credentials(self) -> None:
        environment = {
            "PATH": "/usr/bin",
            "MEMORY_HARNESS_CONTROL_TOKEN": "synthetic-secret-alpha-1234567890",
            "OPENAI_API_KEY": "provider-transport-secret",
        }
        safe = privacy.worker_environment(environment, self.policy)
        self.assertEqual(
            {"PATH": "/usr/bin", "OPENAI_API_KEY": "provider-transport-secret"},
            safe,
        )
        self.assertNotIn("synthetic-secret-alpha-1234567890", json.dumps(safe))

    def test_worker_prompt_has_no_control_credentials_or_forbidden_authority(self) -> None:
        prompt = privacy.worker_prompt(
            "Inspect the failing test.",
            {"steps": ["read", "fix"]},
            optional_content="Prior failure mentions synthetic-secret-alpha-1234567890.",
            privacy_policy=self.policy,
        )
        self.assertNotIn("synthetic-secret-alpha-1234567890", prompt)
        self.assertNotIn("MEMORY_HARNESS_CONTROL_TOKEN", prompt)
        self.assertNotIn("approve/publish/revoke", prompt)

    def test_mandatory_plan_secret_blocks_instead_of_being_rewritten(self) -> None:
        plan = {"steps": ["Use synthetic-secret-alpha-1234567890"]}
        with self.assertRaisesRegex(privacy.MandatorySecretError, "synthetic-secret"):
            privacy.worker_prompt(
                "Inspect the failing test.",
                plan,
                privacy_policy=self.policy,
            )
        self.assertIn("synthetic-secret-alpha-1234567890", plan["steps"][0])

    def test_experience_fixture_sanitizes_but_preserves_raw_authoritative_record(self) -> None:
        examples = experience.load_experience_examples(self.policy)
        self.assertTrue(examples)
        raw = examples[0].raw_content
        derived = experience.derived_optional_content(examples[0], self.policy)
        self.assertNotIn("synthetic-secret-alpha-1234567890", json.dumps(derived))
        self.assertIn("synthetic-secret-alpha-1234567890", raw)

    def test_atlas_query_never_contains_provider_authentication(self) -> None:
        query = atlas.build_atlas_query(
            "Find the prior regression repair",
            objective_id="objective-1",
            route="ordinary",
            privacy_policy=self.policy,
            credentials={"api_key": "synthetic-secret-alpha-1234567890"},
        )
        serialized = json.dumps(query.to_record())
        self.assertNotIn("synthetic-secret-alpha-1234567890", serialized)
        self.assertNotIn("api_key", serialized)

    def test_apc_payload_never_contains_provider_authentication(self) -> None:
        template = {
            "template_id": "template-1",
            "version": 1,
            "required_fields": ["failure"],
            "allowed_edits": ["bindings"],
        }
        request = apc.make_apc_request(
            template=template,
            parent_decision_id="decision-1",
            parent_objective_id="objective-1",
            permitted_edits=["bindings"],
            binding={
                "provider": "codex",
                "model": "model-a",
                "cli": "codex",
                "effort": "high",
                "source": "explicit",
            },
            credentials={"api_key": "synthetic-secret-alpha-1234567890"},
        )
        serialized = json.dumps(request)
        self.assertNotIn("synthetic-secret-alpha-1234567890", serialized)
        self.assertNotIn("api_key", serialized)


if __name__ == "__main__":
    unittest.main()
