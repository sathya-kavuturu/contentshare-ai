"""What is worth a pop-up, and what it says. No Qt: the tray only displays the result.

Toasts can appear on a locked screen or in a screen share, so they carry counts and request
numbers only: never an email subject, a sender, a program name or a path.
"""

from __future__ import annotations

from dataclasses import dataclass

from contentshare.app.controller import COPY_OK, Event
from contentshare.gmail.intake import IntakeReport


@dataclass(frozen=True)
class Toast:
    title: str
    text: str
    kind: str  # "requests" | "copy" | "problem" | "signin" | "info"
    urgent: bool = False  # problems stay on screen longer


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def for_check(report: IntakeReport, *, scheduled: bool) -> Toast | None:
    """Result of a mail check. A manual check is answered on screen, so it gets no toast."""
    if report.needs_sign_in:
        return Toast(
            "ContentShare needs you to sign in",
            "Gmail access has ended. Open Settings → Gmail and connect again.",
            "signin",
            urgent=True,
        )
    if not scheduled:
        return None
    if report.requests:
        waiting = report.ready + report.needs_review
        return Toast(
            f"{_plural(report.requests, 'new request')}",
            f"{waiting} waiting for your review. Nothing has been copied or sent.",
            "requests",
        )
    if report.errors and not report.examined:
        return Toast(
            "Could not check for requests",
            "Gmail could not be reached. ContentShare will try again.",
            "problem",
        )
    return None


def for_event(event: Event) -> Toast | None:
    """Copy and reply outcomes reported by the controller."""
    number = f"Request #{event.request_id}" if event.request_id is not None else "A request"
    if event.kind == "copy_finished":
        if event.message == COPY_OK:
            return Toast(
                f"{number}: copy verified", "All files were checked after copying.", "copy"
            )
        if event.message.startswith("Cancelled"):
            return None  # the user did that on purpose
        return Toast(
            f"{number}: copy failed",
            "Open ContentShare to see the reason. No reply was sent.",
            "problem",
            urgent=True,
        )
    if event.kind == "reply_problem":
        return Toast(
            f"{number}: reply not sent",
            "Open ContentShare to see why.",
            "problem",
            urgent=True,
        )
    return None
