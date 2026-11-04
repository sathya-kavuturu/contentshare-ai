"""Reply templates and destination subfolder patterns.

Only plain ``{name}`` placeholders from a fixed list are allowed. We deliberately do not use
``str.format``: it permits attribute and index access (``{x.__class__}``) which would be a
format-string injection vector for text that originates in an email.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

REPLY_PLACEHOLDERS = frozenset(
    {
        "requester_name", "program", "folders", "file_text", "destination_path", "file_count",
        "total_size", "date",
    }
)  # fmt: skip
SUBFOLDER_PLACEHOLDERS = frozenset({"program", "date", "department"})

# {folders} is one folder per line (also a folder added to the request by hand). The network
# location of the destination is never written in a reply.
DEFAULT_REPLY_TEMPLATE = (
    "Hello {requester_name},\n\n"
    "The content you requested has been shared and checked:\n\n"
    "{folders}\n\n"
    "Total: {file_text}, {total_size}.\n\n"
    "Please let me know if anything is missing.\n"
)
# The wording used before; a saved setting that still has it moves to the new wording.
LEGACY_REPLY_TEMPLATE = (
    "Hello {requester_name},\n\n"
    "The content you requested ({program}, {date}) has been shared: {file_count} files, "
    "{total_size}, at {destination_path}.\n\n"
    "Please let me know if anything is missing.\n"
)


def current_reply_template(template: str) -> str:
    return DEFAULT_REPLY_TEMPLATE if template == LEGACY_REPLY_TEMPLATE else template


_PLACEHOLDER = re.compile(r"\{([^{}]*)\}")


class TemplateError(ValueError):
    """The template uses an unknown placeholder or has stray braces."""


def placeholders_in(template: str) -> set[str]:
    return {m.group(1) for m in _PLACEHOLDER.finditer(template)}


def validate_template(template: str, allowed: frozenset[str]) -> None:
    unknown = placeholders_in(template) - allowed
    if unknown:
        raise TemplateError(
            f"Unknown placeholder(s): {', '.join(sorted(unknown))}. "
            f"Allowed: {', '.join(sorted(allowed))}"
        )
    leftover = _PLACEHOLDER.sub("", template)
    if "{" in leftover or "}" in leftover:
        raise TemplateError("Unbalanced '{' or '}' in template")


def render_template(template: str, values: Mapping[str, str], allowed: frozenset[str]) -> str:
    """Substitute ``{name}`` placeholders. Values are inserted verbatim and never re-expanded."""
    validate_template(template, allowed)
    return _PLACEHOLDER.sub(lambda m: str(values.get(m.group(1), "")), template)
