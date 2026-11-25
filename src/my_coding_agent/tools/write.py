from __future__ import annotations

from pathlib import Path
from typing import Any

from my_agent_core.tools import Tool, ToolResult, tool

from my_coding_agent.mutation_queue import FileMutationQueue
from my_coding_agent.permissions import PermissionGate
from my_coding_agent.tools.base import (
    PATH_ALIASES,
    resolve_path,
    wrap_tool_executor,
)
from my_coding_agent.tools.file_changes import FileChange, apply_reviewed_change, file_change_failure, read_review_snapshot


def make_write_tool(
    workspace: Path,
    mutation_queue: FileMutationQueue | None = None,
    *,
    permission_gate: PermissionGate | None = None,
) -> Tool:
    """创建工作区绑定的 write 工具。

    - workspace: 工作区根目录路径
    - mutation_queue: 单文件并发互斥锁队列，缺省时自动新建
    """
    workspace = Path(workspace).resolve()
    queue = mutation_queue or FileMutationQueue()

    @tool(
        name="write",
        description="Write complete content to a file, automatically creating parent directories.",
        prompt_snippet="Create or overwrite files",
        prompt_guidelines=["Use write only for new files or complete rewrites."],
        is_parallel_safe=True,
    )
    async def write(path: str, content: str, signal: Any | None = None) -> str | ToolResult:
        try:
            target = resolve_path(workspace, path)
            if target.is_dir():
                return file_change_failure(f"Error: Path is a directory: {path}")

            content_bytes = content.encode("utf-8")
            if permission_gate is not None and permission_gate.mode != "autonomous":
                before = read_review_snapshot(target)
                change = FileChange.prepare(target, path, before, content_bytes)
                error = await apply_reviewed_change(
                    change, action="write", workspace=workspace, path=path, queue=queue, gate=permission_gate, signal=signal
                )
                if error is not None:
                    return error
                changed = before != content_bytes
            else:
                async with queue.acquire(target):
                    try:
                        changed = target.stat().st_size != len(content_bytes) or target.read_bytes() != content_bytes
                    except OSError:
                        changed = True
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(content_bytes)
            return ToolResult(
                ok=True,
                data=f"Successfully wrote {len(content_bytes)} bytes ({len(content.splitlines())} lines) to {path}",
                meta={"workspace_changed": changed},
            )
        except (OSError, ValueError) as exc:
            return file_change_failure(f"Error: {exc}")

    return wrap_tool_executor(
        write,
        aliases={"path": PATH_ALIASES, "content": ("contents", "text")},
    )
