from __future__ import annotations

import sqlite3
import uuid
from dataclasses import replace

from papers_cli.db import DATABASE_SCHEMA_VERSION, LIST_PAPER_FIELDS, LOCAL_PAPER_FIELDS, Database
from papers_cli.models import REMOTE_PAPER_FIELDS, DownloadedFile, RemotePaper


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
        content_urls={"pdf": "https://arxiv.org/pdf/2301.00001v2"},
    )


def create_v1_database(path) -> tuple[str, tuple[str, str]]:
    paper_id = "0192e9ba-1234-7000-8000-000000000000"
    file_ids = ("file-a", "file-b")
    digests = ("a" * 64, "b" * 64)
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE papers (
            id TEXT PRIMARY KEY, source TEXT NOT NULL, source_key TEXT NOT NULL,
            source_version TEXT, title TEXT NOT NULL, abstract TEXT NOT NULL,
            authors_json TEXT NOT NULL, categories_json TEXT NOT NULL, published_at TEXT,
            updated_at TEXT, doi TEXT, landing_url TEXT NOT NULL, pdf_url TEXT NOT NULL,
            created_at TEXT NOT NULL, refreshed_at TEXT NOT NULL, UNIQUE(source, source_key)
        );
        CREATE TABLE aliases (
            scheme TEXT NOT NULL, normalized_value TEXT NOT NULL,
            paper_id TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
            created_at TEXT NOT NULL, PRIMARY KEY(scheme, normalized_value)
        );
        CREATE TABLE files (
            id TEXT PRIMARY KEY, sha256 TEXT NOT NULL UNIQUE, media_type TEXT NOT NULL,
            byte_count INTEGER NOT NULL, relative_path TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL
        );
        CREATE TABLE paper_files (
            paper_id TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
            file_id TEXT NOT NULL REFERENCES files(id), role TEXT NOT NULL DEFAULT 'pdf',
            source_version TEXT, retrieved_at TEXT NOT NULL, source_url TEXT NOT NULL,
            PRIMARY KEY(paper_id, file_id)
        );
        """
    )
    timestamp = "2026-01-01T00:00:00Z"
    connection.execute(
        """INSERT INTO papers VALUES (?, 'arxiv', '2301.00001', '2', 'Test', 'Abstract',
        '["Alice"]', '["cs.AI"]', NULL, NULL, '10.1000/test',
        'https://arxiv.org/abs/2301.00001v2', 'https://arxiv.org/pdf/2301.00001v2', ?, ?)""",
        (paper_id, timestamp, timestamp),
    )
    connection.execute(
        "INSERT INTO aliases VALUES ('arxiv', '2301.00001', ?, ?)", (paper_id, timestamp)
    )
    for file_id, digest in zip(file_ids, digests, strict=True):
        relative = f"objects/sha256/{digest[:2]}/{digest[2:4]}/{digest}.pdf"
        connection.execute(
            "INSERT INTO files VALUES (?, ?, 'application/pdf', 12, ?, ?)",
            (file_id, digest, relative, timestamp),
        )
        connection.execute(
            "INSERT INTO paper_files VALUES (?, ?, 'pdf', '2', ?, ?)",
            (paper_id, file_id, timestamp, f"https://arxiv.org/pdf/{file_id}"),
        )
    connection.execute("PRAGMA user_version = 1")
    connection.commit()
    connection.close()
    return paper_id, file_ids


def create_v2_database(path) -> str:
    paper_id = "0192e9ba-1234-7000-8000-000000000001"
    timestamp = "2026-01-01T00:00:00Z"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE papers (
            id TEXT PRIMARY KEY, source TEXT NOT NULL, source_key TEXT NOT NULL,
            source_version TEXT, title TEXT NOT NULL, abstract TEXT NOT NULL,
            authors_json TEXT NOT NULL, categories_json TEXT NOT NULL, published_at TEXT,
            updated_at TEXT, doi TEXT, landing_url TEXT NOT NULL, pdf_url TEXT,
            content_urls_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL,
            refreshed_at TEXT NOT NULL, UNIQUE(source, source_key)
        );
        CREATE TABLE aliases (
            scheme TEXT NOT NULL, normalized_value TEXT NOT NULL,
            paper_id TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
            created_at TEXT NOT NULL, PRIMARY KEY(scheme, normalized_value)
        );
        CREATE TABLE files (
            id TEXT PRIMARY KEY, sha256 TEXT NOT NULL UNIQUE, media_type TEXT NOT NULL,
            byte_count INTEGER NOT NULL, relative_path TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL
        );
        CREATE TABLE paper_files (
            paper_id TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
            file_id TEXT NOT NULL REFERENCES files(id),
            format TEXT NOT NULL CHECK(format IN ('pdf', 'txt', 'xml')),
            source_version TEXT, retrieved_at TEXT NOT NULL, source_url TEXT NOT NULL,
            provider TEXT NOT NULL, PRIMARY KEY(paper_id, file_id)
        );
        CREATE INDEX papers_created_order ON papers(created_at DESC, id DESC);
        CREATE INDEX paper_files_format ON paper_files(paper_id, format);
        """
    )
    connection.execute(
        """INSERT INTO papers VALUES (?, 'arxiv', '2301.00001', '2', 'Test', 'Abstract',
        '["Alice"]', '["cs.AI"]', NULL, NULL, '10.1000/test',
        'https://arxiv.org/abs/2301.00001v2', 'https://arxiv.org/pdf/2301.00001v2',
        '{"pdf":"https://arxiv.org/pdf/2301.00001v2"}', ?, ?)""",
        (paper_id, timestamp, timestamp),
    )
    connection.execute(
        "INSERT INTO aliases VALUES ('arxiv', '2301.00001', ?, ?)", (paper_id, timestamp)
    )
    connection.execute("PRAGMA user_version = 2")
    connection.commit()
    connection.close()
    return paper_id


