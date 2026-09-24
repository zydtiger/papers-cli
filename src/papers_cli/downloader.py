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
from .models import DownloadedFile, DownloadTarget, content_suffix

MAX_BYTES = 100 * 1024 * 1024
PDF_TYPES = {"application/pdf", "application/octet-stream"}
TEXT_TYPES = {"text/plain"}
XML_TYPES = {"application/xml", "text/xml"}
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


def _staging_error(format: str) -> PapersError:
    return PapersError(
        "storage_staging",
        f"Unable to write the temporary {format} download",
        exit_code=5,
    )


def _remove_staging_file(staging: Path) -> None:
    try:
        staging.unlink(missing_ok=True)
    except OSError:
        pass


def _validate_content_type(content_type: str, format: str) -> None:
    allowed = {"pdf": PDF_TYPES, "txt": TEXT_TYPES, "xml": XML_TYPES}[format]
    if content_type in allowed:
        return
    if format == "pdf":
        raise PapersError(
            "not_pdf", "Provider response does not have a PDF content type", exit_code=4
        )
    raise PapersError(
        "invalid_content_type",
        f"Provider response does not have a {format} content type",
        exit_code=4,
    )


def _validate_prefix(
    format: str, prefix: bytes, contains_nul: bool, contains_non_whitespace: bool
) -> None:
    if format == "pdf":
        if prefix[:5] != b"%PDF-":
            raise PapersError(
                "not_pdf", "Provider response does not start with a PDF signature", exit_code=4
            )
        return
    if contains_nul:
        raise PapersError(
            "invalid_content", f"Provider response is not safe {format} content", exit_code=4
        )
    if format == "txt" and not contains_non_whitespace:
        raise PapersError("invalid_content", "Provider response does not contain text", exit_code=4)
    if format == "xml" and not prefix.lstrip(b"\xef\xbb\xbf \t\r\n").startswith(b"<"):
        raise PapersError(
            "invalid_content", "Provider response does not begin with XML markup", exit_code=4
        )


def download_file(client: httpx.Client, target: DownloadTarget, paths: AppPaths) -> DownloadedFile:
    """Stream an official full-text target to durable, content-addressed storage."""
    _validate_url(target.url, target.allowed_hosts)
    current_url = target.url
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
                    _validate_url(current_url, target.allowed_hosts)
                    redirects += 1
                    attempt = 0
                    continue
                if not 200 <= response.status_code < 300:
                    raise PapersError(
                        "download_network",
                        f"{target.format} download failed with HTTP {response.status_code}",
                        exit_code=4,
                    )
                content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
                _validate_content_type(content_type, target.format)
                content_length = response.headers.get("content-length")
                if content_length and (
                    not content_length.isdigit() or int(content_length) > MAX_BYTES
                ):
                    raise PapersError(
                        "download_too_large",
                        f"{target.format} exceeds the 100 MiB download limit",
                        exit_code=4,
                    )
                return _store_stream(response, current_url, target, paths)
        except PapersError:
            raise
        except httpx.HTTPError as exc:
            if attempt < 2:
                time.sleep(0.25 * (2**attempt))
                attempt += 1
                continue
            raise PapersError(
                "download_network", f"{target.format} download failed: {exc}", exit_code=4
            ) from exc


def download_pdf(
    client: httpx.Client, url: str, allowed_hosts: frozenset[str], paths: AppPaths
) -> DownloadedFile:
    """Compatibility wrapper for callers that request a PDF directly."""
    return download_file(
        client,
        DownloadTarget("pdf", url, allowed_hosts, "application/pdf", ""),
        paths,
    )


def _store_stream(
    response: httpx.Response, source_url: str, target: DownloadTarget, paths: AppPaths
) -> DownloadedFile:
    try:
        paths.download_cache_dir.mkdir(parents=True, exist_ok=True)
        descriptor, staging_name = tempfile.mkstemp(
            prefix="download-", suffix=".part", dir=paths.download_cache_dir
        )
    except OSError as exc:
        raise _staging_error(target.format) from exc
    staging = Path(staging_name)
    digest = hashlib.sha256()
    size = 0
    prefix = bytearray()
    contains_nul = False
    contains_non_whitespace = False
    try:
        try:
            handle = os.fdopen(descriptor, "wb")
        except OSError as exc:
            try:
                os.close(descriptor)
            except OSError:
                pass
            raise _staging_error(target.format) from exc
        try:
            with handle:
                for chunk in response.iter_bytes(chunk_size=64 * 1024):
                    if not chunk:
                        continue
                    if len(prefix) < 4096:
                        prefix.extend(chunk[: 4096 - len(prefix)])
                    contains_nul = contains_nul or b"\x00" in chunk
                    contains_non_whitespace = contains_non_whitespace or bool(chunk.strip())
                    size += len(chunk)
                    if size > MAX_BYTES:
                        raise PapersError(
                            "download_too_large",
                            f"{target.format} exceeds the 100 MiB download limit",
                            exit_code=4,
                        )
                    digest.update(chunk)
                    handle.write(chunk)
                _validate_prefix(
                    target.format, bytes(prefix), contains_nul, contains_non_whitespace
                )
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            raise _staging_error(target.format) from exc
        sha256 = digest.hexdigest()
        relative = (
            Path("objects")
            / "sha256"
            / sha256[:2]
            / sha256[2:4]
            / f"{sha256}{content_suffix(target.format)}"
        )
        destination = paths.data_dir / relative
        try:
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
        except OSError as exc:
            raise PapersError(
                "storage_install",
                f"Unable to atomically install the verified {target.format}",
                exit_code=5,
            ) from exc
        return DownloadedFile(
            sha256=sha256,
            byte_count=size,
            relative_path=relative.as_posix(),
            source_url=source_url,
            format=target.format,
            media_type=target.media_type,
            provider=target.provider,
        )
    except Exception:
        _remove_staging_file(staging)
        raise
