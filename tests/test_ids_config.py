from __future__ import annotations

import uuid

import pytest

from papers_cli.config import get_paths
from papers_cli.errors import PapersError
from papers_cli.ids import uuid7


def test_uuid7_has_correct_version_and_variant() -> None:
    identifier = uuid.UUID(uuid7())
    assert identifier.version == 7
    assert identifier.variant == uuid.RFC_4122


def test_paths_honor_absolute_overrides(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("PAPERS_CLI_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("PAPERS_CLI_CACHE_DIR", str(tmp_path / "cache"))
    paths = get_paths()
    assert paths.data_dir == tmp_path / "data"
    assert paths.cache_dir == tmp_path / "cache"


def test_paths_reject_relative_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PAPERS_CLI_DATA_DIR", "relative")
    with pytest.raises(PapersError, match="absolute"):
        get_paths()
