from __future__ import annotations

import asyncio
import contextlib
import inspect
import os
import shutil
import signal
import sys
import tempfile
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any, BinaryIO

from my_agent_core.background import BackgroundRunner
from my_agent_core.tools import Tool, tool

from my_coding_agent.tools.base import (
    DEFAULT_MAX_BYTES,
    DEFAULT_MAX_LINES,
    StringCompatibleToolResult,
    wrap_tool_executor,
)

BLOCKED_COMMANDS = {
    "rm -rf /",
    "rm -rf /*",
    "mkfs",
    "dd if=/dev/zero",
    ":(){ :|:& };:",
    "shutdown",
    "reboot",
    "init 0",
}


def _kill_process_tree(pid: int) -> None:
    """递归杀死指定 PID 的进程树，兼容 Windows 与 POSIX。"""
    if sys.platform == "win32":
        import subprocess

        with contextlib.suppress(Exception):
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
                check=False,
                timeout=5,
            )
    else:
        try:
            pgid = os.getpgid(pid)
            os.killpg(pgid, signal.SIGKILL)
        except Exception:
            with contextlib.suppress(Exception):
                os.kill(pid, signal.SIGKILL)


class BashResult(StringCompatibleToolResult):
    """Bash 工具执行结果：继承 StringCompatibleToolResult。"""


def _resolve_shell() -> tuple[str, bool]:
    """解析当前环境最合适的 Shell 执行器。

    Windows 下对齐 Pi 原厂策略：优先探测 Git Bash，获得一致的 Linux 工具链 (ls/find/head/grep/cat)；
    若未安装则降级为系统默认 cmd.exe。
    返回 (shell_path, is_bash)。
    """
    if sys.platform == "win32":
        # 1. 优先检查用户自定义环境变量
        custom = os.environ.get("PI_BASH_PATH") or os.environ.get("SHELL_PATH")
        if custom and os.path.exists(custom):
            return custom, True

        # 2. 探查 Git Bash 常见安装路径
        candidates = [
            r"D:\gitbash\Git\bin\bash.exe",
            os.path.expandvars(r"%ProgramFiles%\Git\bin\bash.exe"),
            os.path.expandvars(r"%ProgramFiles(x86)%\Git\bin\bash.exe"),
            os.path.expandvars(r"%LocalAppData%\Programs\Git\bin\bash.exe"),
        ]
        for c in candidates:
            if c and os.path.exists(c):
                return c, True

        # 3. 探查 PATH 上的 bash
        found = shutil.which("bash.exe") or shutil.which("bash")
        if found:
            norm = found.replace("/", "\\").lower()
            if not norm.endswith(r"\windows\system32\bash.exe"):
                return found, True

        return "cmd.exe", False

    # POSIX 平台
    found = shutil.which("bash") or "/bin/sh"
    return found, True


def _decode_stream_bytes(data: bytes) -> str:
    """健壮解码子进程字节流，优先 UTF-8，自动防御性回退本地 OEM/GBK 编码，消灭乱码。"""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    try:
        import locale

        enc = locale.getpreferredencoding(False) or "gbk"
        return data.decode(enc, errors="replace")
    except Exception:
        return data.decode("utf-8", errors="replace")


class _BashOutput:
    """Keep a bounded tail in memory and spool the full output on overflow."""

    def __init__(self) -> None:
        self._tail = b""
        self._total_bytes = 0
        self._total_lines = 0
        self._last_char = ""
        self._log: BinaryIO | None = None
        self._keep_log = False

    def _start_log(self) -> BinaryIO:
        log = tempfile.NamedTemporaryFile(prefix="pi-bash-", suffix=".log", delete=False)
        self._log = log
        log.write(self._tail)
        return log

    def append(self, text: str) -> None:
        chunk = text.encode("utf-8")
        self._total_bytes += len(chunk)
        # A partial line or a CRLF may span two reads from the pipe.
        self._total_lines += len((self._last_char + text).splitlines()) - len(self._last_char.splitlines())
        self._last_char = text[-1:]

        log = self._log
        if log is None and (self._total_bytes > DEFAULT_MAX_BYTES or self._total_lines > DEFAULT_MAX_LINES):
            log = self._start_log()
        if log is not None:
            log.write(chunk)
            log.flush()
        self._tail = (self._tail + chunk)[-DEFAULT_MAX_BYTES:]

    def result_text(self, status: str = "") -> str:
        if not self._tail:
            return status

        prefix = f"{status}\n" if status else ""
        prefix_lines = len(prefix.splitlines())
        log = self._log
        if log is None:
            if len(prefix.encode("utf-8")) + self._total_bytes <= DEFAULT_MAX_BYTES and (
                prefix_lines + self._total_lines <= DEFAULT_MAX_LINES
            ):
                return prefix + self._tail.decode("utf-8")
            log = self._start_log()

        notice = f"[Output truncated: showing last {{}} lines of {self._total_lines}. Full output saved to: {log.name}]\n\n"
        max_bytes = DEFAULT_MAX_BYTES - len((prefix + notice.format(DEFAULT_MAX_LINES)).encode("utf-8"))
        max_lines = DEFAULT_MAX_LINES - prefix_lines - 2
        tail = self._tail[-max_bytes:] if max_bytes > 0 and max_lines > 0 else b""
        if len(tail) < self._total_bytes:
            first_newline = tail.find(b"\n")
            if 0 <= first_newline < len(tail) - 1:
                tail = tail[first_newline + 1 :]
        lines = tail.decode("utf-8", errors="ignore").splitlines()[-max_lines:] if tail else []
        log.close()
        self._keep_log = True
        return prefix + notice.format(len(lines)) + "\n".join(lines)

    def close(self) -> None:
        if self._log is not None:
            with contextlib.suppress(OSError):
                self._log.close()
            if not self._keep_log:
                with contextlib.suppress(OSError):
                    Path(self._log.name).unlink(missing_ok=True)


