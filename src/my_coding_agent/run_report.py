"""Collect run statistics and track which native file changes have been verified."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from time import perf_counter
from typing import Any

from my_agent_core.events import AgentEnd, Event, MessageEnd, ToolExecutionEnd, ToolExecutionStart
from my_agent_core.registry import ToolRegistry
from my_agent_llm.request_stats import ModelRequestStats
from my_agent_llm.stream import close_stream


@dataclass
class VerificationState:
    command: str | None = None
    last_result: str | None = None
    changed_since_pass: bool = False

    @property
    def status(self) -> str:
        if self.command is None:
            return "not_configured"
        if self.last_result == "passed" and self.changed_since_pass:
            return "stale"
        return self.last_result or "not_run"

    def record(self, status: str) -> None:
        self.last_result = status
        self.changed_since_pass = False


class RunReport:
    def __init__(self, registry: ToolRegistry, verification: VerificationState) -> None:
        self.registry = registry
        self.verification = verification
        self.started = perf_counter()
        self.stop_reason = "error"
        self.iterations: int | None = None
        self.final_text = ""
        self.message_usage: list[dict[str, int]] = []
        self.model_requests = ModelRequestStats()
        self.tool_counts: defaultdict[str, Counter[str]] = defaultdict(Counter)
        self.verification_attempts: list[dict[str, str]] = []

    @asynccontextmanager
    async def capture(self, stream: AsyncIterator[Event]) -> AsyncIterator[AsyncIterator[Event]]:
        with self.model_requests.collect():
            async with close_stream(stream):
                yield stream

    def observe(self, event: Event) -> None:
        if isinstance(event, AgentEnd):
            self.stop_reason = event.stop_reason
            self.iterations = event.iterations
            self.final_text = event.final_text or ""
        elif isinstance(event, MessageEnd) and event.message.role == "assistant":
            usage = (event.message.metadata or {}).get("usage")
            if isinstance(usage, dict):
                self.message_usage.append(usage)
        elif isinstance(event, ToolExecutionStart):
            self.tool_counts[event.tool_name]["attempted"] += 1
        elif isinstance(event, ToolExecutionEnd):
            self._observe_tool_result(event)

    def _observe_tool_result(self, event: ToolExecutionEnd) -> None:
        status = event.execution_status or ("failed" if event.is_error else "succeeded")
        name = event.tool_name
        self.tool_counts[name][status] += 1
        if name not in {"bash", "edit", "write"} or self.verification.command is None:
            return
        tool = self.registry.get(name)
        if tool is None or tool.func.__module__ != f"my_coding_agent.tools.{name}":
            return

        if name in {"edit", "write"} and status == "succeeded" and event.result_meta.get("workspace_changed") is True:
            self.verification.changed_since_pass = True
        elif name == "bash" and status in {"succeeded", "failed"}:
            args = event.dispatched_args or {}
            command = args.get("command", args.get("cmd", args.get("script")))
            if command == self.verification.command and not args.get("run_in_background"):
                checked = "passed" if status == "succeeded" else "failed"
                self.verification.record(checked)
                self.verification_attempts.append({"tool_call_id": event.tool_call_id, "status": checked})

    def snapshot(self) -> dict[str, Any]:
        outcomes = ("succeeded", "failed", "blocked", "skipped")
        by_tool: dict[str, dict[str, int]] = {}
        totals = Counter(dict.fromkeys(("attempted", *outcomes, "incomplete"), 0))
        for name, counts in sorted(self.tool_counts.items()):
            completed = sum(counts[outcome] for outcome in outcomes)
            by_tool[name] = {
                "attempted": counts["attempted"],
                **{outcome: counts[outcome] for outcome in outcomes},
                "incomplete": max(0, counts["attempted"] - completed),
            }
            totals.update(by_tool[name])
        return {
            "duration_sec": round(perf_counter() - self.started, 3),
            "stop_reason": self.stop_reason,
            "iterations": self.iterations,
            "model_requests": self.model_requests.snapshot(),
            "tool_calls": {**totals, "by_tool": by_tool},
            "verification": {"status": self.verification.status, "attempts": list(self.verification_attempts)},
        }
