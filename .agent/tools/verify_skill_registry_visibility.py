#!/usr/bin/env python3
"""Verify Claude/Codex runtime visibility of secondary skill registries.

This is a dynamic smoke test: it asks each CLI to report whether its loaded
instructions mention the expected secondary registries. It deliberately treats
"successful" zero-turn Claude/Codex results as failures, because that means the
CLI process ran but no model/test execution happened.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass


@dataclass
class RuntimeResult:
    name: str
    command: list[str]
    returncode: int
    stdout: str
    stderr: str
    turns: int | None
    duration_api_ms: int | float | None
    result: str


def _parse_jsonish(stdout: str) -> dict:
    text = stdout.strip()
    if not text:
        return {}
    # Some CLIs/hooks may print warning lines. Prefer the last valid JSON line.
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {}


def run_command(name: str, command: list[str], timeout: int) -> RuntimeResult:
    proc = subprocess.run(command, text=True, capture_output=True, timeout=timeout)
    data = _parse_jsonish(proc.stdout)
    turns = data.get("num_turns")
    if turns is None:
        usage = data.get("usage")
        iterations = usage.get("iterations") if isinstance(usage, dict) else None
        if isinstance(iterations, list):
            turns = len(iterations)
    duration_api_ms = data.get("duration_api_ms") if isinstance(data.get("duration_api_ms"), (int, float)) else None
    result = data.get("result") if isinstance(data.get("result"), str) else proc.stdout.strip()
    return RuntimeResult(name, command, proc.returncode, proc.stdout, proc.stderr, turns, duration_api_ms, result)


def assert_runtime_ok(runtime: RuntimeResult, expected_terms: list[str]) -> list[str]:
    failures: list[str] = []
    if runtime.returncode != 0:
        failures.append(f"{runtime.name}: exit code {runtime.returncode}")
    if runtime.turns is not None and runtime.turns <= 0:
        failures.append(f"{runtime.name}: zero model turns; CLI launched but test did not execute")
    if runtime.duration_api_ms is not None and runtime.duration_api_ms <= 0:
        failures.append(f"{runtime.name}: zero API duration; CLI returned without a model call")
    if "out of extra usage" in runtime.result.lower():
        failures.append(f"{runtime.name}: Claude usage quota exhausted; retry after reset before trusting runtime visibility")
    if not runtime.result.strip():
        failures.append(f"{runtime.name}: empty result")
    lower = runtime.result.lower()
    for term in expected_terms:
        if term.lower() not in lower:
            failures.append(f"{runtime.name}: result missing expected term {term!r}")
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify Claude/Codex secondary skill registry visibility.")
    parser.add_argument("--timeout", type=int, default=90)
    parser.add_argument("--skip-codex", action="store_true")
    parser.add_argument("--skip-claude", action="store_true")
    args = parser.parse_args(argv)

    failures: list[str] = []
    results: list[RuntimeResult] = []

    if not args.skip_codex:
        if shutil.which("codex"):
            prompt = (
                "Check your loaded instructions. Do they say Codex should consult "
                "~/.claude/skills and ~/.agent/skills as secondary skill registries? "
                "Answer exactly: FOUND claude agents, or MISSING."
            )
            results.append(run_command("codex", ["codex", "exec", prompt], args.timeout))
            failures.extend(assert_runtime_ok(results[-1], ["FOUND", "claude", "agent"]))
        else:
            failures.append("codex: command not found")

    if not args.skip_claude:
        if shutil.which("claude"):
            prompt = (
                "Check your loaded instructions. Do they say to consult ~/.agent/skills "
                "as a secondary read-only skill registry? Answer exactly: FOUND agents, or MISSING."
            )
            results.append(run_command("claude", ["claude", "-p", "--output-format", "json", prompt], args.timeout))
            failures.extend(assert_runtime_ok(results[-1], ["FOUND", "agent"]))
        else:
            failures.append("claude: command not found")

    for result in results:
        print(f"## {result.name}")
        print(f"command: {' '.join(result.command)}")
        print(f"exit: {result.returncode}")
        print(f"turns: {result.turns}")
        print(f"duration_api_ms: {result.duration_api_ms}")
        print(f"result: {result.result.strip()!r}")
        if result.stderr.strip():
            print(f"stderr: {result.stderr.strip()}")
        print()

    if failures:
        print("FAILURES:", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        return 1

    print("Skill registry visibility verified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
