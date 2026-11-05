"""Installing and running the local model.

* ``Installer`` downloads the model and the program that runs it (``llama-server`` from
  llama.cpp) into ``%APPDATA%\\ContentShareAI\\llm``, checks every file against the SHA-256 in
  ``catalog.py`` before it is used, and unpacks the zip without letting it write outside its
  folder.
* ``Runtime`` starts that program on demand, listening only on this PC, protected by a random
  key, and stops it after it has been idle for a while so the memory is given back.
"""

from __future__ import annotations

import atexit
import contextlib
import hashlib
import json
import logging
import os
import secrets
import shutil
import socket
import subprocess  # nosec B404 - runs only the checked, pinned llama-server we installed
import threading
import time
import urllib.error
import urllib.request
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

from contentshare.app.updates import UrlLibHttp
from contentshare.config.models import SmartSettings
from contentshare.smart.catalog import MODELS, Download, program_for

log = logging.getLogger(__name__)

SERVER_EXE = "llama-server.exe"
START_TIMEOUT_SECONDS = 240  # loading a model from a slow disk
CALL_TIMEOUT_SECONDS = 180
MAX_UNPACKED_BYTES = 900_000_000
CHUNK = 1024 * 1024
CREATE_NO_WINDOW = 0x08000000


class SmartError(Exception):
    """The message is written for the person and shown as it is."""


@dataclass(frozen=True)
class Status:
    program_ready: bool
    model_ready: bool
    missing_bytes: int
    model_label: str

    @property
    def ready(self) -> bool:
        return self.program_ready and self.model_ready


