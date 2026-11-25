"""Run paired local checks of native Bash logs, edit recovery, and file review."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib
import json
import os
import re
import tempfile
from collections.abc import AsyncIterator
from contextlib import nullcontext
from functools import partial
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import patch

from my_agent_core.hooks import HookResult, ToolCallHook
from my_agent_llm.client import LLM
from my_agent_llm.config import Config
from my_agent_llm.events import StreamDoneEvent, StreamStartEvent, ToolCallDoneEvent
from my_agent_llm.models import Message, ToolCall
from my_coding_agent import tools
from my_coding_agent.agent import CodingAgent
from my_coding_agent.headless import PROVIDER_ENV, model_config, run_native
from my_coding_agent.permissions import PermissionGate, PermissionRequest

ROOT = Path(__file__).resolve().parents[2]
BASH_PATH = "src/my_coding_agent/tools/bash.py"
BASH_BASELINE = "a867a9854fc8e41823658784494aa0c2627aed05"
BASH_BASELINE_SHA256 = "16047f16eabf75b7b4d71510d0744de3244410120736f543be96b6a48dff4ce3"
COMMAND = "python -u check_fixture.py"
MARKER = "ERROR E74295: fixture database connection refused"
LOG_PATH = re.compile(r"Full output saved to: (.*?)\]")
SETTINGS = "settings.txt"
BEFORE = b"TIMEOUT = 45\n"
EXPECTED = b"TIMEOUT = 60\n"
EXTERNAL = b"TIMEOUT = 50\n"
EXPECTED_DIFF = "--- a/settings.txt\n+++ b/settings.txt\n@@ -1 +1 @@\n-TIMEOUT = 45\n+TIMEOUT = 60\n"
Action = tuple[str, dict[str, Any]]


def edit_from(old: str) -> Action:
    return "edit", {"path": SETTINGS, "edits": [{"oldText": old, "newText": "TIMEOUT = 60"}]}


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def fixture_baseline() -> tuple[ModuleType, str]:
    source = (ROOT / "my-pi-eval" / "baselines" / f"bash-{BASH_BASELINE}.py.txt").read_bytes()
    digest = hashlib.sha256(source).hexdigest()
    if digest != BASH_BASELINE_SHA256:
        raise ValueError(f"Historical Bash snapshot failed its integrity check: {BASH_BASELINE}")
    module = ModuleType("my_coding_agent.tools.bash")
    exec(compile(source, f"{BASH_BASELINE}:{BASH_PATH}", "exec"), vars(module))
    return module, digest


class UntrackedRun:
    """Benchmark-only replacement for the edit guard's run-token type check."""


class FixtureRequestLimit(RuntimeError):
    """The evaluator has used its allotted live calls for this fixture."""


class FixtureGate(PermissionGate):
    def __init__(self, workspace: Path, log_dir: Path, scenario: str, review: str | None):
        super().__init__(mode="review", file_confirm_callback=self.approve_fixture)
        self.target = workspace / SETTINGS
        self.log_dir = log_dir
        self.scenario = scenario
        self.review = review
        self.bash_runs = 0
        self.previews: list[str] = []

    async def __call__(self, hook: ToolCallHook) -> HookResult:
        args = hook.args
        path = args.get("path")
        if self.scenario == "long_log":
            if hook.tool_name == "bash" and args.get("command") == COMMAND and not self.bash_runs:
                self.bash_runs += 1
                return HookResult(updated_args={"command": COMMAND, "timeout": 2, "run_in_background": False})
            if hook.tool_name in {"grep", "read"} and isinstance(path, str):
                target = Path(path).resolve()
                if target.parent == self.log_dir and target.match("pi-bash-*.log") and target.is_file():
                    return HookResult()
        elif hook.tool_name in {"read", "edit"} and path == SETTINGS:
            return HookResult()
        return HookResult(block=True, reason="Only the synthetic fixture and its own log may be used.")

    def restrict_file_tools(self, hook: ToolCallHook) -> HookResult | None:
        if hook.tool_name == "write" or (
            hook.tool_name == "edit" and (self.scenario == "long_log" or hook.args.get("path") != SETTINGS)
        ):
            return HookResult(block=True, reason="This fixture only permits edit on its settings.txt in the repair scenarios.")
        return None

    def approve_fixture(self, request: PermissionRequest) -> bool:
        preview = request.preview or ""
        self.previews.append(preview)
        if (
            self.scenario == "long_log"
            or request.action != "edit"
            or Path(request.target) != self.target
            or self.target.read_bytes() != BEFORE
            or preview != EXPECTED_DIFF
        ):
            return False
        if self.review == "stale":
            self.target.write_bytes(EXTERNAL)
        return self.scenario == "repeated_edit" or self.review in {"approve", "stale"}


