"""Registered provider launcher bindings.

Each subpackage mirrors one shipped adapter catalog
(``adapters/<provider-id>/harness/launcher_binding.py``) and is loaded by the
controller through ``importlib``. Receipt selection is shared here so the
registered bindings remain byte-identical to the shipped catalog.
"""

from typing import Any


def usage_observation(event: dict[str, Any], provider_id: str) -> dict[str, Any] | None:
    """Keep native counts and identity; leave interpretation to the joined store.

    A result rollup may include earlier message counts, and cache/reasoning
    counters may be included in input/output. This interface never totals them.
    """
    event_type = event.get("type")
    if event_type not in {
        "codex": {"turn.completed", "turn.failed", "turn.cancelled"},
        "claude-code": {"assistant", "result"},
        "qwen-code": {"assistant", "result"},
    }.get(provider_id, set()):
        return None
    message = event.get("message")
    nested = message if isinstance(message, dict) else {}
    usage = event.get("usage")
    if not isinstance(usage, dict):
        usage = nested.get("usage")
    if not isinstance(usage, dict):
        return None
    observation = {
        "event_type": event.get("type"),
        "usage": usage,
    }
    for key in (
        "subtype", "uuid", "id", "turn_id", "turnId", "thread_id", "threadId",
        "session_id", "sessionId", "model", "parent_tool_use_id",
        "modelUsage", "total_cost_usd", "is_error",
    ):
        if key in event:
            observation[key] = event[key]
    for key, target in (
        ("id", "message_id"),
        ("model", "message_model"),
        ("stop_reason", "message_stop_reason"),
    ):
        if key in nested:
            observation[target] = nested[key]
    return observation
