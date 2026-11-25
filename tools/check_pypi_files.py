"""Check the release files before they go to PyPI: the same files the GitHub release holds, the right ones, the right version.

    python3 tools/check_pypi_files.py DIR TAG MCP_NAME      # for example: dist v0.2.3 io.github.Kayhusk/capalith

DIR holds what the publish workflow downloaded from the release: one wheel, one source archive and the release's SHA256SUMS.
Every problem is printed and the exit code is 1; nothing is printed and the exit code is 0 when all of these hold:
- DIR holds exactly `capalith-<version>-py3-none-any.whl`, `capalith-<version>.tar.gz` and `SHA256SUMS`, and nothing else;
- TAG is `v` plus that version;
- each of the two files has the SHA-256 the release's SHA256SUMS lists for it (other listed files, such as verification.json,
  are not downloaded and are ignored);
- the wheel's METADATA and the archive's PKG-INFO name the project `capalith` at that version and carry no "Private ::"
  classifier (PyPI would refuse the upload);
- the description (what PyPI shows, and what the MCP registry reads) holds the line `<!-- mcp-name: MCP_NAME -->` and nowhere
  `mcp-name:` glued to other characters (the registry needs a boundary after the name).
It reads files only: no network, no upload, no sign-in.
"""
from __future__ import annotations

import hashlib
import re
import sys
import tarfile
import zipfile
from email.parser import Parser
from pathlib import Path


def _sums(path: Path) -> dict[str, str]:
    found = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"([0-9a-f]{64}) [ *](\S+)", line.strip())
        if match:
            found[match[2]] = match[1]
    return found


def _metadata(text: str) -> tuple[dict[str, list[str]], str]:
    message = Parser().parsestr(text)
    fields: dict[str, list[str]] = {}
    for key, value in message.items():
        fields.setdefault(key, []).append(value)
    return fields, message.get_payload()


def _check_metadata(label: str, text: str, version: str, mcp_name: str) -> list[str]:
    problems = []
    fields, description = _metadata(text)
    if fields.get("Name") != ["capalith"]:
        problems.append(f"{label}: Name is {fields.get('Name')}, not ['capalith']")
    if fields.get("Version") != [version]:
        problems.append(f"{label}: Version is {fields.get('Version')}, not ['{version}']")
    if any(value.startswith("Private ::") for value in fields.get("Classifier", [])):
        problems.append(f"{label}: has a Private classifier; PyPI refuses the upload")
    wanted = f"<!-- mcp-name: {mcp_name} -->"
    if wanted not in description.splitlines():
        problems.append(f"{label}: the description has no line {wanted!r} (the MCP registry reads it from PyPI)")
    for glued in re.findall(r"mcp-name:\s*(\S+)", description):
        if glued != mcp_name:
            problems.append(f"{label}: an mcp-name line reads {glued!r}, not {mcp_name!r} followed by a space, a newline or -->")
    return problems


def problems(directory: Path, tag: str, mcp_name: str) -> list[str]:
    found: list[str] = []
    names = sorted(p.name for p in directory.iterdir())
    wheels = [n for n in names if re.fullmatch(r"capalith-[0-9][^-]*-py3-none-any\.whl", n)]
    archives = [n for n in names if re.fullmatch(r"capalith-[0-9].*\.tar\.gz", n)]
    if len(wheels) != 1 or len(archives) != 1 or "SHA256SUMS" not in names:
        found.append(f"expected one wheel, one source archive and SHA256SUMS; found {names}")
        return found
    version = wheels[0][len("capalith-"):-len("-py3-none-any.whl")]
    expected = {wheels[0], archives[0], "SHA256SUMS"}
    found.extend(f"unexpected file {n}" for n in names if n not in expected)
    if archives[0] != f"capalith-{version}.tar.gz":
        found.append(f"the archive {archives[0]} is not for version {version}")
    if tag != f"v{version}":
        found.append(f"the tag {tag!r} is not v{version}")
    listed = _sums(directory / "SHA256SUMS")
    for name in (wheels[0], archives[0]):
        digest = hashlib.sha256((directory / name).read_bytes()).hexdigest()
        if listed.get(name) != digest:
            found.append(f"{name} does not match SHA256SUMS" if name in listed else f"{name} is not listed in SHA256SUMS")
    try:
        with zipfile.ZipFile(directory / wheels[0]) as wheel:
            (path,) = [n for n in wheel.namelist() if n.endswith(".dist-info/METADATA")]
            found.extend(_check_metadata("wheel METADATA", wheel.read(path).decode("utf-8"), version, mcp_name))
        with tarfile.open(directory / archives[0]) as archive:
            (member,) = [m for m in archive.getmembers() if m.name.count("/") == 1 and m.name.endswith("/PKG-INFO")]
            found.extend(_check_metadata("archive PKG-INFO", archive.extractfile(member).read().decode("utf-8"), version, mcp_name))
    except (ValueError, OSError, tarfile.TarError, zipfile.BadZipFile) as error:
        found.append(f"could not read the files: {type(error).__name__}")
    return found


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__)
        return 2
    found = problems(Path(argv[0]), argv[1], argv[2])
    for line in found:
        print(line)
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
