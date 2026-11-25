"""MemoryStore - 条目化长期记忆存储与 System Prompt 冻结快照管理。

采用 Markdown 文件（MEMORY.md / USER.md）持久化存储，支持 `\\n§\\n` 条目分隔、
utf-8-sig 编码容错、唯原子串定位增删改、字符上限约束与原子落盘。
"""

from __future__ import annotations

import logging
import os
import tempfile
from contextlib import suppress
from pathlib import Path
from threading import Lock
from typing import Literal

from filelock import FileLock, Timeout as FileLockTimeout
from pydantic import BaseModel

from my_agent_core.tools import Tool, ToolResult, tool

logger = logging.getLogger(__name__)

MemoryAction = Literal["add", "replace", "remove", "patch"]

ENTRY_DELIMITER = "\n§\n"
MEMORY_CHAR_LIMIT = 2200
USER_CHAR_LIMIT = 1375
MEMORY_LOCK_TIMEOUT_SECONDS = 3


class MemoryOperation(BaseModel):
    """One entry mutation in a single-file batch."""

    action: MemoryAction
    content: str | None = None
    old_text: str | None = None
    new_content: str | None = None


class MemoryStore:
    """管理 MEMORY.md 与 USER.md 的持久化记忆存储仓储（支持 Frozen Snapshot 与原子落盘）。"""

    def __init__(
        self,
        mem_dir: Path | str,
        memory_char_limit: int = MEMORY_CHAR_LIMIT,
        user_char_limit: int = USER_CHAR_LIMIT,
    ) -> None:
        self.mem_dir = Path(mem_dir)
        self.limits: dict[str, int] = {
            "memory": memory_char_limit,
            "user": user_char_limit,
        }
        self.files: dict[str, str] = {
            "memory": "MEMORY.md",
            "user": "USER.md",
        }
        self._snapshot: dict[str, str] = {"memory": "", "user": ""}
        self._lock = Lock()

    def load_from_disk(self) -> dict[str, str]:
        """Reload readable memories and preserve the previous snapshot for unreadable files."""
        errors: dict[str, str] = {}
        with self._lock:
            for target in self.files:
                try:
                    self._snapshot[target] = ENTRY_DELIMITER.join(self._read_entries(target))
                except (OSError, UnicodeError) as exc:
                    errors[target] = self._read_error(target, exc)
                    logger.warning("%s", errors[target])
        return errors

    def _read_entries(self, target: str) -> list[str]:
        path = self.mem_dir / self.files[target]
        try:
            content = path.read_text(encoding="utf-8-sig").strip()
        except FileNotFoundError:
            if path.is_symlink():
                raise
            return []
        parts = (part.strip() for part in content.split(ENTRY_DELIMITER))
        return list(dict.fromkeys(part for part in parts if part))

    def _read_error(self, target: str, exc: Exception) -> str:
        return f"Cannot read {self.files[target]} ({type(exc).__name__}). Restore read access before editing."

    def _atomic_save(self, target: str, text: str) -> None:
        path = self.mem_dir / self.files[target]
        tmp_fd, tmp_path = tempfile.mkstemp(prefix=f".{path.name}.", dir=self.mem_dir)
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8-sig") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, path)
        except BaseException:
            with suppress(OSError):
                Path(tmp_path).unlink(missing_ok=True)
            raise

    def add(self, target: str, content: str) -> str:
        return str(self.apply(target, "add", content=content).serialize())

    def replace(self, target: str, old_text: str, new_content: str) -> str:
        return str(self.apply(target, "replace", old_text=old_text, content=new_content).serialize())

    def remove(self, target: str, old_text: str) -> str:
        return str(self.apply(target, "remove", old_text=old_text).serialize())

    def patch(self, target: str, old_text: str, new_content: str) -> str:
        return str(self.apply(target, "patch", old_text=old_text, content=new_content).serialize())

    def apply(
        self,
        target: str,
        action: MemoryAction,
        *,
        old_text: str = "",
        content: str | None = None,
    ) -> ToolResult:
        if action not in ("add", "replace", "remove", "patch"):
            return _failure("invalid_input", f"Unknown action '{action}'.")
        operation = MemoryOperation(action=action, old_text=old_text, content=content)
        return self._apply_operations(target, [operation], action)

    def apply_batch(self, target: str, operations: list[MemoryOperation]) -> ToolResult:
        return self._apply_operations(target, operations, "batch")

    def _apply_operations(
        self, target: str, operations: list[MemoryOperation], action: MemoryAction | Literal["batch"]
    ) -> ToolResult:
        if target not in self.files:
            return _failure("invalid_target", f"Invalid target '{target}'. Must be 'memory' or 'user'.")
        if not operations:
            return _failure("invalid_input", "At least one memory operation is required.")

        # All cooperating processes hold the same per-file lock for read, validation and replace.
        lock_path = self.mem_dir / f"{self.files[target]}.lock"
        with self._lock:
            try:
                self.mem_dir.mkdir(parents=True, exist_ok=True)
                with FileLock(str(lock_path), timeout=MEMORY_LOCK_TIMEOUT_SECONDS):
                    return self._apply_locked(target, operations, action)
            except FileLockTimeout:
                return _failure("lock_timeout", f"Memory file {self.files[target]} is busy. Retry the operation.")
            except OSError as exc:
                return _failure("write_failed", f"Cannot lock {self.files[target]} ({type(exc).__name__}).")

    def _apply_locked(
        self, target: str, operations: list[MemoryOperation], action: MemoryAction | Literal["batch"]
    ) -> ToolResult:
        try:
            original = self._read_entries(target)
        except (OSError, UnicodeError) as exc:
            return _failure("read_failed", self._read_error(target, exc))

        candidate = original.copy()
        for index, operation in enumerate(operations, start=1):
            failure = _edit_entry(candidate, target, operation)
            if failure is not None:
                if action == "batch":
                    failure.error = f"Memory operation {index} failed: {failure.error}"
                    failure.meta["operation"] = index
                return failure
        if candidate == original:
            return _failure("no_change", f"Memory in {target} already has the requested content.")
        return self._commit(target, action, candidate, original)

    def _commit(
        self, target: str, action: MemoryAction | Literal["batch"], entries: list[str], original: list[str]
    ) -> ToolResult:
        text = ENTRY_DELIMITER.join(entries)
        size, limit = len(text), self.limits[target]
        if action != "remove" and size > limit:
            message = f"Cannot {action}: total length ({size}) exceeds limit ({limit}) for {target}.\n"
            if action == "add":
                current = "\n---\n".join(original) or "(empty)"
                message += f"Please consolidate or remove older entries first.\nCurrent entries:\n{current}"
            else:
                message += "Please consolidate or shorten entries."
            return _failure("limit_exceeded", message)

        try:
            self._atomic_save(target, text)
        except (OSError, UnicodeError) as exc:
            return _failure("write_failed", f"Cannot write {self.files[target]} ({type(exc).__name__}).")
        summary = {
            "add": "Added to", "replace": "Replaced in", "remove": "Removed from",
            "patch": "Patched in", "batch": "Applied batch to",
        }[action]
        return ToolResult(
            ok=True,
            data=f"{summary} {target} ({size}/{limit} chars used).",
            meta={"status": "committed", "used_chars": size, "limit_chars": limit},
        )

    def format_for_system_prompt(self, target: str) -> str | None:
        """获取冻结的快照字符串（返回 None 代表无内容）。"""
        snapshot = self._snapshot.get(target, "")
        return snapshot if snapshot else None

    def format_all_for_system_prompt(self) -> str | None:
        """将冻结的快照格式化为注入 System Prompt 的 XML 块。"""
        blocks = []
        mem = self.format_for_system_prompt("memory")
        if mem:
            blocks.append(f"## MEMORY.md (Agent Notes)\n{mem}")
        usr = self.format_for_system_prompt("user")
        if usr:
            blocks.append(f"## USER.md (User Profile)\n{usr}")

        if not blocks:
            return None
        joined = "\n\n".join(blocks)
        return (
            "<MEMORY_CONTEXT>\n"
            "The following is your long-term memory across sessions. "
            "Use the `memory` tool to update it when learning new facts or preferences.\n\n"
            f"{joined}\n"
            "</MEMORY_CONTEXT>"
        )


