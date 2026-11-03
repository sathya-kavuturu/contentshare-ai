"""Finding, downloading and starting a newer version of ContentShare.

The project publishes each release on GitHub with the installer and a ``SHA256SUMS.txt`` file.
This module asks GitHub for the latest release, and if it is newer than the running program it
downloads the installer, checks it against the checksum file and hands it to Windows to run.

What this protects, and what it does not
* Only the project's own release page and download addresses are used, over HTTPS; a redirect
  to anything but HTTPS is refused.
* The installer must be named like a ContentShare installer, must not be larger than a sane
  limit, and must match the SHA-256 published in the same release. That catches a damaged or
  cut-off download. It cannot catch someone who has taken over the GitHub account and replaced
  both files; only a code-signing certificate would (the installer is not signed yet).
* Nothing about the PC, the settings or the mail is sent: the request is a plain GET of a
  public page. The installer is never started without the user clicking Update.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import logging
import os
import re
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, Protocol, cast
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

REPO = "sathya-kavuturu/content-sharing"
LATEST_RELEASE_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{REPO}/releases"
DOWNLOAD_PREFIX = f"https://github.com/{REPO}/releases/download/"
SUMS_NAME = "SHA256SUMS.txt"
INSTALLER = re.compile(r"^ContentShare-Setup-(\d{1,4})\.(\d{1,4})\.(\d{1,6})\.exe$")
VERSION = re.compile(r"^v?(\d{1,4})\.(\d{1,4})\.(\d{1,6})$")
MAX_INSTALLER_BYTES = 400 * 1024 * 1024  # the real one is about 40 MB
MAX_JSON_BYTES = 2 * 1024 * 1024
TIMEOUT_SECONDS = 20
CHUNK = 256 * 1024

Version = tuple[int, int, int]


class UpdateError(Exception):
    """Something went wrong; the text is written for the user."""


class UpdateCancelledError(Exception):
    """The user cancelled the download."""


def parse_version(text: str) -> Version | None:
    match = VERSION.match(text.strip())
    return (int(match[1]), int(match[2]), int(match[3])) if match else None


def show_version(version: Version) -> str:
    return ".".join(str(part) for part in version)


@dataclass(frozen=True)
class UpdateInfo:
    version: str  # "0.1.7"
    installer_name: str
    installer_url: str
    sums_url: str
    size: int  # bytes, as published
    page_url: str  # the release's page, for "what changed"


# --------------------------------------------------------------------------- network


class Http(Protocol):
    def get_json(self, url: str) -> Any: ...
    def get_text(self, url: str) -> str: ...
    def open_stream(self, url: str) -> IO[bytes]: ...


class _HttpsOnly(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        if urlsplit(newurl).scheme != "https":
            raise urllib.error.URLError("refused a redirect to a non-HTTPS address")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class UrlLibHttp:
    """Plain HTTPS reads with timeouts. Refuses anything that is not ``https://``."""

    def __init__(self) -> None:
        self._opener = urllib.request.build_opener(_HttpsOnly)

    def _open(self, url: str) -> IO[bytes]:
        if urlsplit(url).scheme != "https":
            raise UpdateError("Only secure (https) addresses are used for updates.")
        request = urllib.request.Request(  # noqa: S310 - https only, checked just above
            url, headers={"User-Agent": "ContentShare-updater", "Accept": "application/json"}
        )
        return cast(IO[bytes], self._opener.open(request, timeout=TIMEOUT_SECONDS))  # nosec B310

    def get_text(self, url: str) -> str:
        with self._open(url) as response:
            data = response.read(MAX_JSON_BYTES + 1)
        if len(data) > MAX_JSON_BYTES:
            raise UpdateError("The update information was unexpectedly large.")
        return data.decode("utf-8", "replace")

    def get_json(self, url: str) -> Any:
        try:
            return json.loads(self.get_text(url))
        except ValueError as exc:
            raise UpdateError("The update information could not be read.") from exc

    def open_stream(self, url: str) -> IO[bytes]:
        return self._open(url)


# --------------------------------------------------------------------------- the updater