class FixtureLLM:
    def __init__(self, scenario: str, live_config: Config | None):
        self.scenario = scenario
        self.steps = 0
        self.provider_attempts = 0
        self.live = LLM(config=live_config) if live_config else None
        self.config = live_config or SimpleNamespace(model="scripted", provider="offline")

    def scripted_action(self, step: int, observations: str) -> Action | None:
        if self.scenario == "long_log":
            if step == 0:
                return "bash", {"command": COMMAND, "timeout": 2}
            if step == 1 and (match := LOG_PATH.search(observations)):
                return "grep", {"path": match.group(1), "pattern": "ERROR", "limit": 5}
        elif self.scenario == "repeated_edit":
            if step < 4:
                return edit_from("TIMEOUT = 30")
            if step == 4:
                return "read", {"path": SETTINGS}
            if step == 5:
                return edit_from("TIMEOUT = 45")
        elif step == 0:
            return edit_from("TIMEOUT = 45")
        return None

    async def astream_events(self, messages: list[Message], **kwargs: Any) -> AsyncIterator[Any]:
        step = self.steps
        self.steps += 1
        stimulus_steps = 1 if self.scenario == "long_log" else 4
        if self.live is not None and step >= stimulus_steps:
            # Keep the per-case spend bounded, outside the Agent's production loop.
            if self.provider_attempts >= 6:
                raise FixtureRequestLimit("Fixture model request limit reached")
            self.provider_attempts += 1
            async for event in self.live.astream_events(messages, **kwargs):
                yield event
            return

        observations = "\n".join(str(message.content) for message in messages if message.role == "tool")
        action = self.scripted_action(step, observations)
        if action is None:
            content = MARKER if self.scenario == "long_log" and MARKER in observations else "Fixture run ended."
            message = Message(role="assistant", content=content)
            yield StreamStartEvent(partial=message)
        else:
            name, args = action
            call = ToolCall(id=f"stimulus-{step}", name=name, args=args)
            metadata: dict[str, Any] = {"tool_calls": [call]}
            if self.live is not None and self.config.provider == "deepseek":
                # DeepSeek expects this field on prior assistant tool calls in thinking mode.
                metadata["reasoning_content"] = "This call is a fixed regression fixture stimulus."
            message = Message(role="assistant", content="", metadata=metadata)
            yield StreamStartEvent(partial=message)
            yield ToolCallDoneEvent(index=0, tool_call=call, partial=message)
        yield StreamDoneEvent(message=message)


PROMPTS = {
    "long_log": (
        f"Run `{COMMAND}` once with a timeout of 2 seconds. If a saved log is available, "
        "look up the original ERROR line and quote it verbatim. Do not rerun the command or read the fixture source."
    ),
    "repeated_edit": "Change the TIMEOUT setting in settings.txt to 60 using edit. Inspect the current file to recover from failed edits.",
    "review": "Change the TIMEOUT setting in settings.txt from 45 to 60 using edit and respect the review outcome.",
}