def file_records(record: dict[str, object]) -> list[dict[str, object]]:
    value = record["files"]
    assert isinstance(value, list)
    files = [file for file in value if isinstance(file, dict)]
    assert len(files) == len(value)
    return files


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


def test_read_only_database_observes_committed_active_wal(tmp_path) -> None:
    path = tmp_path / "papers.sqlite3"
    writer = Database(path)
    paper_id = writer.upsert_paper(sample_paper())
    assert path.with_name("papers.sqlite3-wal").exists()

    reader = Database(path, read_only=True)
    assert reader.get(paper_id)["ref"] == "arxiv:2301.00001"
    reader.close()
    writer.close()


def test_v1_read_only_preserves_schema_and_legacy_attachments(tmp_path) -> None:
    path = tmp_path / "papers.sqlite3"
    paper_id, _ = create_v1_database(path)
    original = path.read_bytes()

    reader = Database(path, read_only=True)
    record = reader.get(paper_id)
    reader.close()

    files = file_records(record)
    assert [file["format"] for file in files] == ["pdf", "pdf"]
    assert [file["source_url"] for file in files] == [
        "https://arxiv.org/pdf/file-b",
        "https://arxiv.org/pdf/file-a",
    ]
    assert record["content_urls"] == {"pdf": "https://arxiv.org/pdf/2301.00001v2"}
    assert path.read_bytes() == original
    assert not path.with_name("papers.sqlite3-wal").exists()
    assert not path.with_name("papers.sqlite3-shm").exists()


def test_v1_writer_migrates_without_losing_ids_aliases_or_pdf_provenance(tmp_path) -> None:
    path = tmp_path / "papers.sqlite3"
    paper_id, file_ids = create_v1_database(path)

    database = Database(path)
    record = database.get("arxiv:2301.00001")
    assert database.schema_version == DATABASE_SCHEMA_VERSION
    assert record["id"] == paper_id
    files = file_records(record)
    assert [file["source_url"] for file in files] == [
        "https://arxiv.org/pdf/file-b",
        "https://arxiv.org/pdf/file-a",
    ]
    assert {file["source"] for file in files} == {"arxiv"}
    assert (
        database.connection.execute("PRAGMA user_version").fetchone()[0] == DATABASE_SCHEMA_VERSION
    )
    assert {
        row[0]
        for row in database.connection.execute(
            "SELECT file_id FROM paper_files WHERE paper_id = ?", (paper_id,)
        )
    } == set(file_ids)
    database.close()


