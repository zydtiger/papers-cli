from __future__ import annotations

import errno
import hashlib
from io import BytesIO
from pathlib import Path

import httpx
import pytest
from pypdf import PdfWriter

import papers_cli.downloader as downloader
from papers_cli.config import AppPaths, ensure_paths
from papers_cli.downloader import download_file, download_pdf
from papers_cli.errors import PapersError
from papers_cli.models import DownloadTarget


def pdf_bytes(*, pages: int = 1, encrypted: bool = False) -> bytes:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=72, height=72)
    if encrypted:
        writer.encrypt("secret")
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def test_download_content_addresses_and_deduplicates(tmp_path, monkeypatch) -> None:
    paths = AppPaths(tmp_path / "data", tmp_path / "cache")
    ensure_paths(paths)
    body = pdf_bytes()
    staging_directories: list[Path] = []
    real_mkstemp = downloader.tempfile.mkstemp

    def record_mkstemp(**kwargs):
        staging_directories.append(Path(kwargs["dir"]))
        return real_mkstemp(**kwargs)

    monkeypatch.setattr(downloader.tempfile, "mkstemp", record_mkstemp)

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "application/pdf"}, content=body)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        first = download_pdf(
            client, "https://arxiv.org/pdf/2301.00001", frozenset({"arxiv.org"}), paths
        )
        second = download_pdf(
            client, "https://arxiv.org/pdf/2301.00001", frozenset({"arxiv.org"}), paths
        )
    assert first == second
    assert first.sha256 == hashlib.sha256(body).hexdigest()
    assert (paths.data_dir / first.relative_path).read_bytes() == body
    assert staging_directories == [paths.download_cache_dir, paths.download_cache_dir]
    assert not list(paths.download_cache_dir.glob("download-*.part"))
    assert not (paths.data_dir / ".staging").exists()


@pytest.mark.parametrize(
    ("format", "media_type", "body", "suffix"),
    [
        ("txt", "text/plain", b"Research text containing CAPTCHA as a word.", ".txt"),
        ("xml", "application/xml", b"<article><body>Text</body></article>", ".xml"),
    ],
)
def test_download_stores_supported_non_pdf_formats(
    tmp_path, format, media_type, body, suffix
) -> None:
    paths = AppPaths(tmp_path / "data", tmp_path / "cache")
    ensure_paths(paths)
    target = DownloadTarget(
        format,
        f"https://provider.example/article.{format}",
        frozenset({"provider.example"}),
        media_type,
        "provider",
    )
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, headers={"content-type": media_type}, content=body)
        )
    ) as client:
        downloaded = download_file(client, target, paths)
    assert downloaded.format == format
    assert downloaded.media_type == media_type
    assert downloaded.provider == "provider"
    assert downloaded.relative_path.endswith(suffix)
    assert (paths.data_dir / downloaded.relative_path).read_bytes() == body


@pytest.mark.parametrize(
    ("format", "media_type", "body", "code"),
    [
        ("txt", "text/plain", b"\n\t\r", "not_text"),
        ("xml", "application/xml", b"not markup", "not_xml"),
        ("txt", "text/html", b"<html>error</html>", "invalid_content_type"),
    ],
)
def test_download_rejects_invalid_non_pdf_content(tmp_path, format, media_type, body, code) -> None:
    paths = AppPaths(tmp_path / "data", tmp_path / "cache")
    ensure_paths(paths)
    target = DownloadTarget(
        format,
        f"https://provider.example/article.{format}",
        frozenset({"provider.example"}),
        "text/plain" if format == "txt" else "application/xml",
        "provider",
    )
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, headers={"content-type": media_type}, content=body)
        )
    ) as client:
        with pytest.raises(PapersError) as error:
            download_file(client, target, paths)
    assert error.value.code == code
    assert not list(paths.download_cache_dir.glob("download-*.part"))


