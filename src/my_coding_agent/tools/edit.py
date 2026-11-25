from __future__ import annotations

import json
from collections import OrderedDict
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal
from weakref import WeakKeyDictionary

from my_agent_core.loop import CancellationToken
from my_agent_core.tools import Tool, ToolResult, tool
from pydantic import BaseModel, ConfigDict, Field

from my_coding_agent.mutation_queue import FileMutationQueue
from my_coding_agent.permissions import PermissionGate
from my_coding_agent.tools.file_changes import (
    FileChange,
    apply_reviewed_change,
    file_change_failure,
    file_diff,
    read_review_snapshot,
)
from my_coding_agent.tools.base import (
    PATH_ALIASES,
    is_binary_file,
    resolve_path,
    wrap_tool_executor,
)

_STAGNATION_FAILURE_LIMIT = 3
_MAX_TRACKED_FAILURES = 128
_MatchCode = Literal["old_text_not_found", "old_text_ambiguous"]
_EditKey = tuple[Path, bytes, bytes]
_FailedEdits = OrderedDict[_EditKey, tuple[_MatchCode, int]]

_MATCH_RECOVERY: dict[_MatchCode, str] = {
    "old_text_not_found": "Read the current file and build oldText from the text you find there.",
    "old_text_ambiguous": "Read the relevant region and include enough unchanged surrounding lines to identify one match.",
}


class _EditMatchError(ValueError):
    def __init__(self, code: _MatchCode, message: str) -> None:
        super().__init__(message)
        self.code = code


def _normalize_edits_arg(raw_edits: Any) -> list[Any] | None:
    """规范化 edits 入参（支持 JSON 字符串与单个字典容错）。"""
    if isinstance(raw_edits, str):
        try:
            parsed = json.loads(raw_edits)
            if isinstance(parsed, (list, dict)):
                raw_edits = parsed
        except Exception:
            pass
    if isinstance(raw_edits, dict):
        return [raw_edits]
    if isinstance(raw_edits, list):
        return raw_edits
    return None


class EditBlock(BaseModel):
    """单个代码编辑块：精确旧文本与替换新文本。"""

    model_config = ConfigDict(populate_by_name=True)

    old_text: str = Field(..., alias="oldText", description="Exact original code block to replace")
    new_text: str = Field(..., alias="newText", description="New code block to insert")


def _edit_bytes(path: str, raw_bytes: bytes, edit_blocks: list[EditBlock]) -> bytes:
    has_bom = raw_bytes.startswith(b"\xef\xbb\xbf")
    clean_bytes = raw_bytes[3:] if has_bom else raw_bytes
    try:
        content = clean_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"Error: File is not valid UTF-8: {path}") from exc

    # 3. 探测换行符并统一归一化为 LF
    is_crlf = "\r\n" in content
    normalized_content = content.replace("\r\n", "\n")

    # 4. 预检所有 edit block（唯一匹配与非重叠校验）
    matches: list[tuple[int, int, str, str, int]] = []
    for i, b in enumerate(edit_blocks):
        old_norm = b.old_text.replace("\r\n", "\n")
        new_norm = b.new_text.replace("\r\n", "\n")
        if not old_norm:
            raise ValueError(f"Error: 'oldText' cannot be empty (edit #{i + 1}).")

        count = normalized_content.count(old_norm)
        if count == 0:
            lines_count = len(normalized_content.splitlines())
            raise _EditMatchError(
                "old_text_not_found",
                f"Error: 'oldText' not found in {path} (edit #{i + 1}). "
                f"The file has {lines_count} lines. Please read the file first to check exact indentation.",
            )
        if count > 1:
            raise _EditMatchError(
                "old_text_ambiguous",
                f"Error: 'oldText' matched {count} times in {path} (edit #{i + 1}). "
                "Please provide more surrounding context lines to ensure a unique match.",
            )

        idx = normalized_content.find(old_norm)
        matches.append((idx, idx + len(old_norm), old_norm, new_norm, i + 1))

    # 5. 校验区间非重叠
    matches.sort(key=lambda m: (m[0], m[1]))
    for j in range(len(matches) - 1):
        if matches[j][1] > matches[j + 1][0]:
            e1 = matches[j][4]
            e2 = matches[j + 1][4]
            raise ValueError(
                f"Error: Overlapping edit regions detected between "
                f"edit #{min(e1, e2)} and edit #{max(e1, e2)}."
            )

    # 6. 逆序替换（Reverse replacement）
    modified_content = normalized_content
    for start, end, _, new_norm, _ in sorted(matches, key=lambda m: m[0], reverse=True):
        modified_content = modified_content[:start] + new_norm + modified_content[end:]

    final_text = modified_content.replace("\n", "\r\n") if is_crlf else modified_content
    out_bytes = final_text.encode("utf-8")
    if has_bom:
        out_bytes = b"\xef\xbb\xbf" + out_bytes

    return out_bytes


