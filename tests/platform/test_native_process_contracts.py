"""Native-host process identity and cleanup coordinates.

Only the executing host earns NATIVE credit. Foreign coordinates collect and
skip with an exact reason rather than passing via monkeypatching.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from harness_common.process_identity import exact_process_identity


def test_expected_native_host_is_executing() -> None:
    expected = os.environ.get("MEMORY_HARNESS_EXPECT_PLATFORM")
    if expected is None:
        pytest.skip("no CI target platform was declared")
    actual = "win32" if os.name == "nt" else sys.platform
    assert actual == expected, f"CI selected native {expected}, but Python is running on {actual}"


@pytest.mark.parametrize("target", ["linux", "darwin", "win32"])
def test_native_host_spawn_identity_and_exact_reap(tmp_path: Path, target: str) -> None:
    actual = "win32" if os.name == "nt" else sys.platform
    if actual != target:
        pytest.skip(f"native {target} host required; executing on {actual}")
    ready = tmp_path / "ready"
    sentinel = tmp_path / "unrelated"
    sentinel.write_text("untouched", encoding="utf-8")
    child = subprocess.Popen([
        sys.executable, "-c",
        "import pathlib,sys,time; pathlib.Path(sys.argv[1]).write_text('ready'); time.sleep(30)",
        str(ready),
    ], cwd=tmp_path)
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.read_text(encoding="utf-8") == "ready"
        identity = exact_process_identity(child.pid)
        assert identity is not None
        assert identity["pid"] == child.pid
        assert identity["created_utc"]
        assert exact_process_identity(child.pid) == identity
        child.terminate()
        assert child.wait(timeout=5) != 0
        assert exact_process_identity(child.pid) is None
        assert sentinel.read_text(encoding="utf-8") == "untouched"
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)


def test_path_and_argument_contract_handles_spaces_without_shell(tmp_path: Path) -> None:
    spaced = tmp_path / "directory with spaces"
    spaced.mkdir()
    target = spaced / "value with spaces.json"
    value = 'literal $HOME ; && "quoted"'
    completed = subprocess.run(
        [sys.executable, "-c", "import json,pathlib,sys; pathlib.Path(sys.argv[1]).write_text(json.dumps(sys.argv[2]))", str(target), value],
        cwd=spaced, text=True, capture_output=True, timeout=5,
    )
    assert completed.returncode == 0
    import json
    assert json.loads(target.read_text(encoding="utf-8")) == value
