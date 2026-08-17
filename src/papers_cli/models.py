from __future__ import annotations

from dataclasses import asdict, dataclass


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


@dataclass(frozen=True, slots=True)
class DownloadedFile:
    sha256: str
    byte_count: int
    relative_path: str
    source_url: str
