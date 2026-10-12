from __future__ import annotations

import hashlib
import http.client
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest

from rl_pipeline.dataset import (
    TRAVISTORRENT_FIGSHARE,
    BlockedHostError,
    DatasetCorruptError,
    DatasetMissingError,
    DatasetSource,
    RangeResponse,
    _CheckedRedirectHandler,
    check_url_allowed,
    locate_or_download,
)

PAYLOAD = bytes(range(256)) * 40  # 10,240 synthetic bytes


def _source(payload: bytes = PAYLOAD) -> DatasetSource:
    return replace(
        TRAVISTORRENT_FIGSHARE,
        url="https://example.org/files/1",
        filename="synthetic.csv.gz",
        size=len(payload),
        md5=hashlib.md5(payload, usedforsecurity=False).hexdigest(),
    )


@dataclass
class FakeFetcher:
    """Serves ``payload`` with HTTP range support; records requested offsets."""

    payload: bytes
    honour_range: bool = True
    cut_after: int | None = None  # simulate a dropped connection on the first request
    starts: list[int] = field(default_factory=list)

    def open(self, url: str, start: int) -> RangeResponse:
        check_url_allowed(url)
        self.starts.append(start)
        if start and self.honour_range:
            body, status = self.payload[start:], 206
        else:
            body, status = self.payload, 200
        if self.cut_after is not None and len(self.starts) == 1:
            body = body[: self.cut_after]

        def chunks() -> Iterator[bytes]:
            for i in range(0, len(body), 1000):
                yield body[i : i + 1000]

        return RangeResponse(status, chunks())


class ExplodingFetcher:
    def open(self, url: str, start: int) -> RangeResponse:
        raise AssertionError("must not download")


def test_locates_existing_file_without_download(tmp_path: Path) -> None:
    src = _source()
    (tmp_path / src.filename).write_bytes(PAYLOAD)
    got = locate_or_download(tmp_path, src, fetcher=ExplodingFetcher())
    assert got.downloaded is False
    assert got.md5 == got.version == src.md5
    assert got.size == len(PAYLOAD)


def test_missing_and_downloads_disabled(tmp_path: Path) -> None:
    with pytest.raises(DatasetMissingError):
        locate_or_download(tmp_path, _source(), allow_download=False)


def test_downloads_when_missing_and_verifies(tmp_path: Path) -> None:
    fetcher = FakeFetcher(PAYLOAD)
    got = locate_or_download(tmp_path, _source(), fetcher=fetcher)
    assert got.downloaded is True
    assert (tmp_path / "synthetic.csv.gz").read_bytes() == PAYLOAD
    assert not (tmp_path / "synthetic.csv.gz.part").exists()
    assert fetcher.starts == [0]


def test_resumes_from_partial_download(tmp_path: Path) -> None:
    (tmp_path / "synthetic.csv.gz.part").write_bytes(PAYLOAD[:4000])
    fetcher = FakeFetcher(PAYLOAD)
    locate_or_download(tmp_path, _source(), fetcher=fetcher)
    assert fetcher.starts == [4000]
    assert (tmp_path / "synthetic.csv.gz").read_bytes() == PAYLOAD


def test_resumes_after_dropped_connection(tmp_path: Path) -> None:
    fetcher = FakeFetcher(PAYLOAD, cut_after=3000)
    locate_or_download(tmp_path, _source(), fetcher=fetcher)
    assert fetcher.starts == [0, 3000]
    assert (tmp_path / "synthetic.csv.gz").read_bytes() == PAYLOAD


def test_restarts_when_server_ignores_range(tmp_path: Path) -> None:
    (tmp_path / "synthetic.csv.gz.part").write_bytes(PAYLOAD[:4000])
    fetcher = FakeFetcher(PAYLOAD, honour_range=False)
    locate_or_download(tmp_path, _source(), fetcher=fetcher)
    assert (tmp_path / "synthetic.csv.gz").read_bytes() == PAYLOAD


