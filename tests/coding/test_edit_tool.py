from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from my_agent_core.loop import CancellationToken
from my_coding_agent.mutation_queue import FileMutationQueue
from my_coding_agent.permissions import PermissionGate
from my_coding_agent.tools.edit import EditBlock, make_edit_tool

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize(
    ("original", "code", "advice"),
    [
        (b"TIMEOUT = 45\n", "old_text_not_found", "Read the current file"),
        (b"TIMEOUT = 30\nTIMEOUT = 30\n", "old_text_ambiguous", "unchanged surrounding lines"),
    ],
)
async def test_repeated_edit_uses_file_content_and_run_scope(tmp_path, original, code, advice):
    target = tmp_path / "settings.py"
    target.write_bytes(original)
    tool = make_edit_tool(tmp_path)
    run = CancellationToken()
    args = {"path": "settings.py", "edits": [{"oldText": "TIMEOUT = 30", "newText": "TIMEOUT = 60"}]}

    results = [await tool.execute(args, signal=run) for _ in range(4)]
    assert [result.meta["code"] for result in results] == [code, code, code, "repeated_edit_failure"]
    assert all(not result.ok for result in results)
    assert "Same edit failed 2 times" in results[1].error
    assert "The next unchanged attempt will be blocked" in results[2].error
    assert advice in results[3].error
    assert target.read_bytes() == original

    next_run = await tool.execute(args, signal=CancellationToken())
    assert next_run.meta["code"] == code
    assert "Same edit failed" not in next_run.error

    # The same parameters are now valid on different bytes, even in the original run.
    target.write_bytes(b"TIMEOUT = 30\n")
    recovered = await tool.execute(args, signal=run)
    assert recovered.ok
    assert target.read_bytes() == b"TIMEOUT = 60\n"


async def test_denied_reviews_do_not_count_as_edit_match_failures(tmp_path):
    target = tmp_path / "settings.py"
    target.write_bytes(b"TIMEOUT = 30\n")
    reviews = []

    def deny(request):
        reviews.append(request)
        return False

    tool = make_edit_tool(tmp_path, permission_gate=PermissionGate(confirm_callback=deny))
    run = CancellationToken()
    for _ in range(4):
        result = await tool.execute(path="settings.py", edits={"oldText": "30", "newText": "60"}, signal=run)
        assert result.meta["code"] == "review_denied"
    assert len(reviews) == 4
    assert target.read_bytes() == b"TIMEOUT = 30\n"


async def test_approved_edit_rechecks_snapshot_after_waiting_for_lock(tmp_path: Path):
    target = tmp_path / "settings.py"
    target.write_bytes(b"TIMEOUT = 30\n")
    queue = FileMutationQueue()
    approved = asyncio.Event()

    def confirm(_request):
        approved.set()
        return True

    tool = make_edit_tool(tmp_path, queue, permission_gate=PermissionGate(confirm_callback=confirm))
    async with queue.acquire(target):
        pending = asyncio.create_task(tool.execute(path="settings.py", edits={"oldText": "30", "newText": "60"}))
        try:
            await asyncio.wait_for(approved.wait(), timeout=2)
            assert not pending.done()
            target.write_bytes(b"TIMEOUT = 45\n")
        except BaseException:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
            raise

    result = await asyncio.wait_for(pending, timeout=2)
    assert not result.ok
    assert result.meta["code"] == "preview_stale"
    assert target.read_bytes() == b"TIMEOUT = 45\n"


async def test_edit_single_block(tmp_path: Path):
    f = tmp_path / "code.py"
    f.write_text("def foo():\n    return 1\n", encoding="utf-8")
    tool = make_edit_tool(tmp_path)
    res = await tool.execute(
        path="code.py",
        edits=[{"oldText": "return 1", "newText": "return 42"}],
    )
    assert "Successfully applied 1 edit(s)" in res
    assert "-    return 1" in res
    assert "+    return 42" in res
    assert f.read_text(encoding="utf-8") == "def foo():\n    return 42\n"


