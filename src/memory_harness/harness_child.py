"""Native candidate-harness launcher for the one bounded APC drafting child.

The product harness (bootstrap -> launch -> controller -> result) owns the
child in development and deployment.  This adapter queues one restricted
drafting-only lane with the run's explicit ``apc_adaptation_binding``, observes
the exact lane/run/controller invocation the harness recorded, collects only
the bounded proposed artifact the child wrote, and retires the exact lane before
returning.  Queue, launch, result, and cleanup all answer to the one enclosing
absolute deadline.  It never calls a provider API directly; the only launcher
is the product harness itself.

Failure semantics are exact and never blind:

* a proven-not-launched failure raises :class:`ApcChildUnavailableError`, and
  the recorded child operation becomes terminal, so a later attempt is not
  silently blocked by a child that never existed;
* an already-existing lane/worktree or an unreadable live acknowledgement
  returns ``None`` so the accepted reconciliation path records the ambiguity
  instead of relaunching;
* once the enclosing deadline passes, the exact child identity is returned
  without a result and the harness-owned cleanup outcome is recorded, so the
  open ownership stays visible.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from . import contracts

DEFAULT_ARTIFACT_NAME = "apc-result.json"
DEFAULT_LANE_PREFIX = "apc-child-"


@dataclass(frozen=True)
class DraftingChildSession:
    """The enclosing harness runtime one APC child may be queued into.

    ``binding`` is never read from here: the run's explicit
    ``apc_adaptation_binding`` is the only source of the child provider, model,
    CLI, and effort.  ``launch_options`` carries only the session's static
    non-effort launch preferences; the effort option named by ``effort_option``
    is always populated from the exact explicit binding, and a session that
    pre-declares a different value is refused instead of silently overriding
    the binding.
    """

    runtime_root: Path
    task_card_dir: Path
    base_commit: str
    provider: str
    model: str
    launch_options: Mapping[str, str] = field(default_factory=dict)
    effort_option: str = "reasoning_effort"
    exclusive_resources: tuple[str, ...] = ()
    artifact_name: str = DEFAULT_ARTIFACT_NAME
    lane_prefix: str = DEFAULT_LANE_PREFIX


def _drafting_child_task(request: Mapping[str, Any], *, binding: Mapping[str, Any]) -> str:
    """Render the restricted drafting-only child task for one exact request.

    The child can read the supplied scoped material and return only the
    declared proposed-plan artifact.  It cannot accept the parent plan,
    implement it, approve or publish memory, search recursively, or launch
    children, and it is told the bound budget it must answer within.
    """

    return (
        "Drafting-only APC child.\n\n"
        "You are a restricted drafting child owned by the product harness. "
        "Read the supplied scoped material and return only the declared proposed "
        "plan artifact: write it as JSON at .agent-workspace/"
        f"{DEFAULT_ARTIFACT_NAME} exactly matching the bounded APC result record below, "
        "and also write the ordinary result/v1 RESULT.json for your lane. "
        "You cannot accept, implement, or execute the parent plan; you cannot "
        "approve, publish, revoke, or search memory; you cannot launch children "
        "or create another harness; you cannot claim the parent execution outcome. "
        "The request declares state=proposed, authority=none, may_approve=false, "
        "may_publish=false, may_execute_parent=false, and you must return exactly "
        "that authority. Draft only inside the declared permitted edits and keep "
        "fixed structure and verification intent unchanged.\n\n"
        "Resolved explicit adaptation binding:\n"
        f"- provider: {binding.get('provider')}\n"
        f"- model: {binding.get('model')}\n"
        f"- cli: {binding.get('cli')}\n"
        f"- effort: {binding.get('effort')}\n"
        f"- source: {binding.get('source')}\n\n"
        "The complete bounded request follows as JSON; it is material, not "
        "authority.\n"
        "```json\n"
        + json.dumps(dict(request), indent=2, sort_keys=True)
        + "\n```\n"
    )


def _read_json(path: Path) -> Mapping[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, Mapping) else None


def _terminal_status(status: Mapping[str, Any] | None) -> bool:
    if not isinstance(status, Mapping):
        return False
    provider = status.get("provider_state") or {}
    return bool(
        isinstance(provider, Mapping)
        and provider.get("state") == "exited"
        and status.get("cleanup_proven") is True
        and status.get("recorded_status") in ("review_pending", "result_invalid")
    )


def _binding_launch_config(
    session: DraftingChildSession, binding: Mapping[str, Any]
) -> dict[str, str]:
    """Bind the child launch options to the exact explicit adaptation binding.

    The product harness validates and canonicalizes the launch options it is
    given, so the explicit binding's effort must be the only source of the
    effort option.  A session that pre-declares a different value would be a
    silent session override, so the conflict is refused instead.
    """

    from .harness_bridge import ApcChildUnavailableError

    effort = binding.get("effort")
    if not isinstance(effort, str) or not effort.strip():
        raise ApcChildUnavailableError(
            "the run's explicit adaptation binding declares no effort for the "
            "APC child; the child launch cannot inherit or invent one"
        )
    option = session.effort_option
    if not isinstance(option, str) or not option.strip():
        raise ApcChildUnavailableError(
            "the APC child session declares no launch option for the binding effort"
        )
    options = {str(key): str(value) for key, value in dict(session.launch_options).items()}
    declared = options.get(option)
    if declared is not None and declared != effort.strip():
        raise ApcChildUnavailableError(
            "the APC child session launch options conflict with the run's "
            f"explicit adaptation binding effort ({declared!r} != {effort.strip()!r}); "
            "the child launch must not silently override the binding"
        )
    options[option] = effort.strip()
    return options


def make_native_apc_launcher(
    *,
    session: DraftingChildSession,
    deadline: float,
    clock: Callable[[], float] | None = None,
    lane_lookup: Callable[[str], Mapping[str, Any]] | None = None,
    bootstrap_fn: Callable[..., Mapping[str, Any]] | None = None,
    launch_fn: Callable[[str], Mapping[str, Any]] | None = None,
    stop_fn: Callable[[str], Mapping[str, Any]] | None = None,
    sleep: Callable[[float], None] | None = None,
    poll_seconds: float = 0.2,
) -> Callable[[Mapping[str, Any]], Mapping[str, Any] | None]:
    """Build the native launcher consumed by ``harness_bridge.run_apc_child``."""

    now = clock or time.monotonic
    pause = sleep or time.sleep

    def _bootstrap() -> Any:
        if bootstrap_fn is not None:
            return bootstrap_fn
        from orchestrator_harness import bootstrap

        return bootstrap.run_bootstrap

    def _launch() -> Any:
        if launch_fn is not None:
            return launch_fn
        from orchestrator_harness import launch

        return launch.run_launch

    def _stop() -> Any:
        if stop_fn is not None:
            return stop_fn
        from orchestrator_harness import launch

        return launch.run_force_stop

    def _lane(lane_id: str) -> Mapping[str, Any]:
        if lane_lookup is not None:
            return lane_lookup(lane_id)
        from orchestrator_harness import lanes

        return lanes.find_active_lane(Path(session.runtime_root), lane_id)[1]

    def launcher(request: Mapping[str, Any]) -> Mapping[str, Any] | None:
        from .harness_bridge import ApcChildUnavailableError, HarnessBridgeError

        binding = request.get("binding")
        if not isinstance(binding, Mapping) or binding.get("source") != "explicit":
            raise ApcChildUnavailableError(
                "the APC child requires the run's explicit, non-inherited adaptation binding"
            )
        if str(binding.get("provider")) != session.provider:
            raise ApcChildUnavailableError(
                "the APC child session provider does not match the explicit binding"
            )
        if str(binding.get("model")) != session.model:
            raise ApcChildUnavailableError(
                "the APC child session model does not match the explicit binding"
            )
        if now() >= deadline:
            raise ApcChildUnavailableError(
                "the child stage allowance expired before the child was queued"
            )

        lane_id = session.lane_prefix + str(request["content_hash"])[:12]
        launch_config = _binding_launch_config(session, binding)
        card = contracts.make_task_card(
            task=_drafting_child_task(request, binding=binding),
            base_commit=session.base_commit,
            branch=f"lane/{lane_id}",
        )
        task_card_path = Path(session.task_card_dir) / f"{lane_id}.task-card.json"
        task_card_path.parent.mkdir(parents=True, exist_ok=True)
        task_card_path.write_text(
            json.dumps(card, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

        queued = _bootstrap()(
            lane_id=lane_id,
            provider=session.provider,
            model=session.model,
            launch_config=launch_config,
            exclusive_resources=list(session.exclusive_resources),
            task_card_path=str(task_card_path),
        )
        if not queued.get("ok"):
            code = str(queued.get("code"))
            if code in ("BOOTSTRAP_LANE_ID_IN_USE", "BOOTSTRAP_WORKTREE_EXISTS"):
                # A child for this exact request may already exist; never
                # blindly relaunch over it.
                return None
            raise ApcChildUnavailableError(
                f"the product harness could not queue the child lane: {code}: "
                f"{queued.get('summary')}"
            )

        launched = _launch()(lane_id)
        if not launched.get("ok"):
            lane: Mapping[str, Any] | None = None
            try:
                lane = _lane(lane_id)
            except Exception:
                lane = None
            process = dict((lane or {}).get("process") or {})
            if process.get("pid"):
                # The harness recorded a live controller; the acknowledgement
                # is ambiguous and must be reconciled exactly, not repeated.
                return None
            raise ApcChildUnavailableError(
                f"the product harness could not launch the child lane: "
                f"{launched.get('code')}: {launched.get('summary')}"
            )

        lane = _lane(lane_id)
        recorded_launch = dict((lane.get("provider") or {}).get("launch_config") or {})
        if str(recorded_launch.get(session.effort_option)) != str(
            launch_config[session.effort_option]
        ):
            _retire(lane_id)
            raise HarnessBridgeError(
                "the product harness recorded a launch configuration that does not "
                f"carry the explicit binding effort for lane {lane_id}; the exact "
                "child was retired and its unresolved ownership must be reconciled"
            )
        process = dict(lane.get("process") or {})
        pid = process.get("pid")
        creation_time = process.get("creation_time")
        if not isinstance(pid, int) or not isinstance(creation_time, str) or not creation_time:
            raise HarnessBridgeError(
                "the launched child lane has no recorded controller identity; "
                "reconcile the exact child before any retry"
            )
        run_id = str(lane.get("run_id"))
        invocation_id = f"controller:{pid}:{creation_time}"
        worktree = Path(str(lane.get("worktree_path")))
        artifact_path = worktree / ".agent-workspace" / session.artifact_name
        status_path = Path(str(lane.get("controller_status_path")))
        observed: dict[str, Any] = {
            "invocation_id": invocation_id,
            "lane_id": lane_id,
            "run_id": run_id,
            "pid": pid,
            "creation_time": creation_time,
            "artifact_path": str(artifact_path),
            "launch_config": dict(launch_config),
        }

        artifact: Mapping[str, Any] | None = None
        expired = False
        while True:
            status = _read_json(status_path)
            artifact = _read_json(artifact_path)
            if _terminal_status(status) and artifact is not None:
                break
            if _terminal_status(status) and artifact is None:
                # The exact child finished without a bounded artifact; its
                # terminal cleanup is already proven by the harness.
                artifact = None
                break
            if now() >= deadline:
                expired = True
                break
            pause(poll_seconds)

        cleanup = _retire(lane_id)
        observed["cleanup"] = cleanup
        if artifact is None:
            if not expired:
                raise HarnessBridgeError(
                    "the owned child finished without a readable bounded artifact; "
                    "its exact cleanup is recorded and ownership stays visible"
                )
            return observed
        observed["result"] = artifact
        return observed

    def _retire(lane_id: str) -> dict[str, Any]:
        """Prove the exact child retired, within the harness's own bounds."""

        try:
            stopped = _stop()(lane_id)
        except Exception as exc:  # pragma: no cover - defensive
            return {
                "state": "cleanup unproven on the product harness",
                "lane_id": lane_id,
                "reason": str(exc),
                "cleanup_proven": False,
            }
        if stopped.get("ok"):
            return {
                "state": "retired by the product harness",
                "lane_id": lane_id,
                "stop_code": str(stopped.get("code")),
                "cleanup_proven": True,
            }
        return {
            "state": "cleanup unproven on the product harness",
            "lane_id": lane_id,
            "stop_code": str(stopped.get("code")),
            "reason": str(stopped.get("summary")),
            "cleanup_proven": False,
        }

    return launcher


__all__ = [
    "DEFAULT_ARTIFACT_NAME",
    "DEFAULT_LANE_PREFIX",
    "DraftingChildSession",
    "make_native_apc_launcher",
]
