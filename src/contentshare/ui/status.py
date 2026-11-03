"""How each request state is shown: icon + word + colour, never colour alone."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StatusStyle:
    icon: str
    word: str
    tone: str  # key into ``theme.Theme.tones``

    @property
    def text(self) -> str:
        return f"{self.icon} {self.word}"


STATUS: dict[str, StatusStyle] = {
    "NEW": StatusStyle("•", "New", "neutral"),
    "NEEDS_REVIEW": StatusStyle("⚠", "Needs review", "warn"),
    "READY": StatusStyle("✔", "Ready", "ready"),
    "APPROVED": StatusStyle("⏳", "Approved, waiting", "info"),
    "COPYING": StatusStyle("▶", "Copying", "info"),
    "VERIFIED": StatusStyle("✔✔", "Verified", "ready"),
    "REPLIED": StatusStyle("✉", "Replied", "neutral"),
    "SKIPPED": StatusStyle("↷", "Skipped", "neutral"),
    "REJECTED": StatusStyle("✖", "Rejected", "neutral"),
    "FAILED": StatusStyle("✖", "Failed", "error"),
}


def style_for(state: str) -> StatusStyle:
    return STATUS.get(state, StatusStyle("?", state.title(), "neutral"))


def confidence_dots(score: float | None) -> str:
    """●●●●○ 92%"""
    if score is None:
        return "not scored"
    filled = max(0, min(5, round(score / 20)))
    return "●" * filled + "○" * (5 - filled) + f" {score:.0f}%"


def human_eta(seconds: float | None) -> str:
    if seconds is None:
        return "calculating…"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds} s"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} min {secs:02d} s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes:02d} min"
