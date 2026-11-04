"""Start ContentShare when the user signs in to Windows.

Two methods, tried in this order:

1. The per-user ``Run`` registry key. It needs no administrator rights and is what Windows
   shows under Settings → Apps → Startup.
2. A Task Scheduler "at log on" task for the current user, if the registry key is blocked
   (some managed PCs lock it down).

Both start the app with ``--background`` so it opens in the tray instead of in front of the user.
Both are per-user: nothing here needs, or asks for, administrator rights.
"""

from __future__ import annotations

import logging
import os
import subprocess  # nosec B404 - only to run the Windows schtasks.exe, see SchTasks
import sys
import winreg
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

log = logging.getLogger(__name__)

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "ContentShare"
TASK_NAME = "ContentShare (at log on)"
BACKGROUND_FLAG = "--background"


def launch_command(
    *, frozen: bool | None = None, executable: str | None = None, root: Path | None = None
) -> str:
    """The command line Windows should run at sign-in (paths quoted, no shell involved)."""
    frozen = getattr(sys, "frozen", False) if frozen is None else frozen
    exe = executable or sys.executable
    if frozen:  # the installed program is itself the executable
        return f'"{exe}" {BACKGROUND_FLAG}'
    # Running from source: use pythonw.exe so no console window appears at log on.
    path = Path(exe)
    windowless = path.with_name("pythonw.exe")
    python = windowless if windowless.is_file() else path
    return f'"{python}" -m contentshare {BACKGROUND_FLAG}'


class RegistryRun(Protocol):
    def get(self) -> str | None: ...
    def set(self, command: str) -> None: ...
    def remove(self) -> None: ...


class WindowsRegistryRun:
    """A value in a per-user registry key (by default the real ``Run`` key)."""

    def __init__(self, name: str = VALUE_NAME, key: str = RUN_KEY) -> None:
        self.name, self.key = name, key

    def get(self) -> str | None:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.key) as key:
                value, _kind = winreg.QueryValueEx(key, self.name)
        except FileNotFoundError:
            return None
        return str(value)

    def set(self, command: str) -> None:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, self.key, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, self.name, 0, winreg.REG_SZ, command)

    def remove(self) -> None:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.key, 0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, self.name)
        except FileNotFoundError:
            return


class TaskRunner(Protocol):
    def exists(self) -> bool: ...
    def create(self, command: str) -> None: ...
    def delete(self) -> None: ...


class SchTasks:
    """Task Scheduler through ``schtasks.exe`` (fixed arguments; the command is one argument)."""

    def __init__(self, name: str = TASK_NAME) -> None:
        self.name = name

    @staticmethod
    def _exe() -> str:
        """The full path, so a program of the same name earlier on PATH is never run."""
        return str(Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32" / "schtasks.exe")

    @classmethod
    def _run(cls, args: list[str]) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(  # noqa: S603  # nosec B603 - fixed program, list arguments, no shell
                [cls._exe(), *args],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
                # The program has no console of its own, so Windows would open one for
                # schtasks.exe and close it again: a window flashing on screen.
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.SubprocessError as exc:  # includes timeouts
            raise OSError(f"schtasks did not run ({type(exc).__name__})") from exc

    def exists(self) -> bool:
        return self._run(["/Query", "/TN", self.name]).returncode == 0

    def create(self, command: str) -> None:
        result = self._run(
            ["/Create", "/TN", self.name, "/SC", "ONLOGON", "/TR", command, "/RL", "LIMITED", "/F"]
        )
        if result.returncode != 0:
            raise OSError(result.stderr.strip() or "schtasks could not create the task")

    def delete(self) -> None:
        if self.exists():
            self._run(["/Delete", "/TN", self.name, "/F"])


@dataclass(frozen=True)
class StartupStatus:
    enabled: bool
    method: str | None  # "registry" | "task" | None
    current: bool  # points at this installation (False after the app was moved)


class StartAtLogin:
    def __init__(
        self,
        registry: RegistryRun | None = None,
        tasks: TaskRunner | None = None,
        command: str | None = None,
    ) -> None:
        self.registry = registry or WindowsRegistryRun()
        self.tasks = tasks or SchTasks()
        self._command = command

    @property
    def command(self) -> str:
        return self._command or launch_command()

    def status(self) -> StartupStatus:
        value = self.registry.get()
        if value is not None:
            return StartupStatus(True, "registry", value == self.command)
        if self.tasks.exists():
            return StartupStatus(True, "task", True)
        return StartupStatus(False, None, True)

    def enable(self) -> str:
        """Turn it on; returns the method used. Raises ``OSError`` if both methods fail."""
        command = self.command
        try:
            self.registry.set(command)
        except OSError as exc:
            log.warning("registry Run key not writable (%s); trying Task Scheduler", exc)
            self.tasks.create(command)
            return "task"
        self.tasks.delete()  # never leave both: the app would start twice
        return "registry"

    def disable(self) -> None:
        self.registry.remove()
        self.tasks.delete()

    def apply(self, wanted: bool) -> StartupStatus:
        """Make Windows match the setting. Refreshes the command if the app was moved."""
        now = self.status()
        if wanted and (not now.enabled or not now.current):
            self.enable()
        elif not wanted and now.enabled:
            self.disable()
        return self.status()