async def test_edit_multiple_disjoint_blocks(tmp_path: Path):
    f = tmp_path / "multi.py"
    f.write_text("a = 1\nb = 2\nc = 3\n", encoding="utf-8")
    tool = make_edit_tool(tmp_path)
    res = await tool.execute(
        path="multi.py",
        edits=[
            {"oldText": "a = 1", "newText": "a = 100"},
            {"oldText": "c = 3", "newText": "c = 300"},
        ],
    )
    assert "Successfully applied 2 edit(s)" in res
    assert f.read_text(encoding="utf-8") == "a = 100\nb = 2\nc = 300\n"


async def test_edit_crlf_preservation(tmp_path: Path):
    f = tmp_path / "crlf.py"
    f.write_bytes(b"line1\r\nline2\r\nline3\r\n")
    tool = make_edit_tool(tmp_path)
    res = await tool.execute(
        path="crlf.py",
        edits=[{"oldText": "line2", "newText": "modified"}],
    )
    assert "Successfully applied 1 edit(s)" in res
    assert f.read_bytes() == b"line1\r\nmodified\r\nline3\r\n"


async def test_edit_bom_preservation(tmp_path: Path):
    f = tmp_path / "bom.py"
    f.write_bytes(b"\xef\xbb\xbfname = 'test'\n")
    tool = make_edit_tool(tmp_path)
    res = await tool.execute(
        path="bom.py",
        edits=[{"oldText": "'test'", "newText": "'prod'"}],
    )
    assert "Successfully applied 1 edit(s)" in res
    assert f.read_bytes() == b"\xef\xbb\xbfname = 'prod'\n"


async def test_edit_not_found_error(tmp_path: Path):
    f = tmp_path / "missing.py"
    f.write_text("alpha\nbeta\n", encoding="utf-8")
    tool = make_edit_tool(tmp_path)
    res = await tool.execute(
        path="missing.py",
        edits=[{"oldText": "gamma", "newText": "delta"}],
    )
    assert "Error: 'oldText' not found in missing.py" in res


async def test_edit_multiple_occurrences_error(tmp_path: Path):
    f = tmp_path / "duplicate.py"
    f.write_text("repeat\nrepeat\n", encoding="utf-8")
    tool = make_edit_tool(tmp_path)
    res = await tool.execute(
        path="duplicate.py",
        edits=[{"oldText": "repeat", "newText": "once"}],
    )
    assert "Error: 'oldText' matched 2 times" in res


async def test_edit_overlapping_regions_error(tmp_path: Path):
    f = tmp_path / "overlap.py"
    f.write_text("one two three\n", encoding="utf-8")
    tool = make_edit_tool(tmp_path)
    res = await tool.execute(
        path="overlap.py",
        edits=[
            {"oldText": "one two", "newText": "1 2"},
            {"oldText": "two three", "newText": "2 3"},
        ],
    )
    assert "Error: Overlapping edit regions detected" in res


async def test_edit_file_not_found_error(tmp_path: Path):
    tool = make_edit_tool(tmp_path)
    res = await tool.execute(path="nonexistent.py", edits=[{"oldText": "a", "newText": "b"}])
    assert "Error: File not found: nonexistent.py" in res


async def test_edit_directory_error(tmp_path: Path):
    d = tmp_path / "subdir"
    d.mkdir()
    tool = make_edit_tool(tmp_path)
    res = await tool.execute(path="subdir", edits=[{"oldText": "a", "newText": "b"}])
    assert "Error: Path is a directory: subdir" in res


async def test_edit_binary_file_error(tmp_path: Path):
    f = tmp_path / "binary.bin"
    f.write_bytes(b"hello\x00world")
    tool = make_edit_tool(tmp_path)
    res = await tool.execute(path="binary.bin", edits=[{"oldText": "hello", "newText": "hi"}])
    assert "Error: Cannot edit binary file: binary.bin" in res


