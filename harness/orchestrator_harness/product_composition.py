"""ROOT-owned composition for the installable memory-backed harness.

This is deliberately the only place where the public harness turns its trusted
``memory-product-config.json`` into concrete stores and adapters.  Task text can
neither select a database path nor provide a credential.  The optional
``memory_scope`` on a task card is only an equality assertion against the
captured four-field deployment scope.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .memory_product_config import (
    CONFIG_FILE_NAME,
    MemoryProductConfig,
    assert_exact_scope,
    load_memory_product_config,
    read_known_secrets,
)

CENTRAL_STORE_NAME = "memory-product.sqlite3"


class ProductCompositionError(RuntimeError):
    """The trusted product configuration cannot be composed truthfully."""


@dataclass
class ProductComposition:
    """One short-lived set of trusted dependencies for a preparation call."""

    configuration: MemoryProductConfig
    store: Any
    privacy_policy: Any
    scope: Any
    search_stores: tuple[Any, ...]
    experience_service: Any
    procedure_service: Any
    resolved_memory_config: Any
    everos_adapter: Any | None = None
    atlas_adapter: Any | None = None

    def close(self) -> None:
        try:
            collection = getattr(self.atlas_adapter, "collection", None)
            database = getattr(collection, "database", None)
            client = getattr(database, "client", None)
            close_client = getattr(client, "close", None)
            if callable(close_client):
                close_client()
        finally:
            self.store.close()

    def __enter__(self) -> "ProductComposition":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def configured(harness_root: str | os.PathLike[str]) -> bool:
    """Return whether this harness declares the enhanced product boundary."""

    return (Path(harness_root).absolute() / CONFIG_FILE_NAME).is_file()


def central_store_path(configuration: MemoryProductConfig) -> Path:
    """Return the fixed operator-owned store path (never task-selected)."""

    return configuration.store_root / CENTRAL_STORE_NAME


class _DeterministicEmbeddings:
    """Pinned local-token-overlap vectors used by the product Atlas schema."""

    @staticmethod
    def _vector(text: str) -> list[float]:
        from memory_harness import config

        dimensions = config.REPRESENTATION_DIMENSIONS
        vector = [0.0] * dimensions
        for token in set(re.findall(r"[a-z0-9_]+", text.casefold())):
            bucket = int.from_bytes(
                hashlib.sha256(token.encode("utf-8")).digest()[:8], "big"
            ) % dimensions
            vector[bucket] += 1.0
        length = math.sqrt(sum(value * value for value in vector))
        return [value / length for value in vector] if length else vector

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vector(text)


def deterministic_representation_vector(text: str) -> list[float]:
    """Return the product's canonical local/Atlas representation vector."""

    if not isinstance(text, str) or not text.strip():
        raise ProductCompositionError("representation text must be nonempty")
    return _DeterministicEmbeddings._vector(text)


def _everos_adapter(
    configuration: MemoryProductConfig, scope: Any, privacy_policy: Any
) -> Any:
    from memory_harness import experience

    base_root = configuration.store_root / "everos"
    expected_root = experience.EverOSAdapter.memory_root_for_scope(base_root, scope)
    configured_root = os.environ.get("EVEROS_ROOT")
    if not configured_root:
        raise ProductCompositionError(
            "EverOS is enabled but EVEROS_ROOT is not configured for the exact "
            f"product scope (expected {expected_root})"
        )
    try:
        surface = experience.load_vendored_everos_public_surface(
            memory_root=expected_root
        )
        return experience.EverOSAdapter(
            scope=scope,
            base_root=base_root,
            surface=surface,
            privacy_policy=privacy_policy,
        )
    except Exception as exc:
        raise ProductCompositionError(
            f"EverOS is enabled but its supported adapter is unavailable: {exc}"
        ) from exc


def _atlas_adapter(
    configuration: MemoryProductConfig, privacy_policy: Any
) -> Any:
    from memory_harness import atlas

    service = configuration.services["atlas"]
    assert service.credential_env is not None
    uri = os.environ.get(service.credential_env)
    if not uri:
        raise ProductCompositionError(
            "Atlas is enabled but its configured credential environment reference "
            "is unavailable"
        )
    client = None
    try:
        from pymongo import MongoClient

        client = MongoClient(uri, connect=False)
        collection = client[str(service.database)][str(service.collection)]
        return atlas.AtlasProcedureAdapter.from_pymongo_collection(
            collection=collection,
            embedding=_DeterministicEmbeddings(),
            index_name=str(service.index),
            metric="cosine",
            privacy_policy=privacy_policy,
        )
    except Exception as exc:
        if client is not None:
            client.close()
        raise ProductCompositionError(
            "Atlas is enabled but its supported adapter is unavailable "
            f"({type(exc).__name__}); verify the configured driver, collection, "
            "index, and credential environment reference"
        ) from exc


