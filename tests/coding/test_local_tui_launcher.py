"""Exercise local credential reuse across separate launcher processes, offline."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest

from my_agent_llm.auth.manager import AuthManager


REPO_ROOT = Path(__file__).resolve().parents[2]
PWSH = shutil.which("pwsh") or str(
    Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/native/powershell/pwsh.exe"
)
pytestmark = pytest.mark.skipif(os.name != "nt" or not Path(PWSH).is_file(), reason="Requires PowerShell 7 on Windows")


def run_launcher(home: Path, *, key: str = "", reset: bool = False, smoke: bool = False) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env.pop("DEEPSEEK_API_KEY", None)
    env.update(
        MY_AGENT_HOME=str(home),
        TEST_LAUNCHER=str(REPO_ROOT / "scripts/start-local-tui.ps1"),
        TEST_INPUT_KEY=key,
        TEST_RESET="1" if reset else "0",
        TEST_SMOKE="1" if smoke else "0",
        PYTHONIOENCODING="utf-8",
    )
    command = r'''
$ErrorActionPreference = "Stop"
function global:TestNode {
    if ($args[0] -eq "--version") { "v24.22.1" } else { "TUI_STARTED" }
    $global:LASTEXITCODE = 0
}
function global:Read-Host {
    param([string]$Prompt, [switch]$AsSecureString)
    if (-not $env:TEST_INPUT_KEY) { throw "Unexpected credential prompt" }
    ConvertTo-SecureString $env:TEST_INPUT_KEY -AsPlainText -Force
}
$launchArguments = @{ NodeExecutable = "TestNode" }
if ($env:TEST_RESET -eq "1") { $launchArguments.ResetApiKey = $true }
if ($env:TEST_SMOKE -eq "1") { $launchArguments.UiSmoke = $true }
& $env:TEST_LAUNCHER @launchArguments
if ($env:DEEPSEEK_API_KEY) { throw "Launcher leaked the prompted key to its parent environment" }
'''
    result = subprocess.run(
        [PWSH, "-NoProfile", "-Command", command],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "TUI_STARTED" in result.stdout
    if key:
        assert key not in result.stdout + result.stderr
    return result


def test_first_launch_saves_key_and_next_process_skips_prompt(tmp_path: Path) -> None:
    home = tmp_path / "agent-home"
    fake_key = "sk-test-local-launcher-not-a-real-key"
    run_launcher(home, key=fake_key)
    assert AuthManager(auth_path=home / "auth.json").get_credential("deepseek").resolve_key() == fake_key

    # No input is supplied: this separate process must reuse the saved credential.
    run_launcher(home)


def test_reset_replaces_active_key_and_preserves_other_credentials(tmp_path: Path) -> None:
    home = tmp_path / "agent-home"
    manager = AuthManager(auth_path=home / "auth.json")
    manager.set_api_key("deepseek", "sk-test-old-key-not-a-real-key", profile="work")
    manager.set_api_key("openai", "sk-test-other-provider-not-a-real-key")

    fake_key = "sk-test-replacement-not-a-real-key"
    run_launcher(home, key=fake_key, reset=True)
    assert manager.load_store().active_profiles["deepseek"] == "work"
    assert manager.get_credential("deepseek").resolve_key() == fake_key
    assert manager.get_credential("openai").resolve_key() == "sk-test-other-provider-not-a-real-key"


def test_ui_smoke_never_prompts_or_saves_credentials(tmp_path: Path) -> None:
    home = tmp_path / "agent-home"
    run_launcher(home, smoke=True, reset=True)
    assert not (home / "auth.json").exists()
