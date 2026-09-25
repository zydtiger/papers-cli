from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Final

CONTENT_FORMATS: Final[tuple[str, ...]] = ("pdf", "txt", "xml")
CONTENT_MEDIA_TYPES: Final[dict[str, str]] = {
    "pdf": "application/pdf",
    "txt": "text/plain",
    "xml": "application/xml",
}
CONTENT_SUFFIXES: Final[dict[str, str]] = {"pdf": ".pdf", "txt": ".txt", "xml": ".xml"}


def content_media_type(format: str) -> str:
    try:
        return CONTENT_MEDIA_TYPES[format]
    except KeyError as exc:
        raise ValueError(f"Unsupported content format: {format}") from exc


def content_suffix(format: str) -> str:
    try:
        return CONTENT_SUFFIXES[format]
    except KeyError as exc:
        raise ValueError(f"Unsupported content format: {format}") from exc


@dataclass(frozen=True, slots=True)
class RemotePaper:
    source: str
    source_key: str
    source_version: str | None
    title: str
    abstract: str
    authors: list[str]
    categories: list[str]
    published_at: str | None
    updated_at: str | None
    doi: str | None
    landing_url: str
    pdf_url: str | None
    content_urls: dict[str, str] = field(default_factory=dict)

    @property
    def ref(self) -> str:
        return f"{self.source}:{self.source_key}"

    def as_dict(self) -> dict[str, object]:
        result = asdict(self)
        result["ref"] = self.ref
        return result


# Top-level keys of RemotePaper.as_dict(); tests pin this to the dataclass and ref.
REMOTE_PAPER_FIELDS: Final[tuple[str, ...]] = (
    "ref",
    "source",
    "source_key",
    "source_version",
    "title",
    "abstract",
    "authors",
    "categories",
    "published_at",
    "updated_at",
    "doi",
    "landing_url",
    "pdf_url",
    "content_urls",
)


@dataclass(frozen=True, slots=True)
class DownloadTarget:
    format: str
    url: str
    allowed_hosts: frozenset[str]
    media_type: str
    provider: str


@dataclass(frozen=True, slots=True)
class DownloadedFile:
    sha256: str
    byte_count: int
    relative_path: str
    source_url: str
    format: str = "pdf"
    media_type: str = "application/pdf"
    provider: str = ""
