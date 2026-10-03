"""Best-effort, epoch-scoped launch of the existing read-only visualizer.

The visualizer is not a controller and owns no harness state.  This module
only opens ``operator_launch view`` in a separate terminal at the public
``lane launch`` boundary.  An epoch-scoped decision record prevents duplicate
windows across lanes and concurrent CLI invocations.

Spawning a GUI process and publishing a record cannot be one transaction.  An
``ATTEMPTING`` record is therefore durable before spawn.  A synchronously
proven failure removes it and may be retried; a crash or post-spawn write
failure remains ambiguous and suppresses automatic retry.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from . import processes
from .config import find_harness_root, load_config
from .core import iso_utc, new_id
from .epochs import (
    CURRENT_EPOCH_SCHEMA,
    current_epoch_path,
    epoch_dir,
    read_epoch_state,
)
from .records import RecordLock, atomic_write_json, read_record

DECISION_SCHEMA = "visualizer-auto-launch/v1"
DECISION_FILE_NAME = "visualizer-auto-launch.json"

STATE_ATTEMPTING = "ATTEMPTING"
STATE_OPENED = "OPENED"
STATE_DISABLED = "DISABLED"
_STATES = frozenset({STATE_ATTEMPTING, STATE_OPENED, STATE_DISABLED})
_EPOCH_ID = re.compile(r"[0-9a-f]{32}")

VISUALIZER_OPENED = "VISUALIZER_OPENED"
VISUALIZER_ALREADY_OPEN = "VISUALIZER_ALREADY_OPEN"
VISUALIZER_DISABLED = "VISUALIZER_DISABLED"
VISUALIZER_UNAVAILABLE = "VISUALIZER_UNAVAILABLE"
VISUALIZER_FAILED = "VISUALIZER_FAILED"
VISUALIZER_AMBIGUOUS = "VISUALIZER_AMBIGUOUS"
VISUALIZER_NO_ACTIVE_EPOCH = "VISUALIZER_NO_ACTIVE_EPOCH"

# Long enough to catch an immediate launcher rejection, while keeping the
# visualization hook a small bounded prelude to the provider launch.
IMMEDIATE_EXIT_SECONDS = 0.35

_VIEWER_MODULE = "orchestrator_harness.operator_launch"
_MANUAL_FALLBACK = "run `orchestrator-harness view` manually"


class VisualizerUnavailable(RuntimeError):
    """The host has no supported graphical terminal path."""


class VisualizerLaunchFailed(RuntimeError):
    """A selected graphical terminal rejected the viewer command."""


@dataclass(frozen=True)
class TerminalInvocation:
    """One shell-free terminal launcher invocation and its subprocess options."""

    terminal: str
    argv: list[str]
    options: dict[str, Any]


def decision_path(rt: Path, epoch_id: str) -> Path:
    """Return the decision record for one exact epoch."""

    if not isinstance(epoch_id, str) or _EPOCH_ID.fullmatch(epoch_id) is None:
        raise ValueError("epoch_id must be one lowercase 32-hex harness identity")
    return epoch_dir(rt, epoch_id) / DECISION_FILE_NAME


def _result(
    *,
    ok: bool,
    code: str,
    status: str,
    summary: str,
    evidence_paths: list[str] | None = None,
    next_action: str = "none",
    warning: bool = False,
    decision_committed: bool = False,
) -> dict[str, Any]:
    return {
        "ok": ok,
        "code": code,
        "status": status,
        "summary": summary,
        "evidence_paths": evidence_paths or [],
        "next_action": next_action,
        "warning": warning,
        "decision_committed": decision_committed,
    }


# This is deliberately an allowlist.  In particular, credential-shaped names
# are not copied merely because their current values appear harmless.
_SAFE_ENVIRONMENT_NAMES = frozenset(
    {
        "APPDATA",
        "COLORTERM",
        "COMSPEC",
        "DBUS_SESSION_BUS_ADDRESS",
        "DESKTOP_STARTUP_ID",
        "DISPLAY",
        "GDK_BACKEND",
        "HOME",
        "HOMEDRIVE",
        "HOMEPATH",
        "LANG",
        "LANGUAGE",
        "LC_ADDRESS",
        "LC_ALL",
        "LC_COLLATE",
        "LC_CTYPE",
        "LC_IDENTIFICATION",
        "LC_MEASUREMENT",
        "LC_MESSAGES",
        "LC_MONETARY",
        "LC_NAME",
        "LC_NUMERIC",
        "LC_PAPER",
        "LC_TELEPHONE",
        "LC_TIME",
        "LOCALAPPDATA",
        "LOGNAME",
        "NO_COLOR",
        "PATH",
        "PATHEXT",
        "PYTHONHOME",
        "PYTHONPATH",
        "QT_QPA_PLATFORM",
        "SHELL",
        "SYSTEMDRIVE",
        "SYSTEMROOT",
        "TEMP",
        "TERM",
        "TMP",
        "TMPDIR",
        "USER",
        "USERNAME",
        "USERPROFILE",
        "VIRTUAL_ENV",
        "WAYLAND_DISPLAY",
        "WINDIR",
        "WT_SESSION",
        "XAUTHORITY",
        "XDG_CURRENT_DESKTOP",
        "XDG_RUNTIME_DIR",
        "XDG_SESSION_TYPE",
        "__CF_USER_TEXT_ENCODING",
    }
)

_CREDENTIAL_WORDS = frozenset(
    {
        "AUTH",
        "AUTHORIZATION",
        "BEARER",
        "CREDENTIAL",
        "CREDENTIALS",
        "KEY",
        "PASSWORD",
        "PASSWD",
        "PRIVATEKEY",
        "SECRET",
        "TOKEN",
    }
)


def _credential_environment_name(name: str) -> bool:
    normalized = name.upper()
    words = set(filter(None, re.split(r"[^A-Z0-9]+", normalized)))
    compact = "".join(words)
    return bool(words & _CREDENTIAL_WORDS) or any(
        marker in compact
        for marker in (
            "APIKEY",
            "AUTHORIZATION",
            "BEARER",
            "CREDENTIAL",
            "PASSWORD",
            "PRIVATEKEY",
            "SECRET",
            "TOKEN",
        )
    ) or compact.endswith("KEY")


def visualizer_environment(
    source: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return the minimal noncredential environment needed by the viewer."""

    inherited = os.environ if source is None else source
    credential_values = {
        value
        for key, value in inherited.items()
        if isinstance(key, str)
        and isinstance(value, str)
        and value
        and _credential_environment_name(key)
    }
    selected: dict[str, str] = {}
    for key, value in inherited.items():
        if not isinstance(key, str) or not isinstance(value, str):
            continue
        normalized = key.upper()
        if _credential_environment_name(key):
            continue
        if normalized in _SAFE_ENVIRONMENT_NAMES and value not in credential_values:
            selected[key] = value
    return selected


