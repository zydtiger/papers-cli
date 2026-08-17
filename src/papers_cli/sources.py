from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Protocol
from xml.etree.ElementTree import Element

import httpx
from defusedxml import ElementTree

from .errors import PapersError
from .models import RemotePaper

ARXIV_API = "https://export.arxiv.org/api/query"
BIORXIV_API = "https://api.biorxiv.org/details/biorxiv"
ARXIV_ID = re.compile(
    r"^(?P<id>\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Z]{2})?/\d{7})(?:v(?P<version>\d+))?$", re.I
)
DOI = re.compile(r"^10\.1101/[A-Za-z0-9._;()/:+-]+$", re.I)
ATOM = "{http://www.w3.org/2005/Atom}"
ARXIV = "{http://arxiv.org/schemas/atom}"


class SourceAdapter(Protocol):
    source: str
    allowed_hosts: frozenset[str]

    def normalize_ref(self, raw: str) -> str: ...

    def lookup(self, raw: str, client: httpx.Client) -> RemotePaper: ...

    def search(self, query: str, limit: int, client: httpx.Client) -> list[RemotePaper]: ...


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
        candidate = raw.removeprefix("arxiv:").strip()
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
        )

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
        candidate = raw.removeprefix("biorxiv:").strip()
        if not DOI.fullmatch(candidate):
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
        )

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
        if not DOI.fullmatch(query.removeprefix("biorxiv:").strip()):
            raise PapersError(
                "unsupported_search",
                "bioRxiv official API search currently supports DOI lookup only",
                exit_code=2,
            )
        return [self.lookup(query, client)]


ADAPTERS: dict[str, SourceAdapter] = {"arxiv": ArxivAdapter(), "biorxiv": BiorxivAdapter()}


def adapter_for(source: str) -> SourceAdapter:
    try:
        return ADAPTERS[source]
    except KeyError as exc:
        raise PapersError("unknown_source", f"Unknown source: {source}", exit_code=2) from exc


def infer_adapter(ref: str) -> tuple[SourceAdapter, str]:
    if ":" in ref:
        source, raw = ref.split(":", 1)
        adapter = adapter_for(source.lower())
        return adapter, raw
    if ARXIV_ID.fullmatch(ref.strip()):
        return ADAPTERS["arxiv"], ref
    if DOI.fullmatch(ref.strip()):
        return ADAPTERS["biorxiv"], ref
    raise PapersError(
        "invalid_ref", "Use a UUID, arxiv:IDENTIFIER, or biorxiv:10.1101/DOI", exit_code=2
    )


def source_capabilities() -> Iterable[dict[str, object]]:
    return (
        {
            "name": "arxiv",
            "search": True,
            "lookup": True,
            "download": True,
            "official_api": ARXIV_API,
        },
        {
            "name": "biorxiv",
            "search": "doi_only",
            "lookup": True,
            "download": True,
            "official_api": BIORXIV_API,
        },
    )
