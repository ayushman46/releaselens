"""Locate (or, only if missing/corrupt, download) the TravisTorrent Figshare dump (ADR-0004).

The dataset version is the publisher's md5, verified on every call. Downloads stream to a
``.part`` file with HTTP range resume, are md5-verified, and only then renamed into place.
Hosts under ``testroots.org`` are refused outright: that domain now redirects to an unrelated
site (DATASET_AUDIT / ADR-0004), including when reached through a redirect.
"""

from __future__ import annotations

import http.client
import logging
import shutil
import urllib.request
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import IO, Any, Protocol
from urllib.parse import urlsplit

from rl_pipeline.hashing import md5_file

log = logging.getLogger("rl_pipeline.dataset")

_CHUNK = 1024 * 1024
_DISK_MARGIN = 256 * 1024 * 1024
BLOCKED_HOST_SUFFIXES: tuple[str, ...] = ("testroots.org",)


@dataclass(frozen=True, slots=True)
class DatasetSource:
    name: str
    doi: str
    url: str
    filename: str
    size: int
    md5: str
    license: str
    citation: str


TRAVISTORRENT_FIGSHARE = DatasetSource(
    name="travistorrent-figshare-19314170",
    doi="10.6084/m9.figshare.19314170",
    url="https://ndownloader.figshare.com/files/34303124",
    filename="final-2017-01-25.csv.gz",
    size=264_607_174,
    md5="5d359345fecf0bf47be6edb10b5aae58",
    license="CC BY 4.0",
    citation=(
        "Beller, M., Gousios, G., Zaidman, A. TravisTorrent: Synthesizing Travis CI and GitHub "
        "for Full-Stack Research on Continuous Integration. MSR 2017, "
        "DOI 10.1109/MSR.2017.24. Data: Figshare DOI 10.6084/m9.figshare.19314170 (CC BY 4.0)."
    ),
)


class DatasetError(RuntimeError):
    """Base class for dataset acquisition failures."""


class BlockedHostError(DatasetError):
    pass


class DatasetCorruptError(DatasetError):
    pass


class DatasetMissingError(DatasetError):
    pass


@dataclass(frozen=True, slots=True)
class LocatedDataset:
    path: str
    md5: str
    size: int
    version: str
    downloaded: bool

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def check_url_allowed(url: str) -> None:
    """Only https, and never a host under a blocked suffix (exact or any subdomain)."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower().rstrip(".")
    if parts.scheme != "https":
        raise BlockedHostError(f"refusing non-https dataset URL scheme {parts.scheme!r}")
    if not host:
        raise BlockedHostError("refusing dataset URL without a host")
    for suffix in BLOCKED_HOST_SUFFIXES:
        if host == suffix or host.endswith("." + suffix):
            raise BlockedHostError(f"refusing blocked host {host!r} (matches {suffix!r})")


# ---------------------------------------------------------------- HTTP (injectable for tests)
@dataclass
class RangeResponse:
    status: int
    chunks: Iterator[bytes]


class RangeFetcher(Protocol):
    def open(self, url: str, start: int) -> RangeResponse: ...


class _CheckedRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        check_url_allowed(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class UrllibRangeFetcher:
    """Stdlib HTTP client; every redirect target is re-checked against the host blocklist."""

    def __init__(self, timeout_s: float = 60.0) -> None:
        self._timeout = timeout_s
        self._opener = urllib.request.build_opener(_CheckedRedirectHandler())

    def open(self, url: str, start: int) -> RangeResponse:
        check_url_allowed(url)
        headers = {"User-Agent": "releaselens-pipeline/0.1"}
        if start > 0:
            headers["Range"] = f"bytes={start}-"
        req = urllib.request.Request(url, headers=headers)  # noqa: S310 - https enforced above
        resp = self._opener.open(req, timeout=self._timeout)

        def _iter() -> Iterator[bytes]:
            with resp:
                while chunk := resp.read(_CHUNK):
                    yield chunk

        return RangeResponse(status=int(resp.status), chunks=_iter())


# ---------------------------------------------------------------- locate / download
def _verify(path: Path, source: DatasetSource) -> str | None:
    """Return the md5 if ``path`` matches the source's size and md5, else ``None``."""
    if path.stat().st_size != source.size:
        return None
    digest = md5_file(path)
    return digest if digest == source.md5 else None


