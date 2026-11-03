"""Build the Windows program: ``python packaging/build.py``.

Steps: draw the icon, write the Windows version resource, run PyInstaller (one folder, no
console window), list every bundled library with its licence, prove the result works by
running its own ``--selftest``, and write SHA-256 checksums.

Signing: if the environment variable ``CONTENTSHARE_SIGN_PFX`` points at a code-signing
certificate (and ``CONTENTSHARE_SIGN_PASSWORD`` holds its password), the program is signed
with signtool afterwards. Without it the build is unsigned and Windows SmartScreen will warn
users until it has built a reputation. See docs/PACKAGING.md.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import shutil
import struct
import subprocess  # nosec B404 - runs this project's own build tools
import sys
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "build"
DIST = ROOT / "dist"
APP = "ContentShare"
APP_DIR = DIST / APP
EXE = APP_DIR / f"{APP}.exe"

# Qt parts this program never uses. Leaving them out keeps the download smaller; the
# self-test below fails the build if something that *is* needed went missing.
EXCLUDED_MODULES = [
    "tkinter", "pytest", "hypothesis", "mypy", "ruff", "bandit", "pip_audit",
    "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuickWidgets", "PySide6.QtQuickControls2",
    "PySide6.QtPdf", "PySide6.QtPdfWidgets", "PySide6.QtOpenGL", "PySide6.QtOpenGLWidgets",
    "PySide6.QtSql", "PySide6.QtTest", "PySide6.QtDBus", "PySide6.QtDesigner", "PySide6.QtHelp",
    "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets", "PySide6.QtWebChannel",
    "PySide6.QtSvg", "PySide6.QtSvgWidgets", "PySide6.QtXml", "PySide6.QtPrintSupport",
    "PySide6.Qt3DCore", "PySide6.QtCharts", "PySide6.QtDataVisualization",
]  # fmt: skip


def version() -> str:
    text = (ROOT / "src" / "contentshare" / "__init__.py").read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.startswith("__version__"):
            return line.split("=", 1)[1].strip().strip("\"'")
    raise SystemExit("could not read __version__")


def run(cmd: list[str], **kwargs: object) -> None:
    print("+", " ".join(cmd))
    subprocess.run(cmd, check=True, **kwargs)  # type: ignore[call-overload]  # noqa: S603  # nosec B603


# --------------------------------------------------------------------------- icon
def make_icon(path: Path) -> None:
    """Write a multi-size .ico (PNG-compressed entries) from the in-code application icon."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    sys.path.insert(0, str(ROOT / "src"))
    from PySide6.QtCore import QBuffer, QIODevice  # noqa: PLC0415
    from PySide6.QtWidgets import QApplication  # noqa: PLC0415

    from contentshare.ui.icons import app_icon  # noqa: PLC0415

    _app = QApplication.instance() or QApplication([])
    sizes = [16, 24, 32, 48, 64, 128, 256]
    images = []
    for size in sizes:
        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        app_icon().pixmap(size, size).save(buffer, "PNG")
        images.append(bytes(buffer.data()))
    out = io.BytesIO()
    out.write(struct.pack("<HHH", 0, 1, len(images)))
    offset = 6 + 16 * len(images)
    for size, data in zip(sizes, images, strict=True):
        dim = 0 if size >= 256 else size
        out.write(struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(data), offset))
        offset += len(data)
    for data in images:
        out.write(data)
    path.write_bytes(out.getvalue())


