"""Finding, downloading and installing a newer version, and the box that shows it.

No test touches the network: ``FakeHttp`` stands in for GitHub, and the "installer" is a few
bytes that is never run.
"""

from __future__ import annotations

import hashlib
import io
import threading
import urllib.error
from collections.abc import Iterator
from pathlib import Path
from typing import IO, Any

import pytest
from PySide6.QtWidgets import QApplication

from contentshare.app.updates import (
    DOWNLOAD_CHUNK,
    DOWNLOAD_PREFIX,
    LATEST_RELEASE_URL,
    MAX_INSTALLER_BYTES,
    RELEASES_PAGE,
    UpdateCancelledError,
    UpdateError,
    Updater,
    UrlLibHttp,
    _HttpsOnly,
    parse_sums,
    parse_version,
    show_version,
)
from contentshare.ui.bridge import Bridge
from contentshare.ui.theme import apply_theme
from contentshare.ui.update_card import State, UpdateCard

PAYLOAD = b"pretend this is an installer" * 1000


class FakeHttp:
    def __init__(self) -> None:
        self.json: Any = None
        self.json_error: Exception | None = None
        self.texts: dict[str, str] = {}
        self.streams: dict[str, bytes] = {}
        self.stream_factory: Any = None

    def get_json(self, url: str) -> Any:
        if self.json_error:
            raise self.json_error
        assert url == LATEST_RELEASE_URL
        return self.json

    def get_text(self, url: str) -> str:
        return self.texts[url]

    def open_stream(self, url: str) -> IO[bytes]:
        if self.stream_factory:
            return self.stream_factory(url)  # type: ignore[no-any-return]
        return io.BytesIO(self.streams[url])


def publish(
    http: FakeHttp, version: str = "0.2.0", payload: bytes = PAYLOAD, *, sha: str | None = None
) -> str:
    """Make ``http`` serve release ``version``; returns the installer's file name."""
    name = f"ContentShare-Setup-{version}.exe"
    base = f"{DOWNLOAD_PREFIX}v{version}/"
    http.json = {
        "tag_name": f"v{version}",
        "html_url": f"{RELEASES_PAGE}/tag/v{version}",
        "assets": [
            {"name": name, "browser_download_url": base + name, "size": len(payload)},
            {"name": "SHA256SUMS.txt", "browser_download_url": base + "SHA256SUMS.txt", "size": 90},
        ],
    }
    digest = sha or hashlib.sha256(payload).hexdigest()
    http.texts[base + "SHA256SUMS.txt"] = f"{digest}  {name}\n"
    http.streams[base + name] = payload
    return name


@pytest.fixture
def http() -> FakeHttp:
    return FakeHttp()


@pytest.fixture
def launched() -> list[Path]:
    return []


@pytest.fixture
def updater(http: FakeHttp, tmp_path: Path, launched: list[Path]) -> Updater:
    return Updater("0.1.5", tmp_path / "updates", http, launcher=launched.append)


# --------------------------------------------------------------------------- versions
def test_version_parsing() -> None:
    assert parse_version("v0.1.12") == (0, 1, 12)
    assert parse_version(" 1.2.3 ") == (1, 2, 3)
    assert show_version((0, 1, 12)) == "0.1.12"
    for bad in ("", "1.2", "1.2.3.4", "v1.2.x", "1.2.3-beta", "latest", "1.2.3\n4.5.6"):
        assert parse_version(bad) is None


def test_versions_compare_as_numbers_not_text() -> None:
    assert parse_version("0.1.10") > parse_version("0.1.9")  # type: ignore[operator]


def test_checksum_file_parsing() -> None:
    digest = "ab" * 32
    text = f"{digest}  ContentShare-Setup-0.2.0.exe\nnot a line\n{'zz' * 32}  bad.exe\n"
    assert parse_sums(text) == {"ContentShare-Setup-0.2.0.exe": digest}
    assert parse_sums(f"{digest.upper()} *file.exe") == {"file.exe": digest}


# --------------------------------------------------------------------------- checking
def test_a_newer_release_is_offered(updater: Updater, http: FakeHttp) -> None:
    name = publish(http, "0.2.0")
    info = updater.check()
    assert info is not None
    assert (info.version, info.installer_name, info.size) == ("0.2.0", name, len(PAYLOAD))
    assert info.page_url.startswith(RELEASES_PAGE)