_MACOS_TERMINAL_SCRIPT = r"""
on run argv
    if (count of argv) < 2 then error "visualizer launcher arguments missing"
    set harnessRoot to item 1 of argv
    set pythonPath to item 2 of argv
    set commandText to "cd -- " & quoted form of harnessRoot & " && env -i"
    repeat with itemIndex from 3 to count of argv
        set commandText to commandText & " " & quoted form of (item itemIndex of argv)
    end repeat
    set commandText to commandText & " " & quoted form of pythonPath & " -m orchestrator_harness.operator_launch view"
    tell application "Terminal"
        activate
        do script commandText
    end tell
end run
""".strip()


def _terminal_invocation(
    viewer_argv: list[str],
    harness_root: Path,
    environment: Mapping[str, str],
    *,
    platform_name: str | None = None,
    os_name: str | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> TerminalInvocation:
    """Construct one platform-native new-terminal invocation."""

    platform_name = sys.platform if platform_name is None else platform_name
    os_name = os.name if os_name is None else os_name
    safe_environment = visualizer_environment(environment)
    cwd = str(Path(harness_root).absolute())

    if os_name == "nt" or platform_name == "win32":
        flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0x00000010)
        flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        return TerminalInvocation(
            "windows-console",
            list(viewer_argv),
            {
                "cwd": cwd,
                "env": safe_environment,
                "close_fds": True,
                "creationflags": flags,
            },
        )

    if platform_name == "darwin":
        osascript = which("osascript")
        if not osascript:
            raise VisualizerUnavailable("macOS Terminal automation is unavailable")
        osascript = str(Path(osascript).absolute())
        env_items = [f"{key}={safe_environment[key]}" for key in sorted(safe_environment)]
        # Dynamic paths and environment values are argv data.  The fixed
        # AppleScript applies `quoted form of` before its shell command.
        argv = [
            osascript,
            "-e",
            _MACOS_TERMINAL_SCRIPT,
            "--",
            cwd,
            viewer_argv[0],
            *env_items,
        ]
        return TerminalInvocation(
            "osascript-terminal",
            argv,
            {
                "cwd": cwd,
                "env": safe_environment,
                "stdin": subprocess.DEVNULL,
                "stdout": subprocess.DEVNULL,
                "stderr": subprocess.DEVNULL,
                "start_new_session": True,
            },
        )

    if platform_name.startswith("linux"):
        if not (
            safe_environment.get("DISPLAY")
            or safe_environment.get("WAYLAND_DISPLAY")
        ):
            raise VisualizerUnavailable(
                "no graphical display is available on this Linux session"
            )
        candidates = (
            ("x-terminal-emulator", ("-e",)),
            ("gnome-terminal", ("--",)),
            ("konsole", ("-e",)),
            ("xterm", ("-e",)),
            ("kitty", ("--detach",)),
            ("alacritty", ("-e",)),
        )
        for name, separator in candidates:
            executable = which(name)
            if executable:
                executable = str(Path(executable).absolute())
                return TerminalInvocation(
                    name,
                    [executable, *separator, *viewer_argv],
                    {
                        "cwd": cwd,
                        "env": safe_environment,
                        "stdin": subprocess.DEVNULL,
                        "stdout": subprocess.DEVNULL,
                        "stderr": subprocess.DEVNULL,
                        "start_new_session": True,
                    },
                )
        raise VisualizerUnavailable(
            "no supported terminal emulator was found (tried x-terminal-emulator, "
            "gnome-terminal, konsole, xterm, kitty, and alacritty)"
        )

    raise VisualizerUnavailable(
        f"automatic terminal opening is unsupported on {platform_name or os_name}"
    )


