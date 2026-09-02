from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Final


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
    pdf_url: str

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
)


@dataclass(frozen=True, slots=True)
class DownloadedFile:
    sha256: str
    byte_count: int
    relative_path: str
    source_url: str
