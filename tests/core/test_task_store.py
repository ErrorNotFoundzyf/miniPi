import asyncio
import json
from pathlib import Path

import pytest  # pyright: ignore[reportMissingImports]

from my_agent_core.task_store import TaskStore  # pyright: ignore[reportMissingImports]


def test_task_store_create_and_get(tmp_path: Path):
    async def _test():
        store = TaskStore(tmp_path)
        t1 = await store.create(subject="Design schema", description="Create users table")
        assert t1.id == "task_1"
        assert t1.status == "pending"
        assert t1.subject == "Design schema"
        assert store.get("task_1").description == "Create users table"

    asyncio.run(_test())


def test_task_store_dag_dependencies_and_unblock(tmp_path: Path):
    async def _test():
        store = TaskStore(tmp_path)
        await store.create(subject="Task 1")
        await store.create(subject="Task 2")

        # t2 blocked by t1
        updated_t2, unblocked = await store.update("task_2", add_blocked_by=["task_1"])
        assert updated_t2.blocked_by == ["task_1"]
        assert unblocked == []

        # Complete t1 -> t2 should unblock
        updated_t1, unblocked = await store.update("task_1", status="completed")
        assert updated_t1.status == "completed"
        assert "task_2" in unblocked

    asyncio.run(_test())


def test_task_store_cycle_detection(tmp_path: Path):
    async def _test():
        store = TaskStore(tmp_path)
        await store.create(subject="Task 1")
        await store.create(subject="Task 2")
        await store.update("task_2", add_blocked_by=["task_1"])

        with pytest.raises(ValueError, match="[Cc]ycle"):
            await store.update("task_1", add_blocked_by=["task_2"])

    asyncio.run(_test())


def test_task_store_single_in_progress_enforcement(tmp_path: Path):
    async def _test():
        store = TaskStore(tmp_path, enforce_single_in_progress=True)
        await store.create(subject="Task 1")
        await store.create(subject="Task 2")

        await store.update("task_1", status="in_progress")
        with pytest.raises(ValueError, match="already in progress"):
            await store.update("task_2", status="in_progress")

    asyncio.run(_test())


def test_task_store_render_board(tmp_path: Path):
    async def _test():
        store = TaskStore(tmp_path)
        await store.create(subject="Task 1")
        await store.create(subject="Task 2")
        await store.update("task_1", status="completed")
        await store.update("task_2", status="in_progress", active_form="writing code")

        board = store.render_board()
        assert "[x] task_1: Task 1" in board
        assert "[>] task_2: Task 2" in board
        assert "writing code" in board

    asyncio.run(_test())


def test_task_store_persistence(tmp_path: Path):
    async def _test():
        store1 = TaskStore(tmp_path)
        await store1.create(subject="Persisted task")

        store2 = TaskStore(tmp_path)
        assert len(store2.list()) == 1
        assert store2.get("task_1").subject == "Persisted task"

    asyncio.run(_test())


def test_task_store_batch_write(tmp_path: Path):
    async def _test():
        store = TaskStore(tmp_path)
        items = await store.batch_write(
            [
                {"subject": "Batch task 1", "status": "completed"},
                {"subject": "Batch task 2", "status": "in_progress"},
            ]
        )
        assert len(items) == 2
        assert store.get("task_1").status == "completed"
        assert store.get("task_2").status == "in_progress"

    asyncio.run(_test())


def test_task_store_invalid_operations(tmp_path: Path):
    async def _test():
        store = TaskStore(tmp_path)
        with pytest.raises(ValueError, match="cannot be empty"):
            await store.create(subject="   ")

        with pytest.raises(KeyError, match="not found"):
            store.get("task_999")

        with pytest.raises(KeyError, match="not found"):
            await store.update("task_999", status="completed")

        await store.create(subject="Task 1")
        with pytest.raises(ValueError, match="cannot depend on itself"):
            await store.update("task_1", add_blocked_by=["task_1"])

        with pytest.raises(KeyError, match="not found"):
            await store.update("task_1", add_blocked_by=["task_nonexistent"])

    asyncio.run(_test())


def test_task_store_multiple_creates_and_updates(tmp_path: Path):
    async def _test():
        store = TaskStore(tmp_path)

        # 1. Sequential creates
        tasks = [await store.create(subject=f"Task {i}") for i in range(10)]
        assert len(tasks) == 10
        unique_ids = {t.id for t in tasks}
        assert len(unique_ids) == 10
        assert len(store.list()) == 10

        # 2. Sequential updates on distinct tasks
        for i in range(10):
            await store.update(f"task_{i + 1}", description=f"Desc {i}")
            assert store.get(f"task_{i + 1}").description == f"Desc {i}"

        # 3. Reload from disk and verify consistency
        reloaded = TaskStore(tmp_path)
        assert len(reloaded.list()) == 10

    asyncio.run(_test())


