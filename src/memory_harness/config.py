"""Fixed-strategy configuration and the deferred learned-selector boundary."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Mapping

STANDARD = "standard"
PROBLEM_FOCUSED = "problem_focused"
DEEPER = "deeper"
FIXED_STRATEGIES = frozenset({STANDARD, PROBLEM_FOCUSED, DEEPER})

LEARNED_REQUEST_KEYS = (
    "learned_mode",
    "learned_selection",
    "policy_load",
    "policy_update",
    "training",
    "train",
    "learner",
)


class DeferredCapabilityError(RuntimeError):
    """A future capability was explicitly requested but is not implemented."""


@dataclass(frozen=True)
class MemoryConfig:
    strategy: str = STANDARD
    requested_strategy: str = STANDARD
    reason: str = "fixed strategy"
    experience_read: bool = True
    experience_write: bool = True
    generated_skill_creation: bool = True
    generated_skill_use: bool = True
    shared_publication: bool = True
    atlas_shared_retrieval: bool = True
    template_memory: bool = True
    apc: bool = True
    light_adaptation: bool = True
    deeper: bool = True

    @property
    def all_off(self) -> bool:
        return not any(
            (
                self.experience_read,
                self.experience_write,
                self.generated_skill_creation,
                self.generated_skill_use,
                self.shared_publication,
                self.atlas_shared_retrieval,
                self.template_memory,
                self.apc,
                self.light_adaptation,
                self.deeper,
            )
        )


def _reject_learned_requests(raw: Mapping[str, Any]) -> None:
    requested_strategy = raw.get("strategy")
    if isinstance(requested_strategy, str) and requested_strategy.lower() in {
        "learned",
        "learned-selection",
        "learned_strategy",
        "learned_selector",
    }:
        raise DeferredCapabilityError(
            "deferred/not implemented: learned strategy selection is not implemented"
        )
    for key in LEARNED_REQUEST_KEYS:
        value = raw.get(key)
        if isinstance(value, Mapping):
            if any(bool(item) for item in value.values()):
                raise DeferredCapabilityError(
                    f"deferred/not implemented: learned request {key!r} is not implemented"
                )
        elif value:
            raise DeferredCapabilityError(
                f"deferred/not implemented: learned request {key!r} is not implemented"
            )
    if raw.get("policy") is not None and raw.get("strategy") is None:
        raise DeferredCapabilityError(
            "deferred/not implemented: learned policy loading is not implemented"
        )


def resolve_config(raw: Mapping[str, Any] | None = None) -> MemoryConfig:
    """Resolve one fixed recipe or the all-off baseline before preparation."""

    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        raise ValueError("configuration must be an object")
    _reject_learned_requests(raw)

    all_features = raw.get("all_features", True)
    if not isinstance(all_features, bool):
        raise ValueError("all_features must be boolean")
    if not all_features:
        return MemoryConfig(
            strategy=STANDARD,
            requested_strategy=str(raw.get("strategy", STANDARD)),
            reason="all enhancements off",
            experience_read=False,
            experience_write=False,
            generated_skill_creation=False,
            generated_skill_use=False,
            shared_publication=False,
            atlas_shared_retrieval=False,
            template_memory=False,
            apc=False,
            light_adaptation=False,
            deeper=False,
        )

    requested_strategy = raw.get("strategy", STANDARD)
    if not isinstance(requested_strategy, str):
        raise ValueError("strategy must be a string")
    normalized = requested_strategy.strip().lower().replace("-", "_")
    if normalized in FIXED_STRATEGIES:
        strategy = normalized
        reason = f"fixed strategy: {strategy}"
    else:
        strategy = STANDARD
        reason = f"fallback to standard: unsupported strategy {requested_strategy!r}"

    feature_values = {
        "experience_read": raw.get("experience_read", True),
        "experience_write": raw.get("experience_write", True),
        "generated_skill_creation": raw.get("generated_skill_creation", True),
        "generated_skill_use": raw.get("generated_skill_use", True),
        "shared_publication": raw.get("shared_publication", True),
        "atlas_shared_retrieval": raw.get("atlas_shared_retrieval", True),
        "template_memory": raw.get("template_memory", True),
        "apc": raw.get("apc", True),
        "light_adaptation": raw.get("light_adaptation", True),
        "deeper": raw.get("deeper", strategy == DEEPER),
    }
    for key, value in feature_values.items():
        if not isinstance(value, bool):
            raise ValueError(f"{key} must be boolean")

    return MemoryConfig(
        strategy=strategy,
        requested_strategy=requested_strategy,
        reason=reason,
        **feature_values,  # type: ignore[arg-type]
    )


def all_off() -> MemoryConfig:
    return resolve_config({"all_features": False})


def standard() -> MemoryConfig:
    return resolve_config({"strategy": STANDARD})


def problem_focused() -> MemoryConfig:
    return resolve_config({"strategy": PROBLEM_FOCUSED})


def deeper() -> MemoryConfig:
    return resolve_config({"strategy": DEEPER})


__all__ = [
    "DeferredCapabilityError",
    "MemoryConfig",
    "FIXED_STRATEGIES",
    "STANDARD",
    "PROBLEM_FOCUSED",
    "DEEPER",
    "all_off",
    "standard",
    "problem_focused",
    "deeper",
    "resolve_config",
]
