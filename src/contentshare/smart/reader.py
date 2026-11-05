"""Reading one email with the local model."""

from __future__ import annotations

import json
import logging
from typing import Any

from contentshare.config.models import SmartSettings
from contentshare.smart.prompt import (
    Example,
    build_messages,
    looks_like_request,
    prepare,
    similar_examples,
)
from contentshare.smart.runtime import Runtime
from contentshare.smart.schema import ANSWER_SCHEMA, Reading, parse_answer
from contentshare.storage.repository import Repository

log = logging.getLogger(__name__)

EXAMPLE_POOL = 60


class SmartReader:
    def __init__(self, runtime: Runtime, repo: Repository, settings: SmartSettings) -> None:
        self.runtime, self.repo, self.settings = runtime, repo, settings

    def available(self) -> bool:
        return self.runtime.installer.status(self.settings).ready

    def read(self, subject: str, body: str) -> Reading:
        """What the email asks for. Mail with none of the usual asking words is answered "not a
        request" without starting the model. ``SmartError`` if the model cannot be used."""
        subject, text = prepare(subject, body)
        if not looks_like_request(subject, text):
            return Reading(False, reason="no sign of a request")
        examples = self._examples(f"{subject}\n{text}") if self.settings.learn else []
        reply = self.runtime.chat(build_messages(subject, text, examples), ANSWER_SCHEMA)
        reading = parse_answer(reply, f"{subject}\n{text}")
        if reading is None:
            log.warning("the smart reader gave an answer in the wrong shape")
            return Reading(False, reason="the answer could not be read")
        return reading

    def _examples(self, text: str) -> list[Example]:
        pool = []
        for row in self.repo.recent_examples(EXAMPLE_POOL):
            try:
                folders = tuple(str(f) for f in json.loads(row["folders_json"]))
            except (ValueError, TypeError):
                continue
            pool.append(Example(str(row["text"]), bool(row["is_request"]), folders))
        return similar_examples(text, pool)


def reader_for(runtime: Runtime, repo: Repository, settings: SmartSettings) -> Any:
    return SmartReader(runtime, repo, settings)
