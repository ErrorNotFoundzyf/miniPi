"""Capture reviewed regression drafts, run suites, and compare scorecards."""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from my_pi_eval.regression_case import capture_draft, digest_files, load_case, write_json
from my_pi_eval.regression_run import BASH_SOURCE_PATH, HISTORICAL_BASH, SOURCE_ROOT, BashBaseline, live_preflight, prepare_task, run_live, run_offline
from my_pi_eval.regression_verify import read_json


CASES_DIR = SOURCE_ROOT / "my-pi-eval" / "cases"
EXIT_STATUS = {"pass": 0, "fail": 1, "blocked": 2}


def runtime_files() -> list[Path]:
    files = [SOURCE_ROOT / name for name in ("pyproject.toml", "uv.lock", "README.md")]
    for name in ("my_agent_core", "my_agent_llm", "my_coding_agent"):
        package = SOURCE_ROOT / "src" / name
        files.extend(path for path in package.rglob("*") if path.is_file()
                     and (path.suffix == ".py" or path.name == "py.typed"))
    return files


def snapshot_runtime(
    files: list[Path], target: Path, expected: str, *, replacements: dict[str, bytes] | None = None,
) -> None:
    replacements = replacements or {}
    for file in files:
        if file.is_symlink() or file.resolve() != file.absolute():
            raise ValueError(f"Source links are not supported: {file.name}")
        relative = file.relative_to(SOURCE_ROOT)
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if relative.as_posix() in replacements:
            destination.write_bytes(replacements[relative.as_posix()])
        else:
            shutil.copyfile(file, destination)
    if digest_files(target, [target / file.relative_to(SOURCE_ROOT) for file in files]) != expected:
        raise ValueError("Working tree changed while taking the runtime snapshot")


def snapshot_case(source: Path, target: Path, expected: str) -> None:
    target.mkdir(parents=True)
    for name in ("case.json", "instruction.md"):
        shutil.copyfile(source / name, target / name)
    shutil.copytree(source / "workspace", target / "workspace", symlinks=True)
    if load_case(target)[1] != expected:
        raise ValueError(f"Case changed while taking snapshots: {target.name}")


def summarize(attempts: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    groups: defaultdict[str, Counter[str]] = defaultdict(Counter)
    for attempt in attempts:
        groups[attempt["scenario"]][attempt["status"]] += 1
    return {scenario: {status: counts[status] for status in EXIT_STATUS} for scenario, counts in groups.items()}


def run_suite(args: argparse.Namespace) -> int:
    directories = [path.absolute() for path in args.case] if args.case else sorted(
        path.absolute() for path in CASES_DIR.iterdir() if (path / "case.json").is_file()
    )
    cases = [(path, *load_case(path)) for path in directories]
    cases = [(path, case, digest) for path, case, digest in cases if (case.scripts if args.mode == "offline" else case.live)]
    if not cases or len({case.id for _, case, _ in cases}) != len(cases):
        raise ValueError("Choose at least one eligible case with a unique case ID")
    output = args.output.resolve()
    if output.is_relative_to(SOURCE_ROOT / "src") or any(output.is_relative_to(path.resolve()) for path, _, _ in cases):
        raise ValueError("Output must be outside runtime source and case directories")
    trials = args.trials if args.trials is not None else (1 if args.mode == "offline" else 3)
    if not 1 <= trials <= 30:
        raise ValueError("--trials must be between 1 and 30")
    if (args.mode == "live") != bool(args.model):
        raise ValueError("Only live runs require --model")
    baseline = BashBaseline.load(args.baseline_bash) if args.baseline_bash else None
    environment = live_preflight(args.harbor, args.model) if args.mode == "live" else None
    route = args.model.partition("/")[0].upper() if args.model else None
    endpoint = (os.environ.get(f"{route}_BASE_URL") or "provider-default") if route else None
    harness_dir = Path(__file__).resolve().parent
    harness_files = list(harness_dir.glob("regression*.py"))
    if args.mode == "live":
        harness_files.append(harness_dir / "installed.py")
    files = runtime_files()
    working_runtime = digest_files(SOURCE_ROOT, files)
    runtime_overrides = {BASH_SOURCE_PATH: baseline.source} if baseline else None
    manifest = {
        "version": 1, "mode": args.mode, "model": args.model, "trials": trials,
        "endpoint_sha256": hashlib.sha256(endpoint.encode()).hexdigest() if endpoint else None,
        "cases": {case.id: digest for _, case, digest in cases},
        "runtime_sha256": digest_files(SOURCE_ROOT, files, replacements=runtime_overrides) if baseline else working_runtime,
        "working_runtime_sha256": working_runtime,
        "runtime_override": {"path": BASH_SOURCE_PATH, "revision": baseline.revision,
                             "sha256": hashlib.sha256(baseline.source).hexdigest()} if baseline else None,
        "harness_sha256": digest_files(harness_dir, harness_files),
        "host_platform": sys.platform,
        "host_python": sys.version.split()[0],
    }
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / "manifest.json", manifest)
    runtime = output / "inputs" / "runtime"
    if args.mode == "live":
        snapshot_runtime(files, runtime, manifest["runtime_sha256"], replacements=runtime_overrides)
        if digest_files(SOURCE_ROOT, runtime_files()) != working_runtime:
            raise ValueError("Working tree changed while taking the runtime snapshot")
    snapshots = output / "inputs" / "cases"
    for case_dir, case, digest in cases:
        snapshot_case(case_dir, snapshots / case.id, digest)
    attempts = []
    for _, case, _ in cases:
        case_dir = snapshots / case.id
        task = output / "tasks" / case.id
        if args.mode == "live":
            prepare_task(case, case_dir, task)
        for script in case.scripts if args.mode == "offline" else [None]:
            scenario = f"{case.id}/{script.id if script else 'live'}"
            for repeat in range(1, trials + 1):
                attempt_dir = output / "attempts" / scenario / f"{repeat:02d}"
                result = (run_offline(case, case_dir, script, attempt_dir, baseline) if script else
                          run_live(case, task, attempt_dir, args.harbor, args.model, environment, runtime))
                write_json(attempt_dir / "result.json", result)
                attempts.append({"scenario": scenario, "repeat": repeat, "status": result["status"],
                                 "result": (attempt_dir / "result.json").relative_to(output).as_posix()})
                write_json(output / "scorecard.json", {"manifest": manifest, "complete": False,
                                                       "attempts": attempts, "totals": summarize(attempts)})
                print(f"{result['status']:7} {scenario} #{repeat}", flush=True)
    if args.mode == "offline" and digest_files(SOURCE_ROOT, runtime_files()) != working_runtime:
        raise ValueError("Working tree changed during the offline run; the scorecard is incomplete")
    write_json(output / "scorecard.json", {"manifest": manifest, "complete": True,
                                           "attempts": attempts, "totals": summarize(attempts)})
    return max(EXIT_STATUS[attempt["status"]] for attempt in attempts)


