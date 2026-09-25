from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

import papers_cli.sources as sources
from papers_cli.errors import PapersError
from papers_cli.sources import (
    CROSSREF_API,
    PMC_CLOUD_HOST,
    PUBMED_ESEARCH_API,
    ArxivAdapter,
    BiorxivAdapter,
    CrossrefAdapter,
    PmcAdapter,
    PubmedAdapter,
    infer_adapter,
    normalize_doi,
    normalize_pmid,
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
        assert target.accepted_content_types == frozenset({"binary/octet-stream"})
        assert target.url == paper.content_urls[format]
        assert target.provider == "pmc"
        assert target.source_version == "1"


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
    ("reference", "expected"),
    [
        ("doi:10.1000/example", "10.1000/example"),
        ("10.1000/example", "10.1000/example"),
        (
            "10.1002/(SICI)1099-0844(199612)12:4<290::AID-CBF4>3.0.CO;2-P",
            "10.1002/(sici)1099-0844(199612)12:4<290::aid-cbf4>3.0.co;2-p",
        ),
        ("https://doi.org/10.1000%2FExample?tracking=1#details", "10.1000/example"),
    ],
)
def test_generic_doi_infers_crossref_and_normalizes(reference: str, expected: str) -> None:
    adapter, raw = infer_adapter(reference)
    assert adapter.source == "crossref"
    assert adapter.normalize_ref(raw) == expected


def test_doi_url_decodes_path_once_and_raw_suffix_keeps_query_characters() -> None:
    assert normalize_doi("https://doi.org/10.1000/%252Fexample") == "10.1000/%2fexample"
    assert normalize_doi("10.1000/example?literal#suffix") == "10.1000/example?literal#suffix"


def test_crossref_lookup_uses_crossref_metadata_then_mapped_pmc_fulltext() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "api.crossref.org":
            assert str(request.url).startswith(f"{CROSSREF_API}/10.1093%2Fnar%2Fgks1195")
            return httpx.Response(
                200, content=(FIXTURES / "crossref-work-mapped.json").read_bytes()
            )
        if request.url.host == "pmc.ncbi.nlm.nih.gov":
            assert dict(request.url.params) == {
                "ids": "10.1093/nar/gks1195",
                "format": "json",
                "versions": "yes",
                "showaiid": "yes",
                "tool": "papers_cli",
            }
            return httpx.Response(200, content=(FIXTURES / "pmc-idconv-current.json").read_bytes())
        assert request.url.host == PMC_CLOUD_HOST
        return httpx.Response(
            200, content=(FIXTURES / "pmc-metadata-all-formats.json").read_bytes()
        )

    with client_for(handler) as client:
        paper = CrossrefAdapter(PmcAdapter()).lookup("crossref:10.1093/nar/gks1195", client)

    assert [request.url.host for request in requests] == [
        "api.crossref.org",
        "pmc.ncbi.nlm.nih.gov",
        PMC_CLOUD_HOST,
    ]
    assert paper.ref == "crossref:10.1093/nar/gks1195"
    assert paper.title == "GenBank"
    assert paper.authors == ["David J. Benson"]
    assert paper.abstract == "The GenBank nucleotide sequence database."
    assert paper.published_at == "2012-11-27"
    assert paper.content_urls.keys() == {"pdf", "txt", "xml"}
    target = CrossrefAdapter(PmcAdapter()).download_target(paper, "xml")
    assert target.provider == "pmc"
    assert target.source_version == "1"
    assert target.allowed_hosts == frozenset({PMC_CLOUD_HOST})


def test_crossref_title_flattens_inline_markup_without_inserting_spaces() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.crossref.org"
        return httpx.Response(
            200, content=(FIXTURES / "crossref-work-inline-title.json").read_bytes()
        )

    with client_for(handler) as client:
        paper = CrossrefAdapter(PmcAdapter())._metadata_paper(
            "10.1021/acsbiomaterials.0c00271", client
        )

    assert paper.title == (
        "Efficiency of Cytosolic Delivery with Poly(β-amino ester) Nanoparticles is "
        "Dependent on the Effective pKa of the Polymer"
    )
    assert CrossrefAdapter._title("p<i>K</i><sub>a</sub> &amp; delivery") == "pKa & delivery"


