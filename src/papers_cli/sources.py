from __future__ import annotations

import re
import time
from collections.abc import Iterable
from dataclasses import replace
from html import unescape
from typing import Protocol
from urllib.parse import quote, unquote, urlparse, urlsplit
from xml.etree.ElementTree import Element

import httpx
from defusedxml import ElementTree

from .errors import PapersError
from .models import DownloadTarget, RemotePaper, content_media_type

ARXIV_API = "https://export.arxiv.org/api/query"
BIORXIV_API = "https://api.biorxiv.org/details/biorxiv"
CROSSREF_API = "https://api.crossref.org/v1/works"
PMC_ID_CONVERTER_API = "https://pmc.ncbi.nlm.nih.gov/tools/idconv/api/v1/articles/"
PMC_ESUMMARY_API = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
PUBMED_ESEARCH_API = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
PUBMED_ESUMMARY_API = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
PMC_CLOUD_HOST = "pmc-oa-opendata.s3.amazonaws.com"
PMC_CLOUD_API = f"https://{PMC_CLOUD_HOST}"
PMC_ARTICLE_URL = "https://pmc.ncbi.nlm.nih.gov/articles"
PMC_OPAQUE_CONTENT_TYPES = frozenset({"binary/octet-stream"})
ARXIV_ID = re.compile(
    r"^(?P<id>\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Z]{2})?/\d{7})(?:v(?P<version>\d+))?$", re.I
)
# A DOI suffix is deliberately broad: historical valid DOIs include punctuation
# outside the bioRxiv-specific pattern. The bioRxiv adapter accepts the narrower
# 10.1101 prefix.
GENERIC_DOI = re.compile(r"^10\.\d{4,9}/\S+$", re.I)
BIORXIV_DOI = re.compile(r"^10\.1101/[A-Za-z0-9._;()/:+-]+$", re.I)
PMCID = re.compile(r"^(?P<id>PMC\d+)(?:\.(?P<version>\d+))?$", re.I)
PMID = re.compile(r"^[1-9]\d*$")
DOI_PERCENT_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")
DOI_URI_HOSTS = frozenset({"doi.org", "www.doi.org", "dx.doi.org"})
ATOM = "{http://www.w3.org/2005/Atom}"
ARXIV = "{http://arxiv.org/schemas/atom}"


class SourceAdapter(Protocol):
    source: str
    allowed_hosts: frozenset[str]

    def normalize_ref(self, raw: str) -> str: ...

    def lookup(self, raw: str, client: httpx.Client) -> RemotePaper: ...

    def search(self, query: str, limit: int, client: httpx.Client) -> list[RemotePaper]: ...

    def download_target(self, paper: RemotePaper, format: str) -> DownloadTarget: ...


def _without_source_prefix(raw: str, source: str) -> str:
    """Remove an optional source prefix without making it case-sensitive."""
    candidate = raw.strip()
    prefix = f"{source}:"
    if candidate.lower().startswith(prefix):
        return candidate[len(prefix) :].strip()
    return candidate


def normalize_doi(raw: str) -> str:
    """Return a canonical DOI from identifier, doi: form, or doi.org URI."""
    candidate = raw.strip()
    if candidate.lower().startswith("doi:"):
        candidate = candidate[4:].strip()
    if candidate.lower().startswith(("https://", "http://")):
        parsed = urlsplit(candidate)
        if (
            parsed.scheme.lower() not in {"http", "https"}
            or parsed.netloc.lower() not in DOI_URI_HOSTS
            or not parsed.path.startswith("/")
            or parsed.path.startswith("//")
            or DOI_PERCENT_ESCAPE.search(parsed.path)
        ):
            raise PapersError("invalid_ref", "Expected a valid DOI or doi.org URL", exit_code=2)
        try:
            candidate = unquote(parsed.path[1:], encoding="utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise PapersError(
                "invalid_ref", "Expected a valid DOI or doi.org URL", exit_code=2
            ) from exc
    if not GENERIC_DOI.fullmatch(candidate):
        raise PapersError("invalid_ref", "Expected a valid DOI", exit_code=2)
    return candidate.lower()


def normalize_pmid(raw: str) -> str:
    """Return a canonical PMID from an identifier or pmid: reference."""
    candidate = raw.strip()
    if candidate.lower().startswith("pmid:"):
        candidate = candidate[5:].strip()
    if not PMID.fullmatch(candidate):
        raise PapersError("invalid_ref", "Expected a numeric PMID", exit_code=2)
    return candidate


def _download_target(
    paper: RemotePaper,
    format: str,
    allowed_hosts: frozenset[str],
    accepted_content_types: frozenset[str] = frozenset(),
) -> DownloadTarget:
    url = paper.content_urls.get(format)
    if url is None and format == "pdf":
        url = paper.pdf_url
    if url is None:
        available_formats = sorted(
            set(paper.content_urls) | ({"pdf"} if paper.pdf_url is not None else set())
        )
        availability = (
            "known"
            if available_formats or paper.fulltext_availability == "unavailable"
            else "unknown"
        )
        raise PapersError(
            "format_unavailable",
            f"{paper.source} does not provide {format} full text for {paper.ref}",
            exit_code=3,
            details={
                "ref": paper.ref,
                "requested_format": format,
                "available_formats": available_formats,
                "availability": availability,
            },
        )
    return DownloadTarget(
        format,
        url,
        allowed_hosts,
        content_media_type(format),
        paper.source,
        accepted_content_types,
    )


def _pmc_cloud_source_version(url: str, format: str) -> str:
    parsed = urlparse(url)
    parts = parsed.path.split("/")
    if (
        parsed.scheme != "https"
        or parsed.netloc != PMC_CLOUD_HOST
        or len(parts) != 3
        or parts[0]
        or not PMCID.fullmatch(parts[1])
        or parts[2] != f"{parts[1]}.{format}"
    ):
        raise PapersError("storage_corrupt", "Stored PMC full-text URL is invalid", exit_code=5)
    match = PMCID.fullmatch(parts[1])
    assert match is not None
    version = match.group("version")
    if version is None:
        raise PapersError("storage_corrupt", "Stored PMC full-text URL is invalid", exit_code=5)
    return version