def collect_result(case: str, mode: str, gate: FixtureGate, logs: Path, answer: str) -> dict[str, Any]:
    events_file = logs / "events.jsonl"
    events = [json.loads(line) for line in events_file.read_text(encoding="utf-8").splitlines()]
    end_events = [event for event in events if event["type"] == "tool_execution_end"]
    metrics = json.loads((logs / "metrics.json").read_text(encoding="utf-8"))
    result: dict[str, Any] = {"stop_reason": metrics["stop_reason"], "tool_calls": metrics["tool_calls"]}
    if case == "long_log":
        bash = [event for event in end_events if event["toolName"] == "bash"]
        bash_result = bash[0]["result"] if bash else ""
        result.update(
            timed_out="timed out after 2 seconds" in bash_result,
            has_saved_log=bool(LOG_PATH.search(bash_result)),
            marker_absent_from_preview=MARKER not in bash_result,
            recovered=any(event["toolName"] in {"grep", "read"} and MARKER in event["result"] for event in end_events),
            quoted=MARKER in answer,
            executed_commands=gate.bash_runs,
            repeat_attempts=max(0, len(bash) - 1),
        )
        result["passed"] = all(result[key] for key in ("timed_out", "marker_absent_from_preview", "recovered", "quoted"))
    elif case == "repeated_edit":
        edits = [event for event in end_events if event["toolName"] == "edit"]
        tail = end_events[end_events.index(edits[3]) + 1:] if len(edits) >= 4 else []
        result.update(
            guard_triggered=len(edits) >= 4 and "Repeated edit blocked" in edits[3]["result"],
            failed_stimulus_edits=sum(event["isError"] for event in edits[:4]),
            reread_after_failures=any(event["toolName"] == "read" and "TIMEOUT = 45" in event["result"] for event in tail),
            final_file_correct=gate.target.read_bytes() == EXPECTED,
        )
        result["passed"] = result["final_file_correct"] and result["reread_after_failures"]
    else:
        expected = {"approve": EXPECTED, "deny": BEFORE, "stale": EXTERNAL}[mode]
        result.update(previews=gate.previews, final_content=gate.target.read_text(encoding="utf-8"))
        result["passed"] = (
            gate.previews == [EXPECTED_DIFF]
            and gate.target.read_bytes() == expected and len(end_events) == 1
            and end_events[0]["isError"] == (mode != "approve")
            and end_events[0]["result"].startswith({
                "approve": "Successfully applied",
                "deny": "User declined",
                "stale": "File changed after the preview",
            }[mode])
        )
    return result


async def trial(root: Path, case: str, mode: str, live: Config | None, legacy: ModuleType) -> dict[str, Any]:
    workspace = root / "workspace"
    logs = root / "agent"
    log_dir = root / "bash-output"
    workspace.mkdir(parents=True)
    log_dir.mkdir()
    if case == "long_log":
        (workspace / "check_fixture.py").write_text(
            "import time\nprint('status: waiting\\n' * 299, end='')\n"
            f"print({MARKER!r})\nprint('status: waiting\\n' * 4000, end='')\ntime.sleep(10)\n",
            encoding="utf-8",
        )
    else:
        (workspace / SETTINGS).write_bytes(BEFORE)
    gate = FixtureGate(workspace, log_dir, case, mode if case == "review" else None)
    llm = FixtureLLM(case, None if case == "review" else live)
    fixture_agent = partial(
        CodingAgent, system_prompt="Use the supplied native tools to diagnose the isolated synthetic fixture.",
        auto_load_mcp=False, skill_dirs=[], subagent_dirs=[], extension_dirs=[], plugin_dirs=[],
        memory_dir=False, task_store=False, hooks=[(ToolCallHook, gate.restrict_file_tools)],
    )
    answer = ""
    error = None
    evaluation_stop = "agent_end"
    try:
        with (patch.object(tempfile, "tempdir", str(log_dir)),
              patch.dict(os.environ, {"MY_AGENT_HOME": str(root / "home")}),
              patch("my_coding_agent.headless.CodingAgent", fixture_agent)):
            if mode == "baseline" and case == "long_log":
                replacement = patch.object(tools, "make_bash_tool", legacy.make_bash_tool)
            elif mode == "baseline" and case == "repeated_edit":
                replacement = patch.object(importlib.import_module("my_coding_agent.tools.edit"), "CancellationToken", UntrackedRun)
            else:
                replacement = nullcontext()
            with replacement:
                answer = await asyncio.wait_for(run_native(
                    instruction=PROMPTS[case], workspace=workspace, logs_dir=logs, llm=llm,
                    permission_gate=gate, verification_command=COMMAND if case == "long_log" else None,
                ), timeout=80)
    except FixtureRequestLimit:
        evaluation_stop = "request_limit"
    except Exception as exc:
        error = type(exc).__name__
    try:
        result = collect_result(case, mode, gate, logs, answer)
    except (KeyError, OSError, ValueError) as exc:
        error = error or type(exc).__name__
        result = {"passed": False}
    if error is not None:
        evaluation_stop = "runner_error"
        result["runner_error"] = error
    result["evaluation_stop"] = evaluation_stop
    result["passed"] = result["passed"] and evaluation_stop == "agent_end" and result.get("stop_reason") == "end_turn"
    result["provider_stream_attempts"] = llm.provider_attempts
    write_json(root / "result.json", result)
    return result