def test_crossref_lookup_without_pmc_keeps_metadata_and_reports_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.crossref.org":
            return httpx.Response(
                200, content=(FIXTURES / "crossref-work-no-pmc.json").read_bytes()
            )
        assert request.url.host == "pmc.ncbi.nlm.nih.gov"
        return httpx.Response(200, content=(FIXTURES / "pmc-idconv-doi-missing.json").read_bytes())

    with client_for(handler) as client:
        paper = CrossrefAdapter(PmcAdapter()).lookup("10.1145/3377811.3380366", client)
        with pytest.raises(PapersError) as error:
            CrossrefAdapter(PmcAdapter()).download_target(paper, "pdf")

    assert paper.ref == "crossref:10.1145/3377811.3380366"
    assert paper.title == "Improving data scientist efficiency with provenance"
    assert paper.content_urls == {}
    assert paper.fulltext_availability == "unavailable"
    assert error.value.code == "format_unavailable"
    assert error.value.details["availability"] == "known"
    assert error.value.details["available_formats"] == []


def test_crossref_mapping_access_failure_keeps_metadata_with_unknown_availability() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.crossref.org":
            return httpx.Response(
                200, content=(FIXTURES / "crossref-work-no-pmc.json").read_bytes()
            )
        assert request.url.host == "pmc.ncbi.nlm.nih.gov"
        return httpx.Response(403)

    with client_for(handler) as client:
        paper = CrossrefAdapter(PmcAdapter()).lookup("10.1145/3377811.3380366", client)
        with pytest.raises(PapersError) as error:
            CrossrefAdapter(PmcAdapter()).download_target(paper, "pdf")

    assert paper.content_urls == {}
    assert paper.fulltext_availability == "unknown"
    assert error.value.code == "format_unavailable"
    assert error.value.details["availability"] == "unknown"


def test_crossref_not_found_is_scoped_to_crossref() -> None:
    with client_for(lambda _: httpx.Response(404)) as client:
        with pytest.raises(PapersError) as error:
            CrossrefAdapter(PmcAdapter()).lookup("10.1000/missing", client)
    assert error.value.code == "not_found"
    assert str(error.value) == "DOI was not found in Crossref"


def test_crossref_network_error_is_structured() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    with client_for(handler) as client:
        with pytest.raises(PapersError) as error:
            CrossrefAdapter(PmcAdapter()).lookup("10.1000/example", client)
    assert error.value.code == "source_network"


def test_crossref_rejects_invalid_metadata_and_unknown_pmc_record_errors() -> None:
    with client_for(lambda _: httpx.Response(200, json={"message": {}})) as client:
        with pytest.raises(PapersError) as error:
            CrossrefAdapter(PmcAdapter()).lookup("10.1000/example", client)
    assert error.value.code == "source_protocol"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.crossref.org":
            payload = json.loads((FIXTURES / "crossref-work-no-pmc.json").read_text())
            payload["message"]["DOI"] = "10.1000/example"
            return httpx.Response(200, json=payload)
        return httpx.Response(
            200,
            json={"records": [{"status": "error", "errmsg": "Unexpected converter error"}]},
        )

    with client_for(handler) as client:
        with pytest.raises(PapersError) as error:
            CrossrefAdapter(PmcAdapter()).lookup("10.1000/example", client)
    assert error.value.code == "source_protocol"


