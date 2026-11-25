from __future__ import annotations

import hashlib
import json
import shutil
import tomllib
from copy import deepcopy

import pytest

from my_coding_agent.run_report import VerificationState
from my_pi_eval import regression
from my_pi_eval.regression import CASES_DIR, compare, main
from my_pi_eval.regression_case import capture_draft, digest_files, files_in, load_case, write_json
from my_pi_eval.regression_run import (
    BASH_SOURCE_PATH, SOURCE_ROOT, BashBaseline, collect_harbor, harbor_command, oracle_for, prepare_task, run_offline,
)
from my_pi_eval.regression_verify import verify


CASE_DIR = CASES_DIR / "fresh-verification"


def historical_bash() -> BashBaseline:
    try:
        return BashBaseline.load("pipefail-pre-fix")
    except (FileNotFoundError, ValueError) as exc:
        pytest.skip(str(exc))


def test_cli_runs_both_scripted_paths_and_keeps_separate_evidence(tmp_path):
    output = tmp_path / "run"
    args = ["run", "--case", str(CASE_DIR), "--output", str(output)]
    assert main(args) == 0
    scorecard = json.loads((output / "scorecard.json").read_bytes())
    assert scorecard["complete"]
    assert scorecard["manifest"]["mode"] == "offline"
    assert scorecard["manifest"]["model"] is None
    assert len(scorecard["attempts"]) == 2
    for attempt in scorecard["attempts"]:
        result_path = output / attempt["result"]
        result = json.loads(result_path.read_bytes())
        assert result["status"] == "pass"
        assert all(check["passed"] for check in result["checks"])
        agent = result_path.parent / result["evidence"]["agent"]
        assert (agent / "session.jsonl").is_file()
        assert (agent / "events.jsonl").is_file()
    assert (CASE_DIR / "workspace" / "settings.txt").read_bytes() == b"retries=2\n"
    assert main(args) == 2


@pytest.mark.parametrize("historical", [False, True], ids=["current", "pre-fix"])
def test_incident_case_requires_upstream_exit_code_and_output(tmp_path, historical):
    from my_coding_agent.tools.bash import _resolve_shell

    if not _resolve_shell()[1]:
        pytest.skip("Pipeline incident requires Bash")
    case_dir = CASES_DIR / "bash-pipefail"
    case, _ = load_case(case_dir)
    assert case.origin.kind == "incident"
    baseline = historical_bash() if historical else None
    attempt = tmp_path / "pipeline"
    result = run_offline(case, case_dir, case.scripts[0], attempt, baseline=baseline)
    assert result["status"] == ("fail" if historical else "pass")
    assert all(check["passed"] for check in result["checks"] if check["check"] in {"expected_files", "preserve_files"})
    assert {check["expected"]: check["passed"] for check in result["checks"]
            if check["check"] == "verification_output_contains"} == {
        "Command failed with exit code 42": not historical, "PIPELINE_FIXTURE: upstream check failed": True,
    }
    metrics = json.loads((attempt / "agent" / "metrics.json").read_bytes())
    assert metrics["verification"]["attempts"][-1]["status"] == ("passed" if historical else "failed")


@pytest.mark.parametrize("overrides,expected", [
    ({}, "pass"),
    ({"ready": False}, "blocked"),
    ({"latency_ms": 4402.43}, "fail"),
    ({"finished_naturally": True}, "fail"),
    ({"ok": True}, "fail"),
])
def test_cancellation_uses_observed_outcomes_not_just_tool_message(tmp_path, overrides, expected):
    case_dir = CASES_DIR / "bash-cancellation"
    case, _ = load_case(case_dir)
    workspace = case_dir / "workspace"
    oracle = oracle_for(case, workspace, None)
    oracle["cancel_within_ms"] = case.scripts[0].cancel_within_ms
    write_json(tmp_path / "cancellation.json", {
        "ready": True, "latency_ms": 1500, "ok": False,
        "tool_result": "Tool call interrupted by user", "finished_naturally": False, **overrides,
    })
    assert verify(workspace, tmp_path, oracle)["status"] == expected


def test_cancellation_rejects_a_preexisting_marker(tmp_path):
    case_dir = tmp_path / "bash-cancellation"
    shutil.copytree(CASES_DIR / case_dir.name, case_dir)
    (case_dir / "workspace" / "ready.txt").touch()
    with pytest.raises(ValueError, match="Cancellation marker files must not exist"):
        load_case(case_dir)


