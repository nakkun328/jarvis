# JARVIS — working rules for Claude Code

Read [docs/handoff/README.md](docs/handoff/README.md) first: it holds the continuation protocol, authority limits and the dated state snapshot.

## Non-negotiables
- **Never merge to `main` unless the maintainer names the PR number in the current conversation.** Past permissions do not carry over. CI success, Linear status and roadmap order are not permission. No direct pushes to `main`, no auto-merge, no bypassing repository rules.
- PR #31 (Gemini provider) stays Draft until the maintainer says otherwise. OpenAI live-API verification stays pending. The chat live-API standard is Gemini `gemini-2.5-flash`.
- Never put API keys, `.env`, real databases/vaults, user conversations or model weights in Git, PRs, Linear, logs or browsers. This repository is public.
- Memory contract ([docs/memory.md](docs/memory.md)): transcripts, candidates, approved long-term notes and derived vector indexes are different things; only reviewed, currently approved notes reach the LLM; the vault note is canonical, the index is a rebuildable cache.
- Evidence labels: fake / real model / live API / browser are distinct. Do not report skipped or unrunnable tests as passing.
- Linear issue `JAR-N` and GitHub PR `#N` are unrelated numbers.

## Workflow
- Dedicated branch + worktree per lane; one owner per file set. Do not reuse the maintainer's own checkout.
- Run `python scripts/verify.py --base origin/main --expected-head "$(git rev-parse HEAD)" --output-dir <outside the repo>` on a clean committed tree before pushing; see [docs/verification-gates.md](docs/verification-gates.md).
- Real E5 inference/download: one process at a time, cache outside the repo.
- Prefer a normal fast-forward push; `--force-with-lease` with the expected old SHA only on your own feature branches.
