"""Judge final artifacts independently of the assistant's answer."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verification_output(logs: Path, verification: Any) -> str:
    attempts = verification.get("attempts") if isinstance(verification, dict) else None
    if not isinstance(attempts, list) or not attempts or not isinstance(attempts[-1], dict):
        return ""
    last = attempts[-1]
    call_id = last.get("tool_call_id")
    status = last.get("status")
    if not isinstance(call_id, str) or status not in {"passed", "failed"}:
        return ""
    try:
        with (logs / "events.jsonl").open(encoding="utf-8") as events:
            for line in events:
                event = json.loads(line)
                if (isinstance(event, dict) and event.get("type") == "tool_execution_end" and event.get("toolName") == "bash"
                        and event.get("toolCallId") == call_id and event.get("isError") is (status == "failed")):
                    return str(event.get("result", ""))
    except (OSError, ValueError):
        pass
    return ""


def verify_cancellation(logs: Path, max_ms: int, checks: list[dict[str, Any]]) -> dict[str, Any]:
    evidence = read_json(logs / "cancellation.json")
    if evidence is None or evidence.get("ready") is not True:
        return {"status": "blocked", "reason": "Cancellation fixture did not reach the ready checkpoint", "checks": checks}
    latency = evidence.get("latency_ms")
    checks.extend([
        {"check": "cancel_latency_ms", "max_ms": max_ms, "observed": latency,
         "passed": type(latency) in {int, float} and 0 <= latency <= max_ms},
        {"check": "tool_reported_interruption", "passed": evidence.get("ok") is False
         and "Tool call interrupted by user" in str(evidence.get("tool_result", ""))},
        {"check": "child_did_not_finish_naturally", "passed": evidence.get("finished_naturally") is False},
    ])
    return {"status": "pass" if all(check["passed"] for check in checks) else "fail", "checks": checks}


def verify(workspace: Path, logs: Path, oracle: dict[str, Any]) -> dict[str, Any]:
    checks = []
    for group in ("expected_files", "preserve_files"):
        for name, expected in oracle[group].items():
            path = workspace / name
            try:
                actual = sha256_file(path) if path.resolve() == path.absolute() and path.is_file() else None
            except (OSError, RuntimeError):
                actual = None
            checks.append({"check": group, "path": name, "expected": expected, "observed": actual,
                           "passed": actual == expected})

    if (max_ms := oracle.get("cancel_within_ms")) is not None:
        return verify_cancellation(logs, max_ms, checks)

    metrics = read_json(logs / "metrics.json")
    if metrics is None or metrics.get("stop_reason") != "end_turn" or metrics.get("error_type") is not None:
        return {"status": "blocked", "reason": "No normally completed Agent run", "checks": checks}
    verification = metrics.get("verification")
    if (expected := oracle["verification_status"]) is not None:
        actual = verification.get("status") if isinstance(verification, dict) else None
        checks.append({"check": "runtime_verification", "expected": expected, "observed": actual,
                       "passed": actual == expected})
    fragments = oracle.get("verify_output_contains", [])
    if oracle["checkpoint_files"] or fragments:
        output = verification_output(logs, verification)
        lines = set(output.splitlines())
        for name in oracle["checkpoint_files"]:
            expected = oracle["expected_files"][name]
            checks.append({"check": "verified_final_bytes", "path": name, "expected": expected,
                           "passed": f"REGRESSION_SHA256 {name} {expected}" in lines})
        for fragment in fragments:
            checks.append({"check": "verification_output_contains", "expected": fragment, "passed": fragment in output})
    return {"status": "pass" if all(check["passed"] for check in checks) else "fail", "checks": checks}


def main() -> int:
    oracle_path = Path(sys.argv[1])
    output = Path("/logs/verifier")
    output.mkdir(parents=True, exist_ok=True)
    oracle = read_json(oracle_path)
    if oracle is None:
        raise ValueError("Missing verifier contract")
    result = verify(Path("/app"), Path("/logs/agent"), oracle)
    (output / "checks.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    (output / "reward.txt").write_text("1\n" if result["status"] == "pass" else "0\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
