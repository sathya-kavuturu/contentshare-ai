from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from contentshare.cli import main as cli_main
from contentshare.config.models import AppConfig, Department
from contentshare.config.store import ConfigError, load_config, save_config

MEDIA: dict[str, Any] = {
    "name": "Media Team",
    "sender_emails": ["Anita@Dept.org", "editor@dept.org"],
    "destination_root": r"\\nas02\media\Incoming",
}
FINANCE: dict[str, Any] = {
    "name": "Finance",
    "sender_domains": ["finance.org"],
    "destination_root": r"Z:\Finance\Incoming",
    "subfolder_pattern": "{program}/{date}",
}


def test_valid_config_normalises() -> None:
    cfg = AppConfig(source_roots=[r"\\storage01\Programs\\"], departments=[MEDIA, FINANCE])
    assert cfg.source_roots == [r"\\storage01\Programs"]
    assert cfg.departments[0].sender_emails == ["anita@dept.org", "editor@dept.org"]
    assert cfg.schedule.daily_check_time == "09:00"
    assert cfg.auto_reply_default is False
    assert cfg.matching.llm_extraction_enabled is False


def test_department_for_sender_prefers_exact_then_domain() -> None:
    cfg = AppConfig(departments=[MEDIA, FINANCE])
    assert cfg.department_for_sender("ANITA@dept.org").name == "Media Team"  # type: ignore[union-attr]
    assert cfg.department_for_sender("cfo@finance.org").name == "Finance"  # type: ignore[union-attr]
    assert cfg.department_for_sender("cfo@sub.finance.org") is None  # no implicit subdomains
    assert cfg.department_for_sender("stranger@evil.com") is None


@pytest.mark.parametrize(
    "patch",
    [
        {"destination_root": "relative\\path"},
        {"destination_root": r"C:\a\..\b"},
        {"destination_root": r"C:\a\file:stream"},
        {"sender_emails": ["not-an-email"]},
        {"sender_emails": [], "sender_domains": []},
        {"sender_domains": ["nodot"]},
        {"subfolder_pattern": "../{program}"},
        {"subfolder_pattern": r"C:\{program}"},
        {"subfolder_pattern": "{nonsense}"},
        {"reply_template": "Hi {requester_name.__class__}"},
    ],
)
def test_invalid_department_rejected(patch: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        Department(**{**MEDIA, **patch})


def test_unknown_fields_rejected() -> None:
    with pytest.raises(ValidationError):
        AppConfig(unexpected=1)  # type: ignore[call-arg]


@pytest.mark.parametrize("value", ["9:00", "24:00", "09:60", "noon"])
def test_bad_schedule_time(value: str) -> None:
    with pytest.raises(ValidationError):
        AppConfig(schedule={"daily_check_time": value})


def test_copy_limits() -> None:
    with pytest.raises(ValidationError):
        AppConfig(copy_options={"concurrency": 9})
    with pytest.raises(ValidationError):
        AppConfig(copy_options={"hash_algorithm": "md5"})


def test_duplicate_department_names_rejected() -> None:
    with pytest.raises(ValidationError, match="unique"):
        AppConfig(departments=[MEDIA, {**FINANCE, "name": "media team"}])


def test_sender_in_two_departments_rejected() -> None:
    other = {**FINANCE, "sender_emails": ["anita@dept.org"], "sender_domains": []}
    with pytest.raises(ValidationError, match="assigned to both"):
        AppConfig(departments=[MEDIA, other])


def test_email_conflicting_with_domain_rule_rejected() -> None:
    odd = {**MEDIA, "sender_emails": ["boss@finance.org"]}
    with pytest.raises(ValidationError, match="domain rule"):
        AppConfig(departments=[odd, FINANCE])


def test_save_and_load_roundtrip_with_backup(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    first = AppConfig(departments=[MEDIA])
    save_config(first, path)
    assert load_config(path) == first

    second = AppConfig(departments=[MEDIA, FINANCE], auto_reply_default=True)
    save_config(second, path)
    assert load_config(path) == second
    assert load_config(path.with_suffix(".yaml.bak")) == first
    assert not list(tmp_path.glob("*.tmp"))


def test_load_missing_returns_defaults(tmp_path: Path) -> None:
    assert load_config(tmp_path / "nope.yaml") == AppConfig()


@pytest.mark.parametrize(
    "content",
    ["- a list\n- not a map\n", "key: [unclosed", "departments:\n  - name: X\n"],
)
def test_load_invalid_gives_helpful_error(tmp_path: Path, content: str) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ConfigError, match=r"config\.yaml"):
        load_config(path)


def test_yaml_cannot_execute_python(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("x: !!python/object/apply:os.system ['echo hi']\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path)


def test_cli_init_and_check(isolated_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli_main(["init"]) == 0
    assert (isolated_home / "config.yaml").exists()
    assert (isolated_home / "contentshare.db").exists()
    assert cli_main(["check-config"]) == 0
    assert "OK" in capsys.readouterr().out

    bad = isolated_home / "bad.yaml"
    bad.write_text("schedule:\n  daily_check_time: noon\n", encoding="utf-8")
    assert cli_main(["check-config", str(bad)]) == 1
    assert "INVALID" in capsys.readouterr().err