def compose_product(
    *,
    harness_root: str | os.PathLike[str],
    task_card: Mapping[str, Any],
    route: str,
) -> ProductComposition:
    """Build local stores and explicitly enabled remote stores from ROOT config.

    Missing or malformed enabled remote prerequisites are errors.  In
    particular, this function never substitutes a local fixture while labeling
    it EverOS or Atlas.
    """

    from memory_harness import (
        atlas_adapters,
        everos_adapters,
        experience,
        local_adapters,
        local_procedure_adapters,
        privacy,
        procedures,
        store,
    )

    try:
        configuration = load_memory_product_config(harness_root)
        asserted_scope = task_card.get("memory_scope", configuration.scope)
        scope_record = assert_exact_scope(configuration, asserted_scope)
        known_secrets = read_known_secrets(configuration)
    except Exception as exc:
        raise ProductCompositionError(
            f"trusted memory product configuration is unavailable: {exc}"
        ) from exc

    privacy_policy = privacy.PrivacyPolicy(known_secrets=known_secrets)
    # The exact task card is later retained in the worker-readable control
    # directory and therefore cannot be silently rewritten.  Derived search
    # and optional-memory values are redacted, but a configured secret in this
    # authoritative worker-bound record must fail closed before a worktree is
    # created rather than leaking through task-card.json.
    if privacy_policy.detect(task_card):
        raise ProductCompositionError(
            "trusted task card contains a configured content secret; remove it "
            "or use an approved task-only credential channel before bootstrap"
        )
    scope = experience.ExperienceScope.from_record(scope_record)
    from memory_harness import config as memory_config

    everos_available = configuration.services["everos"].available
    atlas_available = configuration.services["atlas"].available
    local_experience_available = configuration.services["local_experience"].available

    def unavailable_reason(*service_names: str) -> str:
        reasons = [
            configuration.services[name].reason
            for name in service_names
            if not configuration.services[name].available
        ]
        return "; ".join(reasons) or "required service is unavailable"

    availability = {
        "experience_read": {
            "available": local_experience_available or everos_available,
            "reason": unavailable_reason("local_experience", "everos"),
        },
        "experience_write": {
            "available": local_experience_available or everos_available,
            "reason": unavailable_reason("local_experience", "everos"),
        },
        "generated_skill_creation": {
            "available": everos_available,
            "reason": unavailable_reason("everos"),
        },
        "generated_skill_use": {
            # Already reviewed/approved generated procedures are local durable
            # records.  Their use must not depend on EverOS still being online.
            "available": configuration.services["local_procedures"].available,
            "reason": unavailable_reason("local_procedures"),
        },
        "shared_publication": {
            "available": atlas_available,
            "reason": unavailable_reason("atlas"),
        },
        "atlas_shared_retrieval": {
            "available": atlas_available,
            "reason": unavailable_reason("atlas"),
        },
        "template_memory": True,
        "apc": {
            "available": isinstance(task_card.get("apc_adaptation_binding"), Mapping),
            "reason": "no explicit APC adaptation binding was supplied",
        },
        "light_adaptation": {
            "available": isinstance(task_card.get("apc_adaptation_binding"), Mapping),
            "reason": "no explicit APC adaptation binding was supplied",
        },
        "deeper": True,
    }
    resolved_memory_config = memory_config.resolve_config(
        task_card.get("memory_handoff", {}).get("configuration"),
        availability=availability,
        # There is no hot policy-update path in this release.  State that
        # fact explicitly so the captured rich record does not acquire its
        # transition attribution from an implicit resolver default.
        transitions={name: "stable" for name in memory_config.FEATURE_NAMES},
    )
    # Construct every explicitly enabled remote adapter before creating the
    # central SQLite store.  A missing driver/root/credential therefore fails
    # the preflight without leaving even optional local product state behind.
    everos = (
        _everos_adapter(configuration, scope, privacy_policy)
        if configuration.services["everos"].enabled
        else None
    )
    atlas = (
        _atlas_adapter(configuration, privacy_policy)
        if configuration.services["atlas"].enabled
        else None
    )

    memory_store = store.MemoryStore(central_store_path(configuration))
    memory_store.initialize()
    experience_service = experience.ReviewedExperienceService(
        memory_store, privacy_policy=privacy_policy
    )
    # The exact configured owner is the deployment's initial trusted ROOT
    # issuer.  Additional issuers require a future trusted-config schema; a
    # task card cannot add one.
    procedure_service = procedures.TrustedProcedureService(
        memory_store,
        trusted_issuers=(configuration.owner,),
        privacy_policy=privacy_policy,
    )
    stores: list[Any] = []
    local = local_adapters.make_local_search_stores(
        experience_service=experience_service,
        scope=scope,
    )
    if configuration.services["local_experience"].enabled:
        stores.append(local[0])
    # The bundled immutable templates are not a remote service and remain
    # available; MemoryConfig.template_memory is the actual query gate.
    stores.append(local[1])
    if configuration.services["local_procedures"].enabled:
        stores.extend(
            local_procedure_adapters.make_local_procedure_search_stores(
                procedure_service=procedure_service,
                memory_store=memory_store,
                receiver=scope_record,
                facts={"route": route},
                route=route,
            )
        )

    if everos is not None:
        stores.append(
            everos_adapters.make_everos_case_search_store(
                experience_service=experience_service,
                adapter=everos,
                scope=scope,
            )
        )
        stores.append(
            everos_adapters.make_everos_generated_skill_search_store(
                experience_service=experience_service,
                procedure_service=procedure_service,
                memory_store=memory_store,
                adapter=everos,
                scope=scope,
                receiver=scope_record,
                facts={"route": route},
                route=route,
            )
        )

    if atlas is not None:
        stores.append(
            atlas_adapters.make_atlas_search_store(
                procedure_service=procedure_service,
                adapter=atlas,
                receiver=scope_record,
                facts={"route": route},
                route=route,
            )
        )

    return ProductComposition(
        configuration=configuration,
        store=memory_store,
        privacy_policy=privacy_policy,
        scope=scope,
        search_stores=tuple(stores),
        experience_service=experience_service,
        procedure_service=procedure_service,
        resolved_memory_config=resolved_memory_config,
        everos_adapter=everos,
        atlas_adapter=atlas,
    )


