"""Explicit, consumable, session-bounded authorization for live qualification."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import stat
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

import pytest

from scripts import verification_receipt


CONFIRMATION = "I_AUTHORIZE_ONE_DISPOSABLE_RUN"
PRODUCT = Path(__file__).resolve().parents[3]
RECEIPT_SCHEMA = "memory-harness-live-qualification-receipt/v1"


def add_live_authorization_options(parser: pytest.Parser) -> None:
    group = parser.getgroup("memory-harness-live-qualification")
    group.addoption("--authorize-live-qualification", action="store", default="")
    group.addoption("--live-authorization-nonce", action="store", default="")
    group.addoption("--live-authorization-file", action="store", default="")
    group.addoption("--live-candidate-artifact", action="store", default="")
    group.addoption("--live-verification-receipt", action="store", default="")
    group.addoption("--live-namespace", action="store", default="")
    group.addoption("--live-call-budget", action="store", type=int, default=0)
    group.addoption("--live-cost-budget-usd", action="store", type=float, default=0.0)
    group.addoption("--live-timeout", action="store", type=int, default=0)


def _allowed_environment() -> dict[str, str]:
    allowed = {
        "PATH", "SYSTEMROOT", "WINDIR", "HOME", "USERPROFILE", "TMPDIR", "TEMP", "TMP",
        "MEMORY_HARNESS_ATLAS_URI", "MEMORY_HARNESS_EVEROS_ENDPOINT",
        "MEMORY_HARNESS_EVEROS_TOKEN", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
        "QWEN_API_KEY",
    }
    return {key: value for key, value in os.environ.items() if key in allowed}


def _git(product_root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=product_root, text=True, capture_output=True,
        check=True, timeout=10,
    ).stdout.strip()


class LiveAuthorizationError(RuntimeError):
    """The live gate cannot prove its current local authorization binding."""


def _reparse_or_link(path: Path) -> bool:
    """Return whether any existing path component is a link/reparse point."""

    if not path.is_absolute():
        return True
    current = Path(path.anchor)
    try:
        parts = path.parts[1:] if path.anchor else path.parts
        for part in parts:
            current = current / part
            metadata = current.lstat()
            attributes = int(getattr(metadata, "st_file_attributes", 0))
            reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
            junction_probe = getattr(current, "is_junction", None)
            if (
                stat.S_ISLNK(metadata.st_mode)
                or bool(attributes & reparse_flag)
                or bool(junction_probe is not None and junction_probe())
            ):
                return True
    except (OSError, RuntimeError, ValueError):
        return True
    return False


def _plain_file_identity(path: Path) -> tuple[int, int, int, int, int]:
    if not path.is_absolute() or _reparse_or_link(path):
        raise LiveAuthorizationError("live qualification inputs must be absolute plain files")
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise LiveAuthorizationError("live qualification input is unavailable") from exc
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise LiveAuthorizationError("live qualification inputs must be single-link regular files")
    return (
        int(metadata.st_dev), int(metadata.st_ino), int(metadata.st_size),
        int(metadata.st_mtime_ns), int(metadata.st_nlink),
    )


def _validated_candidate(
    artifact: Path, receipt_path: Path, *, product_root: Path = PRODUCT,
) -> dict[str, str]:
    """Validate and bind the current qualifying receipt, candidate, and artifact."""

    root = product_root.resolve()
    artifact = artifact.expanduser()
    receipt_path = receipt_path.expanduser()
    artifact_before = _plain_file_identity(artifact)
    receipt_before = _plain_file_identity(receipt_path)
    try:
        receipt_path.resolve(strict=True).relative_to(root)
    except ValueError:
        pass
    except (OSError, RuntimeError) as exc:
        raise LiveAuthorizationError("verification receipt path is unavailable") from exc
    else:
        raise LiveAuthorizationError("verification receipt must be outside the candidate tree")
    try:
        dirty = _git(root, "status", "--porcelain=v1", "--untracked-files=all")
        if dirty:
            raise LiveAuthorizationError(
                "live qualification requires a clean, committed candidate tree"
            )
        document = verification_receipt.validate_receipt(
            receipt_path, require_qualifying=True,
        )
        receipt_bytes = receipt_path.read_bytes()
        canonical_receipt = (
            json.dumps(document, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        if receipt_bytes != canonical_receipt:
            raise LiveAuthorizationError(
                "verification receipt bytes are not the canonical qualified receipt"
            )
        candidate = document["candidate"]
        if Path(candidate["root"]).resolve() != root:
            raise LiveAuthorizationError(
                "verification receipt does not bind the selected candidate"
            )
        suite = json.loads(
            (root / "tests/suite_manifest.json").read_text(encoding="utf-8")
        )
        required_categories = suite.get("all")
        selection = document.get("selection", {})
        if (
            not isinstance(required_categories, list)
            or not required_categories
            or selection.get("requested") != required_categories
            or selection.get("available") != required_categories
            or [item.get("name") for item in document.get("categories", [])]
            != required_categories
        ):
            raise LiveAuthorizationError(
                "verification receipt does not cover the complete candidate suite"
            )
        artifact_digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
        artifact_files = document["artifacts"]["post"]["files"]
        matches = [
            item for item in artifact_files
            if Path(item.get("path", "")).resolve() == artifact.resolve()
        ]
        if len(matches) != 1 or matches[0] != {
            "path": str(artifact.resolve()),
            "kind": "file",
            "size": artifact.stat().st_size,
            "sha256": artifact_digest,
        }:
            raise LiveAuthorizationError(
                "verification receipt does not bind the selected artifact"
            )
        binding = {
            "commit": _git(root, "rev-parse", "HEAD"),
            "tree": _git(root, "rev-parse", "HEAD^{tree}"),
            "artifact_sha256": artifact_digest,
            "verification_receipt_integrity": str(document["integrity"]),
            "verification_receipt_sha256": hashlib.sha256(
                receipt_bytes
            ).hexdigest(),
        }
    except LiveAuthorizationError:
        raise
    except (
        OSError, KeyError, TypeError, ValueError, UnicodeError,
        json.JSONDecodeError, subprocess.SubprocessError,
        verification_receipt.ReceiptError,
    ) as exc:
        raise LiveAuthorizationError(
            "live qualification requires a current qualifying verification receipt"
        ) from exc
    if (
        _plain_file_identity(artifact) != artifact_before
        or _plain_file_identity(receipt_path) != receipt_before
    ):
        raise LiveAuthorizationError(
            "live qualification receipt or artifact changed during validation"
        )
    return binding


def _poison_file_descriptor(fd: int) -> bool:
    try:
        os.ftruncate(fd, 0)
        os.fsync(fd)
    except OSError:
        return False
    return True


def _fsync_directory(directory: Path) -> None:
    """Durably record a POSIX directory mutation."""

    if os.name == "nt":
        return  # MoveFileExW(MOVEFILE_WRITE_THROUGH) owns Windows durability.
    flags = os.O_RDONLY | int(getattr(os, "O_DIRECTORY", 0))
    directory_fd = os.open(directory, flags)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _durable_nonce_rename(source: Path, destination: Path) -> None:
    """Rename one claimed nonce and durably publish the directory mutation."""

    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.MoveFileExW.argtypes = (
            ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32,
        )
        kernel32.MoveFileExW.restype = ctypes.c_int
        if not kernel32.MoveFileExW(str(source), str(destination), 0x8):
            raise OSError(ctypes.get_last_error(), "cannot durably claim live authorization")
        return
    os.rename(source, destination)
    _fsync_directory(source.parent)


def _destroy_replacement_nonce(
    path: Path, nonce: str, *, protected_identity: tuple[int, int],
) -> None:
    """Atomically detach and destroy a swapped-in copy of this nonce.

    The POSIX path uses one directory descriptor for observation, rename,
    open, unlink, and directory fsync, so pathname replacement cannot redirect
    cleanup outside the authorization directory.
    """

    expected = {nonce.encode("ascii"), (nonce + "\n").encode("ascii")}
    if os.name != "nt":
        directory_flags = os.O_RDONLY | int(getattr(os, "O_DIRECTORY", 0))
        try:
            directory_fd = os.open(path.parent, directory_flags)
        except OSError:
            return
        quarantine = f".{path.name}.rejected-{secrets.token_hex(16)}"
        replacement_fd: int | None = None
        try:
            try:
                current = os.stat(
                    path.name, dir_fd=directory_fd, follow_symlinks=False,
                )
            except OSError:
                return
            if (int(current.st_dev), int(current.st_ino)) == protected_identity:
                return
            try:
                os.rename(
                    path.name, quarantine,
                    src_dir_fd=directory_fd, dst_dir_fd=directory_fd,
                )
                os.fsync(directory_fd)
            except OSError:
                return
            try:
                replacement_fd = os.open(
                    quarantine,
                    os.O_RDWR | int(getattr(os, "O_NOFOLLOW", 0)),
                    dir_fd=directory_fd,
                )
                replacement = os.fstat(replacement_fd)
                if not stat.S_ISREG(replacement.st_mode):
                    raise OSError("replacement is not regular")
                data = os.read(replacement_fd, 257)
            except OSError:
                # Its contents were never proven to be the bearer.  Keep the
                # atomically detached object intact in quarantine rather than
                # risking destruction of unrelated operator data.
                return
            if data not in expected:
                return  # Preserve non-authorization content in quarantine.
            if replacement.st_nlink == 1:
                # Remove discoverability first; the held descriptor keeps the
                # inode available for durable destruction.
                try:
                    os.unlink(quarantine, dir_fd=directory_fd)
                    os.fsync(directory_fd)
                except OSError:
                    pass
                _poison_file_descriptor(replacement_fd)
            else:
                # Truncation clears every hard-link alias before this name goes.
                _poison_file_descriptor(replacement_fd)
                try:
                    os.unlink(quarantine, dir_fd=directory_fd)
                    os.fsync(directory_fd)
                except OSError:
                    pass
        finally:
            if replacement_fd is not None:
                os.close(replacement_fd)
            os.close(directory_fd)
        return

    # Windows has no dir_fd/openat surface. MoveFileExW with WRITE_THROUGH
    # atomically detaches the path; reparse inputs are removed without follow.
    try:
        current = path.lstat()
    except OSError:
        return
    if (int(current.st_dev), int(current.st_ino)) == protected_identity:
        return
    quarantine_path = path.with_name(
        f".{path.name}.rejected-{secrets.token_hex(16)}"
    )
    try:
        _durable_nonce_rename(path, quarantine_path)
        if _reparse_or_link(quarantine_path):
            return
        replacement_fd = os.open(
            quarantine_path,
            os.O_RDWR | int(getattr(os, "O_NOFOLLOW", 0)),
        )
    except OSError:
        return
    try:
        replacement = os.fstat(replacement_fd)
        data = os.read(replacement_fd, 257)
        if stat.S_ISREG(replacement.st_mode) and data in expected:
            _poison_file_descriptor(replacement_fd)
            quarantine_path.unlink(missing_ok=True)
    finally:
        os.close(replacement_fd)


def _consume_nonce(path: Path, nonce: str) -> Path:
    """Atomically claim and destroy one unlinked, non-reparse nonce file."""

    if not re.fullmatch(r"[0-9a-f]{64}", nonce):
        pytest.skip("live qualification requires a 256-bit lowercase-hex nonce")
    if not path.is_absolute() or _reparse_or_link(path):
        pytest.skip("live qualification requires an absolute plain one-use authorization file")
    try:
        initial = path.lstat()
    except OSError:
        pytest.skip("live qualification requires an absolute one-use authorization file")
    if not stat.S_ISREG(initial.st_mode):
        pytest.skip("live qualification requires a regular one-use authorization file")
    flags = os.O_RDWR | int(getattr(os, "O_NOFOLLOW", 0))
    try:
        fd = os.open(path, flags)
    except OSError:
        pytest.skip("live authorization file cannot be opened safely")
    consumed: Path | None = None
    identity: tuple[int, int] | None = None
    nonce_validated = False
    try:
        opened = os.fstat(fd)
        identity = (int(opened.st_dev), int(opened.st_ino))
        try:
            data = os.read(fd, 257)
            data.decode("ascii")
        except (OSError, UnicodeError):
            pytest.skip("live authorization file cannot be read")
        if data not in {nonce.encode("ascii"), (nonce + "\n").encode("ascii")}:
            # The pathname may have been replaced between lstat and open.  Do
            # not modify an inode until its bytes prove that it is this exact
            # bearer.  Still remove a second, concurrently restored copy of
            # the bearer at the pathname when one exists.
            _destroy_replacement_nonce(
                path, nonce, protected_identity=identity,
            )
            pytest.skip("live authorization nonce does not match its one-use file")
        nonce_validated = True
        if (
            identity != (int(initial.st_dev), int(initial.st_ino))
            or initial.st_nlink != 1
            or opened.st_nlink != 1
        ):
            _poison_file_descriptor(fd)
            _destroy_replacement_nonce(
                path, nonce, protected_identity=identity,
            )
            pytest.skip("live authorization file is linked or changed")
        try:
            current = path.lstat()
        except OSError:
            pytest.skip("live authorization was already consumed or changed")
        if (
            (int(current.st_dev), int(current.st_ino)) != identity
            or current.st_nlink != 1
            or _reparse_or_link(path)
        ):
            _poison_file_descriptor(fd)
            pytest.skip("live authorization file changed during claim")
        # Destroy and flush the bearer *before* publishing the consumed name.
        # Consequently a process/power failure at any later instruction can
        # expose only an empty source or destination, never a reusable nonce.
        if not _poison_file_descriptor(fd):
            pytest.skip("live authorization could not be durably destroyed")
        try:
            current = path.lstat()
        except OSError:
            pytest.skip("live authorization changed while being destroyed")
        if (
            (int(current.st_dev), int(current.st_ino)) != identity
            or current.st_nlink != 1
            or current.st_size != 0
        ):
            _destroy_replacement_nonce(
                path, nonce, protected_identity=identity,
            )
            pytest.skip("live authorization changed while being destroyed")
        consumed = path.with_name(
            f".{path.name}.consumed-{os.getpid()}-{secrets.token_hex(16)}"
        )
        try:
            _durable_nonce_rename(path, consumed)
            moved = consumed.lstat()
        except OSError:
            pytest.skip("live authorization was already consumed or cannot be claimed")
        if (
            (int(moved.st_dev), int(moved.st_ino)) != identity
            or moved.st_nlink != 1
            or _reparse_or_link(consumed)
        ):
            _poison_file_descriptor(fd)
            _destroy_replacement_nonce(
                consumed, nonce, protected_identity=identity,
            )
            pytest.skip("live authorization changed during atomic claim")
        return consumed
    finally:
        # This executes for SystemExit/KeyboardInterrupt and exceptions injected
        # immediately after the rename.  The pre-rename fsync already removed
        # the secret; repeat it and persist any observed directory mutation.
        if nonce_validated:
            _poison_file_descriptor(fd)
            if identity is not None:
                _destroy_replacement_nonce(
                    path, nonce, protected_identity=identity,
                )
                if consumed is not None:
                    _destroy_replacement_nonce(
                        consumed, nonce, protected_identity=identity,
                    )
        if consumed is not None:
            try:
                _fsync_directory(consumed.parent)
            except OSError:
                pass
        os.close(fd)


@dataclass
class LiveAuthorization:
    namespace: str
    remaining_calls: int
    remaining_cost_usd: float
    timeout: int
    driver: Path
    candidate: dict[str, str]
    nonce_digest: str
    deadline: float
    candidate_recheck: Callable[[], dict[str, str]]
    _used_scenarios: set[str] = field(default_factory=set)

    def _invoke(
        self, request: Mapping[str, Any], *, tolerate_local_drift_for_cleanup: bool = False,
    ) -> dict[str, Any]:
        if request.get("candidate") != self.candidate:
            raise LiveAuthorizationError(
                "live request does not carry the authorized candidate binding"
            )
        if tolerate_local_drift_for_cleanup and request.get("scenario") != "cleanup":
            raise LiveAuthorizationError(
                "only namespace cleanup may tolerate local candidate drift"
            )
        if (
            not tolerate_local_drift_for_cleanup
            and self.candidate_recheck() != self.candidate
        ):
            raise LiveAuthorizationError(
                "live candidate, artifact, or verification receipt changed before invocation"
            )
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError("live qualification exhausted its session time budget")
        requested_timeout = request.get("timeout_seconds")
        assert isinstance(requested_timeout, (int, float)) and requested_timeout > 0
        completed = subprocess.run(
            [str(self.driver)], cwd=PRODUCT, input=json.dumps(dict(request)),
            text=True, capture_output=True, check=True,
            timeout=max(1.0, min(float(self.timeout), remaining, float(requested_timeout))),
            env=_allowed_environment(),
        )
        if (
            not tolerate_local_drift_for_cleanup
            and self.candidate_recheck() != self.candidate
        ):
            raise LiveAuthorizationError(
                "live candidate, artifact, or verification receipt changed during invocation"
            )
        receipt = json.loads(completed.stdout)
        assert isinstance(receipt, dict)
        assert receipt.get("schema") == RECEIPT_SCHEMA
        assert receipt.get("candidate") == self.candidate
        assert receipt.get("namespace") == request["namespace"]
        return receipt

    @staticmethod
    def _usage(receipt: Mapping[str, Any], calls: int, cost: float) -> None:
        observed_calls = receipt.get("calls")
        observed_cost = receipt.get("cost_usd")
        assert isinstance(observed_calls, int) and not isinstance(observed_calls, bool)
        assert 0 <= observed_calls <= calls
        assert isinstance(observed_cost, (int, float)) and not isinstance(observed_cost, bool)
        assert math.isfinite(observed_cost) and 0 <= observed_cost <= cost

    def execute(
        self, scenario: str, *, call_budget: int, cost_budget_usd: float,
        required: list[str] | None = None, extra: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Run one scenario and always demand independent namespace cleanup."""
        assert re.fullmatch(r"[a-z0-9-]{3,64}", scenario)
        assert scenario not in self._used_scenarios
        assert 2 <= call_budget <= self.remaining_calls
        assert 0 < cost_budget_usd <= self.remaining_cost_usd
        self._used_scenarios.add(scenario)
        self.remaining_calls -= call_budget
        self.remaining_cost_usd -= cost_budget_usd
        namespace = f"{self.namespace}-{scenario}"
        remaining_time = self.deadline - time.monotonic()
        assert remaining_time > 2.0
        cleanup_seconds = min(30.0, max(2.0, remaining_time / 5.0))
        cleanup_calls = 1
        main_calls = call_budget - cleanup_calls
        request: dict[str, Any] = {
            "schema": "memory-harness-live-qualification/v2",
            "scenario": scenario,
            "candidate": self.candidate,
            "namespace": namespace,
            "call_budget": main_calls,
            "cost_budget_usd": cost_budget_usd,
            "timeout_seconds": min(
                self.timeout, max(1.0, remaining_time - cleanup_seconds)
            ),
            "authorization_nonce_sha256": self.nonce_digest,
            "required": list(required or []),
        }
        if extra:
            assert not set(extra).intersection(request), (
                "scenario extras cannot override authorization-owned fields"
            )
            request.update(dict(extra))
        try:
            receipt = self._invoke(request)
            assert receipt.get("scenario") == scenario
            self._usage(receipt, main_calls, cost_budget_usd)
            return receipt
        finally:
            cleanup_request = {
                "schema": "memory-harness-live-qualification/v2",
                "scenario": "cleanup",
                "candidate": self.candidate,
                "namespace": namespace,
                "call_budget": cleanup_calls,
                "cost_budget_usd": 0.0,
                "timeout_seconds": min(self.timeout, cleanup_seconds),
                "authorization_nonce_sha256": self.nonce_digest,
                "required": ["cleanup"],
            }
            # Cleanup remains bound to the originally authorized candidate and
            # namespace even when local drift made that candidate impossible to
            # revalidate.  Only the local-drift checks are bypassed: request and
            # receipt binding, time/call/cost limits, and cleanup completeness
            # are still enforced below.
            cleanup = self._invoke(
                cleanup_request, tolerate_local_drift_for_cleanup=True,
            )
            assert cleanup.get("scenario") == "cleanup"
            self._usage(cleanup, cleanup_calls, 0.0)
            assert cleanup.get("cleanup") == {
                "attempted": True, "complete": True, "remaining": [],
            }


