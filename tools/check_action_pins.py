"""Check that every pinned GitHub Action's commit is the one its version comment names.

    python3 tools/check_action_pins.py [workflow ...]     # default: .github/workflows/*.yml

Reads `uses: owner/repo@<40-hex commit> # vX.Y.Z` lines, asks the action's own repository (`git ls-remote`, public,
read only) for that tag, peels it to its commit and compares. A pin with no version comment, an unknown tag or a
different commit is a problem, and so is any `uses:` that is not a full commit pin (a tag, a branch, a short hash, a
docker image); only a local `./path` action is exempt. The exit code is then 1. The logic takes the resolver as an argument so tests run offline.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from typing import Callable

USES = re.compile(r"uses:\s*(?P<repo>[\w.-]+/[\w.-]+)(?:/[\w./-]+)?@(?P<sha>[0-9a-f]{40})(?:\s*#\s*(?P<tag>v[\w.]+))?")


def pinned(text: str) -> list[tuple[str, str, str | None, int]]:
    found = []
    for number, line in enumerate(text.splitlines(), 1):
        match = USES.search(line)
        if match:
            found.append((match["repo"], match["sha"], match["tag"], number))
    return found


def commit_of(listing: str, tag: str) -> str | None:
    """The commit a tag points at, from `git ls-remote` output: the peeled entry when the tag is annotated."""
    plain = peeled = None
    for line in listing.splitlines():
        sha, _, ref = line.partition("\t")
        if ref == f"refs/tags/{tag}^{{}}":
            peeled = sha
        elif ref == f"refs/tags/{tag}":
            plain = sha
    return peeled or plain


ANY_USES = re.compile(r"^\s*(?:-\s*)?uses:\s*(?P<ref>[^\s#]+)")


def unpinned(text: str) -> list[tuple[str, int]]:
    """Every `uses:` that is neither a local action nor a full commit pin."""
    found = []
    for number, line in enumerate(text.splitlines(), 1):
        match = ANY_USES.match(line)
        if match and not match["ref"].startswith("./") and not USES.search(line):
            found.append((match["ref"], number))
    return found


def problems(text: str, resolve: Callable[[str, str], str | None]) -> list[str]:
    found = [f"line {number}: {ref} is not pinned to a full commit" for ref, number in unpinned(text)]
    for repo, sha, tag, number in pinned(text):
        if tag is None:
            found.append(f"line {number}: {repo}@{sha} has no version comment to check it against")
            continue
        real = resolve(repo, tag)
        if real is None:
            found.append(f"line {number}: {repo} has no tag {tag}")
        elif real != sha:
            found.append(f"line {number}: {repo}@{sha} is not {tag}; {tag} is {real}")
    return found


def ls_remote(repo: str, tag: str) -> str | None:
    done = subprocess.run(["git", "ls-remote", f"https://github.com/{repo}", f"refs/tags/{tag}", f"refs/tags/{tag}^{{}}"],
                          capture_output=True, text=True, timeout=60, check=True)
    return commit_of(done.stdout, tag)


def main(arguments: list[str]) -> int:
    root = Path(__file__).resolve().parents[1]
    files = [Path(a) for a in arguments] or sorted((root / ".github/workflows").glob("*.yml"))
    bad = []
    for file in files:
        bad.extend(f"{file.name}: {problem}" for problem in problems(file.read_text(), ls_remote))
    print("\n".join(bad) if bad else f"every action is pinned to a commit that matches its version comment ({len(files)} workflow file(s))")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