def preparation_arguments(
    *,
    task_card: Mapping[str, Any],
    composition: ProductComposition,
    harness_runtime_root: Path,
    base_commit: str,
) -> dict[str, Any]:
    """Return the bounded ROOT-owned inputs for ``prepare_with_memory``."""

    from memory_harness import harness_child, privacy

    failure = task_card.get("failure_context")
    if failure is not None and not isinstance(failure, str):
        raise ProductCompositionError("failure_context must be a string or null")
    sanitized_failure = (
        privacy.sanitize_text(failure, composition.privacy_policy)
        if isinstance(failure, str)
        else None
    )
    root_replan = task_card.get("root_replan")
    if root_replan is not None and not isinstance(root_replan, Mapping):
        raise ProductCompositionError("root_replan must be an object or null")
    bindings = task_card.get("template_bindings")
    if bindings is not None and not isinstance(bindings, Mapping):
        raise ProductCompositionError("template_bindings must be an object or null")
    apc_binding = task_card.get("apc_adaptation_binding")
    if apc_binding is not None and not isinstance(apc_binding, Mapping):
        raise ProductCompositionError(
            "apc_adaptation_binding must be an object or null"
        )

    launcher = None
    apc_cleanup = None
    if apc_binding is not None:
        required_binding = ("provider", "model", "cli", "effort", "source")
        missing = [name for name in required_binding if name not in apc_binding]
        if missing:
            raise ProductCompositionError(
                f"apc_adaptation_binding is missing {missing[0]}"
            )
        if any(
            not isinstance(apc_binding[name], str)
            or not apc_binding[name].strip()
            or apc_binding[name] != apc_binding[name].strip()
            for name in required_binding
        ):
            raise ProductCompositionError(
                "apc_adaptation_binding fields must be canonical nonempty strings"
            )
        if apc_binding["source"] != "explicit":
            raise ProductCompositionError(
                "apc_adaptation_binding source must be explicit"
            )
        provider = apc_binding["provider"]
        model = apc_binding["model"]
        if provider != "codex":
            raise ProductCompositionError(
                "APC provider filesystem isolation unavailable for enhanced "
                f"memory product: {provider}; the drafting child is refused "
                "until that provider has a verified config/store_root/"
                "secret-file deny mechanism"
            )
        launcher = harness_child.make_native_apc_launcher(
            session=harness_child.DraftingChildSession(
                runtime_root=harness_runtime_root,
                task_card_dir=harness_runtime_root / "operator-only" / "apc-task-cards",
                base_commit=base_commit,
                provider=provider,
                model=model,
            ),
            # The enclosing preparation adds its exact smaller deadline to the
            # request.  This construction value can only tighten, never relax,
            # that bound if the launcher is invoked independently.
            deadline=time.monotonic() + 300.0,
        )
        # The native launcher normally returns the exact stop receipt.  This
        # fallback is deliberately *not* proof of cleanup: if the receipt is
        # absent, the bridge retains unresolved ownership and forbids a blind
        # recursive/retry launch.
        apc_cleanup = {
            "state": "cleanup must be proven by the native harness launcher",
            "cleanup_proven": False,
        }
    return {
        "stores": composition.search_stores,
        "privacy_policy": composition.privacy_policy,
        "resolved_config": composition.resolved_memory_config,
        "failure_context": sanitized_failure,
        "bindings": None if bindings is None else dict(bindings),
        "root_replan": None if root_replan is None else dict(root_replan),
        "apc_binding": None if apc_binding is None else dict(apc_binding),
        "apc_launcher": launcher,
        "apc_cleanup": apc_cleanup,
    }


__all__ = [
    "CENTRAL_STORE_NAME",
    "ProductComposition",
    "ProductCompositionError",
    "central_store_path",
    "compose_product",
    "configured",
    "preparation_arguments",
]