def make_bash_tool(
    workspace: Path | str,
    background_runner: BackgroundRunner | None = None,
) -> Tool:
    """创建 bash 工具工厂，绑定 workspace 目录并支持超时终结、日志外溢与后台运行。"""
    workspace = Path(workspace).resolve()

    @tool(
        name="bash",
        description="Execute a bash command in the workspace (POSIX / Git Bash syntax; do not use cmd.exe syntax like 'dir' or 'cd /d'). Returns stdout and stderr.",
        prompt_snippet="Execute bash commands (ls, grep, find, etc.)",
        prompt_guidelines=["You can inspect PI_* environment variables for current model and session details."],
        is_parallel_safe=False,
    )
    async def bash(
        command: str,
        timeout: int = 120,
        run_in_background: bool = False,
        on_update: Callable[[Any], None] | None = None,
        signal: Any | None = None,
    ) -> Any:
        def _is_cancelled() -> bool:
            if signal is None:
                return False
            check = getattr(signal, "is_cancelled", None)
            if callable(check):
                return bool(check())
            return bool(getattr(signal, "cancelled", False))

        if _is_cancelled():
            return BashResult(
                ok=False,
                data="Tool call interrupted by user",
                error="Tool call interrupted by user",
            )

        for blocked in BLOCKED_COMMANDS:
            if blocked in command:
                msg = f"Error: Blocked dangerous command pattern '{blocked}'."
                return BashResult(ok=False, data=msg, error=msg)

        if run_in_background:
            runner = background_runner() if callable(background_runner) else background_runner
            if runner is None:
                msg = "Error: Background task execution not configured on this agent."
                return BashResult(ok=False, data=msg, error=msg)
            res = runner.run_process(command, cwd=workspace)
            task_id = await res if inspect.isawaitable(res) else res
            return BashResult(
                ok=True,
                data=f"Background task started with ID: {task_id}",
                error=None,
            )

        try:
            kwargs: dict[str, Any] = {
                "cwd": str(workspace),
                "stdout": asyncio.subprocess.PIPE,
                "stderr": asyncio.subprocess.STDOUT,
                "stdin": asyncio.subprocess.DEVNULL,
                "limit": 64 * 1024,
            }
            if sys.platform != "win32":
                kwargs["preexec_fn"] = os.setsid

            shell_path, is_bash = _resolve_shell()
            env = dict(os.environ)
            if is_bash:
                env.setdefault("LANG", "C.UTF-8")
                env.setdefault("LC_ALL", "C.UTF-8")
                resolved_cmd = f"set -o pipefail\n{command}"
                proc = await asyncio.create_subprocess_exec(
                    shell_path,
                    "-c",
                    resolved_cmd,
                    env=env,
                    **kwargs,
                )
            else:
                proc = await asyncio.create_subprocess_shell(command, env=env, **kwargs)

            abort_requested = False
            finished = False
            loop = asyncio.get_running_loop()

            def _abort_proc() -> None:
                nonlocal abort_requested
                if not abort_requested and not finished and proc.pid:
                    abort_requested = True
                    if sys.platform == "win32":
                        loop.run_in_executor(None, _kill_process_tree, proc.pid)
                    else:
                        _kill_process_tree(proc.pid)

            if signal is not None and hasattr(signal, "add_callback"):
                signal.add_callback(lambda: loop.call_soon_threadsafe(_abort_proc))

            output = _BashOutput()
            recent_chunks: deque[str] = deque(maxlen=5)
            last_update_time = loop.time()

            async def _read_until_exit() -> None:
                nonlocal last_update_time
                if proc.stdout is not None:
                    while True:
                        chunk = await proc.stdout.read(64 * 1024)
                        if not chunk:
                            break
                        if _is_cancelled():
                            continue
                        text = _decode_stream_bytes(chunk)
                        output.append(text)
                        if on_update is not None:
                            recent_chunks.append(text)
                            now = loop.time()
                            if now - last_update_time >= 0.1:
                                last_update_time = now
                                tail_preview = "".join(recent_chunks).strip()
                                if tail_preview:
                                    on_update(tail_preview)
                await proc.wait()

            try:
                try:
                    await asyncio.wait_for(_read_until_exit(), timeout=timeout)
                except asyncio.TimeoutError:
                    _abort_proc()
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(proc.wait(), timeout=2.0)
                    command_preview = command[:1024] + ("…" if len(command) > 1024 else "")
                    msg = output.result_text(f"Error: Command timed out after {timeout} seconds: {command_preview}")
                    return BashResult(ok=False, data=msg, error=msg)
                except (Exception, asyncio.CancelledError, GeneratorExit):
                    _abort_proc()
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(proc.wait(), timeout=2.0)
                    raise

                if _is_cancelled():
                    msg = "Tool call interrupted by user"
                    return BashResult(ok=False, data=msg, error=msg)

                exit_code = proc.returncode
                if exit_code != 0:
                    msg = output.result_text(f"Command failed with exit code {exit_code}:")
                    return BashResult(ok=False, data=msg, error=msg)

                return BashResult(ok=True, data=output.result_text() or "(Command executed with no output)")
            finally:
                finished = True
                output.close()
        except Exception as e:
            msg = f"Error: {e}"
            return BashResult(ok=False, data=msg, error=msg)

    return wrap_tool_executor(
        bash,
        aliases={"command": ("cmd", "script")},
    )
