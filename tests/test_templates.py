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
        "destination_path": r"\\nas02\media\Incoming",
    }
    out = render_template(DEFAULT_REPLY_TEMPLATE, values, REPLY_PLACEHOLDERS)
    assert "Hello Anita" in out
    assert "37 files" in out
    assert "{" not in out


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
