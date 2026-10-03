#!/usr/bin/env python3
"""Build the deterministic complete-product ZIP from repository source files."""

from __future__ import annotations

import argparse
import stat
import subprocess
import zipfile
from pathlib import Path, PurePosixPath


EPOCH = (1980, 1, 1, 0, 0, 0)
EXCLUDED_PARTS = {
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "runtime", "local-config", "build", "dist",
}

# Keep the portable artifact reviewable: it contains the two project manifests,
# both source trees, the documented test/install flow, and no repository history
# or runtime output.  Entries are selected from Git's tracked plus nonignored
# untracked candidate, so a not-yet-committed release candidate is represented
# faithfully.
INCLUDED_ROOTS = {".github", "src", "harness", "scripts", "tests"}
INCLUDED_FILES = {
    "pyproject.toml", "README.md", "TESTING.md", "AGENTS.md",
    "install.sh", "install.ps1",
}


def _selected(relative: Path) -> bool:
    return (
        relative.as_posix() in INCLUDED_FILES
        or bool(relative.parts and relative.parts[0] in INCLUDED_ROOTS)
    )


def repository_files(root: Path) -> list[Path]:
    completed = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=root, text=True, capture_output=True, check=True,
    )
    files: list[Path] = []
    for raw in sorted(set(completed.stdout.splitlines())):
        relative = Path(raw)
        if not raw or not _selected(relative):
            continue
        if any(part in EXCLUDED_PARTS or part.endswith(".egg-info") for part in relative.parts):
            continue
        path = root / relative
        if not path.is_file() or path.is_symlink():
            if path.is_symlink():
                raise ValueError(f"portable archive does not accept symlinks: {relative}")
            raise FileNotFoundError(relative)
        files.append(relative)
    if not files:
        raise RuntimeError("no complete-product source files were selected")
    return files


def repository_modes(root: Path) -> dict[Path, int]:
    """Derive executable bits from the Git index, never the build host."""
    completed = subprocess.run(
        ["git", "ls-files", "--stage"], cwd=root,
        text=True, capture_output=True, check=True,
    )
    modes: dict[Path, int] = {}
    for raw in completed.stdout.splitlines():
        metadata, name = raw.split("\t", 1)
        git_mode = metadata.split(" ", 1)[0]
        modes[Path(name)] = 0o755 if git_mode == "100755" else 0o644
    return modes


def _info(name: str, *, mode: int, directory: bool = False) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name + ("/" if directory and not name.endswith("/") else ""), EPOCH)
    info.create_system = 3
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = ((stat.S_IFDIR | 0o755) if directory else (stat.S_IFREG | mode)) << 16
    return info


def build(root: Path, output: Path) -> tuple[int, Path]:
    files = repository_files(root)
    modes = repository_modes(root)
    directories: set[PurePosixPath] = set()
    for relative in files:
        pure = PurePosixPath(relative.as_posix())
        directories.update(pure.parents)
    output = output if output.is_absolute() else root / output
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for directory in sorted(directories, key=lambda item: (len(item.parts), item.as_posix())):
                if directory.as_posix() != ".":
                    archive.writestr(_info(directory.as_posix(), mode=0o755, directory=True), b"")
            for relative in files:
                path = root / relative
                # Untracked source candidates have no index mode and use the
                # deterministic non-executable default.
                mode = modes.get(relative, 0o644)
                archive.writestr(_info(relative.as_posix(), mode=mode), path.read_bytes())
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)
    return len(files), output


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="product-memory-harness-20260926-01.zip")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    count, output = build(root, Path(args.output))
    print(f"{output} {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
