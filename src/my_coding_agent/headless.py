"""Non-interactive entry point using the unmodified CodingAgent and native tools."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from typing import Any

from my_agent_core.session import Session
from my_agent_llm.client import LLM
from my_agent_llm.config import Config
from my_coding_agent.agent import CodingAgent
from my_coding_agent.permissions import PermissionGate
from my_coding_agent.run_report import RunReport, VerificationState
from my_coding_agent.tracer import DebugEventTracer


PROVIDER_ENV = {
    "deepseek": ("DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
    "openai": ("OPENAI_API_KEY", "OPENAI_BASE_URL", None),
    "anthropic": ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", None),
}


def model_config(model: str) -> Config:
    """Resolve an explicit provider/model route without reading personal auth files."""
    provider, separator, name = model.partition("/")
    if not separator or not name or provider not in PROVIDER_ENV:
        raise ValueError("Use provider/model with provider deepseek, openai, or anthropic")
    key_name, url_name, default_url = PROVIDER_ENV[provider]
    api_key = os.environ.get(key_name)
    if not api_key:
        raise ValueError(f"Missing environment variable: {key_name}")
    return Config(provider=provider, model=name, api_key=api_key,
                  base_url=os.environ.get(url_name) or default_url)


async def run_native(
    *,
    instruction: str,
    workspace: Path,
    logs_dir: Path,
    llm: Any,
    verification_command: str | None = None,
    permission_gate: PermissionGate | None = None,
    agent_options: dict[str, Any] | None = None,
) -> str:
    """Run one fresh attempt; never decide the benchmark's reward here."""
    logs_dir.mkdir(parents=True, exist_ok=True)
    session_path = logs_dir / "session.jsonl"
    if session_path.exists():
        raise FileExistsError("Use a fresh logs directory for each attempt")
    session = Session(path=session_path, cwd=str(workspace))
    agent = CodingAgent(
        workspace=workspace, llm=llm, session=session,
        permission_gate=permission_gate or PermissionGate(mode="autonomous"),
        **(agent_options or {}),
    )
    tracer = DebugEventTracer(log_path=logs_dir / "debug.log", events_path=logs_dir / "events.jsonl")
    unsubscribe = agent.subscribe(tracer)
    report = RunReport(agent.registry, VerificationState(command=verification_command))
    error_type = None
    try:
        async with report.capture(agent.run_stream(instruction)) as stream:
            async for event in stream:
                report.observe(event)
        if report.stop_reason != "end_turn":
            raise RuntimeError(f"Agent stopped with reason: {report.stop_reason}")
        (logs_dir / "answer.txt").write_text(report.final_text, encoding="utf-8")
        return report.final_text
    except BaseException as exc:
        error_type = type(exc).__name__
        if isinstance(exc, asyncio.CancelledError):
            report.stop_reason = "cancelled"
        raise
    finally:
        try:
            await agent.background_runner.cancel_all()
            await agent.close_mcp()
        finally:
            unsubscribe()
            tracer.close()
            metrics = {
                "model": getattr(getattr(llm, "config", None), "model", None),
                "provider": getattr(getattr(llm, "config", None), "provider", None),
                **report.snapshot(),
                "error_type": error_type,
                "message_usage": report.message_usage,
                "usage_scope": "completed assistant messages; excludes separate summary calls and unreported requests",
                "tools": {tool.name: tool.func.__module__ for tool in agent.registry.list()},
            }
            (logs_dir / "metrics.json").write_text(json.dumps(metrics, indent=2), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True, help="provider/model")
    parser.add_argument("--instruction-file", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--logs-dir", type=Path, required=True)
    parser.add_argument("--verify-command", help="Track this exact command when the agent runs it in foreground Bash.")
    args = parser.parse_args(argv)
    workspace = args.workspace.resolve(strict=True)
    instruction = args.instruction_file.read_text(encoding="utf-8")
    logs_dir = args.logs_dir.resolve()
    os.environ.setdefault("MY_AGENT_HOME", str(logs_dir / "home"))
    os.chdir(workspace)
    llm = LLM(config=model_config(args.model))
    answer = asyncio.run(run_native(
        instruction=instruction, workspace=workspace, logs_dir=logs_dir, llm=llm,
        verification_command=args.verify_command,
    ))
    print(answer)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