# --------------------------------------------------------------------------- version resource
def write_version_file(path: Path, ver: str) -> None:
    parts = [int(p) for p in ver.split(".")[:3]] + [0] * (4 - len(ver.split(".")[:3]))
    tup = tuple(parts[:4])
    path.write_text(
        f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={tup}, prodvers={tup}, mask=0x3f, flags=0x0, OS=0x40004,
                    fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('FileDescription', 'ContentShare'),
      StringStruct('FileVersion', '{ver}'),
      StringStruct('InternalName', 'ContentShare'),
      StringStruct('OriginalFilename', 'ContentShare.exe'),
      StringStruct('ProductName', 'ContentShare'),
      StringStruct('ProductVersion', '{ver}')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
""",
        encoding="utf-8",
    )


# --------------------------------------------------------------------------- licences
def write_licenses(path: Path) -> int:
    """Every installed distribution this program ships with, and the licence it declares."""
    wanted = {
        "pydantic", "pydantic-core", "pyyaml", "xxhash", "blake3", "rapidfuzz",
        "google-api-python-client", "google-auth-oauthlib", "google-auth", "google-api-core",
        "keyring", "pyside6-essentials", "shiboken6", "httplib2", "oauthlib", "requests",
        "requests-oauthlib", "uritemplate", "cachetools", "pyasn1", "pyasn1-modules", "rsa",
        "certifi", "charset-normalizer", "idna", "urllib3", "protobuf", "googleapis-common-protos",
        "proto-plus", "pywin32-ctypes", "jaraco.classes", "jaraco.functools", "jaraco.context",
        "more-itertools", "importlib-metadata", "zipp", "typing-extensions", "typing-inspection",
        "annotated-types", "pyparsing", "pyinstaller", "pyinstaller-hooks-contrib",
    }  # fmt: skip
    rows = []
    for dist in sorted(metadata.distributions(), key=lambda d: d.metadata["Name"].lower()):
        name = dist.metadata["Name"]
        if name.lower() not in wanted:
            continue
        meta = dist.metadata
        declared = meta.get("License-Expression") or meta.get("License") or ""
        if not declared or len(declared) > 80:
            classifiers = [c for c in meta.get_all("Classifier", []) if c.startswith("License ::")]
            declared = "; ".join(c.split("::")[-1].strip() for c in classifiers) or "see package"
        rows.append(f"{name} {dist.version}: {declared}")
    header = (
        "ContentShare bundles the open-source programs and libraries below.\n"
        "Qt for Python (PySide6) is used under the LGPLv3. It is included as separate,\n"
        "replaceable DLL/PYD files in this folder; its source is available from\n"
        "https://www.qt.io/ and https://code.qt.io/.\n"
        "Licence names are as declared by each package.\n\n"
    )
    path.write_text(header + "\n".join(rows) + "\n", encoding="utf-8")
    return len(rows)


# --------------------------------------------------------------------------- steps
def build_exe(ver: str) -> None:
    import googleapiclient  # noqa: PLC0415

    BUILD.mkdir(exist_ok=True)
    icon, vfile = BUILD / "contentshare.ico", BUILD / "version_info.txt"
    make_icon(icon)
    write_version_file(vfile, ver)
    gmail_doc = Path(googleapiclient.__file__).parent / "discovery_cache" / "documents"
    cmd = [
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
        "--onedir", "--windowed", "--name", APP,
        "--icon", str(icon), "--version-file", str(vfile),
        "--distpath", str(DIST), "--workpath", str(BUILD / "work"), "--specpath", str(BUILD),
        "--paths", str(ROOT / "src"),
        # only the one API description this program uses (the library ships 600 of them)
        "--add-data",
        f"{gmail_doc / 'gmail.v1.json'}{os.pathsep}googleapiclient/discovery_cache/documents",
        # the in-app Help text
        "--add-data", f"{ROOT / 'src' / 'contentshare' / 'help'}{os.pathsep}contentshare/help",
        "--collect-submodules", "keyring.backends",
        "--hidden-import", "win32ctypes.pywin32.pywintypes",
        "--hidden-import", "win32ctypes.pywin32.win32cred",
        "--noupx",
    ]  # fmt: skip
    for module in EXCLUDED_MODULES:
        cmd += ["--exclude-module", module]
    cmd.append(str(ROOT / "packaging" / "entry.py"))
    run(cmd)


def prune() -> int:
    """Remove files that were collected but are never used. Returns the bytes saved.

    * the library ships 600 Google API descriptions; only Gmail's is needed (tens of MiB)
    * ``opengl32sw.dll`` is a software OpenGL renderer for Qt Quick/3D, not used by widgets
    """
    internal = APP_DIR / "_internal"
    saved = 0
    docs = internal / "googleapiclient" / "discovery_cache" / "documents"
    for doc in docs.glob("*.json"):
        if doc.name != "gmail.v1.json":
            saved += doc.stat().st_size
            doc.unlink()
    for extra in internal.rglob("opengl32sw.dll"):
        saved += extra.stat().st_size
        extra.unlink()
    return saved


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>{title}</title>
<style>
 body{{font:16px/1.55 "Segoe UI",system-ui,sans-serif;max-width:52rem;margin:2rem auto;
 padding:0 1rem;color:#1b1f24;background:#fff}}
 h1,h2{{line-height:1.25}}
 img{{max-width:100%;border:1px solid #ccd;border-radius:6px}}
 table{{border-collapse:collapse;width:100%}}
 th,td{{border:1px solid #ccd;padding:.4rem .6rem;text-align:left;vertical-align:top}}
 th{{background:#f1f3f6}}
 code{{background:#f1f3f6;padding:.1rem .3rem;border-radius:4px}}
 a{{color:#0b57d0}}
 @media (prefers-color-scheme:dark){{
  body{{background:#14171a;color:#e6e8eb}} th,code{{background:#242a30}}
  th,td,img{{border-color:#3a424a}} a{{color:#8ab4f8}}}}
</style></head><body>{body}</body></html>
"""


def write_guides() -> list[str]:
    """The guides as web pages (every PC can open them; Markdown needs a special viewer)."""
    import markdown  # noqa: PLC0415

    shutil.copytree(ROOT / "docs" / "screenshots", APP_DIR / "screenshots", dirs_exist_ok=True)
    written = []
    pages = (("USER_GUIDE", "ContentShare user guide"), ("GMAIL_SETUP", "Connecting Gmail"))
    for name, title in pages:
        text = (ROOT / "docs" / f"{name}.md").read_text(encoding="utf-8")
        text = text.replace("GMAIL_SETUP.md", "GMAIL_SETUP.html")
        body = markdown.markdown(text, extensions=["tables", "fenced_code"])
        page = PAGE.format(title=title, body=body)
        (APP_DIR / f"{name}.html").write_text(page, encoding="utf-8")
        written.append(f"{name}.html")
    return written


def selftest_exe() -> dict[str, object]:
    report = BUILD / "selftest.json"
    report.unlink(missing_ok=True)
    result = subprocess.run(  # noqa: S603  # nosec B603 - the program we just built
        [str(EXE), "--selftest", str(report)], check=False, timeout=180
    )
    if not report.is_file():
        raise SystemExit("the built program wrote no self-test report (it crashed on start)")
    data: dict[str, object] = json.loads(report.read_text(encoding="utf-8"))
    for check in data["checks"]:  # type: ignore[attr-defined]
        print(f"  {'ok  ' if check['ok'] else 'FAIL'} {check['name']}: {check['detail']}")
    if result.returncode != 0 or not data["ok"] or not data["frozen"]:
        raise SystemExit("SELF-TEST FAILED: do not ship this build")
    return data


def sign() -> bool:
    pfx = os.environ.get("CONTENTSHARE_SIGN_PFX")
    if not pfx:
        print("not signed (set CONTENTSHARE_SIGN_PFX to sign)")
        return False
    signtool = shutil.which("signtool")
    if not signtool:
        raise SystemExit("CONTENTSHARE_SIGN_PFX is set but signtool.exe was not found on PATH")
    password = os.environ.get("CONTENTSHARE_SIGN_PASSWORD", "")
    run(
        [signtool, "sign", "/f", pfx, "/p", password, "/fd", "SHA256",
         "/tr", "http://timestamp.digicert.com", "/td", "SHA256", str(EXE)]
    )  # fmt: skip
    return True


def checksums(files: list[Path]) -> Path:
    lines = []
    for f in files:
        digest = hashlib.sha256(f.read_bytes()).hexdigest()
        lines.append(f"{digest} *{f.name}")
    out = DIST / "SHA256SUMS.txt"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


def folder_size(folder: Path) -> int:
    return sum(f.stat().st_size for f in folder.rglob("*") if f.is_file())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-selftest", action="store_true", help="not for releases")
    args = parser.parse_args()
    ver = version()
    print(f"Building ContentShare {ver}")
    if APP_DIR.exists():
        shutil.rmtree(APP_DIR)
    build_exe(ver)
    print(f"pruned {prune() / 1_048_576:.0f} MiB of unused files")
    count = write_licenses(APP_DIR / "THIRD_PARTY_LICENSES.txt")
    print(f"licence list: {count} packages")
    shutil.copy2(ROOT / "README.md", APP_DIR / "README.md")
    print(f"guides: {', '.join(write_guides())}")
    if not args.skip_selftest:
        print("self-test of the built program:")
        selftest_exe()
    signed = sign()
    sums = checksums([EXE])
    print(f"\nProgram: {EXE}  ({folder_size(APP_DIR) / 1_048_576:.0f} MiB in {APP_DIR.name}/)")
    print(f"Checksums: {sums}  signed={signed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
