"""Builders for fake storage servers used by index and matching tests."""

from __future__ import annotations

from pathlib import Path

STORAGE_FILES: dict[str, int] = {
    "2026/2026-10-12_SunriseTalk_EN/video.mxf": 2000,
    "2026/2026-10-12_SunriseTalk_EN/audio.wav": 500,
    "2026/2026-10-12_SunriseTalk_EN/notes/readme.txt": 10,
    "2026/2026-10-12_SunriseTalk_HI/video.mxf": 1500,
    "2026/2026-10-19_SunriseTalk_EN/video.mxf": 1800,
    "2026/YouthRetreat_2026-09-03_Day1/clip.mp4": 700,
    "2026/YouthRetreat_2026-09-04_Day2/clip.mp4": 800,
    "2025/AnnualDay_2025-12-25/photos/IMG_001.jpg": 100,
    "2025/AnnualDay_2025-12-25/photos/IMG_002.jpg": 120,
    "2025/AnnualDay_2025-12-25/video/annual.mp4": 900,
    "Archive/Satsang Morning (Café)/2026-10-12 Morning Satsang audio.mp3": 300,
    "$RECYCLE.BIN/junk.txt": 5,
    "Thumbs.db": 3,
}


def make_storage(root: Path, files: dict[str, int] | None = None) -> Path:
    """Create the fake server under ``root`` and return its path."""
    root.mkdir(parents=True, exist_ok=True)
    for rel, size in (files or STORAGE_FILES).items():
        path = root.joinpath(*rel.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * size)
    return root


# ---------------------------------------------------------------------------------- mail world
from dataclasses import dataclass  # noqa: E402
from datetime import date  # noqa: E402
from typing import Any  # noqa: E402

from contentshare.config.models import AppConfig  # noqa: E402
from contentshare.gmail.gateway import GmailGateway  # noqa: E402
from contentshare.gmail.intake import IntakeService  # noqa: E402
from contentshare.gmail.replies import ReplyService  # noqa: E402
from contentshare.index import ContentIndex  # noqa: E402
from contentshare.storage.db import Database  # noqa: E402
from contentshare.storage.repository import Repository  # noqa: E402
from fake_gmail import FakeGmail  # noqa: E402

TODAY = date(2026, 11, 3)


@dataclass
class MailWorld:
    storage: Path
    media: Path
    config: AppConfig
    index: ContentIndex
    repo: Repository
    gmail: FakeGmail
    gateway: GmailGateway
    intake: IntakeService
    replies: ReplyService
    sleeps: list[float]

    def rebuild(self, **gmail_settings: Any) -> None:
        """Apply new Gmail settings and recreate the services that read them."""
        self.config = self.config.model_copy(
            update={"gmail": self.config.gmail.model_copy(update=gmail_settings)}
        )
        self.intake = IntakeService(
            self.gateway, self.repo, self.index, self.config, today=lambda: TODAY
        )
        self.replies = ReplyService(self.repo, self.gateway, self.config, today=lambda: TODAY)


def build_mail_world(tmp_path: Path, db: Database, *, page_size: int = 100) -> MailWorld:
    storage = make_storage(tmp_path / "Programs")
    media = tmp_path / "media_incoming"
    media.mkdir()
    config = AppConfig(
        source_roots=[str(storage)],
        departments=[
            {
                "name": "Media Team",
                "sender_emails": ["anita@dept.org", "editor@dept.org"],
                "destination_root": str(media),
            },
            {
                "name": "Finance",
                "sender_domains": ["finance.org"],
                "destination_root": str(tmp_path / "finance"),
                "subfolder_pattern": "{program}/{date}",
                "reply_template": (
                    "Dear {requester_name}, {program} ({file_count} files) "
                    "is at {destination_path}."
                ),
            },
        ],
    )
    repo = Repository(db)
    index = ContentIndex(db, config.index)
    index.scan_root(str(storage))
    gmail = FakeGmail(page_size=page_size)
    sleeps: list[float] = []
    gateway = GmailGateway(gmail, sleep=sleeps.append)
    return MailWorld(
        storage, media, config, index, repo, gmail, gateway,
        IntakeService(gateway, repo, index, config, today=lambda: TODAY),
        ReplyService(repo, gateway, config, today=lambda: TODAY),
        sleeps,
    )  # fmt: skip