class Installer:
    def __init__(self, root: Path, http: Any = None) -> None:
        self.root = root
        self._http = http or UrlLibHttp()

    # ------------------------------------------------------------------ where things are
    def program_dir(self, settings: SmartSettings) -> Path:
        return self.root / "program" / Path(program_for(settings.use_graphics_card).name).stem

    def program_exe(self, settings: SmartSettings) -> Path:
        return self.program_dir(settings) / SERVER_EXE

    def model_file(self, settings: SmartSettings) -> Path:
        return self.root / "models" / MODELS[settings.model].file.name

    def status(self, settings: SmartSettings) -> Status:
        model = MODELS[settings.model].file
        program_ready = self.program_exe(settings).is_file()
        model_ready = self.model_file(settings).is_file()
        missing = (0 if program_ready else program_for(settings.use_graphics_card).size) + (
            0 if model_ready else model.size
        )
        return Status(program_ready, model_ready, missing, MODELS[settings.model].label)

    # ------------------------------------------------------------------ installing
    def install(
        self,
        settings: SmartSettings,
        *,
        progress: Callable[[str, int, int], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> None:
        """Download what is missing. ``progress(what, done_bytes, total_bytes)``."""
        status = self.status(settings)
        free = shutil.disk_usage(
            self.root.parent if self.root.parent.exists() else self.root.anchor
        )
        if free.free < status.missing_bytes * 2:
            raise SmartError(
                "There is not enough free space on this drive for the download "
                f"(about {status.missing_bytes * 2 // 1_000_000_000 + 1} GB needed)."
            )
        if not status.program_ready:
            self._install_program(settings, progress, should_stop)
        if not status.model_ready:
            self._download(
                MODELS[settings.model].file, self.model_file(settings), progress, should_stop
            )

    def _install_program(
        self,
        settings: SmartSettings,
        progress: Callable[[str, int, int], None] | None,
        should_stop: Callable[[], bool] | None,
    ) -> None:
        item = program_for(settings.use_graphics_card)
        archive = self.root / "downloads" / item.name
        self._download(item, archive, progress, should_stop)
        target = self.program_dir(settings)
        shutil.rmtree(target, ignore_errors=True)
        try:
            unpack(archive, target)
        except (zipfile.BadZipFile, SmartError):
            shutil.rmtree(target, ignore_errors=True)
            raise
        finally:
            with contextlib.suppress(OSError):
                archive.unlink()
        if not (target / SERVER_EXE).is_file():
            shutil.rmtree(target, ignore_errors=True)
            raise SmartError("The downloaded program did not contain llama-server.")

    def _download(
        self,
        item: Download,
        destination: Path,
        progress: Callable[[str, int, int], None] | None,
        should_stop: Callable[[], bool] | None,
    ) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        part = destination.with_name(destination.name + ".part")
        digest = hashlib.sha256()
        done = 0
        try:
            with self._http.open_stream(item.url) as response, part.open("wb") as out:
                while True:
                    if should_stop and should_stop():
                        raise SmartError("The download was stopped.")
                    block = response.read(CHUNK)
                    if not block:
                        break
                    out.write(block)
                    digest.update(block)
                    done += len(block)
                    if progress:
                        progress(item.name, done, max(item.size, done))
            if digest.hexdigest() != item.sha256:
                raise SmartError(
                    f"{item.name} did not match the expected checksum, so it was not used. "
                    "Try again; if it keeps happening the download is being changed on the way."
                )
            part.replace(destination)
        except OSError as exc:
            raise SmartError(f"Could not download {item.name}: {exc}") from exc
        finally:
            with contextlib.suppress(OSError):
                part.unlink()


def unpack(archive: Path, target: Path) -> None:
    """Unzip into ``target`` only. Names that climb out of it, absolute names and archives
    that would unpack to something huge are refused."""
    target.mkdir(parents=True, exist_ok=True)
    root = target.resolve()
    with zipfile.ZipFile(archive) as bundle:
        members = bundle.infolist()
        if sum(m.file_size for m in members) > MAX_UNPACKED_BYTES:
            raise SmartError("The downloaded program is larger than expected, so it was not used.")
        for member in members:
            name = member.filename.replace("\\", "/")
            if name.endswith("/"):
                continue
            destination = (target / name).resolve()
            if (
                not destination.is_relative_to(root)
                or name.startswith("/")
                or ".." in name.split("/")
            ):
                raise SmartError("The downloaded program contains an unsafe file name.")
            destination.parent.mkdir(parents=True, exist_ok=True)
            with bundle.open(member) as source, destination.open("wb") as out:
                shutil.copyfileobj(source, out)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


CommandBuilder = Callable[[Path, Path, int, str, SmartSettings], list[str]]


def llama_command(
    exe: Path, model: Path, port: int, key: str, settings: SmartSettings
) -> list[str]:
    return [
        str(exe), "-m", str(model), "--host", "127.0.0.1", "--port", str(port),
        "--api-key", key, "-c", "4096", "-np", "1", "-t", str(max(2, (os.cpu_count() or 4) // 2)),
        "-ngl", "99" if settings.use_graphics_card else "0", "--no-webui",
    ]  # fmt: skip


class Runtime:
    """One local model server, started when first needed and stopped when idle."""

    def __init__(
        self,
        installer: Installer,
        settings: SmartSettings,
        *,
        log_file: Path | None = None,
        command_builder: CommandBuilder = llama_command,
    ) -> None:
        self.installer, self.settings = installer, settings
        self._log_file = log_file
        self._command = command_builder
        self._lock = threading.Lock()
        self._process: subprocess.Popen[bytes] | None = None
        self._log_handle: IO[bytes] | None = None
        self._port = 0
        self._key = ""
        self._timer: threading.Timer | None = None
        atexit.register(self.stop)

    def update(self, settings: SmartSettings) -> None:
        """Settings changed: a running model is stopped so the new choice is used."""
        if settings != self.settings:
            self.stop()
            self.settings = settings

    # ------------------------------------------------------------------ using it
    def chat(self, messages: list[dict[str, str]], schema: dict[str, Any]) -> str:
        """One answer in the given JSON shape. Starts the model if it is not running."""
        with self._lock:
            self._ensure_started()
            self._arm_idle_timer()
            return _ask(self._port, self._key, messages, schema)

    def start(self) -> None:
        """Start the model now (it also starts by itself when mail has to be read)."""
        with self._lock:
            self._ensure_started()
            self._arm_idle_timer()

    def running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def stop(self) -> None:
        with self._lock:
            self._stop_locked()

    # ------------------------------------------------------------------ inside
    def _ensure_started(self) -> None:
        if self.running():
            return
        self._stop_locked()
        status = self.installer.status(self.settings)
        if not status.ready:
            raise SmartError("The smart reader is not installed yet (Settings → Smart reading).")
        self._port, self._key = free_port(), secrets.token_hex(16)
        command = self._command(
            self.installer.program_exe(self.settings),
            self.installer.model_file(self.settings),
            self._port,
            self._key,
            self.settings,
        )
        self._log_handle = self._open_log()
        try:
            self._process = subprocess.Popen(  # noqa: S603  # nosec B603 - our own pinned program
                command,
                stdin=subprocess.DEVNULL,
                stdout=self._log_handle or subprocess.DEVNULL,
                stderr=subprocess.STDOUT,
                creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except OSError as exc:
            self._stop_locked()
            raise SmartError(f"The smart reader could not be started: {exc}") from exc
        self._wait_until_ready()

    def _open_log(self) -> IO[bytes] | None:
        if self._log_file is None:
            return None
        self._log_file.parent.mkdir(parents=True, exist_ok=True)
        if self._log_file.exists() and self._log_file.stat().st_size > 2_000_000:
            self._log_file.unlink()
        return self._log_file.open("ab")

    def _wait_until_ready(self) -> None:
        deadline = time.monotonic() + START_TIMEOUT_SECONDS
        url = f"http://127.0.0.1:{self._port}/health"
        while time.monotonic() < deadline:
            if self._process is None or self._process.poll() is not None:
                self._stop_locked()
                raise SmartError(
                    "The smart reader stopped while starting. See llm.log in the logs folder."
                )
            try:
                request = urllib.request.Request(  # nosec B310 - local address
                    url, headers={"Authorization": f"Bearer {self._key}"}
                )
                with urllib.request.urlopen(request, timeout=3) as response:  # noqa: S310  # nosec B310
                    if response.status == 200:
                        return
            except (urllib.error.URLError, OSError, ValueError):
                pass
            time.sleep(0.5)
        self._stop_locked()
        raise SmartError("The smart reader took too long to start.")

    def _arm_idle_timer(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
        timer = threading.Timer(self.settings.keep_loaded_minutes * 60, self.stop)
        timer.daemon = True
        timer.start()
        self._timer = timer

    def _stop_locked(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        process, self._process = self._process, None
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
        if self._log_handle is not None:
            with contextlib.suppress(OSError):
                self._log_handle.close()
            self._log_handle = None


def _ask(port: int, key: str, messages: list[dict[str, str]], schema: dict[str, Any]) -> str:
    body = json.dumps(
        {
            "messages": messages,
            "temperature": 0,
            "max_tokens": 600,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "answer", "strict": True, "schema": schema},
            },
            "chat_template_kwargs": {"enable_thinking": False},
        }
    ).encode()
    request = urllib.request.Request(  # nosec B310 - local address
        f"http://127.0.0.1:{port}/v1/chat/completions",
        data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=CALL_TIMEOUT_SECONDS) as response:  # noqa: S310  # nosec B310
            data = json.loads(response.read(2_000_000))
        return str(data["choices"][0]["message"]["content"])
    except (urllib.error.URLError, OSError, ValueError, KeyError, IndexError, TypeError) as exc:
        raise SmartError(f"The smart reader did not answer ({type(exc).__name__}).") from exc