@pytest.mark.anyio
async def test_task_dependencies_are_enforced_for_the_final_single_and_batch_state(tmp_path):
    store = TaskStore(tmp_path)
    first = await store.create("Prerequisite")
    second = await store.create("Dependent", blocked_by=[first.id])
    before = store.file_path.read_bytes()

    with pytest.raises(ValueError, match="unfinished dependencies"):
        await store.update(second.id, status="in_progress")
    with pytest.raises(ValueError, match="unfinished dependencies"):
        await store.batch_write([{"id": second.id, "status": "in_progress"}])
    assert store.file_path.read_bytes() == before
    assert store.get(second.id).status == "pending"

    await store.batch_write([
        {"id": second.id, "status": "in_progress"},
        {"id": first.id, "status": "completed"},
    ])
    store = TaskStore(tmp_path)
    assert store.get(second.id).blocked_by == []
    assert store.get(second.id).dependencies == [first.id]
    with pytest.raises(ValueError, match="unfinished dependencies"):
        await store.update(first.id, status="pending")
    with pytest.raises(ValueError, match="depends on deleted task"):
        await store.update(first.id, status="deleted")
    await store.batch_write([
        {"id": first.id, "status": "pending"},
        {"id": second.id, "status": "pending"},
    ])
    assert store.get(second.id).blocked_by == [first.id]
    ready, _ = await store.update(second.id, status="in_progress", remove_blocked_by=[first.id])
    assert ready.status == "in_progress"


@pytest.mark.anyio
async def test_task_errors_preserve_persisted_board_and_next_id(tmp_path, monkeypatch):
    store = TaskStore(tmp_path)
    first = await store.create("First")
    before = store.file_path.read_bytes()
    with pytest.raises(KeyError, match="not found"):
        await store.create("Invalid", blocked_by=["missing"])
    with pytest.raises(ValueError, match="already in progress"):
        await store.batch_write([
            {"id": first.id, "subject": "Renamed", "status": "in_progress"},
            {"subject": "Second", "status": "in_progress"},
        ])
    with pytest.raises(ValueError, match="subject cannot be empty"):
        await store.batch_write([{"subject": "Second"}, {"subject": ""}])

    def deny_replace(*_):
        raise PermissionError("disk busy")

    with monkeypatch.context() as patch:
        patch.setattr("my_agent_core.task_store.os.replace", deny_replace)
        with pytest.raises(PermissionError, match="disk busy"):
            await store.batch_write([{"id": first.id, "subject": "Renamed"}, {"subject": "Second"}])
        with pytest.raises(PermissionError, match="disk busy"):
            store.clear()

    assert store.file_path.read_bytes() == before
    assert not list(store.store_dir.glob("tasks_*.tmp"))
    assert [(task.id, task.subject, task.status) for task in store.list()] == [(first.id, "First", "pending")]
    assert (await store.create("Second")).id == "task_2"


@pytest.mark.anyio
async def test_task_store_reads_previous_dependency_format(tmp_path):
    store = TaskStore(tmp_path)
    store.store_dir.mkdir()
    store.file_path.write_text(json.dumps({"next_id": 3, "tasks": [
        {"id": "task_1", "subject": "First"},
        {"id": "task_2", "subject": "Dependent", "blocked_by": ["task_1"]},
    ]}), encoding="utf-8")
    loaded = TaskStore(tmp_path)
    with pytest.raises(ValueError, match="unfinished dependencies"):
        await loaded.update("task_2", status="in_progress")
    _, unblocked = await loaded.update("task_1", status="completed")
    assert unblocked == ["task_2"]
    assert loaded.get("task_2").blocked_by == []
    assert loaded.get("task_2").dependencies == ["task_1"]


@pytest.mark.anyio
async def test_task_store_rejects_stale_update_between_instances(tmp_path):
    first, second = TaskStore(tmp_path), TaskStore(tmp_path)
    await first.create("First task")
    before = first.file_path.read_bytes()

    with pytest.raises(ValueError, match="changed on disk"):
        await second.create("Second task")
    assert first.file_path.read_bytes() == before
    assert [task.subject for task in second.list()] == ["First task"]

    assert (await second.create("Second task")).id == "task_2"
    with pytest.raises(ValueError, match="changed on disk"):
        await first.update("task_1", status="completed")
    assert [task.subject for task in TaskStore(tmp_path).list()] == ["First task", "Second task"]
