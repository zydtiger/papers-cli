from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from papers_cli.errors import PapersError
from papers_cli.sources import (
    PMC_CLOUD_HOST,
    ArxivAdapter,
    BiorxivAdapter,
    PmcAdapter,
    infer_adapter,
    source_capabilities,
)

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


def test_pmc_lookup_uses_converter_then_cloud_metadata_and_all_format_targets() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "pmc.ncbi.nlm.nih.gov":
            assert dict(request.url.params) == {
                "ids": "PMC3531190",
                "format": "json",
                "versions": "yes",
                "showaiid": "yes",
                "tool": "papers_cli",
            }
            return httpx.Response(200, content=(FIXTURES / "pmc-idconv-current.json").read_bytes())
        assert request.url.host == PMC_CLOUD_HOST
        assert request.url.path == "/metadata/PMC3531190.1.json"
        return httpx.Response(
            200, content=(FIXTURES / "pmc-metadata-all-formats.json").read_bytes()
        )

    with client_for(handler) as client:
        paper = PmcAdapter().lookup("pmc:PMC3531190", client)

    assert [request.url.host for request in requests] == [
        "pmc.ncbi.nlm.nih.gov",
        PMC_CLOUD_HOST,
    ]
    assert paper.ref == "pmc:PMC3531190"
    assert paper.source_version == "1"
    assert paper.pmcid == "PMC3531190"
    assert paper.pmid == "23193287"
    assert paper.license_code == "CC BY-NC"
    assert paper.fulltext_availability == "available"
    assert paper.landing_url == "https://pmc.ncbi.nlm.nih.gov/articles/PMC3531190/"
    assert paper.content_urls == {
        "pdf": "https://pmc-oa-opendata.s3.amazonaws.com/PMC3531190.1/PMC3531190.1.pdf",
        "txt": "https://pmc-oa-opendata.s3.amazonaws.com/PMC3531190.1/PMC3531190.1.txt",
        "xml": "https://pmc-oa-opendata.s3.amazonaws.com/PMC3531190.1/PMC3531190.1.xml",
    }
    for format in ("pdf", "txt", "xml"):
        target = PmcAdapter().download_target(paper, format)
        assert target.allowed_hosts == frozenset({PMC_CLOUD_HOST})
        assert target.url == paper.content_urls[format]
        assert target.provider == "pmc"


def test_pmc_explicit_version_selects_requested_cloud_metadata() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "pmc.ncbi.nlm.nih.gov":
            payload = json.loads((FIXTURES / "pmc-idconv-current.json").read_text())
            versions = payload["records"][0]["versions"]
            assert isinstance(versions, list)
            versions.append({"pmcid": "PMC3531190.2", "current": True, "live": True})
            versions[0]["current"] = False
            return httpx.Response(200, json=payload)
        assert request.url.path == "/metadata/PMC3531190.1.json"
        return httpx.Response(
            200, content=(FIXTURES / "pmc-metadata-all-formats.json").read_bytes()
        )

    with client_for(handler) as client:
        paper = PmcAdapter().lookup("PMC3531190.1", client)
    assert paper.source_key == "PMC3531190"
    assert paper.source_version == "1"


def test_pmc_unversioned_lookup_uses_converter_current_version() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "pmc.ncbi.nlm.nih.gov":
            payload = json.loads((FIXTURES / "pmc-idconv-current.json").read_text())
            versions = payload["records"][0]["versions"]
            assert isinstance(versions, list)
            versions.append({"pmcid": "PMC3531190.2", "current": True, "live": True})
            versions[0]["current"] = False
            return httpx.Response(200, json=payload)
        assert request.url.path == "/metadata/PMC3531190.2.json"
        metadata = json.loads((FIXTURES / "pmc-metadata-all-formats.json").read_text())
        metadata["version"] = 2
        for field in ("pdf_url", "text_url", "xml_url"):
            metadata[field] = metadata[field].replace("PMC3531190.1", "PMC3531190.2")
        return httpx.Response(200, json=metadata)

    with client_for(handler) as client:
        paper = PmcAdapter().lookup("PMC3531190", client)
    assert paper.source_version == "2"


def test_pmc_metadata_without_pdf_reports_available_text_and_xml() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "pmc.ncbi.nlm.nih.gov":
            payload = json.loads((FIXTURES / "pmc-idconv-current.json").read_text())
            record = payload["records"][0]
            assert isinstance(record, dict)
            record["pmcid"] = "PMC6817404"
            record["pmid"] = "31567725"
            record["versions"] = [{"pmcid": "PMC6817404.1", "current": True, "live": True}]
            return httpx.Response(200, json=payload)
        return httpx.Response(200, content=(FIXTURES / "pmc-metadata-text-xml.json").read_bytes())

    with client_for(handler) as client:
        paper = PmcAdapter().lookup("PMC6817404", client)
        target = PmcAdapter().download_target(paper, "txt")
        with pytest.raises(PapersError) as error:
            PmcAdapter().download_target(paper, "pdf")

    assert target.url.endswith("/PMC6817404.1.txt")
    assert paper.pdf_url is None
    assert set(paper.content_urls) == {"txt", "xml"}
    assert error.value.code == "format_unavailable"
    assert error.value.details == {
        "availability": "known",
        "available_formats": ["txt", "xml"],
        "ref": "pmc:PMC6817404",
        "requested_format": "pdf",
    }


