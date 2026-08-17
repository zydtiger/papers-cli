from __future__ import annotations

import json
from pathlib import Path

import httpx

from papers_cli import cli

FIXTURES = Path(__file__).parent / "fixtures"


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
