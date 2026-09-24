from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import subprocess
from dataclasses import replace
from io import BytesIO
from pathlib import Path

import httpx
import pytest
from pypdf import PdfWriter

from papers_cli import cli
from papers_cli.db import LIST_PAPER_FIELDS, Database
from papers_cli.errors import PapersError
from papers_cli.models import (
    REMOTE_PAPER_FIELDS,
    DownloadedFile,
    RemotePaper,
    content_media_type,
    content_suffix,
)

FIXTURES = Path(__file__).parent / "fixtures"


def pdf_bytes(*, width: int = 72) -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=width, height=72)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def cyclic_page_tree_pdf_bytes() -> bytes:
    objects = (
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [2 0 R] /Count 1 >>",
    )
    output = BytesIO()
    output.write(b"%PDF-1.7\n")
    offsets = [0]
    for identifier, object_value in enumerate(objects, start=1):
        offsets.append(output.tell())
        output.write(f"{identifier} 0 obj\n".encode())
        output.write(object_value)
        output.write(b"\nendobj\n")
    xref_offset = output.tell()
    output.write(f"xref\n0 {len(objects) + 1}\n".encode())
    output.write(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.write(f"{offset:010d} 00000 n \n".encode())
    output.write(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_offset}\n%%EOF\n".encode()
    )
    return output.getvalue()


def local_paper(source_key: str = "2301.00001") -> RemotePaper:
    return RemotePaper(
        source="arxiv",
        source_key=source_key,
        source_version="2",
        title="Fixture",
        abstract="",
        authors=[],
        categories=[],
        published_at=None,
        updated_at=None,
        doi=None,
        landing_url=f"https://arxiv.org/abs/{source_key}v2",
        pdf_url=f"https://arxiv.org/pdf/{source_key}v2",
    )


def seed_database(data_dir: Path, paper: RemotePaper) -> None:
    data_dir.mkdir()
    database = Database(data_dir / "papers.sqlite3")
    database.upsert_paper(paper)
    database.close()


def seed_downloaded_paper(
    data_dir: Path,
    paper: RemotePaper,
    *,
    sha256: str = "a" * 64,
    body: bytes | None = None,
) -> tuple[str, Path]:
    if body is None:
        body = pdf_bytes()
    data_dir.mkdir(exist_ok=True)
    relative = Path("objects") / "sha256" / sha256[:2] / sha256[2:4] / f"{sha256}.pdf"
    object_path = data_dir / relative
    object_path.parent.mkdir(parents=True, exist_ok=True)
    object_path.write_bytes(body)
    database = Database(data_dir / "papers.sqlite3")
    paper_id = database.upsert_paper(paper)
    if paper.pdf_url is None:
        raise ValueError("test helper requires a PDF URL")
    database.attach_file(
        paper_id,
        DownloadedFile(sha256, len(body), relative.as_posix(), paper.pdf_url),
        paper.source_version,
    )
    database.close()
    return paper_id, object_path


def seed_verified_paper(
    data_dir: Path, source_key: str, *, body: bytes | None = None
) -> tuple[str, Path]:
    if body is None:
        body = pdf_bytes()
    return seed_downloaded_paper(
        data_dir,
        local_paper(source_key),
        sha256=hashlib.sha256(body).hexdigest(),
        body=body,
    )


def attach_downloaded_format(
    data_dir: Path,
    paper_id: str,
    *,
    format: str,
    body: bytes,
    source: str = "pmc",
    source_version: str = "1",
) -> tuple[DownloadedFile, Path]:
    digest = hashlib.sha256(body).hexdigest()
    relative = (
        Path("objects") / "sha256" / digest[:2] / digest[2:4] / f"{digest}{content_suffix(format)}"
    )
    path = data_dir / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    downloaded = DownloadedFile(
        digest,
        len(body),
        relative.as_posix(),
        f"https://files.example/{paper_id}.{format}",
        format=format,
        media_type=content_media_type(format),
        provider=source,
    )
    database = Database(data_dir / "papers.sqlite3")
    database.attach_file(paper_id, downloaded, source_version)
    database.close()
    return downloaded, path


def read_jsonl(capsys: pytest.CaptureFixture[str]) -> list[dict[str, object]]:
    captured = capsys.readouterr()
    assert captured.err == ""
    envelopes: list[dict[str, object]] = []
    for line in captured.out.splitlines():
        envelope = json.loads(line)
        assert isinstance(envelope, dict)
        envelopes.append(envelope)
    return envelopes


def data_records(envelopes: list[dict[str, object]]) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for envelope in envelopes:
        assert envelope["schema_version"] == 1
        assert envelope["ok"] is True
        record = envelope["data"]
        assert isinstance(record, dict)
        records.append(record)
    return records


def read_jsonl_records(capsys: pytest.CaptureFixture[str]) -> list[dict[str, object]]:
    return data_records(read_jsonl(capsys))


def read_error(capsys: pytest.CaptureFixture[str]) -> dict[str, object]:
    captured = capsys.readouterr()
    assert captured.err == ""
    lines = captured.out.splitlines()
    assert len(lines) == 1
    document = json.loads(lines[0])
    assert isinstance(document, dict)
    assert document["schema_version"] == 1
    assert document["ok"] is False
    error = document["error"]
    assert isinstance(error, dict)
    return error


