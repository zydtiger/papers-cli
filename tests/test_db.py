from __future__ import annotations

import uuid

from papers_cli.db import Database
from papers_cli.models import DownloadedFile, RemotePaper


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