def _failure(code: str, message: str) -> ToolResult:
    status = "no_change" if code == "no_change" else "failed"
    return ToolResult(ok=False, error=message, meta={"status": status, "error_code": code})


def _normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _edit_entry(entries: list[str], target: str, operation: MemoryOperation) -> ToolResult | None:
    action = operation.action
    if action == "add":
        if operation.content is None:
            return _failure("invalid_input", "`content` is required when action is 'add'.")
        content = _normalize_newlines(operation.content).strip()
        failure = _validate_entry(content, "Content")
        if failure is not None:
            return failure
        if content in entries:
            return _failure("no_change", f"Entry already exists in {target}.")
        entries.append(content)
        return None

    if not operation.old_text:
        return _failure("invalid_input", f"`old_text` is required when action is '{action}'.")
    needle = _normalize_newlines(operation.old_text)
    if action != "patch":
        needle = needle.strip()
    if not needle.strip():
        return _failure("invalid_input", "old_text cannot be empty.")

    replacement = operation.new_content if operation.new_content is not None else operation.content
    if action in ("patch", "replace") and (replacement is None or (action == "replace" and replacement == "")):
        return _failure("invalid_input", f"`new_content` is required when action is '{action}'.")
    text = _normalize_newlines(replacement or "")
    if action == "replace":
        text = text.strip()
        failure = _validate_entry(text, "new_content")
        if failure is not None:
            return failure

    matches = [index for index, entry in enumerate(entries) if needle in entry]
    if not matches:
        return _failure("not_found", f"Text '{needle}' not found in {target}.")
    if len(matches) > 1:
        matched = "\n---\n".join(entries[index] for index in matches)
        return _failure(
            "ambiguous_match",
            f"Ambiguous match: found {len(matches)} entries matching '{needle}' in {target}.\n"
            f"Please provide a more specific old_text.\nMatching entries:\n{matched}",
        )

    index = matches[0]
    previous = entries[index]
    if action == "remove":
        entries.pop(index)
        return None

    if action == "patch":
        first_match = previous.index(needle)
        if previous.find(needle, first_match + 1) != -1:
            return _failure("ambiguous_match", "old_text occurs multiple times in the same entry.")
        text = previous.replace(needle, text, 1).strip()
        failure = _validate_entry(text, "new_content")
        if failure is not None:
            return failure
    if text == previous:
        return _failure("no_change", f"Entry already has the requested content in {target}.")
    if text in entries:
        return _failure("no_change", f"The requested content already exists in another entry in {target}.")
    entries[index] = text
    return None


