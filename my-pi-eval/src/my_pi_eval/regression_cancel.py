"""Exercise the native Bash tool with a user-controlled cancellation signal."""

from __future__ import annotations

import asyncio
from pathlib import Path
from time import perf_counter

from my_pi_eval.regression_case import write_json


COMPLETION_OBSERVATION_SEC = 9


async def collect_cancellation(workspace: Path, evidence: Path, command: str, timeout: int) -> None:
    from my_agent_core.loop import CancellationToken
    from my_coding_agent.tools import make_bash_tool

    signal = CancellationToken()
    ready = workspace / "ready.txt"
    finished = workspace / "finished.txt"
    result_path = evidence / "cancellation.json"
    evidence.mkdir(parents=True)
    task = asyncio.create_task(make_bash_tool(workspace).execute(command=command, signal=signal, timeout=20))
    try:
        async with asyncio.timeout(timeout):
            while not ready.is_file() and not task.done():
                await asyncio.sleep(0.02)
            if not ready.is_file() or task.done():
                write_json(result_path, {"ready": False})
                return
            started = perf_counter()
            signal.cancel()
            result = await task
            latency = round((perf_counter() - started) * 1000, 2)
            await asyncio.sleep(max(0, COMPLETION_OBSERVATION_SEC - (perf_counter() - started)))
            write_json(result_path, {
                "ready": True, "latency_ms": latency, "ok": result.ok, "tool_result": result.serialize(),
                "finished_naturally": finished.is_file(),
            })
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
