"""Matching request items to indexed content."""

from contentshare.matching.scorer import Candidate, rank_candidates, score_item
from contentshare.matching.service import (
    Triage,
    destination_for,
    process_email_requests,
    program_label,
    triage_item,
)

__all__ = [
    "Candidate",
    "Triage",
    "destination_for",
    "process_email_requests",
    "program_label",
    "rank_candidates",
    "score_item",
    "triage_item",
]
