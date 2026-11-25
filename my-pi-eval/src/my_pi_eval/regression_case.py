"""Versioned, reviewed inputs for scripted and real-model regression cases."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


PORTABLE_PART = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*\Z")
CASE_ID = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"


def fixture_path(value: str) -> str:
    for part in value.split("/"):
        stem = part.split(".")[0].upper()
        if (not PORTABLE_PART.fullmatch(part) or part.endswith(".")
                or stem in {"CON", "PRN", "AUX", "NUL"}
                or re.fullmatch(r"(?:COM|LPT)[0-9]", stem)
                or re.search(r"(?:^|[._-])(?:credentials?|secrets?|api[_-]?keys?|auth)(?:[._-]|$)", part, re.I)):
            raise ValueError(f"Unsupported fixture path: {value!r}")
    return value


class CaseModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Origin(CaseModel):
    kind: Literal["synthetic", "session", "incident"]
    session_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    user_message: int | None = Field(default=None, ge=1)
    reference: str | None = Field(default=None, min_length=1)
    fix_commit: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")

    @model_validator(mode="after")
    def has_provenance(self) -> Origin:
        if self.kind == "session" and (self.session_sha256 is None or self.user_message is None):
            raise ValueError("Session cases must identify the source digest and user message")
        if self.kind == "incident" and (not self.reference or not self.fix_commit):
            raise ValueError("Incident cases must identify a documented source and fix commit")
        return self


class Step(CaseModel):
    tool: Literal["read", "write", "edit", "verify"]
    args: dict[str, Any] = Field(default_factory=dict)


class Script(CaseModel):
    id: str = Field(pattern=CASE_ID)
    steps: list[Step] = Field(min_length=1, max_length=40)
    verification_status: Literal["passed", "failed", "stale", "not_run"] | None = None
    cancel_within_ms: int | None = Field(default=None, ge=100, le=5000)


class Case(CaseModel):
    version: Literal[1]
    id: str = Field(pattern=CASE_ID)
    origin: Origin
    draft: bool = False
    timeout_sec: int = Field(default=90, ge=5, le=600)
    verify_script: str | None = None
    verify_pipe_to_cat: bool = False
    verify_output_contains: list[str] = Field(default_factory=list)
    live_verification_status: Literal["passed", "failed"] = "passed"
    expected_files: dict[str, str] = Field(default_factory=dict)
    preserve_files: list[str] = Field(default_factory=list)
    checkpoint_files: list[str] = Field(default_factory=list)
    scripts: list[Script] = Field(default_factory=list)
    live: bool = False

    @model_validator(mode="after")
    def validate_contract(self) -> Case:
        for name in [*self.expected_files, *self.preserve_files]:
            fixture_path(name)
        if self.verify_script is not None:
            fixture_path(self.verify_script)
            if not self.verify_script.endswith(".py") or self.verify_script not in self.preserve_files:
                raise ValueError("verify_script must name a protected Python file in preserve_files")
        if not self.verify_script and (self.verify_pipe_to_cat or self.verify_output_contains or self.live_verification_status != "passed"):
            raise ValueError("Pipeline, output, and verification status expectations require verify_script")
        if any(not fragment.strip() for fragment in self.verify_output_contains):
            raise ValueError("Verification output fragments must not be blank")
        if self.checkpoint_files and (self.verify_script is None or not set(self.checkpoint_files) <= self.expected_files.keys()):
            raise ValueError("Checkpoint files require verify_script and must appear in expected_files")
        protected = {name.casefold() for name in self.preserve_files}
        if protected.intersection(name.casefold() for name in self.expected_files):
            raise ValueError("A file cannot be both writable and protected")
        if len({script.id for script in self.scripts}) != len(self.scripts):
            raise ValueError("Script IDs must be unique")
        if not self.draft and (not (self.scripts or self.live) or
                               (not self.expected_files and (self.live or not all(s.cancel_within_ms for s in self.scripts)))):
            raise ValueError("A runnable case needs expected_files or an offline-only cancellation probe")
        for script in self.scripts:
            if script.cancel_within_ms is not None:
                if (self.verify_script is None or self.verify_pipe_to_cat or self.live or script.verification_status is not None
                        or len(script.steps) != 1 or script.steps[0].tool != "verify"):
                    raise ValueError("Cancellation probes require a single offline foreground verify step")
            if script.verification_status is not None and self.verify_script is None:
                raise ValueError("A verification status requires verify_script")
            for step in script.steps:
                if step.tool == "verify":
                    if step.args or self.verify_script is None:
                        raise ValueError("verify steps take no args and require verify_script")
                    continue
                path = step.args.get("path")
                if not isinstance(path, str):
                    raise ValueError(f"A {step.tool} step needs a workspace-relative path")
                fixture_path(path)
                if step.tool in {"write", "edit"} and path not in self.expected_files:
                    raise ValueError(f"Scripted writes require a declared expected file: {path}")
        return self


def files_in(root: Path) -> list[Path]:
    if not root.is_dir() or root.is_symlink() or root.resolve() != root.absolute():
        raise ValueError(f"Expected a regular fixture directory: {root}")
    files: list[Path] = []
    for path in sorted(root.rglob("*")):
        fixture_path(path.relative_to(root).as_posix())
        if path.is_symlink() or path.resolve() != path.absolute():
            raise ValueError(f"Links are not allowed in a fixture: {path.relative_to(root)}")
        if path.is_file():
            files.append(path)
        elif not path.is_dir():
            raise ValueError(f"Unsupported fixture entry: {path.relative_to(root)}")
    return files


def digest_files(root: Path, files: list[Path], *, replacements: dict[str, bytes] | None = None) -> str:
    digest = hashlib.sha256()
    for path in sorted(files):
        name = path.relative_to(root).as_posix()
        content = replacements[name] if replacements and name in replacements else path.read_bytes()
        digest.update(name.encode() + b"\0" + hashlib.sha256(content).digest())
    return digest.hexdigest()


def load_case(root: Path) -> tuple[Case, str]:
    case_file, instruction = root / "case.json", root / "instruction.md"
    for path in (case_file, instruction):
        if path.resolve() != path.absolute() or not path.is_file():
            raise ValueError(f"Expected a regular case file: {path}")
    case = Case.model_validate_json(case_file.read_bytes())
    if case.draft:
        raise ValueError(f"Review and complete the draft before running: {case.id}")
    if case.id != root.name or not instruction.read_text(encoding="utf-8").strip():
        raise ValueError("Case ID must match its folder and instruction.md must not be empty")
    workspace = root / "workspace"
    files = files_in(workspace)
    if any(script.cancel_within_ms is not None for script in case.scripts):
        if any((workspace / name).exists() for name in ("ready.txt", "finished.txt")):
            raise ValueError("Cancellation marker files must not exist in the initial workspace")
    for name in case.preserve_files:
        if workspace / name not in files:
            raise ValueError(f"Missing protected fixture file: {name}")
    return case, digest_files(root, [case_file, instruction, *files])


def write_json(path: Path, payload: Any) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    temporary.replace(path)


def capture_draft(session: Path, user_message: int, instruction: Path, destination: Path) -> None:
    raw = session.read_bytes()
    users = 0
    for line in raw.splitlines():
        entry = json.loads(line)
        if not isinstance(entry, dict):
            raise ValueError("Expected a native session JSONL, with one entry object per line")
        message = entry.get("message")
        if entry.get("type") == "message" and isinstance(message, dict) and message.get("role") == "user":
            users += 1
    if not 1 <= user_message <= users:
        raise ValueError(f"User message {user_message} is outside the session's {users} user entries")
    sanitized = instruction.read_text(encoding="utf-8")
    if not sanitized.strip() or instruction.resolve() == session.resolve():
        raise ValueError("Provide a separate, reviewed instruction file")
    case = Case(
        version=1, id=destination.name, draft=True,
        origin=Origin(kind="session", session_sha256=hashlib.sha256(raw).hexdigest(), user_message=user_message),
    )
    destination.mkdir(parents=True, exist_ok=False)
    (destination / "workspace").mkdir()
    (destination / "instruction.md").write_text(sanitized, encoding="utf-8", newline="\n")
    write_json(destination / "case.json", case.model_dump())