@pytest.mark.parametrize(
    ("headers", "body", "code"),
    [
        ({"content-type": "text/html"}, b"%PDF-1.7", "not_pdf"),
        ({"content-type": "binary/octet-stream"}, b"%PDF-1.7", "not_pdf"),
        ({"content-type": "application/pdf"}, b"HTML", "not_pdf"),
    ],
)
def test_download_rejects_non_pdf(tmp_path, headers, body, code) -> None:
    paths = AppPaths(tmp_path / "data", tmp_path / "cache")
    ensure_paths(paths)
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, headers=headers, content=body))
    ) as client:
        with pytest.raises(PapersError) as error:
            download_pdf(client, "https://arxiv.org/pdf/x", frozenset({"arxiv.org"}), paths)
    assert error.value.code == code
    assert not list(paths.download_cache_dir.glob("download-*.part"))


@pytest.mark.parametrize(
    ("format", "body", "code"),
    [
        ("pdf", b"not a PDF", "not_pdf"),
        ("txt", b"<!doctype html><html><body>error</body></html>", "not_text"),
        ("xml", b"<article><body> </body></article>", "not_xml"),
    ],
)
def test_pmc_opaque_content_type_still_validates_requested_format(
    tmp_path, format, body, code
) -> None:
    paths = AppPaths(tmp_path / "data", tmp_path / "cache")
    ensure_paths(paths)
    target = DownloadTarget(
        format,
        f"https://pmc-oa-opendata.s3.amazonaws.com/PMC1.1/PMC1.1.{format}",
        frozenset({"pmc-oa-opendata.s3.amazonaws.com"}),
        {"pdf": "application/pdf", "txt": "text/plain", "xml": "application/xml"}[format],
        "pmc",
        frozenset({"binary/octet-stream"}),
    )
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, headers={"content-type": "binary/octet-stream"}, content=body
            )
        )
    ) as client:
        with pytest.raises(PapersError) as error:
            download_file(client, target, paths)
    assert error.value.code == code
    assert not list(paths.download_cache_dir.glob("download-*.part"))
    assert not list(paths.objects_dir.rglob(f"*.{format}"))


@pytest.mark.parametrize(
    ("body", "code"),
    [
        (b"%PDF-1.7\nfixture", "not_pdf"),
        (pdf_bytes(pages=0), "not_pdf"),
        (pdf_bytes(encrypted=True), "encrypted_pdf"),
    ],
)
def test_download_rejects_unreadable_pdf_before_install(tmp_path, body, code) -> None:
    paths = AppPaths(tmp_path / "data", tmp_path / "cache")
    ensure_paths(paths)
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, headers={"content-type": "application/pdf"}, content=body)
        )
    ) as client:
        with pytest.raises(PapersError) as error:
            download_pdf(client, "https://arxiv.org/pdf/x", frozenset({"arxiv.org"}), paths)
    assert error.value.code == code
    assert not list(paths.download_cache_dir.glob("download-*.part"))
    assert not list(paths.objects_dir.rglob("*.pdf"))


@pytest.mark.parametrize("status", [401, 403])
def test_download_reports_restricted_access_separately(tmp_path, status) -> None:
    paths = AppPaths(tmp_path / "data", tmp_path / "cache")
    ensure_paths(paths)
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(status, content=b"restricted"))
    ) as client:
        with pytest.raises(PapersError) as error:
            download_pdf(client, "https://arxiv.org/pdf/x", frozenset({"arxiv.org"}), paths)
    assert error.value.code == "download_access"