def test_truncated_final_file_is_resumed(tmp_path: Path) -> None:
    (tmp_path / "synthetic.csv.gz").write_bytes(PAYLOAD[:5000])
    fetcher = FakeFetcher(PAYLOAD)
    got = locate_or_download(tmp_path, _source(), fetcher=fetcher)
    assert got.downloaded is True
    assert fetcher.starts == [5000]


def test_corrupt_full_size_file_is_redownloaded(tmp_path: Path) -> None:
    (tmp_path / "synthetic.csv.gz").write_bytes(b"\x00" * len(PAYLOAD))
    got = locate_or_download(tmp_path, _source(), fetcher=FakeFetcher(PAYLOAD))
    assert got.downloaded is True
    assert (tmp_path / "synthetic.csv.gz").read_bytes() == PAYLOAD


def test_corrupt_file_with_downloads_disabled_fails(tmp_path: Path) -> None:
    (tmp_path / "synthetic.csv.gz").write_bytes(b"\x00" * len(PAYLOAD))
    with pytest.raises(DatasetCorruptError):
        locate_or_download(tmp_path, _source(), allow_download=False)
    assert (tmp_path / "synthetic.csv.gz").exists()  # never deleted when downloads are off


def test_md5_mismatch_after_download_is_rejected(tmp_path: Path) -> None:
    bad = bytes(reversed(PAYLOAD))
    with pytest.raises(DatasetCorruptError, match="failed verification"):
        locate_or_download(tmp_path, _source(), fetcher=FakeFetcher(bad))
    assert not (tmp_path / "synthetic.csv.gz").exists()
    assert not (tmp_path / "synthetic.csv.gz.part").exists()


@pytest.mark.parametrize(
    "url",
    [
        "https://testroots.org/x.csv.gz",
        "https://travistorrent.testroots.org/dumps/x.csv.gz",
        "https://TRAVISTORRENT.TESTROOTS.ORG./x",
        "http://ndownloader.figshare.com/files/34303124",
        "ftp://example.org/x",
    ],
)
def test_blocked_urls(url: str) -> None:
    with pytest.raises(BlockedHostError):
        check_url_allowed(url)


def test_allowed_urls() -> None:
    check_url_allowed(TRAVISTORRENT_FIGSHARE.url)
    check_url_allowed("https://nottestroots.org.example.com/x")


def test_blocked_source_never_fetches(tmp_path: Path) -> None:
    src = replace(_source(), url="https://travistorrent.testroots.org/x.csv.gz")
    with pytest.raises(BlockedHostError):
        locate_or_download(tmp_path, src, fetcher=ExplodingFetcher())


def test_redirect_to_blocked_host_is_refused() -> None:
    handler = _CheckedRedirectHandler()
    with pytest.raises(BlockedHostError):
        handler.redirect_request(
            None,  # type: ignore[arg-type]
            None,  # type: ignore[arg-type]
            302,
            "Found",
            {},
            "https://travistorrent.testroots.org/evil",
        )


def test_real_source_constants() -> None:
    assert TRAVISTORRENT_FIGSHARE.size == 264_607_174
    assert TRAVISTORRENT_FIGSHARE.md5 == "5d359345fecf0bf47be6edb10b5aae58"
    assert TRAVISTORRENT_FIGSHARE.doi == "10.6084/m9.figshare.19314170"
    assert TRAVISTORRENT_FIGSHARE.license == "CC BY 4.0"


@dataclass
class IncompleteReadFetcher(FakeFetcher):
    """First response dies mid-body with http.client.IncompleteRead (not an OSError)."""

    def open(self, url: str, start: int) -> RangeResponse:
        resp = super().open(url, start)
        if len(self.starts) > 1:
            return resp

        def chunks() -> Iterator[bytes]:
            yield next(iter(resp.chunks))
            raise http.client.IncompleteRead(b"", 123)

        return RangeResponse(resp.status, chunks())


def test_incomplete_read_is_retried_and_resumed(tmp_path: Path) -> None:
    fetcher = IncompleteReadFetcher(PAYLOAD)
    got = locate_or_download(tmp_path, _source(), fetcher=fetcher)
    assert got.downloaded is True
    assert fetcher.starts == [0, 1000]
    assert (tmp_path / "synthetic.csv.gz").read_bytes() == PAYLOAD
