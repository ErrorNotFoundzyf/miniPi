"""Index fresh run evidence without copying raw traces into replay drafts."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

from my_pi_eval.regression_verify import read_json


TRACE_FILES = ("session.jsonl", "events.jsonl", "metrics.json", "answer.txt", "cancellation.json")
TRACE_EVENTS = {"tool_execution_start", "tool_execution_end", "auto_retry_start", "auto_retry_end"}


def local_entry(root: Path, relative: Any) -> Path:
    if (not isinstance(relative, str) or not relative or "\\" in relative or ":" in relative
            or any(part in {"", ".", ".."} for part in relative.split("/"))):
        raise ValueError("Evidence paths must stay inside the attempt directory")
    path = root / relative
    if path.resolve() != path.absolute() or not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("Evidence paths must not traverse links")
    return path


def fingerprint(path: Path) -> dict[str, Any]:
    with path.open("rb") as stream:
        size = path.stat().st_size
        return {"sha256": hashlib.file_digest(stream, "sha256").hexdigest(), "size_bytes": size}


def index_attempt(attempt: Path, scenario: str, repeat: int, result: dict[str, Any]) -> dict[str, Any]:
    references = result.get("evidence", {})
    directory = local_entry(attempt, references.get("agent", "agent"))
    files = {}
    for name in TRACE_FILES:
        source = local_entry(attempt, (directory / name).relative_to(attempt).as_posix())
        if source.is_file():
            files[name] = {"path": source.relative_to(attempt).as_posix(), **fingerprint(source)}
    counts: Counter[str] = Counter()
    if "events.jsonl" in files:
        with (directory / "events.jsonl").open(encoding="utf-8") as events:
            for line in events:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                name = event.get("type") if isinstance(event, dict) else None
                if isinstance(name, str) and name in TRACE_EVENTS:
                    counts[name] += 1
    return {
        "version": 1, "scenario": scenario, "repeat": repeat, "status": result["status"],
        "trace_files": files, "event_counts": dict(counts),
        "artifacts": [
            {"path": item["path"], "sha256": item.get("observed"), "matches": item["passed"]}
            for item in result.get("checks", [])
            if item["check"] in {"expected_files", "preserve_files"}
        ],
    }


def session_from_index(index: Path) -> tuple[Path, str]:
    record = read_json(index)
    if record is None or record.get("version") != 1:
        raise ValueError("Expected a version 1 evidence index")
    files = record.get("trace_files")
    session = files.get("session.jsonl") if isinstance(files, dict) else None
    if not isinstance(session, dict):
        raise ValueError("This evidence index has no interactive or model session")
    path = local_entry(index.parent, session.get("path"))
    if path.name != "session.jsonl" or fingerprint(path) != {
        "sha256": session.get("sha256"), "size_bytes": session.get("size_bytes"),
    }:
        raise ValueError("Indexed session does not match its evidence fingerprint")
    return path, fingerprint(index)["sha256"]
