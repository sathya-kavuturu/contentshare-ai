from __future__ import annotations

import pytest

from contentshare.config.templates import (
    DEFAULT_REPLY_TEMPLATE,
    REPLY_PLACEHOLDERS,
    SUBFOLDER_PLACEHOLDERS,
    TemplateError,
    render_template,
    validate_template,
)


def test_render_default_template() -> None:
    values = {
        "requester_name": "Anita",
        "program": "Program X",
        "date": "12 Oct 2026",
        "file_count": "37",
        "total_size": "48.2 GB",
        "destination_path": "  - Program X",
        "folders": "  - Program X\n  - Program Y",
        "file_text": "37 files",
    }
    out = render_template(DEFAULT_REPLY_TEMPLATE, values, REPLY_PLACEHOLDERS)
    assert "Hello Anita" in out
    assert "  - Program X\n  - Program Y" in out  # one folder per line
    assert "Total: 37 files, 48.2 GB." in out
    assert "{" not in out


def test_a_saved_setting_with_the_old_wording_moves_to_the_new_one() -> None:
    from contentshare.config.models import AppConfig
    from contentshare.config.templates import LEGACY_REPLY_TEMPLATE

    config = AppConfig(default_reply_template=LEGACY_REPLY_TEMPLATE)
    assert config.default_reply_template == DEFAULT_REPLY_TEMPLATE
    own = AppConfig(default_reply_template="Hi {requester_name}.")  # a changed one stays
    assert own.default_reply_template == "Hi {requester_name}."


def test_values_are_not_reexpanded() -> None:
    out = render_template(
        "Hi {requester_name}", {"requester_name": "{program}"}, REPLY_PLACEHOLDERS
    )
    assert out == "Hi {program}"


@pytest.mark.parametrize(
    "bad",
    ["{unknown}", "{requester_name.__class__}", "{program[0]}", "{", "}", "{{program}}", "{0}"],
)
def test_rejects_unsafe_templates(bad: str) -> None:
    with pytest.raises(TemplateError):
        validate_template(bad, REPLY_PLACEHOLDERS)


def test_subfolder_placeholders() -> None:
    validate_template("{program}/{date}", SUBFOLDER_PLACEHOLDERS)
    with pytest.raises(TemplateError):
        validate_template("{file_count}", SUBFOLDER_PLACEHOLDERS)