def _download(source: DatasetSource, dest: Path, fetcher: RangeFetcher, attempts: int) -> None:
    check_url_allowed(source.url)
    part = dest.with_name(dest.name + ".part")
    dest.parent.mkdir(parents=True, exist_ok=True)
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        start = part.stat().st_size if part.exists() else 0
        if start > source.size:
            part.unlink()
            start = 0
        free = shutil.disk_usage(dest.parent).free
        if free < (source.size - start) + _DISK_MARGIN:
            raise DatasetError(
                f"not enough free disk in {dest.parent}: {free} bytes free, "
                f"need {source.size - start} + {_DISK_MARGIN} margin"
            )
        if start == source.size:
            break
        log.info(
            "downloading dataset",
            extra={"url": source.url, "resume_from": start, "attempt": attempt},
        )
        try:
            resp = fetcher.open(source.url, start)
            if start > 0 and resp.status == 200:
                start = 0  # server ignored the range request: restart from scratch
            elif start > 0 and resp.status != 206:
                raise DatasetError(f"unexpected HTTP status {resp.status} for a range request")
            with part.open("ab" if start > 0 else "wb") as fh:
                for chunk in resp.chunks:
                    fh.write(chunk)
                    if fh.tell() > source.size:
                        raise DatasetError("download larger than the expected size")
        # URLError/timeouts are OSError; a connection dropped mid-body raises
        # http.client.IncompleteRead (an HTTPException, not an OSError) and must be retried too.
        except (OSError, http.client.HTTPException, DatasetError) as exc:
            if isinstance(exc, BlockedHostError):
                raise
            last_error = exc
            log.warning("download attempt failed", extra={"attempt": attempt, "error": str(exc)})
            continue
        if part.stat().st_size == source.size:
            break
    else:
        raise DatasetError(f"download failed after {attempts} attempts: {last_error}")

    digest = md5_file(part)
    if part.stat().st_size != source.size or digest != source.md5:
        part.unlink()
        raise DatasetCorruptError(
            f"downloaded file failed verification (md5 {digest}, expected {source.md5})"
        )
    part.replace(dest)


def locate_or_download(
    dataset_dir: Path,
    source: DatasetSource = TRAVISTORRENT_FIGSHARE,
    *,
    allow_download: bool = True,
    fetcher: RangeFetcher | None = None,
    attempts: int = 3,
) -> LocatedDataset:
    """Return the verified local dataset; download only when it is missing or corrupt."""
    dest = dataset_dir / source.filename
    downloaded = False
    if dest.is_file():
        digest = _verify(dest, source)
        if digest is not None:
            log.info("dataset located", extra={"path": str(dest), "md5": digest})
            return LocatedDataset(str(dest), digest, source.size, digest, downloaded=False)
        size = dest.stat().st_size
        if not allow_download:
            raise DatasetCorruptError(
                f"{dest} does not match the expected size/md5 ({size} bytes) and downloads "
                "are disabled"
            )
        log.warning("dataset present but corrupt; re-acquiring", extra={"size": size})
        part = dest.with_name(dest.name + ".part")
        if size < source.size and not part.exists():
            dest.replace(part)  # a truncated copy is a resumable prefix
        else:
            dest.unlink()
    elif not allow_download:
        raise DatasetMissingError(f"{dest} not found and downloads are disabled")

    _download(source, dest, fetcher or UrllibRangeFetcher(), attempts)
    downloaded = True
    digest = md5_file(dest)
    return LocatedDataset(str(dest), digest, source.size, digest, downloaded=downloaded)


def source_as_dict(source: DatasetSource = TRAVISTORRENT_FIGSHARE) -> dict[str, Any]:
    return asdict(source)
