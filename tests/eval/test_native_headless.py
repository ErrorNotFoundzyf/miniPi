from __future__ import annotations

import asyncio
import json
import re
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from my_agent_llm.events import StreamDoneEvent, StreamStartEvent, ToolCallDoneEvent
from my_agent_llm.models import Message, ToolCall
from my_coding_agent.headless import model_config, run_native
from my_coding_agent.permissions import PermissionGate


class NativeWorkflowLLM:
    config = SimpleNamespace(model="offline", provider="openai")

    def __init__(self):
        self.calls = 0

    def next_action(self, step, observed):
        if step == 2:
            assert "old" in observed
        actions = [
            ("write", {"path": "result.txt", "content": "old"}),
            ("read", {"path": "result.txt"}),
            ("edit", {"path": "result.txt", "edits": [{"oldText": "old", "newText": "new"}]}),
            ("bash", {"command": f'"{Path(sys.executable).as_posix()}" -c "print(12345)"'}),
        ]
        # Use an unmistakable numeric marker to avoid shell quoting differences on Windows.
        if step == 4:
            assert "12345" in observed
        return actions[step] if step < len(actions) else None

    async def astream_events(self, messages, **kwargs):
        step = self.calls
        self.calls += 1
        observed = "\n".join(str(m.content) for m in messages if m.role == "tool")
        action = self.next_action(step, observed)
        usage = {"prompt_tokens": 2, "completion_tokens": 1, "cache_read_tokens": 3}
        if action is not None:
            name, args = action
            call = ToolCall(id=f"native-{step}", name=name, args=args)
            message = Message(role="assistant", content="", metadata={"tool_calls": [call], "usage": usage})
            yield StreamStartEvent(partial=message)
            yield ToolCallDoneEvent(index=0, tool_call=call, partial=message)
        else:
            message = Message(role="assistant", content="done", metadata={"usage": usage})
            yield StreamStartEvent(partial=message)
        yield StreamDoneEvent(message=message, usage=usage)


