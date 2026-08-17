from __future__ import annotations

import hashlib
from pathlib import Path

from .config import AppPaths
from .errors import PapersError


def verify_file(paths: AppPaths, paper: dict[str, object]) -> dict[str, object]:
    file = paper.get("file")
    if not isinstance(file, dict):
        raise PapersError("no_file", "Paper has no downloaded PDF", exit_code=3)
    relative = file.get("relative_path")
    expected = file.get("sha256")
    expected_size = file.get("byte_count")
    if (
        not isinstance(relative, str)
        or not isinstance(expected, str)
        or not isinstance(expected_size, int)
    ):
        raise PapersError("storage_corrupt", "Stored file metadata is invalid", exit_code=5)
    path = paths.data_dir / relative
    if not path.is_file():
        return {"ref": paper["ref"], "ok": False, "status": "missing", "path": str(path)}
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(64 * 1024), b""):
            size += len(chunk)
            digest.update(chunk)
    actual = digest.hexdigest()
    ok = actual == expected and size == expected_size
    return {
        "ref": paper["ref"],
        "ok": ok,
        "status": "verified" if ok else "mismatch",
        "path": str(path),
        "sha256": actual,
        "byte_count": size,
    }


def local_path(paths: AppPaths, paper: dict[str, object]) -> Path:
    file = paper.get("file")
    if not isinstance(file, dict) or not isinstance(file.get("relative_path"), str):
        raise PapersError("no_file", "Paper has no downloaded PDF", exit_code=3)
    return paths.data_dir / str(file["relative_path"])
