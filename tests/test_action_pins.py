"""A pinned action's commit must be the one its version comment names (no network here: the resolver is injected)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import check_action_pins as pins  # noqa: E402

GOOD = "4c48163d9f981914dd8caa0fc335fe19cca5bff4"
OTHER = "faf9eaee8f6157d3ec3616bf3ae9a5b838c9a614"
WORKFLOW = f"""
jobs:
  a:
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
      - uses: Vampire/setup-wsl@{GOOD} # v7.2.0
      - uses: ./local-action
"""


def resolver(table):
    return lambda repo, tag: table.get((repo, tag))


class ActionPinTests(unittest.TestCase):
    def test_pins_are_read_with_their_repository_commit_and_version_comment(self):
        found = pins.pinned(WORKFLOW)
        self.assertEqual([("actions/checkout", "3d3c42e5aac5ba805825da76410c181273ba90b1", "v7.0.1"), ("Vampire/setup-wsl", GOOD, "v7.2.0")],
                         [(repo, sha, tag) for repo, sha, tag, _ in found])

    def test_a_matching_workflow_has_no_problem_and_a_wrong_commit_names_the_line(self):
        table = {("actions/checkout", "v7.0.1"): "3d3c42e5aac5ba805825da76410c181273ba90b1", ("Vampire/setup-wsl", "v7.2.0"): GOOD}
        self.assertEqual([], pins.problems(WORKFLOW, resolver(table)))
        table[("Vampire/setup-wsl", "v7.2.0")] = OTHER
        found = pins.problems(WORKFLOW, resolver(table))
        self.assertEqual(1, len(found))
        self.assertIn("Vampire/setup-wsl", found[0])
        self.assertIn("v7.2.0", found[0])
        self.assertIn(OTHER, found[0])

    def test_an_unknown_tag_is_a_problem_not_a_pass(self):
        table = {("actions/checkout", "v7.0.1"): "3d3c42e5aac5ba805825da76410c181273ba90b1"}
        found = pins.problems(WORKFLOW, resolver(table))
        self.assertEqual(1, len(found))
        self.assertIn("no tag", found[0])

    def test_the_tag_is_peeled_and_a_pin_without_a_version_comment_is_refused(self):
        listing = f"e707f0b61a07f8ea48a64d83edfcbd4a7af45926\trefs/tags/v7.2.0\n{GOOD}\trefs/tags/v7.2.0^{{}}\n"
        self.assertEqual(GOOD, pins.commit_of(listing, "v7.2.0"))
        self.assertEqual(GOOD, pins.commit_of(f"{GOOD}\trefs/tags/v1\n", "v1"))      # a lightweight tag is its own commit
        self.assertIsNone(pins.commit_of("", "v7.2.0"))
        bare = "      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1\n"
        self.assertEqual(1, len(pins.problems(bare, resolver({}))))

    def test_an_unpinned_third_party_action_is_a_problem_and_a_local_one_is_not(self):
        table = {("actions/checkout", "v7.0.1"): "3d3c42e5aac5ba805825da76410c181273ba90b1", ("Vampire/setup-wsl", "v7.2.0"): GOOD}
        for line in ("someone/else@v3", "someone/else/sub/dir@main", "docker://alpine:3.20", "someone/else@" + GOOD[:12]):
            with self.subTest(line):
                found = pins.problems(WORKFLOW + f"      - uses: {line}\n", resolver(table))
                self.assertEqual(1, len(found), found)
                self.assertIn("not pinned", found[0])
                self.assertIn(line, found[0])
        self.assertEqual([], pins.problems(WORKFLOW + "      - uses: ./.github/actions/x\n", resolver(table)))

    def test_the_printed_success_is_not_printed_for_a_workflow_with_an_unpinned_action(self):
        import contextlib
        import io
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            workflow = Path(directory) / "ci.yml"
            workflow.write_text("jobs:\n  a:\n    steps:\n      - uses: someone/else@v3\n")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = pins.main([str(workflow)])
        self.assertEqual(1, code)
        self.assertNotIn("matches its version comment", out.getvalue())
        self.assertIn("not pinned", out.getvalue())

    def test_the_repository_workflow_is_readable_by_it(self):
        text = (Path(__file__).resolve().parents[1] / ".github/workflows/ci.yml").read_text()
        self.assertTrue(pins.pinned(text))
        self.assertEqual([], pins.unpinned(text), "every action in the repository workflow is a full commit pin or local")
        self.assertEqual([], [line for line in text.splitlines() if "uses:" in line and "./" not in line and not pins.USES.search(line)])


if __name__ == "__main__":
    unittest.main()
