"""Runs one approved request: checks paths, copies with verification, journals every file, and
moves the request to VERIFIED or FAILED. A copy failure never leads to a "shared" reply."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from contentshare.config.models import (
    OPEN_DEPARTMENT,
    AppConfig,
    Department,
    inside_system_folder,
)
from contentshare.copy_engine import (
    MIB,
    Control,
    CopyOptions,
    FileOutcome,
    FileResult,
    PreflightError,
    Progress,
    TreeResult,
    TrustedFile,
    build_plan,
    cleanup_partials,
    copy_tree,
)
from contentshare.jobs.states import RequestState
from contentshare.matching.backup import resolve_backup_destination, resolve_backup_source
from contentshare.security.paths import (
    PathSafetyError,
    resolve_anywhere,
    resolve_within,
    resolve_within_any,
)
from contentshare.storage.repository import Repository

log = logging.getLogger(__name__)
_REASON_MAX = 500
_OK = {FileOutcome.COPIED, FileOutcome.SKIPPED_IDENTICAL, FileOutcome.SKIPPED_TRUSTED}


@dataclass
class JobOutcome:
    request_id: int
    state: RequestState
    error: str | None = None
    tree: TreeResult | None = None
    started: bool = True  # False if cancelled before any work began


def options_from_config(
    config: AppConfig,
    *,
    overwrite: frozenset[str] = frozenset(),
    trusted: dict[str, TrustedFile] | None = None,
) -> CopyOptions:
    c = config.copy_options
    return CopyOptions(
        algorithm=c.hash_algorithm,
        write_checksum_file=c.write_checksum_file,
        chunk_bytes=c.chunk_mb * MIB,
        parallel_files=c.files_at_once,
        max_attempts=c.max_attempts,
        free_space_margin_pct=c.free_space_margin_pct,
        overwrite=overwrite,
        trusted=trusted or {},
    )


def resolve_source(config: AppConfig, dept: Department, text: str) -> Path:
    """The content to copy: inside the department's folders, or, for a department that is
    allowed to take content from anywhere, any folder or file the user picked (never inside
    Windows or Program Files, and never a whole drive)."""
    try:
        return resolve_within_any(config.roots_for(dept), text)
    except PathSafetyError:
        if not dept.may_pick_anywhere:
            raise
    path = resolve_anywhere(text)
    protected = inside_system_folder(str(path))
    if protected:
        raise PathSafetyError(f"Content inside {protected} cannot be copied")
    return path


def resolve_destination(dept: Department, text: str) -> Path:
    """Where the copy goes: inside the department's folder, or, with no department set up, any
    folder the person picked (never inside Windows or Program Files)."""
    if dept.name != OPEN_DEPARTMENT:
        return resolve_within(dept.destination_root, text)
    path = resolve_anywhere(text)
    protected = inside_system_folder(str(path))
    if protected:
        raise PathSafetyError(f"Copies must not go into {protected}")
    return path


def _replace_existing(row: dict[str, object]) -> bool:
    """The person chose, for a backup of something already in the archive, to bring in only the
    files that are new or changed."""
    try:
        detail = json.loads(str(row.get("query_json") or "{}"))
        return bool(detail.get("backup", {}).get("overwrite"))
    except (ValueError, AttributeError):
        return False


def paths_overlap(a: Path, b: Path) -> bool:
    a_s, b_s = str(a).casefold().rstrip("\\"), str(b).casefold().rstrip("\\")
    return a_s == b_s or a_s.startswith(b_s + "\\") or b_s.startswith(a_s + "\\")


class CopyJobRunner:
    def __init__(
        self,
        repo: Repository,
        config: AppConfig,
        *,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.repo, self.config, self.sleep = repo, config, sleep

    def run(
        self,
        request_id: int,
        control: Control | None = None,
        *,
        progress: Callable[[Progress], None] | None = None,
        overwrite: frozenset[str] = frozenset(),
    ) -> JobOutcome:
        control = control or Control()
        if control.cancelled:
            return JobOutcome(
                request_id, RequestState.APPROVED, "Cancelled before start", started=False
            )

        # Raises InvalidTransitionError unless the user approved this request.
        self.repo.transition(request_id, RequestState.COPYING, actor="system")
        job_id, _attempt = self.repo.create_job(request_id)
        outcome = self._execute(request_id, job_id, control, progress, overwrite)
        self.repo.finish_job(job_id, outcome.state.value, outcome.error)
        return outcome

    # ------------------------------------------------------------------
    def _execute(
        self,
        request_id: int,
        job_id: int,
        control: Control,
        progress: Callable[[Progress], None] | None,
        overwrite: frozenset[str],
    ) -> JobOutcome:
        try:
            return self._copy(request_id, job_id, control, progress, overwrite)
        except PreflightError as exc:
            return self._fail(request_id, "; ".join(exc.problems[:5]))
        except (PathSafetyError, OSError, ValueError) as exc:
            return self._fail(request_id, f"{type(exc).__name__}: {exc}")
        except Exception as exc:
            log.exception("unexpected error copying request %s", request_id)
            return self._fail(request_id, f"Unexpected error ({type(exc).__name__}); see the log")

    def _copy(
        self,
        request_id: int,
        job_id: int,
        control: Control,
        progress: Callable[[Progress], None] | None,
        overwrite: frozenset[str],
    ) -> JobOutcome:
        row = self.repo.get_request(request_id)
        if row is None:
            raise KeyError(f"No request {request_id}")
        if not row["source_path"] or not row["destination_path"]:
            raise ValueError("Request has no source or destination path")

        if row.get("kind") == "backup":  # a department's own content into the archive
            ctx = self.config.backup_context(row["department"])
            src = resolve_backup_source(ctx, row["source_path"])
            dst = resolve_backup_destination(ctx, row["destination_path"])
        else:
            dept = self._department(row["department"])
            src = resolve_source(self.config, dept, row["source_path"])
            dst = resolve_destination(dept, row["destination_path"])
        if paths_overlap(src, dst):
            raise PathSafetyError("Source and destination overlap")

        algorithm = self.config.copy_options.hash_algorithm
        trusted = {
            rel: TrustedFile(size, mtime, digest)
            for rel, (size, mtime, digest) in self.repo.verified_files(
                request_id, algorithm
            ).items()
        }
        plan = build_plan(src, dst)
        if _replace_existing(
            row
        ):  # "copy only new or changed files": a file that differs is replaced
            overwrite = overwrite | frozenset(
                e.rel for e in plan.files if (dst / e.rel).is_file()
            )  # identical files are still skipped by the copy: only differing ones are replaced
        options = options_from_config(self.config, overwrite=overwrite, trusted=trusted)
        cleanup_partials(dst)  # leftovers from a crash
        self.repo.log("system", "copy_started", request_id, src=str(src), dst=str(dst),
                      algorithm=algorithm, job_id=job_id)  # fmt: skip

        def journal(result: FileResult) -> None:
            self.repo.record_file(
                job_id,
                rel_path=result.rel,
                size=result.size,
                mtime_ns=result.mtime_ns,
                algorithm=algorithm,
                state="VERIFIED" if result.outcome in _OK else result.outcome.value,
                hash_hex=result.hash_hex,
                attempts=result.attempts,
                error=result.error,
            )

        tree = copy_tree(plan, options, control, progress=progress, sleep=self.sleep,
                         on_file_done=journal)  # fmt: skip
        return self._conclude(request_id, job_id, tree)

    def _conclude(self, request_id: int, job_id: int, tree: TreeResult) -> JobOutcome:
        if tree.cancelled:
            return self._fail(request_id, "Cancelled", tree)
        if not tree.ok:
            problems = [f"{f.rel}: {f.error}" for f in tree.failures]
            more = f" (+{len(problems) - 3} more)" if len(problems) > 3 else ""
            return self._fail(
                request_id,
                f"{len(problems)} file(s) not delivered: " + "; ".join(problems[:3]) + more,
                tree,
            )
        self.repo.transition(
            request_id,
            RequestState.VERIFIED,
            actor="system",
            file_count=len(tree.files),
            total_bytes=sum(f.size for f in tree.files),
            failure_reason=None,
        )
        self.repo.log("system", "copy_verified", request_id, job_id=job_id,
                      files=len(tree.files), seconds=round(tree.duration_s, 1),
                      manifest=str(tree.manifest_path))  # fmt: skip
        return JobOutcome(request_id, RequestState.VERIFIED, tree=tree)

    def _fail(self, request_id: int, reason: str, tree: TreeResult | None = None) -> JobOutcome:
        reason = reason[:_REASON_MAX]
        self.repo.transition(
            request_id, RequestState.FAILED, actor="system", reason=reason, failure_reason=reason
        )
        return JobOutcome(request_id, RequestState.FAILED, reason, tree)

    def _department(self, name: str | None) -> Department:
        dept = self.config.department_for(name)
        if dept is None:
            raise ValueError(f"Department {name!r} is not in the settings")
        return dept