async def run_suite(output: Path, trials: int, model: Config | None) -> int:
    legacy, legacy_sha = fixture_baseline()
    output.mkdir(parents=True, exist_ok=False)
    report: dict[str, Any] = {
        "model": model.model if model else "offline-scripted",
        "provider": model.provider if model else "offline",
        "trials_per_arm": trials,
        "bash_baseline": {"revision": BASH_BASELINE, "path": BASH_PATH, "sha256": legacy_sha},
        "edit_baseline": "Current native edit with per-run repeat tracking disabled by the evaluation harness.",
        "stimuli": {"long_log": "1 fixed Bash call", "repeated_edit": "4 identical invalid edits", "review": "fixed edit and review response"},
        "source_sha256": {path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest() for path in (
            BASH_PATH, "src/my_coding_agent/tools/edit.py", "src/my_coding_agent/headless.py",
        )},
        "runs": [],
    }
    runs = report["runs"]
    for case in ("long_log", "repeated_edit", "review"):
        modes = ("approve", "deny", "stale") if case == "review" else ("baseline", "current")
        for attempt in range(1, (1 if case == "review" else trials) + 1):
            for mode in (modes[::-1] if case != "review" and attempt % 2 == 0 else modes):
                relative = Path(case) / mode / f"trial-{attempt}"
                result = await trial(output / relative, case, mode, model, legacy)
                runs.append({"case": case, "mode": mode, "trial": attempt, "artifact": relative.as_posix(), **result})
                write_json(output / "summary.json", report)
    checks = []
    for run in runs:
        case, mode = run["case"], run["mode"]
        ended = run.get("stop_reason") == "end_turn" or (
            mode == "baseline" and run.get("evaluation_stop") == "request_limit"
        )
        if not ended or run.get("runner_error"):
            checks.append(False)
        elif case == "long_log":
            checks.append(run.get("timed_out") and run.get("executed_commands") == 1 and (
                run["passed"] if mode == "current" else not run.get("has_saved_log") and not run["passed"]
            ))
        elif case == "repeated_edit":
            checks.append(run.get("failed_stimulus_edits") == 4 and run.get("guard_triggered") == (mode == "current")
                          and (run["passed"] or mode == "baseline"))
        else:
            checks.append(run["passed"])
    report["checks_passed"] = all(checks)
    write_json(output / "summary.json", report)
    print(f"{len(runs)} runs, checks_passed={report['checks_passed']}. Report: {output / 'summary.json'}")
    return 0 if report["checks_passed"] else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="Fresh local results directory")
    parser.add_argument("--trials", type=int, default=1, help="Number of paired trials per scenario")
    parser.add_argument("--model", help="Optional provider/model for recovery turns, e.g. deepseek/deepseek-flash")
    args = parser.parse_args()
    if args.trials < 1:
        parser.error("--trials must be positive")
    model = None
    if args.model:
        model = model_config(args.model).model_copy(
            update={"timeout": 20, "max_tokens": 1200, "temperature": 0.0, "max_retries": 0},
        )
        os.environ.pop(PROVIDER_ENV[model.provider][0], None)
    return asyncio.run(run_suite(args.output.resolve(), args.trials, model))


if __name__ == "__main__":
    raise SystemExit(main())
