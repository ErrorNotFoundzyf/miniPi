from __future__ import annotations

import asyncio
import json
import tarfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

pytest.importorskip("harbor", reason="Run installed-adapter tests in the separate Harbor environment")

from my_pi_eval.installed import MyPiInstalledAgent, create_source_bundle


def test_bundle_contains_only_native_source_and_lock(tmp_path):
    source = tmp_path / "repo"
    source.mkdir()
    for name in ("pyproject.toml", "uv.lock", "README.md"):
        (source / name).write_text(name)
    for package in ("my_agent_core", "my_agent_llm", "my_coding_agent"):
        directory = source / "src" / package
        directory.mkdir(parents=True)
        (directory / "__init__.py").write_text("")
    (source / ".env").write_text("PRIVATE=do-not-bundle")
    (source / "solution.txt").write_text("do-not-bundle")
    first = tmp_path / "first.tar.gz"
    second = tmp_path / "second.tar.gz"
    assert create_source_bundle(source, first) == create_source_bundle(source, second)
    with tarfile.open(first) as archive:
        names = archive.getnames()
    assert "uv.lock" in names
    assert "src/my_coding_agent/__init__.py" in names
    assert ".env" not in names
    assert "solution.txt" not in names
    assert not any(name.startswith("/") or ".." in Path(name).parts for name in names)


def test_adapter_starts_native_process_without_bridged_tools(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-test-key")
    adapter = MyPiInstalledAgent(logs_dir=tmp_path / "logs", model_name="deepseek/deepseek-chat")
    environment = SimpleNamespace(upload_file=AsyncMock(), exec=AsyncMock(), default_user=None)
    adapter.exec_as_agent = AsyncMock()
    asyncio.run(adapter.run("Quoted task: 'hello'\nsecond line", environment, SimpleNamespace()))
    command = adapter.exec_as_agent.call_args.args[1]
    assert "-m my_coding_agent.headless" in command
    assert "--model deepseek/deepseek-chat" in command
    assert "Quoted task" not in command
    assert "offline-test-key" not in command
    assert "HarborToolRegistry" not in command
    assert adapter.exec_as_agent.call_args.kwargs["env"]["DEEPSEEK_API_KEY"] == "offline-test-key"


def test_install_uses_locked_isolated_runtime_and_records_source(tmp_path):
    source = tmp_path / "repo"
    source.mkdir()
    for name in ("pyproject.toml", "uv.lock", "README.md"):
        (source / name).write_text(name)
    for package in ("my_agent_core", "my_agent_llm", "my_coding_agent"):
        directory = source / "src" / package
        directory.mkdir(parents=True)
        (directory / "__init__.py").write_text("")
    logs = tmp_path / "logs"
    adapter = MyPiInstalledAgent(logs_dir=logs, source_root=source)
    adapter.exec_as_root = AsyncMock()
    environment = SimpleNamespace(upload_file=AsyncMock(), default_user=None)
    asyncio.run(adapter.install(environment))
    install_command = adapter.exec_as_root.call_args_list[1].args[1]
    assert "--frozen --no-dev --no-editable --python 3.12" in install_command
    assert "export PATH" not in install_command
    assert "source.tar.gz" in environment.upload_file.call_args.args[1]
    manifest = json.loads((logs / "source-manifest.json").read_text())
    assert len(manifest["source_sha256"]) == 64
    assert manifest["mode"] == "installed-native"
