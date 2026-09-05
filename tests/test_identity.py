import hashlib
import os
import signal
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from capalith.identity import BundleEntryError, FileRecord, artifact_id, hash_bundle
from tests.helpers import make_skill


class FifoInspectionTimeout(RuntimeError):
    pass


class IdentityTests(unittest.TestCase):
    def test_bundle_digest_is_location_independent_and_covers_linked_resources(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            first_bundle = make_skill(temp / "first")
            second_bundle = make_skill(temp / "second")

            first_manifest = hash_bundle(first_bundle)
            second_manifest = hash_bundle(second_bundle)
            self.assertEqual(first_manifest, second_manifest)
            self.assertEqual(
                "sha256:31d847e2a557ca484f238eddcb2e71f4d7ee09b7096152fb8127248e9832995a",
                first_manifest.digest,
            )

            skill_bytes = b"---\nname: demo\ndescription: Test skill.\n---\n\n# Test\n"
            resource_bytes = b"v1"
            self.assertEqual(
                (
                    FileRecord(
                        path="SKILL.md",
                        executable=False,
                        size=len(skill_bytes),
                        sha256=f"sha256:{hashlib.sha256(skill_bytes).hexdigest()}",
                    ),
                    FileRecord(
                        path="references/data.bin",
                        executable=False,
                        size=len(resource_bytes),
                        sha256=f"sha256:{hashlib.sha256(resource_bytes).hexdigest()}",
                    ),
                ),
                first_manifest.files,
            )
            self.assertEqual(len(skill_bytes) + len(resource_bytes), first_manifest.byte_count)

            (second_bundle / "references" / "data.bin").write_bytes(b"v2")
            self.assertNotEqual(first_manifest.digest, hash_bundle(second_bundle).digest)

        source_id = "src_00000000000000000000000000000000"
        first_artifact_id = artifact_id(source_id, "demo")
        self.assertEqual(first_artifact_id, artifact_id(source_id, "demo"))
        self.assertNotEqual(
            first_artifact_id,
            artifact_id("src_11111111111111111111111111111111", "demo"),
        )
        self.assertEqual(
            "art_fa21ae19aab7e142ceec45f9795b4e55129f2e683301ba4437c4036f3171de23",
            first_artifact_id,
        )

    def test_bundle_digest_rejects_unsafe_entries_and_directory_swaps(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            bundle = make_skill(Path(temp_dir))
            unsupported = bundle / "references" / "unsupported"
            unsupported.symlink_to("data.bin")
            with self.assertRaises(BundleEntryError):
                hash_bundle(bundle)

            unsupported.unlink()
            os.mkfifo(unsupported)

            def timeout_if_blocked(_signum: int, _frame: object) -> None:
                raise FifoInspectionTimeout("FIFO inspection blocked")

            previous_handler = signal.signal(signal.SIGALRM, timeout_if_blocked)
            signal.setitimer(signal.ITIMER_REAL, 1.0)
            try:
                with self.assertRaises(BundleEntryError):
                    hash_bundle(bundle)
            finally:
                signal.setitimer(signal.ITIMER_REAL, 0.0)
                signal.signal(signal.SIGALRM, previous_handler)

        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            bundle = make_skill(temp / "bundle")
            outside = temp / "outside"
            outside.mkdir()
            outside_bytes = b"outside"
            (outside / "data.bin").write_bytes(outside_bytes)
            references = bundle / "references"
            saved_references = bundle / "references-original"
            real_open = os.open
            real_read = os.read
            replaced_directory = False
            read_chunks: list[bytes] = []

            def open_with_replaced_directory(
                path: os.PathLike[str] | os.PathLike[bytes] | str | bytes,
                flags: int,
                mode: int = 0o777,
                *,
                dir_fd: int | None = None,
            ) -> int:
                nonlocal replaced_directory
                name = Path(os.fsdecode(os.fspath(path))).name
                if not replaced_directory and name == "data.bin":
                    references.rename(saved_references)
                    references.symlink_to(outside, target_is_directory=True)
                    replaced_directory = True
                return real_open(path, flags, mode, dir_fd=dir_fd)

            def read_observed(descriptor: int, size: int) -> bytes:
                chunk = real_read(descriptor, size)
                if chunk:
                    read_chunks.append(chunk)
                return chunk

            with (
                patch("capalith.identity.os.open", side_effect=open_with_replaced_directory),
                patch("capalith.identity.os.read", side_effect=read_observed),
            ):
                with self.assertRaises(BundleEntryError):
                    hash_bundle(bundle)
            self.assertTrue(replaced_directory)
            self.assertFalse(any(outside_bytes in chunk for chunk in read_chunks))


if __name__ == "__main__":
    unittest.main()
