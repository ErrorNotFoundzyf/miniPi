"""Execute regression cases against the native runtime, offline or via Harbor."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import sys
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from types import ModuleType
from typing import Any, Iterator

from my_pi_eval import regression_verify
from my_pi_eval.regression_cancel import collect_cancellation
from my_pi_eval.regression_case import Case, Script, write_json
from my_pi_eval.regression_verify import read_json, sha256_file, verify


SOURCE_ROOT = Path(__file__).resolve().parents[3]
BASH_SOURCE_PATH = "src/my_coding_agent/tools/bash.py"
HISTORICAL_BASH = {
    "pipefail-pre-fix": (
        "601067d60fc510f4a0739894cbf4aeb664cc2e47",
        "a7a8f4d26715402c73c5fd82226b12f5b68d399e386419e83ae15c432df4b6f3",
    ),
    "timeout-output-pre-fix": (
        "a867a9854fc8e41823658784494aa0c2627aed05",
        "16047f16eabf75b7b4d71510d0744de3244410120736f543be96b6a48dff4ce3",
    ),
}
PROVIDER_KEYS = {"deepseek": "DEEPSEEK_API_KEY", "openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}
TASK_DOCKERFILE = """FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates ripgrep && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY workspace/ /app/
"""


@dataclass(frozen=True)
class BashBaseline:
    revision: str
    source: bytes

    @classmethod
    def load(cls, name: str) -> BashBaseline:
        revision, expected = HISTORICAL_BASH[name]
        source = (SOURCE_ROOT / "my-pi-eval" / "baselines" / f"bash-{revision}.py.txt").read_bytes()
        if hashlib.sha256(source).hexdigest() != expected:
            raise ValueError(f"Historical Bash snapshot failed its integrity check: {revision}")
        return cls(revision, source)

    @contextmanager
    def apply(self) -> Iterator[None]:
        from unittest.mock import patch

        from my_coding_agent import tools

        module = ModuleType("my_coding_agent.tools.bash")
        exec(compile(self.source, f"{self.revision}:{BASH_SOURCE_PATH}", "exec"), vars(module))
        with patch.object(tools, "make_bash_tool", module.make_bash_tool):
            yield


class BashUnavailable(RuntimeError):
    """A case needs real Bash rather than the Windows cmd fallback."""


def oracle_for(case: Case, workspace: Path, verification_status: str | None) -> dict[str, Any]:
    return {
        "expected_files": {name: hashlib.sha256(content.encode("utf-8")).hexdigest()
                           for name, content in case.expected_files.items()},
        "preserve_files": {name: sha256_file(workspace / name) for name in case.preserve_files},
        "verification_status": verification_status,
        "checkpoint_files": case.checkpoint_files if verification_status == "passed" else [],
        "verify_output_contains": case.verify_output_contains,
    }


def verification_command(case: Case, *, offline: bool) -> str | None:
    if case.verify_script is None:
        return None
    executable = f'"{Path(sys.executable).as_posix()}"' if offline else "python"
    pipe = " | cat" if case.verify_pipe_to_cat else ""
    return f"{executable} -I -B {case.verify_script}{pipe}"


async def execute_script(
    case: Case, script: Script, workspace: Path, logs: Path, instruction: str,
) -> None:
    from types import SimpleNamespace

    from my_agent_llm.events import StreamDoneEvent, StreamStartEvent, ToolCallDoneEvent
    from my_agent_llm.models import Message, ToolCall
    from my_coding_agent.headless import run_native
    from my_coding_agent.permissions import PermissionGate, PermissionRequest
    from my_coding_agent.tools.bash import _resolve_shell

    if case.verify_pipe_to_cat and not _resolve_shell()[1]:
        raise BashUnavailable("The pipeline fixture requires Bash or Git Bash")
    command = verification_command(case, offline=True)
    checker_digest = sha256_file(workspace / case.verify_script) if case.verify_script else None

    class ScriptedModel:
        config = SimpleNamespace(model="scripted-regression", provider="offline")

        def __init__(self) -> None:
            self.calls = 0

        async def astream_events(self, messages: Any, **kwargs: Any):
            index = self.calls
            self.calls += 1
            call = None
            if index < len(script.steps):
                step = script.steps[index]
                name, args = (("bash", {"command": command, "timeout": case.verify_timeout_sec})
                              if step.tool == "verify" else (step.tool, step.args))
                call = ToolCall(id=f"regression-{index + 1}", name=name, args=args)
            message = Message(
                role="assistant", content="" if call else "Scripted run finished.",
                metadata={"tool_calls": [call]} if call else {},
            )
            yield StreamStartEvent(partial=message)
            if call:
                yield ToolCallDoneEvent(index=0, tool_call=call, partial=message)
            yield StreamDoneEvent(message=message)

    def approve(request: PermissionRequest) -> bool:
        if request.action == "bash":
            if case.verify_script is None or request.target != command or request.details.get("run_in_background"):
                return False
            checker = workspace / case.verify_script
            return checker.resolve() == checker.absolute() and sha256_file(checker) == checker_digest
        target = (workspace / request.target).resolve()
        if not target.is_relative_to(workspace):
            return False
        if request.action == "read":
            return True
        return request.action in {"edit", "write"} and target.relative_to(workspace).as_posix() in case.expected_files

    await asyncio.wait_for(run_native(
        instruction=instruction, workspace=workspace, logs_dir=logs, llm=ScriptedModel(),
        verification_command=command, permission_gate=PermissionGate(mode="strict", confirm_callback=approve),
        agent_options={"system_prompt": "Run the reviewed offline regression fixture.", "auto_load_mcp": False,
                       "skill_dirs": [], "subagent_dirs": [], "extension_dirs": [], "plugin_dirs": [],
                       "memory_dir": False, "task_store": False},
    ), timeout=case.timeout_sec)


def run_offline(
    case: Case, case_dir: Path, script: Script, attempt_dir: Path, baseline: BashBaseline | None = None,
) -> dict[str, Any]:
    started = perf_counter()
    workspace = attempt_dir / "workspace"
    shutil.copytree(case_dir / "workspace", workspace)
    logs = attempt_dir / "agent"
    oracle = oracle_for(case, workspace, script.verification_status)
    if script.cancel_within_ms is not None:
        oracle["cancel_within_ms"] = script.cancel_within_ms
    previous_home = os.environ.get("MY_AGENT_HOME")
    os.environ["MY_AGENT_HOME"] = str(attempt_dir / "home")
    error = None
    try:
        with baseline.apply() if baseline is not None else nullcontext():
            if script.cancel_within_ms is not None:
                command = verification_command(case, offline=True)
                assert command is not None
                asyncio.run(collect_cancellation(workspace, logs, command, case.timeout_sec))
            else:
                asyncio.run(execute_script(case, script, workspace, logs, (case_dir / "instruction.md").read_text(encoding="utf-8")))
    except Exception as exc:
        error = type(exc).__name__
    finally:
        if previous_home is None:
            os.environ.pop("MY_AGENT_HOME", None)
        else:
            os.environ["MY_AGENT_HOME"] = previous_home
    result = verify(workspace, logs, oracle)
    if error is not None:
        result.update(status="blocked", reason=error)
    evidence = {"cancellation": "agent/cancellation.json"} if script.cancel_within_ms is not None else {"agent": "agent"}
    return {**result, "elapsed_sec": round(perf_counter() - started, 3), "evidence": evidence}


def prepare_task(case: Case, case_dir: Path, task: Path) -> None:
    task.mkdir(parents=True)
    environment = task / "environment"
    tests = task / "tests"
    tests.mkdir()
    shutil.copytree(case_dir / "workspace", environment / "workspace")
    shutil.copyfile(case_dir / "instruction.md", task / "instruction.md")
    (environment / "Dockerfile").write_text(TASK_DOCKERFILE, encoding="utf-8", newline="\n")
    (task / "task.toml").write_text(
        'version = "1.0"\n\n[agent]\n'
        f'timeout_sec = {case.timeout_sec}.0\n\n[verifier]\ntimeout_sec = 60.0\n'
        '\n[environment]\nbuild_timeout_sec = 600.0\ncpus = 1\nmemory_mb = 2048\nstorage_mb = 4096\n',
        encoding="utf-8", newline="\n",
    )
    (tests / "test.sh").write_text(
        '#!/bin/bash\nset -eu\n/usr/local/bin/python -I -B /tests/verify.py /tests/oracle.json\n',
        encoding="utf-8", newline="\n",
    )
    shutil.copyfile(Path(regression_verify.__file__), tests / "verify.py")
    expected_status = case.live_verification_status if case.verify_script else None
    write_json(tests / "oracle.json", oracle_for(case, case_dir / "workspace", expected_status))


def harbor_command(harbor: str, task: Path, jobs: Path, model: str, case: Case, source_root: Path) -> list[str]:
    command = [harbor, "run", "-p", str(task), "--agent", "my_pi_eval.installed:MyPiInstalledAgent",
               "-m", model, "--skip-model-parsing-and-provider-mapping",
               "--jobs-dir", str(jobs), "--job-name", "regression", "--max-retries", "0",
               "--agent-kwarg", f"source_root={json.dumps(str(source_root))}"]
    if checked := verification_command(case, offline=False):
        command.extend(["--agent-kwarg", f"verification_command={json.dumps(checked)}"])
    return command


def collect_harbor(attempt_dir: Path, returncode: int) -> dict[str, Any]:
    job = attempt_dir / "jobs" / "regression"
    trials = sorted(job.glob("*/result.json"))
    evidence = {"harbor_log": "harbor.log"}
    if len(trials) != 1:
        return {"status": "blocked", "reason": f"Expected one Harbor trial, found {len(trials)}", "evidence": evidence}
    trial = trials[0]
    for key, path in {"harbor_result": trial, "agent": trial.parent / "agent", "verifier": trial.parent / "verifier"}.items():
        evidence[key] = path.relative_to(attempt_dir).as_posix()
    result = read_json(trial)
    checks = read_json(trial.parent / "verifier" / "checks.json")
    if returncode or result is None or result.get("exception_info") or checks is None:
        return {"status": "blocked", "reason": "Harbor did not deliver a valid verdict", "evidence": evidence}
    reward = ((result.get("verifier_result") or {}).get("rewards") or {}).get("reward")
    status = checks.get("status")
    if status not in {"pass", "fail", "blocked"} or reward != (1 if status == "pass" else 0):
        return {"status": "blocked", "reason": "Harbor reward and verifier verdict disagree", "evidence": evidence}
    return {**checks, "evidence": evidence}


def live_preflight(harbor: str, model: str) -> dict[str, str]:
    provider, separator, name = model.partition("/")
    if not separator or not name.strip() or provider not in PROVIDER_KEYS:
        raise ValueError("Use an explicit deepseek/model, openai/model, or anthropic/model route")
    if not os.environ.get(PROVIDER_KEYS[provider]):
        raise ValueError(f"Missing evaluation credential: {PROVIDER_KEYS[provider]}")
    if shutil.which(harbor) is None:
        raise ValueError("Harbor is unavailable; pass --harbor from the Harbor 0.26 environment")
    try:
        docker = subprocess.run(["docker", "info"], capture_output=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError("Live regression requires an accessible Docker engine") from exc
    if docker.returncode != 0:
        raise ValueError("Live regression requires an accessible Docker engine")
    environment = dict(os.environ)
    for other, key in PROVIDER_KEYS.items():
        if other != provider:
            environment.pop(key, None)
            environment.pop(f"{other.upper()}_BASE_URL", None)
    return environment


def run_live(
    case: Case, task: Path, attempt_dir: Path, harbor: str, model: str, environment: dict[str, str], source_root: Path,
) -> dict[str, Any]:
    started = perf_counter()
    attempt_dir.mkdir(parents=True)
    command = harbor_command(harbor, task, attempt_dir / "jobs", model, case, source_root)
    with (attempt_dir / "harbor.log").open("wb") as log:
        try:
            completed = subprocess.run(command, env=environment, stdout=log, stderr=subprocess.STDOUT, check=False)
            result = collect_harbor(attempt_dir, completed.returncode)
        except OSError as exc:
            result = {"status": "blocked", "reason": type(exc).__name__, "evidence": {"harbor_log": "harbor.log"}}
    return {**result, "elapsed_sec": round(perf_counter() - started, 3)}
