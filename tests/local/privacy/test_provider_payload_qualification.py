"""Real local child-process qualification for provider payload privacy boundaries."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from memory_harness import config, contracts, preparation, privacy, search, store
from orchestrator_harness import provider_network_payload


CONTROL_SECRET = "CONTROL-SECRET-MUST-NOT-CROSS"
PROVIDER_SECRET = "PROVIDER-TRANSPORT-ONLY"
CONTENT_SECRET = "CONTENT-SECRET-MUST-NOT-CROSS"


def _probe(tmp_path: Path) -> Path:
    path = tmp_path / "provider-probe.py"
    path.write_text(
        "import json,os,sys\n"
        "print(json.dumps({'argv':sys.argv[1:],'env':dict(os.environ),"
        "'prompt':sys.stdin.read()},sort_keys=True))\n",
        encoding="utf-8",
    )
    return path


def test_spawned_worker_gets_transport_auth_but_no_control_or_content_secret(tmp_path: Path) -> None:
    policy = privacy.PrivacyPolicy(
        known_secrets=(CONTENT_SECRET,),
        forbidden_environment_keys=("MEMORY_HARNESS_CONTROL_TOKEN",),
    )
    raw_environment = {
        "PATH": os.environ.get("PATH", ""),
        "MEMORY_HARNESS_CONTROL_TOKEN": CONTROL_SECRET,
        "OPENAI_API_KEY": PROVIDER_SECRET,
    }
    child_environment = privacy.worker_environment(raw_environment, policy)
    prompt = privacy.worker_prompt(
        "Inspect the parser", {"steps": ["read", "verify"]},
        optional_content=[
            {"id": "safe", "content": "reviewed parser evidence"},
            {"id": "unsafe", "content": CONTENT_SECRET},
        ],
        privacy_policy=policy,
    )
    completed = subprocess.run(
        [sys.executable, str(_probe(tmp_path)), "provider-transport"],
        input=prompt, text=True, capture_output=True, check=True,
        cwd=tmp_path, env=child_environment, timeout=5,
    )
    observed = json.loads(completed.stdout)
    assert observed["env"]["OPENAI_API_KEY"] == PROVIDER_SECRET
    assert "MEMORY_HARNESS_CONTROL_TOKEN" not in observed["env"]
    assert "reviewed parser evidence" in observed["prompt"]
    serialized = json.dumps(observed, sort_keys=True)
    assert CONTROL_SECRET not in serialized
    assert CONTENT_SECRET not in serialized
    assert PROVIDER_SECRET not in observed["prompt"]


@pytest.mark.parametrize(
    ("provider", "argv", "required"),
    [
        ("codex", ["codex", "exec", "-"], 'web_search="disabled"'),
        ("claude-code", ["claude", "--print"], "--disallowedTools"),
        ("qwen-code", ["qwen", "--model", "m"], "--exclude-tools"),
    ],
)
def test_soft_network_controls_reach_actual_spawn_argv(
    tmp_path: Path, provider: str, argv: list[str], required: str,
) -> None:
    if provider == "qwen-code":
        provider_network_payload.install_soft_controls(provider, tmp_path)
    with patch.object(provider_network_payload, "_qwen_cli_supports_deny", return_value=True):
        transformed, facts = provider_network_payload.resolve_launch(
            provider, tmp_path, argv, "soft_guardrail_network",
        )
    # Replace only the executable; the transformed arguments are passed to a
    # real child so quoting/ordering survives the subprocess boundary.
    completed = subprocess.run(
        [sys.executable, str(_probe(tmp_path)), *transformed[1:]],
        input="bounded task", text=True, capture_output=True, check=True,
        cwd=tmp_path, env={"PATH": os.environ.get("PATH", "")}, timeout=5,
    )
    observed = json.loads(completed.stdout)
    assert required in observed["argv"]
    assert facts["effective_profile"] == "soft_guardrail_network"
    assert facts["hardened_sandbox"] is False
    assert facts["shell_egress_possible"] is True


def test_restricted_local_discloses_service_gate_and_never_claims_hard_egress(
    tmp_path: Path,
) -> None:
    transformed, facts = provider_network_payload.resolve_launch(
        "codex", tmp_path, ["codex", "exec", "-"], "restricted_local",
    )
    assert 'web_search="disabled"' in transformed
    assert facts["atlas_task_path_reenabled_by_network_controls"] is False
    assert facts["effective_profile"] == "soft_guardrail_network"
    assert facts["hardened_sandbox"] is False
    assert "service" in facts["reason"]


def test_restricted_local_blocks_actual_remote_search_before_adapter_call(
    tmp_path: Path,
) -> None:
    """Prove the service-layer counter stays zero, not only the launch facts."""
    state = store.MemoryStore(tmp_path / "memory.sqlite3")
    state.initialize()
    calls = 0

    def remote_query(_query: dict) -> list[dict]:
        nonlocal calls
        calls += 1
        raise AssertionError("restricted-local began a remote service call")

    plan = contracts.make_plan(
        plan_id="plan-privacy", objective_id="objective-privacy", route="ordinary",
        state="accepted", accepted_by="ROOT", content={"steps": ["inspect"]},
    )
    card = contracts.make_task_card(
        task="inspect without remote retrieval", base_commit="base",
        memory_handoff=contracts.make_memory_handoff(
            objective_id="objective-privacy", route="ordinary", plan=plan,
            checkpoint="checkpoint-privacy",
        ),
    )
    remote = search.SearchStore(
        store_id="atlas-remote", kind="procedure", query=remote_query,
        requires_network=True,
    )
    try:
        result = preparation.PreparationService(
            store=state,
            config=config.resolve_config({
                "strategy": "standard", "atlas_shared_retrieval": True,
            }),
        ).prepare(
            task_card=card, plan=plan, objective_id="objective-privacy",
            request={"strategy": "standard", "atlas_shared_retrieval": True},
            network_mode="restricted_local", stores=[remote],
        )
        attempt = next(
            item for item in result.trace["attempts"]
            if item["store_id"] == "atlas-remote"
        )
        assert calls == 0
        assert attempt["status"] == "disabled"
        assert "never begins" in attempt["reason"]
    finally:
        state.close()


def test_provider_version_or_settings_mismatch_fails_closed_before_child(tmp_path: Path) -> None:
    provider_network_payload.install_soft_controls("qwen-code", tmp_path)
    settings = tmp_path / ".qwen/settings.json"
    payload = json.loads(settings.read_text(encoding="utf-8"))
    payload["tools"]["webSearch"]["enabled"] = True
    settings.write_text(json.dumps(payload), encoding="utf-8")
    calls: list[list[str]] = []
    transformed, facts = provider_network_payload.resolve_launch(
        "qwen-code", tmp_path, ["qwen", "--model", "m"], "soft_guardrail_network",
    )
    if facts["effective_profile"] == "soft_guardrail_network":
        calls.append(transformed)
    assert calls == []
    assert facts["effective_profile"] == "uncontrolled_network"
