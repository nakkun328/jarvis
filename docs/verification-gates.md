# Local verification gates

Install Python 3.11+, Git, Node.js 22, and `pip install -e '.[dev,vector]'`. The vector extra is required for a complete run: skipped pytest cases are recorded separately and make this gate fail. Frontend skips and TODO cases also fail the gate, even when Node exits zero. Tests use disposable data and fake providers/embeddings; no real API credentials are required. The runner removes OpenAI/Gemini keys and memory-vault configuration from child processes, selects provider `none`, and directs the default database into the run directory.

Commit the reviewed changes first, then run from a clean worktree:

```sh
python scripts/verify.py --base origin/main \
  --expected-head "$(git rev-parse HEAD)" \
  --output-dir /private/tmp/jarvis-verification
```

The command never pushes. Its exit status must be zero before a caller can proceed. It runs Secret scan first, then pytest, Ruff, compileall, frontend tests, JavaScript syntax checks, and worktree/branch whitespace checks. Scanning committed snapshots before other tools prevents known credential patterns from reaching their source-line diagnostics. Each command's actual exit status is recorded and the first failure stops later checks. A missing command also fails. No shell pipelines, `tee`, success-marker parsing, or exception-to-success fallback are used.

Each run gets a new `<full-SHA>-<run-ID>` directory outside the worktree. An explicit `--run-id` may be supplied for a known operation; an existing directory is rejected without changing old logs. Each check owns a separate log. `result.json` is written atomically only after the run has reached its terminal state; a running or interrupted check may leave logs or an XML file without a completed result. Those files and any success text inside them do not authorize a push.

A successful result records the expected HEAD, resolved base SHA, initial/final HEAD and clean status, command return codes, pytest and frontend passed/skipped/failed counts, and run ID. Frontend counts come from Node's JUnit reporter in `frontend.log`; TODO cases are reported as skipped. A changed HEAD or dirty final worktree invalidates the run even if every command succeeded. Read the actual command exit status and this specific run's completed result together. Before any separately authorized push, confirm the HEAD is still the recorded SHA and the worktree remains clean; changes after verification require a new run. This script supplies evidence, not a Git locking or remote publication mechanism.

Secret scan checks the current committed snapshot and every commit in `base..HEAD`, so a credential added and later removed on the branch still fails. It rejects known credential/private-key patterns, non-placeholder environment-style secret assignments, and tracked `.env*` (except `.env.example`), `.pem`, and `.key` files. Reports contain only commit/path/reason, never matching values. It does not scan ignored personal files, history already in the supplied base, submodule repositories, or detect every possible secret. Keep real credentials out of commits and logs independently of this check.

The gate regression tests create tiny disposable Git repositories, run real pytest/Ruff/Git/Secret scan/frontend failures, and check that a simulated follow-up file is never created. They also exercise signal termination, missing tools, reused run IDs, changed/dirty HEAD, skipped prerequisites (including frontend skips and TODO cases), removed dummy credentials, and observation while a test is still running. No test pushes or stops unrelated processes:

```sh
python -m pytest -q tests/test_verification_gates.py
```