@pytest.mark.parametrize(
    "body",
    [
        b"<error>not found</error>",
        b"<article><front><article-meta/></front></article>",
        b"<article><body>  </body></article>",
        b"<!DOCTYPE article [<!ENTITY blocked SYSTEM 'file:///etc/passwd'>]><article><body>&blocked;</body></article>",
    ],
)
def test_download_rejects_non_fulltext_jats_before_install(tmp_path, body) -> None:
    paths = AppPaths(tmp_path / "data", tmp_path / "cache")
    ensure_paths(paths)
    target = DownloadTarget(
        "xml",
        "https://provider.example/article.xml",
        frozenset({"provider.example"}),
        "application/xml",
        "provider",
    )
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, headers={"content-type": "application/xml"}, content=body)
        )
    ) as client:
        with pytest.raises(PapersError) as error:
            download_file(client, target, paths)
    assert error.value.code == "not_xml"
    assert not list(paths.download_cache_dir.glob("download-*.part"))
    assert not list(paths.objects_dir.rglob("*.xml"))


def test_download_accepts_jats_with_external_doctype_without_expansion(tmp_path) -> None:
    paths = AppPaths(tmp_path / "data", tmp_path / "cache")
    ensure_paths(paths)
    body = (
        b'<?xml version="1.0"?>\n'
        b'<!DOCTYPE article SYSTEM "https://example.test/jats.dtd">\n'
        b"<article><body><sec><p>Full text</p></sec></body></article>"
    )
    target = DownloadTarget(
        "xml",
        "https://provider.example/article.xml",
        frozenset({"provider.example"}),
        "application/xml",
        "provider",
    )
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, headers={"content-type": "application/xml"}, content=body)
        )
    ) as client:
        downloaded = download_file(client, target, paths)
    assert (paths.data_dir / downloaded.relative_path).read_bytes() == body


@pytest.mark.parametrize(
    "body",
    [
        b"\xffnot UTF-8",
        b"<!doctype html><html><body>CAPTCHA challenge</body></html>",
        b"<!-- provider preamble -->\n<html><body>CAPTCHA challenge</body></html>",
        *[
            b"<!DOCTYPE" + whitespace + b"HTML><html><body>CAPTCHA challenge</body></html>"
            for whitespace in (b" ", b"\t", b"\n", b"\f")
        ],
    ],
)
def test_download_rejects_non_text_before_install(tmp_path, body) -> None:
    paths = AppPaths(tmp_path / "data", tmp_path / "cache")
    ensure_paths(paths)
    target = DownloadTarget(
        "txt",
        "https://provider.example/article.txt",
        frozenset({"provider.example"}),
        "text/plain",
        "provider",
    )
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, headers={"content-type": "text/plain"}, content=body)
        )
    ) as client:
        with pytest.raises(PapersError) as error:
            download_file(client, target, paths)
    assert error.value.code == "not_text"
    assert not list(paths.download_cache_dir.glob("download-*.part"))
    assert not list(paths.objects_dir.rglob("*.txt"))


def test_download_rejects_unsafe_redirect(tmp_path) -> None:
    paths = AppPaths(tmp_path / "data", tmp_path / "cache")
    ensure_paths(paths)
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(302, headers={"location": "http://evil.test/x"})
        )
    ) as client:
        with pytest.raises(PapersError) as error:
            download_pdf(client, "https://arxiv.org/pdf/x", frozenset({"arxiv.org"}), paths)
    assert error.value.code == "unsafe_download_url"


def test_download_reports_atomic_move_failure_and_removes_cached_part(
    tmp_path, monkeypatch
) -> None:
    paths = AppPaths(tmp_path / "data", tmp_path / "cache")
    ensure_paths(paths)
    body = pdf_bytes()

    def fail_replace(_: Path, __: Path) -> None:
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    monkeypatch.setattr("papers_cli.downloader.os.replace", fail_replace)
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, headers={"content-type": "application/pdf"}, content=body)
        )
    ) as client:
        with pytest.raises(PapersError) as error:
            download_pdf(client, "https://arxiv.org/pdf/x", frozenset({"arxiv.org"}), paths)

    assert error.value.code == "storage_install"
    assert not list(paths.download_cache_dir.glob("download-*.part"))
    assert not list(paths.objects_dir.rglob("*.pdf"))


