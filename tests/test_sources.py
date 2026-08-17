from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from papers_cli.errors import PapersError
from papers_cli.sources import ArxivAdapter, BiorxivAdapter

FIXTURES = Path(__file__).parent / "fixtures"


def client_for(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_arxiv_normalizes_atom_fixture() -> None:
    adapter = ArxivAdapter()
    records = adapter._parse((FIXTURES / "arxiv.xml").read_bytes())
    assert len(records) == 1
    paper = records[0]
    assert paper.ref == "arxiv:2301.00001"
    assert paper.source_version == "2"
    assert paper.authors == ["Alice Example", "Bob Example"]
    assert paper.doi == "10.1000/test"


def test_arxiv_lookup_uses_official_api() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "export.arxiv.org"
        assert request.url.params["id_list"] == "2301.00001"
        return httpx.Response(200, content=(FIXTURES / "arxiv.xml").read_bytes())

    with client_for(handler) as client:
        assert ArxivAdapter().lookup("arxiv:2301.00001", client).ref == "arxiv:2301.00001"


def test_biorxiv_normalizes_official_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.biorxiv.org"
        return httpx.Response(200, content=(FIXTURES / "biorxiv.json").read_bytes())

    with client_for(handler) as client:
        paper = BiorxivAdapter().lookup("10.1101/2024.01.01.123456", client)
    assert paper.ref == "biorxiv:10.1101/2024.01.01.123456"
    assert paper.pdf_url.endswith("v3.full.pdf")


def test_biorxiv_rejects_general_search() -> None:
    with pytest.raises(PapersError, match="DOI"):
        BiorxivAdapter().search(
            "genomics",
            5,
            httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(500))),
        )
