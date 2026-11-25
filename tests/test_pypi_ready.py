"""Everything for PyPI and the MCP registry except Eddy's account: metadata, description, server.json, the publish workflow, the file check.

Nothing here uploads or signs in. What the tests pin is the part that cannot be undone once published: a filename goes to
PyPI once, the `mcp-name` line in the description is immutable per version, and the workflow stays disabled until the
maintainers set its repository variable, after the account and the publisher exist.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import tarfile
import tempfile
import tomllib
import unittest
import zipfile
from pathlib import Path

import yaml

import capalith
from tools import check_pypi_files

ROOT = Path(__file__).resolve().parents[1]
NAME = "io.github.Kayhusk/capalith"    # the GitHub login's own letter case: the registry compares it exactly (registry issue 689)
EXPECTED_CLASSIFIERS = [
    "Development Status :: 3 - Alpha",
    "Environment :: Console",
    "Intended Audience :: Developers",
    "Operating System :: POSIX :: Linux",
    "Programming Language :: Python :: 3",
    "Programming Language :: Python :: 3 :: Only",
    "Programming Language :: Python :: 3.11",
]


def project() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]


class MetadataTests(unittest.TestCase):
    def test_the_package_can_be_uploaded_and_its_classifiers_say_only_what_is_supported(self):
        classifiers = project()["classifiers"]
        self.assertNotIn("Private :: Do Not Upload", classifiers)
        self.assertEqual([c for c in classifiers if c.startswith("Private")], [])
        self.assertEqual(EXPECTED_CLASSIFIERS, classifiers)          # Linux and Python 3.11 only: what CI and the README support
        self.assertFalse([c for c in classifiers if c.startswith("License ::")], "the license is the SPDX expression")
        self.assertEqual("MIT", project()["license"])

    def test_the_classifiers_are_real_trove_classifiers(self):
        try:
            from trove_classifiers import classifiers as known
        except ImportError:
            self.skipTest("trove-classifiers is not installed here; the release check runs it")
        self.assertEqual([], [c for c in project()["classifiers"] if c not in known])

    def test_the_project_links_point_at_the_public_repository(self):
        urls = project()["urls"]
        self.assertEqual({"Homepage", "Documentation", "Source", "Changelog", "Issues"}, set(urls))
        for label, url in urls.items():
            self.assertTrue(url.startswith("https://github.com/Kayhusk/capalith"), (label, url))

    def test_the_description_file_has_no_link_that_breaks_on_pypi(self):
        text = (ROOT / project()["readme"]).read_text(encoding="utf-8")
        relative = [target for target in re.findall(r"\]\(([^)\s]+)\)", text) if not target.startswith("https://")]
        self.assertEqual([], relative, "PyPI shows this file away from the repository: every link is absolute")

    def test_the_description_names_the_registry_server_the_way_the_registry_checks(self):
        text = (ROOT / project()["readme"]).read_text(encoding="utf-8")
        lines = [line for line in text.splitlines() if "mcp-name:" in line]
        self.assertEqual([f"<!-- mcp-name: {NAME} -->"], lines)       # on its own line, a boundary after the name, exact case


class ServerJsonTests(unittest.TestCase):
    def setUp(self):
        self.server = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))

    def test_it_follows_the_registry_schema_rules_that_can_be_read_offline(self):
        server = self.server
        self.assertEqual("https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json", server["$schema"])
        self.assertEqual(NAME, server["name"])
        self.assertRegex(server["name"], r"^[a-zA-Z0-9.-]+/[a-zA-Z0-9._-]+$")
        self.assertTrue(3 <= len(server["name"]) <= 200)
        self.assertTrue(1 <= len(server["description"]) <= 100)
        self.assertTrue(1 <= len(server.get("title", "x")) <= 100)
        self.assertEqual({"url": "https://github.com/Kayhusk/capalith", "source": "github"}, server["repository"])

    def test_its_package_is_this_package_at_this_version(self):
        (package,) = self.server["packages"]
        self.assertEqual("pypi", package["registryType"])
        self.assertEqual(project()["name"], package["identifier"])
        self.assertEqual("https://pypi.org", package["registryBaseUrl"])
        self.assertEqual({"type": "stdio"}, package["transport"])
        self.assertEqual(capalith.__version__, package["version"])
        self.assertEqual(capalith.__version__, self.server["version"])
        self.assertNotRegex(package["version"], r"[\^~<>*xX]|latest")

    def test_the_registry_name_is_the_same_in_the_description_and_in_the_file(self):
        text = (ROOT / project()["readme"]).read_text(encoding="utf-8")
        self.assertIn(f"mcp-name: {self.server['name']} -->", text)


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.path = ROOT / ".github/workflows/publish-pypi.yml"
        self.text = self.path.read_text(encoding="utf-8")
        self.flow = yaml.safe_load(self.text)
        self.triggers = self.flow.get("on", self.flow.get(True))     # PyYAML reads a bare `on` as True

    def test_it_runs_only_for_a_published_release(self):
        self.assertEqual({"release": {"types": ["published"]}}, self.triggers)

    def test_it_cannot_run_before_the_owner_switches_it_on(self):
        jobs = self.flow["jobs"]
        self.assertEqual("${{ vars.PYPI_PUBLISH_ENABLED == 'true' }}", jobs["fetch"]["if"])
        self.assertEqual(["fetch"], jobs["publish"]["needs"])        # skipped with its dependency
        self.assertEqual({"name": "pypi", "url": "https://pypi.org/p/capalith"}, jobs["publish"]["environment"])

    def test_it_has_least_permissions_and_no_token(self):
        jobs = self.flow["jobs"]
        self.assertEqual({}, self.flow["permissions"])
        self.assertEqual({"contents": "read"}, jobs["fetch"]["permissions"])
        self.assertEqual({"id-token": "write"}, jobs["publish"]["permissions"])   # the one permission trusted publishing needs, on one job
        self.assertNotIn("secrets.", self.text)
        for job in jobs.values():
            for step in job["steps"]:
                self.assertFalse({"password", "username", "user", "token"} & set(step.get("with", {})), step)
        self.assertNotRegex(self.text, r"(?m)^\s*(workflow_dispatch|pull_request|push|schedule):")

    def test_every_action_is_a_full_commit_pin_and_the_publish_job_has_two_steps(self):
        from tools import check_action_pins as pins
        self.assertEqual([], pins.unpinned(self.text))
        self.assertTrue(pins.pinned(self.text))
        steps = self.flow["jobs"]["publish"]["steps"]
        self.assertEqual(2, len(steps), "download the checked files, then publish them: nothing else may hold the token")
        self.assertTrue(steps[1]["uses"].startswith("pypa/gh-action-pypi-publish@"))
        self.assertNotIn("with", steps[1])                             # no username, no password: the tokenless route


def make_release(directory: Path, version: str = "0.2.3", *, name: str = "capalith", private: bool = False,
                 description: str = f"hello\n<!-- mcp-name: {NAME} -->\n") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    metadata = f"Metadata-Version: 2.4\nName: {name}\nVersion: {version}\n" + ("Classifier: Private :: Do Not Upload\n" if private else "") + f"\n{description}"
    wheel = directory / f"capalith-{version}-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(f"capalith-{version}.dist-info/METADATA", metadata)
    sdist = directory / f"capalith-{version}.tar.gz"
    with tarfile.open(sdist, "w:gz") as archive:
        data = metadata.encode()
        info = tarfile.TarInfo(f"capalith-{version}/PKG-INFO")
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
    sums = "".join(f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}\n" for p in (wheel, sdist))
    (directory / "SHA256SUMS").write_text(sums + f"{'0' * 64}  verification.json\n")      # a listed file that is not downloaded
    return directory


class FileCheckTests(unittest.TestCase):
    def check(self, directory: Path, tag: str = "v0.2.3") -> list[str]:
        return check_pypi_files.problems(directory, tag, NAME)

    def test_a_good_release_passes(self):
        with tempfile.TemporaryDirectory() as temp:
            self.assertEqual([], self.check(make_release(Path(temp))))

    def test_a_file_that_differs_from_the_release_sums_is_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = make_release(Path(temp))
            (directory / "capalith-0.2.3.tar.gz").write_bytes(b"changed")
            self.assertTrue(any("does not match SHA256SUMS" in p for p in self.check(directory)))

    def test_a_tag_that_names_another_version_is_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            self.assertTrue(any("tag" in p for p in self.check(make_release(Path(temp)), "v0.2.4")))
            self.assertTrue(any("tag" in p for p in self.check(make_release(Path(temp) / "b", "0.2.3"), "0.2.3")))

    def test_a_missing_or_extra_file_is_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = make_release(Path(temp))
            (directory / "capalith-0.2.3.tar.gz").unlink()
            self.assertTrue(self.check(directory))
            directory = make_release(Path(temp) / "b")
            (directory / "extra.txt").write_text("x")
            self.assertTrue(any("unexpected file" in p for p in self.check(directory)))

    def test_the_wrong_project_the_private_classifier_and_a_missing_registry_line_are_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.assertTrue(any("Name" in p for p in self.check(make_release(root / "a", name="other"))))
            self.assertTrue(any("Private" in p for p in self.check(make_release(root / "b", private=True))))
            self.assertTrue(any("mcp-name" in p for p in self.check(make_release(root / "c", description="no line\n"))))
            self.assertTrue(any("mcp-name" in p for p in self.check(make_release(root / "d", description=f"<!-- mcp-name: {NAME}. -->\n"))))


if __name__ == "__main__":
    unittest.main()