def _spawn_terminal(invocation: TerminalInvocation) -> dict[str, str]:
    """Spawn and briefly probe a terminal launcher for immediate rejection."""

    try:
        process = subprocess.Popen(invocation.argv, **invocation.options)
    except OSError as exc:
        raise VisualizerLaunchFailed(
            f"{invocation.terminal} could not start: {exc}"
        ) from exc
    try:
        returncode = process.wait(timeout=IMMEDIATE_EXIT_SECONDS)
    except subprocess.TimeoutExpired:
        return {"terminal": invocation.terminal}
    if returncode != 0:
        raise VisualizerLaunchFailed(
            f"{invocation.terminal} rejected the viewer command (exit {returncode})"
        )
    # Some desktop launchers hand the command to an existing terminal service
    # and exit zero immediately.  That is their positive acceptance signal.
    return {"terminal": invocation.terminal}


def _open_visualizer_terminal(harness_root: Path) -> dict[str, str]:
    environment = visualizer_environment()
    viewer_argv = [sys.executable, "-m", _VIEWER_MODULE, "view"]
    invocation = _terminal_invocation(viewer_argv, harness_root, environment)
    return _spawn_terminal(invocation)


def _launcher_identity() -> dict[str, Any]:
    identity = processes.process_identity(os.getpid())
    if identity is not None:
        return identity
    return {"pid": os.getpid(), "creation_time": None}


