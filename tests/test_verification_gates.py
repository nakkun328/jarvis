"""Real failing tools must stop verification before any simulated follow-up."""

import json
import os
import runpy
import subprocess
import sys
import time
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
VERIFY = SCRIPTS / "verify.py"
SCANNER = SCRIPTS / "secret_scan.py"


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def commit(repo: Path, message: str) -> str:
    git(repo, "add", ".")
    git(repo, "commit", "-qm", message)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.name", "Gate tests")
    git(root, "config", "user.email", "gate-tests@example.invalid")
    for directory in ("tests", "backend", "scripts", "frontend/test"):
        (root / directory).mkdir(parents=True)
    (root / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n.ruff_cache/\n")
    (root / "backend/__init__.py").write_text("")
    (root / "scripts/__init__.py").write_text("")
    (root / "tests/test_one.py").write_text("def test_one():\n    assert True\n")
    (root / "frontend/app.js").write_text("const value = 1;\n")
    (root / "frontend/test/chat-api.test.mjs").write_text(
        "import test from 'node:test';\n"
        "import assert from 'node:assert/strict';\n"
        "test('fixture', () => assert.ok(true));\n"
    )
    (root / "README.md").write_text("Disposable verification fixture.\n")
    return root, commit(root, "base fixture")


def command(root: Path, base: str, output: Path, run_id: str = "test") -> list[str]:
    return [
        sys.executable, str(VERIFY), "--repo", str(root), "--base", base,
        "--expected-head", git(root, "rev-parse", "HEAD"),
        "--output-dir", str(output), "--run-id", run_id,
    ]


def run_gate(root: Path, base: str, output: Path, run_id: str = "test"):
    followup = output.parent / f"followup-{run_id}"
    # This is deliberately only a disposable file, never a real push command.
    process = subprocess.run(
        ["sh", "-c", '\"$@\" && : > \"$FOLLOWUP\"', "gate-followup",
         *command(root, base, output, run_id)],
        env={**os.environ, "FOLLOWUP": str(followup)},
        capture_output=True, text=True, timeout=60, check=False,
    )
    folder = output / f"{git(root, 'rev-parse', 'HEAD')}-{run_id}"
    # A test may intentionally move HEAD while tools run; use the original run folder.
    if not folder.exists():
        folder = next(output.glob(f"*-{run_id}"))
    receipt = json.loads((folder / "result.json").read_text())
    return process, receipt, folder, followup


@pytest.mark.parametrize(("failure", "exitcode", "last_check"), [
    ("pytest", 1, "pytest"), ("lint", 1, "ruff"),
    ("diff", 2, "diff-branch"), ("secret", 1, "secret"),
])
def test_real_gate_failure_blocks_followup(
    repo: tuple[Path, str], tmp_path: Path, failure: str, exitcode: int, last_check: str
) -> None:
    root, base = repo
    credential = "s" + "k-" + "SYNTHETIC" * 5
    if failure == "pytest":
        (root / "tests/test_one.py").write_text(
            'def test_one():\n    print("SUCCESS MARKER")\n    assert False\n'
        )
    elif failure == "lint":
        (root / "backend/broken.py").write_text("undefined_name\n")
    elif failure == "diff":
        (root / "README.md").write_text("Whitespace failure.  \n")
    else:
        (root / "credential.txt").write_text(credential + "  \n")
    commit(root, f"intentional {failure} failure")
    process, receipt, folder, followup = run_gate(root, base, tmp_path / "logs")
    assert process.returncode == exitcode
    assert receipt["exitcode"] == exitcode
    assert receipt["status"] == "failed"
    assert receipt["checks"][-1]["name"] == last_check
    assert receipt["checks"][-1]["returncode"] == exitcode
    assert not followup.exists()
    assert len(list(folder.glob("*.log"))) == len(receipt["checks"])
    if failure == "pytest":
        assert "SUCCESS MARKER" in (folder / "pytest.log").read_text()
        assert not (folder / "ruff.log").exists()
    if failure == "secret":
        assert credential not in (folder / "secret.log").read_text()
        assert not (folder / "pytest.log").exists()
        assert not (folder / "diff-branch.log").exists()


def test_success_requires_final_same_clean_head_and_distinct_logs(
    repo: tuple[Path, str], tmp_path: Path
) -> None:
    root, base = repo
    output = tmp_path / "logs"
    process, receipt, folder, followup = run_gate(root, base, output, "one")
    assert process.returncode == 0
    assert followup.exists()
    assert receipt["initial"] == receipt["final"] == {"head": base, "dirty": False}
    assert receipt["checks"][1]["tests"] == {"passed": 1, "skipped": 0, "failed": 0}
    frontend = next(check for check in receipt["checks"] if check["name"] == "frontend")
    assert frontend["tests"] == {"passed": 1, "skipped": 0, "failed": 0}
    assert receipt["status"] == "passed"
    old_receipt = (folder / "result.json").read_bytes()
    process = subprocess.run(command(root, base, output, "one"), capture_output=True, check=False)
    assert process.returncode != 0
    assert (folder / "result.json").read_bytes() == old_receipt
    process, _, second_folder, _ = run_gate(root, base, output, "two")
    assert process.returncode == 0
    assert second_folder != folder


@pytest.mark.parametrize("mutation", ["head", "dirty"])
def test_changes_during_checks_invalidate_success(
    repo: tuple[Path, str], tmp_path: Path, mutation: str
) -> None:
    root, base = repo
    change = (
        '    subprocess.run(["git", "commit", "--allow-empty", "-qm", "moved"], check=True)\n'
        if mutation == "head" else '    Path("unexpected.txt").write_text("changed")\n'
    )
    imports = "import subprocess\n" if mutation == "head" else "from pathlib import Path\n"
    (root / "tests/test_one.py").write_text(imports + "\n\ndef test_one():\n" + change)
    original = commit(root, "test mutates source")
    process, receipt, _, followup = run_gate(root, base, tmp_path / "logs")
    assert process.returncode != 0
    assert not followup.exists()
    assert receipt["status"] == "failed"
    assert all(check["returncode"] == 0 for check in receipt["checks"])
    assert receipt["initial"] == {"head": original, "dirty": False}
    assert receipt["final"] != receipt["initial"]
    assert receipt["reason"] == "HEAD or worktree changed during verification"


def test_skipped_pytest_is_not_a_success(repo: tuple[Path, str], tmp_path: Path) -> None:
    root, base = repo
    (root / "tests/test_one.py").write_text(
        'import pytest\n\n\ndef test_one():\n    pytest.skip("missing prerequisite")\n'
    )
    commit(root, "explicit prerequisite skip")
    process, receipt, folder, followup = run_gate(root, base, tmp_path / "logs")
    assert process.returncode != 0
    assert receipt["checks"][1]["returncode"] == 0
    assert receipt["checks"][1]["tests"] == {"passed": 0, "skipped": 1, "failed": 0}
    assert not (folder / "ruff.log").exists()
    assert not followup.exists()


@pytest.mark.parametrize("pending", ["skip", "todo"])
def test_pending_frontend_is_not_a_success(
    repo: tuple[Path, str], tmp_path: Path, pending: str
) -> None:
    root, base = repo
    (root / "frontend/test/chat-api.test.mjs").write_text(
        "import test from 'node:test';\n"
        f"test.{pending}('missing prerequisite', () => {{}});\n"
    )
    head = commit(root, f"frontend {pending} prerequisite")
    process, receipt, folder, followup = run_gate(root, base, tmp_path / "logs", pending)
    assert process.returncode == receipt["exitcode"] == 1
    assert receipt["status"] == "failed"
    assert receipt["initial"] == receipt["final"] == {"head": head, "dirty": False}
    assert receipt["run_id"] == pending
    assert folder.name == f"{head}-{pending}"
    assert receipt["checks"][-1] == {
        "name": "frontend", "returncode": 0,
        "tests": {"passed": 0, "skipped": 1, "failed": 0},
    }
    assert len(list(folder.glob("*.log"))) == len(receipt["checks"])
    assert not (folder / "syntax-app.js.log").exists()
    assert not followup.exists()


def test_frontend_success_marker_does_not_hide_failure(
    repo: tuple[Path, str], tmp_path: Path
) -> None:
    root, base = repo
    (root / "frontend/test/chat-api.test.mjs").write_text(
        "import test from 'node:test';\n"
        "import assert from 'node:assert/strict';\n"
        "test('SUCCESS MARKER', () => assert.ok(true));\n"
        "test('failure after marker', () => {\n"
        "  assert.fail('intentional frontend failure');\n"
        "});\n"
    )
    commit(root, "frontend marker precedes real failure")
    process, receipt, folder, followup = run_gate(root, base, tmp_path / "logs")
    assert process.returncode == receipt["exitcode"] == 1
    assert receipt["status"] == "failed"
    assert receipt["checks"][-1] == {"name": "frontend", "returncode": 1}
    assert "SUCCESS MARKER" in (folder / "frontend.log").read_text()
    assert not (folder / "diff-worktree.log").exists()
    assert not followup.exists()


def test_no_terminal_receipt_while_test_is_running(repo: tuple[Path, str], tmp_path: Path) -> None:
    root, base = repo
    (root / "tests/test_one.py").write_text(
        "import time\nfrom pathlib import Path\n\n\ndef test_one():\n"
        '    Path("started").write_text("SUCCESS MARKER")\n'
        '    while not Path("release").exists():\n        time.sleep(0.02)\n'
    )
    with (root / ".gitignore").open("a") as file:
        file.write("started\nrelease\n")
    head = commit(root, "hold test until observation")
    output = tmp_path / "logs"
    process = subprocess.Popen(command(root, base, output), stdout=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 20
        while not (root / "started").exists():
            assert process.poll() is None
            assert time.monotonic() < deadline
            time.sleep(0.02)
        folder = output / f"{head}-test"
        assert not (folder / "result.json").exists()
        assert not (folder / "ruff.log").exists()
    finally:
        (root / "release").touch()
        process.wait(timeout=30)
    assert process.returncode == 0
    assert json.loads((folder / "result.json").read_text())["status"] == "passed"


def test_scanner_checks_removed_credentials_and_redacts_values(
    repo: tuple[Path, str]
) -> None:
    root, base = repo
    credential = "s" + "k-" + "SYNTHETIC" * 5
    (root / "removed.txt").write_text(credential)
    bad_head = commit(root, "dummy credential snapshot")
    (root / "removed.txt").unlink()
    commit(root, "remove dummy credential")
    result = subprocess.run(
        [sys.executable, str(SCANNER), "--repo", str(root), "--base", base],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 1
    assert bad_head[:12] in result.stdout
    assert credential not in result.stdout + result.stderr


def test_secret_assignment_placeholders_and_sensitive_names() -> None:
    findings = runpy.run_path(str(SCANNER))["findings"]
    assert findings(".env.example", b"API_KEY=example\nPASSWORD=${PLACEHOLDER}\n") == set()
    assert findings("config.txt", b"API_KEY=not-a-placeholder\n") == {
        "non-placeholder secret assignment"
    }
    assert findings(".env", b"") == {"sensitive filename"}
    assert findings("private.pem", b"") == {"sensitive filename"}


@pytest.mark.parametrize("preflight", ["head", "dirty"])
def test_preflight_rejects_wrong_head_or_dirty_source(
    repo: tuple[Path, str], tmp_path: Path, preflight: str
) -> None:
    root, base = repo
    output = tmp_path / "logs"
    args = command(root, base, output)
    if preflight == "head":
        args[args.index("--expected-head") + 1] = "0" * 40
    else:
        (root / "untracked.txt").write_text("dirty")
    process = subprocess.run(args, capture_output=True, check=False)
    assert process.returncode != 0
    receipt = json.loads(next(output.glob("*/result.json")).read_text())
    assert receipt["checks"] == []
    assert receipt["status"] == "failed"


def test_missing_tool_returns_nonzero_without_later_checks(
    repo: tuple[Path, str], tmp_path: Path
) -> None:
    root, base = repo
    output = tmp_path / "logs"
    process = subprocess.run(
        [*command(root, base, output), "--node", str(tmp_path / "missing-node")],
        capture_output=True, check=False,
    )
    assert process.returncode == 127
    receipt = json.loads(next(output.glob("*/result.json")).read_text())
    assert receipt["checks"][-1] == {"name": "frontend", "returncode": 127}
    assert receipt["status"] == "failed"


def test_terminated_pytest_preserves_signal_status(repo: tuple[Path, str], tmp_path: Path) -> None:
    root, base = repo
    (root / "tests/test_one.py").write_text(
        "import os\nimport signal\n\n\ndef test_one():\n"
        "    os.kill(os.getpid(), signal.SIGTERM)\n"
    )
    commit(root, "terminate only fixture pytest process")
    process, receipt, _, followup = run_gate(root, base, tmp_path / "logs")
    assert process.returncode == 143
    assert receipt["exitcode"] == 143
    assert receipt["checks"] == [
        {"name": "secret", "returncode": 0}, {"name": "pytest", "returncode": -15}
    ]
    assert not followup.exists()


def test_unreadable_scan_base_is_failure(repo: tuple[Path, str]) -> None:
    root, _ = repo
    result = subprocess.run(
        [sys.executable, str(SCANNER), "--repo", str(root), "--base", "missing-ref"],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 2
    assert "could not read" in result.stdout