def test_pubmed_search_uses_esearch_then_one_batched_summary_in_result_order() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/esearch.fcgi"):
            assert str(request.url) == (
                f"{PUBMED_ESEARCH_API}?db=pubmed&term=GenBank&retmode=json&retmax=2&tool=papers_cli"
            )
            return httpx.Response(200, content=(FIXTURES / "pubmed-esearch.json").read_bytes())
        assert request.url.path.endswith("/esummary.fcgi")
        assert dict(request.url.params) == {
            "db": "pubmed",
            "id": "23193287,31567725",
            "retmode": "json",
            "tool": "papers_cli",
        }
        return httpx.Response(200, content=(FIXTURES / "pubmed-esummary.json").read_bytes())

    with client_for(handler) as client:
        papers = PubmedAdapter(PmcAdapter()).search("GenBank", 2, client)

    assert [request.url.path.rsplit("/", 1)[-1] for request in requests] == [
        "esearch.fcgi",
        "esummary.fcgi",
    ]
    assert [paper.ref for paper in papers] == ["pubmed:23193287", "pubmed:31567725"]
    assert papers[0].authors == ["Benson DA", "Karsch-Mizrachi I"]
    assert papers[0].abstract == "GenBank is a nucleotide sequence database."
    assert papers[0].doi == "10.1093/nar/gks1195"
    assert papers[0].pmcid == "PMC3531190"
    assert all(paper.fulltext_availability == "unknown" for paper in papers)


def test_pubmed_search_empty_result_skips_summary_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/esearch.fcgi")
        return httpx.Response(200, json={"esearchresult": {"idlist": []}})

    with client_for(handler) as client:
        assert PubmedAdapter(PmcAdapter()).search("absent", 2, client) == []


def test_pubmed_search_enforces_requested_limit_before_batched_summary() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/esearch.fcgi"):
            return httpx.Response(200, content=(FIXTURES / "pubmed-esearch.json").read_bytes())
        assert request.url.params["id"] == "23193287"
        payload = json.loads((FIXTURES / "pubmed-esummary.json").read_text())
        result = payload["result"]
        assert isinstance(result, dict)
        result["uids"] = ["23193287"]
        result.pop("31567725")
        return httpx.Response(200, json=payload)

    with client_for(handler) as client:
        papers = PubmedAdapter(PmcAdapter()).search("GenBank", 1, client)
    assert [paper.ref for paper in papers] == ["pubmed:23193287"]


def test_pubmed_lookup_keeps_pubmed_metadata_and_maps_pmc_fulltext() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/esummary.fcgi"):
            return httpx.Response(200, content=(FIXTURES / "pubmed-esummary.json").read_bytes())
        if request.url.host == "pmc.ncbi.nlm.nih.gov":
            assert request.url.params["ids"] == "23193287"
            return httpx.Response(
                200, content=(FIXTURES / "pmc-idconv-pmid-current.json").read_bytes()
            )
        assert request.url.host == PMC_CLOUD_HOST
        return httpx.Response(
            200, content=(FIXTURES / "pmc-metadata-all-formats.json").read_bytes()
        )

    adapter = PubmedAdapter(PmcAdapter())
    with client_for(handler) as client:
        paper = adapter.lookup("pmid:23193287", client)

    assert [request.url.host for request in requests] == [
        "eutils.ncbi.nlm.nih.gov",
        "pmc.ncbi.nlm.nih.gov",
        PMC_CLOUD_HOST,
    ]
    assert paper.ref == "pubmed:23193287"
    assert paper.source_version is None
    assert paper.title == "GenBank"
    assert paper.abstract == "GenBank is a nucleotide sequence database."
    assert paper.published_at == "2013 Jan"
    assert paper.doi == "10.1093/nar/gks1195"
    assert paper.pmcid == "PMC3531190"
    assert paper.content_urls.keys() == {"pdf", "txt", "xml"}
    target = adapter.download_target(paper, "txt")
    assert target.provider == "pmc"
    assert target.source_version == "1"
    assert target.url.endswith("/PMC3531190.1/PMC3531190.1.txt")