def test_scripted_case_catches_missing_stale_behavior(tmp_path, monkeypatch):
    observe = VerificationState.status.fget

    def without_stale(state):
        return "passed" if observe(state) == "stale" else observe(state)

    monkeypatch.setattr(VerificationState, "status", property(without_stale))
    case, _ = load_case(CASE_DIR)
    result = run_offline(case, CASE_DIR, case.scripts[0], tmp_path / "mutated-runtime")
    assert result["status"] == "fail"
    assert result["checks"][-1] == {
        "check": "runtime_verification", "expected": "stale", "observed": "passed", "passed": False,
    }


def test_verifier_rejects_green_check_of_different_bytes(tmp_path):
    case, _ = load_case(CASE_DIR)
    oracle = oracle_for(case, CASE_DIR / "workspace", "passed")
    workspace = tmp_path / "workspace"
    shutil.copytree(CASE_DIR / "workspace", workspace)
    (workspace / "settings.txt").write_bytes(b"retries=3\n")
    logs = tmp_path / "agent"
    logs.mkdir()
    write_json(logs / "metrics.json", {"stop_reason": "end_turn", "verification": {
        "status": "passed", "attempts": [{"status": "passed", "tool_call_id": "old-check"}],
    }})
    event = {"type": "tool_execution_end", "toolName": "bash", "toolCallId": "old-check", "isError": False,
             "result": "REGRESSION_SHA256 settings.txt " + "0" * 64}
    (logs / "events.jsonl").write_text(json.dumps(event) + "\n", encoding="utf-8")
    result = verify(workspace, logs, oracle)
    assert result["status"] == "fail"
    assert all(check["passed"] for check in result["checks"][:-1])
    assert result["checks"][-1]["check"] == "verified_final_bytes"
    assert verify(workspace, tmp_path / "missing", oracle)["status"] == "blocked"


def test_capture_preserves_provenance_without_copying_raw_history(tmp_path):
    session = tmp_path / "session.jsonl"
    session.write_text(json.dumps({"type": "message", "message": {"role": "user", "content": "PRIVATE_INPUT"}})
                       + "\n", encoding="utf-8")
    instruction = tmp_path / "reviewed.md"
    instruction.write_text("A minimal replacement prompt.", encoding="utf-8")
    draft = tmp_path / "new-case"
    capture_draft(session, 1, instruction, draft)
    case_text = (draft / "case.json").read_text(encoding="utf-8")
    assert "PRIVATE_INPUT" not in case_text and str(session) not in case_text
    assert json.loads(case_text)["origin"]["user_message"] == 1
    assert (draft / "instruction.md").read_text(encoding="utf-8") == "A minimal replacement prompt."
    with pytest.raises(ValueError, match="complete the draft"):
        load_case(draft)
    with pytest.raises(ValueError, match="outside the session"):
        capture_draft(session, 2, instruction, tmp_path / "invalid-message")


@pytest.mark.parametrize("unsafe_path", ["../outside.txt", "nested/../../outside.txt", "C:/outside.txt", "NUL.txt", ".env"])
def test_cases_reject_unportable_fixture_paths(tmp_path, unsafe_path):
    case_dir = tmp_path / CASE_DIR.name
    shutil.copytree(CASE_DIR, case_dir)
    case = json.loads((case_dir / "case.json").read_bytes())
    case["expected_files"][unsafe_path] = "bad"
    write_json(case_dir / "case.json", case)
    with pytest.raises(ValueError, match="Unsupported fixture path"):
        load_case(case_dir)


def test_live_task_keeps_oracle_and_scripts_outside_agent_image(tmp_path):
    case, _ = load_case(CASE_DIR)
    task = tmp_path / case.id
    prepare_task(case, CASE_DIR, task)
    assert tomllib.loads((task / "task.toml").read_text())["agent"]["timeout_sec"] == case.timeout_sec
    assert (task / "environment" / "workspace" / "check.py").is_file()
    assert not (task / "environment" / "workspace" / "case.json").exists()
    assert (task / "tests" / "oracle.json").is_file()
    command = harbor_command("harbor", task, tmp_path / "jobs", "deepseek/model", case, tmp_path / "runtime")
    assert "my_pi_eval.installed:MyPiInstalledAgent" in command
    assert "--skip-model-parsing-and-provider-mapping" in command
    assert command[-2:] == ["--agent-kwarg", 'verification_command="python -I -B check.py"']
    assert command[command.index("--max-retries") + 1] == "0"


def test_live_incident_requires_a_failed_verification_with_the_exit_code(tmp_path):
    case_dir = CASES_DIR / "bash-pipefail"
    case, _ = load_case(case_dir)
    task = tmp_path / case.id
    prepare_task(case, case_dir, task)
    oracle = json.loads((task / "tests" / "oracle.json").read_bytes())
    assert oracle["verification_status"] == "failed"
    assert "Command failed with exit code 42" in oracle["verify_output_contains"]
    command = harbor_command("harbor", task, tmp_path / "jobs", "deepseek/model", case, tmp_path / "runtime")
    assert command[-2:] == ["--agent-kwarg", 'verification_command="python -I -B upstream.py | cat"']


