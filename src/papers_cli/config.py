from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from platformdirs import PlatformDirs

from .errors import PapersError


@dataclass(frozen=True, slots=True)
class AppPaths:
    data_dir: Path
    cache_dir: Path

    @property
    def database_path(self) -> Path:
        return self.data_dir / "papers.sqlite3"

    @property
    def objects_dir(self) -> Path:
        return self.data_dir / "objects" / "sha256"

    @property
    def staging_dir(self) -> Path:
        # Staging is intentionally in data: atomic replacement requires the same filesystem.
        return self.data_dir / ".staging"


def _override(name: str) -> Path | None:
    value = os.environ.get(name)
    if not value:
        return None
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise PapersError("invalid_config", f"{name} must be an absolute path", exit_code=2)
    return path


def get_paths() -> AppPaths:
    dirs = PlatformDirs("papers-cli", appauthor=False)
    return AppPaths(
        data_dir=_override("PAPERS_CLI_DATA_DIR") or Path(dirs.user_data_dir),
        cache_dir=_override("PAPERS_CLI_CACHE_DIR") or Path(dirs.user_cache_dir),
    )


def ensure_paths(paths: AppPaths) -> None:
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    paths.cache_dir.mkdir(parents=True, exist_ok=True)
    paths.objects_dir.mkdir(parents=True, exist_ok=True)
    paths.staging_dir.mkdir(parents=True, exist_ok=True)
