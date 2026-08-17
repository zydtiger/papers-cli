from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path

from .errors import PapersError
from .ids import uuid7
from .models import DownloadedFile, RemotePaper


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class Database:
    def __init__(self, path: Path, *, read_only: bool = False) -> None:
        if read_only:
            self.connection = sqlite3.connect(
                f"{path.absolute().as_uri()}?mode=ro&immutable=1", uri=True
            )
        else:
            self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        if not read_only:
            self.connection.execute("PRAGMA journal_mode = WAL")
            self._initialize()

    def close(self) -> None:
        self.connection.close()

    def _initialize(self) -> None:
        with self.connection:
            self.connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS papers (
                    id TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    source_key TEXT NOT NULL,
                    source_version TEXT,
                    title TEXT NOT NULL,
                    abstract TEXT NOT NULL,
                    authors_json TEXT NOT NULL,
                    categories_json TEXT NOT NULL,
                    published_at TEXT,
                    updated_at TEXT,
                    doi TEXT,
                    landing_url TEXT NOT NULL,
                    pdf_url TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    refreshed_at TEXT NOT NULL,
                    UNIQUE(source, source_key)
                );
                CREATE TABLE IF NOT EXISTS aliases (
                    scheme TEXT NOT NULL,
                    normalized_value TEXT NOT NULL,
                    paper_id TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(scheme, normalized_value)
                );
                CREATE TABLE IF NOT EXISTS files (
                    id TEXT PRIMARY KEY,
                    sha256 TEXT NOT NULL UNIQUE,
                    media_type TEXT NOT NULL,
                    byte_count INTEGER NOT NULL,
                    relative_path TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS paper_files (
                    paper_id TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
                    file_id TEXT NOT NULL REFERENCES files(id),
                    role TEXT NOT NULL DEFAULT 'pdf',
                    source_version TEXT,
                    retrieved_at TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    PRIMARY KEY(paper_id, file_id)
                );
                CREATE INDEX IF NOT EXISTS papers_created_order ON papers(created_at DESC, id DESC);
                """
            )
            self.connection.execute("PRAGMA user_version = 1")

    @staticmethod
    def _aliases(paper: RemotePaper) -> list[tuple[str, str]]:
        aliases = [(paper.source, paper.source_key)]
        if paper.doi:
            aliases.append(("doi", paper.doi.lower()))
        return aliases

    def upsert_paper(self, paper: RemotePaper) -> str:
        now = _now()
        candidate_id = uuid7()
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO papers (
                    id, source, source_key, source_version, title, abstract,
                    authors_json, categories_json, published_at, updated_at, doi,
                    landing_url, pdf_url, created_at, refreshed_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source, source_key) DO UPDATE SET
                  source_version=excluded.source_version,
                  title=excluded.title,
                  abstract=excluded.abstract,
                  authors_json=excluded.authors_json,
                  categories_json=excluded.categories_json,
                  published_at=excluded.published_at,
                  updated_at=excluded.updated_at,
                  doi=excluded.doi,
                  landing_url=excluded.landing_url,
                  pdf_url=excluded.pdf_url,
                  refreshed_at=excluded.refreshed_at
                """,
                (
                    candidate_id,
                    paper.source,
                    paper.source_key,
                    paper.source_version,
                    paper.title,
                    paper.abstract,
                    json.dumps(paper.authors),
                    json.dumps(paper.categories),
                    paper.published_at,
                    paper.updated_at,
                    paper.doi,
                    paper.landing_url,
                    paper.pdf_url,
                    now,
                    now,
                ),
            )
            row = self.connection.execute(
                "SELECT id FROM papers WHERE source = ? AND source_key = ?",
                (paper.source, paper.source_key),
            ).fetchone()
            if row is None:
                raise PapersError(
                    "storage_corrupt", "Paper upsert did not persist a record", exit_code=5
                )
            paper_id = str(row["id"])
            for scheme, value in self._aliases(paper):
                self.connection.execute(
                    """INSERT OR IGNORE INTO aliases
                    (scheme, normalized_value, paper_id, created_at) VALUES (?, ?, ?, ?)""",
                    (scheme, value, paper_id, now),
                )
        return paper_id

    def attach_file(self, paper_id: str, file: DownloadedFile, source_version: str | None) -> None:
        now = _now()
        candidate_id = uuid7()
        with self.connection:
            self.connection.execute(
                """INSERT OR IGNORE INTO files
                (id, sha256, media_type, byte_count, relative_path, created_at)
                VALUES (?, ?, 'application/pdf', ?, ?, ?)""",
                (candidate_id, file.sha256, file.byte_count, file.relative_path, now),
            )
            row = self.connection.execute(
                "SELECT id FROM files WHERE sha256 = ?", (file.sha256,)
            ).fetchone()
            if row is None:
                raise PapersError(
                    "storage_corrupt", "File upsert did not persist a record", exit_code=5
                )
            file_id = str(row["id"])
            self.connection.execute(
                "DELETE FROM paper_files WHERE paper_id = ? AND role = 'pdf'", (paper_id,)
            )
            self.connection.execute(
                """INSERT INTO paper_files
                (paper_id, file_id, role, source_version, retrieved_at, source_url)
                VALUES (?, ?, 'pdf', ?, ?, ?)""",
                (paper_id, file_id, source_version, now, file.source_url),
            )

    def _row_to_dict(self, row: sqlite3.Row) -> dict[str, object]:
        result: dict[str, object] = {
            "id": row["id"],
            "source": row["source"],
            "source_key": row["source_key"],
            "source_version": row["source_version"],
            "ref": f"{row['source']}:{row['source_key']}",
            "title": row["title"],
            "abstract": row["abstract"],
            "authors": json.loads(row["authors_json"]),
            "categories": json.loads(row["categories_json"]),
            "published_at": row["published_at"],
            "updated_at": row["updated_at"],
            "doi": row["doi"],
            "landing_url": row["landing_url"],
            "pdf_url": row["pdf_url"],
            "created_at": row["created_at"],
            "refreshed_at": row["refreshed_at"],
        }
        if row["sha256"] is not None:
            result["file"] = {
                "sha256": row["sha256"],
                "byte_count": row["byte_count"],
                "relative_path": row["relative_path"],
                "source_url": row["file_source_url"],
            }
        return result

    @staticmethod
    def _select() -> str:
        return """
        SELECT p.*, f.sha256, f.byte_count, f.relative_path, pf.source_url AS file_source_url
        FROM papers p
        LEFT JOIN paper_files pf ON pf.paper_id = p.id AND pf.role = 'pdf'
        LEFT JOIN files f ON f.id = pf.file_id
        """

    def get(self, ref: str) -> dict[str, object]:
        row = None
        try:
            identifier = str(uuid.UUID(ref))
            row = self.connection.execute(
                self._select() + " WHERE p.id = ?", (identifier,)
            ).fetchone()
        except ValueError:
            if ":" in ref:
                scheme, value = ref.split(":", 1)
                row = self.connection.execute(
                    self._select()
                    + " JOIN aliases a ON a.paper_id = p.id"
                    + " WHERE a.scheme = ? AND a.normalized_value = ?",
                    (scheme.lower(), value.lower()),
                ).fetchone()
        if row is None:
            raise PapersError("not_found", f"No local paper matches {ref}", exit_code=3)
        return self._row_to_dict(row)

    def list(self, source: str | None, limit: int | None) -> list[dict[str, object]]:
        query = self._select()
        params: tuple[object, ...] = ()
        if source:
            query += " WHERE p.source = ?"
            params = (source,)
        query += " ORDER BY p.created_at DESC, p.id DESC"
        if limit is not None:
            query += " LIMIT ?"
            params = (*params, limit)
        rows = self.connection.execute(query, params).fetchall()
        return [self._row_to_dict(row) for row in rows]