def test_v2_writer_adds_pmc_metadata_columns_without_losing_identity(tmp_path) -> None:
    path = tmp_path / "papers.sqlite3"
    paper_id = create_v2_database(path)

    database = Database(path)
    record = database.get("arxiv:2301.00001")

    assert database.schema_version == DATABASE_SCHEMA_VERSION
    assert record["id"] == paper_id
    assert record["pmcid"] is None
    assert record["pmid"] is None
    assert record["license_code"] is None
    assert record["fulltext_availability"] == "unknown"
    assert database.connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert (
        database.connection.execute("PRAGMA user_version").fetchone()[0] == DATABASE_SCHEMA_VERSION
    )
    database.close()


def test_v2_read_only_preserves_schema_without_pmc_columns(tmp_path) -> None:
    path = tmp_path / "papers.sqlite3"
    paper_id = create_v2_database(path)
    original = path.read_bytes()

    database = Database(path, read_only=True)
    record = database.get(paper_id)
    database.close()

    assert record["fulltext_availability"] == "unknown"
    assert record["pmcid"] is None
    assert path.read_bytes() == original
    assert not path.with_name("papers.sqlite3-wal").exists()
    assert not path.with_name("papers.sqlite3-shm").exists()


def test_pmc_identifiers_license_and_availability_persist(tmp_path) -> None:
    database = Database(tmp_path / "papers.sqlite3")
    paper = replace(
        sample_paper(),
        source="pmc",
        source_key="PMC3531190",
        source_version="1",
        pmcid="PMC3531190",
        pmid="23193287",
        license_code="CC BY-NC",
        fulltext_availability="available",
    )
    paper_id = database.upsert_paper(paper)

    stored = database.get(paper_id)

    assert stored["pmcid"] == "PMC3531190"
    assert stored["pmid"] == "23193287"
    assert stored["license_code"] == "CC BY-NC"
    assert stored["fulltext_availability"] == "available"
    database.close()


def test_same_paper_formats_coexist_and_replacing_txt_keeps_pdf(tmp_path) -> None:
    database = Database(tmp_path / "papers.sqlite3")
    paper_id = database.upsert_paper(sample_paper())
    pdf = DownloadedFile(
        "a" * 64,
        12,
        "objects/sha256/aa/aa/" + "a" * 64 + ".pdf",
        "https://arxiv.org/pdf/x",
    )
    txt = DownloadedFile(
        "b" * 64,
        8,
        "objects/sha256/bb/bb/" + "b" * 64 + ".txt",
        "https://pmc.example/text/x",
        format="txt",
        media_type="text/plain",
        provider="pmc",
    )
    database.attach_file(paper_id, pdf, "2")
    database.attach_file(paper_id, txt, "7")
    first = database.get(paper_id)
    first_files = file_records(first)
    assert [file["format"] for file in first_files] == ["pdf", "txt"]
    legacy_file = first["file"]
    assert isinstance(legacy_file, dict)
    assert legacy_file["sha256"] == pdf.sha256
    assert first_files[1]["source"] == "pmc"
    assert first_files[1]["source_version"] == "7"

    replacement = replace(
        txt, sha256="c" * 64, relative_path="objects/sha256/cc/cc/" + "c" * 64 + ".txt"
    )
    database.attach_file(paper_id, replacement, "8")
    second = database.get(paper_id)
    assert [file["sha256"] for file in file_records(second)] == [pdf.sha256, replacement.sha256]
    assert len(database.list(None, None)) == 1
    database.close()


def test_remote_paper_fields_match_serialized_record_keys() -> None:
    assert set(REMOTE_PAPER_FIELDS) == set(sample_paper().as_dict())


def test_list_paper_fields_match_local_record_keys(tmp_path) -> None:
    database = Database(tmp_path / "papers.sqlite3")
    paper_id = database.upsert_paper(sample_paper())
    file = DownloadedFile(
        "a" * 64, 12, "objects/sha256/aa/aa/" + "a" * 64 + ".pdf", "https://arxiv.org/pdf/x"
    )
    database.attach_file(paper_id, file, "2")
    fileless_id = database.upsert_paper(replace(sample_paper(), source_key="2301.00002"))

    assert set(database.get(paper_id)) == set(LIST_PAPER_FIELDS)
    assert set(database.get(fileless_id)) == set(LIST_PAPER_FIELDS) - {"file"}
    assert set(LOCAL_PAPER_FIELDS) == {"id", "created_at", "refreshed_at", "file", "files"}
    assert set(LIST_PAPER_FIELDS) == set(REMOTE_PAPER_FIELDS) | set(LOCAL_PAPER_FIELDS)
    database.close()