def _plain_inline_text(value: str) -> str:
    """Flatten inline markup without separating the text it encloses."""
    return " ".join(unescape(re.sub(r"<[^>]*>", "", value)).split())


def _text(element: Element | None) -> str:
    return "" if element is None or element.text is None else " ".join(element.text.split())


def _response_json(response: httpx.Response, source: str) -> dict[str, object]:
    if response.status_code == 404:
        raise PapersError("not_found", f"No {source} record was found", exit_code=3)
    if response.status_code >= 400:
        raise PapersError(
            "source_network",
            f"{source} metadata request failed with HTTP {response.status_code}",
            exit_code=4,
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise PapersError(
            "source_protocol", f"{source} returned invalid JSON", exit_code=4
        ) from exc
    if not isinstance(payload, dict):
        raise PapersError(
            "source_protocol", f"{source} returned an unexpected JSON payload", exit_code=4
        )
    return payload


class ArxivAdapter:
    source = "arxiv"
    allowed_hosts = frozenset({"export.arxiv.org", "arxiv.org"})

    def normalize_ref(self, raw: str) -> str:
        candidate = _without_source_prefix(raw, self.source)
        match = ARXIV_ID.fullmatch(candidate)
        if not match:
            raise PapersError("invalid_ref", "Expected a valid arXiv identifier", exit_code=2)
        return match.group("id").lower()

    def _parse_entry(self, entry: Element) -> RemotePaper:
        identifier = _text(entry.find(f"{ATOM}id")).rsplit("/", 1)[-1]
        match = ARXIV_ID.fullmatch(identifier)
        if not match:
            raise PapersError(
                "source_protocol", "arXiv returned an invalid entry identifier", exit_code=4
            )
        source_key = match.group("id").lower()
        version = match.group("version")
        pdf_link = next(
            (
                link.attrib.get("href")
                for link in entry.findall(f"{ATOM}link")
                if link.attrib.get("title") == "pdf"
            ),
            None,
        )
        if not pdf_link:
            suffix = f"v{version}" if version else ""
            pdf_link = f"https://arxiv.org/pdf/{source_key}{suffix}"
        authors = [_text(author.find(f"{ATOM}name")) for author in entry.findall(f"{ATOM}author")]
        categories = [
            category.attrib["term"]
            for category in entry.findall(f"{ATOM}category")
            if "term" in category.attrib
        ]
        doi = _text(entry.find(f"{ARXIV}doi")) or None
        landing_url = f"https://arxiv.org/abs/{source_key}{f'v{version}' if version else ''}"
        return RemotePaper(
            source=self.source,
            source_key=source_key,
            source_version=version,
            title=_text(entry.find(f"{ATOM}title")),
            abstract=_text(entry.find(f"{ATOM}summary")),
            authors=authors,
            categories=categories,
            published_at=_text(entry.find(f"{ATOM}published")) or None,
            updated_at=_text(entry.find(f"{ATOM}updated")) or None,
            doi=doi.lower() if doi else None,
            landing_url=landing_url,
            pdf_url=pdf_link,
            content_urls={"pdf": pdf_link},
        )

    def download_target(self, paper: RemotePaper, format: str) -> DownloadTarget:
        return _download_target(paper, format, self.allowed_hosts)

    def _parse(self, body: bytes) -> list[RemotePaper]:
        try:
            root = ElementTree.fromstring(body)
        except ElementTree.ParseError as exc:
            raise PapersError(
                "source_protocol", "arXiv returned invalid Atom XML", exit_code=4
            ) from exc
        return [self._parse_entry(entry) for entry in root.findall(f"{ATOM}entry")]

    def _query(self, params: dict[str, str | int], client: httpx.Client) -> list[RemotePaper]:
        try:
            response = client.get(ARXIV_API, params=params)
        except httpx.HTTPError as exc:
            raise PapersError(
                "source_network", f"arXiv metadata request failed: {exc}", exit_code=4
            ) from exc
        if response.status_code >= 400:
            raise PapersError(
                "source_network",
                f"arXiv metadata request failed with HTTP {response.status_code}",
                exit_code=4,
            )
        return self._parse(response.content)

    def lookup(self, raw: str, client: httpx.Client) -> RemotePaper:
        records = self._query({"id_list": self.normalize_ref(raw)}, client)
        if not records:
            raise PapersError("not_found", "No arXiv record was found", exit_code=3)
        return records[0]

    def search(self, query: str, limit: int, client: httpx.Client) -> list[RemotePaper]:
        if not query.strip():
            raise PapersError("invalid_query", "Search query must not be empty", exit_code=2)
        return self._query(
            {"search_query": f"all:{query}", "start": 0, "max_results": limit}, client
        )


class BiorxivAdapter:
    source = "biorxiv"
    allowed_hosts = frozenset({"api.biorxiv.org", "www.biorxiv.org"})

    def normalize_ref(self, raw: str) -> str:
        candidate = _without_source_prefix(raw, self.source)
        if not BIORXIV_DOI.fullmatch(candidate):
            raise PapersError(
                "invalid_ref", "Expected a bioRxiv DOI beginning with 10.1101/", exit_code=2
            )
        return candidate.lower()

    def _parse_record(self, record: dict[str, object]) -> RemotePaper:
        raw_doi = record.get("doi")
        if not isinstance(raw_doi, str):
            raise PapersError("source_protocol", "bioRxiv response lacks a DOI", exit_code=4)
        doi = self.normalize_ref(raw_doi)
        version = str(record.get("version") or "") or None
        if not version or not version.isdigit():
            raise PapersError(
                "source_protocol", "bioRxiv response lacks a valid version", exit_code=4
            )
        title = record.get("title")
        if not isinstance(title, str) or not title.strip():
            raise PapersError("source_protocol", "bioRxiv response lacks a title", exit_code=4)
        authors_value = record.get("authors", "")
        authors = (
            [part.strip() for part in authors_value.split(";") if part.strip()]
            if isinstance(authors_value, str)
            else []
        )
        category = record.get("category")
        date = record.get("date")
        return RemotePaper(
            source=self.source,
            source_key=doi,
            source_version=version,
            title=" ".join(title.split()),
            abstract=" ".join(str(record.get("abstract", "")).split()),
            authors=authors,
            categories=[category] if isinstance(category, str) and category else [],
            published_at=date if isinstance(date, str) else None,
            updated_at=date if isinstance(date, str) else None,
            doi=doi,
            landing_url=f"https://www.biorxiv.org/content/{doi}v{version}",
            pdf_url=f"https://www.biorxiv.org/content/{doi}v{version}.full.pdf",
            content_urls={"pdf": f"https://www.biorxiv.org/content/{doi}v{version}.full.pdf"},
        )

    def download_target(self, paper: RemotePaper, format: str) -> DownloadTarget:
        return _download_target(paper, format, self.allowed_hosts)

    def lookup(self, raw: str, client: httpx.Client) -> RemotePaper:
        doi = self.normalize_ref(raw)
        try:
            response = client.get(f"{BIORXIV_API}/{doi}/na/json")
        except httpx.HTTPError as exc:
            raise PapersError(
                "source_network", f"bioRxiv metadata request failed: {exc}", exit_code=4
            ) from exc
        payload = _response_json(response, "bioRxiv")
        collection = payload.get("collection")
        if not isinstance(collection, list) or not collection:
            raise PapersError("not_found", "No bioRxiv record was found", exit_code=3)
        first = collection[0]
        if not isinstance(first, dict):
            raise PapersError("source_protocol", "bioRxiv returned an invalid record", exit_code=4)
        return self._parse_record(first)

    def search(self, query: str, limit: int, client: httpx.Client) -> list[RemotePaper]:
        # bioRxiv's official API is DOI/detail oriented, not a general-search API.
        stripped_query = query.strip()
        candidate = _without_source_prefix(stripped_query, self.source)
        if stripped_query.lower().startswith("biorxiv:") or candidate.lower().startswith("10."):
            # Preserve the historical DOI lookup convenience while making a DOI for
            # another publisher an invalid bioRxiv reference rather than a search error.
            return [self.lookup(candidate, client)]
        raise PapersError(
            "unsupported_search",
            "bioRxiv official API search currently supports DOI lookup only",
            exit_code=2,
        )


class PmcAdapter:
    source = "pmc"
    allowed_hosts = frozenset({PMC_CLOUD_HOST})

    def __init__(self) -> None:
        self._last_request_at: float | None = None

    def _parse_ref(self, raw: str) -> tuple[str, str | None]:
        candidate = _without_source_prefix(raw, self.source)
        match = PMCID.fullmatch(candidate)
        if not match:
            raise PapersError(
                "invalid_ref",
                "Expected a PMC identifier such as PMC3531190 or PMC3531190.1",
                exit_code=2,
            )
        return match.group("id").upper(), match.group("version")

    def normalize_ref(self, raw: str) -> str:
        return self._parse_ref(raw)[0]

    def _wait_for_request_slot(self) -> None:
        if self._last_request_at is not None:
            remaining = 1 / 3 - (time.monotonic() - self._last_request_at)
            if remaining > 0:
                time.sleep(remaining)
        self._last_request_at = time.monotonic()

    def _request(
        self,
        client: httpx.Client,
        url: str,
        *,
        params: dict[str, str] | None = None,
        source: str,
    ) -> httpx.Response:
        for attempt in range(3):
            self._wait_for_request_slot()
            try:
                response = client.get(url, params=params)
            except httpx.HTTPError as exc:
                if attempt < 2:
                    time.sleep(0.25 * (2**attempt))
                    continue
                raise PapersError(
                    "source_network", f"{source} metadata request failed: {exc}", exit_code=4
                ) from exc
            if response.status_code in {429, 502, 503, 504} and attempt < 2:
                time.sleep(0.25 * (2**attempt))
                continue
            if response.status_code in {401, 403}:
                raise PapersError(
                    "source_access",
                    f"{source} metadata access was restricted with HTTP {response.status_code}",
                    exit_code=4,
                )
            return response
        raise AssertionError("unreachable")

    def _json(
        self,
        client: httpx.Client,
        url: str,
        *,
        params: dict[str, str] | None = None,
        source: str,
    ) -> dict[str, object]:
        response = self._request(client, url, params=params, source=source)
        if response.status_code == 404:
            raise PapersError("not_found", f"No {source} record was found", exit_code=3)
        if response.status_code >= 400:
            raise PapersError(
                "source_network",
                f"{source} metadata request failed with HTTP {response.status_code}",
                exit_code=4,
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise PapersError(
                "source_protocol", f"{source} returned invalid JSON", exit_code=4
            ) from exc
        if not isinstance(payload, dict):
            raise PapersError(
                "source_protocol", f"{source} returned an unexpected JSON payload", exit_code=4
            )
        return payload

    def _converter_record(self, pmcid: str, client: httpx.Client) -> dict[str, object]:
        payload = self._json(
            client,
            PMC_ID_CONVERTER_API,
            params={
                "ids": pmcid,
                "format": "json",
                "versions": "yes",
                "showaiid": "yes",
                "tool": "papers_cli",
            },
            source="PMC ID Converter",
        )
        records = payload.get("records")
        if not isinstance(records, list) or len(records) != 1 or not isinstance(records[0], dict):
            raise PapersError(
                "source_protocol", "PMC ID Converter returned an invalid record", exit_code=4
            )
        record = records[0]
        if record.get("status") == "error":
            raise PapersError("not_found", "No PMC record was found", exit_code=3)
        returned = record.get("pmcid")
        if not isinstance(returned, str) or returned.upper().split(".", 1)[0] != pmcid:
            raise PapersError(
                "source_protocol", "PMC ID Converter returned a mismatched PMCID", exit_code=4
            )
        return record

    def _doi_record(self, doi: str, client: httpx.Client) -> dict[str, object] | None:
        payload = self._json(
            client,
            PMC_ID_CONVERTER_API,
            params={
                "ids": doi,
                "format": "json",
                "versions": "yes",
                "showaiid": "yes",
                "tool": "papers_cli",
            },
            source="PMC ID Converter",
        )
        records = payload.get("records")
        if not isinstance(records, list) or len(records) != 1 or not isinstance(records[0], dict):
            raise PapersError(
                "source_protocol", "PMC ID Converter returned an invalid record", exit_code=4
            )
        record = records[0]
        if record.get("status") == "error":
            if record.get("errmsg") == "Identifier not found in PMC":
                return None
            raise PapersError(
                "source_protocol", "PMC ID Converter could not resolve the DOI", exit_code=4
            )
        returned_doi = record.get("doi")
        if returned_doi is not None and (
            not isinstance(returned_doi, str) or normalize_doi(returned_doi) != doi
        ):
            raise PapersError(
                "source_protocol", "PMC ID Converter returned a mismatched DOI", exit_code=4
            )
        returned = record.get("pmcid")
        if not isinstance(returned, str) or not PMCID.fullmatch(returned):
            raise PapersError(
                "source_protocol", "PMC ID Converter returned an invalid PMCID", exit_code=4
            )
        return record

    def _pmid_record(self, pmid: str, client: httpx.Client) -> dict[str, object] | None:
        payload = self._json(
            client,
            PMC_ID_CONVERTER_API,
            params={
                "ids": pmid,
                "format": "json",
                "versions": "yes",
                "showaiid": "yes",
                "tool": "papers_cli",
            },
            source="PMC ID Converter",
        )
        records = payload.get("records")
        if not isinstance(records, list) or len(records) != 1 or not isinstance(records[0], dict):
            raise PapersError(
                "source_protocol", "PMC ID Converter returned an invalid record", exit_code=4
            )
        record = records[0]
        if record.get("status") == "error":
            if record.get("errmsg") == "Identifier not found in PMC":
                return None
            raise PapersError(
                "source_protocol", "PMC ID Converter could not resolve the PMID", exit_code=4
            )
        returned_pmid = self._optional_identifier(record, "pmid")
        if returned_pmid != pmid:
            raise PapersError(
                "source_protocol", "PMC ID Converter returned a mismatched PMID", exit_code=4
            )
        returned = record.get("pmcid")
        if not isinstance(returned, str) or not PMCID.fullmatch(returned):
            raise PapersError(
                "source_protocol", "PMC ID Converter returned an invalid PMCID", exit_code=4
            )
        return record

    def _version_from_record(
        self, record: dict[str, object], pmcid: str, requested_version: str | None
    ) -> tuple[str, bool | None]:
        versions = record.get("versions")
        if not isinstance(versions, list):
            raise PapersError(
                "source_protocol", "PMC ID Converter returned no version information", exit_code=4
            )
        expected = f"{pmcid}.{requested_version}" if requested_version is not None else None
        selected: dict[str, object] | None = None
        for item in versions:
            if not isinstance(item, dict):
                raise PapersError(
                    "source_protocol", "PMC ID Converter returned an invalid version", exit_code=4
                )
            versioned = item.get("pmcid")
            if not isinstance(versioned, str):
                continue
            if expected is not None and versioned.upper() == expected:
                selected = item
                break
            if expected is None and item.get("current") is True:
                selected = item
                break
        if selected is None:
            raise PapersError("not_found", "No requested PMC version was found", exit_code=3)
        versioned = selected.get("pmcid")
        if not isinstance(versioned, str) or not PMCID.fullmatch(versioned):
            raise PapersError(
                "source_protocol",
                "PMC ID Converter returned an invalid versioned PMCID",
                exit_code=4,
            )
        match = PMCID.fullmatch(versioned)
        assert match is not None
        if match.group("id").upper() != pmcid or match.group("version") is None:
            raise PapersError(
                "source_protocol",
                "PMC ID Converter returned an invalid versioned PMCID",
                exit_code=4,
            )
        live = selected.get("live")
        return versioned.upper(), live if isinstance(live, bool) else None

    @staticmethod
    def _optional_identifier(record: dict[str, object], name: str) -> str | None:
        value = record.get(name)
        if value is None:
            return None
        if isinstance(value, int) or isinstance(value, str):
            normalized = str(value).strip()
            return normalized or None
        raise PapersError("source_protocol", f"PMC metadata has an invalid {name}", exit_code=4)

    @staticmethod
    def _cloud_content_url(value: object, versioned_pmcid: str, format: str) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise PapersError(
                "source_protocol", "PMC metadata has an invalid full-text URL", exit_code=4
            )
        parsed = urlparse(value)
        expected_path = f"/{versioned_pmcid}/{versioned_pmcid}.{format}"
        if (
            parsed.scheme != "s3"
            or parsed.netloc != "pmc-oa-opendata"
            or parsed.path != expected_path
        ):
            raise PapersError(
                "source_protocol", "PMC metadata has an unexpected full-text URL", exit_code=4
            )
        return f"{PMC_CLOUD_API}{parsed.path}"

    def _paper_from_metadata(
        self,
        metadata: dict[str, object],
        record: dict[str, object],
        pmcid: str,
        versioned_pmcid: str,
    ) -> RemotePaper:
        returned_pmcid = self._optional_identifier(metadata, "pmcid")
        version = self._optional_identifier(metadata, "version")
        expected_version = versioned_pmcid.rsplit(".", 1)[1]
        if returned_pmcid != pmcid or version != expected_version:
            raise PapersError(
                "source_protocol",
                "PMC Cloud metadata does not match the requested version",
                exit_code=4,
            )
        content_urls: dict[str, str] = {}
        metadata_fields = {"pdf": "pdf_url", "txt": "text_url", "xml": "xml_url"}
        for format, field in metadata_fields.items():
            url = self._cloud_content_url(metadata.get(field), versioned_pmcid, format)
            if url is not None:
                content_urls[format] = url
        if content_urls:
            availability = "available"
        elif all(field in metadata for field in metadata_fields.values()):
            availability = "unavailable"
        else:
            availability = "unknown"
        title = metadata.get("title")
        if not isinstance(title, str) or not title.strip():
            raise PapersError("source_protocol", "PMC metadata has an invalid title", exit_code=4)
        license_code = metadata.get("license_code")
        if license_code is not None and (
            not isinstance(license_code, str) or not license_code.strip()
        ):
            raise PapersError(
                "source_protocol", "PMC metadata has an invalid license code", exit_code=4
            )
        return RemotePaper(
            source=self.source,
            source_key=pmcid,
            source_version=expected_version,
            title=" ".join(title.split()),
            abstract="",
            authors=[],
            categories=[],
            published_at=None,
            updated_at=None,
            doi=(
                self._optional_identifier(metadata, "doi")
                or self._optional_identifier(record, "doi")
            ),
            landing_url=f"{PMC_ARTICLE_URL}/{pmcid}/",
            pdf_url=content_urls.get("pdf"),
            content_urls=content_urls,
            pmcid=pmcid,
            pmid=self._optional_identifier(metadata, "pmid")
            or self._optional_identifier(record, "pmid"),
            license_code=license_code,
            fulltext_availability=availability,
        )

    def _unavailable_paper(
        self,
        record: dict[str, object],
        pmcid: str,
        versioned_pmcid: str,
        client: httpx.Client,
        availability: str,
    ) -> RemotePaper:
        summary = self._json(
            client,
            PMC_ESUMMARY_API,
            params={
                "db": "pmc",
                "id": pmcid.removeprefix("PMC"),
                "retmode": "json",
                "tool": "papers_cli",
            },
            source="PMC ESummary",
        )
        result = summary.get("result")
        if not isinstance(result, dict):
            raise PapersError(
                "source_protocol", "PMC ESummary returned an invalid record", exit_code=4
            )
        uids = result.get("uids")
        if not isinstance(uids, list) or len(uids) != 1 or not isinstance(uids[0], str):
            raise PapersError("not_found", "No PMC summary record was found", exit_code=3)
        item = result.get(uids[0])
        if not isinstance(item, dict):
            raise PapersError(
                "source_protocol", "PMC ESummary returned an invalid record", exit_code=4
            )
        title = item.get("title")
        if not isinstance(title, str) or not title.strip():
            raise PapersError("source_protocol", "PMC ESummary returned no title", exit_code=4)
        authors_value = item.get("authors")
        authors = []
        if isinstance(authors_value, list):
            for author in authors_value:
                if isinstance(author, dict) and isinstance(author.get("name"), str):
                    authors.append(author["name"])
                elif not isinstance(author, dict):
                    raise PapersError(
                        "source_protocol", "PMC ESummary returned invalid authors", exit_code=4
                    )
        published_at = item.get("pubdate")
        if published_at is not None and not isinstance(published_at, str):
            raise PapersError(
                "source_protocol", "PMC ESummary returned an invalid publication date", exit_code=4
            )
        return RemotePaper(
            source=self.source,
            source_key=pmcid,
            source_version=versioned_pmcid.rsplit(".", 1)[1],
            title=" ".join(title.split()),
            abstract="",
            authors=authors,
            categories=[],
            published_at=published_at,
            updated_at=None,
            doi=self._optional_identifier(record, "doi"),
            landing_url=f"{PMC_ARTICLE_URL}/{pmcid}/",
            pdf_url=None,
            pmcid=pmcid,
            pmid=self._optional_identifier(record, "pmid"),
            fulltext_availability=availability,
        )

    def download_target(self, paper: RemotePaper, format: str) -> DownloadTarget:
        target = _download_target(
            paper,
            format,
            self.allowed_hosts,
            accepted_content_types=PMC_OPAQUE_CONTENT_TYPES,
        )
        return replace(target, provider=self.source, source_version=paper.source_version)

    def _paper_for_converter_record(
        self, record: dict[str, object], client: httpx.Client
    ) -> RemotePaper:
        returned = record.get("pmcid")
        assert isinstance(returned, str)
        pmcid = returned.upper().split(".", 1)[0]
        versioned_pmcid, live = self._version_from_record(record, pmcid, None)
        if live is False:
            return self._unavailable_paper(record, pmcid, versioned_pmcid, client, "unavailable")
        try:
            metadata = self._json(
                client,
                f"{PMC_CLOUD_API}/metadata/{versioned_pmcid}.json",
                source="PMC Cloud",
            )
        except PapersError as error:
            if error.code != "not_found":
                raise
            return self._unavailable_paper(record, pmcid, versioned_pmcid, client, "unknown")
        return self._paper_from_metadata(metadata, record, pmcid, versioned_pmcid)

    def lookup_doi(self, doi: str, client: httpx.Client) -> RemotePaper | None:
        record = self._doi_record(doi, client)
        if record is None:
            return None
        return self._paper_for_converter_record(record, client)

    def lookup_pmid(self, pmid: str, client: httpx.Client) -> RemotePaper | None:
        record = self._pmid_record(pmid, client)
        if record is None:
            return None
        return self._paper_for_converter_record(record, client)

    def lookup(self, raw: str, client: httpx.Client) -> RemotePaper:
        pmcid, requested_version = self._parse_ref(raw)
        record = self._converter_record(pmcid, client)
        versioned_pmcid, live = self._version_from_record(record, pmcid, requested_version)
        if live is False:
            return self._unavailable_paper(record, pmcid, versioned_pmcid, client, "unavailable")
        try:
            metadata = self._json(
                client,
                f"{PMC_CLOUD_API}/metadata/{versioned_pmcid}.json",
                source="PMC Cloud",
            )
        except PapersError as error:
            if error.code != "not_found":
                raise
            return self._unavailable_paper(record, pmcid, versioned_pmcid, client, "unknown")
        return self._paper_from_metadata(metadata, record, pmcid, versioned_pmcid)

    def search(self, query: str, limit: int, client: httpx.Client) -> list[RemotePaper]:
        raise PapersError("unsupported_search", "PMC keyword search is not installed", exit_code=2)


class CrossrefAdapter:
    source = "crossref"
    allowed_hosts: frozenset[str] = frozenset()

    def __init__(self, pmc_adapter: PmcAdapter) -> None:
        self._pmc_adapter = pmc_adapter

    def normalize_ref(self, raw: str) -> str:
        return normalize_doi(_without_source_prefix(raw, self.source))

    @staticmethod
    def _date(message: dict[str, object]) -> str | None:
        for field in ("published", "issued"):
            value = message.get(field)
            if not isinstance(value, dict):
                continue
            date_parts = value.get("date-parts")
            if (
                not isinstance(date_parts, list)
                or not date_parts
                or not isinstance(date_parts[0], list)
                or not date_parts[0]
                or not all(isinstance(part, int) for part in date_parts[0])
            ):
                continue
            parts = date_parts[0]
            if len(parts) == 1:
                return f"{parts[0]:04d}"
            if len(parts) == 2:
                return f"{parts[0]:04d}-{parts[1]:02d}"
            return f"{parts[0]:04d}-{parts[1]:02d}-{parts[2]:02d}"
        return None

    @staticmethod
    def _authors(message: dict[str, object]) -> list[str]:
        value = message.get("author", [])
        if not isinstance(value, list):
            raise PapersError("source_protocol", "Crossref returned invalid authors", exit_code=4)
        authors: list[str] = []
        for author in value:
            if not isinstance(author, dict):
                raise PapersError(
                    "source_protocol", "Crossref returned invalid authors", exit_code=4
                )
            name = author.get("name")
            if isinstance(name, str) and name.strip():
                authors.append(" ".join(name.split()))
                continue
            parts = [author.get("given"), author.get("family")]
            full_name = " ".join(
                part.strip() for part in parts if isinstance(part, str) and part.strip()
            )
            if full_name:
                authors.append(full_name)
        return authors

    @staticmethod
    def _abstract(message: dict[str, object]) -> str:
        value = message.get("abstract")
        if value is None:
            return ""
        if not isinstance(value, str):
            raise PapersError(
                "source_protocol", "Crossref returned an invalid abstract", exit_code=4
            )
        return " ".join(unescape(re.sub(r"<[^>]*>", " ", value)).split())

    @staticmethod
    def _title(value: str) -> str:
        """Flatten title markup without separating adjacent inline text."""
        return _plain_inline_text(value)

    def _metadata_paper(self, doi: str, client: httpx.Client) -> RemotePaper:
        try:
            response = client.get(f"{CROSSREF_API}/{quote(doi, safe='')}")
        except httpx.HTTPError as exc:
            raise PapersError(
                "source_network", f"Crossref metadata request failed: {exc}", exit_code=4
            ) from exc
        if response.status_code == 404:
            raise PapersError("not_found", "DOI was not found in Crossref", exit_code=3)
        if response.status_code in {401, 403}:
            raise PapersError(
                "source_access",
                f"Crossref metadata access was restricted with HTTP {response.status_code}",
                exit_code=4,
            )
        if response.status_code >= 400:
            raise PapersError(
                "source_network",
                f"Crossref metadata request failed with HTTP {response.status_code}",
                exit_code=4,
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise PapersError(
                "source_protocol", "Crossref returned invalid JSON", exit_code=4
            ) from exc
        if not isinstance(payload, dict) or payload.get("status") != "ok":
            raise PapersError("source_protocol", "Crossref returned an invalid record", exit_code=4)
        message = payload.get("message")
        if not isinstance(message, dict):
            raise PapersError("source_protocol", "Crossref returned an invalid record", exit_code=4)
        returned_doi = message.get("DOI")
        if not isinstance(returned_doi, str) or normalize_doi(returned_doi) != doi:
            raise PapersError("source_protocol", "Crossref returned a mismatched DOI", exit_code=4)
        titles = message.get("title")
        if not isinstance(titles, list) or not titles or not isinstance(titles[0], str):
            raise PapersError("source_protocol", "Crossref returned no title", exit_code=4)
        title = self._title(titles[0])
        if not title:
            raise PapersError("source_protocol", "Crossref returned no title", exit_code=4)
        url = message.get("URL")
        if not isinstance(url, str) or not url.strip():
            url = f"https://doi.org/{quote(doi, safe='/')}"
        indexed = message.get("indexed")
        updated_at = (
            indexed.get("date-time")
            if isinstance(indexed, dict) and isinstance(indexed.get("date-time"), str)
            else None
        )
        return RemotePaper(
            source=self.source,
            source_key=doi,
            source_version=None,
            title=title,
            abstract=self._abstract(message),
            authors=self._authors(message),
            categories=[],
            published_at=self._date(message),
            updated_at=updated_at,
            doi=doi,
            landing_url=url,
            pdf_url=None,
        )

    def download_target(self, paper: RemotePaper, format: str) -> DownloadTarget:
        target = self._pmc_adapter.download_target(paper, format)
        return replace(target, source_version=_pmc_cloud_source_version(target.url, format))

    def lookup(self, raw: str, client: httpx.Client) -> RemotePaper:
        doi = self.normalize_ref(raw)
        metadata = self._metadata_paper(doi, client)
        try:
            pmc_paper = self._pmc_adapter.lookup_doi(doi, client)
        except PapersError as error:
            if error.code not in {"source_access", "source_network"}:
                raise
            return replace(metadata, fulltext_availability="unknown")
        if pmc_paper is None:
            return replace(metadata, fulltext_availability="unavailable")
        return replace(
            metadata,
            pmcid=pmc_paper.pmcid,
            pmid=pmc_paper.pmid,
            license_code=pmc_paper.license_code,
            fulltext_availability=pmc_paper.fulltext_availability,
            pdf_url=pmc_paper.pdf_url,
            content_urls=pmc_paper.content_urls,
        )

    def search(self, query: str, limit: int, client: httpx.Client) -> list[RemotePaper]:
        raise PapersError(
            "unsupported_search", "Crossref keyword search is not installed", exit_code=2
        )


class PubmedAdapter:
    source = "pubmed"
    allowed_hosts: frozenset[str] = frozenset()

    def __init__(self, pmc_adapter: PmcAdapter) -> None:
        self._pmc_adapter = pmc_adapter

    @staticmethod
    def _normalize_pmid(value: str, *, error_code: str, message: str, exit_code: int) -> str:
        candidate = value.strip()
        if not PMID.fullmatch(candidate):
            raise PapersError(error_code, message, exit_code=exit_code)
        return candidate

    def normalize_ref(self, raw: str) -> str:
        candidate = _without_source_prefix(raw, self.source)
        return normalize_pmid(candidate)

    @staticmethod
    def _article_identifiers(record: dict[str, object]) -> tuple[str | None, str | None]:
        values = record.get("articleids", [])
        if values is None:
            values = []
        if not isinstance(values, list):
            raise PapersError(
                "source_protocol", "PubMed returned invalid article identifiers", exit_code=4
            )
        doi: str | None = None
        pmcid: str | None = None
        for value in values:
            if not isinstance(value, dict):
                raise PapersError(
                    "source_protocol", "PubMed returned invalid article identifiers", exit_code=4
                )
            id_type = value.get("idtype")
            identifier = value.get("value")
            if not isinstance(id_type, str) or not isinstance(identifier, str):
                raise PapersError(
                    "source_protocol", "PubMed returned invalid article identifiers", exit_code=4
                )
            if id_type.lower() == "doi":
                try:
                    doi = normalize_doi(identifier)
                except PapersError as exc:
                    raise PapersError(
                        "source_protocol", "PubMed returned an invalid DOI", exit_code=4
                    ) from exc
            if id_type.lower() == "pmc":
                match = PMCID.fullmatch(identifier)
                if match is None:
                    raise PapersError(
                        "source_protocol", "PubMed returned an invalid PMCID", exit_code=4
                    )
                pmcid = match.group("id").upper()
        return doi, pmcid

    def _paper_from_summary(self, pmid: str, record: dict[str, object]) -> RemotePaper:
        uid = record.get("uid")
        if not isinstance(uid, str) or uid != pmid:
            raise PapersError("source_protocol", "PubMed returned a mismatched PMID", exit_code=4)
        title = record.get("title")
        if not isinstance(title, str) or not title.strip():
            raise PapersError("source_protocol", "PubMed returned no title", exit_code=4)
        normalized_title = _plain_inline_text(title)
        if not normalized_title:
            raise PapersError("source_protocol", "PubMed returned no title", exit_code=4)
        authors_value = record.get("authors", [])
        if not isinstance(authors_value, list):
            raise PapersError("source_protocol", "PubMed returned invalid authors", exit_code=4)
        authors: list[str] = []
        for author in authors_value:
            if not isinstance(author, dict) or not isinstance(author.get("name"), str):
                raise PapersError("source_protocol", "PubMed returned invalid authors", exit_code=4)
            name = " ".join(author["name"].split())
            if name:
                authors.append(name)
        abstract = record.get("abstract", "")
        if abstract is None:
            abstract = ""
        if not isinstance(abstract, str):
            raise PapersError("source_protocol", "PubMed returned an invalid abstract", exit_code=4)
        published_at = record.get("pubdate")
        if published_at is not None and not isinstance(published_at, str):
            raise PapersError(
                "source_protocol", "PubMed returned an invalid publication date", exit_code=4
            )
        normalized_published_at = published_at.strip() if isinstance(published_at, str) else None
        doi, pmcid = self._article_identifiers(record)
        return RemotePaper(
            source=self.source,
            source_key=pmid,
            source_version=None,
            title=normalized_title,
            abstract=" ".join(unescape(re.sub(r"<[^>]*>", " ", abstract)).split()),
            authors=authors,
            categories=[],
            published_at=normalized_published_at or None,
            updated_at=None,
            doi=doi,
            landing_url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
            pdf_url=None,
            pmcid=pmcid,
            pmid=pmid,
            fulltext_availability="unknown",
        )

    def _summary_papers(self, pmids: list[str], client: httpx.Client) -> list[RemotePaper]:
        if not pmids:
            return []
        payload = self._pmc_adapter._json(
            client,
            PUBMED_ESUMMARY_API,
            params={
                "db": "pubmed",
                "id": ",".join(pmids),
                "retmode": "json",
                "tool": "papers_cli",
            },
            source="PubMed ESummary",
        )
        result = payload.get("result")
        if not isinstance(result, dict):
            raise PapersError(
                "source_protocol", "PubMed ESummary returned an invalid record", exit_code=4
            )
        uids = result.get("uids")
        if not isinstance(uids, list) or any(not isinstance(uid, str) for uid in uids):
            raise PapersError(
                "source_protocol", "PubMed ESummary returned invalid IDs", exit_code=4
            )
        if len(pmids) == 1 and not uids:
            raise PapersError("not_found", "No PubMed record was found", exit_code=3)
        papers: list[RemotePaper] = []
        for pmid in pmids:
            record = result.get(pmid)
            if not isinstance(record, dict):
                raise PapersError(
                    "source_protocol", "PubMed ESummary omitted a requested record", exit_code=4
                )
            if isinstance(record.get("error"), str) and record["error"].strip():
                if len(pmids) == 1:
                    raise PapersError("not_found", "No PubMed record was found", exit_code=3)
                raise PapersError(
                    "source_protocol", "PubMed ESummary omitted a requested record", exit_code=4
                )
            papers.append(self._paper_from_summary(pmid, record))
        return papers

    def lookup(self, raw: str, client: httpx.Client) -> RemotePaper:
        pmid = self.normalize_ref(raw)
        metadata = self._summary_papers([pmid], client)[0]
        try:
            pmc_paper = self._pmc_adapter.lookup_pmid(pmid, client)
        except PapersError as error:
            if error.code not in {"source_access", "source_network"}:
                raise
            return replace(metadata, fulltext_availability="unknown")
        if pmc_paper is None:
            return replace(metadata, fulltext_availability="unavailable")
        return replace(
            metadata,
            pmcid=pmc_paper.pmcid,
            license_code=pmc_paper.license_code,
            fulltext_availability=pmc_paper.fulltext_availability,
            pdf_url=pmc_paper.pdf_url,
            content_urls=pmc_paper.content_urls,
        )

    def search(self, query: str, limit: int, client: httpx.Client) -> list[RemotePaper]:
        term = query.strip()
        if not term:
            raise PapersError("invalid_query", "Search query must not be empty", exit_code=2)
        payload = self._pmc_adapter._json(
            client,
            PUBMED_ESEARCH_API,
            params={
                "db": "pubmed",
                "term": term,
                "retmode": "json",
                "retmax": str(limit),
                "tool": "papers_cli",
            },
            source="PubMed ESearch",
        )
        result = payload.get("esearchresult")
        if not isinstance(result, dict):
            raise PapersError(
                "source_protocol", "PubMed ESearch returned an invalid result", exit_code=4
            )
        id_list = result.get("idlist")
        if not isinstance(id_list, list) or any(not isinstance(value, str) for value in id_list):
            raise PapersError("source_protocol", "PubMed ESearch returned invalid IDs", exit_code=4)
        pmids = [
            self._normalize_pmid(
                value,
                error_code="source_protocol",
                message="PubMed ESearch returned an invalid PMID",
                exit_code=4,
            )
            for value in id_list[:limit]
        ]
        return self._summary_papers(pmids, client)

    def download_target(self, paper: RemotePaper, format: str) -> DownloadTarget:
        target = self._pmc_adapter.download_target(paper, format)
        return replace(target, source_version=_pmc_cloud_source_version(target.url, format))


_PMC_ADAPTER = PmcAdapter()
ADAPTERS: dict[str, SourceAdapter] = {
    "arxiv": ArxivAdapter(),
    "biorxiv": BiorxivAdapter(),
    "pmc": _PMC_ADAPTER,
    "crossref": CrossrefAdapter(_PMC_ADAPTER),
    "pubmed": PubmedAdapter(_PMC_ADAPTER),
}


def adapter_for(source: str) -> SourceAdapter:
    try:
        return ADAPTERS[source]
    except KeyError as exc:
        raise PapersError("unknown_source", f"Unknown source: {source}", exit_code=2) from exc


def infer_adapter(ref: str) -> tuple[SourceAdapter, str]:
    candidate = ref.strip()
    if ARXIV_ID.fullmatch(candidate):
        return ADAPTERS["arxiv"], ref
    if BIORXIV_DOI.fullmatch(candidate):
        return ADAPTERS["biorxiv"], ref
    if PMCID.fullmatch(candidate):
        return ADAPTERS["pmc"], ref
    if candidate.lower().startswith("pmid:"):
        return ADAPTERS["pubmed"], ref
    if candidate.lower().startswith("doi:"):
        return ADAPTERS["crossref"], ref
    if candidate.lower().startswith(("https://", "http://")):
        return ADAPTERS["crossref"], ref
    if GENERIC_DOI.fullmatch(candidate):
        return ADAPTERS["crossref"], ref
    if ":" in candidate:
        source, raw = candidate.split(":", 1)
        adapter = adapter_for(source.lower())
        return adapter, raw
    raise PapersError(
        "invalid_ref",
        "Use a UUID, arxiv:IDENTIFIER, biorxiv:10.1101/DOI, pmc:PMCIDENTIFIER, PMID, or DOI",
        exit_code=2,
    )


def source_capabilities() -> Iterable[dict[str, object]]:
    return (
        {
            "name": "arxiv",
            "search": True,
            "metadata_search": True,
            "lookup": True,
            "reference_formats": ["arxiv_id"],
            "fulltext_formats": ["pdf"],
            "download": True,
            "official_api": ARXIV_API,
        },
        {
            "name": "biorxiv",
            "search": "doi_only",
            "metadata_search": False,
            "lookup": True,
            "reference_formats": ["doi"],
            "fulltext_formats": ["pdf"],
            "download": True,
            "official_api": BIORXIV_API,
        },
        {
            "name": "pmc",
            "search": False,
            "metadata_search": False,
            "lookup": True,
            "reference_formats": ["pmcid"],
            "fulltext_formats": ["pdf", "txt", "xml"],
            "download": True,
            "official_api": PMC_ID_CONVERTER_API,
        },
        {
            "name": "crossref",
            "search": False,
            "metadata_search": False,
            "lookup": True,
            "reference_formats": ["doi"],
            "fulltext_formats": [],
            "download": True,
            "delivery_source": "pmc",
            "delivery_formats": ["pdf", "txt", "xml"],
            "official_api": CROSSREF_API,
        },
        {
            "name": "pubmed",
            "search": True,
            "metadata_search": True,
            "lookup": True,
            "reference_formats": ["pmid"],
            "fulltext_formats": [],
            "download": True,
            "delivery_source": "pmc",
            "delivery_formats": ["pdf", "txt", "xml"],
            "official_api": PUBMED_ESEARCH_API,
        },
    )
