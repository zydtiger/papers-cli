from __future__ import annotations

import hashlib

import httpx
import pytest

from papers_cli.config import AppPaths, ensure_paths
from papers_cli.downloader import download_pdf
from papers_cli.errors import PapersError


def test_download_content_addresses_and_deduplicates(tmp_path) -> None:
    paths = AppPaths(tmp_path / "data", tmp_path / "cache")
    ensure_paths(paths)
    body = b"%PDF-1.7\nfixture"

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


@pytest.mark.parametrize(
    ("headers", "body", "code"),
    [
        ({"content-type": "text/html"}, b"%PDF-1.7", "not_pdf"),
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
    assert not list(paths.staging_dir.iterdir())


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
