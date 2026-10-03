"""The one public launcher: ``python -m orchestrator_harness.operator_launch``.

Every public command returns the small structured result ``{ ok, code,
summary, evidence_paths, next_action }``.  Success prints a short human
status line on stdout and exits 0; failure prints one stable failure code and
a message on stderr and exits non-zero.  ``--json`` emits the machine-readable
result object instead.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from . import (
    bootstrap,
    launch,
    memory_commands,
    resume,
    review,
    scan_watch,
    setup,
    shutdown,
    visualizer_launch,
    view_term,
)
from .config import find_harness_root, load_config, load_resource_manifest
from .lanes import LaneError, find_active_lane
from .leases import (
    FORCE_RELEASE_CONFIG_INVALID,
    FORCE_RELEASE_DELETE_FAILED,
    FORCE_RELEASE_HOLDER_LIVE,
    FORCE_RELEASE_HOLDER_UNPROVEN,
    FORCE_RELEASE_AUDIT_START_FAILED,
    FORCE_RELEASE_AUDIT_TERMINAL_FAILED,
    FORCE_RELEASE_LEASE_INVALID,
    FORCE_RELEASE_LEASE_MISSING,
    FORCE_RELEASE_OK,
    FORCE_RELEASE_UNDECLARED,
    LeaseError,
    force_release_lease_audited,
    force_release_audit_path,
    lease_path,
)
from .manager_queue import (
    EVENT_TYPES,
    MANAGER_ACK_ALREADY_ACKNOWLEDGED,
    MANAGER_ACK_EVENT_NOT_FOUND,
    MANAGER_ACK_NOT_ROOT_EVENT,
    MANAGER_CLOSE_ALREADY_CLOSED,
    MANAGER_CLOSE_INVALID_OUTCOME,
    MANAGER_CLOSE_NOT_ACKNOWLEDGED,
    ManagerQueueError,
    acknowledge_event,
    append_assignment,
    close_event,
    read_manager_queue,
)

SEND_LANE_NOT_FOUND = "SEND_LANE_NOT_FOUND"
SEND_LANE_NOT_MANAGED = "SEND_LANE_NOT_MANAGED"
SEND_LANE_NOT_RUNNING = "SEND_LANE_NOT_RUNNING"
SEND_LANE_WRITE_FAILED = "SEND_LANE_WRITE_FAILED"


def _provider_option(value: str) -> tuple[str, str]:
    """Parse one explicit provider preference without interpreting its value."""
    key, separator, configured = value.partition("=")
    if not separator or not key.strip() or not configured.strip():
        raise argparse.ArgumentTypeError("provider options must use NAME=VALUE")
    return key.strip(), configured.strip()


def _provider_options(values: list[tuple[str, str]]) -> dict[str, str]:
    configured: dict[str, str] = {}
    for key, value in values:
        if key in configured:
            raise ValueError(f"duplicate provider option: {key}")
        configured[key] = value
    return configured


def _emit(result: dict[str, Any], *, as_json: bool) -> int:
    """Print one structured result and return the process exit code."""
    if as_json:
        sys.stdout.write(json.dumps(result, sort_keys=True, indent=2) + "\n")
    else:
        if result.get("ok"):
            sys.stdout.write(str(result.get("summary", "")).strip() + "\n")
        else:
            sys.stderr.write(
                f"{result.get('code', 'FAILED')}: {result.get('summary', '')}\n"
            )
        visualizer = result.get("visualizer")
        if isinstance(visualizer, dict) and visualizer.get("warning") is True:
            sys.stderr.write(
                f"{visualizer.get('code', 'VISUALIZER_WARNING')}: "
                f"{visualizer.get('summary', '')}. "
                f"{visualizer.get('next_action', 'run `orchestrator-harness view` manually')}\n"
            )
    return 0 if result.get("ok") else 1


def _manager_acknowledge(event_id: str) -> dict[str, Any]:
    try:
        harness_root = find_harness_root()
        config = load_config(harness_root)
    except Exception as exc:
        return {
            "ok": False,
            "code": MANAGER_ACK_EVENT_NOT_FOUND,
            "summary": str(exc),
            "evidence_paths": [],
            "next_action": "fix the configuration and re-run setup",
        }
    rt = config.runtime_root
    try:
        queue = read_manager_queue(rt)
        event = next(
            (item for item in queue.get("events", []) if item.get("event_id") == event_id),
            None,
        )
        if event is None:
            raise ManagerQueueError(
                MANAGER_ACK_EVENT_NOT_FOUND, f"event not found: {event_id}"
            )
        if event.get("type") not in EVENT_TYPES:
            raise ManagerQueueError(
                MANAGER_ACK_NOT_ROOT_EVENT,
                f"event {event_id} is not a valid ROOT event",
            )
        acknowledge_event(rt, event_id)
    except ManagerQueueError as exc:
        return {
            "ok": False,
            "code": exc.code,
            "summary": str(exc),
            "evidence_paths": [],
            "next_action": "handle the event, then `manager close`",
        }
    except Exception as exc:
        return {
            "ok": False,
            "code": MANAGER_ACK_EVENT_NOT_FOUND,
            "summary": str(exc),
            "evidence_paths": [],
            "next_action": "resolve the error and retry",
        }
    return {
        "ok": True,
        "code": "MANAGER_ACK_OK",
        "summary": f"event {event_id} acknowledged",
        "evidence_paths": [],
        "next_action": "handle the event, then `manager close`",
    }


def _manager_close(
    event_id: str, outcome: str, summary: str | None
) -> dict[str, Any]:
    try:
        harness_root = find_harness_root()
        config = load_config(harness_root)
    except Exception as exc:
        return {
            "ok": False,
            "code": MANAGER_CLOSE_NOT_ACKNOWLEDGED,
            "summary": str(exc),
            "evidence_paths": [],
            "next_action": "fix the configuration and re-run setup",
        }
    rt = config.runtime_root
    try:
        queue = read_manager_queue(rt)
        event = next(
            (item for item in queue.get("events", []) if item.get("event_id") == event_id),
            None,
        )
        if event is None:
            raise ManagerQueueError(
                MANAGER_CLOSE_NOT_ACKNOWLEDGED, f"event not found: {event_id}"
            )
        if event.get("type") not in EVENT_TYPES:
            raise ManagerQueueError(
                MANAGER_CLOSE_NOT_ACKNOWLEDGED,
                f"event {event_id} is not a valid ROOT event",
            )
        close_event(rt, event_id, outcome, summary=summary)
    except ManagerQueueError as exc:
        return {
            "ok": False,
            "code": exc.code,
            "summary": str(exc),
            "evidence_paths": [],
            "next_action": "closing the event does not clear the underlying condition",
        }
    except Exception as exc:
        return {
            "ok": False,
            "code": MANAGER_CLOSE_NOT_ACKNOWLEDGED,
            "summary": str(exc),
            "evidence_paths": [],
            "next_action": "resolve the error and retry",
        }
    return {
        "ok": True,
        "code": "MANAGER_CLOSE_OK",
        "summary": f"event {event_id} closed as {outcome}",
        "evidence_paths": [],
        "next_action": "none",
    }


def _send_lane_notification(lane_id: str, prompt: str) -> dict[str, Any]:
    try:
        harness_root = find_harness_root()
        config = load_config(harness_root)
    except Exception as exc:
        return {
            "ok": False,
            "code": SEND_LANE_NOT_FOUND,
            "summary": str(exc),
            "evidence_paths": [],
            "next_action": "fix the configuration and re-run setup",
        }
    rt = config.runtime_root
    try:
        _epoch_id, lane = find_active_lane(rt, lane_id)
    except Exception as exc:
        return {
            "ok": False,
            "code": SEND_LANE_NOT_FOUND,
            "summary": f"lane not found: {lane_id}",
            "evidence_paths": [],
            "next_action": "check the lane id",
        }
    if not lane.get("incoming_queue_path"):
        return {
            "ok": False,
            "code": SEND_LANE_NOT_MANAGED,
            "summary": f"lane {lane_id} is not a managed lane",
            "evidence_paths": [],
            "next_action": "use a managed lane for ROOT-to-worker assignments",
        }
    from . import processes

    process = lane.get("process") or {}
    if lane.get("lifecycle") != "running" or not processes.identity_matches(
        process.get("pid"), process.get("creation_time")
    ):
        return {
            "ok": False,
            "code": SEND_LANE_NOT_RUNNING,
            "summary": f"lane {lane_id} is not running",
            "evidence_paths": [],
            "next_action": "launch the lane before messaging it",
        }
    try:
        assignment = append_assignment(rt, lane, prompt)
    except ManagerQueueError as exc:
        return {
            "ok": False,
            "code": SEND_LANE_WRITE_FAILED,
            "summary": str(exc),
            "evidence_paths": [],
            "next_action": "resolve the error and retry",
        }
    return {
        "ok": True,
        "code": "SEND_LANE_OK",
        "summary": f"assignment {assignment['event_id']} appended to lane {lane_id}",
        "evidence_paths": [str(lane["incoming_queue_path"])],
        "next_action": "the worker acts via its lane-assignment skill",
    }


def _lease_force_release(resource_id: str) -> dict[str, Any]:
    try:
        harness_root = find_harness_root()
        config = load_config(harness_root)
        manifest = load_resource_manifest(harness_root)
    except Exception as exc:
        return {
            "ok": False,
            "code": FORCE_RELEASE_CONFIG_INVALID,
            "summary": str(exc),
            "evidence_paths": [],
            "next_action": "fix the configuration and re-run setup",
        }
    if not manifest.is_declared(resource_id):
        return {
            "ok": False,
            "code": FORCE_RELEASE_UNDECLARED,
            "summary": f"resource not declared: {resource_id}",
            "evidence_paths": [],
            "next_action": "declare the resource in resource-manifest.json",
        }
    rt = config.runtime_root

    def resolve_current_lane(lane_id: str) -> dict[str, Any] | None:
        try:
            _epoch_id, lane = find_active_lane(rt, lane_id)
        except LaneError:
            return None
        return lane

    try:
        audit_result = force_release_lease_audited(
            rt, resource_id, lane_resolver=resolve_current_lane
        )
    except LeaseError as exc:
        released = bool(getattr(exc, "released", False))
        audit_path = str(force_release_audit_path(rt))
        evidence = [audit_path, str(lease_path(rt, resource_id))]
        result: dict[str, Any] = {
            "ok": False,
            "code": exc.code,
            "summary": str(exc),
            "evidence_paths": evidence,
            "next_action": {
                FORCE_RELEASE_AUDIT_START_FAILED: "repair runtime audit storage; the lease was not deleted",
                FORCE_RELEASE_AUDIT_TERMINAL_FAILED: (
                    "the lease release already happened; retain STARTED evidence and repair audit storage"
                ),
                FORCE_RELEASE_LEASE_MISSING: "the resource has no lease; nothing to release",
                FORCE_RELEASE_LEASE_INVALID: "resolve the invalid lease record and retry",
                FORCE_RELEASE_HOLDER_LIVE: "stop the exact holder process before force-releasing",
                FORCE_RELEASE_HOLDER_UNPROVEN: "prove the holder dead or retire/abandon the lane before retrying",
                FORCE_RELEASE_DELETE_FAILED: "resolve the error and retry",
            }.get(exc.code, "resolve the error and retry"),
            "released": released,
        }
        if hasattr(exc, "lease_absent"):
            result["lease_absent"] = bool(exc.lease_absent)
        return result
    return {
        "ok": True,
        "code": FORCE_RELEASE_OK,
        "summary": f"lease force-released: {resource_id}; audit recorded",
        "evidence_paths": [str(lease_path(rt, resource_id)), audit_result["audit_path"]],
        "next_action": "none",
        "released": True,
        "lease_absent": bool(audit_result["lease_absent"]),
    }


def _nonblank_summary(value: str) -> str:
    summary = value.strip()
    if not summary:
        raise argparse.ArgumentTypeError("summary must be nonblank")
    return summary


def _json_value(value: str) -> Any:
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise argparse.ArgumentTypeError(f"value must be valid JSON: {exc.msg}") from exc


def _json_object(value: str) -> dict[str, Any]:
    decoded = _json_value(value)
    if not isinstance(decoded, dict):
        raise argparse.ArgumentTypeError("value must be a JSON object")
    return decoded


def _add_expected_config(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--expected-config-digest", required=True)
    parser.add_argument("--expected-policy-generation", required=True)


def _add_plan_observation(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--lane-id", required=True)
    parser.add_argument("--expected-run-id", required=True)
    parser.add_argument("--expected-task-card-digest", required=True)
    parser.add_argument("--disposition-id", required=True)
    parser.add_argument("--expected-disposition-digest", required=True)
    parser.add_argument("--expected-decision-id", required=True)
    parser.add_argument("--expected-objective-id", required=True)


def _add_pending_plan_observation(parser: argparse.ArgumentParser) -> None:
    _add_plan_observation(parser)
    parser.add_argument("--expected-plan-id", required=True)
    parser.add_argument("--expected-plan-digest", required=True)


def _add_memory_parser(subparsers: Any) -> None:
    memory = subparsers.add_parser(
        "memory", help="ROOT-only memory, trust, effect, and snapshot administration"
    )
    memory_sub = memory.add_subparsers(dest="memory_command", required=True)

    config = memory_sub.add_parser("config", help="trusted memory-product configuration")
    config_sub = config.add_subparsers(dest="config_command", required=True)
    config_sub.add_parser("status", help="inspect nonsecret resolved configuration")
    for name in ("init-secret-state", "rotate-secret-state"):
        action = config_sub.add_parser(name, help=f"{name.replace('-', ' ')}")
        _add_expected_config(action)
        if name == "rotate-secret-state":
            action.add_argument("--expected-prior-policy-generation", required=True)

    plan = memory_sub.add_parser("plan", help="review exact pending plans")
    plan_sub = plan.add_subparsers(dest="plan_command", required=True)
    inspect = plan_sub.add_parser("inspect", help="inspect a pending plan")
    _add_plan_observation(inspect)
    for name in ("propose", "correct"):
        action = plan_sub.add_parser(name, help=f"{name} a distinct exact plan revision")
        _add_pending_plan_observation(action)
        action.add_argument("--new-plan-id", required=True)
        action.add_argument("--content-json", dest="plan_content", required=True, type=_json_value)
    accept = plan_sub.add_parser("accept", help="accept and safely reprepare an exact pending plan")
    _add_pending_plan_observation(accept)
    accept.add_argument("--checkpoint", required=True)
    reject = plan_sub.add_parser("reject", help="reject an exact pending plan")
    _add_pending_plan_observation(reject)
    reject.add_argument("--reason", required=True, type=_nonblank_summary)
    reject.add_argument("--fresh-plan-id", required=True)

    procedure = memory_sub.add_parser("procedure", help="govern trusted procedure revisions")
    procedure_sub = procedure.add_subparsers(dest="procedure_command", required=True)
    approve = procedure_sub.add_parser("approve", help="approve one exact procedure revision")
    _add_expected_config(approve)
    approve.add_argument("--procedure-json", dest="procedure", required=True, type=_json_object)
    approve.add_argument("--expected-logical-id", required=True)
    approve.add_argument("--expected-revision-id", required=True)
    approve.add_argument("--expected-revision-digest", required=True)
    approve.add_argument("--expected-origin", required=True, choices=["curated", "generated"])
    approve.add_argument("--approval-id", required=True)

    represent = procedure_sub.add_parser(
        "represent", help="create the canonical search representation for one revision"
    )
    _add_expected_config(represent)
    represent.add_argument("--logical-id", required=True)
    represent.add_argument("--revision-id", required=True)
    represent.add_argument("--expected-revision-digest", required=True)
    represent.add_argument(
        "--expected-origin", required=True, choices=["curated", "generated"]
    )
    represent.add_argument("--search-text", required=True, type=_nonblank_summary)

    designate = procedure_sub.add_parser("designate", help="designate one approved project revision")
    _add_expected_config(designate)
    designate.add_argument("--logical-id", required=True)
    designate.add_argument("--revision-id", required=True)
    designate.add_argument("--expected-revision-digest", required=True)
    designate.add_argument("--approval-id", required=True)
    designate.add_argument("--expected-approval-digest", required=True)
    designate.add_argument("--expected-origin", required=True, choices=["curated", "generated"])

    publish = procedure_sub.add_parser("publish", help="publish through configured governed Atlas")
    _add_expected_config(publish)
    publish.add_argument("--logical-id", required=True)
    publish.add_argument("--revision-id", required=True)
    publish.add_argument("--expected-revision-digest", required=True)
    publish.add_argument("--approval-id", required=True)
    publish.add_argument("--expected-approval-digest", required=True)
    publish.add_argument("--representation-id", required=True)
    publish.add_argument("--expected-representation-digest", required=True)
    publish.add_argument("--designation-id", required=True)
    publish.add_argument("--expected-designation-digest", required=True)

    withdraw = procedure_sub.add_parser("withdraw", help="withdraw one exact current designation")
    _add_expected_config(withdraw)
    withdraw.add_argument("--logical-id", required=True)
    withdraw.add_argument("--expected-designation-id", required=True)
    withdraw.add_argument("--expected-revision-id", required=True)
    withdraw.add_argument("--expected-generation", required=True, type=int)
    withdraw.add_argument("--expected-designation-digest", required=True)

    revoke = procedure_sub.add_parser("revoke", help="revoke one exact procedure revision")
    _add_expected_config(revoke)
    revoke.add_argument("--revision-id", required=True)
    revoke.add_argument("--expected-revision-digest", required=True)
    revoke.add_argument("--expected-origin", required=True, choices=["curated", "generated"])
    revoke.add_argument("--reason", required=True, type=_nonblank_summary)

    effects = memory_sub.add_parser("effects", help="inspect or reconcile pending effects")
    effects_sub = effects.add_subparsers(dest="effects_command", required=True)
    listing = effects_sub.add_parser("list", help="list privacy-safe actionable effect summaries")
    listing.add_argument("--outcome-id")
    reconcile = effects_sub.add_parser("reconcile", help="reconcile exact observed effect evidence")
    _add_expected_config(reconcile)
    reconcile.add_argument("--operation-id", required=True)
    reconcile.add_argument("--expected-status", required=True)
    reconcile.add_argument("--expected-version", required=True, type=int)
    reconcile.add_argument("--expected-kind", required=True)
    reconcile.add_argument("--expected-source-digest", required=True)
    reconcile.add_argument("--evidence-json", dest="evidence", required=True, type=_json_object)
    reconcile.add_argument("--result", required=True, choices=["acknowledged", "absent", "idempotent"])
    reconcile.add_argument("--claim-id")

    snapshot = memory_sub.add_parser("snapshot", help="authorized snapshot operations")
    snapshot_sub = snapshot.add_subparsers(dest="snapshot_command", required=True)
    export = snapshot_sub.add_parser("export", help="export the configured central store")
    _add_expected_config(export)
    export.add_argument("--snapshot-id", required=True)
    readiness = snapshot_sub.add_parser("readiness", help="fully inspect one derived snapshot")
    _add_expected_config(readiness)
    readiness.add_argument("--snapshot-id", required=True)
    readiness.add_argument("--expected-snapshot-digest", required=True)
    restore = snapshot_sub.add_parser("import", help="restore into a new derived store")
    _add_expected_config(restore)
    restore.add_argument("--snapshot-id", required=True)
    restore.add_argument("--expected-snapshot-digest", required=True)
    restore.add_argument("--restore-id", required=True)
    restore.add_argument(
        "--required-capability", action="append", choices=["local", "experience", "atlas"]
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="operator_launch",
        description="The harness v2 public launcher.",
    )
    parser.add_argument("--json", action="store_true", help="emit the machine-readable result object")
    subparsers = parser.add_subparsers(dest="command", required=True)

    harness = subparsers.add_parser("harness", help="runtime lifecycle commands")
    harness_sub = harness.add_subparsers(dest="harness_command", required=True)
    setup_parser = harness_sub.add_parser("setup", help="one-time idempotent integration")
    setup_parser.add_argument(
        "--overwrite",
        action="store_true",
        help="replace harness-owned payloads; shared provider configuration is merged",
    )
    setup_parser.add_argument(
        "--check",
        action="store_true",
        help="validate and report the complete setup plan without writing",
    )
    harness_sub.add_parser("shutdown", help="end the whole runtime")

    lane = subparsers.add_parser("lane", help="lane lifecycle commands")
    lane_sub = lane.add_subparsers(dest="lane_command", required=True)
    bootstrap_parser = lane_sub.add_parser("bootstrap", help="prepare one lane")
    bootstrap_parser.add_argument("--lane-id", required=True)
    bootstrap_parser.add_argument("--provider", required=True)
    bootstrap_parser.add_argument("--model", required=True)
    bootstrap_parser.add_argument(
        "--provider-option",
        action="append",
        default=[],
        type=_provider_option,
        metavar="NAME=VALUE",
        help="explicit provider launch preference; repeat for each adapter-required option",
    )
    bootstrap_parser.add_argument("--exclusive-resource", action="append", default=[])
    bootstrap_parser.add_argument("--task-card", required=True)
    launch_parser = lane_sub.add_parser("launch", help="start one prepared lane")
    launch_parser.add_argument("--lane-id", required=True)
    launch_parser.add_argument(
        "--no-visualizer",
        action="store_true",
        help="do not automatically open the live viewer for this epoch",
    )
    review_parser = lane_sub.add_parser("completion-review", help="record ROOT's review and acceptance")
    review_selector = review_parser.add_mutually_exclusive_group(required=True)
    review_selector.add_argument("--event-id")
    review_selector.add_argument("--lane-id")
    review_parser.add_argument("--review-outcome", required=True, choices=["PASS", "FAIL", "BLOCKED", "UNKNOWN"])
    review_parser.add_argument("--approval", required=True, choices=["ACCEPTED", "REJECTED"])
    review_parser.add_argument("--review-summary", required=True)
    review_parser.add_argument("--evidence", action="append", default=[])
    review_parser.add_argument("--force-accept", action="store_true")
    review_parser.add_argument("--force-reason")
    force_stop_parser = lane_sub.add_parser("force-stop", help="hard-stop one stuck lane")
    force_stop_parser.add_argument("--lane-id", required=True)
    retire_parser = lane_sub.add_parser("retire", help="gracefully retire one accepted lane")
    retire_parser.add_argument("--acceptance-ref", required=True)

    resume_parser = subparsers.add_parser("resume-lane", help="re-run a stopped, unaccepted lane")
    resume_parser.add_argument("--lane-id", required=True)
    resume_parser.add_argument("--resume-task-card", required=True)
    resume_parser.add_argument("--rationale")

    manager = subparsers.add_parser("manager", help="manager-queue commands (managed)")
    manager_sub = manager.add_subparsers(dest="manager_command", required=True)
    ack_parser = manager_sub.add_parser("acknowledge", help="move one event PENDING -> ACKNOWLEDGED")
    ack_parser.add_argument("--event-id", required=True)
    close_parser = manager_sub.add_parser("close", help="close an acknowledged event")
    close_parser.add_argument("--event-id", required=True)
    close_parser.add_argument("--outcome", required=True, choices=["COMPLETE", "BLOCKED"])
    close_parser.add_argument("--summary", required=True, type=_nonblank_summary)

    lease = subparsers.add_parser("lease", help="exclusive-resource lease commands")
    lease_sub = lease.add_subparsers(dest="lease_command", required=True)
    force_release_parser = lease_sub.add_parser(
        "force-release", help="force-release one orphaned exclusive-resource lease"
    )
    force_release_parser.add_argument("--resource-id", required=True)

    send_parser = subparsers.add_parser("send-lane-notification", help="append one assignment to a running managed lane")
    send_parser.add_argument("--lane-id", required=True)
    send_parser.add_argument("--prompt", required=True)

    scan_parser = subparsers.add_parser("scan", help="read-only lane-status snapshot")
    scan_parser.add_argument("--no-write", action="store_true")
    watch_parser = subparsers.add_parser("watch", help="block until an actionable condition exists")
    watch_parser.add_argument("--until-actionable", action="store_true")
    watch_parser.add_argument("--timeout")
    watch_parser.add_argument("--until-event")

    view_parser = subparsers.add_parser("view", help="read-only terminal view of every lane's steps")
    view_parser.add_argument("--once", action="store_true", help="print one snapshot and exit")
    view_parser.add_argument("--no-color", action="store_true")
    view_parser.add_argument("--ascii", action="store_true", help="use plain ASCII glyphs")
    view_parser.add_argument("--refresh", type=float, default=2.0, help="seconds between record reads")

    health = subparsers.add_parser("health", help="health commands")
    health_sub = health.add_subparsers(dest="health_command", required=True)
    health_sub.add_parser("reconcile", help="rebuild active-lanes and re-derive status")
    health_sub.add_parser(
        "monitor-recover", help="recover the persistent monitor (managed, runtime OPEN)"
    )

    _add_memory_parser(subparsers)
    return parser


def _dispatch(args: argparse.Namespace) -> dict[str, Any]:
    command = args.command
    if command == "harness":
        if args.harness_command == "setup":
            if args.check:
                return setup.run_setup_check(overwrite=args.overwrite)
            return setup.run_setup(overwrite=args.overwrite)
        if args.harness_command == "shutdown":
            return shutdown.run_shutdown()
        raise ValueError(f"unknown harness command: {args.harness_command}")
    if command == "lane":
        if args.lane_command == "bootstrap":
            return bootstrap.run_bootstrap(
                lane_id=args.lane_id,
                provider=args.provider,
                model=args.model,
                launch_config=_provider_options(args.provider_option),
                exclusive_resources=list(args.exclusive_resource),
                task_card_path=args.task_card,
            )
        if args.lane_command == "launch":
            visualizer = visualizer_launch.ensure_for_current_epoch(
                disabled=args.no_visualizer
            )
            if args.no_visualizer and visualizer.get("decision_committed") is not True:
                return {
                    "ok": False,
                    "code": "VISUALIZER_OPT_OUT_FAILED",
                    "summary": (
                        "the explicit --no-visualizer decision could not be "
                        "persisted; no provider was started"
                    ),
                    "evidence_paths": list(
                        visualizer.get("evidence_paths") or []
                    ),
                    "next_action": str(
                        visualizer.get(
                            "next_action", "resolve runtime storage and retry"
                        )
                    ),
                    "visualizer": visualizer,
                }
            result = launch.run_launch(args.lane_id)
            return {**result, "visualizer": visualizer}
        if args.lane_command == "completion-review":
            return review.run_completion_review(
                event_id=args.event_id,
                lane_id=args.lane_id,
                review_outcome=args.review_outcome,
                approval=args.approval,
                review_summary=args.review_summary,
                evidence=list(args.evidence),
                force_accept=args.force_accept,
                force_reason=args.force_reason,
            )
        if args.lane_command == "force-stop":
            return launch.run_force_stop(args.lane_id)
        if args.lane_command == "retire":
            return launch.run_retire(args.acceptance_ref)
        raise ValueError(f"unknown lane command: {args.lane_command}")
    if command == "resume-lane":
        return resume.run_resume(
            lane_id=args.lane_id,
            resume_task_card=args.resume_task_card,
            rationale=args.rationale,
        )
    if command == "manager":
        if args.manager_command == "acknowledge":
            return _manager_acknowledge(args.event_id)
        if args.manager_command == "close":
            return _manager_close(args.event_id, args.outcome, args.summary)
        raise ValueError(f"unknown manager command: {args.manager_command}")
    if command == "lease":
        if args.lease_command == "force-release":
            return _lease_force_release(args.resource_id)
        raise ValueError(f"unknown lease command: {args.lease_command}")
    if command == "send-lane-notification":
        return _send_lane_notification(args.lane_id, args.prompt)
    if command == "scan":
        return scan_watch.run_scan()
    if command == "watch":
        return scan_watch.run_watch(timeout=args.timeout, until_event=args.until_event)
    if command == "view":
        return view_term.run_view(
            once=args.once,
            no_color=args.no_color,
            ascii_only=args.ascii,
            refresh_seconds=max(0.5, args.refresh),
        )
    if command == "health":
        if args.health_command == "reconcile":
            return scan_watch.run_health_reconcile()
        if args.health_command == "monitor-recover":
            return setup.run_monitor_recover()
        raise ValueError(f"unknown health command: {args.health_command}")
    if command == "memory":
        return memory_commands.run(args)
    raise ValueError(f"unknown command: {command}")


def main(argv: list[str] | None = None) -> int:
    """Run the sole v2 public launcher route."""
    if argv is None:
        argv = sys.argv[1:]
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        result = _dispatch(args)
    except Exception as exc:
        result = {
            "ok": False,
            "code": "LAUNCHER_FAILED",
            "summary": str(exc),
            "evidence_paths": [],
            "next_action": "resolve the error and retry",
        }
    return _emit(result, as_json=args.json)


if __name__ == "__main__":
    raise SystemExit(main())