def _validate_entry(text: str, label: str) -> ToolResult | None:
    if not text:
        return _failure("invalid_input", f"{label} cannot be empty.")
    if ENTRY_DELIMITER.strip() in text.splitlines():
        return _failure("invalid_input", "Content cannot contain a standalone memory entry separator (§).")
    return None


def make_memory_tool(store: MemoryStore) -> Tool:
    """Create the tool for updating long-term memory."""

    @tool(
        name="memory",
        description=(
            "Manage long-term memory across sessions. "
            "Target 'memory' for agent knowledge/notes, 'user' for user preferences/profile. "
            "Keep entries concise, high-signal, and consolidate when approaching limits. "
            "Use patch for one exact fragment, replace for the entire entry, or batch "
            "to apply several operations to one target together."
        ),
    )
    def memory(
        target: Literal["memory", "user"],
        action: MemoryAction | Literal["batch"],
        content: str | None = None,
        old_text: str | None = None,
        new_content: str | None = None,
        operations: list[MemoryOperation] | None = None,
    ) -> ToolResult:
        if action == "batch":
            return store.apply_batch(target, [MemoryOperation.model_validate(item) for item in operations or []])
        if action == "replace":
            content = new_content or content
        elif action == "patch" and new_content is not None:
            content = new_content
        return store.apply(target, action, old_text=old_text or "", content=content)

    return memory
