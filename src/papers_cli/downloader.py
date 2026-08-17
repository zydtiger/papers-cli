from __future__ import annotations

import hashlib
import os
import tempfile
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx

from .config import AppPaths
from .errors import PapersError
from .models import DownloadedFile

MAX_BYTES = 100 * 1024 * 1024
PDF_TYPES = {"application/pdf", "application/octet-stream"}
RETRYABLE = {429, 502, 503, 504}


def _validate_url(url: str, allowed_hosts: frozenset[str]) -> None:
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.hostname.lower() not in allowed_hosts
    ):
        raise PapersError(
            "unsafe_download_url", "Download URL is not an allowed HTTPS provider URL", exit_code=4
        )


def _retry_delay(response: httpx.Response, attempt: int) -> float:
    retry_after = response.headers.get("retry-after")
    if retry_after and retry_after.isdigit():
        return min(float(retry_after), 5.0)
    return 0.25 * (2**attempt)


def download_pdf(
    client: httpx.Client, url: str, allowed_hosts: frozenset[str], paths: AppPaths
) -> DownloadedFile:
    """Stream an official PDF to durable, content-addressed storage."""
    _validate_url(url, allowed_hosts)
    current_url = url
    redirects = 0
    attempt = 0
    while True:
        try:
            with client.stream("GET", current_url, follow_redirects=False) as response:
                if response.status_code in RETRYABLE and attempt < 2:
                    time.sleep(_retry_delay(response, attempt))
                    attempt += 1
                    continue
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location or redirects >= 3:
                        raise PapersError(
                            "download_redirect",
                            "Download redirect was missing or exceeded the limit",
                            exit_code=4,
                        )
                    current_url = urljoin(current_url, location)
                    _validate_url(current_url, allowed_hosts)
                    redirects += 1
                    attempt = 0
                    continue
                if not 200 <= response.status_code < 300:
                    raise PapersError(
                        "download_network",
                        f"PDF download failed with HTTP {response.status_code}",
                        exit_code=4,
                    )
                content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
                if content_type not in PDF_TYPES:
                    raise PapersError(
                        "not_pdf", "Provider response does not have a PDF content type", exit_code=4
                    )
                content_length = response.headers.get("content-length")
                if content_length and (
                    not content_length.isdigit() or int(content_length) > MAX_BYTES
                ):
                    raise PapersError(
                        "download_too_large", "PDF exceeds the 100 MiB download limit", exit_code=4
                    )
                return _store_stream(response, current_url, paths)
        except PapersError:
            raise
        except httpx.HTTPError as exc:
            if attempt < 2:
                time.sleep(0.25 * (2**attempt))
                attempt += 1
                continue
            raise PapersError(
                "download_network", f"PDF download failed: {exc}", exit_code=4
            ) from exc


def _store_stream(response: httpx.Response, source_url: str, paths: AppPaths) -> DownloadedFile:
    paths.staging_dir.mkdir(parents=True, exist_ok=True)
    descriptor, staging_name = tempfile.mkstemp(
        prefix="download-", suffix=".part", dir=paths.staging_dir
    )
    staging = Path(staging_name)
    digest = hashlib.sha256()
    size = 0
    prefix = bytearray()
    try:
        with os.fdopen(descriptor, "wb") as handle:
            for chunk in response.iter_bytes(chunk_size=64 * 1024):
                if not chunk:
                    continue
                if len(prefix) < 5:
                    prefix.extend(chunk[: 5 - len(prefix)])
                size += len(chunk)
                if size > MAX_BYTES:
                    raise PapersError(
                        "download_too_large", "PDF exceeds the 100 MiB download limit", exit_code=4
                    )
                digest.update(chunk)
                handle.write(chunk)
            if bytes(prefix) != b"%PDF-":
                raise PapersError(
                    "not_pdf", "Provider response does not start with a PDF signature", exit_code=4
                )
            handle.flush()
            os.fsync(handle.fileno())
        sha256 = digest.hexdigest()
        relative = Path("objects") / "sha256" / sha256[:2] / sha256[2:4] / f"{sha256}.pdf"
        destination = paths.data_dir / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            staging.unlink()
        else:
            os.replace(staging, destination)
            parent_fd = os.open(destination.parent, os.O_RDONLY)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
        return DownloadedFile(
            sha256=sha256, byte_count=size, relative_path=relative.as_posix(), source_url=source_url
        )
    except Exception:
        staging.unlink(missing_ok=True)
        raise