def test_download_reports_destination_directory_failure_and_removes_cached_part(tmp_path) -> None:
    data_file = tmp_path / "data-file"
    data_file.write_text("not a directory")
    paths = AppPaths(data_file, tmp_path / "cache")
    body = pdf_bytes()

    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, headers={"content-type": "application/pdf"}, content=body)
        )
    ) as client:
        with pytest.raises(PapersError) as error:
            download_pdf(client, "https://arxiv.org/pdf/x", frozenset({"arxiv.org"}), paths)

    assert error.value.code == "storage_install"
    assert not list(paths.download_cache_dir.glob("download-*.part"))
    assert data_file.read_text() == "not a directory"


def test_download_reports_blocked_cache_directory_failure(tmp_path) -> None:
    cache_file = tmp_path / "cache-file"
    cache_file.write_text("not a directory")
    paths = AppPaths(tmp_path / "data", cache_file)
    body = pdf_bytes()

    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, headers={"content-type": "application/pdf"}, content=body)
        )
    ) as client:
        with pytest.raises(PapersError) as error:
            download_pdf(client, "https://arxiv.org/pdf/x", frozenset({"arxiv.org"}), paths)

    assert error.value.code == "storage_staging"
    assert cache_file.read_text() == "not a directory"


def test_download_reports_mkstemp_failure(tmp_path, monkeypatch) -> None:
    paths = AppPaths(tmp_path / "data", tmp_path / "cache")
    body = pdf_bytes()

    def fail_mkstemp(**_: object) -> tuple[int, str]:
        raise OSError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(downloader.tempfile, "mkstemp", fail_mkstemp)
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, headers={"content-type": "application/pdf"}, content=body)
        )
    ) as client:
        with pytest.raises(PapersError) as error:
            download_pdf(client, "https://arxiv.org/pdf/x", frozenset({"arxiv.org"}), paths)

    assert error.value.code == "storage_staging"
    assert not list(paths.download_cache_dir.glob("download-*.part"))


def test_download_reports_temporary_file_fsync_failure(tmp_path, monkeypatch) -> None:
    paths = AppPaths(tmp_path / "data", tmp_path / "cache")
    ensure_paths(paths)
    body = pdf_bytes()

    def fail_fsync(_: int) -> None:
        raise OSError(errno.EIO, "I/O error")

    monkeypatch.setattr(downloader.os, "fsync", fail_fsync)
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, headers={"content-type": "application/pdf"}, content=body)
        )
    ) as client:
        with pytest.raises(PapersError) as error:
            download_pdf(client, "https://arxiv.org/pdf/x", frozenset({"arxiv.org"}), paths)

    assert error.value.code == "storage_staging"
    assert not list(paths.download_cache_dir.glob("download-*.part"))
    assert not list(paths.objects_dir.rglob("*.pdf"))


def test_download_keeps_installed_object_when_directory_fsync_fails(tmp_path, monkeypatch) -> None:
    paths = AppPaths(tmp_path / "data", tmp_path / "cache")
    ensure_paths(paths)
    body = pdf_bytes()
    real_fsync = downloader.os.fsync
    fsync_calls = 0

    def fail_directory_fsync(descriptor: int) -> None:
        nonlocal fsync_calls
        fsync_calls += 1
        if fsync_calls == 2:
            raise OSError(errno.EIO, "I/O error")
        real_fsync(descriptor)

    monkeypatch.setattr(downloader.os, "fsync", fail_directory_fsync)
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, headers={"content-type": "application/pdf"}, content=body)
        )
    ) as client:
        with pytest.raises(PapersError) as error:
            download_pdf(client, "https://arxiv.org/pdf/x", frozenset({"arxiv.org"}), paths)

    sha256 = hashlib.sha256(body).hexdigest()
    destination = paths.objects_dir / sha256[:2] / sha256[2:4] / f"{sha256}.pdf"
    assert error.value.code == "storage_install"
    assert destination.read_bytes() == body
    assert not list(paths.download_cache_dir.glob("download-*.part"))