def test_headless_uses_native_tools_and_persists_run(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    logs = tmp_path / "logs"
    monkeypatch.chdir(workspace)
    monkeypatch.setenv("MY_AGENT_HOME", str(tmp_path / "home"))
    llm = NativeWorkflowLLM()
    answer = asyncio.run(run_native(instruction="Perform the offline tool workflow", workspace=workspace,
                                    logs_dir=logs, llm=llm))
    assert answer == "done"
    assert (workspace / "result.txt").read_text() == "new"
    metrics = json.loads((logs / "metrics.json").read_text())
    assert metrics["stop_reason"] == "end_turn"
    assert metrics["iterations"] == 5
    assert metrics["verification"] == {"status": "not_configured", "attempts": []}
    assert metrics["tool_calls"]["attempted"] == metrics["tool_calls"]["succeeded"] == 4
    for name in ("read", "write", "edit", "bash", "grep", "find", "ls"):
        assert metrics["tools"][name] == f"my_coding_agent.tools.{name}"
    assert len(metrics["message_usage"]) == 5
    assert metrics["model_requests"]["conversation"] == {
        "calls": 5,
        "http_attempts_observed": 0,
        "sdk_retry_http_attempts_observed": 0,
        "calls_without_http_observations": 5,
        "calls_without_usage": 0,
        "reported_usage": {"prompt_tokens": 10, "completion_tokens": 5, "cache_read_tokens": 15},
    }
    assert metrics["model_requests"]["retry"]["calls"] == 0
    assert metrics["model_requests"]["summary"]["calls"] == 0
    assert (logs / "session.jsonl").exists()
    assert (logs / "events.jsonl").stat().st_size > 0
    with pytest.raises(FileExistsError):
        asyncio.run(run_native(instruction="repeat", workspace=workspace, logs_dir=logs, llm=llm))


class RepairWorkflowLLM(NativeWorkflowLLM):
    def __init__(self, command: str, *, repair: bool):
        super().__init__()
        self.command = command
        self.repair = repair

    def next_action(self, step, observed):
        if step == 0:
            return "bash", {"command": self.command}
        if step == 1:
            assert "Command failed with exit code 1" in observed
            assert "ERROR: expected new" not in observed
            if not self.repair:
                return None
            location = re.search(r"Full output saved to: (.*?)\]", observed)
            assert location is not None
            return "grep", {"path": location.group(1), "pattern": "ERROR: expected new"}
        if step == 2:
            assert "ERROR: expected new" in observed
            return "bash", {"command": "echo unapproved command"}
        if step == 3:
            return "edit", {"path": "result.txt", "edits": [{"oldText": "old", "newText": "new"}]}
        if step == 4:
            return "bash", {"command": self.command}
        assert "fixture verified" in observed
        return None


@pytest.mark.parametrize("repair", [False, True])
def test_headless_reports_verification_separately_from_stop_reason(tmp_path, monkeypatch, repair):
    monkeypatch.setenv("MY_AGENT_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    target = tmp_path / "result.txt"
    target.write_bytes(b"old\n")
    (tmp_path / "verify.py").write_text(
        'from pathlib import Path\n'
        'if Path("result.txt").read_text().strip() != "new":\n'
        '    print("step\\n" * 299, end="")\n'
        '    print("ERROR: expected new")\n'
        '    print("step\\n" * 2400, end="")\n'
        '    raise SystemExit(1)\n'
        'print("fixture verified")\n',
        encoding="utf-8",
    )
    command = f'"{Path(sys.executable).as_posix()}" verify.py'
    reviewed = []

    def approve_diff(request):
        assert request.action == "edit" and Path(request.target) == target
        assert target.read_bytes() == b"old\n"
        assert "-old\n+new\n" in request.preview
        reviewed.append(request.preview)
        return True

    gate = PermissionGate(
        mode="review",
        confirm_callback=lambda request: request.action == "bash" and request.target == command,
        file_confirm_callback=approve_diff,
    )
    logs = tmp_path / "logs"
    asyncio.run(run_native(
        instruction="Diagnose and verify the synthetic fixture.", workspace=tmp_path, logs_dir=logs,
        llm=RepairWorkflowLLM(command, repair=repair), permission_gate=gate, verification_command=command,
    ))

    metrics = json.loads((logs / "metrics.json").read_text())
    assert metrics["stop_reason"] == "end_turn"
    assert metrics["verification"] == {
        "status": "passed" if repair else "failed",
        "attempts": [{"tool_call_id": "native-0", "status": "failed"}]
        + ([{"tool_call_id": "native-4", "status": "passed"}] if repair else []),
    }
    assert metrics["tool_calls"]["by_tool"]["bash"] == {
        "attempted": 3 if repair else 1, "succeeded": int(repair), "failed": 1,
        "blocked": int(repair), "skipped": 0, "incomplete": 0,
    }
    assert len(reviewed) == int(repair)
    assert target.read_bytes() == (b"new\n" if repair else b"old\n")


def test_headless_rejects_error_end_event(tmp_path, monkeypatch):
    from my_agent_core.events import AgentEnd
    from my_coding_agent.agent import CodingAgent

    async def failed_stream(self, instruction):
        yield AgentEnd(messages=[], final_text=None, iterations=0, stop_reason="error")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MY_AGENT_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(CodingAgent, "run_stream", failed_stream)
    logs = tmp_path / "logs"
    with pytest.raises(RuntimeError, match="error"):
        asyncio.run(run_native(instruction="fail", workspace=tmp_path, logs_dir=logs, llm=NativeWorkflowLLM(),
                               verification_command="pytest"))
    metrics = json.loads((logs / "metrics.json").read_text())
    assert metrics["error_type"] == "RuntimeError"
    assert metrics["verification"] == {"status": "not_run", "attempts": []}
    assert not (logs / "answer.txt").exists()


def test_model_config_requires_explicit_eval_credentials(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with pytest.raises(ValueError, match="DEEPSEEK_API_KEY"):
        model_config("deepseek/deepseek-chat")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-test-key")
    config = model_config("deepseek/deepseek-chat")
    assert config.model == "deepseek-chat"
    assert config.base_url == "https://api.deepseek.com"
    with pytest.raises(ValueError, match="provider/model"):
        model_config("deepseek-chat")