async def test_edit_no_params_error(tmp_path: Path):
    f = tmp_path / "code.py"
    f.write_text("test", encoding="utf-8")
    tool = make_edit_tool(tmp_path)
    res = await tool.execute(path="code.py")
    assert "Error: 'edits' must be provided as a list of edit blocks." in res

    res_empty = await tool.execute(path="code.py", edits=[])
    assert "Error: No edits provided." in res_empty


async def test_edit_empty_old_text_error(tmp_path: Path):
    f = tmp_path / "code.py"
    f.write_text("test", encoding="utf-8")
    tool = make_edit_tool(tmp_path)
    res = await tool.execute(
        path="code.py",
        edits=[{"oldText": "", "newText": "replaced"}],
    )
    assert "Error: 'oldText' cannot be empty" in res


async def test_edit_delete_content(tmp_path: Path):
    f = tmp_path / "delete.py"
    f.write_text("line1\nremove_me\nline2\n", encoding="utf-8")
    tool = make_edit_tool(tmp_path)
    res = await tool.execute(
        path="delete.py",
        edits=[{"oldText": "remove_me\n", "newText": ""}],
    )
    assert "Successfully applied 1 edit(s)" in res
    assert f.read_text(encoding="utf-8") == "line1\nline2\n"


async def test_edit_edit_block_instances(tmp_path: Path):
    f = tmp_path / "blocks.py"
    f.write_text("var = 1\n", encoding="utf-8")
    tool = make_edit_tool(tmp_path)
    res = await tool.execute(
        path="blocks.py",
        edits=[EditBlock(old_text="var = 1", new_text="var = 2")],
    )
    assert "Successfully applied 1 edit(s)" in res
    assert f.read_text(encoding="utf-8") == "var = 2\n"


async def test_edit_dict_call_and_is_parallel_safe(tmp_path: Path):
    f = tmp_path / "dict_call.py"
    f.write_text("hello world\n", encoding="utf-8")
    tool = make_edit_tool(tmp_path)
    assert tool.is_parallel_safe is True

    res = await tool.execute({"path": "dict_call.py", "edits": [{"old_text": "world", "new_text": "friend"}]})
    assert res.ok is True
    assert "Successfully applied 1 edit(s)" in str(res.data)
    assert f.read_text(encoding="utf-8") == "hello friend\n"


async def test_edit_concurrency_serialization(tmp_path: Path):
    f = tmp_path / "counter.txt"
    f.write_text("start\n", encoding="utf-8")
    queue = FileMutationQueue()
    tool = make_edit_tool(tmp_path, mutation_queue=queue)

    events: list[str] = []

    async def op1():
        events.append("start_1")
        await tool.execute(path="counter.txt", old_text="start", new_text="mid")
        await asyncio.sleep(0.01)
        events.append("end_1")

    async def op2():
        events.append("start_2")
        await tool.execute(path="counter.txt", old_text="mid", new_text="finish")
        events.append("end_2")

    await asyncio.gather(op1(), op2())

    assert len(events) == 4


async def test_edit_json_string_and_single_dict_compat(tmp_path: Path):
    """测试 edits 传入 JSON 字符串与单 dict 对象的容错兼容（对标 Pi prepareEditArguments）。"""
    f = tmp_path / "compat.txt"
    f.write_text("aaa bbb ccc\n", encoding="utf-8")
    tool = make_edit_tool(tmp_path)

    # 1. edits 传入 JSON 字符串
    res1 = await tool.execute(
        {
            "path": "compat.txt",
            "edits": '[{"oldText": "aaa", "newText": "xxx"}]',
        }
    )
    assert res1.ok is True
    assert f.read_text(encoding="utf-8") == "xxx bbb ccc\n"

    # 2. edits 传入单个 dict
    res2 = await tool.execute(
        {
            "path": "compat.txt",
            "edits": {"oldText": "bbb", "newText": "yyy"},
        }
    )
    assert res2.ok is True
    assert f.read_text(encoding="utf-8") == "xxx yyy ccc\n"
