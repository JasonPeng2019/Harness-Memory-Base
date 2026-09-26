"""In-process trusted SearchStores reach the real bootstrap worker handoff."""

from __future__ import annotations

import json
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from memory_harness import contracts
from tests.local.mvp.test_coherent_memory_path import (
    ATLAS_MARKER,
    EVEROS_MARKER,
    RAW_APPROVAL_MATERIAL,
    _CoherentFixture,
)

from orchestrator_harness import bootstrap, memory_handoff
from orchestrator_harness.tests.test_step04_launch_boundary import LaunchBoundaryFixture


class BootstrapSearchStoreTests(_CoherentFixture, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.harness = LaunchBoundaryFixture()
        self.addCleanup(self.harness.close)

    def _bootstrap(self, *, lane_id: str, card: dict, search_stores=None) -> tuple[dict, Path]:
        task_card_path = self.harness.root / f"{lane_id}-task-card.json"
        self.harness.write_json(task_card_path, card)
        worktree = self.harness.runtime / "worktrees" / self.harness.EPOCH / lane_id

        def fake_git_add(root: Path, branch: str, target: Path, base_commit: str) -> None:
            target.mkdir(parents=True, exist_ok=True)

        with (
            patch("orchestrator_harness.config.find_harness_root", return_value=self.harness.harness),
            patch.object(bootstrap, "open_epoch", return_value={"epoch_id": self.harness.EPOCH}),
            patch.object(bootstrap, "read_active_lanes", return_value=[]),
            patch.object(bootstrap, "_git_worktree_add", side_effect=fake_git_add),
            patch.object(bootstrap.subprocess, "run"),
        ):
            store_argument = {} if search_stores is None else {"search_stores": search_stores}
            result = bootstrap.run_bootstrap(
                lane_id=lane_id,
                provider="codex",
                model="test-model",
                launch_config=dict(self.harness.launch_config),
                exclusive_resources=[],
                task_card_path=str(task_card_path),
                **store_argument,
            )
        return result, worktree

    def test_accepted_standard_bootstrap_delivers_both_trusted_sources(self) -> None:
        credential = "SEARCH_STORE_CREDENTIAL_MUST_STAY_IN_PROCESS"
        live_query = self.everos_store.query
        everos_store = replace(
            self.everos_store,
            query=lambda request: live_query(request) if credential else (),
        )
        result, worktree = self._bootstrap(
            lane_id="lane-1", card=self.card,
            search_stores=(everos_store, self.atlas_store),
        )
        self.assertTrue(result["ok"], result)
        envelope = memory_handoff.load_envelope(worktree)
        self.assertIsNotNone(envelope)
        context = memory_handoff.load_final_context(worktree_path=worktree, envelope=envelope)
        self.assertEqual(
            {"everos-generated-skills", "atlas-shared-procedures"},
            {item["source_id"] for item in context["optional_content"]},
        )
        selected = {
            item["provenance"]["source_id"]: item["provenance"]
            for item in context["delivery_trace"]["selected"]
        }
        for source, procedure in (
            ("everos-generated-skills", self.everos_procedure),
            ("atlas-shared-procedures", self.atlas_procedure),
        ):
            self.assertEqual(procedure["logical_id"], selected[source]["logical_id"])
            self.assertEqual(procedure["revision_id"], selected[source]["revision_id"])
            delivered = next(
                item for item in context["optional_content"]
                if item["source_id"] == source
            )
            self.assertEqual(self.receiver, delivered["content"]["recipient"])
        prompt = (worktree / ".agent-workspace/worker-prompt.md").read_text(encoding="utf-8")
        for marker in (EVEROS_MARKER, ATLAS_MARKER):
            self.assertIn(marker, prompt)
            self.assertIn(marker, json.dumps(context))
        for source, procedure in (
            ("everos-generated-skills", self.everos_procedure),
            ("atlas-shared-procedures", self.atlas_procedure),
        ):
            self.assertIn(source, prompt)
            self.assertIn(procedure["revision_id"], prompt)
        self.assertNotIn(RAW_APPROVAL_MATERIAL, prompt)
        self.assertNotIn("authority_evidence", prompt)
        self.assertNotIn(RAW_APPROVAL_MATERIAL, json.dumps(context))
        self.assertEqual(2, len(self.everos.fake.search_calls))
        self.assertEqual(2, len(self.vector_store.calls))
        for path in (
            worktree / ".agent-workspace/task-card.json",
            self.harness.runtime / "epochs" / self.harness.EPOCH / "lanes" / "lane-1" / "lane.json",
            worktree / ".agent-workspace/invocation.json",
            worktree / ".agent-workspace/result-template.json",
            memory_handoff.memory_paths(worktree)[1],
        ):
            data = path.read_text(encoding="utf-8")
            self.assertNotIn(RAW_APPROVAL_MATERIAL, data)
            self.assertNotIn(credential, data)
            self.assertNotIn("SearchStore(", data)
        self.assertNotIn(credential, prompt)
        self.assertNotIn(credential, json.dumps(context))
        self.assertNotIn(credential, json.dumps(result))
        self.assertNotIn(credential, json.dumps(envelope))

    def test_default_and_all_off_make_no_optional_store_calls(self) -> None:
        default, default_worktree = self._bootstrap(lane_id="lane-1", card=self.card)
        self.assertTrue(default["ok"], default)
        default_envelope = memory_handoff.load_envelope(default_worktree)
        self.assertEqual([], default_envelope["optional_content"])
        self.assertEqual(0, len(self.everos.fake.search_calls))
        self.assertEqual(0, len(self.vector_store.calls))

        all_off_card = contracts.make_task_card(
            task=self.card["task"], base_commit=self.card["base_commit"],
            memory_handoff=contracts.make_memory_handoff(
                objective_id=self.plan["objective_id"], route="ordinary",
                plan=self.plan, checkpoint="checkpoint-1",
                configuration={"all_features": False},
            ),
        )
        all_off, all_off_worktree = self._bootstrap(
            lane_id="all-off", card=all_off_card,
            search_stores=(self.everos_store, self.atlas_store),
        )
        self.assertTrue(all_off["ok"], all_off)
        self.assertIsNone(memory_handoff.load_envelope(all_off_worktree))
        self.assertEqual(0, len(self.everos.fake.search_calls))
        self.assertEqual(0, len(self.vector_store.calls))


if __name__ == "__main__":
    unittest.main()