def _is_redirected(path: Path) -> bool:
    """Return whether an existing component is a symlink or Windows reparse point."""

    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _record_lock_path(path: Path) -> Path:
    return path.parent / f".{path.name}.lock"


def _validate_active_epoch_storage(rt: Path, epoch_id: str) -> Path:
    """Validate the active epoch and every component used by the decision."""

    # decision_path performs the closed identity grammar check before the ID is
    # ever joined into a filesystem path.
    runtime = Path(rt).absolute()
    path = decision_path(runtime, epoch_id)
    epochs_root = runtime / "epochs"
    directory = epoch_dir(runtime, epoch_id)
    state_path = directory / "epoch-state.json"
    for component, label in (
        (runtime, "runtime root"),
        (epochs_root, "epochs root"),
        (directory, "epoch directory"),
    ):
        if not component.is_dir() or _is_redirected(component):
            raise ValueError(f"{label} is missing or redirected: {component}")
    for component, label in (
        (state_path, "epoch state"),
        (path, "visualizer decision"),
        (_record_lock_path(path), "visualizer decision lock"),
    ):
        if _is_redirected(component):
            raise ValueError(f"{label} is redirected: {component}")
    state = read_epoch_state(runtime, epoch_id)
    if state.get("epoch_id") != epoch_id or state.get("lifecycle") != "active":
        raise ValueError("the selected epoch state is not the matching active epoch")
    return path


def _validated_decision(path: Path, epoch_id: str) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    decision = read_record(path, DECISION_SCHEMA)
    if decision.get("epoch_id") != epoch_id:
        raise ValueError("visualizer decision belongs to a different epoch")
    state = decision.get("state")
    if state not in _STATES:
        raise ValueError("visualizer decision has an unknown state")
    attempt_id = decision.get("attempt_id")
    if not isinstance(attempt_id, str) or _EPOCH_ID.fullmatch(attempt_id) is None:
        raise ValueError("visualizer decision has an invalid attempt identity")
    launcher = decision.get("launcher")
    if not isinstance(launcher, dict):
        raise ValueError("visualizer decision has an invalid launcher identity")
    pid = launcher.get("pid")
    creation_time = launcher.get("creation_time")
    if (
        not isinstance(pid, int)
        or isinstance(pid, bool)
        or pid <= 0
        or not (
            creation_time is None
            or (isinstance(creation_time, str) and bool(creation_time.strip()))
        )
    ):
        raise ValueError("visualizer decision has an invalid launcher identity")

    def require_nonblank(field: str) -> None:
        value = decision.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"visualizer decision has an invalid {field}")

    if state == STATE_DISABLED:
        require_nonblank("decided_at")
    else:
        require_nonblank("attempted_at")
        if state == STATE_OPENED:
            require_nonblank("opened_at")
            require_nonblank("terminal")
    return decision


def _ambiguous(
    path: Path,
    summary: str,
    *,
    decision_committed: bool = False,
) -> dict[str, Any]:
    return _result(
        ok=False,
        code=VISUALIZER_AMBIGUOUS,
        status="ambiguous",
        summary=summary,
        evidence_paths=[str(path)],
        next_action=_MANUAL_FALLBACK
        + "; inspect the epoch decision before removing it or retrying automatic open",
        warning=True,
        decision_committed=decision_committed,
    )


