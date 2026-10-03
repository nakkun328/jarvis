"""Scan HEAD and branch commit snapshots without printing credential values."""

import argparse
import re
import subprocess
from pathlib import Path

PATTERNS = (
    re.compile(rb"sk-(?:proj-)?[A-Za-z0-9_-]{20,}"),
    re.compile(rb"AIza[A-Za-z0-9_-]{30,}"),
    re.compile(rb"(?:ghp_|github_pat_)[A-Za-z0-9_]{30,}"),
    re.compile(rb"AKIA[0-9A-Z]{16}"),
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
)
ASSIGNMENT = re.compile(
    rb"(?im)^[ \t]*(?:export[ \t]+)?[A-Z_]*(?:API_KEY|SECRET|ACCESS_TOKEN|PASSWORD)"
    rb"[ \t]*=[ \t]*[\"']?([^\s\"']+)"
)
PLACEHOLDERS = {b"example", b"dummy", b"test", b"test-key", b"fake", b"fake-key", b"none"}


def git(repo: Path, *args: str) -> bytes:
    return subprocess.check_output(["git", "-C", str(repo), *args], stderr=subprocess.DEVNULL)


def findings(path: str, content: bytes) -> set[str]:
    reasons = set()
    name = Path(path).name
    if (name.startswith(".env") and name != ".env.example") or Path(path).suffix in {
        ".pem", ".key"
    }:
        reasons.add("sensitive filename")
    if any(pattern.search(content) for pattern in PATTERNS):
        reasons.add("credential pattern")
    for match in ASSIGNMENT.finditer(content):
        value = match.group(1)
        if value.lower() not in PLACEHOLDERS and not value.startswith(
            (b"$", b"your", b"<", b"{", b"os.", b'"', b"'", b"(")
        ):
            reasons.add("non-placeholder secret assignment")
    return reasons


def scan(repo: Path, base: str) -> int:
    head = git(repo, "rev-parse", "HEAD").decode().strip()
    base_sha = git(repo, "rev-parse", "--verify", f"{base}^{{commit}}").decode().strip()
    refs = [head, *git(repo, "rev-list", f"{base_sha}..{head}").decode().splitlines()]
    issues = 0
    for ref in dict.fromkeys(refs):
        for entry in git(repo, "ls-tree", "-r", "-z", ref).split(b"\0"):
            if not entry:
                continue
            metadata, path_bytes = entry.split(b"\t", 1)
            _, kind, object_id = metadata.split()
            if kind != b"blob":
                continue
            path = path_bytes.decode(errors="replace")
            content = git(repo, "cat-file", "blob", object_id.decode())
            for reason in sorted(findings(path, content)):
                # Never print the matching text, even for a rejected credential.
                print(f"{ref[:12]} {path!r}: {reason}")
                issues += 1
    if issues:
        print(f"Secret scan failed: {issues} findings")
        return 1
    print(f"Secret scan passed: {len(set(refs))} commit snapshots")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--base", required=True)
    args = parser.parse_args()
    try:
        return scan(args.repo.resolve(), args.base)
    except (OSError, subprocess.CalledProcessError, ValueError):
        print("Secret scan could not read the requested Git snapshots")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
