from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # UI tests run without a screen

import pytest

from contentshare.storage.db import Database
from contentshare.storage.repository import Repository


@pytest.fixture
def db(tmp_path: Path) -> Database:
    return Database(tmp_path / "test.db")


@pytest.fixture
def repo(db: Database) -> Repository:
    return Repository(db)


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "appdata"
    monkeypatch.setenv("CONTENTSHARE_HOME", str(home))
    return home
