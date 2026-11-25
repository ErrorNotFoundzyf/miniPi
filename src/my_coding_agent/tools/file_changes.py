"""Prepare text diffs and commit exactly the bytes approved by the user."""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from hashlib import sha256
from io import StringIO
from pathlib import Path
from typing import Any

from my_agent_core.tools import ToolResult
from my_coding_agent.mutation_queue import FileMutationQueue
from my_coding_agent.permissions import PermissionGate, PermissionRequest
from my_coding_agent.tools.base import resolve_path

MAX_REVIEW_FILE_BYTES = 1024 * 1024
MAX_REVIEW_DIFF_BYTES = 48 * 1024


def _digest(content: bytes | None) -> bytes | None:
    return None if content is None else sha256(content).digest()


def read_review_snapshot(target: Path) -> bytes | None:
    try:
        with target.open("rb") as stream:
            content = stream.read(MAX_REVIEW_FILE_BYTES + 1)
    except FileNotFoundError:
        return None
    if len(content) > MAX_REVIEW_FILE_BYTES:
        raise ValueError("File is too large for an interactive review.")
    return content


def file_diff(path: str, before: bytes | None, after: bytes) -> str:
    old = StringIO((before or b"").decode("utf-8")).readlines()
    new = StringIO(after.decode("utf-8")).readlines()
    lines = difflib.unified_diff(old, new, fromfile="/dev/null" if before is None else f"a/{path}", tofile=f"b/{path}")
    return "".join(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n" for line in lines)


@dataclass(frozen=True)
class FileChange:
    target: Path
    before_digest: bytes | None
    content: bytes
    diff: str

    @classmethod
    def prepare(cls, target: Path, path: str, before: bytes | None, after: bytes) -> FileChange:
        if max(len(before or b""), len(after)) > MAX_REVIEW_FILE_BYTES:
            raise ValueError("File is too large for an interactive review.")
        if b"\0" in (before or b"") or b"\0" in after:
            raise ValueError("Interactive review requires a UTF-8 text file.")
        diff = file_diff(path.replace("\\", "/"), before, after)
        if len(diff.encode("utf-8")) > MAX_REVIEW_DIFF_BYTES:
            raise ValueError("Diff is too large to review. Split the change into smaller edits.")
        return cls(target, _digest(before), after, diff)


def _cancelled(signal: Any | None) -> bool:
    check = getattr(signal, "is_cancelled", None)
    return bool(check() if callable(check) else getattr(signal, "cancelled", False))


def file_change_failure(message: str, *, code: str | None = None) -> ToolResult:
    # Keep data available to callers that read the original file tools' text.
    return ToolResult(ok=False, data=message, error=message, meta={"code": code} if code else {})


async def apply_reviewed_change(
    change: FileChange,
    *,
    action: str,
    workspace: Path,
    path: str,
    queue: FileMutationQueue,
    gate: PermissionGate,
    signal: Any | None,
) -> ToolResult | None:
    if _cancelled(signal):
        return file_change_failure("File change cancelled before review.", code="review_cancelled")
    try:
        approved = await gate.confirm_file_change(PermissionRequest(
            action=action,
            target=str(change.target),
            details={"create": change.before_digest is None},
            preview=change.diff,
        ))
    except TimeoutError:
        return file_change_failure("File approval timed out. The proposed change was not applied.", code="review_timeout")
    except Exception:
        return file_change_failure(
            "File approval is unavailable. The proposed change was not applied.", code="review_unavailable"
        )
    if approved is not True:
        return file_change_failure("User declined the proposed file change.", code="review_denied")

    async with queue.acquire(change.target):
        if _cancelled(signal):
            return file_change_failure("File change cancelled before writing.", code="review_cancelled")
        stale = file_change_failure(
            "File changed after the preview. Read it again and request a new review.", code="preview_stale"
        )
        try:
            if resolve_path(workspace, path) != change.target:
                return stale
            if _digest(read_review_snapshot(change.target)) != change.before_digest:
                return stale
        except (OSError, ValueError):
            return stale
        if action == "write":
            change.target.parent.mkdir(parents=True, exist_ok=True)
        change.target.write_bytes(change.content)
    return None