def live_authorization_session(pytestconfig: pytest.Config):
    if pytestconfig.getoption("--authorize-live-qualification") != CONFIRMATION:
        pytest.skip("live qualification requires explicit CLI authorization")
    namespace = pytestconfig.getoption("--live-namespace")
    if not re.fullmatch(r"mhq-[a-z0-9-]{8,64}", namespace):
        pytest.skip("live qualification requires an exact disposable mhq-* namespace")
    calls = pytestconfig.getoption("--live-call-budget")
    cost = pytestconfig.getoption("--live-cost-budget-usd")
    timeout = pytestconfig.getoption("--live-timeout")
    if not 1 <= calls <= 100 or not 0 < cost <= 100 or not 1 <= timeout <= 3600:
        pytest.skip("live qualification requires bounded aggregate call, cost, and time budgets")
    raw_driver = os.environ.get("MEMORY_HARNESS_LIVE_QUALIFICATION_DRIVER", "")
    driver = Path(raw_driver).expanduser()
    if not driver.is_absolute() or not driver.is_file() or not os.access(driver, os.X_OK):
        pytest.skip("live qualification driver must be an absolute executable file")
    artifact = Path(pytestconfig.getoption("--live-candidate-artifact")).expanduser()
    if not artifact.is_absolute() or not artifact.is_file():
        pytest.skip("live qualification requires an absolute candidate artifact")
    receipt_path = Path(
        pytestconfig.getoption("--live-verification-receipt")
    ).expanduser()
    try:
        candidate = _validated_candidate(artifact, receipt_path)
    except LiveAuthorizationError as exc:
        pytest.skip(str(exc))
    nonce = pytestconfig.getoption("--live-authorization-nonce")
    nonce_path = Path(pytestconfig.getoption("--live-authorization-file")).expanduser()
    consumed = _consume_nonce(nonce_path, nonce)
    try:
        try:
            refreshed = _validated_candidate(artifact, receipt_path)
        except LiveAuthorizationError as exc:
            pytest.skip(str(exc))
        if refreshed != candidate:
            pytest.skip(
                "live candidate binding changed while authorization was consumed"
            )
        authorization = LiveAuthorization(
            namespace=namespace, remaining_calls=calls, remaining_cost_usd=cost,
            timeout=timeout, driver=driver, candidate=candidate,
            nonce_digest=hashlib.sha256(nonce.encode("ascii")).hexdigest(),
            deadline=time.monotonic() + timeout,
            candidate_recheck=lambda: _validated_candidate(
                artifact, receipt_path,
            ),
        )
        preflight = authorization._invoke({
            "schema": "memory-harness-live-qualification/v2",
            "scenario": "preflight", "candidate": authorization.candidate,
            "namespace": namespace, "call_budget": 0, "cost_budget_usd": 0.0,
            "timeout_seconds": timeout,
            "authorization_nonce_sha256": authorization.nonce_digest,
            "required": [
                "budget_enforcement", "namespace_cleanup", "candidate_binding",
                "verification_receipt_binding",
            ],
        })
        assert preflight.get("scenario") == "preflight"
        authorization._usage(preflight, 0, 0.0)
        assert preflight.get("preflight") == {"ready": True}
        yield authorization
    finally:
        consumed.unlink(missing_ok=True)
