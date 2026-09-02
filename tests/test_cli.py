from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import subprocess
from pathlib import Path

import httpx
import pytest

from papers_cli import cli
from papers_cli.db import Database
from papers_cli.errors import PapersError
from papers_cli.models import DownloadedFile, RemotePaper

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


def seed_downloaded_paper(
    data_dir: Path,
    paper: RemotePaper,
    *,
    sha256: str = "a" * 64,
    body: bytes = b"%PDF-1.7\nfixture",
) -> tuple[str, Path]:
    data_dir.mkdir(exist_ok=True)
    relative = Path("objects") / "sha256" / sha256[:2] / sha256[2:4] / f"{sha256}.pdf"
    object_path = data_dir / relative
    object_path.parent.mkdir(parents=True, exist_ok=True)
    object_path.write_bytes(body)
    database = Database(data_dir / "papers.sqlite3")
    paper_id = database.upsert_paper(paper)
    database.attach_file(
        paper_id,
        DownloadedFile(sha256, len(body), relative.as_posix(), paper.pdf_url),
        paper.source_version,
    )
    database.close()
    return paper_id, object_path


def seed_verified_paper(
    data_dir: Path, source_key: str, *, body: bytes = b"%PDF-1.7\nfixture"
) -> tuple[str, Path]:
    return seed_downloaded_paper(
        data_dir,
        local_paper(source_key),
        sha256=hashlib.sha256(body).hexdigest(),
        body=body,
    )


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


def test_download_then_verify_has_stable_jsonl(monkeypatch, tmp_path, capsys) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "export.arxiv.org":
            return httpx.Response(200, content=(FIXTURES / "arxiv.xml").read_bytes())
        if request.url.host == "arxiv.org":
            return httpx.Response(
                200, headers={"content-type": "application/pdf"}, content=b"%PDF-1.7\nfixture"
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
                200, headers={"content-type": "application/pdf"}, content=b"%PDF-1.7\nfixture"
            )
        return httpx.Response(500)

    mock_transport(monkeypatch, handler)

    def fail_mkstemp(**_: object) -> tuple[int, str]:
        raise PermissionError("Permission denied")

    monkeypatch.setattr("papers_cli.downloader.tempfile.mkstemp", fail_mkstemp)
    isolated_dirs(monkeypatch, tmp_path)

    assert cli.main(["download", "arxiv:2301.00001", "--jsonl"]) == 5
    assert read_error(capsys)["code"] == "storage_staging"


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
        if name
        and removed_token.search((repo_root / name).read_bytes().decode("utf-8", "ignore"))
    ]
    assert offenders == []


def test_sources_emits_one_record_per_capability(capsys) -> None:
    assert cli.main(["sources", "--jsonl"]) == 0
    records = read_jsonl_records(capsys)
    assert len(records) >= 2
    assert {"arxiv", "biorxiv"} <= {str(record["name"]) for record in records}


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
                200, headers={"content-type": "application/pdf"}, content=b"%PDF-1.7\nfixture"
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
            return httpx.Response(
                200, content=fixture.replace(b"2301.00001", requested.encode())
            )
        if request.url.host == "arxiv.org":
            return httpx.Response(
                200, headers={"content-type": "application/pdf"}, content=b"%PDF-1.7\nfixture"
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
    second_id, second_path = seed_verified_paper(
        data_dir, "2301.00002", body=b"%PDF-1.7\nsecond"
    )

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


def test_verify_help_documents_human_only_summary(capsys) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["verify", "--help"])
    assert excinfo.value.code == 0
    flattened = " ".join(capsys.readouterr().out.split())
    assert "no machine summary record" in flattened
    assert "Verification summaries are reported in human mode only" in flattened
