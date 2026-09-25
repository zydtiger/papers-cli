from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from papers_cli.errors import PapersError
from papers_cli.sources import ArxivAdapter, BiorxivAdapter, infer_adapter, source_capabilities

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
    assert paper.content_urls == {"pdf": paper.pdf_url}


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
    assert paper.pdf_url is not None
    assert paper.pdf_url.endswith("v3.full.pdf")
    assert paper.content_urls == {"pdf": paper.pdf_url}


def test_download_target_reports_missing_pdf_with_available_formats() -> None:
    paper = replace(
        ArxivAdapter()._parse((FIXTURES / "arxiv.xml").read_bytes())[0],
        pdf_url=None,
        content_urls={"txt": "https://arxiv.org/text/2301.00001"},
    )
    with pytest.raises(PapersError) as error:
        ArxivAdapter().download_target(paper, "pdf")
    assert error.value.code == "format_unavailable"
    assert error.value.details == {
        "availability": "known",
        "available_formats": ["txt"],
        "ref": "arxiv:2301.00001",
        "requested_format": "pdf",
    }


def test_biorxiv_rejects_general_search() -> None:
    with client_for(
        lambda _: pytest.fail("keyword search must not request the provider")
    ) as client:
        with pytest.raises(PapersError) as error:
            BiorxivAdapter().search("genomics", 5, client)
    assert error.value.code == "unsupported_search"


@pytest.mark.parametrize("query", ["10.1000/example", "biorxiv:not-a-doi", "10.1101/"])
def test_biorxiv_search_rejects_invalid_doi_references(query: str) -> None:
    with client_for(
        lambda _: pytest.fail("invalid references must not request the provider")
    ) as client:
        with pytest.raises(PapersError) as error:
            BiorxivAdapter().search(query, 5, client)
    assert error.value.code == "invalid_ref"


def test_biorxiv_search_delegates_valid_doi_to_lookup() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.biorxiv.org"
        return httpx.Response(200, content=(FIXTURES / "biorxiv.json").read_bytes())

    with client_for(handler) as client:
        records = BiorxivAdapter().search("10.1101/2024.01.01.123456", 5, client)
    assert [paper.ref for paper in records] == ["biorxiv:10.1101/2024.01.01.123456"]


def test_biorxiv_search_reports_not_found_for_valid_missing_doi() -> None:
    with client_for(lambda _: httpx.Response(200, json={"collection": []})) as client:
        with pytest.raises(PapersError) as error:
            BiorxivAdapter().search("10.1101/2024.01.01.999999", 5, client)
    assert error.value.code == "not_found"


@pytest.mark.parametrize(
    "reference",
    [
        "doi:10.1000/example",
        "10.1000/example",
        "10.1002/(SICI)1099-0844(199612)12:4<290::AID-CBF4>3.0.CO;2-P",
    ],
)
def test_generic_remote_doi_is_unsupported(reference: str) -> None:
    with pytest.raises(PapersError) as error:
        infer_adapter(reference)
    assert error.value.code == "unsupported_ref"


def test_unqualified_biorxiv_doi_remains_a_supported_remote_reference() -> None:
    adapter, raw = infer_adapter("10.1101/2024.01.01.123456")
    assert adapter.source == "biorxiv"
    assert raw == "10.1101/2024.01.01.123456"


def test_source_capabilities_describe_metadata_and_fulltext_formats() -> None:
    capabilities = {record["name"]: record for record in source_capabilities()}
    assert capabilities["arxiv"] == {
        "name": "arxiv",
        "search": True,
        "metadata_search": True,
        "lookup": True,
        "reference_formats": ["arxiv_id"],
        "fulltext_formats": ["pdf"],
        "download": True,
        "official_api": "https://export.arxiv.org/api/query",
    }
    assert capabilities["biorxiv"] == {
        "name": "biorxiv",
        "search": "doi_only",
        "metadata_search": False,
        "lookup": True,
        "reference_formats": ["doi"],
        "fulltext_formats": ["pdf"],
        "download": True,
        "official_api": "https://api.biorxiv.org/details/biorxiv",
    }
