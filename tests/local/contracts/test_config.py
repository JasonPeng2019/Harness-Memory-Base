from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from memory_harness import config


class FixedConfigurationTests(unittest.TestCase):
    def test_standard_problem_focused_and_deeper_resolve_deterministically(self) -> None:
        standard = config.resolve_config({"strategy": "standard"})
        problem = config.resolve_config({"strategy": "problem_focused"})
        deeper = config.resolve_config({"strategy": "deeper"})
        self.assertEqual("standard", standard.strategy)
        self.assertEqual("problem_focused", problem.strategy)
        self.assertEqual("deeper", deeper.strategy)
        self.assertEqual(standard, config.resolve_config({"strategy": "standard"}))

    def test_unknown_strategy_uses_recorded_standard_fallback(self) -> None:
        resolved = config.resolve_config({"strategy": "not-a-strategy"})
        self.assertEqual("standard", resolved.strategy)
        self.assertIn("fallback", resolved.reason.lower())

    def test_all_off_is_available_and_contains_no_memory_pipeline(self) -> None:
        resolved = config.resolve_config({"all_features": False})
        self.assertTrue(resolved.all_off)
        self.assertFalse(resolved.experience_read)
        self.assertFalse(resolved.experience_write)
        self.assertFalse(resolved.template_memory)
        self.assertFalse(resolved.apc)
        self.assertFalse(resolved.light_adaptation)

    def test_learned_mode_is_rejected_before_preparation(self) -> None:
        requests = [
            {"strategy": "learned"},
            {"learned_mode": True},
            {"learned_selection": True},
            {"policy_load": True},
            {"training": True},
            {"policy_update": True},
        ]
        for request in requests:
            with self.assertRaisesRegex(config.DeferredCapabilityError, "deferred/not implemented"):
                config.resolve_config(request)

    def test_feature_prerequisites_resolve_to_effective_off(self) -> None:
        resolved = config.resolve_config(
            {
                "experience_write": False,
                "generated_skill_creation": True,
                "template_memory": False,
                "apc": True,
                "light_adaptation": True,
            }
        )
        self.assertFalse(resolved.generated_skill_creation)
        self.assertFalse(resolved.apc)
        self.assertFalse(resolved.light_adaptation)

        deeper_disabled = config.resolve_config(
            {"strategy": "deeper", "deeper": False}
        )
        self.assertEqual("standard", deeper_disabled.strategy)
        self.assertIn("fallback", deeper_disabled.reason)


if __name__ == "__main__":
    unittest.main()
