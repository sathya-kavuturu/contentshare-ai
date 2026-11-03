# Building and releasing ContentShare

## What you get

| Step | Command | Result |
|---|---|---|
| 1. Program | `python packaging/build.py` | `dist/ContentShare/ContentShare.exe` plus its libraries (about 115 MiB), licence list, guides |
| 2. Installer | `iscc /DAppVersion=0.1.0 installer\contentshare.iss` | `dist/ContentShare-Setup-0.1.0.exe` |

One **folder** build, not a single-file `.exe`. A single file unpacks itself into a temporary
folder on every start (slower, and antivirus tools distrust it) and the tray app starts at every
sign-in. The installer is what gives users "one file to run".

Install the build tools once: `pip install -e ".[build]"` (PyInstaller and Markdown). Inno Setup
6 is a separate Windows program: <https://jrsoftware.org/isinfo.php>.

## What the build checks

`packaging/build.py` runs the program it just built with `--selftest`, which uses only throwaway
files and exercises every part that a packaging mistake could silently remove: hashing, fuzzy
matching, SQLite FTS5, the settings model, the Windows Credential Manager backend, the bundled
Gmail API description (so start-up needs no network), a real verified copy, the Windows start-up
entry, and drawing the Qt user interface. If anything fails the build stops with
`SELF-TEST FAILED: do not ship this build`.

The same check is available to users: `ContentShare.exe --selftest report.json`.

Things the build does for you, and why:

* Keeps only the Gmail API description (the library ships 600; the rest were 100+ MiB) and drops
  Qt's software OpenGL renderer, which widgets do not use. The self-test proves nothing needed was lost.
* Writes `THIRD_PARTY_LICENSES.txt` from the installed package metadata.
  PySide6 (Qt) is LGPLv3: it ships as separate DLL/PYD files that can be replaced, which that
  licence requires. Do not switch to a one-file build without revisiting this.
* Writes `SHA256SUMS.txt` (for `ContentShare.exe`).

## Signing

An unsigned program works but Windows SmartScreen shows "Windows protected your PC" until the
file builds a reputation, and some antivirus products are suspicious of unsigned PyInstaller
output. To sign, get a code-signing certificate (an organisation certificate, ideally from your
IT department) and build with:

```powershell
$env:CONTENTSHARE_SIGN_PFX = "C:\secure\codesign.pfx"
$env:CONTENTSHARE_SIGN_PASSWORD = "..."     # do not put this in a file in the project
python packaging/build.py
```

`signtool.exe` (Windows SDK) must be on PATH. The build signs `ContentShare.exe`; to sign the
installer too, add a `SignTool` entry to `installer/contentshare.iss` (see Inno Setup's help).
**Not tried here: no certificate was available.**

## The installer

* Per-user install into `%LOCALAPPDATA%\Programs\ContentShare`: no administrator rights, no
  UAC prompt. A Start-menu shortcut (and optional desktop shortcut) carry the application id
  `ContentShare.App`, the same id the program sets for itself, so pop-ups and the taskbar belong
  to one application.
* Before installing, upgrading or uninstalling it asks a running copy to quit
  (`ContentShare.exe --quit`; the app asks the user first if copies are running).
* Uninstall runs `ContentShare.exe --remove-startup` so Windows is not left pointing at a missing
  program. It keeps `%APPDATA%\ContentShare` (settings, history, log) and says so, and reminds
  the user that the Gmail sign-in lives in Windows Credential Manager (Settings → Gmail →
  Disconnect before uninstalling revokes it).
* Upgrades keep the same `AppId`, so installing a newer setup over an older one just replaces files.
  Never change the `AppId` GUID.
* **Tested** (Inno Setup 6.7.3, Windows 11, silent install into a temporary folder): installs; the
  installed program passes `--selftest`; installing over a running copy closes it and replaces
  the files; uninstall removes the files, Start-menu shortcuts, uninstall entry and the
  start-with-Windows entry, and keeps `%APPDATA%\ContentShare`. **Not tried:** the visible wizard
  pages (only silent mode), a clean machine without Python, the desktop-shortcut option.

Compile with Inno Setup 6 (`winget install JRSoftware.InnoSetup --scope user`). In Git Bash set
`MSYS_NO_PATHCONV=1`, or the `/DAppVersion=` switch is mistaken for a path.

## Releasing

1. Bump `__version__` in `src/contentshare/__init__.py` and add a line to the change log.
2. `python -m pytest`, `ruff check`, `mypy`, `bandit`, `pip-audit`.
3. `python packaging/build.py` (must end with all self-test lines `ok`).
4. `iscc /DAppVersion=<version> installer\contentshare.iss`.
5. On a clean Windows account (or VM): install, start, run Settings → Gmail → Connect, tick
   *Start with Windows*, sign out and in, check the tray icon, uninstall.
6. Publish the installer with its checksum (`certutil -hashfile dist\ContentShare-Setup-<version>.exe SHA256`).
   `dist/SHA256SUMS.txt` covers `ContentShare.exe` only.