def test_pubmed_lookup_without_pmc_keeps_metadata_and_reports_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/esummary.fcgi"):
            return httpx.Response(
                200, content=(FIXTURES / "pubmed-esummary-no-pmc.json").read_bytes()
            )
        assert request.url.host == "pmc.ncbi.nlm.nih.gov"
        return httpx.Response(200, content=(FIXTURES / "pmc-idconv-pmid-missing.json").read_bytes())

    adapter = PubmedAdapter(PmcAdapter())
    with client_for(handler) as client:
        paper = adapter.lookup("99999999", client)
        with pytest.raises(PapersError) as error:
            adapter.download_target(paper, "pdf")

    assert paper.ref == "pubmed:99999999"
    assert paper.title == "Metadata-only PubMed fixture"
    assert paper.content_urls == {}
    assert paper.fulltext_availability == "unavailable"
    assert error.value.code == "format_unavailable"
    assert error.value.details["availability"] == "known"


def test_pubmed_lookup_missing_summary_record_is_not_found() -> None:
    with client_for(
        lambda _: httpx.Response(
            200, content=(FIXTURES / "pubmed-esummary-missing.json").read_bytes()
        )
    ) as client:
        with pytest.raises(PapersError) as error:
            PubmedAdapter(PmcAdapter()).lookup("999999999", client)
    assert error.value.code == "not_found"


@pytest.mark.parametrize("reference", ["pmid:0", "pmid:not-a-number", "pubmed:001"])
def test_pubmed_rejects_invalid_pmid_without_request(reference: str) -> None:
    with client_for(lambda _: pytest.fail("invalid PMID must not request PubMed")) as client:
        with pytest.raises(PapersError) as error:
            PubmedAdapter(PmcAdapter()).lookup(reference, client)
    assert error.value.code == "invalid_ref"


def test_pubmed_retries_esearch_and_rejects_invalid_summary_ids(monkeypatch) -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(503)
        return httpx.Response(200, json={"esearchresult": {"idlist": ["bad"]}})

    monkeypatch.setattr(sources.time, "sleep", lambda _: None)
    with client_for(handler) as client:
        with pytest.raises(PapersError) as error:
            PubmedAdapter(PmcAdapter()).search("GenBank", 2, client)
    assert attempts == 2
    assert error.value.code == "source_protocol"


def test_pubmed_title_normalization_matches_crossref_inline_title_handling() -> None:
    record: dict[str, object] = {
        "uid": "23193287",
        "title": "Effective p<i>K</i><sub>a</sub> &amp; delivery",
        "authors": [],
        "articleids": [],
    }
    paper = PubmedAdapter(PmcAdapter())._paper_from_summary("23193287", record)
    assert paper.title == "Effective pKa & delivery"


def test_pmid_reference_infers_pubmed_and_normalizes() -> None:
    adapter, raw = infer_adapter("pmid:23193287")
    assert adapter.source == "pubmed"
    assert adapter.normalize_ref(raw) == "23193287"
    source_qualified, raw = infer_adapter("pubmed:23193287")
    assert source_qualified.source == "pubmed"
    assert raw == "23193287"
    assert normalize_pmid("pmid:23193287") == "23193287"


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
    assert capabilities["crossref"] == {
        "name": "crossref",
        "search": False,
        "metadata_search": False,
        "lookup": True,
        "reference_formats": ["doi"],
        "fulltext_formats": [],
        "download": True,
        "delivery_source": "pmc",
        "delivery_formats": ["pdf", "txt", "xml"],
        "official_api": "https://api.crossref.org/v1/works",
    }
    assert capabilities["pubmed"] == {
        "name": "pubmed",
        "search": True,
        "metadata_search": True,
        "lookup": True,
        "reference_formats": ["pmid"],
        "fulltext_formats": [],
        "download": True,
        "delivery_source": "pmc",
        "delivery_formats": ["pdf", "txt", "xml"],
        "official_api": "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
    }
