from __future__ import annotations

import uuid

from papers_cli.db import Database
from papers_cli.models import DownloadedFile, RemotePaper


class RaceInjectingConnection:
    """Inject a second writer immediately before the first insert executes."""

    def __init__(self, connection, competitor: Database, paper: RemotePaper) -> None:
        self._connection = connection
        self._competitor = competitor
        self._paper = paper
        self._injected = False

    def __enter__(self):
        self._connection.__enter__()
        return self

    def __exit__(self, *args):
        return self._connection.__exit__(*args)

    def __getattr__(self, name):
        return getattr(self._connection, name)

    def execute(self, sql, parameters=()):
        if not self._injected and "INSERT INTO papers" in sql:
            self._injected = True
            self._competitor.upsert_paper(self._paper)
        return self._connection.execute(sql, parameters)


class FileRaceInjectingConnection:
    """Inject a competing file write immediately before the first file insert."""

    def __init__(
        self, connection, competitor: Database, paper_id: str, file: DownloadedFile
    ) -> None:
        self._connection = connection
        self._competitor = competitor
        self._paper_id = paper_id
        self._file = file
        self._injected = False

    def __enter__(self):
        self._connection.__enter__()
        return self

    def __exit__(self, *args):
        return self._connection.__exit__(*args)

    def __getattr__(self, name):
        return getattr(self._connection, name)

    def execute(self, sql, parameters=()):
        if not self._injected and "INSERT OR IGNORE INTO files" in sql:
            self._injected = True
            self._competitor.attach_file(self._paper_id, self._file, "2")
        return self._connection.execute(sql, parameters)


def sample_paper() -> RemotePaper:
    return RemotePaper(
        source="arxiv",
        source_key="2301.00001",
        source_version="2",
        title="Test",
        abstract="Abstract",
        authors=["Alice"],
        categories=["cs.AI"],
        published_at=None,
        updated_at=None,
        doi="10.1000/test",
        landing_url="https://arxiv.org/abs/2301.00001v2",
        pdf_url="https://arxiv.org/pdf/2301.00001v2",
    )


def test_upsert_keeps_uuid7_and_deduplicates_file(tmp_path) -> None:
    database = Database(tmp_path / "papers.sqlite3")
    paper_id = database.upsert_paper(sample_paper())
    assert uuid.UUID(paper_id).version == 7
    assert database.upsert_paper(sample_paper()) == paper_id
    file = DownloadedFile(
        "a" * 64, 12, "objects/sha256/aa/aa/" + "a" * 64 + ".pdf", "https://arxiv.org/pdf/x"
    )
    database.attach_file(paper_id, file, "2")
    record = database.get("arxiv:2301.00001")
    assert record["id"] == paper_id
    assert record["file"] == {
        "sha256": "a" * 64,
        "byte_count": 12,
        "relative_path": file.relative_path,
        "source_url": file.source_url,
    }
    database.close()


def test_upsert_returns_canonical_id_after_insert_race(tmp_path) -> None:
    path = tmp_path / "papers.sqlite3"
    database = Database(path)
    competitor = Database(path)
    paper = sample_paper()
    database.connection = RaceInjectingConnection(database.connection, competitor, paper)  # type: ignore[assignment]

    canonical_id = database.upsert_paper(paper)
    file = DownloadedFile(
        "b" * 64,
        12,
        "objects/sha256/bb/bb/" + "b" * 64 + ".pdf",
        "https://arxiv.org/pdf/x",
    )
    database.attach_file(canonical_id, file, "2")

    assert canonical_id == competitor.get("arxiv:2301.00001")["id"]
    assert database.get(canonical_id)["file"] is not None
    database.close()
    competitor.close()


def test_attach_file_uses_canonical_id_after_digest_race(tmp_path) -> None:
    path = tmp_path / "papers.sqlite3"
    database = Database(path)
    competitor = Database(path)
    paper_id = database.upsert_paper(sample_paper())
    file = DownloadedFile(
        "c" * 64,
        12,
        "objects/sha256/cc/cc/" + "c" * 64 + ".pdf",
        "https://arxiv.org/pdf/x",
    )
    database.connection = FileRaceInjectingConnection(  # type: ignore[assignment]
        database.connection, competitor, paper_id, file
    )

    database.attach_file(paper_id, file, "2")

    canonical_id = competitor.connection.execute(
        "SELECT id FROM files WHERE sha256 = ?", (file.sha256,)
    ).fetchone()["id"]
    attached_id = database.connection.execute(
        "SELECT file_id FROM paper_files WHERE paper_id = ?", (paper_id,)
    ).fetchone()["file_id"]
    assert attached_id == canonical_id
    database.close()
    competitor.close()
