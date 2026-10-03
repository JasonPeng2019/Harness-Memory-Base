from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from memory_harness import contracts


class ExactScopeIdentityTests(unittest.TestCase):
    def test_exact_scope_comparison_rejects_missing_extra_and_different_values(self) -> None:
        expected = {
            "application": "app",
            "namespace": "ns",
            "project": "project",
            "owner": "ROOT",
        }
        self.assertTrue(contracts.exact_scope_matches(expected, dict(expected)))
        self.assertEqual(expected, contracts.require_exact_scope(expected, dict(expected)))
        for actual in (
            {key: value for key, value in expected.items() if key != "owner"},
            {**expected, "extra": "value"},
            {**expected, "owner": "worker"},
        ):
            with self.subTest(actual=actual):
                self.assertFalse(contracts.exact_scope_matches(expected, actual))
                with self.assertRaisesRegex(contracts.ContractError, "scope"):
                    contracts.require_exact_scope(expected, actual)


if __name__ == "__main__":
    unittest.main()
