from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from papers_cli import cli
from papers_cli.db import Database
from papers_cli.models import RemotePaper

FIXTURES = Path(__file__).parent / "fixtures"


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


def test_download_then_verify_has_stable_json(monkeypatch, tmp_path, capsys) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "export.arxiv.org":
            return httpx.Response(200, content=(FIXTURES / "arxiv.xml").read_bytes())
        if request.url.host == "arxiv.org":
            return httpx.Response(
                200, headers={"content-type": "application/pdf"}, content=b"%PDF-1.7\nfixture"
            )
        return httpx.Response(500)

    real_client = httpx.Client

    def mock_client(**kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(**kwargs)

    monkeypatch.setattr(cli.httpx, "Client", mock_client)
    monkeypatch.setenv("PAPERS_CLI_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("PAPERS_CLI_CACHE_DIR", str(tmp_path / "cache"))

    assert cli.main(["download", "arxiv:2301.00001", "--json"]) == 0
    downloaded = json.loads(capsys.readouterr().out)
    assert downloaded["ok"] is True
    paper_id = downloaded["data"][0]["id"]

    assert cli.main(["verify", paper_id, "--json"]) == 0
    verified = json.loads(capsys.readouterr().out)
    assert verified["data"]["status"] == "verified"


def test_download_staging_failure_has_stable_json(monkeypatch, tmp_path, capsys) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "export.arxiv.org":
            return httpx.Response(200, content=(FIXTURES / "arxiv.xml").read_bytes())
        if request.url.host == "arxiv.org":
            return httpx.Response(
                200, headers={"content-type": "application/pdf"}, content=b"%PDF-1.7\nfixture"
            )
        return httpx.Response(500)

    real_client = httpx.Client

    def mock_client(**kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(**kwargs)

    def fail_mkstemp(**_: object) -> tuple[int, str]:
        raise PermissionError("Permission denied")

    monkeypatch.setattr(cli.httpx, "Client", mock_client)
    monkeypatch.setattr("papers_cli.downloader.tempfile.mkstemp", fail_mkstemp)
    monkeypatch.setenv("PAPERS_CLI_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("PAPERS_CLI_CACHE_DIR", str(tmp_path / "cache"))

    assert cli.main(["download", "arxiv:2301.00001", "--json"]) == 5
    output = json.loads(capsys.readouterr().out)
    assert output["ok"] is False
    assert output["error"]["code"] == "storage_staging"


@pytest.mark.parametrize("blocked_override", ["PAPERS_CLI_DATA_DIR", "PAPERS_CLI_CACHE_DIR"])
def test_download_path_initialization_failure_has_stable_json(
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

    assert cli.main(["download", "arxiv:2301.00001", "--json"]) == 5
    output_text = capsys.readouterr().out
    output = json.loads(output_text)
    assert output_text.count("\n") == 1
    assert output["schema_version"] == 1
    assert output["ok"] is False
    assert output["error"]["code"] == "storage_initialize"


def test_dry_run_does_not_create_collection_state(monkeypatch, tmp_path, capsys) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "export.arxiv.org"
        return httpx.Response(200, content=(FIXTURES / "arxiv.xml").read_bytes())

    real_client = httpx.Client

    def mock_client(**kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(**kwargs)

    data_dir = tmp_path / "data"
    cache_dir = tmp_path / "cache"
    monkeypatch.setattr(cli.httpx, "Client", mock_client)
    monkeypatch.setenv("PAPERS_CLI_DATA_DIR", str(data_dir))
    monkeypatch.setenv("PAPERS_CLI_CACHE_DIR", str(cache_dir))

    assert cli.main(["download", "arxiv:2301.00001", "--dry-run", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["data"][0]["dry_run"] is True
    assert not data_dir.exists()
    assert not cache_dir.exists()


def test_parse_errors_use_json_contract_when_requested(capsys) -> None:
    assert cli.main(["search", "--json"]) == 2
    output = json.loads(capsys.readouterr().out)
    assert output["schema_version"] == 1
    assert output["ok"] is False
    assert output["error"]["code"] == "usage"


def test_remote_lookup_does_not_create_collection_state(monkeypatch, tmp_path, capsys) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "export.arxiv.org"
        return httpx.Response(200, content=(FIXTURES / "arxiv.xml").read_bytes())

    real_client = httpx.Client

    def mock_client(**kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(**kwargs)

    data_dir = tmp_path / "data"
    cache_dir = tmp_path / "cache"
    monkeypatch.setattr(cli.httpx, "Client", mock_client)
    monkeypatch.setenv("PAPERS_CLI_DATA_DIR", str(data_dir))
    monkeypatch.setenv("PAPERS_CLI_CACHE_DIR", str(cache_dir))

    assert cli.main(["lookup", "arxiv:2301.00001", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["data"]["ref"] == "arxiv:2301.00001"
    assert not data_dir.exists()
    assert not cache_dir.exists()


def test_dry_run_existing_database_does_not_create_wal_artifacts(
    monkeypatch, tmp_path, capsys
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "export.arxiv.org"
        return httpx.Response(200, content=(FIXTURES / "arxiv.xml").read_bytes())

    real_client = httpx.Client

    def mock_client(**kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(**kwargs)

    data_dir = tmp_path / "data"
    cache_dir = tmp_path / "cache"
    seed_database(data_dir, local_paper())
    assert sorted(path.name for path in data_dir.iterdir()) == ["papers.sqlite3"]
    monkeypatch.setattr(cli.httpx, "Client", mock_client)
    monkeypatch.setenv("PAPERS_CLI_DATA_DIR", str(data_dir))
    monkeypatch.setenv("PAPERS_CLI_CACHE_DIR", str(cache_dir))

    assert cli.main(["download", "arxiv:2301.00001", "--dry-run", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["data"][0]["dry_run"] is True
    assert sorted(path.name for path in data_dir.iterdir()) == ["papers.sqlite3"]
    assert not cache_dir.exists()


def test_remote_lookup_existing_database_does_not_create_wal_artifacts(
    monkeypatch, tmp_path, capsys
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "export.arxiv.org"
        return httpx.Response(200, content=(FIXTURES / "arxiv.xml").read_bytes())

    real_client = httpx.Client

    def mock_client(**kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(**kwargs)

    data_dir = tmp_path / "data"
    cache_dir = tmp_path / "cache"
    seed_database(data_dir, local_paper("2301.00002"))
    assert sorted(path.name for path in data_dir.iterdir()) == ["papers.sqlite3"]
    monkeypatch.setattr(cli.httpx, "Client", mock_client)
    monkeypatch.setenv("PAPERS_CLI_DATA_DIR", str(data_dir))
    monkeypatch.setenv("PAPERS_CLI_CACHE_DIR", str(cache_dir))

    assert cli.main(["lookup", "arxiv:2301.00001", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["data"]["ref"] == "arxiv:2301.00001"
    assert sorted(path.name for path in data_dir.iterdir()) == ["papers.sqlite3"]
    assert not cache_dir.exists()


def test_lookup_and_dry_run_observe_committed_active_wal(monkeypatch, tmp_path, capsys) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "export.arxiv.org"
        return httpx.Response(200, content=(FIXTURES / "arxiv.xml").read_bytes())

    real_client = httpx.Client

    def mock_client(**kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(**kwargs)

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    writer = Database(data_dir / "papers.sqlite3")
    paper_id = writer.upsert_paper(local_paper())
    assert (data_dir / "papers.sqlite3-wal").exists()
    monkeypatch.setattr(cli.httpx, "Client", mock_client)
    monkeypatch.setenv("PAPERS_CLI_DATA_DIR", str(data_dir))
    monkeypatch.setenv("PAPERS_CLI_CACHE_DIR", str(tmp_path / "cache"))

    assert cli.main(["lookup", paper_id, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["data"]["id"] == paper_id
    assert cli.main(["download", paper_id, "--dry-run", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["data"][0]["ref"] == "arxiv:2301.00001"
    writer.close()


def test_unsupported_remote_search_does_not_create_collection_state(
    monkeypatch, tmp_path, capsys
) -> None:
    data_dir = tmp_path / "data"
    cache_dir = tmp_path / "cache"
    monkeypatch.setenv("PAPERS_CLI_DATA_DIR", str(data_dir))
    monkeypatch.setenv("PAPERS_CLI_CACHE_DIR", str(cache_dir))

    assert cli.main(["search", "--source", "unknown", "--query", "test", "--json"]) == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "unknown_source"
    assert not data_dir.exists()
    assert not cache_dir.exists()


def test_remote_search_does_not_create_collection_state(monkeypatch, tmp_path, capsys) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "export.arxiv.org"
        return httpx.Response(200, content=(FIXTURES / "arxiv.xml").read_bytes())

    real_client = httpx.Client

    def mock_client(**kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(**kwargs)

    data_dir = tmp_path / "data"
    cache_dir = tmp_path / "cache"
    monkeypatch.setattr(cli.httpx, "Client", mock_client)
    monkeypatch.setenv("PAPERS_CLI_DATA_DIR", str(data_dir))
    monkeypatch.setenv("PAPERS_CLI_CACHE_DIR", str(cache_dir))

    assert cli.main(["search", "--source", "arxiv", "--query", "fixture", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["data"][0]["ref"] == "arxiv:2301.00001"
    assert not data_dir.exists()
    assert not cache_dir.exists()


def test_verify_all_includes_more_than_ten_thousand_records(monkeypatch, tmp_path) -> None:
    data_dir = tmp_path / "data"
    cache_dir = tmp_path / "cache"
    data_dir.mkdir()
    monkeypatch.setenv("PAPERS_CLI_DATA_DIR", str(data_dir))
    monkeypatch.setenv("PAPERS_CLI_CACHE_DIR", str(cache_dir))
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
    database.close()
    monkeypatch.setattr(
        cli, "verify_file", lambda _paths, record: {"ref": record["ref"], "ok": True}
    )

    args = cli.build_parser().parse_args(["verify", "--all", "--json"])
    result = cli.execute(args)
    assert isinstance(result, dict)
    assert result["total"] == 10_001
    assert result["verified"] == 10_001