@pytest.mark.parametrize("version", ["0.1.5", "0.1.4", "0.0.9"])
def test_the_same_or_an_older_release_is_not_offered(
    updater: Updater, http: FakeHttp, version: str
) -> None:
    publish(http, version)
    assert updater.check() is None


def test_a_release_without_its_files_yet_is_reported(updater: Updater, http: FakeHttp) -> None:
    publish(http, "0.2.0")
    http.json["assets"] = []
    with pytest.raises(UpdateError, match="installer and checksum"):
        updater.check()


def test_the_installer_name_must_match_the_release_version(
    updater: Updater, http: FakeHttp
) -> None:
    publish(http, "0.2.0")
    http.json["tag_name"] = "v0.3.0"
    with pytest.raises(UpdateError, match="does not match"):
        updater.check()


def test_downloads_only_from_the_projects_own_release_address(
    updater: Updater, http: FakeHttp
) -> None:
    publish(http, "0.2.0")
    http.json["assets"][0]["browser_download_url"] = "https://evil.example/ContentShare.exe"
    with pytest.raises(UpdateError, match="unexpected download address"):
        updater.check()


@pytest.mark.parametrize("size", [0, -5, MAX_INSTALLER_BYTES + 1, "40", None])
def test_an_implausible_size_is_refused(updater: Updater, http: FakeHttp, size: Any) -> None:
    publish(http, "0.2.0")
    http.json["assets"][0]["size"] = size
    with pytest.raises(UpdateError, match="size"):
        updater.check()


@pytest.mark.parametrize("payload", [None, [], "text", {"tag_name": "nightly"}])
def test_nonsense_from_the_server_is_an_error_not_a_crash(
    updater: Updater, http: FakeHttp, payload: Any
) -> None:
    http.json = payload
    with pytest.raises(UpdateError):
        updater.check()


def test_a_network_failure_is_explained(updater: Updater, http: FakeHttp) -> None:
    http.json_error = urllib.error.URLError("no route")
    with pytest.raises(UpdateError, match="internet connection"):
        updater.check()


# --------------------------------------------------------------------------- downloading
def test_download_verifies_and_keeps_the_installer(
    updater: Updater, http: FakeHttp, tmp_path: Path
) -> None:
    publish(http, "0.2.0")
    info = updater.check()
    assert info
    seen: list[tuple[int, int]] = []
    path = updater.download(info, lambda got, total: seen.append((got, total)))
    assert path == tmp_path / "updates" / info.installer_name
    assert path.read_bytes() == PAYLOAD
    assert seen[-1] == (len(PAYLOAD), len(PAYLOAD)) and seen == sorted(seen)
    assert [p.name for p in path.parent.iterdir()] == [info.installer_name]  # no .part left


def test_progress_starts_at_once_and_moves_in_small_steps(updater: Updater, http: FakeHttp) -> None:
    big = b"x" * (DOWNLOAD_CHUNK * 5 + 123)
    publish(http, "0.2.0", big)
    info = updater.check()
    assert info
    seen: list[tuple[int, int]] = []
    updater.download(info, lambda got, total: seen.append((got, total)))
    # "connecting" is reported before the checksum and the connection, then every small block
    assert seen[0] == (0, len(big))
    assert len(seen) == 1 + 6 and seen[-1] == (len(big), len(big))
    assert seen == sorted(seen)


