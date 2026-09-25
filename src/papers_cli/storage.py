from __future__ import annotations

import hashlib
import stat
from pathlib import Path

from .config import AppPaths
from .db import Database
from .errors import PapersError
from .models import CONTENT_FORMATS, content_suffix


def _available_formats(paper: dict[str, object]) -> list[str]:
    files = paper.get("files")
    if not isinstance(files, list):
        raise PapersError("storage_corrupt", "Stored file metadata is invalid", exit_code=5)
    formats = {
        file.get("format")
        for file in files
        if isinstance(file, dict) and isinstance(file.get("format"), str)
    }
    if any(format not in CONTENT_FORMATS for format in formats):
        raise PapersError("storage_corrupt", "Stored file metadata is invalid", exit_code=5)
    return [format for format in CONTENT_FORMATS if format in formats]


def _file_for_format(paper: dict[str, object], format: str) -> dict[str, object]:
    if format not in CONTENT_FORMATS:
        raise PapersError("storage_corrupt", "Stored file format is invalid", exit_code=5)
    files = paper.get("files")
    if not isinstance(files, list):
        raise PapersError("storage_corrupt", "Stored file metadata is invalid", exit_code=5)
    for file in files:
        if isinstance(file, dict) and file.get("format") == format:
            return file
    raise PapersError(
        "no_file",
        f"Paper has no downloaded {format} file",
        exit_code=3,
        details={"requested_format": format, "available_formats": _available_formats(paper)},
    )


def _validated_object_path(paths: AppPaths, file: dict[str, object]) -> Path:
    sha256 = file.get("sha256")
    relative = file.get("relative_path")
    format = file.get("format")
    if (
        not isinstance(sha256, str)
        or len(sha256) != 64
        or any(character not in "0123456789abcdef" for character in sha256)
        or not isinstance(relative, str)
        or not isinstance(format, str)
        or format not in CONTENT_FORMATS
    ):
        raise PapersError("storage_corrupt", "Stored file metadata is invalid", exit_code=5)
    expected = (
        Path("objects") / "sha256" / sha256[:2] / sha256[2:4] / f"{sha256}{content_suffix(format)}"
    ).as_posix()
    if relative != expected:
        raise PapersError(
            "storage_corrupt",
            "Stored file path is outside the content-addressed object layout",
            exit_code=5,
        )
    root = paths.objects_dir.resolve(strict=False)
    path = paths.data_dir / relative
    resolved = path.resolve(strict=False)
    if not resolved.is_relative_to(root) or path.is_symlink():
        raise PapersError("storage_corrupt", "Stored file path is unsafe", exit_code=5)
    return path


def _object_exists(path: Path) -> bool:
    try:
        metadata = path.stat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise PapersError(
            "storage_unavailable", "Unable to inspect the stored file", exit_code=5
        ) from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise PapersError("storage_corrupt", "Stored file is not a regular file", exit_code=5)
    return True


def remove_local(
    paths: AppPaths, database: Database, ref: str, *, dry_run: bool
) -> dict[str, object]:
    """Remove local metadata and safely unlink objects no longer referenced."""
    validated: dict[str, tuple[Path, bool]] = {}

    def validate(plan: dict[str, object]) -> None:
        files = plan.get("files")
        if not isinstance(files, list):
            raise PapersError("storage_corrupt", "Removal plan is invalid", exit_code=5)
        for file in files:
            if not isinstance(file, dict) or not isinstance(file.get("id"), str):
                raise PapersError("storage_corrupt", "Removal plan is invalid", exit_code=5)
            path = _validated_object_path(paths, file)
            validated[str(file["id"])] = (path, _object_exists(path))

    plan = database.removal_plan(ref) if dry_run else database.remove_paper(ref, validate)
    if dry_run:
        validate(plan)

    paper = plan.get("paper")
    files = plan.get("files")
    if not isinstance(paper, dict) or not isinstance(files, list):
        raise PapersError("storage_corrupt", "Removal plan is invalid", exit_code=5)

    objects: list[dict[str, object]] = []
    for file in files:
        if not isinstance(file, dict):
            raise PapersError("storage_corrupt", "Removal plan is invalid", exit_code=5)
        file_id = str(file["id"])
        path, exists = validated[file_id]
        references = int(file["reference_count"])
        if dry_run:
            disposition = (
                "retained_shared"
                if references > 1
                else "would_delete"
                if exists
                else "already_missing"
            )
        elif file.get("catalog_action") == "retain_shared":
            disposition = "retained_shared"
        else:

            def unlink(file_record: dict[str, object] = file) -> str:
                current_path = _validated_object_path(paths, file_record)
                if not _object_exists(current_path):
                    return "already_missing"
                try:
                    current_path.unlink()
                except FileNotFoundError:
                    return "already_missing"
                except OSError as exc:
                    raise PapersError(
                        "storage_remove",
                        "Paper metadata was removed, but an unreferenced file could not be deleted",
                        exit_code=5,
                        details={"paper_id": paper["id"], "path": str(current_path)},
                    ) from exc
                return "deleted"

            removed, guarded_disposition = database.remove_object_if_unreferenced(
                str(file["sha256"]), unlink
            )
            disposition = str(guarded_disposition) if removed else "retained_shared"
        objects.append(
            {
                "format": file["format"],
                "sha256": file["sha256"],
                "path": str(path),
                "disposition": disposition,
            }
        )

    return {
        "id": paper["id"],
        "ref": paper["ref"],
        "title": paper["title"],
        "dry_run": dry_run,
        "objects": objects,
    }


def _verify_attachment(
    paths: AppPaths, paper: dict[str, object], file: dict[str, object]
) -> dict[str, object]:
    relative = file.get("relative_path")
    expected = file.get("sha256")
    expected_size = file.get("byte_count")
    if (
        not isinstance(relative, str)
        or not isinstance(expected, str)
        or not isinstance(expected_size, int)
    ):
        raise PapersError("storage_corrupt", "Stored file metadata is invalid", exit_code=5)
    path = _validated_object_path(paths, file)
    format = str(file["format"])
    if not path.is_file():
        return {
            "ref": paper["ref"],
            "format": format,
            "ok": False,
            "status": "missing",
            "path": str(path),
        }
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
        "format": format,
        "ok": ok,
        "status": "verified" if ok else "mismatch",
        "path": str(path),
        "sha256": actual,
        "byte_count": size,
    }


def verify_file(
    paths: AppPaths, paper: dict[str, object], format: str | None = None
) -> dict[str, object]:
    if format is not None:
        return _verify_attachment(paths, paper, _file_for_format(paper, format))
    files = paper.get("files")
    if not isinstance(files, list):
        raise PapersError("storage_corrupt", "Stored file metadata is invalid", exit_code=5)
    if not files:
        raise PapersError(
            "no_file",
            "Paper has no downloaded files",
            exit_code=3,
            details={"requested_format": None, "available_formats": []},
        )
    results = [_verify_attachment(paths, paper, file) for file in files if isinstance(file, dict)]
    if len(results) != len(files):
        raise PapersError("storage_corrupt", "Stored file metadata is invalid", exit_code=5)
    if len(results) == 1 and results[0]["format"] == "pdf":
        legacy = dict(results[0])
        del legacy["format"]
        return legacy
    return {
        "ref": paper["ref"],
        "ok": all(result["ok"] is True for result in results),
        "status": "verified" if all(result["ok"] is True for result in results) else "failed",
        "files": results,
    }


def local_path(paths: AppPaths, paper: dict[str, object], format: str = "pdf") -> Path:
    return paths.data_dir / str(_file_for_format(paper, format)["relative_path"])