def _decide_for_epoch(
    rt: Path,
    harness_root: Path,
    epoch_id: str,
    *,
    disabled: bool,
    opener: Callable[[], dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Apply the first-decision-wins visualizer contract for one epoch."""

    path = decision_path(rt, epoch_id)
    opener = opener or (lambda: _open_visualizer_terminal(harness_root))
    with RecordLock(path):
        try:
            decision = _validated_decision(path, epoch_id)
        except (OSError, ValueError) as exc:
            return _ambiguous(path, f"visualizer decision is unreadable or invalid: {exc}")

        if decision is not None:
            state = decision["state"]
            if state == STATE_DISABLED:
                return _result(
                    ok=True,
                    code=VISUALIZER_DISABLED,
                    status="disabled",
                    summary="automatic visualizer opening is disabled for this epoch",
                    evidence_paths=[str(path)],
                    next_action=_MANUAL_FALLBACK + " if a viewer is wanted",
                    decision_committed=True,
                )
            if state == STATE_OPENED:
                late_disable = disabled
                return _result(
                    ok=True,
                    code=VISUALIZER_ALREADY_OPEN,
                    status="already_open",
                    summary=(
                        "the visualizer is already open; --no-visualizer cannot close "
                        "an existing window"
                        if late_disable
                        else "the visualizer was already opened for this epoch"
                    ),
                    evidence_paths=[str(path)],
                    next_action=(
                        "quit the existing viewer window to close it"
                        if late_disable
                        else "none"
                    ),
                    warning=late_disable,
                    decision_committed=True,
                )
            return _ambiguous(
                path,
                "a prior visualizer launch may have opened a window; automatic retry "
                "is suppressed to prevent a duplicate",
                decision_committed=True,
            )

        if disabled:
            decision = {
                "schema": DECISION_SCHEMA,
                "epoch_id": epoch_id,
                "state": STATE_DISABLED,
                "attempt_id": new_id(),
                "decided_at": iso_utc(),
                "launcher": _launcher_identity(),
            }
            try:
                atomic_write_json(path, decision)
            except OSError as exc:
                return _result(
                    ok=False,
                    code=VISUALIZER_FAILED,
                    status="failed",
                    summary=f"cannot persist the visualizer opt-out: {exc}",
                    evidence_paths=[str(path)],
                    next_action="resolve runtime storage, then retry with --no-visualizer",
                    warning=True,
                )
            return _result(
                ok=True,
                code=VISUALIZER_DISABLED,
                status="disabled",
                summary="automatic visualizer opening is disabled for this epoch",
                evidence_paths=[str(path)],
                next_action=_MANUAL_FALLBACK + " if a viewer is wanted",
                decision_committed=True,
            )

        attempt_id = new_id()
        attempt = {
            "schema": DECISION_SCHEMA,
            "epoch_id": epoch_id,
            "state": STATE_ATTEMPTING,
            "attempt_id": attempt_id,
            "attempted_at": iso_utc(),
            "launcher": _launcher_identity(),
        }
        try:
            atomic_write_json(path, attempt)
        except OSError as exc:
            return _result(
                ok=False,
                code=VISUALIZER_FAILED,
                status="failed",
                summary=f"cannot persist visualizer launch intent: {exc}",
                evidence_paths=[str(path)],
                next_action=_MANUAL_FALLBACK,
                warning=True,
            )

        try:
            opened = opener()
        except (VisualizerUnavailable, VisualizerLaunchFailed) as exc:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError as cleanup_exc:
                return _ambiguous(
                    path,
                    "the visualizer was unavailable but its attempt record could not "
                    f"be cleared: {cleanup_exc}",
                    decision_committed=True,
                )
            unavailable = isinstance(exc, VisualizerUnavailable)
            return _result(
                ok=False,
                code=(VISUALIZER_UNAVAILABLE if unavailable else VISUALIZER_FAILED),
                status=("unavailable" if unavailable else "failed"),
                summary=str(exc),
                evidence_paths=[],
                next_action=_MANUAL_FALLBACK,
                warning=True,
            )
        except Exception as exc:
            return _ambiguous(
                path,
                "the visualizer spawn outcome is uncertain; automatic retry is "
                f"suppressed to prevent a duplicate: {exc}",
                decision_committed=True,
            )

        try:
            complete = {
                **attempt,
                "state": STATE_OPENED,
                "opened_at": iso_utc(),
                "terminal": opened.get("terminal", "unknown"),
            }
            atomic_write_json(path, complete)
        except Exception as exc:
            return _ambiguous(
                path,
                "the terminal accepted the viewer but OPENED publication failed; "
                f"automatic retry is suppressed: {exc}",
                decision_committed=True,
            )
        return _result(
            ok=True,
            code=VISUALIZER_OPENED,
            status="opened",
            summary="visualizer opened in a new terminal window",
            evidence_paths=[str(path)],
            next_action="leave the viewer open while lanes run; press q to close it",
            decision_committed=True,
        )


def _ensure_for_current_epoch(*, disabled: bool) -> dict[str, Any]:
    try:
        harness_root = find_harness_root()
        config = load_config(harness_root)
    except Exception as exc:
        return _result(
            ok=False,
            code=VISUALIZER_UNAVAILABLE,
            status="unavailable",
            summary=f"visualizer configuration is unavailable: {exc}",
            next_action=_MANUAL_FALLBACK + " after fixing harness configuration",
            warning=True,
        )

    marker_path = current_epoch_path(config.runtime_root)
    if not marker_path.is_file():
        return _result(
            ok=False,
            code=VISUALIZER_NO_ACTIVE_EPOCH,
            status="no_active_epoch",
            summary="there is no active epoch to visualize",
            next_action="bootstrap a lane before launching it",
            warning=True,
        )
    if _is_redirected(config.runtime_root) or _is_redirected(marker_path) or _is_redirected(
        _record_lock_path(marker_path)
    ):
        return _result(
            ok=False,
            code=VISUALIZER_AMBIGUOUS,
            status="ambiguous",
            summary="the active epoch marker path is redirected",
            evidence_paths=[str(marker_path)],
            next_action="repair or retire the exact runtime before retrying",
            warning=True,
        )
    # Coordinate with epoch retirement so the selected epoch cannot be cleared
    # while its first visualizer decision is being committed.
    with RecordLock(marker_path):
        if not marker_path.is_file():
            return _result(
                ok=False,
                code=VISUALIZER_NO_ACTIVE_EPOCH,
                status="no_active_epoch",
                summary="there is no active epoch to visualize",
                next_action="bootstrap a lane before launching it",
                warning=True,
            )
        try:
            marker = read_record(marker_path, CURRENT_EPOCH_SCHEMA)
            epoch_id = marker.get("epoch_id")
            if not isinstance(epoch_id, str) or _EPOCH_ID.fullmatch(epoch_id) is None:
                raise ValueError(
                    "current epoch ID is not a lowercase 32-hex harness identity"
                )
            _validate_active_epoch_storage(config.runtime_root, epoch_id)
        except (OSError, ValueError) as exc:
            return _result(
                ok=False,
                code=VISUALIZER_AMBIGUOUS,
                status="ambiguous",
                summary=f"the active epoch marker is invalid: {exc}",
                evidence_paths=[str(marker_path)],
                next_action="repair or retire the exact runtime before retrying",
                warning=True,
            )
        return _decide_for_epoch(
            config.runtime_root,
            harness_root,
            epoch_id,
            disabled=disabled,
        )


def ensure_for_current_epoch(*, disabled: bool = False) -> dict[str, Any]:
    """Apply automatic-viewer policy without blocking the lane on UI failure."""

    try:
        return _ensure_for_current_epoch(disabled=disabled)
    except Exception as exc:
        # Runtime record locks and local UI I/O are best-effort at this
        # boundary.  The native lane launch remains authoritative and must
        # still run.
        return _result(
            ok=False,
            code=VISUALIZER_FAILED,
            status="failed",
            summary=f"automatic visualizer integration failed: {exc}",
            next_action=_MANUAL_FALLBACK,
            warning=True,
        )