def test_the_installer_is_requested_as_a_binary_download(monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[str] = []
    http = UrlLibHttp()
    monkeypatch.setattr(http, "_open", lambda url, accept="application/json": asked.append(accept))
    http.open_stream("https://github.com/x")
    assert asked == ["application/octet-stream"]


def test_a_checksum_mismatch_deletes_the_download(
    updater: Updater, http: FakeHttp, tmp_path: Path
) -> None:
    publish(http, "0.2.0", sha="0" * 64)
    info = updater.check()
    assert info
    with pytest.raises(UpdateError, match="did not match"):
        updater.download(info)
    assert list((tmp_path / "updates").iterdir()) == []


def test_an_installer_missing_from_the_checksum_file_is_refused(
    updater: Updater, http: FakeHttp
) -> None:
    publish(http, "0.2.0")
    info = updater.check()
    assert info
    http.texts[info.sums_url] = f"{'1' * 64}  other-file.exe\n"
    with pytest.raises(UpdateError, match="does not list"):
        updater.download(info)


def test_an_existing_good_download_is_reused(updater: Updater, http: FakeHttp) -> None:
    publish(http, "0.2.0")
    info = updater.check()
    assert info
    first = updater.download(info)
    http.streams.clear()  # a second download would now fail
    assert updater.download(info) == first


def test_a_damaged_earlier_download_is_fetched_again(updater: Updater, http: FakeHttp) -> None:
    publish(http, "0.2.0")
    info = updater.check()
    assert info
    path = updater.download(info)
    path.write_bytes(b"cut off")
    assert updater.download(info).read_bytes() == PAYLOAD


def test_cancelling_leaves_nothing_behind(updater: Updater, http: FakeHttp, tmp_path: Path) -> None:
    publish(http, "0.2.0")
    info = updater.check()
    assert info
    calls = 0

    def cancelled() -> bool:
        nonlocal calls
        calls += 1
        return calls > 1

    with pytest.raises(UpdateCancelledError):
        updater.download(info, cancelled=cancelled)
    assert list((tmp_path / "updates").iterdir()) == []


def test_a_dropped_connection_is_explained_and_cleaned_up(
    updater: Updater, http: FakeHttp, tmp_path: Path
) -> None:
    publish(http, "0.2.0")
    info = updater.check()
    assert info

    class Dropping(io.BytesIO):
        def read(self, size: int | None = -1) -> bytes:
            raise ConnectionResetError

    http.stream_factory = lambda _url: Dropping()
    with pytest.raises(UpdateError, match="interrupted"):
        updater.download(info)
    assert list((tmp_path / "updates").iterdir()) == []


def test_a_download_larger_than_published_is_stopped(
    updater: Updater, http: FakeHttp, tmp_path: Path
) -> None:
    publish(http, "0.2.0")
    info = updater.check()
    assert info
    http.streams[info.installer_url] = b"x" * (len(PAYLOAD) * 3)
    with pytest.raises(UpdateError, match="larger than expected"):
        updater.download(info)
    assert list((tmp_path / "updates").iterdir()) == []


def test_older_downloaded_installers_are_tidied_but_nothing_else(
    updater: Updater, http: FakeHttp, tmp_path: Path
) -> None:
    folder = tmp_path / "updates"
    folder.mkdir()
    (folder / "ContentShare-Setup-0.1.6.exe").write_bytes(b"old")
    (folder / "notes.txt").write_text("mine")
    publish(http, "0.2.0")
    info = updater.check()
    assert info
    updater.download(info)
    assert sorted(p.name for p in folder.iterdir()) == [info.installer_name, "notes.txt"]


# --------------------------------------------------------------------------- installing
def test_install_starts_only_our_own_downloaded_installer(
    updater: Updater, http: FakeHttp, launched: list[Path], tmp_path: Path
) -> None:
    publish(http, "0.2.0")
    info = updater.check()
    assert info
    path = updater.download(info)
    updater.install(path)
    assert launched == [path]
    for stranger in (
        tmp_path / "ContentShare-Setup-0.2.0.exe",  # right name, wrong folder
        tmp_path / "updates" / "calc.exe",  # wrong name
    ):
        stranger.write_bytes(b"x")
        with pytest.raises(UpdateError, match="not a downloaded"):
            updater.install(stranger)
    assert launched == [path]


def test_windows_refusing_to_start_the_installer_is_explained(
    http: FakeHttp, tmp_path: Path
) -> None:
    def refuse(_path: Path) -> None:
        raise PermissionError

    updater = Updater("0.1.5", tmp_path / "u", http, launcher=refuse)
    publish(http, "0.2.0")
    info = updater.check()
    assert info
    with pytest.raises(UpdateError, match="run it yourself"):
        updater.install(updater.download(info))


# --------------------------------------------------------------------------- real HTTP class
def test_plain_http_is_never_used() -> None:
    client = UrlLibHttp()
    for call in (client.get_text, client.get_json, client.open_stream):
        with pytest.raises(UpdateError, match="https"):
            call("http://github.com/x")


def test_a_redirect_away_from_https_is_refused() -> None:
    handler = _HttpsOnly()
    with pytest.raises(urllib.error.URLError, match="non-HTTPS"):
        handler.redirect_request(  # type: ignore[no-untyped-call]
            None, None, 302, "Found", {}, "http://evil.example/x.exe"
        )


# --------------------------------------------------------------------------- the box
class Rig:
    def __init__(self, updater: Updater, bridge: Bridge) -> None:
        self.busy = False
        self.card = UpdateCard(updater, bridge, lambda: self.busy)
        self.found: list[str] = []
        self.card.found.connect(self.found.append)


@pytest.fixture
def rig(qtbot: Any, updater: Updater) -> Iterator[Rig]:
    apply_theme(QApplication.instance())  # type: ignore[arg-type]
    bridge = Bridge()
    r = Rig(updater, bridge)
    qtbot.addWidget(r.card)
    yield r
    bridge.close()


def buttons(rig: Rig) -> tuple[str, str]:
    first = rig.card.primary.text() if not rig.card.primary.isHidden() else ""
    second = rig.card.secondary.text() if not rig.card.secondary.isHidden() else ""
    return first, second


def test_the_box_is_hidden_until_there_is_something_to_say(rig: Rig) -> None:
    assert rig.card.isHidden() and rig.card.state is State.HIDDEN


def test_a_new_version_shows_download_and_not_now(qtbot: Any, rig: Rig, http: FakeHttp) -> None:
    publish(http, "0.2.0")
    rig.card.check()
    qtbot.waitUntil(lambda: rig.card.state is State.AVAILABLE)
    assert not rig.card.isHidden()
    assert "0.2.0" in rig.card.title.text() and "0.1.5" in rig.card.detail.text()
    assert buttons(rig) == ("Download", "Not now")
    assert rig.found == ["0.2.0"]
    rig.card.secondary.click()  # Not now
    assert rig.card.isHidden()


def test_download_then_update_and_cancel_buttons(
    qtbot: Any, rig: Rig, http: FakeHttp, launched: list[Path]
) -> None:
    publish(http, "0.2.0")
    rig.card.check()
    qtbot.waitUntil(lambda: rig.card.state is State.AVAILABLE)
    rig.card.primary.click()  # Download
    qtbot.waitUntil(lambda: rig.card.state is State.READY)
    assert buttons(rig) == ("Update", "Cancel")
    assert "ready to install" in rig.card.title.text()
    assert not launched  # nothing runs until Update is pressed
    rig.card.primary.click()  # Update
    assert len(launched) == 1 and launched[0].name == "ContentShare-Setup-0.2.0.exe"
    assert "installer is starting" in rig.card.title.text()
    rig.card.primary.click()  # OK
    assert rig.card.isHidden()


def test_cancel_on_the_ready_box_keeps_the_download_but_installs_nothing(
    qtbot: Any, rig: Rig, http: FakeHttp, launched: list[Path], tmp_path: Path
) -> None:
    publish(http, "0.2.0")
    rig.card.check()
    qtbot.waitUntil(lambda: rig.card.state is State.AVAILABLE)
    rig.card.primary.click()
    qtbot.waitUntil(lambda: rig.card.state is State.READY)
    rig.card.secondary.click()  # Cancel
    assert rig.card.isHidden() and not launched
    assert (tmp_path / "updates" / "ContentShare-Setup-0.2.0.exe").exists()


def test_update_waits_while_copies_are_running(
    qtbot: Any, rig: Rig, http: FakeHttp, launched: list[Path]
) -> None:
    publish(http, "0.2.0")
    rig.card.check()
    qtbot.waitUntil(lambda: rig.card.state is State.AVAILABLE)
    rig.card.primary.click()
    qtbot.waitUntil(lambda: rig.card.state is State.READY)
    rig.busy = True
    rig.card.primary.click()
    assert not launched and rig.card.state is State.READY
    assert "Copies are running" in rig.card.detail.text()
    rig.busy = False
    rig.card.primary.click()
    assert len(launched) == 1


def test_a_bad_checksum_is_shown_not_installed(
    qtbot: Any, rig: Rig, http: FakeHttp, launched: list[Path]
) -> None:
    publish(http, "0.2.0", sha="f" * 64)
    rig.card.check()
    qtbot.waitUntil(lambda: rig.card.state is State.AVAILABLE)
    rig.card.primary.click()
    qtbot.waitUntil(lambda: rig.card.state is State.MESSAGE)
    assert "not downloaded" in rig.card.title.text() and "checksum" in rig.card.detail.text()
    assert buttons(rig) == ("OK", "") and not launched


def test_cancelling_a_running_download_stops_it_and_ignores_the_late_result(
    qtbot: Any, rig: Rig, http: FakeHttp, launched: list[Path]
) -> None:
    publish(http, "0.2.0")
    rig.card.check()
    qtbot.waitUntil(lambda: rig.card.state is State.AVAILABLE)
    started, release = threading.Event(), threading.Event()

    class Slow(io.BytesIO):
        def read(self, size: int | None = -1) -> bytes:
            started.set()
            release.wait(5)
            return super().read(size)

    http.stream_factory = lambda _url: Slow(PAYLOAD)
    rig.card.primary.click()
    assert rig.card.state is State.DOWNLOADING and buttons(rig) == ("", "Cancel")
    assert started.wait(5)
    rig.card.secondary.click()  # Cancel
    assert rig.card.isHidden()
    release.set()
    qtbot.wait(300)
    assert rig.card.isHidden() and not launched  # the stopped job did not bring it back


def test_progress_is_shown_while_downloading(qtbot: Any, rig: Rig, http: FakeHttp) -> None:
    publish(http, "0.2.0")
    rig.card.check()
    qtbot.waitUntil(lambda: rig.card.state is State.AVAILABLE)
    rig.card.info = rig.card.info
    rig.card._show(State.DOWNLOADING)
    rig.card._on_progress(len(PAYLOAD) // 2, len(PAYLOAD))
    assert rig.card.bar.value() == 50 and " of " in rig.card.detail.text()
    assert not rig.card.bar.isHidden()


def test_the_box_says_it_is_connecting_until_the_first_bytes_arrive(
    qtbot: Any, rig: Rig, http: FakeHttp
) -> None:
    publish(http, "0.2.0")
    rig.card.check()
    qtbot.waitUntil(lambda: rig.card.state is State.AVAILABLE)
    rig.card._show(State.DOWNLOADING)
    # no percentage yet: a moving bar and a line saying what is happening, not a stuck 0%
    assert (rig.card.bar.minimum(), rig.card.bar.maximum()) == (0, 0)
    assert "Connecting" in rig.card.detail.text()
    rig.card._on_progress(0, len(PAYLOAD))
    assert rig.card.bar.maximum() == 0
    rig.card._on_progress(len(PAYLOAD) // 4, len(PAYLOAD))
    assert (rig.card.bar.minimum(), rig.card.bar.maximum()) == (0, 100)
    assert rig.card.bar.value() == 25 and "Connecting" not in rig.card.detail.text()


def test_automatic_checks_stay_quiet_but_manual_ones_answer(
    qtbot: Any, rig: Rig, http: FakeHttp
) -> None:
    publish(http, "0.1.5")  # the same version as the program
    rig.card.check()
    qtbot.wait(200)
    assert rig.card.isHidden()  # automatic: nothing to say
    rig.card.check(manual=True)
    qtbot.waitUntil(lambda: "latest version" in rig.card.title.text())
    assert buttons(rig) == ("OK", "")
    http.json_error = urllib.error.URLError("offline")
    rig.card.primary.click()
    rig.card.check()
    qtbot.wait(200)
    assert rig.card.isHidden()  # a failed automatic check is not worth a box
    rig.card.check(manual=True)
    qtbot.waitUntil(lambda: "Could not check" in rig.card.title.text())
    assert "internet connection" in rig.card.detail.text()


def test_a_check_is_ignored_while_an_update_is_being_handled(
    qtbot: Any, rig: Rig, http: FakeHttp
) -> None:
    publish(http, "0.2.0")
    rig.card.check()
    qtbot.waitUntil(lambda: rig.card.state is State.AVAILABLE)
    rig.card.primary.click()
    qtbot.waitUntil(lambda: rig.card.state is State.READY)
    http.json_error = urllib.error.URLError("offline")
    rig.card.check(manual=True)
    qtbot.wait(100)
    assert rig.card.state is State.READY
