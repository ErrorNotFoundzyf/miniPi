"""TaskItem 数据模型与 TaskStore DAG 依赖状态机。"""

from __future__ import annotations

import json
import os
import tempfile
from contextlib import suppress
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from graphlib import CycleError, TopologicalSorter
from hashlib import sha256
from pathlib import Path
from threading import Lock
from typing import Any, Literal, cast

from filelock import FileLock

TaskStatus = Literal["pending", "in_progress", "completed", "deleted"]
VALID_STATUSES = ("pending", "in_progress", "completed", "deleted")
TASK_LOCK_TIMEOUT_SECONDS = 3


@dataclass
class TaskItem:
    """单个结构化工单项（对标 OpenHands TaskItem 与 trpc-agent TaskRecord）。"""

    id: str
    subject: str
    description: str = ""
    status: TaskStatus = "pending"
    owner: str | None = None
    active_form: str | None = None
    blocked_by: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    dependencies: list[str] = field(default_factory=list)


class TaskStore:
    """基于 DAG 依赖图与原子持久化的项目任务仓库。"""

    def __init__(
        self,
        workspace: Path | str,
        enforce_single_in_progress: bool = True,
        allow_parallel: bool = False,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.store_dir = self.workspace / ".my_agent_core"
        self.file_path = self.store_dir / "tasks.json"
        self.enforce_single_in_progress = False if allow_parallel else enforce_single_in_progress
        self.tasks: dict[str, TaskItem] = {}
        self._next_id = 1
        self._load_error: str | None = None
        self._disk_revision: bytes | None = None
        self._lock = Lock()
        self._load_from_disk()

    def _load_from_disk(self) -> None:
        try:
            content = self.file_path.read_bytes()
            self._disk_revision = sha256(content).digest()
            data = json.loads(content.decode("utf-8"))
            next_id = data.get("next_id", 1)
            if type(next_id) is not int or next_id < 1:
                raise ValueError("Invalid next_id")
            tasks: dict[str, TaskItem] = {}
            if not isinstance(data.get("tasks", []), list):
                raise ValueError("Invalid tasks list")
            for item in data.get("tasks", []):
                item.setdefault("dependencies", item.get("blocked_by", []))
                task = TaskItem(**item)
                if not isinstance(task.id, str) or not task.id or task.id in tasks:
                    raise ValueError("Invalid or duplicate task ID")
                if not isinstance(task.subject, str):
                    raise ValueError(f"Invalid subject for {task.id}")
                if not isinstance(task.dependencies, list) or not all(isinstance(dep, str) for dep in task.dependencies):
                    raise ValueError(f"Invalid dependencies for {task.id}")
                tasks[task.id] = task
                prefix, _, number = task.id.partition("_")
                if prefix == "task" and number.isascii() and number.isdecimal():
                    next_id = max(next_id, int(number) + 1)
            for task in tasks.values():
                task.blocked_by = self._unfinished_dependencies(task, tasks)
        except FileNotFoundError:
            self.tasks = {}
            self._next_id = 1
            self._disk_revision = None
            self._load_error = None
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            self._load_error = f"Cannot load {self.file_path.name} ({type(exc).__name__}). Restore it before editing."
        else:
            self.tasks = tasks
            self._next_id = next_id
            self._load_error = None

    def _save_to_disk(self, tasks: dict[str, TaskItem], next_id: int) -> bytes:
        payload = {"next_id": next_id, "tasks": [asdict(task) for task in tasks.values()]}
        content = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        revision = sha256(content).digest()
        fd, tmp_path = tempfile.mkstemp(dir=self.store_dir, prefix="tasks_", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, self.file_path)
        except BaseException:
            with suppress(OSError):
                Path(tmp_path).unlink(missing_ok=True)
            raise
        return revision

    def _commit(self, tasks: dict[str, TaskItem], next_id: int) -> None:
        self.store_dir.mkdir(parents=True, exist_ok=True)
        lock_path = self.file_path.with_suffix(".json.lock")
        with FileLock(str(lock_path), timeout=TASK_LOCK_TIMEOUT_SECONDS):
            try:
                current = sha256(self.file_path.read_bytes()).digest()
            except FileNotFoundError:
                current = None
            if current != self._disk_revision:
                self._load_from_disk()
                if self._load_error is None:
                    raise ValueError("Task board changed on disk and has been refreshed. Review it with todo list, then retry.")
            if self._load_error is not None:
                raise ValueError(self._load_error)
            self._validate(tasks)
            self._disk_revision = self._save_to_disk(tasks, next_id)
            self.tasks = tasks
            self._next_id = next_id

    def _validate(self, tasks: dict[str, TaskItem]) -> None:
        graph = TopologicalSorter()
        active: list[str] = []
        for task in tasks.values():
            if not task.subject.strip():
                raise ValueError("Task subject cannot be empty")
            if task.status not in VALID_STATUSES:
                raise ValueError(f"Invalid task status '{task.status}' for {task.id}")
            for dependency in task.dependencies:
                if dependency == task.id:
                    raise ValueError("Task cannot depend on itself")
                if dependency not in tasks:
                    raise KeyError(f"Dependency task '{dependency}' not found")
                if task.status != "deleted" and tasks[dependency].status == "deleted":
                    raise ValueError(f"Task '{task.id}' depends on deleted task '{dependency}'")
            graph.add(task.id, *task.dependencies)
            task.blocked_by = self._unfinished_dependencies(task, tasks)
            if task.status == "in_progress":
                if task.blocked_by:
                    raise ValueError(f"Task '{task.id}' has unfinished dependencies: {', '.join(task.blocked_by)}")
                active.append(task.id)

        try:
            graph.prepare()
        except CycleError as exc:
            raise ValueError(f"Cycle detected: {' -> '.join(exc.args[1])}") from exc
        if self.enforce_single_in_progress and len(active) > 1:
            existing = next(
                (task_id for task_id in active if task_id in self.tasks and self.tasks[task_id].status == "in_progress"),
                active[0],
            )
            raise ValueError(f"Task '{existing}' is already in progress")

    @staticmethod
    def _unfinished_dependencies(task: TaskItem, tasks: dict[str, TaskItem]) -> list[str]:
        return [dep for dep in task.dependencies if dep not in tasks or tasks[dep].status != "completed"]

    async def create(
        self,
        subject: str,
        description: str = "",
        active_form: str | None = None,
        metadata: dict | None = None,
        blocked_by: list[str] | None = None,
    ) -> TaskItem:
        """创建新任务，自动分配递增 ID。"""
        with self._lock:
            tasks = deepcopy(self.tasks)
            task_id = f"task_{self._next_id}"
            tasks[task_id] = TaskItem(
                id=task_id,
                subject=subject.strip(),
                description=description,
                active_form=active_form,
                dependencies=list(dict.fromkeys(blocked_by or [])),
                metadata=deepcopy(metadata or {}),
            )
            self._commit(tasks, self._next_id + 1)
            return tasks[task_id]

    async def update(
        self,
        task_id: str,
        status: TaskStatus | None = None,
        subject: str | None = None,
        description: str | None = None,
        active_form: str | None = None,
        owner: str | None = None,
        metadata: dict | None = None,
        add_blocked_by: list[str] | None = None,
        remove_blocked_by: list[str] | None = None,
    ) -> tuple[TaskItem, list[str]]:
        """局部增量更新任务字段与 DAG 依赖，并计算自动解锁列表。"""
        with self._lock:
            self.get(task_id)
            tasks = deepcopy(self.tasks)
            task = tasks[task_id]

            removed = set(remove_blocked_by or [])
            task.dependencies = [
                dep for dep in dict.fromkeys([*task.dependencies, *(add_blocked_by or [])]) if dep not in removed
            ]
            if status is not None:
                task.status = status
            if subject is not None:
                task.subject = subject.strip()
            if description is not None:
                task.description = description
            if active_form is not None:
                task.active_form = active_form
            if owner is not None:
                task.owner = owner
            if metadata is not None:
                task.metadata.update(deepcopy(metadata))

            previously_blocked = {item.id for item in self.tasks.values() if item.status == "pending" and item.blocked_by}
            self._commit(tasks, self._next_id)
            unblocked = [
                item.id for item in tasks.values()
                if item.id in previously_blocked and item.status == "pending" and not item.blocked_by
            ]
            return task, unblocked

    def get(self, task_id: str) -> TaskItem:
        """获取单个任务详情。"""
        if task_id not in self.tasks:
            raise KeyError(f"Task '{task_id}' not found")
        return self.tasks[task_id]

    def list(self, include_deleted: bool = False) -> list[TaskItem]:
        """列出所有活跃任务。"""
        return [t for t in self.tasks.values() if include_deleted or t.status != "deleted"]

    async def batch_write(self, todos: list[dict[str, Any]]) -> list[TaskItem]:
        """Update items with known IDs and add new items in one commit."""
        with self._lock:
            tasks = deepcopy(self.tasks)
            next_id = self._next_id
            for item in todos:
                task_id = item.get("id")
                if isinstance(task_id, str) and task_id in self.tasks:
                    task = tasks[task_id]
                else:
                    task_id = f"task_{next_id}"
                    next_id += 1
                    task = TaskItem(id=task_id, subject="Untitled")
                    tasks[task_id] = task
                if "subject" in item:
                    if not isinstance(item["subject"], str):
                        raise ValueError("Task subject must be text")
                    task.subject = item["subject"].strip()
                if "description" in item:
                    task.description = str(item["description"])
                if "status" in item:
                    if item["status"] not in VALID_STATUSES:
                        raise ValueError(f"Invalid task status '{item['status']}'")
                    task.status = cast(TaskStatus, item["status"])
                if "blocked_by" in item:
                    dependencies = item["blocked_by"]
                    if not isinstance(dependencies, list) or not all(isinstance(dep, str) for dep in dependencies):
                        raise ValueError("blocked_by must be a list of task IDs")
                    task.dependencies = list(dict.fromkeys(dependencies))

            self._commit(tasks, next_id)
            return self.list()

    def clear(self) -> None:
        """Clear the board after the empty state has been committed to disk."""
        with self._lock:
            self._commit({}, 1)

    def render_board(self) -> str:
        """渲染紧凑 Markdown 看板。"""
        tasks = self.list()
        if not tasks:
            return "(No active tasks)"
        lines: list[str] = []
        for t in tasks:
            if t.status == "completed":
                icon = "[x]"
            elif t.status == "in_progress":
                icon = "[>]"
            else:
                icon = "[ ]"

            status_desc = f"{t.status}"
            if t.active_form and t.status == "in_progress":
                status_desc += f" - {t.active_form}"
            if t.blocked_by:
                status_desc += f", blocked by: {t.blocked_by}"

            lines.append(f"{icon} {t.id}: {t.subject} ({status_desc})")
        return "\n".join(lines)