def make_edit_tool(
    workspace: Path,
    mutation_queue: FileMutationQueue | None = None,
    *,
    permission_gate: PermissionGate | None = None,
) -> Tool:
    """创建工作区绑定的 edit 工具。

    - workspace: 工作区根目录路径
    - mutation_queue: 单文件并发互斥锁队列，缺省时自动新建
    """
    workspace = workspace.resolve()
    queue = mutation_queue or FileMutationQueue()
    failures_by_run: WeakKeyDictionary[CancellationToken, _FailedEdits] = WeakKeyDictionary()

    def prepare_edit(
        path: str, target: Path, before: bytes, blocks: list[EditBlock], signal: Any | None,
    ) -> bytes | ToolResult:
        # A new Agent run, including a delegated run, has a distinct token.
        failures = None
        key = None
        previous = None
        if isinstance(signal, CancellationToken):
            failures = failures_by_run.setdefault(signal, OrderedDict())
            parameters = json.dumps([(b.old_text, b.new_text) for b in blocks], ensure_ascii=True)
            key = (target, sha256(before).digest(), sha256(parameters.encode("ascii")).digest())
            previous = failures.get(key)
            if previous is not None and previous[1] >= _STAGNATION_FAILURE_LIMIT:
                failures.move_to_end(key)
                return file_change_failure(
                    f"Repeated edit blocked for {path}: these parameters failed three times on this file version. "
                    f"{_MATCH_RECOVERY[previous[0]]}",
                    code="repeated_edit_failure",
                )

        try:
            return _edit_bytes(path, before, blocks)
        except _EditMatchError as exc:
            message = str(exc)
            if failures is not None and key is not None:
                attempts = previous[1] + 1 if previous is not None else 1
                failures[key] = (exc.code, attempts)
                failures.move_to_end(key)
                if len(failures) > _MAX_TRACKED_FAILURES:
                    failures.popitem(last=False)
                if attempts > 1:
                    message += f"\nSame edit failed {attempts} times on this file version. {_MATCH_RECOVERY[exc.code]}"
                    if attempts == _STAGNATION_FAILURE_LIMIT:
                        message += " The next unchanged attempt will be blocked."
            return file_change_failure(message, code=exc.code)

    @tool(
        name="edit",
        description="Surgically edit file using multi-edit atomic blocks and unified diff generation.",
        prompt_snippet="Make precise file edits with exact text replacement, including multiple disjoint edits in one call",
        prompt_guidelines=[
            "Use edit for precise changes (edits[].oldText must match exactly)",
            "When changing multiple separate locations in one file, use one edit call with multiple entries in edits[] instead of multiple edit calls",
            "Each edits[].oldText is matched against the original file, not after earlier edits are applied. Do not emit overlapping or nested edits. Merge nearby changes into one edit.",
            "Keep edits[].oldText as small as possible while still being unique in the file. Do not pad with large unchanged regions.",
        ],
        is_parallel_safe=True,
    )
    async def edit(
        path: str,
        edits: list[dict] | list[EditBlock] | None = None,
        signal: Any | None = None,
    ) -> str | ToolResult:
        try:
            target = resolve_path(workspace, path)
            if not target.exists():
                return file_change_failure(f"Error: File not found: {path}")
            if target.is_dir():
                return file_change_failure(f"Error: Path is a directory: {path}")
            if is_binary_file(target):
                return file_change_failure(f"Error: Cannot edit binary file: {path}")

            # 1. 规范化 edits 入参 (对标 Pi 官方 prepareEditArguments 容错)
            raw_edits = _normalize_edits_arg(edits)

            edit_blocks: list[EditBlock] = []
            if raw_edits is not None and isinstance(raw_edits, list):
                if len(raw_edits) == 0:
                    return file_change_failure("Error: No edits provided.")
                for item in raw_edits:
                    if isinstance(item, EditBlock):
                        edit_blocks.append(item)
                    elif isinstance(item, dict):
                        old_val = item.get("oldText") if "oldText" in item else item.get("old_text")
                        new_val = item.get("newText") if "newText" in item else item.get("new_text")
                        if old_val is None or new_val is None:
                            return file_change_failure(
                                "Error: Each edit block must contain 'oldText' (or 'old_text') "
                                "and 'newText' (or 'new_text').",
                            )
                        edit_blocks.append(EditBlock(old_text=str(old_val), new_text=str(new_val)))
                    else:
                        return file_change_failure(f"Error: Invalid edit block type: {type(item).__name__}")
            else:
                return file_change_failure("Error: 'edits' must be provided as a list of edit blocks.")

            if not edit_blocks:
                return file_change_failure("Error: No edits provided.")

            if permission_gate is not None and permission_gate.mode != "autonomous":
                before = read_review_snapshot(target)
                if before is None:
                    return file_change_failure(f"Error: File not found: {path}")
                content = prepare_edit(path, target, before, edit_blocks, signal)
                if isinstance(content, ToolResult):
                    return content
                change = FileChange.prepare(target, path, before, content)
                error = await apply_reviewed_change(
                    change, action="edit", workspace=workspace, path=path, queue=queue, gate=permission_gate, signal=signal
                )
                if error is not None:
                    return error
                diff = change.diff
            else:
                async with queue.acquire(target):
                    before = target.read_bytes()
                    content = prepare_edit(path, target, before, edit_blocks, signal)
                    if isinstance(content, ToolResult):
                        return content
                    diff = file_diff(path.replace("\\", "/"), before, content)
                    target.write_bytes(content)
            return ToolResult(
                ok=True,
                data=f"Successfully applied {len(edit_blocks)} edit(s) to {path}.\nDiff:\n```diff\n{diff}```",
                meta={"workspace_changed": content != before},
            )
        except OSError as exc:
            return file_change_failure(f"Error: {exc}")
        except ValueError as exc:
            return file_change_failure(str(exc))

    def _edit_normalizer(call_args: dict[str, Any]) -> None:
        if "edits" in call_args:
            norm = _normalize_edits_arg(call_args["edits"])
            if norm is not None:
                call_args["edits"] = norm

    return wrap_tool_executor(
        edit,
        aliases={"path": PATH_ALIASES},
        normalizer=_edit_normalizer,
    )