def mock_transport(monkeypatch, handler) -> None:
    real_client = httpx.Client

    def mock_client(**kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(**kwargs)

    monkeypatch.setattr(cli.httpx, "Client", mock_client)


def isolated_dirs(monkeypatch, tmp_path) -> tuple[Path, Path]:
    data_dir = tmp_path / "data"
    cache_dir = tmp_path / "cache"
    monkeypatch.setenv("PAPERS_CLI_DATA_DIR", str(data_dir))
    monkeypatch.setenv("PAPERS_CLI_CACHE_DIR", str(cache_dir))
    return data_dir, cache_dir


def feed_with_entries(*source_keys: bytes) -> bytes:
    fixture = (FIXTURES / "arxiv.xml").read_bytes()
    prefix, _, rest = fixture.partition(b"<entry>")
    entry, _, _ = rest.partition(b"</entry>")
    entries = b"".join(
        b"<entry>" + entry.replace(b"2301.00001", key) + b"</entry>" for key in source_keys
    )
    return prefix + entries + b"</feed>\n"


def test_download_then_verify_has_stable_jsonl(monkeypatch, tmp_path, capsys) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "export.arxiv.org":
            return httpx.Response(200, content=(FIXTURES / "arxiv.xml").read_bytes())
        if request.url.host == "arxiv.org":
            return httpx.Response(
                200, headers={"content-type": "application/pdf"}, content=pdf_bytes()
            )
        return httpx.Response(500)

    mock_transport(monkeypatch, handler)
    isolated_dirs(monkeypatch, tmp_path)

    assert cli.main(["download", "arxiv:2301.00001", "--jsonl"]) == 0
    downloaded = read_jsonl_records(capsys)
    assert len(downloaded) == 1
    paper_id = downloaded[0]["id"]
    assert isinstance(paper_id, str)

    assert cli.main(["verify", paper_id, "--jsonl"]) == 0
    verified = read_jsonl_records(capsys)
    assert len(verified) == 1
    assert verified[0]["status"] == "verified"


def test_download_staging_failure_has_stable_jsonl(monkeypatch, tmp_path, capsys) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "export.arxiv.org":
            return httpx.Response(200, content=(FIXTURES / "arxiv.xml").read_bytes())
        if request.url.host == "arxiv.org":
            return httpx.Response(
                200, headers={"content-type": "application/pdf"}, content=pdf_bytes()
            )
        return httpx.Response(500)

    mock_transport(monkeypatch, handler)

    def fail_mkstemp(**_: object) -> tuple[int, str]:
        raise PermissionError("Permission denied")

    monkeypatch.setattr("papers_cli.downloader.tempfile.mkstemp", fail_mkstemp)
    isolated_dirs(monkeypatch, tmp_path)

    assert cli.main(["download", "arxiv:2301.00001", "--jsonl"]) == 5
    assert read_error(capsys)["code"] == "storage_staging"


def test_download_rejects_malformed_pdf_without_persisting_attachment(
    monkeypatch, tmp_path, capsys
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "export.arxiv.org":
            return httpx.Response(200, content=(FIXTURES / "arxiv.xml").read_bytes())
        if request.url.host == "arxiv.org":
            return httpx.Response(
                200,
                headers={"content-type": "application/pdf"},
                content=b"%PDF-1.7\nfixture",
            )
        return httpx.Response(500)

    mock_transport(monkeypatch, handler)
    data_dir, cache_dir = isolated_dirs(monkeypatch, tmp_path)

    assert cli.main(["download", "arxiv:2301.00001", "--jsonl"]) == 4
    assert read_error(capsys)["code"] == "not_pdf"
    database = Database(data_dir / "papers.sqlite3", read_only=True)
    assert database.list(None, None) == []
    database.close()
    assert not list(cache_dir.glob("downloads/download-*.part"))
    assert not list((data_dir / "objects").rglob("*.pdf"))


def test_download_rejects_cyclic_pdf_page_tree_without_stderr(
    monkeypatch, tmp_path, capsys
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "export.arxiv.org":
            return httpx.Response(200, content=(FIXTURES / "arxiv.xml").read_bytes())
        if request.url.host == "arxiv.org":
            return httpx.Response(
                200,
                headers={"content-type": "application/pdf"},
                content=cyclic_page_tree_pdf_bytes(),
            )
        return httpx.Response(500)

    mock_transport(monkeypatch, handler)
    data_dir, cache_dir = isolated_dirs(monkeypatch, tmp_path)

    assert cli.main(["download", "arxiv:2301.00001", "--jsonl"]) == 4
    assert read_error(capsys)["code"] == "not_pdf"
    database = Database(data_dir / "papers.sqlite3", read_only=True)
    assert database.list(None, None) == []
    database.close()
    assert not list(cache_dir.glob("downloads/download-*.part"))


def test_download_silences_repairable_pdf_parser_diagnostics(monkeypatch, tmp_path, capsys) -> None:
    repaired_pdf = pdf_bytes().replace(b"startxref\n", b"startxref\n0\n%\n")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "export.arxiv.org":
            return httpx.Response(200, content=(FIXTURES / "arxiv.xml").read_bytes())
        if request.url.host == "arxiv.org":
            return httpx.Response(
                200, headers={"content-type": "application/pdf"}, content=repaired_pdf
            )
        return httpx.Response(500)

    mock_transport(monkeypatch, handler)
    isolated_dirs(monkeypatch, tmp_path)

    assert cli.main(["download", "arxiv:2301.00001", "--jsonl"]) == 0
    assert len(read_jsonl_records(capsys)) == 1


@pytest.mark.parametrize("blocked_override", ["PAPERS_CLI_DATA_DIR", "PAPERS_CLI_CACHE_DIR"])
def test_download_path_initialization_failure_has_stable_jsonl(
    monkeypatch, tmp_path, capsys, blocked_override
) -> None:
    blocked_path = tmp_path / "blocked"
    blocked_path.write_text("not a directory")
    monkeypatch.setenv(blocked_override, str(blocked_path))
    other_override = (
        "PAPERS_CLI_CACHE_DIR"
        if blocked_override == "PAPERS_CLI_DATA_DIR"
        else "PAPERS_CLI_DATA_DIR"
    )
    monkeypatch.setenv(other_override, str(tmp_path / "other"))

    assert cli.main(["download", "arxiv:2301.00001", "--jsonl"]) == 5
    captured = capsys.readouterr()
    assert captured.err == ""
    lines = captured.out.splitlines()
    assert len(lines) == 1
    output = json.loads(lines[0])
    assert isinstance(output, dict)
    assert output["schema_version"] == 1
    assert output["ok"] is False
    assert output["error"]["code"] == "storage_initialize"


def test_dry_run_does_not_create_collection_state(monkeypatch, tmp_path, capsys) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "export.arxiv.org"
        return httpx.Response(200, content=(FIXTURES / "arxiv.xml").read_bytes())

    mock_transport(monkeypatch, handler)
    data_dir, cache_dir = isolated_dirs(monkeypatch, tmp_path)

    assert cli.main(["download", "arxiv:2301.00001", "--dry-run", "--jsonl"]) == 0
    records = read_jsonl_records(capsys)
    assert len(records) == 1
    assert records[0]["dry_run"] is True
    assert not data_dir.exists()
    assert not cache_dir.exists()


def test_pmc_downloads_each_official_fulltext_format(monkeypatch, tmp_path, capsys) -> None:
    requested_files: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "pmc.ncbi.nlm.nih.gov":
            return httpx.Response(200, content=(FIXTURES / "pmc-idconv-current.json").read_bytes())
        if request.url.path == "/metadata/PMC3531190.1.json":
            return httpx.Response(
                200, content=(FIXTURES / "pmc-metadata-all-formats.json").read_bytes()
            )
        requested_files.append(request.url.path)
        if request.url.path.endswith(".pdf"):
            return httpx.Response(
                200, headers={"content-type": "binary/octet-stream"}, content=pdf_bytes()
            )
        if request.url.path.endswith(".txt"):
            return httpx.Response(
                200, headers={"content-type": "binary/octet-stream"}, content=b"article text"
            )
        if request.url.path.endswith(".xml"):
            return httpx.Response(
                200,
                headers={"content-type": "binary/octet-stream"},
                content=b"<article><body>article body</body></article>",
            )
        pytest.fail(f"unexpected request {request.url}")

    mock_transport(monkeypatch, handler)
    data_dir, _ = isolated_dirs(monkeypatch, tmp_path)
    for format in ("pdf", "txt", "xml"):
        assert cli.main(["download", "PMC3531190", "--format", format, "--jsonl"]) == 0
        assert read_jsonl_records(capsys)[0]["source"] == "pmc"

    database = Database(data_dir / "papers.sqlite3", read_only=True)
    record = database.get("pmc:PMC3531190")
    database.close()
    files = record["files"]
    assert isinstance(files, list)
    assert [file["format"] for file in files if isinstance(file, dict)] == ["pdf", "txt", "xml"]
    assert requested_files == [
        "/PMC3531190.1/PMC3531190.1.pdf",
        "/PMC3531190.1/PMC3531190.1.txt",
        "/PMC3531190.1/PMC3531190.1.xml",
    ]


def test_pmc_dry_run_selects_cloud_url_without_writing(monkeypatch, tmp_path, capsys) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "pmc.ncbi.nlm.nih.gov":
            return httpx.Response(200, content=(FIXTURES / "pmc-idconv-current.json").read_bytes())
        assert request.url.path == "/metadata/PMC3531190.1.json"
        return httpx.Response(
            200, content=(FIXTURES / "pmc-metadata-all-formats.json").read_bytes()
        )

    mock_transport(monkeypatch, handler)
    data_dir, cache_dir = isolated_dirs(monkeypatch, tmp_path)

    assert cli.main(["download", "pmc:PMC3531190", "--format", "xml", "--dry-run", "--jsonl"]) == 0
    record = read_jsonl_records(capsys)[0]

    assert record["source_url"] == (
        "https://pmc-oa-opendata.s3.amazonaws.com/PMC3531190.1/PMC3531190.1.xml"
    )
    assert [request.url.path for request in requests] == [
        "/tools/idconv/api/v1/articles/",
        "/metadata/PMC3531190.1.json",
    ]
    assert not data_dir.exists()
    assert not cache_dir.exists()


def test_crossref_download_persists_pmc_file_provenance(monkeypatch, tmp_path, capsys) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.crossref.org":
            return httpx.Response(
                200, content=(FIXTURES / "crossref-work-mapped.json").read_bytes()
            )
        if request.url.host == "pmc.ncbi.nlm.nih.gov":
            return httpx.Response(200, content=(FIXTURES / "pmc-idconv-current.json").read_bytes())
        if request.url.path == "/metadata/PMC3531190.1.json":
            return httpx.Response(
                200, content=(FIXTURES / "pmc-metadata-all-formats.json").read_bytes()
            )
        assert request.url.path.endswith(".txt")
        return httpx.Response(
            200, headers={"content-type": "binary/octet-stream"}, content=b"article text"
        )

    mock_transport(monkeypatch, handler)
    data_dir, _ = isolated_dirs(monkeypatch, tmp_path)

    assert cli.main(["download", "10.1093/nar/gks1195", "--format", "txt", "--jsonl"]) == 0
    downloaded = read_jsonl_records(capsys)[0]
    assert downloaded["ref"] == "crossref:10.1093/nar/gks1195"
    assert downloaded["source"] == "crossref"

    database = Database(data_dir / "papers.sqlite3", read_only=True)
    record = database.get("crossref:10.1093/nar/gks1195")
    database.close()
    files = record["files"]
    assert isinstance(files, list)
    assert len(files) == 1
    assert files[0]["source"] == "pmc"
    assert files[0]["source_version"] == "1"
    assert str(files[0]["source_url"]).endswith("/PMC3531190.1/PMC3531190.1.txt")


def test_pubmed_search_uses_batched_metadata_and_emits_reusable_refs(
    monkeypatch, tmp_path, capsys
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/esearch.fcgi"):
            assert request.url.params["term"] == "GenBank"
            assert request.url.params["retmax"] == "2"
            return httpx.Response(200, content=(FIXTURES / "pubmed-esearch.json").read_bytes())
        assert request.url.path.endswith("/esummary.fcgi")
        assert request.url.params["id"] == "23193287,31567725"
        return httpx.Response(200, content=(FIXTURES / "pubmed-esummary.json").read_bytes())

    mock_transport(monkeypatch, handler)
    data_dir, cache_dir = isolated_dirs(monkeypatch, tmp_path)

    assert (
        cli.main(["search", "--source", "pubmed", "--query", "GenBank", "--limit", "2", "--jsonl"])
        == 0
    )
    records = read_jsonl_records(capsys)
    assert [record["ref"] for record in records] == ["pubmed:23193287", "pubmed:31567725"]
    assert all(record["fulltext_availability"] == "unknown" for record in records)
    assert not data_dir.exists()
    assert not cache_dir.exists()


def test_pubmed_download_persists_pmc_file_provenance(monkeypatch, tmp_path, capsys) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/esummary.fcgi"):
            return httpx.Response(200, content=(FIXTURES / "pubmed-esummary.json").read_bytes())
        if request.url.host == "pmc.ncbi.nlm.nih.gov":
            return httpx.Response(
                200, content=(FIXTURES / "pmc-idconv-pmid-current.json").read_bytes()
            )
        if request.url.path == "/metadata/PMC3531190.1.json":
            return httpx.Response(
                200, content=(FIXTURES / "pmc-metadata-all-formats.json").read_bytes()
            )
        assert request.url.path.endswith(".pdf")
        return httpx.Response(
            200, headers={"content-type": "binary/octet-stream"}, content=pdf_bytes()
        )

    mock_transport(monkeypatch, handler)
    data_dir, _ = isolated_dirs(monkeypatch, tmp_path)

    assert cli.main(["download", "pmid:23193287", "--jsonl"]) == 0
    downloaded = read_jsonl_records(capsys)[0]
    assert downloaded["ref"] == "pubmed:23193287"
    assert downloaded["source"] == "pubmed"

    database = Database(data_dir / "papers.sqlite3", read_only=True)
    record = database.get("pubmed:23193287")
    database.close()
    files = record["files"]
    assert isinstance(files, list)
    assert len(files) == 1
    file = files[0]
    assert isinstance(file, dict)
    assert file["source"] == "pmc"
    assert file["source_version"] == "1"
    assert str(file["source_url"]).endswith("/PMC3531190.1/PMC3531190.1.pdf")


def test_download_format_is_strict_and_never_falls_back_to_pdf(
    monkeypatch, tmp_path, capsys
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "export.arxiv.org":
            return httpx.Response(200, content=(FIXTURES / "arxiv.xml").read_bytes())
        pytest.fail("an unavailable format must not request the PDF URL")

    mock_transport(monkeypatch, handler)
    isolated_dirs(monkeypatch, tmp_path)

    assert cli.main(["download", "arxiv:2301.00001", "--format", "txt", "--jsonl"]) == 3
    error = read_error(capsys)
    assert error["code"] == "format_unavailable"
    assert error["details"] == {
        "availability": "known",
        "available_formats": ["pdf"],
        "ref": "arxiv:2301.00001",
        "requested_format": "txt",
    }


def test_download_rejects_unknown_format_before_network_or_collection_access(
    monkeypatch, tmp_path, capsys
) -> None:
    no_network_client(monkeypatch)
    data_dir, _ = isolated_dirs(monkeypatch, tmp_path)

    assert cli.main(["download", "arxiv:2301.00001", "--format", "html", "--jsonl"]) == 2
    assert read_error(capsys)["code"] == "usage"
    assert not data_dir.exists()


def test_parse_errors_use_jsonl_contract_when_requested(capsys) -> None:
    assert cli.main(["search", "--jsonl"]) == 2
    assert read_error(capsys)["code"] == "usage"


def test_remote_lookup_does_not_create_collection_state(monkeypatch, tmp_path, capsys) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "export.arxiv.org"
        return httpx.Response(200, content=(FIXTURES / "arxiv.xml").read_bytes())

    mock_transport(monkeypatch, handler)
    data_dir, cache_dir = isolated_dirs(monkeypatch, tmp_path)

    assert cli.main(["lookup", "arxiv:2301.00001", "--jsonl"]) == 0
    records = read_jsonl_records(capsys)
    assert len(records) == 1
    assert records[0]["ref"] == "arxiv:2301.00001"
    assert not data_dir.exists()
    assert not cache_dir.exists()


def test_lookup_resolves_stored_doi_alias_without_remote_doi_request(
    monkeypatch, tmp_path, capsys
) -> None:
    data_dir, _ = isolated_dirs(monkeypatch, tmp_path)
    seed_database(data_dir, replace(local_paper(), doi="10.1000/Test"))
    mock_transport(
        monkeypatch,
        lambda _: pytest.fail("a stored DOI alias must not make a provider request"),
    )

    assert cli.main(["lookup", "doi:10.1000/test", "--jsonl"]) == 0
    records = read_jsonl_records(capsys)
    assert [record["ref"] for record in records] == ["arxiv:2301.00001"]


def test_local_pmid_reference_prefers_stored_pubmed_record_without_network(
    monkeypatch, tmp_path, capsys
) -> None:
    data_dir, _ = isolated_dirs(monkeypatch, tmp_path)
    data_dir.mkdir()
    database = Database(data_dir / "papers.sqlite3")
    database.upsert_paper(
        replace(
            local_paper("PMC3531190"),
            source="pmc",
            source_version="1",
            pmid="23193287",
        )
    )
    database.upsert_paper(
        replace(
            local_paper("23193287"),
            source="pubmed",
            source_version=None,
            pmid="23193287",
            landing_url="https://pubmed.ncbi.nlm.nih.gov/23193287/",
        )
    )
    database.close()
    mock_transport(
        monkeypatch, lambda _: pytest.fail("stored PubMed references must not request a provider")
    )

    assert cli.main(["lookup", "pubmed:23193287", "pmid:23193287", "--jsonl"]) == 0
    records = read_jsonl_records(capsys)
    assert [record["ref"] for record in records] == ["pubmed:23193287", "pubmed:23193287"]


def test_unresolved_doi_uses_crossref_without_creating_collection_state(
    monkeypatch, tmp_path, capsys
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.crossref.org":
            return httpx.Response(
                200, content=(FIXTURES / "crossref-work-no-pmc.json").read_bytes()
            )
        assert request.url.host == "pmc.ncbi.nlm.nih.gov"
        return httpx.Response(200, content=(FIXTURES / "pmc-idconv-doi-missing.json").read_bytes())

    data_dir, cache_dir = isolated_dirs(monkeypatch, tmp_path)
    mock_transport(monkeypatch, handler)

    assert cli.main(["lookup", "doi:10.1145/3377811.3380366", "--jsonl"]) == 0
    record = read_jsonl_records(capsys)[0]
    assert record["ref"] == "crossref:10.1145/3377811.3380366"
    assert record["fulltext_availability"] == "unavailable"
    assert not data_dir.exists()
    assert not cache_dir.exists()


def test_dry_run_existing_database_does_not_create_wal_artifacts(
    monkeypatch, tmp_path, capsys
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "export.arxiv.org"
        return httpx.Response(200, content=(FIXTURES / "arxiv.xml").read_bytes())

    data_dir, cache_dir = isolated_dirs(monkeypatch, tmp_path)
    seed_database(data_dir, local_paper())
    assert sorted(path.name for path in data_dir.iterdir()) == ["papers.sqlite3"]
    mock_transport(monkeypatch, handler)

    assert cli.main(["download", "arxiv:2301.00001", "--dry-run", "--jsonl"]) == 0
    records = read_jsonl_records(capsys)
    assert len(records) == 1
    assert records[0]["dry_run"] is True
    assert sorted(path.name for path in data_dir.iterdir()) == ["papers.sqlite3"]
    assert not cache_dir.exists()


def test_remote_lookup_existing_database_does_not_create_wal_artifacts(
    monkeypatch, tmp_path, capsys
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "export.arxiv.org"
        return httpx.Response(200, content=(FIXTURES / "arxiv.xml").read_bytes())

    data_dir, cache_dir = isolated_dirs(monkeypatch, tmp_path)
    seed_database(data_dir, local_paper("2301.00002"))
    assert sorted(path.name for path in data_dir.iterdir()) == ["papers.sqlite3"]
    mock_transport(monkeypatch, handler)

    assert cli.main(["lookup", "arxiv:2301.00001", "--jsonl"]) == 0
    records = read_jsonl_records(capsys)
    assert len(records) == 1
    assert records[0]["ref"] == "arxiv:2301.00001"
    assert sorted(path.name for path in data_dir.iterdir()) == ["papers.sqlite3"]
    assert not cache_dir.exists()


def test_lookup_and_dry_run_observe_committed_active_wal(monkeypatch, tmp_path, capsys) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "export.arxiv.org"
        return httpx.Response(200, content=(FIXTURES / "arxiv.xml").read_bytes())

    mock_transport(monkeypatch, handler)
    data_dir, _ = isolated_dirs(monkeypatch, tmp_path)
    data_dir.mkdir()
    writer = Database(data_dir / "papers.sqlite3")
    paper_id = writer.upsert_paper(local_paper())
    assert (data_dir / "papers.sqlite3-wal").exists()

    assert cli.main(["lookup", paper_id, "--jsonl"]) == 0
    records = read_jsonl_records(capsys)
    assert len(records) == 1
    assert records[0]["id"] == paper_id
    assert cli.main(["download", paper_id, "--dry-run", "--jsonl"]) == 0
    records = read_jsonl_records(capsys)
    assert len(records) == 1
    assert records[0]["ref"] == "arxiv:2301.00001"
    writer.close()


def test_unsupported_remote_search_does_not_create_collection_state(
    monkeypatch, tmp_path, capsys
) -> None:
    data_dir, cache_dir = isolated_dirs(monkeypatch, tmp_path)

    assert cli.main(["search", "--source", "unknown", "--query", "test", "--jsonl"]) == 2
    assert read_error(capsys)["code"] == "unknown_source"
    assert not data_dir.exists()
    assert not cache_dir.exists()


def test_remote_search_does_not_create_collection_state(monkeypatch, tmp_path, capsys) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "export.arxiv.org"
        return httpx.Response(200, content=(FIXTURES / "arxiv.xml").read_bytes())

    mock_transport(monkeypatch, handler)
    data_dir, cache_dir = isolated_dirs(monkeypatch, tmp_path)

    assert cli.main(["search", "--source", "arxiv", "--query", "fixture", "--jsonl"]) == 0
    records = read_jsonl_records(capsys)
    assert records[0]["ref"] == "arxiv:2301.00001"
    assert not data_dir.exists()
    assert not cache_dir.exists()


@pytest.mark.parametrize(
    ("query", "expected_code"),
    [("genomics", "unsupported_search"), ("10.1000/example", "invalid_ref")],
)
def test_biorxiv_search_has_distinct_jsonl_error_contracts(
    monkeypatch, tmp_path, capsys, query, expected_code
) -> None:
    data_dir, cache_dir = isolated_dirs(monkeypatch, tmp_path)
    mock_transport(monkeypatch, lambda _: pytest.fail("rejected search must not request provider"))

    assert cli.main(["search", "--source", "biorxiv", "--query", query, "--jsonl"]) == 2
    assert read_error(capsys)["code"] == expected_code
    assert not data_dir.exists()
    assert not cache_dir.exists()


def test_biorxiv_search_reports_not_found_for_valid_missing_doi_jsonl(
    monkeypatch, tmp_path, capsys
) -> None:
    data_dir, cache_dir = isolated_dirs(monkeypatch, tmp_path)
    mock_transport(monkeypatch, lambda _: httpx.Response(200, json={"collection": []}))

    assert (
        cli.main(
            [
                "search",
                "--source",
                "biorxiv",
                "--query",
                "10.1101/2024.01.01.999999",
                "--jsonl",
            ]
        )
        == 3
    )
    assert read_error(capsys)["code"] == "not_found"
    assert not data_dir.exists()
    assert not cache_dir.exists()


def test_verify_all_emits_one_jsonl_record_per_paper_at_scale(
    monkeypatch, tmp_path, capsys
) -> None:
    data_dir, _ = isolated_dirs(monkeypatch, tmp_path)
    data_dir.mkdir()
    database = Database(data_dir / "papers.sqlite3")
    timestamp = "2026-01-01T00:00:00Z"
    rows = [
        (
            f"fixture-{number}",
            "fixture",
            str(number),
            None,
            "Fixture",
            "",
            "[]",
            "[]",
            None,
            None,
            None,
            "https://example.test/landing",
            "https://example.test/pdf",
            timestamp,
            timestamp,
        )
        for number in range(10_001)
    ]
    with database.connection:
        database.connection.executemany(
            """INSERT INTO papers (
            id, source, source_key, source_version, title, abstract, authors_json,
            categories_json, published_at, updated_at, doi, landing_url, pdf_url,
            created_at, refreshed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )
    database.attach_file(
        "fixture-3",
        DownloadedFile(
            "d" * 64,
            4,
            "objects/sha256/dd/dd/" + "d" * 64 + ".txt",
            "https://files.example/fixture-3.txt",
            format="txt",
            media_type="text/plain",
            provider="fixture-files",
        ),
        None,
    )
    database.close()
    monkeypatch.setattr(
        cli, "verify_file", lambda _paths, record: {"ref": record["ref"], "ok": True}
    )

    assert cli.main(["verify", "--all", "--jsonl"]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    lines = captured.out.splitlines()
    assert len(lines) == 10_001
    for line in (lines[0], lines[-1]):
        envelope = json.loads(line)
        assert envelope["schema_version"] == 1
        assert envelope["ok"] is True
        assert envelope["data"]["ok"] is True


def test_verify_all_machine_mode_emits_records_without_summary(
    monkeypatch, tmp_path, capsys
) -> None:
    data_dir, _ = isolated_dirs(monkeypatch, tmp_path)
    seed_verified_paper(data_dir, "2301.00001")

    assert cli.main(["verify", "--all", "--jsonl"]) == 0
    records = read_jsonl_records(capsys)
    assert len(records) == 1
    assert records[0]["status"] == "verified"


def test_verify_all_human_mode_reports_readable_summary(monkeypatch, tmp_path, capsys) -> None:
    data_dir, _ = isolated_dirs(monkeypatch, tmp_path)
    seed_verified_paper(data_dir, "2301.00001")

    assert cli.main(["verify", "--all"]) == 0
    captured = capsys.readouterr()
    assert '"ref": "arxiv:2301.00001"' in captured.out
    assert '"status": "verified"' in captured.out
    assert '"sha256"' in captured.out
    assert '"byte_count"' in captured.out
    assert '"path"' in captured.out
    assert "Verified 1 of 1 papers." in captured.out


def test_verify_batch_human_mode_reports_readable_summary(monkeypatch, tmp_path, capsys) -> None:
    data_dir, _ = isolated_dirs(monkeypatch, tmp_path)
    seed_verified_paper(data_dir, "2301.00001")
    seed_verified_paper(data_dir, "2301.00002")

    assert cli.main(["verify", "arxiv:2301.00001", "arxiv:2301.00002"]) == 0
    captured = capsys.readouterr()
    assert '"ref": "arxiv:2301.00001"' in captured.out
    assert '"ref": "arxiv:2301.00002"' in captured.out
    assert '"status": "verified"' in captured.out
    assert '"sha256"' in captured.out
    assert '"byte_count"' in captured.out
    assert '"path"' in captured.out
    assert "Verified 2 of 2 papers." in captured.out


@pytest.mark.parametrize("use_alias", [False, True])
def test_remove_accepts_uuid_or_local_alias_without_network(
    monkeypatch, tmp_path, capsys, use_alias
) -> None:
    data_dir, _ = isolated_dirs(monkeypatch, tmp_path)
    paper = local_paper()
    paper_id, object_path = seed_downloaded_paper(data_dir, paper)
    monkeypatch.setattr(
        cli.httpx,
        "Client",
        lambda **_: pytest.fail("remove must not create an HTTP client"),
    )

    ref = paper.ref if use_alias else paper_id
    assert cli.main(["remove", ref, "--jsonl"]) == 0
    records = read_jsonl_records(capsys)
    assert len(records) == 1
    assert records[0]["id"] == paper_id
    objects = records[0]["objects"]
    assert isinstance(objects, list)
    assert objects[0]["disposition"] == "deleted"
    assert not object_path.exists()

    assert cli.main(["path", ref, "--jsonl"]) == 3
    assert read_error(capsys)["code"] == "not_found"


def test_remove_dry_run_is_read_only_and_reports_plan(monkeypatch, tmp_path, capsys) -> None:
    data_dir, _ = isolated_dirs(monkeypatch, tmp_path)
    paper_id, object_path = seed_downloaded_paper(data_dir, local_paper())
    assert sorted(path.name for path in data_dir.iterdir()) == ["objects", "papers.sqlite3"]

    assert cli.main(["remove", paper_id, "--dry-run", "--jsonl"]) == 0
    records = read_jsonl_records(capsys)
    assert len(records) == 1
    assert records[0]["dry_run"] is True
    objects = records[0]["objects"]
    assert isinstance(objects, list)
    assert objects[0]["disposition"] == "would_delete"
    assert object_path.is_file()
    database = Database(data_dir / "papers.sqlite3", read_only=True)
    assert database.get(paper_id)["id"] == paper_id
    database.close()
    assert sorted(path.name for path in data_dir.iterdir()) == ["objects", "papers.sqlite3"]


def test_remove_dry_run_absent_collection_creates_nothing(monkeypatch, tmp_path, capsys) -> None:
    data_dir, cache_dir = isolated_dirs(monkeypatch, tmp_path)

    assert cli.main(["remove", "arxiv:2301.00001", "--dry-run", "--jsonl"]) == 3
    assert read_error(capsys)["code"] == "not_found"
    assert not data_dir.exists()
    assert not cache_dir.exists()


def test_remove_retains_shared_object(monkeypatch, tmp_path, capsys) -> None:
    data_dir, _ = isolated_dirs(monkeypatch, tmp_path)
    first = local_paper("2301.00001")
    first_id, object_path = seed_downloaded_paper(data_dir, first)
    second = local_paper("2301.00002")
    database = Database(data_dir / "papers.sqlite3")
    second_id = database.upsert_paper(second)
    file_record = database.get(first_id)["file"]
    assert isinstance(file_record, dict)
    assert second.pdf_url is not None
    database.attach_file(
        second_id,
        DownloadedFile(
            str(file_record["sha256"]),
            int(file_record["byte_count"]),
            str(file_record["relative_path"]),
            second.pdf_url,
        ),
        second.source_version,
    )
    database.close()

    assert cli.main(["remove", first_id, "--jsonl"]) == 0
    records = read_jsonl_records(capsys)
    objects = records[0]["objects"]
    assert isinstance(objects, list)
    assert objects[0]["disposition"] == "retained_shared"
    assert object_path.is_file()
    database = Database(data_dir / "papers.sqlite3", read_only=True)
    assert database.get(second_id)["file"] == file_record | {"source_url": second.pdf_url}
    database.close()


def test_remove_rechecks_new_reference_before_unlink(monkeypatch, tmp_path, capsys) -> None:
    data_dir, _ = isolated_dirs(monkeypatch, tmp_path)
    first = local_paper("2301.00001")
    first_id, object_path = seed_downloaded_paper(data_dir, first)
    second = local_paper("2301.00002")
    relative = object_path.relative_to(data_dir).as_posix()
    original = Database.remove_object_if_unreferenced
    second_id: str | None = None

    def attach_before_guard(self, sha256, remove):
        nonlocal second_id
        competitor = Database(data_dir / "papers.sqlite3")
        second_id = competitor.upsert_paper(second)
        competitor.attach_file(
            second_id,
            DownloadedFile(sha256, object_path.stat().st_size, relative, second.pdf_url),
            second.source_version,
        )
        competitor.close()
        return original(self, sha256, remove)

    monkeypatch.setattr(Database, "remove_object_if_unreferenced", attach_before_guard)

    assert cli.main(["remove", first_id, "--jsonl"]) == 0
    records = read_jsonl_records(capsys)
    objects = records[0]["objects"]
    assert isinstance(objects, list)
    assert objects[0]["disposition"] == "retained_shared"
    assert object_path.is_file()
    assert second_id is not None
    database = Database(data_dir / "papers.sqlite3", read_only=True)
    assert database.get(second_id)["file"] is not None
    database.close()


def test_remove_cleans_metadata_for_already_missing_object(monkeypatch, tmp_path, capsys) -> None:
    data_dir, _ = isolated_dirs(monkeypatch, tmp_path)
    paper_id, object_path = seed_downloaded_paper(data_dir, local_paper())
    object_path.unlink()

    assert cli.main(["remove", paper_id, "--jsonl"]) == 0
    records = read_jsonl_records(capsys)
    objects = records[0]["objects"]
    assert isinstance(objects, list)
    assert objects[0]["disposition"] == "already_missing"
    database = Database(data_dir / "papers.sqlite3", read_only=True)
    with pytest.raises(PapersError, match="No local paper"):
        database.get(paper_id)
    database.close()


def test_remove_unsafe_path_fails_before_mutation(monkeypatch, tmp_path, capsys) -> None:
    data_dir, _ = isolated_dirs(monkeypatch, tmp_path)
    paper_id, object_path = seed_downloaded_paper(data_dir, local_paper())
    database = Database(data_dir / "papers.sqlite3")
    with database.connection:
        database.connection.execute("UPDATE files SET relative_path = ?", ("../outside.pdf",))
    database.close()

    assert cli.main(["remove", paper_id, "--jsonl"]) == 5
    assert read_error(capsys)["code"] == "storage_corrupt"
    assert object_path.is_file()
    database = Database(data_dir / "papers.sqlite3", read_only=True)
    assert database.get(paper_id)["id"] == paper_id
    database.close()


def test_remove_filesystem_failure_is_normalized_without_live_record(
    monkeypatch, tmp_path, capsys
) -> None:
    data_dir, _ = isolated_dirs(monkeypatch, tmp_path)
    paper_id, object_path = seed_downloaded_paper(data_dir, local_paper())
    real_unlink = Path.unlink

    def fail_object_unlink(path: Path, *args, **kwargs) -> None:
        if path == object_path:
            raise PermissionError("blocked")
        real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_object_unlink)

    assert cli.main(["remove", paper_id, "--jsonl"]) == 5
    assert read_error(capsys)["code"] == "storage_remove"
    assert object_path.is_file()
    database = Database(data_dir / "papers.sqlite3", read_only=True)
    with pytest.raises(PapersError, match="No local paper"):
        database.get(paper_id)
    database.close()


def test_remove_database_failure_rolls_back_before_unlink(monkeypatch, tmp_path, capsys) -> None:
    data_dir, _ = isolated_dirs(monkeypatch, tmp_path)
    paper_id, object_path = seed_downloaded_paper(data_dir, local_paper())
    database = sqlite3.connect(data_dir / "papers.sqlite3")
    database.execute(
        """
        CREATE TRIGGER reject_paper_delete BEFORE DELETE ON papers
        BEGIN SELECT RAISE(ABORT, 'injected delete failure'); END
        """
    )
    database.commit()
    database.close()

    assert cli.main(["remove", paper_id, "--jsonl"]) == 5
    assert read_error(capsys)["code"] == "storage_unavailable"
    assert object_path.is_file()
    database_wrapper = Database(data_dir / "papers.sqlite3", read_only=True)
    assert database_wrapper.get(paper_id)["id"] == paper_id
    database_wrapper.close()


def test_every_subcommand_accepts_the_jsonl_flag() -> None:
    invocations = [
        ["sources", "--jsonl"],
        ["search", "--source", "arxiv", "--query", "term", "--jsonl"],
        ["lookup", "arxiv:2301.00001", "--jsonl"],
        ["download", "arxiv:2301.00001", "--jsonl"],
        ["list", "--jsonl"],
        ["path", "arxiv:2301.00001", "--jsonl"],
        ["remove", "arxiv:2301.00001", "--jsonl"],
        ["verify", "arxiv:2301.00001", "--jsonl"],
        ["verify", "--all", "--jsonl"],
    ]
    for invocation in invocations:
        assert cli.build_parser().parse_args(invocation).jsonl is True, invocation


def test_removed_aggregate_flag_is_rejected(capsys) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["sources", "--jso" + "n"])
    assert excinfo.value.code == 2
    assert "unrecognized arguments" in capsys.readouterr().err


def test_tracked_files_contain_no_removed_flag_token() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=repo_root, capture_output=True, text=True, check=True
    )
    removed_token = re.compile("--jso" + r"n(?![0-9A-Za-z_-])")
    offenders = [
        name
        for name in tracked.stdout.splitlines()
        if name and removed_token.search((repo_root / name).read_bytes().decode("utf-8", "ignore"))
    ]
    assert offenders == []


def test_sources_emits_one_record_per_capability(capsys) -> None:
    assert cli.main(["sources", "--jsonl"]) == 0
    records = read_jsonl_records(capsys)
    assert len(records) >= 2
    by_name = {str(record["name"]): record for record in records}
    assert {"arxiv", "biorxiv", "pubmed"} <= set(by_name)
    assert by_name["arxiv"]["metadata_search"] is True
    assert by_name["arxiv"]["reference_formats"] == ["arxiv_id"]
    assert by_name["arxiv"]["fulltext_formats"] == ["pdf"]
    assert by_name["biorxiv"]["search"] == "doi_only"
    assert by_name["biorxiv"]["metadata_search"] is False
    assert by_name["biorxiv"]["reference_formats"] == ["doi"]
    assert by_name["biorxiv"]["fulltext_formats"] == ["pdf"]
    assert by_name["pubmed"]["metadata_search"] is True
    assert by_name["pubmed"]["reference_formats"] == ["pmid"]
    assert by_name["pubmed"]["delivery_source"] == "pmc"


def test_list_emits_one_record_per_paper(monkeypatch, tmp_path, capsys) -> None:
    data_dir, _ = isolated_dirs(monkeypatch, tmp_path)
    data_dir.mkdir()
    database = Database(data_dir / "papers.sqlite3")
    database.upsert_paper(local_paper("2301.00001"))
    database.upsert_paper(local_paper("2301.00002"))
    database.close()

    assert cli.main(["list", "--jsonl"]) == 0
    records = read_jsonl_records(capsys)
    assert len(records) == 2
    assert {str(record["ref"]) for record in records} == {
        "arxiv:2301.00001",
        "arxiv:2301.00002",
    }


def test_list_aggregates_multiple_formats_and_preserves_legacy_pdf_file(
    monkeypatch, tmp_path, capsys
) -> None:
    data_dir, _ = isolated_dirs(monkeypatch, tmp_path)
    paper_id, pdf_path = seed_verified_paper(data_dir, "2301.00001")
    txt, txt_path = attach_downloaded_format(
        data_dir, paper_id, format="txt", body=b"A plain-text full text"
    )

    assert cli.main(["list", "--fields", "ref,file,files", "--jsonl"]) == 0
    records = read_jsonl_records(capsys)
    assert len(records) == 1
    record = records[0]
    legacy_file = record["file"]
    assert isinstance(legacy_file, dict)
    assert legacy_file["relative_path"] == str(pdf_path.relative_to(data_dir))
    files = record["files"]
    assert isinstance(files, list)
    assert [file["format"] for file in files] == ["pdf", "txt"]
    assert files[1]["source"] == "pmc"
    assert files[1]["source_url"] == txt.source_url

    assert cli.main(["path", paper_id, "--jsonl"]) == 0
    assert read_jsonl(capsys) == [{"data": str(pdf_path), "ok": True, "schema_version": 1}]
    assert cli.main(["path", paper_id, "--format", "txt", "--jsonl"]) == 0
    assert read_jsonl(capsys) == [{"data": str(txt_path), "ok": True, "schema_version": 1}]

    assert cli.main(["verify", paper_id, "--jsonl"]) == 0
    verification = read_jsonl_records(capsys)
    assert len(verification) == 1
    assert verification[0]["status"] == "verified"
    verified_files = verification[0]["files"]
    assert isinstance(verified_files, list)
    assert [file["format"] for file in verified_files if isinstance(file, dict)] == ["pdf", "txt"]
    assert cli.main(["verify", paper_id, "--format", "txt", "--jsonl"]) == 0
    assert read_jsonl_records(capsys)[0]["format"] == "txt"


def test_requested_absent_format_reports_available_formats(monkeypatch, tmp_path, capsys) -> None:
    data_dir, _ = isolated_dirs(monkeypatch, tmp_path)
    data_dir.mkdir()
    database = Database(data_dir / "papers.sqlite3")
    paper_id = database.upsert_paper(replace(local_paper(), pdf_url=None, content_urls={}))
    database.close()
    attach_downloaded_format(data_dir, paper_id, format="txt", body=b"text only")

    assert cli.main(["path", paper_id, "--jsonl"]) == 3
    error = read_error(capsys)
    assert error["code"] == "no_file"
    assert error["details"] == {"available_formats": ["txt"], "requested_format": "pdf"}

    assert cli.main(["verify", paper_id, "--format", "xml", "--jsonl"]) == 3
    error = read_error(capsys)
    assert error["code"] == "no_file"
    assert error["details"] == {"available_formats": ["txt"], "requested_format": "xml"}


def test_remove_deletes_all_formats_and_retains_shared_text_object(
    monkeypatch, tmp_path, capsys
) -> None:
    data_dir, _ = isolated_dirs(monkeypatch, tmp_path)
    first_id, pdf_path = seed_verified_paper(data_dir, "2301.00001")
    text, text_path = attach_downloaded_format(
        data_dir, first_id, format="txt", body=b"shared text"
    )
    database = Database(data_dir / "papers.sqlite3")
    second_id = database.upsert_paper(local_paper("2301.00002"))
    database.attach_file(second_id, text, "1")
    database.close()

    assert cli.main(["remove", first_id, "--jsonl"]) == 0
    objects = read_jsonl_records(capsys)[0]["objects"]
    assert isinstance(objects, list)
    dispositions = {object["format"]: object["disposition"] for object in objects}
    assert dispositions == {"pdf": "deleted", "txt": "retained_shared"}
    assert not pdf_path.exists()
    assert text_path.is_file()


def test_empty_list_emits_no_jsonl_records(monkeypatch, tmp_path, capsys) -> None:
    isolated_dirs(monkeypatch, tmp_path)

    assert cli.main(["list", "--jsonl"]) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_lookup_batch_preserves_order_and_duplicates(monkeypatch, tmp_path, capsys) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "export.arxiv.org"
        return httpx.Response(200, content=(FIXTURES / "arxiv.xml").read_bytes())

    mock_transport(monkeypatch, handler)
    data_dir, _ = isolated_dirs(monkeypatch, tmp_path)
    data_dir.mkdir()
    database = Database(data_dir / "papers.sqlite3")
    database.upsert_paper(local_paper("2301.00002"))
    database.close()

    refs = ["arxiv:2301.00002", "arxiv:2301.00001", "arxiv:2301.00002"]
    assert cli.main(["lookup", *refs, "--jsonl"]) == 0
    records = read_jsonl_records(capsys)
    assert [str(record["ref"]) for record in records] == refs
    assert "id" in records[0]
    assert "id" not in records[1]
    assert "id" in records[2]


def test_lookup_batch_repeats_remote_records(monkeypatch, tmp_path, capsys) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "export.arxiv.org"
        return httpx.Response(200, content=(FIXTURES / "arxiv.xml").read_bytes())

    mock_transport(monkeypatch, handler)
    data_dir, cache_dir = isolated_dirs(monkeypatch, tmp_path)

    assert cli.main(["lookup", "arxiv:2301.00001", "arxiv:2301.00001", "--jsonl"]) == 0
    records = read_jsonl_records(capsys)
    assert [str(record["ref"]) for record in records] == ["arxiv:2301.00001", "arxiv:2301.00001"]
    assert not data_dir.exists()
    assert not cache_dir.exists()


def test_download_batch_preserves_duplicate_references(monkeypatch, tmp_path, capsys) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "export.arxiv.org":
            return httpx.Response(200, content=(FIXTURES / "arxiv.xml").read_bytes())
        if request.url.host == "arxiv.org":
            return httpx.Response(
                200, headers={"content-type": "application/pdf"}, content=pdf_bytes()
            )
        return httpx.Response(500)

    mock_transport(monkeypatch, handler)
    isolated_dirs(monkeypatch, tmp_path)

    assert cli.main(["download", "arxiv:2301.00001", "arxiv:2301.00001", "--jsonl"]) == 0
    records = read_jsonl_records(capsys)
    assert len(records) == 2
    assert str(records[0]["ref"]) == "arxiv:2301.00001"
    assert records[1]["id"] == records[0]["id"]


def test_download_batch_preserves_order_for_distinct_references(
    monkeypatch, tmp_path, capsys
) -> None:
    fixture = (FIXTURES / "arxiv.xml").read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "export.arxiv.org":
            requested = str(request.url.params.get("id_list", "2301.00001"))
            return httpx.Response(200, content=fixture.replace(b"2301.00001", requested.encode()))
        if request.url.host == "arxiv.org":
            return httpx.Response(
                200, headers={"content-type": "application/pdf"}, content=pdf_bytes()
            )
        return httpx.Response(500)

    mock_transport(monkeypatch, handler)
    isolated_dirs(monkeypatch, tmp_path)

    refs = ["arxiv:2301.00002", "arxiv:2301.00003"]
    assert cli.main(["download", *refs, "--jsonl"]) == 0
    records = read_jsonl_records(capsys)
    assert [str(record["ref"]) for record in records] == refs
    assert records[0]["id"] != records[1]["id"]


def test_path_batch_preserves_order_and_duplicates(monkeypatch, tmp_path, capsys) -> None:
    data_dir, _ = isolated_dirs(monkeypatch, tmp_path)
    first_id, first_path = seed_verified_paper(data_dir, "2301.00001")
    second_id, second_path = seed_verified_paper(data_dir, "2301.00002", body=pdf_bytes(width=73))

    assert cli.main(["path", first_id, second_id, first_id, "--jsonl"]) == 0
    envelopes = read_jsonl(capsys)
    assert [envelope["data"] for envelope in envelopes] == [
        str(first_path),
        str(second_path),
        str(first_path),
    ]


def test_verify_batch_preserves_order_and_duplicates(monkeypatch, tmp_path, capsys) -> None:
    data_dir, _ = isolated_dirs(monkeypatch, tmp_path)
    first_id, _ = seed_verified_paper(data_dir, "2301.00001")
    second_id, _ = seed_verified_paper(data_dir, "2301.00002")

    assert cli.main(["verify", first_id, second_id, first_id, "--jsonl"]) == 0
    records = read_jsonl_records(capsys)
    assert [str(record["ref"]) for record in records] == [
        "arxiv:2301.00001",
        "arxiv:2301.00002",
        "arxiv:2301.00001",
    ]
    assert all(record["ok"] is True for record in records)


def test_verify_rejects_references_combined_with_all(capsys) -> None:
    assert cli.main(["verify", "arxiv:2301.00001", "--all", "--jsonl"]) == 2
    assert read_error(capsys)["code"] == "usage"


def test_verify_requires_references_or_all(capsys) -> None:
    assert cli.main(["verify", "--jsonl"]) == 2
    assert read_error(capsys)["code"] == "usage"


def test_verify_without_targets_fails_usage_in_human_mode(capsys) -> None:
    assert cli.main(["verify"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "verify requires references or --all" in captured.err


def test_verify_references_with_all_fails_usage_in_human_mode(capsys) -> None:
    assert cli.main(["verify", "arxiv:2301.00001", "--all"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "--all cannot be combined with references" in captured.err


def test_domain_error_emits_single_jsonl_error(monkeypatch, tmp_path, capsys) -> None:
    data_dir, _ = isolated_dirs(monkeypatch, tmp_path)
    seed_database(data_dir, local_paper())

    assert cli.main(["path", "arxiv:9999.99999", "--jsonl"]) == 3
    assert read_error(capsys)["code"] == "not_found"


@pytest.mark.parametrize(
    ("argv", "cardinality"),
    [
        (["sources", "--help"], "one JSONL record per source"),
        (["search", "--help"], "one JSONL record per result"),
        (["lookup", "--help"], "one JSONL record per reference"),
        (["download", "--help"], "one JSONL record per reference"),
        (["list", "--help"], "one JSONL record per paper"),
        (["path", "--help"], "one JSONL record per reference"),
        (["remove", "--help"], "exactly one JSONL record"),
        (["verify", "--help"], "one JSONL record per paper"),
    ],
)
def test_subcommand_help_documents_jsonl_contract(argv, cardinality, capsys) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(argv)
    assert excinfo.value.code == 0
    flattened = " ".join(capsys.readouterr().out.split())
    assert "--jsonl" in flattened
    assert cardinality in flattened
    assert "An empty success emits no records" in flattened
    assert "error object" in flattened
    assert "Machine summaries and handled command or usage errors are not written to stderr" in (
        flattened
    )


@pytest.mark.parametrize("command", ["lookup", "download"])
def test_reference_help_documents_doi_and_pmid_resolution(command, capsys) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main([command, "--help"])
    assert excinfo.value.code == 0
    flattened = " ".join(capsys.readouterr().out.split())
    assert "crossref:DOI" in flattened
    assert "pubmed:PMID" in flattened
    assert "DOI references use Crossref metadata" in flattened
    assert "pmid:PMID uses PubMed metadata" in flattened


def test_search_help_documents_pubmed_metadata_search(capsys) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["search", "--help"])
    assert excinfo.value.code == 0
    assert "PubMed supports keyword metadata search" in " ".join(capsys.readouterr().out.split())


def test_verify_help_documents_human_only_summary(capsys) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["verify", "--help"])
    assert excinfo.value.code == 0
    flattened = " ".join(capsys.readouterr().out.split())
    assert "no machine summary record" in flattened
    assert "Verification summaries are reported in human mode only" in flattened


FIELDS_COMMANDS = [
    ["search", "--source", "arxiv", "--query", "fixture"],
    ["lookup", "arxiv:2301.00001"],
    ["list"],
]

FIELDS_EXCLUDED_COMMANDS = [
    ["sources"],
    ["download", "arxiv:2301.00001"],
    ["path", "arxiv:2301.00001"],
    ["remove", "arxiv:2301.00001"],
    ["verify", "arxiv:2301.00001"],
]


def no_network_client(monkeypatch) -> None:
    monkeypatch.setattr(
        cli.httpx,
        "Client",
        lambda **_: pytest.fail("field validation must fail before creating an HTTP client"),
    )


def test_search_fields_project_every_record_without_unselected_fields(
    monkeypatch, tmp_path, capsys
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "export.arxiv.org"
        return httpx.Response(200, content=feed_with_entries(b"2301.00001", b"2301.00002"))

    mock_transport(monkeypatch, handler)
    isolated_dirs(monkeypatch, tmp_path)

    assert (
        cli.main(
            [
                "search",
                "--source",
                "arxiv",
                "--query",
                "fixture",
                "--fields",
                "ref,title,authors",
                "--jsonl",
            ]
        )
        == 0
    )
    records = read_jsonl_records(capsys)
    assert [str(record["ref"]) for record in records] == [
        "arxiv:2301.00001",
        "arxiv:2301.00002",
    ]
    for record in records:
        assert set(record) == {"ref", "title", "authors"}
        assert "abstract" not in record
        assert "landing_url" not in record
    assert records[0]["authors"] == ["Alice Example", "Bob Example"]

    assert cli.main(["search", "--source", "arxiv", "--query", "fixture", "--jsonl"]) == 0
    complete = read_jsonl_records(capsys)
    assert len(complete) == len(records)
    for projected, full in zip(records, complete, strict=True):
        assert projected == {name: full[name] for name in ("ref", "title", "authors")}


def test_search_fields_project_in_human_mode(monkeypatch, tmp_path, capsys) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "export.arxiv.org"
        return httpx.Response(200, content=(FIXTURES / "arxiv.xml").read_bytes())

    mock_transport(monkeypatch, handler)
    isolated_dirs(monkeypatch, tmp_path)

    selection = ["--fields", "ref,title"]
    assert cli.main(["search", "--source", "arxiv", "--query", "fixture", *selection]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert set(json.loads(captured.out)) == {"ref", "title"}


def test_search_fields_with_empty_results_emits_no_records(monkeypatch, tmp_path, capsys) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "export.arxiv.org"
        return httpx.Response(200, content=feed_with_entries())

    mock_transport(monkeypatch, handler)
    data_dir, _ = isolated_dirs(monkeypatch, tmp_path)

    assert (
        cli.main(
            ["search", "--source", "arxiv", "--query", "fixture", "--fields", "ref", "--jsonl"]
        )
        == 0
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
    assert not data_dir.exists()


def test_lookup_fields_project_remote_batch_in_input_order(monkeypatch, tmp_path, capsys) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "export.arxiv.org"
        return httpx.Response(200, content=(FIXTURES / "arxiv.xml").read_bytes())

    mock_transport(monkeypatch, handler)
    isolated_dirs(monkeypatch, tmp_path)

    refs = ["arxiv:2301.00001", "arxiv:2301.00001"]
    assert cli.main(["lookup", *refs, "--fields", "ref,doi", "--jsonl"]) == 0
    records = read_jsonl_records(capsys)
    assert [str(record["ref"]) for record in records] == refs
    for record in records:
        assert set(record) == {"ref", "doi"}


def test_lookup_fields_project_single_remote_reference(monkeypatch, tmp_path, capsys) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "export.arxiv.org"
        return httpx.Response(200, content=(FIXTURES / "arxiv.xml").read_bytes())

    mock_transport(monkeypatch, handler)
    isolated_dirs(monkeypatch, tmp_path)

    assert cli.main(["lookup", "arxiv:2301.00001", "--fields", "ref,title", "--jsonl"]) == 0
    records = read_jsonl_records(capsys)
    assert len(records) == 1
    assert set(records[0]) == {"ref", "title"}
    assert str(records[0]["ref"]) == "arxiv:2301.00001"


def test_lookup_fields_project_local_records_to_shared_fields(
    monkeypatch, tmp_path, capsys
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "export.arxiv.org"
        return httpx.Response(200, content=(FIXTURES / "arxiv.xml").read_bytes())

    mock_transport(monkeypatch, handler)
    data_dir, _ = isolated_dirs(monkeypatch, tmp_path)
    data_dir.mkdir()
    database = Database(data_dir / "papers.sqlite3")
    database.upsert_paper(local_paper("2301.00002"))
    database.close()

    refs = ["arxiv:2301.00002", "arxiv:2301.00001", "arxiv:2301.00002"]
    assert cli.main(["lookup", *refs, "--fields", "ref,title", "--jsonl"]) == 0
    records = read_jsonl_records(capsys)
    assert [str(record["ref"]) for record in records] == refs
    for record in records:
        assert set(record) == {"ref", "title"}

    assert cli.main(["lookup", *refs, "--jsonl"]) == 0
    complete = read_jsonl_records(capsys)
    assert "id" in complete[0]
    assert "created_at" in complete[0]
    for projected, full in zip(records, complete, strict=True):
        assert projected == {"ref": full["ref"], "title": full["title"]}


def test_list_fields_return_intact_file_object_and_omit_absent(
    monkeypatch, tmp_path, capsys
) -> None:
    data_dir, _ = isolated_dirs(monkeypatch, tmp_path)
    paper_id, _ = seed_downloaded_paper(data_dir, local_paper("2301.00001"))
    database = Database(data_dir / "papers.sqlite3")
    database.upsert_paper(local_paper("2301.00002"))
    database.close()
    reader = Database(data_dir / "papers.sqlite3", read_only=True)
    expected_file = reader.get(paper_id)["file"]
    reader.close()
    assert isinstance(expected_file, dict)

    assert cli.main(["list", "--fields", "ref,file", "--jsonl"]) == 0
    records = read_jsonl_records(capsys)
    assert len(records) == 2
    projected = {str(record["ref"]): record for record in records}
    assert set(projected["arxiv:2301.00001"]) == {"ref", "file"}
    assert projected["arxiv:2301.00001"]["file"] == expected_file
    assert set(projected["arxiv:2301.00002"]) == {"ref"}


def test_fields_defaults_preserve_complete_records(monkeypatch, tmp_path, capsys) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "export.arxiv.org"
        return httpx.Response(200, content=(FIXTURES / "arxiv.xml").read_bytes())

    mock_transport(monkeypatch, handler)
    data_dir, _ = isolated_dirs(monkeypatch, tmp_path)
    seed_downloaded_paper(data_dir, local_paper("2301.00002"))

    assert cli.main(["search", "--source", "arxiv", "--query", "fixture", "--jsonl"]) == 0
    assert set(read_jsonl_records(capsys)[0]) == set(REMOTE_PAPER_FIELDS)

    assert cli.main(["lookup", "arxiv:2301.00001", "--jsonl"]) == 0
    assert set(read_jsonl_records(capsys)[0]) == set(REMOTE_PAPER_FIELDS)

    assert cli.main(["lookup", "arxiv:2301.00002", "--jsonl"]) == 0
    assert set(read_jsonl_records(capsys)[0]) == set(LIST_PAPER_FIELDS)

    assert cli.main(["list", "--jsonl"]) == 0
    assert set(read_jsonl_records(capsys)[0]) == set(LIST_PAPER_FIELDS)


def test_fields_projection_matches_across_human_and_jsonl_modes(
    monkeypatch, tmp_path, capsys
) -> None:
    data_dir, _ = isolated_dirs(monkeypatch, tmp_path)
    seed_downloaded_paper(data_dir, local_paper("2301.00001"))

    assert cli.main(["list", "--fields", "ref,file", "--jsonl"]) == 0
    machine = read_jsonl_records(capsys)

    assert cli.main(["list", "--fields", "ref,file"]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert [json.loads(captured.out)] == machine


def test_list_fields_with_empty_collection_emits_no_records(monkeypatch, tmp_path, capsys) -> None:
    isolated_dirs(monkeypatch, tmp_path)

    assert cli.main(["list", "--fields", "ref", "--jsonl"]) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


@pytest.mark.parametrize(
    "selection", ["", "ref,", ",ref", "ref,,title", "ref,ref", "bogus", "ref,title,bogus"]
)
@pytest.mark.parametrize("argv", FIELDS_COMMANDS)
def test_fields_reject_invalid_selections_before_access(
    monkeypatch, tmp_path, capsys, argv, selection
) -> None:
    no_network_client(monkeypatch)
    data_dir, _ = isolated_dirs(monkeypatch, tmp_path)

    assert cli.main([*argv, "--fields", selection, "--jsonl"]) == 2
    assert read_error(capsys)["code"] == "usage"
    assert not data_dir.exists()


@pytest.mark.parametrize("selection", ["id", "created_at", "refreshed_at", "file"])
@pytest.mark.parametrize("argv", FIELDS_COMMANDS[:2])
def test_fields_reject_local_only_names_on_remote_commands(
    monkeypatch, tmp_path, capsys, argv, selection
) -> None:
    no_network_client(monkeypatch)
    data_dir, _ = isolated_dirs(monkeypatch, tmp_path)

    assert cli.main([*argv, "--fields", selection, "--jsonl"]) == 2
    assert read_error(capsys)["code"] == "usage"
    assert not data_dir.exists()


def test_fields_invalid_selection_fails_usage_in_human_mode(capsys) -> None:
    assert cli.main(["list", "--fields", "ref,ref"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "duplicate field name" in captured.err


def test_fields_validation_precedes_source_resolution(monkeypatch, tmp_path, capsys) -> None:
    no_network_client(monkeypatch)
    data_dir, _ = isolated_dirs(monkeypatch, tmp_path)

    assert (
        cli.main(["search", "--source", "unknown", "--query", "x", "--fields", "bogus", "--jsonl"])
        == 2
    )
    assert read_error(capsys)["code"] == "usage"
    assert not data_dir.exists()


@pytest.mark.parametrize("argv", FIELDS_EXCLUDED_COMMANDS)
def test_other_commands_reject_fields_flag(argv, capsys) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main([*argv, "--fields", "ref"])
    assert excinfo.value.code == 2
    assert "unrecognized arguments" in capsys.readouterr().err


@pytest.mark.parametrize("argv", FIELDS_EXCLUDED_COMMANDS)
def test_other_commands_reject_fields_flag_in_jsonl_mode(argv, capsys) -> None:
    assert cli.main([*argv, "--fields", "ref", "--jsonl"]) == 2
    assert read_error(capsys)["code"] == "usage"


@pytest.mark.parametrize("command", ["search", "lookup", "list"])
def test_metadata_help_documents_fields(command, capsys) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main([command, "--help"])
    assert excinfo.value.code == 0
    flattened = " ".join(capsys.readouterr().out.split())
    assert "--fields" in flattened
    assert "default: all fields" in flattened
    assert "usage errors" in flattened
    for field in cli.FIELD_VOCABULARIES[command]:
        assert field in flattened


@pytest.mark.parametrize(
    "argv",
    [
        ["sources", "--help"],
        ["download", "--help"],
        ["path", "--help"],
        ["remove", "--help"],
        ["verify", "--help"],
    ],
)
def test_other_command_help_omits_fields(argv, capsys) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(argv)
    assert excinfo.value.code == 0
    flattened = " ".join(capsys.readouterr().out.split())
    assert "--fields" not in flattened


def test_list_ordering_and_limit_are_unchanged_with_fields(monkeypatch, tmp_path, capsys) -> None:
    data_dir, _ = isolated_dirs(monkeypatch, tmp_path)
    data_dir.mkdir()
    database = Database(data_dir / "papers.sqlite3")
    rows = [
        (
            f"fixture-{number}",
            "fixture",
            str(number),
            None,
            "Fixture",
            "",
            "[]",
            "[]",
            None,
            None,
            None,
            "https://example.test/landing",
            "https://example.test/pdf",
            f"2026-01-0{number}T00:00:00Z",
            f"2026-01-0{number}T00:00:00Z",
        )
        for number in (1, 2, 3)
    ]
    with database.connection:
        database.connection.executemany(
            """INSERT INTO papers (
            id, source, source_key, source_version, title, abstract, authors_json,
            categories_json, published_at, updated_at, doi, landing_url, pdf_url,
            created_at, refreshed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )
    database.close()

    assert cli.main(["list", "--jsonl"]) == 0
    assert [str(record["ref"]) for record in read_jsonl_records(capsys)] == [
        "fixture:3",
        "fixture:2",
        "fixture:1",
    ]

    assert cli.main(["list", "--limit", "2", "--fields", "ref", "--jsonl"]) == 0
    records = read_jsonl_records(capsys)
    assert [str(record["ref"]) for record in records] == ["fixture:3", "fixture:2"]
    assert all(set(record) == {"ref"} for record in records)
