"""Harbor installed adapter: run the repository's CodingAgent inside each task container.

Requires Harbor 0.26.x. The host uploads source and starts the process; it does
not replace any of the native read/write/edit/bash tool implementations.
"""

from __future__ import annotations

import hashlib
import io
import json
import shlex
import tarfile
import tempfile
from pathlib import Path
from typing import Any

from harbor.agents.installed.base import BaseInstalledAgent

REMOTE_ROOT = "/installed-agent/my-pi-native"
REMOTE_LOGS = "/logs/agent"
UV_VERSION = "0.9.29"


def create_source_bundle(source_root: Path, destination: Path) -> str:
    """Archive only package sources and locked packaging files; return its SHA256."""
    files = [source_root / name for name in ("pyproject.toml", "uv.lock", "README.md")]
    for package in ("my_agent_core", "my_agent_llm", "my_coding_agent"):
        package_dir = source_root / "src" / package
        files.extend(path for path in package_dir.rglob("*")
                     if path.is_file() and (path.suffix == ".py" or path.name == "py.typed"))
    digest = hashlib.sha256()
    with tarfile.open(destination, "w:gz") as archive:
        for path in sorted(files):
            if path.is_symlink() or not path.resolve().is_relative_to(source_root.resolve()):
                raise ValueError(f"Source file must remain inside the repository: {path.name}")
            name = path.relative_to(source_root).as_posix()
            data = path.read_bytes()
            digest.update(name.encode("utf-8") + b"\0" + data + b"\0")
            member = tarfile.TarInfo(name)
            member.size = len(data)
            member.mode = 0o644
            archive.addfile(member, io.BytesIO(data))
    return digest.hexdigest()


class MyPiInstalledAgent(BaseInstalledAgent):
    """Install the current working-tree snapshot and execute native tools in-container."""

    def __init__(
        self, *args: Any, source_root: str | Path | None = None,
        verification_command: str | None = None, **kwargs: Any,
    ):
        self.source_root = Path(source_root).resolve() if source_root else Path(__file__).resolve().parents[3]
        self.verification_command = verification_command
        super().__init__(*args, **kwargs)

    @staticmethod
    def name() -> str:
        return "my-pi-agent-native"

    def _parse_model_name(self, model_name: str) -> tuple[str, str]:
        provider, separator, model = model_name.partition("/")
        if not separator or not model or provider not in {"deepseek", "openai", "anthropic"}:
            raise ValueError("Use an explicit deepseek/model, openai/model, or anthropic/model route")
        return provider, model

    async def install(self, environment: Any) -> None:
        await self.exec_as_root(environment, f"mkdir -p {REMOTE_ROOT} {REMOTE_LOGS}")
        with tempfile.TemporaryDirectory(prefix="my-pi-native-") as temp:
            bundle = Path(temp) / "source.tar.gz"
            digest = create_source_bundle(self.source_root, bundle)
            await environment.upload_file(bundle, f"{REMOTE_ROOT}/source.tar.gz")
        manifest = {
            "source_sha256": digest, "uv_version": UV_VERSION,
            "mode": "installed-native", "harbor_contract": "0.26.x",
        }
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        (self.logs_dir / "source-manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        command = f"""set -eu
if ! command -v curl >/dev/null 2>&1; then
    echo 'curl is required in the task image to bootstrap the isolated Agent runtime' >&2
    exit 1
fi
curl -LsSf https://astral.sh/uv/{UV_VERSION}/install.sh -o {REMOTE_ROOT}/install-uv.sh
UV_INSTALL_DIR={REMOTE_ROOT}/uv UV_NO_MODIFY_PATH=1 sh {REMOTE_ROOT}/install-uv.sh
mkdir -p {REMOTE_ROOT}/source
tar -xzf {REMOTE_ROOT}/source.tar.gz -C {REMOTE_ROOT}/source
UV_PYTHON_INSTALL_DIR={REMOTE_ROOT}/python {REMOTE_ROOT}/uv/uv sync --project {REMOTE_ROOT}/source --frozen --no-dev --no-editable --python 3.12
{REMOTE_ROOT}/source/.venv/bin/python --version > {REMOTE_LOGS}/python-version.txt
{REMOTE_ROOT}/uv/uv pip freeze --python {REMOTE_ROOT}/source/.venv/bin/python > {REMOTE_LOGS}/dependencies.txt
"""
        await self.exec_as_root(environment, command, timeout_sec=1200)
        # Native tools keep the task image's PATH; its Python is not replaced by the Agent venv.
        if environment.default_user is not None:
            await self.exec_as_root(
                environment,
                f"chown -R {shlex.quote(str(environment.default_user))} {REMOTE_ROOT} {REMOTE_LOGS}",
            )

    async def run(self, instruction: str, environment: Any, context: Any) -> None:
        model = self.model_name
        if not model:
            raise ValueError("Pass an explicit model with -m provider/model")
        instruction = self.render_instruction(instruction)
        with tempfile.TemporaryDirectory(prefix="my-pi-instruction-") as temp:
            path = Path(temp) / "instruction.txt"
            path.write_text(instruction, encoding="utf-8")
            await environment.upload_file(path, f"{REMOTE_ROOT}/instruction.txt")
        variables = {"MY_AGENT_HOME": f"{REMOTE_LOGS}/home"}
        # Forward explicit eval credentials only; personal auth files are never bundled.
        for name in ("DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "OPENAI_API_KEY", "OPENAI_BASE_URL",
                     "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL"):
            value = self._get_env(name)
            if value:
                variables[name] = value
        command = (
            f"{REMOTE_ROOT}/source/.venv/bin/python -m my_coding_agent.headless "
            f"--model {shlex.quote(model)} --instruction-file {REMOTE_ROOT}/instruction.txt "
            f"--logs-dir {REMOTE_LOGS}"
        )
        if self.verification_command:
            command += f" --verify-command {shlex.quote(self.verification_command)}"
        await self.exec_as_agent(environment, command, env=variables)
