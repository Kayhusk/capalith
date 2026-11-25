"""Write hash-pinned requirement files for more Pythons and platforms from the pins in requirements.txt.

Reads each pinned `name==version` of requirements.txt, asks PyPI's public JSON API for that exact release's
files (no download, no install), keeps the wheels that fit a target and writes every fitting wheel's SHA-256.
Nothing is resolved or upgraded: a pin with no fitting wheel for a target is reported as missing and left out.

    python tools/lock_hashes.py            # writes requirements-<target>.txt next to requirements.txt
    python tools/lock_hashes.py --check    # reports what is missing, writes nothing

Targets are CI jobs: a job is "supported" only once its file installs and the suite passes there.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GLIBC_MINOR = 39      # ubuntu-24.04 runners
MACOS_MAJOR = 15      # macos-15 (arm64) runners
TARGETS = {
    # name: (python minor, platform family)
    "linux-py312": (12, "linux-x86_64"),
    "linux-py313": (13, "linux-x86_64"),
    "macos-arm64-py311": (11, "macos-arm64"),
    "macos-arm64-py312": (12, "macos-arm64"),
}
WHEEL = re.compile(r"^(?P<name>.+?)-(?P<version>[^-]+)(?:-(?P<build>\d[^-]*))?-(?P<py>[^-]+)-(?P<abi>[^-]+)-(?P<plat>[^-]+)\.whl$")


class LockError(Exception):
    """A requirement the tool cannot read. It is never skipped: a skipped pin is a pin missing from the lock."""


NAME = r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?"
PIN = re.compile(rf"^(?P<name>{NAME})\s*==\s*(?P<version>[A-Za-z0-9][A-Za-z0-9._!+-]*)(?P<hashes>(?:\s+--hash=sha256:[0-9a-f]{{64}})*)$")
OPTIONS = ("--only-binary=:all:",)


def pins(path: Path) -> list[tuple[str, str]]:
    """Every `name==version` of a requirements file, in order.

    The packaging parser is not guaranteed where this tool runs (it runs before any install; a plain python3 has none),
    so it reads the forms pip's requirements-file format documents for an exact pin: comments, blank lines, `\\` continuations, `--hash=sha256:`
    options and `--only-binary=:all:`. Any other line (a range, extras, a marker, a URL, another option, a bare name)
    raises LockError naming the file and line.
    """
    logical: list[tuple[int, str]] = []
    pending, start = "", 0
    for number, raw in enumerate(path.read_text().splitlines(), 1):
        line = re.sub(r"(^|\s+)#.*$", "", raw).strip()
        if not pending:
            start = number
        if line.endswith("\\"):
            pending += line[:-1] + " "
            continue
        text = (pending + line).strip()
        pending = ""
        if text:
            logical.append((start, text))
    if pending.strip():
        logical.append((start, pending.strip()))
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    for number, text in logical:
        if text in OPTIONS:
            continue
        match = PIN.match(text)
        if not match:
            raise LockError(f"{path.name}: line {number}: cannot read this requirement as an exact pin (name==version): {text}")
        key = re.sub(r"[-_.]+", "-", match["name"]).lower()
        if key in seen:
            raise LockError(f"{path.name}: line {number}: {match['name']} is pinned twice")
        seen.add(key)
        found.append((match["name"], match["version"]))
    if not found:
        raise LockError(f"{path.name}: no pinned requirement found")
    return found


def python_fits(py: str, abi: str, minor: int) -> bool:
    """Whether a wheel's python and abi tags (dot-joined sets) can run on CPython 3.<minor>."""
    def one(tag: str, abi_tag: str) -> bool:
        generic = re.fullmatch(r"py3(\d*)", tag)
        if generic:       # py3 or py3N: any Python 3, or 3.N and later; never py2
            return abi_tag == "none" and (not generic[1] or int(generic[1]) <= minor)
        match = re.fullmatch(r"cp3(\d+)", tag)
        return bool(match) and (
            (abi_tag == f"cp3{minor}" and int(match[1]) == minor)
            or (abi_tag == "abi3" and int(match[1]) <= minor)
        )
    return any(one(tag, abi_tag) for tag in py.split(".") for abi_tag in abi.split("."))


def platform_fits(plat: str, family: str) -> bool:
    for tag in plat.split("."):
        if tag == "any":
            return True
        if family == "linux-x86_64":
            if tag.endswith("_x86_64") and tag.startswith("manylinux"):
                match = re.match(r"manylinux_2_(\d+)_", tag)
                if not match or int(match.group(1)) <= GLIBC_MINOR:
                    return True
        elif family == "macos-arm64":
            match = re.match(r"macosx_(\d+)_(\d+)_(arm64|universal2)$", tag)
            if match and int(match.group(1)) <= MACOS_MAJOR:
                return True
    return False


def release_files(name: str, version: str) -> list[dict]:
    url = f"https://pypi.org/pypi/{name}/{version}/json"
    with urllib.request.urlopen(url, timeout=30) as response:  # public metadata; nothing about this project is sent
        return json.load(response)["urls"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    arguments = parser.parse_args(argv)
    try:
        packages = pins(ROOT / "requirements.txt")
    except LockError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    releases = {(n, v): release_files(n, v) for n, v in packages}
    missing_any = False
    for target, (minor, family) in TARGETS.items():
        lines, missing = ["--only-binary=:all:", ""], []
        for name, version in packages:
            digests = sorted({
                f["digests"]["sha256"] for f in releases[(name, version)]
                if (m := WHEEL.match(f["filename"])) and python_fits(m["py"], m["abi"], minor) and platform_fits(m["plat"], family)
            })
            if not digests:
                missing.append(f"{name}=={version}")
                continue
            lines.append(f"{name}=={version} \\")
            lines.extend(f"    --hash=sha256:{d}" + (" \\" if i < len(digests) - 1 else "") for i, d in enumerate(digests))
            lines.append("")
        print(f"{target}: {len(packages) - len(missing)} of {len(packages)} pins have a fitting wheel" + (f"; missing: {', '.join(missing)}" if missing else ""))
        missing_any = missing_any or bool(missing)
        if not arguments.check:
            (ROOT / f"requirements-{target}.txt").write_text("\n".join(lines))
    return 1 if missing_any else 0


if __name__ == "__main__":
    sys.exit(main())
