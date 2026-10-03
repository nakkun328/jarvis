"""Run local verification gates; never push or mutate a source branch."""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import uuid
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(repo), *args], text=True, stderr=subprocess.DEVNULL
    ).strip()


def state(repo: Path) -> dict[str, str | bool]:
    return {
        "head": git(repo, "rev-parse", "HEAD"),
        "dirty": bool(git(repo, "status", "--porcelain", "--untracked-files=all")),
    }


def pytest_counts(path: Path) -> dict[str, int]:
    cases = ET.parse(path).getroot().findall(".//testcase")
    return {
        "passed": sum(not any(case.find(tag) is not None for tag in (
            "skipped", "failure", "error"
        )) for case in cases),
        "skipped": sum(case.find("skipped") is not None for case in cases),
        "failed": sum(any(case.find(tag) is not None for tag in (
            "failure", "error"
        )) for case in cases),
    }


def verify(args: argparse.Namespace) -> int:
    repo = args.repo.resolve()
    initial = state(repo)
    base = git(repo, "rev-parse", "--verify", f"{args.base}^{{commit}}")
    run_id = args.run_id or uuid.uuid4().hex
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", run_id):
        raise ValueError("run ID must use letters, digits, underscores or hyphens")
    output_root = args.output_dir.resolve()
    if output_root == repo or repo in output_root.parents:
        raise ValueError("output directory must be outside the source worktree")
    output = output_root / f"{initial['head']}-{run_id}"
    # A reused ID fails before opening any log; old writers cannot share this run.
    output.mkdir(parents=True, exist_ok=False)
    print(f"Verification output: {output}", flush=True)
    environment = dict(os.environ)
    for key in ("OPENAI_API_KEY", "GEMINI_API_KEY", "JARVIS_MEMORY_VAULT_PATH"):
        environment.pop(key, None)
    environment.update(JARVIS_LLM_PROVIDER="none", JARVIS_DB_PATH=str(output / "test.sqlite3"))
    result = {
        "run_id": run_id,
        "started_at": datetime.now(UTC).isoformat(),
        "repo": str(repo),
        "expected_head": args.expected_head,
        "base": base,
        "initial": initial,
        "checks": [],
        "status": "failed",
    }
    exit_code = 1
    if initial["head"] != args.expected_head or initial["dirty"]:
        result["reason"] = "initial HEAD mismatch or dirty worktree"
    else:
        scanner = Path(__file__).with_name("secret_scan.py")
        commands = [
            ("secret", [args.python, str(scanner), "--repo", str(repo), "--base", base]),
            ("pytest", [args.python, "-m", "pytest", "-q", f"--junitxml={output / 'pytest.xml'}"]),
            ("ruff", [args.python, "-m", "ruff", "check", "."]),
            ("compileall", [args.python, "-m", "compileall", "-q", "backend", "tests", "scripts"]),
            ("frontend", [args.node, "--test", "frontend/test/chat-api.test.mjs"]),
            *[(f"syntax-{path.name}", [args.node, "--check", str(path)])
              for path in sorted((repo / "frontend").glob("*.js"))],
            ("diff-worktree", ["git", "diff", "--check"]),
            ("diff-branch", ["git", "diff", "--check", f"{base}...HEAD"]),
        ]
        for name, command in commands:
            with (output / f"{name}.log").open("x") as log:
                try:
                    return_code = subprocess.run(
                        command, cwd=repo, env=environment, stdout=log,
                        stderr=subprocess.STDOUT, check=False
                    ).returncode
                except OSError:
                    log.write("Verification command could not start\n")
                    return_code = 127
            check = {"name": name, "returncode": return_code}
            result["checks"].append(check)
            print(f"{name}: exit {return_code}", flush=True)
            if return_code:
                exit_code = return_code if return_code > 0 else 128 - return_code
                result["reason"] = f"{name} failed"
                break
            if name == "pytest":
                counts = pytest_counts(output / "pytest.xml")
                check["tests"] = counts
                if counts["skipped"] or not counts["passed"] or counts["failed"]:
                    result["reason"] = "pytest skipped tests or did not record passing tests"
                    break
        else:
            exit_code = 0
    final = state(repo)
    if final != initial:
        result["reason"] = "HEAD or worktree changed during verification"
        # Preserve a failed command's exit code while also recording source changes.
        exit_code = exit_code or 1
    result["final"] = final
    result["exitcode"] = exit_code
    result["status"] = "passed" if exit_code == 0 else "failed"
    result["finished_at"] = datetime.now(UTC).isoformat()
    # Only the terminal result is published. Logs alone never authorize a push.
    temporary = output / "result.tmp"
    temporary.write_text(json.dumps(result, indent=2) + "\n")
    temporary.rename(output / "result.json")
    return exit_code


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--expected-head", required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--node", default="node")
    parser.add_argument(
        "--output-dir", type=Path, default=Path(tempfile.gettempdir()) / "jarvis-checks"
    )
    parser.add_argument("--run-id")
    args = parser.parse_args()
    try:
        return verify(args)
    except (OSError, subprocess.CalledProcessError, ValueError, ET.ParseError):
        print("Verification did not complete; no successful result is available", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