def test_live_component_comparison_freezes_only_the_historical_bash(tmp_path, monkeypatch):
    historical = historical_bash()
    current = (SOURCE_ROOT / BASH_SOURCE_PATH).read_bytes()
    assert historical.source != current
    monkeypatch.setattr(regression, "live_preflight", lambda harbor, model: {})
    snapshots, tasks = [], []

    def simulated_trial(case, task, attempt_dir, harbor, model, environment, source_root):
        attempt_dir.mkdir(parents=True)
        snapshots.append(source_root)
        tasks.append(task)
        return {"status": "blocked", "reason": "Test replaced Harbor execution"}

    monkeypatch.setattr(regression, "run_live", simulated_trial)
    args = ["run", "--mode", "live", "--model", "openai/mock-regression-test", "--trials", "1",
            "--case", str(CASES_DIR / "bash-pipefail")]
    before, after = tmp_path / "before", tmp_path / "after"
    assert main([*args, "--output", str(before), "--baseline-bash", "pipefail-pre-fix"]) == 2
    assert main([*args, "--output", str(after)]) == 2

    before_root, after_root = snapshots
    assert (before_root / BASH_SOURCE_PATH).read_bytes() == historical.source
    assert (after_root / BASH_SOURCE_PATH).read_bytes() == current
    assert (SOURCE_ROOT / BASH_SOURCE_PATH).read_bytes() == current
    before_files = [path for path in before_root.rglob("*") if path.is_file()]
    after_files = [path for path in after_root.rglob("*") if path.is_file()]
    assert digest_files(before_root, before_files, replacements={BASH_SOURCE_PATH: current}) == digest_files(
        after_root, after_files,
    )
    before_task, after_task = tasks
    assert digest_files(before_task, files_in(before_task)) == digest_files(after_task, files_in(after_task))
    baseline, candidate = (json.loads((run / "scorecard.json").read_bytes()) for run in (before, after))
    assert baseline["manifest"]["runtime_override"] == {
        "path": BASH_SOURCE_PATH, "revision": historical.revision, "sha256": hashlib.sha256(historical.source).hexdigest(),
    }
    assert digest_files(before_root, before_files) == baseline["manifest"]["runtime_sha256"]
    assert digest_files(after_root, after_files) == candidate["manifest"]["runtime_sha256"]
    assert compare(baseline, candidate)["scenarios"][0]["change"] == "inconclusive"
    candidate["manifest"]["working_runtime_sha256"] = "different"
    with pytest.raises(ValueError, match="same working runtime"):
        compare(baseline, candidate)


@pytest.mark.parametrize("status,reward,expected", [("pass", 1, "pass"), ("fail", 0, "fail"),
                                                   ("blocked", 0, "blocked"), ("pass", 0, "blocked")])
def test_live_result_requires_consistent_external_verdict(tmp_path, status, reward, expected):
    trial = tmp_path / "jobs" / "regression" / "case__trial"
    (trial / "verifier").mkdir(parents=True)
    write_json(trial / "result.json", {"verifier_result": {"rewards": {"reward": reward}}})
    write_json(trial / "verifier" / "checks.json", {"status": status, "checks": []})
    result = collect_harbor(tmp_path, 0)
    assert result["status"] == expected
    assert result["evidence"]["harbor_result"].endswith("case__trial/result.json")


def test_scorecards_require_identical_experiment_inputs():
    baseline = {
        "complete": True,
        "manifest": {"version": 1, "mode": "offline", "model": None, "trials": 2, "endpoint_sha256": None,
                     "cases": {"fresh-verification": "case"}, "harness_sha256": "harness", "host_platform": "win32",
                     "host_python": "3.12.0", "runtime_sha256": "before"},
        "totals": {"fresh-verification/stale": {"pass": 2, "fail": 0, "blocked": 0}},
    }
    candidate = deepcopy(baseline)
    candidate["manifest"]["runtime_sha256"] = "after"
    counts = candidate["totals"]["fresh-verification/stale"]
    counts.update({"pass": 1, "fail": 1})
    assert compare(baseline, candidate)["scenarios"][0]["change"] == "regression"
    counts.update({"fail": 0, "blocked": 1})
    assert compare(baseline, candidate)["scenarios"][0]["change"] == "inconclusive"
    candidate["manifest"]["cases"]["fresh-verification"] = "different"
    with pytest.raises(ValueError, match="cases differs"):
        compare(baseline, candidate)