def parse_sums(text: str) -> dict[str, str]:
    """``<sha256>  <file name>`` lines (the format ``sha256sum`` writes) -> {name: hash}."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]):
            out[parts[1].lstrip("*").strip()] = parts[0].lower()
    return out


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _start_installer(path: Path) -> None:
    # Same as double-clicking the file: Windows runs it for the current user.
    os.startfile(path)  # noqa: S606  # nosec B606


class Updater:
    def __init__(
        self,
        current_version: str,
        folder: Path,
        http: Http | None = None,
        launcher: Callable[[Path], None] = _start_installer,
    ) -> None:
        self.current = parse_version(current_version) or (0, 0, 0)
        self.folder = folder
        self._http = http or UrlLibHttp()
        self._launcher = launcher

    # ------------------------------------------------------------------ checking
    def check(self) -> UpdateInfo | None:
        """The newest release if it is newer than this program, else ``None``."""
        try:
            release = self._http.get_json(LATEST_RELEASE_URL)
        except UpdateError:
            raise
        except (OSError, urllib.error.URLError) as exc:
            log.info("update check failed: %s", type(exc).__name__)
            raise UpdateError(
                "Could not reach the update server. Check the internet connection."
            ) from exc
        return self._read_release(release)

    def _read_release(self, release: Any) -> UpdateInfo | None:
        if not isinstance(release, dict):
            raise UpdateError("The update information was not in the expected form.")
        version = parse_version(str(release.get("tag_name", "")))
        if version is None:
            raise UpdateError("The newest release has a version number ContentShare cannot read.")
        if version <= self.current:
            return None
        assets = {str(a.get("name")): a for a in release.get("assets") or [] if isinstance(a, dict)}
        installer = next((a for name, a in assets.items() if INSTALLER.match(name)), None)
        sums = assets.get(SUMS_NAME)
        if installer is None or sums is None:
            raise UpdateError(
                "The newest release does not include an installer and checksum file yet. "
                "Try again later."
            )
        name = str(installer["name"])
        match = INSTALLER.match(name)
        if match is None or (int(match[1]), int(match[2]), int(match[3])) != version:
            raise UpdateError("The release's installer does not match its version number.")
        urls = (
            str(installer.get("browser_download_url", "")),
            str(sums.get("browser_download_url", "")),
        )
        if not all(u.startswith(DOWNLOAD_PREFIX) for u in urls):
            raise UpdateError("The release points at an unexpected download address; not used.")
        size = installer.get("size")
        if not isinstance(size, int) or not 0 < size <= MAX_INSTALLER_BYTES:
            raise UpdateError("The installer's size is not what was expected; not used.")
        page = str(release.get("html_url", ""))
        return UpdateInfo(
            version=show_version(version),
            installer_name=name,
            installer_url=urls[0],
            sums_url=urls[1],
            size=size,
            page_url=page if page.startswith(RELEASES_PAGE) else RELEASES_PAGE,
        )

    # ------------------------------------------------------------------ downloading
    def target(self, info: UpdateInfo) -> Path:
        return self.folder / info.installer_name

    def download(
        self,
        info: UpdateInfo,
        progress: Callable[[int, int], None] | None = None,
        cancelled: Callable[[], bool] = lambda: False,
    ) -> Path:
        """Fetch and verify the installer. A copy that is already here and still matches the
        published checksum is reused. Raises ``UpdateError`` or ``UpdateCancelledError``."""
        self.folder.mkdir(parents=True, exist_ok=True)
        expected = self._expected_hash(info)
        final = self.target(info)
        if final.is_file() and hmac.compare_digest(sha256_of(final), expected):
            if progress:
                progress(info.size, info.size)
            return final
        part = final.with_name(final.name + ".part")
        try:
            self._fetch(info, part, progress, cancelled)
            if not hmac.compare_digest(sha256_of(part), expected):
                raise UpdateError(
                    "The downloaded installer did not match its published checksum, so it was "
                    "deleted. Try again; if it keeps happening, download it from the releases page."
                )
            part.replace(final)
        except BaseException:
            with contextlib.suppress(OSError):
                part.unlink()
            raise
        self._tidy(keep=final)
        return final

    def _expected_hash(self, info: UpdateInfo) -> str:
        try:
            sums = parse_sums(self._http.get_text(info.sums_url))
        except UpdateError:
            raise
        except (OSError, urllib.error.URLError) as exc:
            raise UpdateError("Could not download the checksum file.") from exc
        expected = sums.get(info.installer_name)
        if expected is None:
            raise UpdateError("The checksum file does not list this installer; not used.")
        return expected

    def _fetch(
        self,
        info: UpdateInfo,
        part: Path,
        progress: Callable[[int, int], None] | None,
        cancelled: Callable[[], bool],
    ) -> None:
        received = 0
        try:
            with self._http.open_stream(info.installer_url) as stream, part.open("wb") as out:
                while True:
                    if cancelled():
                        raise UpdateCancelledError
                    chunk = stream.read(CHUNK)
                    if not chunk:
                        break
                    received += len(chunk)
                    if received > min(MAX_INSTALLER_BYTES, info.size * 2):
                        raise UpdateError("The download was larger than expected; stopped.")
                    out.write(chunk)
                    if progress:
                        progress(received, info.size)
        except (OSError, urllib.error.URLError) as exc:
            log.info("update download failed: %s", type(exc).__name__)
            raise UpdateError(
                "The download was interrupted. Check the internet connection and try again."
            ) from exc

    def _tidy(self, keep: Path) -> None:
        """Delete installers this program downloaded earlier (only files named like ours)."""
        for old in self._ours():
            if old != keep:
                with contextlib.suppress(OSError):
                    old.unlink()

    def _ours(self) -> Iterator[Path]:
        if self.folder.is_dir():
            for entry in self.folder.iterdir():
                if entry.is_file() and INSTALLER.match(entry.name):
                    yield entry

    # ------------------------------------------------------------------ installing
    def install(self, path: Path) -> None:
        """Start the downloaded installer. Refuses any file that is not one of ours."""
        if path.parent != self.folder or not INSTALLER.match(path.name) or not path.is_file():
            raise UpdateError("That is not a downloaded ContentShare installer.")
        try:
            self._launcher(path)
        except OSError as exc:
            raise UpdateError(
                "Windows would not start the installer. Open the folder and run it yourself: "
                f"{self.folder}"
            ) from exc
