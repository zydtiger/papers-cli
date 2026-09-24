from __future__ import annotations

import hashlib
import os
import re
import tempfile
import time
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from urllib.parse import urljoin, urlparse
from xml.etree.ElementTree import ParseError

import httpx
from defusedxml import ElementTree as DefusedElementTree
from defusedxml.common import DefusedXmlException
from pypdf import PdfReader
from pypdf.errors import PyPdfError

from .config import AppPaths
from .errors import PapersError
from .models import DownloadedFile, DownloadTarget, content_suffix

MAX_BYTES = 100 * 1024 * 1024
PDF_TYPES = {"application/pdf", "application/octet-stream"}
TEXT_TYPES = {"text/plain"}
XML_TYPES = {"application/xml", "text/xml"}
RETRYABLE = {429, 502, 503, 504}
HTML_DOCUMENT = re.compile(r"^<html(?:\s|/?>)", re.IGNORECASE)


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


def _validate_prefix(format: str, prefix: bytes) -> None:
    if format == "pdf":
        if prefix[:5] != b"%PDF-":
            raise PapersError(
                "not_pdf", "Provider response does not start with a PDF signature", exit_code=4
            )


def _validate_pdf(staging: Path) -> None:
    reader: PdfReader | None = None
    try:
        # pypdf emits recovery diagnostics to stderr in non-strict mode. They are
        # parser implementation details, not CLI output, so keep them out of the
        # JSONL stream while validating this staged provider response.
        with redirect_stderr(StringIO()):
            reader = PdfReader(staging, strict=False)
            if reader.is_encrypted:
                raise PapersError(
                    "encrypted_pdf",
                    "Provider response is an encrypted PDF and cannot be structurally validated",
                    exit_code=4,
                )
            if len(reader.pages) < 1:
                raise PapersError(
                    "not_pdf", "Provider response does not contain readable PDF pages", exit_code=4
                )
    except PapersError:
        raise
    except (PyPdfError, ValueError, TypeError, KeyError, IndexError) as exc:
        raise PapersError(
            "not_pdf", "Provider response is not a structurally readable PDF", exit_code=4
        ) from exc
    finally:
        if reader is not None:
            reader.close()


def _local_name(tag: object) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _validate_xml(staging: Path) -> None:
    try:
        root = DefusedElementTree.parse(staging).getroot()
    except (DefusedXmlException, ParseError) as exc:
        raise PapersError("not_xml", "Provider response is not safe JATS XML", exit_code=4) from exc
    if root is None:
        raise PapersError("not_xml", "Provider response is not a JATS article", exit_code=4)
    if _local_name(root.tag) != "article":
        raise PapersError("not_xml", "Provider response is not a JATS article", exit_code=4)
    body = next(
        (
            element
            for element in root.iter()
            if element is not None and _local_name(element.tag) == "body"
        ),
        None,
    )
    if body is None:
        raise PapersError(
            "not_xml", "Provider response does not contain JATS article body text", exit_code=4
        )
    if not any(text.strip() for text in body.itertext()):
        raise PapersError(
            "not_xml", "Provider response does not contain JATS article body text", exit_code=4
        )


def _validate_txt(staging: Path) -> None:
    saw_text = False
    try:
        with staging.open("r", encoding="utf-8-sig") as handle:
            for chunk in iter(lambda: handle.read(64 * 1024), ""):
                initial = chunk.lstrip()
                if not initial:
                    continue
                if not saw_text:
                    if _looks_like_html_document(initial[:4096]):
                        raise PapersError(
                            "not_text",
                            "Provider response is an HTML document, not plain text",
                            exit_code=4,
                        )
                saw_text = True
    except UnicodeDecodeError as exc:
        raise PapersError(
            "not_text", "Provider response is not valid UTF-8 text", exit_code=4
        ) from exc
    if not saw_text:
        raise PapersError("not_text", "Provider response does not contain text", exit_code=4)


def _looks_like_html_document(preview: str) -> bool:
    candidate = preview.lstrip()
    while candidate.startswith("<!--"):
        closing = candidate.find("-->")
        if closing == -1:
            return False
        candidate = candidate[closing + 3 :].lstrip()
    normalized = candidate.casefold()
    return normalized.startswith("<!doctype html") or HTML_DOCUMENT.match(candidate) is not None


def _validate_staged(staging: Path, format: str, prefix: bytes) -> None:
    _validate_prefix(format, prefix)
    if format == "pdf":
        _validate_pdf(staging)
    elif format == "xml":
        _validate_xml(staging)
    elif format == "txt":
        _validate_txt(staging)


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
                if response.status_code in {401, 403}:
                    raise PapersError(
                        "download_access",
                        f"{target.format} download access was restricted with HTTP "
                        f"{response.status_code}",
                        exit_code=4,
                    )
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
                    size += len(chunk)
                    if size > MAX_BYTES:
                        raise PapersError(
                            "download_too_large",
                            f"{target.format} exceeds the 100 MiB download limit",
                            exit_code=4,
                        )
                    digest.update(chunk)
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            raise _staging_error(target.format) from exc
        _validate_staged(staging, target.format, bytes(prefix))
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