def compare(baseline: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    if not baseline.get("complete") or not candidate.get("complete"):
        raise ValueError("Compare only completed scorecards")
    keys = ("version", "mode", "model", "trials", "endpoint_sha256", "cases", "harness_sha256", "host_platform", "host_python")
    before_manifest, after_manifest = baseline["manifest"], candidate["manifest"]
    for key in keys:
        if before_manifest[key] != after_manifest[key]:
            raise ValueError(f"Scorecards are not comparable: {key} differs")
    if before_manifest.get("runtime_override") or after_manifest.get("runtime_override"):
        working_runtime = before_manifest.get("working_runtime_sha256")
        if not working_runtime or working_runtime != after_manifest.get("working_runtime_sha256"):
            raise ValueError("A component comparison requires the same working runtime in both runs")
    old, new = baseline["totals"], candidate["totals"]
    if not old or old.keys() != new.keys():
        raise ValueError("Scorecards contain different scenarios")
    scenarios = []
    for scenario in sorted(old):
        before, after = old[scenario], new[scenario]
        delta = after["pass"] - before["pass"]
        change = ("inconclusive" if before["blocked"] or after["blocked"] else
                  "regression" if delta < 0 else "improvement" if delta > 0 else "unchanged")
        scenarios.append({"scenario": scenario, "change": change, "pass_delta": delta, "before": before, "after": after})
    return {"baseline_runtime": baseline["manifest"]["runtime_sha256"],
            "candidate_runtime": candidate["manifest"]["runtime_sha256"], "scenarios": scenarios}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    capture = commands.add_parser("capture", help="Start a draft from a session reference and an already sanitized prompt")
    capture.add_argument("--session", required=True, type=Path)
    capture.add_argument("--user-message", default=1, type=int)
    capture.add_argument("--instruction-file", required=True, type=Path)
    capture.add_argument("--output", required=True, type=Path)
    run = commands.add_parser("run", help="Run curated cases and produce a scorecard")
    run.add_argument("--mode", choices=("offline", "live"), default="offline")
    run.add_argument("--case", action="append", type=Path, help="Case directory; repeat to select several")
    run.add_argument("--output", required=True, type=Path)
    run.add_argument("--trials", type=int)
    run.add_argument("--model", help="Live provider/model route")
    run.add_argument("--harbor", default="harbor", help="Path to the Harbor 0.26 executable")
    run.add_argument("--baseline-bash", choices=sorted(HISTORICAL_BASH), help="Use a pinned historical Bash tool in the runtime")
    diff = commands.add_parser("compare", help="Compare two like-for-like scorecards")
    diff.add_argument("baseline", type=Path)
    diff.add_argument("candidate", type=Path)
    try:
        args = parser.parse_args(argv)
        if args.command == "capture":
            capture_draft(args.session, args.user_message, args.instruction_file, args.output)
            print(f"Draft created: {args.output}. Review the fixture and checks before marking it runnable.")
            return 0
        if args.command == "run":
            return run_suite(args)
        baseline, candidate = read_json(args.baseline), read_json(args.candidate)
        if baseline is None or candidate is None:
            raise ValueError("Both inputs must be scorecard JSON files")
        comparison = compare(baseline, candidate)
        for scenario in comparison["scenarios"]:
            print(f"{scenario['change']:12} {scenario['scenario']} ({scenario['pass_delta']:+d} passes)")
        changes = {scenario["change"] for scenario in comparison["scenarios"]}
        return 2 if "inconclusive" in changes else 1 if "regression" in changes else 0
    except (KeyError, OSError, TypeError, ValueError) as exc:
        print(f"regression: {exc}", file=sys.stderr)
        return 2
