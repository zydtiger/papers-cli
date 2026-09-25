from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from .errors import PapersError
from .ids import uuid7
from .models import (
    CONTENT_FORMATS,
    REMOTE_PAPER_FIELDS,
    DownloadedFile,
    RemotePaper,
    content_media_type,
)

DATABASE_SCHEMA_VERSION = 3
LEGACY_DATABASE_SCHEMA_VERSION = 1
MULTIFORMAT_DATABASE_SCHEMA_VERSION = 2

# Top-level keys that Database._row_to_dict adds beyond the shared remote-paper
# vocabulary; tests pin both constants to the serialized record keys.
LOCAL_PAPER_FIELDS: Final[tuple[str, ...]] = (
    "id",
    "created_at",
    "refreshed_at",
    "file",
    "files",
)
LIST_PAPER_FIELDS: Final[tuple[str, ...]] = (*REMOTE_PAPER_FIELDS, *LOCAL_PAPER_FIELDS)


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class Database:
    def __init__(self, path: Path, *, read_only: bool = False) -> None:
        self.path = path
        self.read_only = read_only
        if read_only:
            # SQLite needs its normal readonly VFS to observe committed records in an active WAL.
            # Without a WAL, immutable mode avoids creating a sidecar pair merely to read.
            query = (
                "mode=ro" if path.with_name(f"{path.name}-wal").exists() else "mode=ro&immutable=1"
            )
            self.connection = sqlite3.connect(f"{path.absolute().as_uri()}?{query}", uri=True)
        else:
            self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        if read_only:
            self.schema_version = self._read_schema_version()
            self._validate_schema_version()
        else:
            self.connection.execute("PRAGMA journal_mode = WAL")
            self._initialize()

    def close(self) -> None:
        self.connection.close()

    def _read_schema_version(self) -> int:
        row = self.connection.execute("PRAGMA user_version").fetchone()
        if row is None:
            raise PapersError(
                "storage_unavailable",
                "Unable to read the local collection schema version",
                exit_code=5,
            )
        return int(row[0])

    def _validate_schema_version(self) -> None:
        if self.schema_version not in {
            LEGACY_DATABASE_SCHEMA_VERSION,
            MULTIFORMAT_DATABASE_SCHEMA_VERSION,
            DATABASE_SCHEMA_VERSION,
        }:
            raise PapersError(
                "storage_unavailable",
                f"Unsupported local collection schema version: {self.schema_version}",
                exit_code=5,
            )

    def _initialize(self) -> None:
        version = self._read_schema_version()
        if version == 0:
            existing = self.connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'papers'"
            ).fetchone()
            if existing is not None:
                raise PapersError(
                    "storage_unavailable",
                    "Local collection has no recognized schema version",
                    exit_code=5,
                )
            self._create_schema_v3()
        elif version == LEGACY_DATABASE_SCHEMA_VERSION:
            self._migrate_v1_to_v2()
            self._migrate_v2_to_v3()
        elif version == MULTIFORMAT_DATABASE_SCHEMA_VERSION:
            self._migrate_v2_to_v3()
        elif version != DATABASE_SCHEMA_VERSION:
            raise PapersError(
                "storage_unavailable",
                f"Unsupported local collection schema version: {version}",
                exit_code=5,
            )
        self.schema_version = DATABASE_SCHEMA_VERSION

    def _create_schema_v3(self) -> None:
        try:
            self.connection.executescript(
                f"""
                BEGIN IMMEDIATE;
                CREATE TABLE papers (
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
                    pdf_url TEXT,
                    content_urls_json TEXT NOT NULL DEFAULT '{{}}',
                    pmcid TEXT,
                    pmid TEXT,
                    license_code TEXT,
                    fulltext_availability TEXT NOT NULL DEFAULT 'unknown'
                        CHECK(fulltext_availability IN ('available', 'unavailable', 'unknown')),
                    created_at TEXT NOT NULL,
                    refreshed_at TEXT NOT NULL,
                    UNIQUE(source, source_key)
                );
                CREATE TABLE aliases (
                    scheme TEXT NOT NULL,
                    normalized_value TEXT NOT NULL,
                    paper_id TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(scheme, normalized_value)
                );
                CREATE TABLE files (
                    id TEXT PRIMARY KEY,
                    sha256 TEXT NOT NULL UNIQUE,
                    media_type TEXT NOT NULL,
                    byte_count INTEGER NOT NULL,
                    relative_path TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE paper_files (
                    paper_id TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
                    file_id TEXT NOT NULL REFERENCES files(id),
                    format TEXT NOT NULL CHECK(format IN ('pdf', 'txt', 'xml')),
                    source_version TEXT,
                    retrieved_at TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    PRIMARY KEY(paper_id, file_id)
                );
                CREATE INDEX papers_created_order ON papers(created_at DESC, id DESC);
                CREATE INDEX paper_files_format ON paper_files(paper_id, format);
                PRAGMA user_version = {DATABASE_SCHEMA_VERSION};
                COMMIT;
                """
            )
        except sqlite3.Error as exc:
            if self.connection.in_transaction:
                self.connection.rollback()
            raise PapersError(
                "storage_unavailable", "Unable to initialize the local collection", exit_code=5
            ) from exc

    def _migrate_v1_to_v2(self) -> None:
        """Upgrade in one transaction while retaining legacy attachments and identities."""
        try:
            self.connection.execute("PRAGMA foreign_keys = OFF")
            self.connection.execute("BEGIN IMMEDIATE")
            self.connection.execute(
                """
                CREATE TABLE papers_v2 (
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
                    pdf_url TEXT,
                    content_urls_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    refreshed_at TEXT NOT NULL,
                    UNIQUE(source, source_key)
                )
                """
            )
            self.connection.execute(
                """
                INSERT INTO papers_v2 (
                    id, source, source_key, source_version, title, abstract,
                    authors_json, categories_json, published_at, updated_at, doi,
                    landing_url, pdf_url, content_urls_json, created_at, refreshed_at
                )
                SELECT id, source, source_key, source_version, title, abstract,
                    authors_json, categories_json, published_at, updated_at, doi,
                    landing_url, pdf_url, '{}', created_at, refreshed_at
                FROM papers
                """
            )
            self.connection.execute(
                """
                CREATE TABLE paper_files_v2 (
                    paper_id TEXT NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
                    file_id TEXT NOT NULL REFERENCES files(id),
                    format TEXT NOT NULL CHECK(format IN ('pdf', 'txt', 'xml')),
                    source_version TEXT,
                    retrieved_at TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    PRIMARY KEY(paper_id, file_id)
                )
                """
            )
            self.connection.execute(
                """
                INSERT INTO paper_files_v2 (
                    paper_id, file_id, format, source_version, retrieved_at, source_url, provider
                )
                SELECT pf.paper_id, pf.file_id, pf.role, pf.source_version, pf.retrieved_at,
                       pf.source_url, p.source
                FROM paper_files pf
                JOIN papers p ON p.id = pf.paper_id
                """
            )
            self.connection.execute("DROP TABLE paper_files")
            self.connection.execute("DROP TABLE papers")
            self.connection.execute("ALTER TABLE papers_v2 RENAME TO papers")
            self.connection.execute("ALTER TABLE paper_files_v2 RENAME TO paper_files")
            self.connection.execute(
                "CREATE INDEX papers_created_order ON papers(created_at DESC, id DESC)"
            )
            self.connection.execute(
                "CREATE INDEX paper_files_format ON paper_files(paper_id, format)"
            )
            self.connection.execute(f"PRAGMA user_version = {MULTIFORMAT_DATABASE_SCHEMA_VERSION}")
            self.connection.commit()
        except sqlite3.Error as exc:
            if self.connection.in_transaction:
                self.connection.rollback()
            raise PapersError(
                "storage_unavailable", "Unable to migrate the local collection", exit_code=5
            ) from exc
        finally:
            self.connection.execute("PRAGMA foreign_keys = ON")

    def _migrate_v2_to_v3(self) -> None:
        """Add optional provider identifiers without changing stored paper identities."""
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            self.connection.execute("ALTER TABLE papers ADD COLUMN pmcid TEXT")
            self.connection.execute("ALTER TABLE papers ADD COLUMN pmid TEXT")
            self.connection.execute("ALTER TABLE papers ADD COLUMN license_code TEXT")
            self.connection.execute(
                "ALTER TABLE papers ADD COLUMN fulltext_availability TEXT NOT NULL "
                "DEFAULT 'unknown' "
                "CHECK(fulltext_availability IN ('available', 'unavailable', 'unknown'))"
            )
            self.connection.execute(f"PRAGMA user_version = {DATABASE_SCHEMA_VERSION}")
            self.connection.commit()
        except sqlite3.Error as exc:
            if self.connection.in_transaction:
                self.connection.rollback()
            raise PapersError(
                "storage_unavailable", "Unable to migrate the local collection", exit_code=5
            ) from exc

    @staticmethod
    def _aliases(paper: RemotePaper) -> list[tuple[str, str]]:
        aliases = [(paper.source, paper.source_key.lower())]
        if paper.doi:
            aliases.append(("doi", paper.doi.lower()))
        if paper.pmid:
            aliases.append(("pmid", paper.pmid.lower()))
        return aliases

    @staticmethod
    def _content_urls(paper: RemotePaper) -> dict[str, str]:
        urls = dict(paper.content_urls)
        if paper.pdf_url is not None:
            urls.setdefault("pdf", paper.pdf_url)
        if any(format not in CONTENT_FORMATS or not url for format, url in urls.items()):
            raise PapersError("storage_corrupt", "Paper full-text URLs are invalid", exit_code=5)
        return urls

    def upsert_paper(self, paper: RemotePaper) -> str:
        now = _now()
        candidate_id = uuid7()
        content_urls = self._content_urls(paper)
        if paper.fulltext_availability not in {"available", "unavailable", "unknown"}:
            raise PapersError(
                "storage_corrupt", "Paper full-text availability is invalid", exit_code=5
            )
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO papers (
                    id, source, source_key, source_version, title, abstract,
                    authors_json, categories_json, published_at, updated_at, doi,
                    landing_url, pdf_url, content_urls_json, pmcid, pmid, license_code,
                    fulltext_availability, created_at, refreshed_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                  content_urls_json=excluded.content_urls_json,
                  pmcid=excluded.pmcid,
                  pmid=excluded.pmid,
                  license_code=excluded.license_code,
                  fulltext_availability=excluded.fulltext_availability,
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
                    json.dumps(content_urls, sort_keys=True),
                    paper.pmcid,
                    paper.pmid,
                    paper.license_code,
                    paper.fulltext_availability,
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
        if file.format not in CONTENT_FORMATS or file.media_type != content_media_type(file.format):
            raise PapersError("storage_corrupt", "Downloaded file metadata is invalid", exit_code=5)
        now = _now()
        candidate_id = uuid7()
        with self.connection:
            provider = file.provider
            if not provider:
                paper_row = self.connection.execute(
                    "SELECT source FROM papers WHERE id = ?", (paper_id,)
                ).fetchone()
                if paper_row is None:
                    raise PapersError(
                        "storage_corrupt", "Cannot attach a file to a missing paper", exit_code=5
                    )
                provider = str(paper_row["source"])
            self.connection.execute(
                """INSERT OR IGNORE INTO files
                (id, sha256, media_type, byte_count, relative_path, created_at)
                VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    candidate_id,
                    file.sha256,
                    file.media_type,
                    file.byte_count,
                    file.relative_path,
                    now,
                ),
            )
            row = self.connection.execute(
                "SELECT id, media_type, relative_path FROM files WHERE sha256 = ?", (file.sha256,)
            ).fetchone()
            if row is None:
                raise PapersError(
                    "storage_corrupt", "File upsert did not persist a record", exit_code=5
                )
            if row["media_type"] != file.media_type or row["relative_path"] != file.relative_path:
                raise PapersError(
                    "storage_corrupt",
                    "A digest is already stored with incompatible metadata",
                    exit_code=5,
                )
            file_id = str(row["id"])
            self.connection.execute(
                "DELETE FROM paper_files WHERE paper_id = ? AND format = ?",
                (paper_id, file.format),
            )
            self.connection.execute(
                """INSERT INTO paper_files
                (paper_id, file_id, format, source_version, retrieved_at, source_url, provider)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (paper_id, file_id, file.format, source_version, now, file.source_url, provider),
            )

    @staticmethod
    def _parse_content_urls(value: object, pdf_url: object) -> dict[str, str]:
        try:
            urls = json.loads(str(value))
        except (TypeError, ValueError) as exc:
            raise PapersError(
                "storage_corrupt", "Stored full-text URLs are invalid", exit_code=5
            ) from exc
        if not isinstance(urls, dict) or any(
            format not in CONTENT_FORMATS or not isinstance(url, str) or not url
            for format, url in urls.items()
        ):
            raise PapersError("storage_corrupt", "Stored full-text URLs are invalid", exit_code=5)
        result = {str(format): str(url) for format, url in urls.items()}
        if isinstance(pdf_url, str) and pdf_url:
            result.setdefault("pdf", pdf_url)
        return result

    def _file_records(self, paper_id: str, source: str) -> list[dict[str, object]]:
        if self.schema_version == LEGACY_DATABASE_SCHEMA_VERSION:
            query = """
                SELECT f.sha256, f.media_type, f.byte_count, f.relative_path,
                       'pdf' AS format, pf.source_version, pf.retrieved_at, pf.source_url
                FROM paper_files pf
                JOIN files f ON f.id = pf.file_id
                WHERE pf.paper_id = ? AND pf.role = 'pdf'
                ORDER BY pf.retrieved_at DESC, f.id DESC
            """
        else:
            query = """
                SELECT f.sha256, f.media_type, f.byte_count, f.relative_path,
                       pf.format, pf.source_version, pf.retrieved_at, pf.source_url, pf.provider
                FROM paper_files pf
                JOIN files f ON f.id = pf.file_id
                WHERE pf.paper_id = ?
                ORDER BY CASE pf.format WHEN 'pdf' THEN 0 WHEN 'txt' THEN 1 ELSE 2 END,
                         pf.retrieved_at DESC, f.id DESC
            """
        rows = self.connection.execute(query, (paper_id,)).fetchall()
        records: list[dict[str, object]] = []
        for row in rows:
            format = str(row["format"])
            media_type = str(row["media_type"])
            if format not in CONTENT_FORMATS or media_type != content_media_type(format):
                raise PapersError("storage_corrupt", "Stored file metadata is invalid", exit_code=5)
            records.append(
                {
                    "format": format,
                    "media_type": media_type,
                    "sha256": str(row["sha256"]),
                    "byte_count": int(row["byte_count"]),
                    "relative_path": str(row["relative_path"]),
                    "source": str(row["provider"]) if "provider" in row.keys() else source,
                    "source_url": str(row["source_url"]),
                    "source_version": row["source_version"],
                    "retrieved_at": str(row["retrieved_at"]),
                }
            )
        return records

    def _row_to_dict(self, row: sqlite3.Row) -> dict[str, object]:
        content_urls = self._parse_content_urls(
            row["content_urls_json"] if "content_urls_json" in row.keys() else "{}",
            row["pdf_url"],
        )
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
            "pmcid": row["pmcid"] if "pmcid" in row.keys() else None,
            "pmid": row["pmid"] if "pmid" in row.keys() else None,
            "license_code": row["license_code"] if "license_code" in row.keys() else None,
            "fulltext_availability": (
                row["fulltext_availability"] if "fulltext_availability" in row.keys() else "unknown"
            ),
            "landing_url": row["landing_url"],
            "pdf_url": row["pdf_url"],
            "content_urls": content_urls,
            "created_at": row["created_at"],
            "refreshed_at": row["refreshed_at"],
        }
        files = self._file_records(str(row["id"]), str(row["source"]))
        result["files"] = files
        pdf = next((file for file in files if file["format"] == "pdf"), None)
        if pdf is not None:
            result["file"] = {
                "sha256": pdf["sha256"],
                "byte_count": pdf["byte_count"],
                "relative_path": pdf["relative_path"],
                "source_url": pdf["source_url"],
            }
        return result

    @staticmethod
    def _select() -> str:
        return "SELECT p.* FROM papers p"

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
                normalized_scheme = scheme.lower()
                normalized_value = value.lower()
                if normalized_scheme == "doi":
                    rows = self.connection.execute(
                        self._select() + " WHERE lower(p.doi) = ? OR EXISTS ("
                        "SELECT 1 FROM aliases a "
                        "WHERE a.paper_id = p.id AND a.scheme = 'doi' "
                        "AND a.normalized_value = ?"
                        ") ORDER BY p.source, p.source_key",
                        (normalized_value, normalized_value),
                    ).fetchall()
                    if len(rows) > 1:
                        refs = [f"{item['source']}:{item['source_key']}" for item in rows]
                        raise PapersError(
                            "ambiguous_ref",
                            f"DOI matches multiple local papers: {', '.join(refs)}",
                            exit_code=3,
                            details={"ref": f"doi:{normalized_value}", "refs": refs},
                        ) from None
                    row = rows[0] if rows else None
                else:
                    row = self.connection.execute(
                        self._select()
                        + " JOIN aliases a ON a.paper_id = p.id"
                        + " WHERE a.scheme = ? AND a.normalized_value = ?",
                        (normalized_scheme, normalized_value),
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

    def removal_plan(self, ref: str) -> dict[str, object]:
        paper = self.get(ref)
        paper_id = str(paper["id"])
        rows = self.connection.execute(
            """
            SELECT f.id, f.sha256, f.media_type, f.byte_count, f.relative_path, target.format,
                   (SELECT COUNT(*) FROM paper_files all_links WHERE all_links.file_id = f.id)
                       AS reference_count
            FROM paper_files target
            JOIN files f ON f.id = target.file_id
            WHERE target.paper_id = ?
            ORDER BY f.id
            """,
            (paper_id,),
        ).fetchall()
        files = [
            {
                "id": str(row["id"]),
                "sha256": str(row["sha256"]),
                "media_type": str(row["media_type"]),
                "format": str(row["format"]),
                "byte_count": int(row["byte_count"]),
                "relative_path": str(row["relative_path"]),
                "reference_count": int(row["reference_count"]),
            }
            for row in rows
        ]
        return {
            "paper": {"id": paper_id, "ref": paper["ref"], "title": paper["title"]},
            "files": files,
        }

    def remove_paper(
        self,
        ref: str,
        validate: Callable[[dict[str, object]], None],
    ) -> dict[str, object]:
        """Remove a paper and unreferenced file rows in one write transaction."""
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            plan = self.removal_plan(ref)
            validate(plan)
            paper = plan["paper"]
            files = plan["files"]
            if not isinstance(paper, dict) or not isinstance(files, list):
                raise PapersError("storage_corrupt", "Removal plan is invalid", exit_code=5)
            self.connection.execute("DELETE FROM papers WHERE id = ?", (paper["id"],))
            for file in files:
                if not isinstance(file, dict):
                    raise PapersError("storage_corrupt", "Removal plan is invalid", exit_code=5)
                cursor = self.connection.execute(
                    """
                    DELETE FROM files
                    WHERE id = ?
                      AND NOT EXISTS (
                          SELECT 1 FROM paper_files WHERE file_id = files.id
                      )
                    """,
                    (file["id"],),
                )
                file["catalog_action"] = (
                    "delete_object" if cursor.rowcount == 1 else "retain_shared"
                )
            self.connection.commit()
            return plan
        except Exception:
            self.connection.rollback()
            raise

    def remove_object_if_unreferenced(
        self, sha256: str, remove: Callable[[], str]
    ) -> tuple[bool, str | None]:
        """Run an object unlink callback while preventing new file references."""
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute(
                "SELECT 1 FROM files WHERE sha256 = ?", (sha256,)
            ).fetchone()
            if row is not None:
                self.connection.rollback()
                return False, None
            disposition = remove()
            self.connection.rollback()
            return True, disposition
        except Exception:
            self.connection.rollback()
            raise