def test_pmc_metadata_with_confirmed_missing_files_reports_known_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "pmc.ncbi.nlm.nih.gov":
            return httpx.Response(200, content=(FIXTURES / "pmc-idconv-current.json").read_bytes())
        payload = json.loads((FIXTURES / "pmc-metadata-all-formats.json").read_text())
        payload.update({"pdf_url": None, "text_url": None, "xml_url": None})
        return httpx.Response(200, json=payload)

    with client_for(handler) as client:
        paper = PmcAdapter().lookup("PMC3531190", client)
        with pytest.raises(PapersError) as error:
            PmcAdapter().download_target(paper, "pdf")

    assert paper.content_urls == {}
    assert paper.fulltext_availability == "unavailable"
    assert error.value.details["availability"] == "known"
    assert error.value.details["available_formats"] == []


def test_pmc_live_false_uses_esummary_without_fulltext_targets() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "pmc.ncbi.nlm.nih.gov":
            return httpx.Response(200, content=(FIXTURES / "pmc-idconv-embargo.json").read_bytes())
        assert request.url.host == "eutils.ncbi.nlm.nih.gov"
        assert request.url.params["db"] == "pmc"
        assert request.url.params["id"] == "9999999"
        return httpx.Response(200, content=(FIXTURES / "pmc-esummary.json").read_bytes())

    with client_for(handler) as client:
        paper = PmcAdapter().lookup("PMC9999999", client)
        with pytest.raises(PapersError) as error:
            PmcAdapter().download_target(paper, "xml")

    assert paper.title == "Embargoed PMC article"
    assert paper.content_urls == {}
    assert paper.fulltext_availability == "unavailable"
    assert error.value.details["availability"] == "known"
    assert error.value.details["available_formats"] == []


@pytest.mark.parametrize(
    ("response", "code"),
    [
        (httpx.Response(404), "not_found"),
        (httpx.Response(403), "source_access"),
        (httpx.Response(503), "source_network"),
        (httpx.Response(200, content=b"not json"), "source_protocol"),
    ],
)
def test_pmc_converter_errors_are_structured(response: httpx.Response, code: str) -> None:
    with client_for(lambda _: response) as client:
        with pytest.raises(PapersError) as error:
            PmcAdapter().lookup("PMC3531190", client)
    assert error.value.code == code


def test_pmc_missing_cloud_metadata_uses_esummary_with_unknown_availability() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "pmc.ncbi.nlm.nih.gov":
            payload = json.loads((FIXTURES / "pmc-idconv-embargo.json").read_text())
            payload["records"][0]["versions"][0]["live"] = True
            return httpx.Response(200, json=payload)
        if request.url.host == PMC_CLOUD_HOST:
            return httpx.Response(404)
        return httpx.Response(200, content=(FIXTURES / "pmc-esummary.json").read_bytes())

    with client_for(handler) as client:
        paper = PmcAdapter().lookup("PMC9999999", client)
        with pytest.raises(PapersError) as error:
            PmcAdapter().download_target(paper, "pdf")
    assert paper.fulltext_availability == "unknown"
    assert error.value.details["availability"] == "unknown"


@pytest.mark.parametrize(
    ("response", "code"),
    [
        (httpx.Response(403), "source_access"),
        (httpx.Response(503), "source_network"),
        (httpx.Response(200, content=b"not json"), "source_protocol"),
    ],
)
def test_pmc_cloud_errors_are_structured(response: httpx.Response, code: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "pmc.ncbi.nlm.nih.gov":
            return httpx.Response(200, content=(FIXTURES / "pmc-idconv-current.json").read_bytes())
        return response

    with client_for(handler) as client:
        with pytest.raises(PapersError) as error:
            PmcAdapter().lookup("PMC3531190", client)
    assert error.value.code == code


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


def test_unqualified_pmcid_remains_a_supported_remote_reference() -> None:
    adapter, raw = infer_adapter("PMC3531190.1")
    assert adapter.source == "pmc"
    assert raw == "PMC3531190.1"


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
    assert capabilities["pmc"] == {
        "name": "pmc",
        "search": False,
        "metadata_search": False,
        "lookup": True,
        "reference_formats": ["pmcid"],
        "fulltext_formats": ["pdf", "txt", "xml"],
        "download": True,
        "official_api": "https://pmc.ncbi.nlm.nih.gov/tools/idconv/api/v1/articles/",
    }
